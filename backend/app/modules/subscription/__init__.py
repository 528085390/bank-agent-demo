"""订阅领域：查询工具、冻结快照的写工具与自有图。

模块带着七个工具进场：两个只读工具（订阅列表、订阅详情）、四个写工具
（取消／暂停／恢复／代扣），以及只对模型隐藏的执行工具 `subscription_execute`。
整轮处理交给自己的领域图（`graph_builder=build_subscription_graph`）。它**不再声明
`turn`** —— `ModuleSpec` 不允许一次请求被两条路径回答。

写工具只冻结快照并弹确认卡，不会调用银行的写接口；只有确认卡被点击后由
`subscription_execute` 打银行，且它从不出现在交给模型的工具清单里。

装配由本包的 `bind_subscription` 完成：写门面绑进图构建器与 `subscription_execute`，
数据库绑进 `subscription_charge`（扣款要读当日累计）。取消／暂停／恢复不是资金写，
不需要这两样。
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from app.agents.declaration import DomainDeclaration, FieldMeta
from app.modules.base import ModuleDeps, ModuleSpec, bind_writes
from app.modules.subscription.tools import HANDLERS, TOOLS
from app.modules.subscription.tools import execute as execute_subscription
from app.modules.subscription.tools import prepare as prepare_subscription
from app.modules.subscription.workflow import build_subscription_graph

SUBSCRIPTION = DomainDeclaration(
    agent_id="subscription",
    display="订阅 Agent",
    purpose="处理订阅查询、识别、续费提醒与取消/暂停/恢复/代扣相关的请求",
    fields={
        "subscription_id": FieldMeta(label="订阅"),
        "merchant_name": FieldMeta(label="商户名称"),
        "status": FieldMeta(
            label="订阅状态", type="select", options=("active", "paused", "cancelled")
        ),
    },
    graph_builder=build_subscription_graph,
)


def bind_subscription(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """装配订阅要的两样运行时依赖：图构建器的写门面，以及扣款预检的数据库。

    扣款是资金写：确认卡要先读当日累计，所以 `subscription_charge` 得拿到组合根的数据库；
    点击后要过门面的日限额预检与记账，所以 `subscription_execute` 得拿到同一个写门面。
    `HANDLERS` 是 import 期的模块级常量（`modules/subscription/tools.py`），改不动它，
    只能在装配时替换这两个键，其余 handler 原样保留。

    组合根只把 `ModuleDeps` 交进来（`modules/__init__.py` 的 `production_modules`），
    它不认识订阅需要什么 —— 那是本函数的事。
    """

    bound = bind_writes(module, deps)
    return replace(
        bound,
        handlers={
            **bound.handlers,
            "subscription_charge": partial(prepare_subscription, database=deps.database),
            "subscription_execute": partial(execute_subscription, writes=deps.writes),
        },
    )


MODULE = ModuleSpec(
    name="subscription",
    declaration=SUBSCRIPTION,
    tools=TOOLS,
    handlers=HANDLERS,
    binder=bind_subscription,
)
