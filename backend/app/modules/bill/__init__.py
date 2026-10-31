"""bill 模块：声明 + 工具装配。

`MODULE` 是 import 期的那一份 —— 它**不声明任何工具**：`app/agents/domain.py:23` 在
import 期就跑 `build_registry().roster`，模块自己带工具会把服务依赖（数据库、
`NotificationService`）拖进 import 链。真正带工具的 bill 模块由本包的 `bind_bill` 装配：
组合根（`app/modules/__init__.py` 的 `production_modules`）只把 `ModuleDeps` 交给它，
它再调 `build_bill_module`。

R2：`graph_builder` 指向 `build_bill_graph`（`bill/workflow.py`）—— 账单声明了业务工具，
就必须自带图，否则 supervisor 会拒绝它（`app/agents/supervisor/graph.py:427-434`）。
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from app.agents.declaration import DomainDeclaration, FieldMeta
from app.modules.base import ModuleDeps, ModuleSpec
from app.modules.bill.tools import TOOLS, build_handlers
from app.modules.bill.workflow import build_bill_graph

BILL = DomainDeclaration(
    agent_id="bill",
    display="账单 Agent",
    # 最后那句只出现在总调度菜单里 bill 这一行：让「用户明确要求分析」总能派进来。
    # 用领域自己的声明表达，而不是去改总调度那份共享提示词。
    purpose=(
        "处理账单查询、账单分析、消费分类相关的请求。"
        "用户明确要求分析账单 / 流水时一律派给本领域，不要用之前的分析结论代替；"
        "本领域会自己判断要不要重新计算。"
    ),
    fields={
        # 用户说「分析流水」时先问这个 —— 但「查 / 看流水」不触发它，那条路直接查明细。
        "intent": FieldMeta(
            label="你想做什么",
            type="select",
            options=("上传新的流水文件", "查询已导入的账单"),
        ),
        "period_start": FieldMeta(
            label="开始日期",
            type="date",
            required=False,  # 只在「查询已导入」时需要，不是全局必填
        ),
        "period_end": FieldMeta(
            label="结束日期",
            type="date",
            required=False,  # 只在「查询已导入」时需要，不是全局必填
        ),
        # 分类是可选筛选条件，不是必填——追问它属于「追问可选信息」，提示词已禁止。
        "category": FieldMeta(label="分类", type="text", required=False),
    },
    graph_builder=build_bill_graph,
)


def build_bill_module(*, notification_service=None, database=None) -> ModuleSpec:
    """装配带工具的 bill 模块：声明 + 依赖注入后的 handler。

    Args:
        notification_service: 可选的 NotificationService。给了才登记邮件工具
            （`bill_send_report`）。
        database: 可选的数据库句柄。给了才登记明细查询与深度分析
            （`bill_query_transactions` / `bill_analyze_comprehensive`）。

    Returns:
        ModuleSpec: 带 `bill_` 前缀工具的 bill 模块。
    """
    handlers = build_handlers(database=database, notification_service=notification_service)
    # 把数据库绑进图构建器：范围清洗要查「上一条分析成没成功」。写法与 base.py 给
    # transfer/wealth 绑 writes 一致 —— 用 partial 而不是改全局声明。
    declaration = (
        replace(BILL, graph_builder=partial(BILL.graph_builder, database=database))
        if database is not None
        else BILL
    )
    return ModuleSpec(
        name="bill",
        declaration=declaration,
        # 只声明「这次装配真的配上了 handler」的那些工具：`ModuleSpec.__post_init__`
        # 强制 tools 与 handlers 键集相等（modules/base.py:124-129），缺依赖时把声明
        # 一并摘掉，而不是构造一个立刻抛错的模块 —— 与 dev 的 `build_module` 一致
        # （工具跟着 handler 走）。
        tools={name: item for name, item in TOOLS.items() if name in handlers},
        handlers=handlers,
    )


def bind_bill(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """账单的装配就是 `build_bill_module`：通知服务给发信，数据库给查询与分析。

    `MODULE` 本身不带工具（见文件头），所以 binder 是唯一给它工具的东西 —— 少了它，账单
    域在注册表里就是一个没有工具的空壳，而且从注册表那一侧看不出来。
    """

    return build_bill_module(notification_service=deps.notification_service, database=deps.database)


# import 期的那一份：不带工具、不带 handler；带工具的那一份由 `bind_bill` 装配。
MODULE = ModuleSpec(name="bill", declaration=BILL, binder=bind_bill)
