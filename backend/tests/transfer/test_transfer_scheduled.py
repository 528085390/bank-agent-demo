"""定时转账创建：工具层只解析并冻结执行时刻，真正的创建在用户确认之后。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.modules.base import ToolFailure
from app.core.clock import CST
from app.modules.transfer.tools import schedule_prepare
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

CUSTOMER = "u_zhang"

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


def bank():
    fake = FakeBankClient()
    fake.register("/accounts", ACCOUNTS)
    fake.register("/contacts", CONTACTS)
    return fake


def invocation(arguments):
    return ToolInvocation(
        "transfer",
        ToolCall(
            tool_name="transfer_schedule",
            arguments=dict(arguments),
            call_id="c_1",
            idempotency_key=None,
        ),
        TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id=CUSTOMER),
    )


def test_schedule_prepare_freezes_the_execution_time_and_marks_the_operation():
    fake = bank()
    result = schedule_prepare(
        invocation(
            {
                "amount_yuan": "100.00",
                "to_name": "老王",
                "from_hint": "活期",
                "execute_date": "2026-10-31",
                "execute_time": "14:30",
            }
        ),
        fake,
        now=datetime(2026, 10, 30, 8, 0, tzinfo=CST),
    )

    facts = result.facts
    assert facts["status"] == "ready"
    assert facts["operation"] == "schedule_transfer"
    assert facts["intent"]["execute_at"] == "2026-10-31T14:30:00+08:00"
    assert facts["intent"]["execute_at_label"] == "2026-10-31 14:30"
    assert facts["execute_at_label"] == "2026-10-31 14:30"
    assert "请确认这笔定时转账" in result.summary
    assert "执行时间：2026-10-31 14:30" in result.summary
    assert all(request["method"] == "GET" for request in fake.requests)


def test_schedule_prepare_defaults_to_nine_and_understands_tomorrow():
    fake = bank()
    result = schedule_prepare(
        invocation({"amount_yuan": "100.00", "to_name": "老王", "execute_date": "明天"}),
        fake,
        now=datetime(2026, 10, 30, 8, 0, tzinfo=CST),
    )
    assert result.facts["intent"]["execute_at"] == "2026-10-31T09:00:00+08:00"


def test_schedule_prepare_refuses_a_time_that_has_already_passed():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        schedule_prepare(
            invocation({"amount_yuan": "100.00", "to_name": "老王", "execute_date": "2026-10-29"}),
            fake,
            now=datetime(2026, 10, 30, 8, 0, tzinfo=CST),
        )
    assert failure.value.code == "VALIDATION"
    assert "必须晚于当前时间" in str(failure.value)
    assert fake.requests == []


def test_schedule_prepare_keeps_the_execute_at_when_the_user_must_choose_an_account():
    fake = bank()
    fake.register("/accounts", [ACCOUNTS[0], {**ACCOUNTS[0], "account_id": "a_zhang_current2"}])
    result = schedule_prepare(
        invocation({"amount_yuan": "100.00", "to_name": "老王", "execute_date": "2026-10-31"}),
        fake,
        now=datetime(2026, 10, 30, 8, 0, tzinfo=CST),
    )
    assert result.facts["status"] == "select"
    assert result.facts["operation"] == "schedule_transfer"
    assert result.facts["intent"]["execute_at"] == "2026-10-31T09:00:00+08:00"


def test_schedule_prepare_refuses_a_bad_time_format():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        schedule_prepare(
            invocation(
                {
                    "amount_yuan": "100.00",
                    "to_name": "老王",
                    "execute_date": "2026-10-31",
                    "execute_time": "25:00",
                }
            ),
            fake,
            now=datetime(2026, 10, 30, 8, 0, tzinfo=CST),
        )
    assert failure.value.code == "VALIDATION"
    assert "24 小时制" in str(failure.value)
