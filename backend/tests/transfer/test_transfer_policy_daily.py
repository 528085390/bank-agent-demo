"""`transfer_prepare` 的红黄标来自统一策略引擎，而不是模块里的金额阈值。

四条用例覆盖：空库判黄、日累计升红并留一条审计、日累计读失败 Fail-Closed、组合根把
database 绑进 `transfer_prepare`。业务日固定，避免 `SystemClock.today()` 去问银行。
"""

from __future__ import annotations

from datetime import date
from functools import partial

import pytest

from app.modules import production_modules
from app.modules.transfer import tools as transfer_tools
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

APP_USER = "user_0001"
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)

ACCOUNTS = [
    {
        "account_id": "a_zhang_current",
        "user_id": CUSTOMER,
        "account_type": "current",
        "balance_minor": 832000,
        "available_minor": 800000,
        "currency": "CNY",
        "interest_rate": 0.0,
        "status": "active",
    }
]

CONTACTS = [
    {
        "contact_id": "c_1",
        "user_id": CUSTOMER,
        "name": "老王",
        "phone": "13800138000",
        "remark": None,
        "recipient_user_id": "u_partner",
        "recipient_account_id": "a_partner_current",
        "birthday": None,
    }
]


class _FixedClock:
    """固定业务日：真实的 `SystemClock.today()` 会去问银行，测试不该有网络。"""

    @staticmethod
    def today(client=None):
        return BUSINESS_DAY


@pytest.fixture
def business_day(monkeypatch):
    monkeypatch.setattr(transfer_tools, "SystemClock", _FixedClock)


def bank():
    fake = FakeBankClient()
    fake.register("/accounts", ACCOUNTS)
    fake.register("/contacts", CONTACTS)
    return fake


def prepare_invocation(amount_yuan):
    return ToolInvocation(
        "transfer",
        ToolCall(
            tool_name="transfer_prepare",
            arguments={"amount_yuan": amount_yuan, "to_name": "老王", "from_hint": "活期"},
            call_id="c_1",
        ),
        TrustedContext(user_id=APP_USER, session_id="sess_1", bank_customer_id=CUSTOMER),
    )


def test_a_small_transfer_is_yellow_when_nothing_accumulated_today(rt, business_day):
    data = transfer_tools.prepare(prepare_invocation("600.00"), bank(), database=rt.database)

    assert data.facts["requires_code"] is False
    assert data.facts["risk_level"] == "yellow"
    assert "验证码" not in data.summary


def test_a_daily_accumulation_escalates_to_red_and_leaves_one_audit_row(rt, business_day):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 60000)

    data = transfer_tools.prepare(prepare_invocation("600.00"), bank(), database=rt.database)

    assert data.facts["requires_code"] is True
    assert data.facts["risk_level"] == "red"
    assert "超过日限额" in data.facts["reason"]
    assert "需要验证码" in data.summary

    rows = [
        item
        for item in rt.audit.reader.business_events(APP_USER)["items"]
        if item["action"] == "transfer_prepare"
    ]
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "policy_decision"
    assert rows[0]["resource_id"] == "red"
    assert "超过日限额" in rows[0]["reason"]


def test_a_failed_daily_total_read_fails_closed(rt, business_day, monkeypatch):
    def boom(session, user_id, business_date):
        raise RuntimeError("daily total store unavailable")

    monkeypatch.setattr(DailyTotalStore, "get_accumulated_minor", staticmethod(boom))

    data = transfer_tools.prepare(prepare_invocation("200.00"), bank(), database=rt.database)

    assert data.facts["requires_code"] is True
    assert data.facts["risk_level"] == "red"


def test_the_composition_root_binds_the_database_into_transfer_prepare(rt):
    modules = production_modules(
        notification_service=rt.notification, database=rt.database, writes=rt.writes
    )
    transfer = next(module for module in modules if module.name == "transfer")

    handler = transfer.handlers["transfer_prepare"]
    assert isinstance(handler, partial)
    assert handler.keywords["database"] is rt.database
    assert isinstance(transfer.declaration.graph_builder, partial)
    assert transfer.declaration.graph_builder.keywords["writes"] is rt.writes
