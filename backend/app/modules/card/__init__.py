"""Card management tools with bank-owned authorization and a deterministic workflow."""

from __future__ import annotations

from app.agents.declaration import DomainDeclaration, FieldMeta
from app.modules.base import ModuleSpec
from app.modules.card.tools import HANDLERS, TOOLS
from app.modules.card.workflow import build_card_graph

CARD = DomainDeclaration(
    agent_id="card",
    display="卡片 Agent",
    purpose="处理银行卡查询、申请、日/月交易限额、冻结、解冻、商户黑名单、挂失、解挂及交易密码修改；密码只在独立表单填写，写操作由代码要求确认或验证",
    fields={
        "card_suffix": FieldMeta(label="卡号末四位", type="text"),
        "card_type": FieldMeta(
            label="卡类型", type="select", options=("debit", "credit", "virtual")
        ),
        "daily_limit_yuan": FieldMeta(label="日交易限额（元）", type="text"),
        "monthly_limit_yuan": FieldMeta(label="月交易限额（元）", type="text"),
        "merchant_name": FieldMeta(label="商户名称"),
        "mode": FieldMeta(
            label="名单修改方式", type="select", options=("add", "remove", "replace")
        ),
    },
    graph_builder=build_card_graph,
)

MODULE = ModuleSpec(name="card", declaration=CARD, tools=TOOLS, handlers=HANDLERS)
