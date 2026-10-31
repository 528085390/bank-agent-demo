"""The life event domain: its declaration, its tools and its own graph.

生日链路：life_birthdays 读联系人，life_gift_plan 挑礼物，life_prepare_order 冻结订单
快照并挂确认卡；用户点"确认"后由本域图直连 life_execute_order 提交（验证码走
TurnContext，不进工具参数）。life_cancel_order 同样挂卡，确认后才经资金写门面撤销。

装配由本包的 `bind_life_event` 完成：数据库绑进 `life_prepare_order`（确认卡要先读当日
累计，否则按 0 算），写门面绑进图构建器与 `life_execute_order` / `life_cancel_order`
（下单与撤销都要过门面的日限额预检、审计与记账）。两个只读工具不需要这两样。
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from app.agents.declaration import DomainDeclaration, FieldMeta
from app.modules.base import ModuleDeps, ModuleSpec, bind_writes
from app.modules.life_event.tools import HANDLERS, TOOLS, execute_order, prepare_order
from app.modules.life_event.tools import cancel as cancel_order
from app.modules.life_event.workflow import build_life_event_graph

LIFE_EVENT = DomainDeclaration(
    agent_id="life_event",
    display="生活事件 Agent",
    purpose="处理生日、节日、生活提醒等事件相关的请求；生日礼物的选礼、下单与付款都在本域完成",
    fields={
        "contact_id": FieldMeta(label="生日联系人"),
        "product_code": FieldMeta(label="礼物", type="select", options=("flowers", "cake")),
        "from_account_id": FieldMeta(label="付款账户"),
        "birthday_date": FieldMeta(label="生日日期"),
    },
    graph_builder=build_life_event_graph,
)


def bind_life_event(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """装配生活事件要的两样运行时依赖：图构建器的写门面，以及确认卡的数据库。

    下单是资金写：确认卡要先读当日累计，所以 `life_prepare_order` 得拿到组合根的数据库；
    点击后要过门面的日限额预检与记账，撤销同理，所以 `life_execute_order` 与
    `life_cancel_order` 得拿到同一个写门面。`HANDLERS` 是 import 期的模块级常量
    （`modules/life_event/tools.py`），改不动它，只能在装配时替换这三个键，其余 handler
    原样保留。

    组合根只把 `ModuleDeps` 交进来（`modules/__init__.py` 的 `production_modules`），
    它不认识生活事件需要什么 —— 那是本函数的事。
    """

    bound = bind_writes(module, deps)
    return replace(
        bound,
        handlers={
            **bound.handlers,
            "life_prepare_order": partial(prepare_order, database=deps.database),
            "life_execute_order": partial(execute_order, writes=deps.writes),
            "life_cancel_order": partial(cancel_order, writes=deps.writes),
        },
    )


MODULE = ModuleSpec(
    name="life_event",
    declaration=LIFE_EVENT,
    tools=TOOLS,
    handlers=HANDLERS,
    binder=bind_life_event,
)
