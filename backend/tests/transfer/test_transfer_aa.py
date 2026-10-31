"""AA 收款：发起只解析并冻结拆分，支付/取消都在用户点击确认之后才发生。

工具层与 `test_transfer_scheduled.py` 同构：FakeBankClient、直接调 handler、
中文断言子串；负例同时断言「零银行写调用」。
"""

from __future__ import annotations

from datetime import date
from functools import partial

import pytest

from app.modules import production_modules
from app.modules.base import ToolFailure
from app.modules.transfer import tools as transfer_tools
from app.modules.transfer.tools import aa_cancel, aa_collect, aa_pay, aa_status
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

BACKUP = {
    **ACCOUNTS[0],
    "account_id": "a_zhang_backup",
    "balance_minor": 500000,
    "available_minor": 500000,
}

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
    },
    {
        # 指向我自己账户的联系人：绑上它等于对同一账户自转，必须被退回未绑定。
        "contact_id": "c_2",
        "user_id": CUSTOMER,
        "name": "我自己",
        "phone": None,
        "remark": None,
        "recipient_user_id": CUSTOMER,
        "recipient_account_id": "a_zhang_current",
        "birthday": None,
    },
]


def aa_detail(**overrides):
    detail = {
        "aa_request_id": "aa_1",
        "user_id": "u_li",
        "from_account_id": "a_li_current",
        "total_amount_minor": 30000,
        "participant_count": 2,
        "remark": "演示聚餐",
        "status": "open",
        "created_at": "2026-10-30T09:00:00+08:00",
        "expires_at": "2026-11-06T09:00:00+08:00",
        "participants": [
            {
                "id": "aap_1",
                "participant_index": 0,
                "name": "张三",
                "phone": None,
                "account_id": "a_zhang_current",
                "amount_minor": 15000,
                "status": "pending",
                "transaction_id": None,
                "paid_at": None,
            },
            {
                "id": "aap_2",
                "participant_index": 1,
                "name": "李四",
                "phone": None,
                "account_id": "a_li_current",
                "amount_minor": 15000,
                "status": "pending",
                "transaction_id": None,
                "paid_at": None,
            },
        ],
    }
    detail.update(overrides)
    return detail


class _FixedClock:
    """固定业务日：真实的 `SystemClock.today()` 会去问银行，测试不该有网络。"""

    @staticmethod
    def today(client=None):
        return BUSINESS_DAY


@pytest.fixture
def business_day(monkeypatch):
    monkeypatch.setattr(transfer_tools, "SystemClock", _FixedClock)


def bank(*, accounts=None, contacts=None, detail=None, pay=None, cancel=None):
    fake = FakeBankClient()
    fake.register("/accounts", ACCOUNTS if accounts is None else accounts)
    fake.register("/contacts", CONTACTS if contacts is None else contacts)
    if detail is not None:
        fake.register("/transfers/aa/aa_1", detail)
    if pay is not None:
        fake.register("/transfers/aa/aa_1/pay", pay)
    if cancel is not None:
        fake.register(
            "/transfers/aa/aa_1/cancel",
            cancel if isinstance(cancel, dict) else {"status": "cancelled"},
        )
    return fake


def invocation(tool, arguments, *, idempotency_key=None):
    return ToolInvocation(
        "transfer",
        ToolCall(
            tool_name=tool,
            arguments=dict(arguments),
            call_id="c_1",
            idempotency_key=idempotency_key,
        ),
        TrustedContext(user_id=APP_USER, session_id="sess_1", bank_customer_id=CUSTOMER),
    )


def posts(fake):
    return [request for request in fake.requests if request["method"] == "POST"]


# ------------------------------------------------------------------ 发起：拆分与绑定


def test_aa_collect_splits_with_remainder_to_the_last_participant():
    fake = bank()
    result = aa_collect(
        invocation(
            "transfer_aa",
            {"amount_yuan": "100.00", "participants": ["老王", "小李", "张三"]},
        ),
        fake,
    )

    facts = result.facts
    assert facts["status"] == "ready"
    assert facts["operation"] == "aa_collect"
    assert [item["amount_minor"] for item in facts["split_preview"]] == [3333, 3333, 3334]
    # 只有精确同名匹配的联系人绑定账户；小李和张三保持未绑定，卡片上如实展示。
    assert facts["intent"]["participants"][0]["account_id"] == "a_partner_current"
    assert facts["intent"]["participants"][1]["account_id"] is None
    assert facts["intent"]["participants"][2]["account_id"] is None
    assert facts["requires_code"] is False
    assert facts["risk_level"] == "yellow"
    assert "未绑定收款账户" in result.summary
    assert posts(fake) == []


