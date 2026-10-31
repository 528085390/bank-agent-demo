"""Task 1 of L1.A: the life_event module's four visible tools.

生日链路读三个银行端点（/birthdays、/mock-products、/accounts），在 life_prepare_order
里冻结一份订单快照，再用 life_cancel_order 经资金写门面取消它。

时钟一律固定：SystemClock 会去银行取时间，不 monkeypatch 就会打网络。日累计只由
rt.database 提供（真实 Session）；这里的用例多半直接调 handler，所以显式传或不传
database——生产路径由 `bind_life_event` 注入，不传时按 0 计只是降级路径。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from app.core.clock import CST
from app.integration.write_facade import WriteFacade
from app.modules.base import ToolFailure
from app.modules.life_event import MODULE, bind_life_event
from app.modules.life_event import tools as life_tools
from app.modules.life_event.tools import days_until, next_birthday, pick_products
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, SideEffect, ToolCall, ToolInvocation, TrustedContext
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
COLLEAGUE = {
    "contact_id": "c_colleague",
    "user_id": CUSTOMER,
    "name": "同事1",
    "phone": "13800138003",
    "remark": "同事",
    "recipient_user_id": None,
    "recipient_account_id": None,
    "birthday": None,
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
FIXED_ACCOUNT = {
    "account_id": "a_zhang_fixed",
    "user_id": CUSTOMER,
    "account_type": "fixed",
    "balance_minor": 5000000,
    "available_minor": 5000000,
    "currency": "CNY",
    "interest_rate": "0.03",
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


def bank(*, accounts=None, contacts=None, products=None) -> FakeBankClient:
    client = FakeBankClient()
    client.register("/birthdays", [PARTNER, COLLEAGUE] if contacts is None else contacts)
    client.register("/mock-products", PRODUCTS if products is None else products)
    client.register("/accounts", [CURRENT_ACCOUNT, FIXED_ACCOUNT] if accounts is None else accounts)
    return client


def _invocation(tool: str, arguments: dict | None = None, *, key: str | None = "idem-life-1"):
    return ToolInvocation(
        "life_event",
        ToolCall(
            tool_name=tool,
            arguments=arguments or {},
            call_id="call_life_1",
            idempotency_key=key,
        ),
        TrustedContext(user_id=APP_USER, session_id="sess_life", bank_customer_id=CUSTOMER),
    )


# ------------------------------------------------------------------ 纯函数


def test_the_next_birthday_is_this_years_and_the_days_count_down():
    today = date(2031, 3, 4)

    assert next_birthday(today, date(1990, 3, 11)) == date(2031, 3, 11)
    assert days_until(today, date(1990, 3, 11)) == 7


def test_a_birthday_today_counts_as_zero_and_rolls_forward_tomorrow():
    assert next_birthday(date(2031, 3, 4), date(1990, 3, 4)) == date(2031, 3, 4)
    assert days_until(date(2031, 3, 4), date(1990, 3, 4)) == 0
    assert next_birthday(date(2031, 3, 5), date(1990, 3, 4)) == date(2032, 3, 4)


def test_a_leap_day_birthday_aligns_to_march_first_in_a_common_year():
    assert next_birthday(date(2031, 1, 10), date(2000, 2, 29)) == date(2031, 3, 1)
    assert days_until(date(2031, 1, 10), date(2000, 2, 29)) == 50
    # 闰年里仍然是 2 月 29 日：对齐只在平年发生。
    assert next_birthday(date(2032, 1, 10), date(2000, 2, 29)) == date(2032, 2, 29)


def test_pick_products_keeps_the_cheapest_first_and_filters_by_budget():
    picked = pick_products(PRODUCTS)

    assert [item["product_code"] for item in picked] == ["cake", "flowers"]
    assert [item["product_code"] for item in pick_products(PRODUCTS, budget_minor=15000)] == [
        "cake"
    ]
    assert pick_products(PRODUCTS, budget_minor=1000) == []
    # 入参不被改写，返回的是副本。
    assert [item["product_code"] for item in PRODUCTS] == ["flowers", "cake"]
    assert picked[0] is not PRODUCTS[1]


# ------------------------------------------------------------------ life_birthdays


def test_life_birthdays_reports_the_partner_next_birthday(frozen_clock):
    data = life_tools.birthdays(_invocation("life_birthdays"), bank())

    rows = data.facts["contacts"]
    assert [row["name"] for row in rows] == ["爱人"]
    assert rows[0] == {
        "contact_id": "c_partner",
        "name": "爱人",
        "birthday": "1995-09-25",
        "next_birthday": "2031-09-25",
        "days_until": 205,
    }
    assert "爱人" in data.summary
    assert "2031-09-25" in data.summary


def test_life_birthdays_says_so_when_no_contact_has_a_birthday(frozen_clock):
    data = life_tools.birthdays(_invocation("life_birthdays"), bank(contacts=[COLLEAGUE]))

    assert data.facts["contacts"] == []
    assert "还没有" in data.summary


# ------------------------------------------------------------------ life_gift_plan


def test_life_gift_plan_filters_the_bank_products_by_budget(frozen_clock):
    data = life_tools.gift_plan(_invocation("life_gift_plan", {"budget_yuan": "150"}), bank())

    assert [item["product_code"] for item in data.facts["products"]] == ["cake"]
    assert data.facts["budget_minor"] == 15000
    assert "129.00 元" in data.summary
    assert "199.00 元" not in data.summary


def test_life_gift_plan_without_a_budget_offers_every_product(frozen_clock):
    data = life_tools.gift_plan(_invocation("life_gift_plan"), bank())

    assert [item["product_code"] for item in data.facts["products"]] == ["cake", "flowers"]
    assert data.facts["budget_minor"] is None


def test_life_gift_plan_rejects_a_budget_that_is_not_money(frozen_clock):
    with pytest.raises(ToolFailure) as caught:
        life_tools.gift_plan(_invocation("life_gift_plan", {"budget_yuan": "两百"}), bank())

    assert caught.value.code == ErrorCode.VALIDATION


def test_life_gift_plan_truncates_a_fractional_cent_budget(frozen_clock):
    """A model computing one tenth of 8,510.58 may send 851.058; cents are the floor."""

    data = life_tools.gift_plan(_invocation("life_gift_plan", {"budget_yuan": "851.058"}), bank())

    assert data.facts["budget_minor"] == 85105


# ------------------------------------------------------------------ life_prepare_order


def test_life_prepare_order_freezes_a_yellow_snapshot_and_picks_the_only_current_account(
    rt, frozen_clock
):
    data = life_tools.prepare_order(
        _invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}),
        bank(),
        rt.database,
    )

    assert data.facts["status"] == "ready"
    assert data.facts["requires_code"] is False
    assert data.facts["risk_level"] == "yellow"
    assert data.facts["intent"] == {
        "contact_id": "c_partner",
        "contact_name": "爱人",
        "product_code": "flowers",
        "product_name": "模拟鲜花",
        "amount_minor": 19900,
        "from_account_id": "a_zhang_current",
        "from_account_label": "活期账户（尾号 rent）",
        "birthday_date": "2031-09-25",
        "execute_at": "2031-09-23T08:00:00+08:00",
    }
    assert "爱人" in data.summary
    assert "199.00 元" in data.summary
    assert "验证码" not in data.summary


def test_life_prepare_order_treats_an_unbound_database_as_zero_and_says_so(frozen_clock, caplog):
    with caplog.at_level(logging.DEBUG, logger="app.modules.life_event.tools"):
        data = life_tools.prepare_order(
            _invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "cake"}),
            bank(),
        )

    assert data.facts["risk_level"] == "yellow"
    assert data.facts["requires_code"] is False
    assert any(APP_USER in record.getMessage() for record in caplog.records)


def test_life_prepare_order_turns_red_when_the_day_total_crosses_the_limit(rt, frozen_clock):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 99000)

    data = life_tools.prepare_order(
        _invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}),
        bank(),
        rt.database,
    )

    assert data.facts["requires_code"] is True
    assert data.facts["risk_level"] == "red"
    assert "超过日限额" in data.facts["reason"]
    assert "验证码" in data.summary


def test_life_prepare_order_fails_closed_when_the_day_total_cannot_be_read(
    rt, frozen_clock, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("daily total store unavailable")

    monkeypatch.setattr(DailyTotalStore, "get_accumulated_minor", staticmethod(boom))

    data = life_tools.prepare_order(
        _invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}),
        bank(),
        rt.database,
    )

    assert data.facts["risk_level"] == "red"
    assert data.facts["requires_code"] is True


def test_life_prepare_order_refuses_a_birthday_that_is_too_close(frozen_clock):
    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation(
                "life_prepare_order",
                {
                    "contact_id": "c_partner",
                    "product_code": "flowers",
                    "birthday_date": "2031-03-05",
                },
            ),
            bank(),
        )

    assert caught.value.code == ErrorCode.VALIDATION
    assert "来不及" in caught.value.message
    assert "爱人" in caught.value.message


def test_life_prepare_order_refuses_a_birthday_that_is_today(frozen_clock):
    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation(
                "life_prepare_order",
                {
                    "contact_id": "c_partner",
                    "product_code": "flowers",
                    "birthday_date": "2031-03-04",
                },
            ),
            bank(),
        )

    assert caught.value.code == ErrorCode.VALIDATION


def test_life_prepare_order_rolls_a_birth_year_forward_to_the_next_birthday(frozen_clock):
    data = life_tools.prepare_order(
        _invocation(
            "life_prepare_order",
            {"contact_id": "c_partner", "product_code": "cake", "birthday_date": "1995-09-25"},
        ),
        bank(),
    )

    assert data.facts["intent"]["birthday_date"] == "2031-09-25"
    assert data.facts["intent"]["execute_at"] == "2031-09-23T08:00:00+08:00"


def test_life_prepare_order_accepts_an_explicit_current_account(frozen_clock):
    data = life_tools.prepare_order(
        _invocation(
            "life_prepare_order",
            {
                "contact_id": "c_partner",
                "product_code": "flowers",
                "from_account_id": "a_zhang_current",
            },
        ),
        bank(),
    )

    assert data.facts["intent"]["from_account_id"] == "a_zhang_current"


def test_life_prepare_order_refuses_a_fixed_account(frozen_clock):
    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation(
                "life_prepare_order",
                {
                    "contact_id": "c_partner",
                    "product_code": "flowers",
                    "from_account_id": "a_zhang_fixed",
                },
            ),
            bank(),
        )

    assert caught.value.code == ErrorCode.NOT_FOUND


def test_life_prepare_order_asks_which_account_when_two_current_accounts_exist(frozen_clock):
    second = {**CURRENT_ACCOUNT, "account_id": "a_zhang_current_2"}

    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation(
                "life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}
            ),
            bank(accounts=[CURRENT_ACCOUNT, second, FIXED_ACCOUNT]),
        )

    assert caught.value.code == ErrorCode.VALIDATION
    assert "a_zhang_current" in caught.value.message


def test_life_prepare_order_reports_an_unknown_contact(frozen_clock):
    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation(
                "life_prepare_order", {"contact_id": "c_missing", "product_code": "flowers"}
            ),
            bank(),
        )

    assert caught.value.code == ErrorCode.NOT_FOUND


def test_life_prepare_order_accepts_a_contact_name(frozen_clock):
    """模型把姓名当 ID 传也能用：ID 找不到时按姓名精确匹配。"""

    data = life_tools.prepare_order(
        _invocation("life_prepare_order", {"contact_id": "爱人", "product_code": "flowers"}),
        bank(),
    )

    assert data.facts["intent"]["contact_id"] == "c_partner"
    assert data.facts["intent"]["contact_name"] == "爱人"


def test_life_docs_tell_the_model_a_contact_name_is_enough():
    """The birthdays summary has no internal id, so the docs must allow the name."""

    from app.modules.life_event import workflow

    assert "姓名" in workflow.SYSTEM_PROMPT
    description = MODULE.tools["life_prepare_order"].parameters["properties"]["contact_id"][
        "description"
    ]
    assert "ID 或姓名" in description


def test_life_prepare_order_refuses_an_ambiguous_contact_name(frozen_clock):
    """同名多人：拒绝并列出 ID，让模型回头问用户，别替人挑一个。"""

    twin = {**PARTNER, "contact_id": "c_partner_2", "recipient_account_id": "a_partner_2"}

    with pytest.raises(ToolFailure) as caught:
        life_tools.prepare_order(
            _invocation("life_prepare_order", {"contact_id": "爱人", "product_code": "flowers"}),
            bank(contacts=[PARTNER, twin, COLLEAGUE]),
        )

    assert caught.value.code == ErrorCode.VALIDATION
    assert "c_partner" in caught.value.message
    assert "c_partner_2" in caught.value.message


# ------------------------------------------------------------------ life_cancel_order


def test_life_cancel_order_posts_to_the_bank_and_skips_the_daily_total(rt):
    client = bank()
    client.register(
        "/birthdays/orders/o_birthday_1/cancel", {"order_id": "o_birthday_1", "status": "cancelled"}
    )
    events: list[tuple[str, dict]] = []
    facade = WriteFacade(
        client,
        audit_hook=lambda event, payload: events.append((event, dict(payload))),
        database=rt.database,
    )

    data = life_tools.cancel(
        _invocation("life_cancel_order", {"order_id": "o_birthday_1"}), client, writes=facade
    )

    posts = [request for request in client.requests if request["method"] == "POST"]
    assert [request["path"] for request in posts] == ["/birthdays/orders/o_birthday_1/cancel"]
    assert posts[0]["idempotency_key"] == "idem-life-1"
    assert [name for name, _ in events] == [
        "fund_write_attempt",
        "fund_write_success",
        "daily_total_skipped",
    ]
    assert events[-1][1]["operation"] == "cancel_birthday_order"
    assert data.facts["order"]["status"] == "cancelled"
    assert data.business_ids == {"order_id": "o_birthday_1"}
    assert "o_birthday_1" in data.summary


def test_life_cancel_order_without_an_idempotency_key_never_reaches_the_bank():
    client = bank()

    with pytest.raises(ToolFailure) as caught:
        life_tools.cancel(
            _invocation("life_cancel_order", {"order_id": "o_birthday_1"}, key=None),
            client,
            writes=WriteFacade(client),
        )

    assert caught.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    assert [request for request in client.requests if request["method"] == "POST"] == []


def test_life_cancel_order_maps_a_bank_rejection_to_a_tool_failure():
    client = bank()
    client.register(
        "/birthdays/orders/o_birthday_1/cancel", None, code=1005, message="invalid state"
    )

    with pytest.raises(ToolFailure) as caught:
        life_tools.cancel(
            _invocation("life_cancel_order", {"order_id": "o_birthday_1"}),
            client,
            writes=WriteFacade(client),
        )

    assert caught.value.code == ErrorCode.INVALID_STATE


# ------------------------------------------------------------------ 模块骨架


def test_the_module_declares_five_tools_with_their_handlers():
    assert set(MODULE.tools) == {
        "life_birthdays",
        "life_gift_plan",
        "life_prepare_order",
        "life_cancel_order",
        "life_execute_order",
    }
    assert set(MODULE.handlers) == set(MODULE.tools)
    assert MODULE.tools["life_cancel_order"].side_effect is SideEffect.WRITE
    assert MODULE.tools["life_execute_order"].side_effect is SideEffect.WRITE
    assert MODULE.tools["life_birthdays"].side_effect is SideEffect.READ
    assert MODULE.tools["life_gift_plan"].side_effect is SideEffect.READ
    assert MODULE.tools["life_prepare_order"].side_effect is SideEffect.READ
    # 运行时依赖由模块自己装配（见 test_life_composition.py）：组合根只递一把 ModuleDeps。
    assert MODULE.binder is bind_life_event


def test_the_declaration_names_the_fields_the_birthday_chain_asks_for():
    fields = MODULE.declaration.fields

    assert set(fields) == {"contact_id", "product_code", "from_account_id", "birthday_date"}
    assert fields["product_code"].type == "select"
    assert fields["product_code"].options == ("flowers", "cake")
    assert MODULE.declaration.graph_builder is not None


def test_every_tool_describes_its_arguments_and_the_facts_it_returns():
    for name, spec in MODULE.tools.items():
        assert spec.description, name
        assert spec.parameters["type"] == "object", name
        assert spec.output_schema is not None, name
        assert spec.output_schema["type"] == "object", name
        assert set(spec.output_schema["properties"]) == set(spec.output_schema["required"]), name
