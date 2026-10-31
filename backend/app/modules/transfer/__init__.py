"""转账领域：工具、自有图，以及一次点击才发生的那一次写。

这个模块声明十个平台工具（账户、联系人、定时转账查询、即时/定时意图解析、取消定时转账、
AA 收款发起/查进度/付份额/取消），并把整轮交给自己的领域图（`graph_builder=build_transfer_graph`）。
它**不声明 `turn`** —— `ModuleSpec` 不允许一次请求被两条路径回答。

- 确认卡是平台的 `kind="confirmation"`：`transfer_prepare` 只解析，图把冻结的意图挂成一张卡；
- 验证码由平台经 `graph_builder` 的 `turn` 关键字到达模块图，从不进 state／检查点／账本／SSE；
- `POST /transfers` 由 `writes.py` 在用户点击确认之后发起（不走网关 —— 网关没有验证码通道）；
- `transfer_cancel` / `transfer_aa_cancel` 对模型可见，但图一律先把它拦成确认卡，点击后才经网关执行；
- 装配由本包的 `bind_transfer` 完成：写门面绑进图构建器，数据库绑进 `transfer_prepare`。

定时转账创建走 `transfer_schedule`：工具层只解析并冻结 execute_at，用户点击确认后才由
`writes.create_scheduled` 创建银行任务单；创建任务单不移动资金，因此不占当日累计。

AA 收款走 `transfer_aa`：发起只生成收款请求（钱向内收），黄标确认、不占当日累计；
`transfer_aa_pay` 支付我的那一份才是真资金写，`aa_payment` 受 单笔+当日累计 日限额动态分级；
`transfer_aa_cancel` 与定时转账取消同构，拦卡后经网关执行。
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from app.agents.declaration import DomainDeclaration, FieldMeta
from app.modules.base import ModuleDeps, ModuleSpec, bind_writes
from app.modules.transfer.tools import HANDLERS, TOOLS
from app.modules.transfer.tools import aa_pay as pay_aa_share
from app.modules.transfer.tools import prepare as prepare_transfer
from app.modules.transfer.tools import schedule_prepare as schedule_transfer
from app.modules.transfer.workflow import build_transfer_graph

TRANSFER = DomainDeclaration(
    agent_id="transfer",
    display="转账 Agent",
    purpose="处理转账、汇款、收款人相关的请求；转账由代码要求确认或验证码",
    fields={
        "amount_yuan": FieldMeta(label="转账金额"),
        "to_name": FieldMeta(label="收款人"),
        "from_hint": FieldMeta(label="付款账户"),
        "execute_date": FieldMeta(label="执行日期"),
        "execute_time": FieldMeta(label="执行时间"),
        "remark": FieldMeta(label="备注"),
        "status": FieldMeta(
            label="定时转账状态",
            type="select",
            options=("pending", "completed", "cancelled", "failed"),
        ),
        "participants": FieldMeta(label="AA 参与人"),
        "participant_count": FieldMeta(label="AA 总人数"),
        "aa_request_id": FieldMeta(label="AA 收款编号"),
    },
    graph_builder=build_transfer_graph,
)


def bind_transfer(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """装配转账要的两样运行时依赖：图构建器的写门面，以及读日累计工具的数据库。

    确认卡判定要读当日累计金额，所以 `transfer_prepare` 与 `transfer_aa_pay`（AA 分摊
    支付是真资金写，同样按日累计动态分级）得拿到组合根的数据库；`HANDLERS` 是 import 期
    的模块级常量（`modules/transfer/tools.py`），改不动它，只能在装配时替换这几个键，
    其余 handler 原样保留。

    组合根只把 `ModuleDeps` 交进来（`modules/__init__.py` 的 `production_modules`），
    它不认识转账需要什么 —— 那是本函数的事。
    """

    bound = bind_writes(module, deps)
    return replace(
        bound,
        handlers={
            **bound.handlers,
            "transfer_prepare": partial(prepare_transfer, database=deps.database),
            "transfer_schedule": partial(schedule_transfer, database=deps.database),
            "transfer_aa_pay": partial(pay_aa_share, database=deps.database),
        },
    )


MODULE = ModuleSpec(
    name="transfer",
    declaration=TRANSFER,
    tools=TOOLS,
    handlers=HANDLERS,
    binder=bind_transfer,
)
