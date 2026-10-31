"""转账工具层：金额解析、只读查询与 ToolSpec 构造。

这一层不碰平台图：handler 拿的是 (invocation, client)，与 card 的工具层同构。
银行用 tests.support.domain_harness.FakeBankClient（未注册的路径会报错，而不是静默返回空）。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from app.modules.base import ToolFailure
from app.core.clock import CST
from app.modules.transfer.tools import (
    HANDLERS,
    MAX_TRANSFER_MINOR,
    TOOLS,
    account_label,
    accounts,
    cancel,
    contact_label,
    contacts,
    load_accounts,
    prepare,
    schedule_prepare,
    scheduled,
    yuan_minor,
)
from app.tools.protocol import SideEffect, ToolCall, ToolInvocation, TrustedContext
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
    },
    {
        "account_id": "a_zhang_fixed",
        "user_id": CUSTOMER,
        "account_type": "fixed",
        "balance_minor": 500000,
        "available_minor": 0,
        "currency": "CNY",
        "interest_rate": 0.02,
        "status": "active",
    },
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
    },
    {
        "contact_id": "c_2",
        "user_id": CUSTOMER,
        "name": "朋友1",
        "phone": None,
        "remark": "没有收款账户",
        "recipient_user_id": None,
        "recipient_account_id": None,
        "birthday": None,
    },
]

SCHEDULED = [
    {
        "task_id": "task_1",
        "user_id": CUSTOMER,
        "task_type": "scheduled_transfer",
        "execute_at": "2026-10-14T09:00:00+08:00",
        "status": "pending",
        "amount_minor": 150000,
        "result": None,
        "error_code": None,
    }
]


def bank():
    fake = FakeBankClient()
    fake.register("/accounts", ACCOUNTS)
    fake.register("/contacts", CONTACTS)
    fake.register("/transfers/scheduled", SCHEDULED)
    return fake


def invocation(name, arguments=None, *, customer_id=CUSTOMER, key=None):
    return ToolInvocation(
        "transfer",
        ToolCall(
            tool_name=name,
            arguments=dict(arguments or {}),
            call_id="c_1",
            idempotency_key=key,
        ),
        TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id=customer_id),
    )


# ------------------------------------------------------------------ 金额解析


def test_yuan_minor_accepts_a_plain_decimal_yuan_string():
    assert yuan_minor("5000.00") == 500000
    assert yuan_minor("0.01") == 1
    assert yuan_minor("1000000") == MAX_TRANSFER_MINOR


@pytest.mark.parametrize(
    "value",
    ["5,000", "五千", "5000元", "", "0", "-1", "5000.001", "1000000.01", None, True],
)
def test_yuan_minor_refuses_anything_that_is_not_a_plain_yuan_string(value):
    with pytest.raises(ToolFailure) as failure:
        yuan_minor(value)
    assert failure.value.code == "VALIDATION"


# 浏览器级验收发现：真实模型经常把金额当 number 送（100 而不是 "100.00"）。
# 类型放宽、取值仍然严格，是这次验收的产物。
@pytest.mark.parametrize("value, expected", [(5000, 500000), (100.5, 10050), (0.01, 1)])
def test_yuan_minor_accepts_a_number_the_way_a_model_sends_it(value, expected):
    assert yuan_minor(value) == expected


def test_yuan_minor_refuses_zero_with_its_own_wording():
    with pytest.raises(ToolFailure) as failure:
        yuan_minor("0")
    assert "大于 0" in failure.value.message


# ------------------------------------------------------------------ 身份


def test_a_request_without_a_bank_customer_never_reaches_the_bank():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        accounts(invocation("transfer_accounts", customer_id=None), fake)
    assert failure.value.code == "NO_CUSTOMER_BINDING"
    assert fake.requests == []


# ------------------------------------------------------------------ 账户


def test_account_label_falls_back_to_the_raw_type():
    assert account_label({"account_id": "a_x_9999", "account_type": "unknown"}) == "unknown（尾号 9999）"


def test_accounts_reports_every_account_with_its_label_and_balance():
    fake = bank()
    result = accounts(invocation("transfer_accounts"), fake)

    assert "活期账户（尾号 rent）" in result.summary
    assert "8,320.00 元" in result.summary
    assert "定期账户（尾号 ixed）" in result.summary
    assert result.facts["accounts"] == ACCOUNTS
    assert result.display == {"blocks": []}
    assert fake.requests[0]["path"] == "/accounts"
    assert fake.requests[0]["user_id"] == CUSTOMER


def test_load_accounts_refuses_a_response_that_is_not_a_list():
    fake = FakeBankClient()
    fake.register("/accounts", {"accounts": []})
    with pytest.raises(ToolFailure) as failure:
        load_accounts(invocation("transfer_accounts"), fake)
    assert failure.value.code == "PROTOCOL"


# ------------------------------------------------------------------ 联系人


def test_contacts_passes_the_query_and_reports_the_recipient_account():
    fake = bank()
    result = contacts(invocation("transfer_contacts", {"query": "老王"}), fake)

    assert fake.requests[0]["path"] == "/contacts"
    assert fake.requests[0]["params"] == {"q": "老王"}
    assert "老王" in result.summary
    assert "可转账" in result.summary
    assert "没有可用的收款账户" in result.summary
    assert result.facts["contacts"] == CONTACTS


def test_contact_label_carries_the_recipient_account_tail():
    assert contact_label(CONTACTS[0]) == "老王（尾号 rent）"


def test_string_null_placeholders_still_reach_the_balance_check():
    """弱模型把「没提供」写成字符串 "null"，它必须和省略一样，让余额校验生效。

    归一化收口在网关（spec D1：不在模块里再抄一份），所以这条走真实边界——网关 + 真
    BankToolAdapter——而不是直接调 prepare()：直调会绕过网关，测不到本次修复的那一层。
    """

    from app.modules.registry import build_registry
    from app.modules.transfer import MODULE
    from app.tools.bank_adapter import BankToolAdapter
    from app.tools.gateway import ToolGateway
    from app.tools.protocol import ToolStatus

    fake = bank()
    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, fake))
    context = TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id=CUSTOMER)

    result = gateway.invoke(
        gateway.handle("transfer", context),
        {
            "tool": "transfer_prepare",
            "arguments": {
                "amount_yuan": "9999.00",
                "to_name": "老王",
                "from_hint": "null",
                "from_account_id": "null",
            },
        },
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "INSUFFICIENT_BALANCE"


def test_contacts_without_a_query_lists_every_contact():
    fake = bank()
    result = contacts(invocation("transfer_contacts", {}), fake)
    assert result.facts["contacts"] == CONTACTS
    assert fake.requests[0]["params"] == {}


# ------------------------------------------------------------------ 定时转账


def test_scheduled_forwards_the_status_filter_and_formats_the_amount():
    fake = bank()
    result = scheduled(invocation("transfer_scheduled", {"status": "pending"}), fake)

    assert fake.requests[0]["path"] == "/transfers/scheduled"
    assert fake.requests[0]["params"] == {"status": "pending"}
    assert "task_1" in result.summary
    assert "1,500.00 元" in result.summary
    assert result.facts["scheduled"] == SCHEDULED


def test_scheduled_survives_a_task_without_an_amount():
    fake = bank()
    fake.register(
        "/transfers/scheduled",
        [
            {
                "task_id": "task_2",
                "user_id": CUSTOMER,
                "task_type": "scheduled_transfer",
                "execute_at": "2026-10-14T09:00:00+08:00",
                "status": "pending",
                "amount_minor": None,
                "result": None,
                "error_code": None,
            }
        ],
    )
    result = scheduled(invocation("transfer_scheduled"), fake)

    assert "task_2" in result.summary
    assert "金额未知" in result.summary
    assert fake.requests[0]["params"] == {}


def test_scheduled_without_a_filter_sends_no_status_parameter():
    fake = bank()
    scheduled(invocation("transfer_scheduled"), fake)
    assert "status" not in fake.requests[0]["params"]


# ------------------------------------------------------------------ 工具声明


def test_every_declared_tool_name_is_a_dot_free_transfer_identifier():
    for name, declared in TOOLS.items():
        assert declared.name == name
        assert name.startswith("transfer_")
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name)


def test_read_tools_declare_no_output_schema():
    assert all(declared.output_schema is None for declared in TOOLS.values())

# ------------------------------------------------------------------ 意图解析


def test_prepare_freezes_a_ready_intent_with_the_amount_and_labels():
    fake = bank()
    result = prepare(
        invocation("transfer_prepare", {"amount_yuan": "5000.00", "to_name": "老王", "from_hint": "活期"}),
        fake,
    )

    facts = result.facts
    assert facts["status"] == "ready"
    assert facts["intent"] == {
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 500000,
        "remark": None,
    }
    assert facts["from_label"] == "活期账户（尾号 rent）"
    assert facts["to_label"] == "老王"
    assert facts["requires_code"] is True
    assert "请确认这笔转账" in result.summary
    assert "需要验证码" in result.summary
    assert all(request["method"] == "GET" for request in fake.requests)


@pytest.mark.parametrize("amount_yuan, expected", [("1000.00", False), ("1000.01", True)])
def test_requires_code_follows_the_daily_red_threshold(amount_yuan, expected):
    fake = bank()
    result = prepare(
        invocation("transfer_prepare", {"amount_yuan": amount_yuan, "to_name": "老王", "from_hint": "活期"}),
        fake,
    )
    assert result.facts["requires_code"] is expected
    if expected:
        assert "需要验证码：" in result.summary
    else:
        assert "风险等级：yellow，确认卡片点一下即可发起。" in result.summary


@pytest.mark.parametrize(
    "hint",
    [
        "活期账户（尾号 rent）",
        "活期账户 尾号 rent",
        "活期账户尾号rent",
        "活期账户(尾号 rent)",
    ],
)
def test_prepare_accepts_the_label_exactly_as_the_card_shows_it(hint):
    """卡片上写的是「活期账户（尾号 rent）」，用户/模型复述时会丢掉全角括号或空格。"""

    fake = bank()
    result = prepare(
        invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王", "from_hint": hint}),
        fake,
    )
    assert result.facts["intent"]["from_account_id"] == "a_zhang_current"

def test_prepare_ignores_the_fixed_deposit_account():
    fake = bank()
    result = prepare(invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王"}), fake)
    assert result.facts["intent"]["from_account_id"] == "a_zhang_current"


def test_prepare_asks_the_user_to_choose_between_several_matching_accounts():
    fake = bank()
    fake.register("/accounts", [ACCOUNTS[0], {**ACCOUNTS[0], "account_id": "a_zhang_current2"}])
    result = prepare(invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王"}), fake)

    facts = result.facts
    assert facts["status"] == "select"
    assert facts["field"] == "from_account"
    assert [item["value"] for item in facts["candidates"]] == [
        "a_zhang_current",
        "a_zhang_current2",
    ]
    assert facts["intent"]["amount_minor"] == 10000
    assert "请选择付款账户" in result.summary


def test_prepare_asks_the_user_to_choose_between_same_named_contacts():
    fake = bank()
    fake.register(
        "/contacts",
        [CONTACTS[0], {**CONTACTS[0], "contact_id": "c_9", "recipient_account_id": "a_partner_save"}],
    )
    result = prepare(invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王"}), fake)

    facts = result.facts
    assert facts["status"] == "select"
    assert facts["field"] == "to_account"
    assert [item["value"] for item in facts["candidates"]] == [
        "a_partner_current",
        "a_partner_save",
    ]


def test_prepare_refuses_when_no_contact_can_receive():
    fake = bank()
    fake.register("/contacts", [CONTACTS[1]])
    with pytest.raises(ToolFailure) as failure:
        prepare(invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "朋友1"}), fake)
    assert failure.value.code == "NOT_FOUND"


def test_prepare_refuses_when_the_hint_matches_nothing():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        prepare(
            invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王", "from_hint": "不存在"}),
            fake,
        )
    assert failure.value.code == "NOT_FOUND"


def test_prepare_refuses_an_amount_over_the_available_balance():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        prepare(
            invocation("transfer_prepare", {"amount_yuan": "9000.00", "to_name": "老王", "from_hint": "活期"}),
            fake,
        )
    assert failure.value.code == "INSUFFICIENT_BALANCE"


def test_prepare_refuses_the_same_account_on_both_sides():
    fake = bank()
    fake.register("/contacts", [{**CONTACTS[0], "recipient_account_id": "a_zhang_current"}])
    with pytest.raises(ToolFailure) as failure:
        prepare(
            invocation("transfer_prepare", {"amount_yuan": "100.00", "to_name": "老王", "from_hint": "活期"}),
            fake,
        )
    assert failure.value.code == "VALIDATION"


def test_prepare_accepts_the_backfilled_account_ids():
    fake = bank()
    result = prepare(
        invocation(
            "transfer_prepare",
            {
                "amount_yuan": "100.00",
                "to_name": "老王",
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
            },
        ),
        fake,
    )
    assert result.facts["status"] == "ready"
    assert result.facts["intent"]["from_account_id"] == "a_zhang_current"


def test_prepare_refuses_an_unknown_backfilled_account_id():
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        prepare(
            invocation(
                "transfer_prepare",
                {"amount_yuan": "100.00", "to_name": "老王", "from_account_id": "a_nope"},
            ),
            fake,
        )
    assert failure.value.code == "NOT_FOUND"


# ------------------------------------------------------------------ 取消定时转账


def test_cancel_is_a_write_and_needs_a_task_id():
    assert TOOLS["transfer_cancel"].side_effect is SideEffect.WRITE
    fake = bank()
    with pytest.raises(ToolFailure) as failure:
        cancel(invocation("transfer_cancel", {}), fake)
    assert failure.value.code == "VALIDATION"
    assert fake.requests == []


def test_cancel_forwards_the_idempotency_key_and_reports_the_task():
    fake = bank()
    fake.register("/transfers/scheduled/task_1/cancel", {"status": "cancelled"})
    result = cancel(invocation("transfer_cancel", {"task_id": "task_1"}, key="k1"), fake)

    assert fake.requests[0]["path"] == "/transfers/scheduled/task_1/cancel"
    assert fake.requests[0]["idempotency_key"] == "k1"
    assert fake.requests[0]["method"] == "POST"
    assert "task_1" in result.summary
    assert result.facts["task"] == {"status": "cancelled"}


def test_the_ten_tools_and_their_handlers_line_up():
    assert set(TOOLS) == {
        "transfer_accounts",
        "transfer_contacts",
        "transfer_scheduled",
        "transfer_prepare",
        "transfer_schedule",
        "transfer_cancel",
        "transfer_aa",
        "transfer_aa_status",
        "transfer_aa_pay",
        "transfer_aa_cancel",
    }
    assert set(HANDLERS) == set(TOOLS)

