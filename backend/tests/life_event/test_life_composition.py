"""Task 4: 生活事件域自己装配运行时依赖，组合根只递一把 `ModuleDeps`。

三条钉子：

1. `MODULE.binder` 就是 `bind_life_event` —— 装配是模块自己的事，组合根
   （`modules/__init__.py` 的 `production_modules`）不认识任何一个域；
2. 组合根把**同一把**写门面与数据库绑进图构建器与三个 handler：`life_prepare_order`
   拿数据库（确认卡要先读当日累计），`life_execute_order` / `life_cancel_order` 拿写门面
   （下单与撤销都要过门面的日限额预检、审计与记账）；
3. 读的是 binder 注入的那份数据库，而不是 `database=None` 的降级路径：真写 90000 分当日
   累计后，一笔 19900 分的生日单应当被抬成红标（90000 + 19900 > 100000 分日限额）。
"""

from __future__ import annotations

import functools
from datetime import date, datetime

import pytest

from app.core.clock import CST
from app.modules import production_modules
from app.modules.life_event import MODULE, bind_life_event
from app.modules.life_event import tools as life_tools
from app.modules.registry import build_registry
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

APP_USER = "user_0001"
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
NOW = datetime(2031, 3, 4, 9, 0, tzinfo=CST)

PARTNER = {
    "contact_id": "c_partner",
    "user_id": CUSTOMER,
    "name": "爱人",
    "phone": "13800138002",
    "remark": "家人",
    "recipient_user_id": "u_partner",
    "recipient_account_id": "a_partner_current",
    "birthday": "1995-09-25",
}
PRODUCTS = [
    {"product_code": "flowers", "name": "模拟鲜花", "price_minor": 19900, "currency": "CNY"},
    {"product_code": "cake", "name": "模拟蛋糕", "price_minor": 12900, "currency": "CNY"},
]
CURRENT_ACCOUNT = {
    "account_id": "a_zhang_current",
    "user_id": CUSTOMER,
    "account_type": "current",
    "balance_minor": 2465950,
    "available_minor": 2465950,
    "currency": "CNY",
    "interest_rate": "0.0025",
    "status": "active",
}


class _FixedClock:
    @staticmethod
    def today(client=None) -> date:
        return BUSINESS_DAY

    @staticmethod
    def now(client=None) -> datetime:
        return NOW


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(life_tools, "SystemClock", _FixedClock)
    return _FixedClock


def bank() -> FakeBankClient:
    client = FakeBankClient()
    client.register("/birthdays", [PARTNER])
    client.register("/mock-products", PRODUCTS)
    client.register("/accounts", [CURRENT_ACCOUNT])
    return client


def _invocation(tool: str, arguments: dict | None = None):
    return ToolInvocation(
        "life_event",
        ToolCall(
            tool_name=tool,
            arguments=arguments or {},
            call_id="call_life_comp_1",
            idempotency_key="idem-life-comp-1",
        ),
        TrustedContext(user_id=APP_USER, session_id="sess_life", bank_customer_id=CUSTOMER),
    )


def assembled_life_event(rt):
    """组合根装配出来的那一个 `life_event` 模块。"""

    registry = build_registry(
        production_modules(
            notification_service=rt.notification,
            database=rt.database,
            writes=rt.writes,
        )
    )
    return next(module for module in registry.modules if module.name == "life_event")


# ------------------------------------------------------------------ 声明


def test_life_event_declares_its_binder():
    # 不声明 binder，组合根就不会把 database / writes 交给它：确认卡按 0 算当日累计，
    # 下单与撤销也拿不到写门面，而这从注册表那一侧完全看不出来。
    assert MODULE.binder is bind_life_event


# ------------------------------------------------------------------ 组合根接线


def test_the_composition_root_injects_database_and_writes(rt):
    module = assembled_life_event(rt)

    builder = module.declaration.graph_builder
    assert isinstance(builder, functools.partial), "life_event 的图构建器没有被包一层"
    assert builder.keywords["writes"] is rt.writes, "图构建器拿到的不是组合根那把门面"

    for name in ("life_prepare_order", "life_execute_order", "life_cancel_order"):
        assert isinstance(module.handlers[name], functools.partial), f"{name} 没有被装配"
    assert module.handlers["life_prepare_order"].keywords["database"] is rt.database
    assert module.handlers["life_execute_order"].keywords["writes"] is rt.writes
    assert module.handlers["life_cancel_order"].keywords["writes"] is rt.writes
    # 只读工具不需要这两样：它们不进 handler 的装配，原样留在 HANDLERS 里。
    assert module.handlers["life_birthdays"] is MODULE.handlers["life_birthdays"]
    assert module.handlers["life_gift_plan"] is MODULE.handlers["life_gift_plan"]


# ------------------------------------------------------------------ 当日累计


def test_a_real_daily_total_escalates_prepare_to_red(rt, frozen_clock):
    """90000 分存量 + 19900 分生日单越过 100000 分日限额，确认卡必须抬成红标。

    走的是 binder 注入的 `rt.database`：如果没有 binder，`prepare_order` 会退回
    `database=None` 的降级路径（当日累计按 0 算），这张卡就只是黄标。
    """

    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 90000)

    data = assembled_life_event(rt).handlers["life_prepare_order"](
        _invocation(
            "life_prepare_order",
            {"contact_id": "c_partner", "product_code": "flowers"},
        ),
        bank(),
    )

    assert data.facts["intent"]["amount_minor"] == 19900
    assert data.facts["risk_level"] == "red"
    assert data.facts["requires_code"] is True
    assert "超过日限额" in data.facts["reason"]