def test_aa_collect_with_only_a_count_uses_placeholder_names():
    fake = bank()
    result = aa_collect(
        invocation(
            "transfer_aa",
            {"amount_yuan": "300.00", "participant_count": 4, "include_me": True},
        ),
        fake,
    )

    facts = result.facts
    assert facts["participant_count"] == 4
    names = [item["name"] for item in facts["split_preview"]]
    assert names == ["参与者1", "参与者2", "参与者3", "我"]
    # 名下只有一个活期账户（就是收款账户），我的一份只能未绑定。
    assert facts["intent"]["participants"][-1]["account_id"] is None
    assert [item["amount_minor"] for item in facts["split_preview"]] == [7500, 7500, 7500, 7500]


def test_aa_collect_includes_me_bound_to_a_second_current_account():
    fake = bank(accounts=[ACCOUNTS[0], BACKUP])
    result = aa_collect(
        invocation(
            "transfer_aa",
            {
                "amount_yuan": "300.00",
                "participant_count": 4,
                "include_me": True,
                "from_account_id": "a_zhang_current",
            },
        ),
        fake,
    )

    facts = result.facts
    assert facts["intent"]["from_account_id"] == "a_zhang_current"
    assert facts["intent"]["participants"][-1]["account_id"] == "a_zhang_backup"


def test_aa_collect_rejects_fewer_than_two_participants():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        aa_collect(
            invocation("transfer_aa", {"amount_yuan": "100.00", "participants": ["老王"]}),
            fake,
        )
    assert failure.value.code == "VALIDATION"
    assert "参与人数须在 2 到 20 之间" in str(failure.value)
    assert fake.requests == []

    with pytest.raises(ToolFailure) as count_only:
        aa_collect(
            invocation(
                "transfer_aa", {"amount_yuan": "100.00", "participant_count": 1, "include_me": True}
            ),
            fake,
        )
    assert count_only.value.code == "VALIDATION"
    assert "至少需要 2 名参与者" in str(count_only.value)
    assert fake.requests == []


def test_aa_collect_never_binds_a_contact_at_the_receiving_account():
    fake = bank()
    result = aa_collect(
        invocation(
            "transfer_aa",
            {"amount_yuan": "100.00", "participants": ["老王", "我自己"]},
        ),
        fake,
    )

    participants = result.facts["intent"]["participants"]
    # 「我自己」虽然精确匹配到 a_zhang_current，但那就是收款账户：必须退回未绑定。
    assert participants[1]["account_id"] is None


def test_aa_collect_with_two_current_accounts_asks_which_receives():
    fake = bank(accounts=[ACCOUNTS[0], BACKUP])
    result = aa_collect(
        invocation("transfer_aa", {"amount_yuan": "300.00", "participants": ["老王", "小李"]}),
        fake,
    )

    facts = result.facts
    assert facts["status"] == "select"
    assert facts["field"] == "from_account"
    assert len(facts["candidates"]) == 2
    assert facts["intent"]["total_amount_minor"] == 30000
    assert posts(fake) == []


# ------------------------------------------------------------------ 查进度


def test_aa_status_renders_progress_and_my_share():
    fake = bank(detail=aa_detail())
    result = aa_status(invocation("transfer_aa_status", {"aa_request_id": "aa_1"}), fake)

    assert "aa_1" in result.summary
    assert "进行中" in result.summary
    assert "待付 150.00 元" in result.summary
    assert "（我的一份）" in result.summary
    assert result.business_ids == {"aa_request_id": "aa_1"}
    assert posts(fake) == []


# ------------------------------------------------------------------ 付我的一份


def test_aa_pay_finds_my_share_and_is_yellow_under_the_cap():
    fake = bank(detail=aa_detail())
    result = aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)

    facts = result.facts
    assert facts["status"] == "ready"
    assert facts["operation"] == "aa_payment"
    assert facts["intent"]["aa_request_id"] == "aa_1"
    assert facts["intent"]["participant_index"] == 0
    assert facts["intent"]["amount_minor"] == 15000
    assert facts["intent"]["from_account_id"] == "a_zhang_current"
    assert facts["requires_code"] is False
    assert facts["risk_level"] == "yellow"
    assert posts(fake) == []


def test_a_daily_accumulation_escalates_aa_pay_to_red_and_leaves_one_audit_row(
    rt, business_day
):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 90000)

    fake = bank(detail=aa_detail())
    result = aa_pay(
        invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake, database=rt.database
    )

    assert result.facts["requires_code"] is True
    assert result.facts["risk_level"] == "red"
    assert "超过日限额" in result.facts["reason"]
    assert "需要验证码" in result.summary

    rows = [
        item
        for item in rt.audit.reader.business_events(APP_USER)["items"]
        if item["action"] == "transfer_aa_pay"
    ]
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "policy_decision"
    assert rows[0]["resource_id"] == "red"


def test_aa_pay_refuses_when_i_am_not_a_participant():
    detail = aa_detail(
        participants=[
            {**aa_detail()["participants"][0], "account_id": "a_other_current"},
            aa_detail()["participants"][1],
        ]
    )
    fake = bank(detail=detail)
    with pytest.raises(ToolFailure) as failure:
        aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)
    assert failure.value.code == "NOT_FOUND"
    assert "参与者" in str(failure.value)
    assert posts(fake) == []


def test_aa_pay_refuses_when_my_share_is_already_paid():
    detail = aa_detail(
        participants=[
            {**aa_detail()["participants"][0], "status": "paid"},
            aa_detail()["participants"][1],
        ]
    )
    fake = bank(detail=detail)
    with pytest.raises(ToolFailure) as failure:
        aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)
    assert failure.value.code == "INVALID_STATE"
    assert "已经支付" in str(failure.value)
    assert posts(fake) == []


def test_aa_pay_refuses_a_closed_request():
    fake = bank(detail=aa_detail(status="completed"))
    with pytest.raises(ToolFailure) as failure:
        aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)
    assert failure.value.code == "INVALID_STATE"
    assert "已全部付清" in str(failure.value)
    assert posts(fake) == []


def test_aa_pay_refuses_a_share_bound_at_the_receiving_account():
    detail = aa_detail(
        from_account_id="a_zhang_current",
        participants=[
            {**aa_detail()["participants"][0], "account_id": "a_zhang_current"},
            aa_detail()["participants"][1],
        ],
    )
    fake = bank(detail=detail)
    with pytest.raises(ToolFailure) as failure:
        aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)
    assert failure.value.code == "VALIDATION"
    assert "转出与转入账户不能相同" in str(failure.value)
    assert posts(fake) == []


def test_aa_pay_with_two_of_my_rows_asks_which_share():
    detail = aa_detail(
        participants=[
            aa_detail()["participants"][0],
            {**aa_detail()["participants"][1], "account_id": "a_zhang_backup"},
        ]
    )
    fake = bank(accounts=[ACCOUNTS[0], BACKUP], detail=detail)
    result = aa_pay(invocation("transfer_aa_pay", {"aa_request_id": "aa_1"}), fake)

    facts = result.facts
    assert facts["status"] == "select"
    assert facts["field"] == "aa_participant"
    assert len(facts["candidates"]) == 2
    assert all("150.00 元" in item["label"] for item in facts["candidates"])
    assert posts(fake) == []


def test_the_composition_root_binds_the_database_into_transfer_aa_pay(rt):
    modules = production_modules(
        notification_service=rt.notification, database=rt.database, writes=rt.writes
    )
    transfer = next(module for module in modules if module.name == "transfer")

    handler = transfer.handlers["transfer_aa_pay"]
    assert isinstance(handler, partial)
    assert handler.keywords["database"] is rt.database


# ------------------------------------------------------------------ 取消


def test_aa_cancel_delegates_one_write_with_the_call_idempotency_key():
    fake = bank(cancel={"aa_request_id": "aa_1", "status": "cancelled"})
    call = invocation(
        "transfer_aa_cancel", {"aa_request_id": "aa_1"}, idempotency_key="k1"
    )
    result = aa_cancel(call, fake)

    assert "已取消 AA 收款 aa_1" in result.summary
    assert result.business_ids == {"aa_request_id": "aa_1"}

    # 唯一一次银行写：取消路径、空请求体、 Supervisor 铸的幂等键原样到达银行。
    assert len(posts(fake)) == 1
    request = posts(fake)[0]
    assert request["path"] == "/transfers/aa/aa_1/cancel"
    assert request["json"] == {}
    assert request["idempotency_key"] == "k1"
    assert request["user_id"] == CUSTOMER
