"""理财、持仓、收益与赎回的领域测试。

覆盖计划要求：产品限额、余额、持仓余额、收益和幂等。

理财写接口固定要求有效测评、`confirm=true` 和等于配置演示码的验证码，
因此本模块用 autouse fixture 准备测评，并在请求里显式带上演示码，
而不是跳过校验维持旧行为。
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.errors import (
    BusinessError,
    ConfirmationRequiredError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from app.domain.investment_service import InvestmentService, accrued_interest_minor
from app.storage.models import Account, Holding, Order, Transaction
from tests.conftest import mfa_code

MFA = mfa_code()
A_YEAR_LATER = date(2027, 9, 20)


@pytest.fixture
def inv(seeded_database):
    return InvestmentService(seeded_database)


@pytest.fixture(autouse=True)
def _wealth_profile(wealth_risk_profile):
    """u_zhang 的有效测评：没有它申购会被适当性校验拒绝。"""
    return wealth_risk_profile


def _balance(database, account_id) -> int:
    with database.sessions() as session:
        return session.get_one(Account, account_id).balance_minor


def _purchase(inv, **overrides):
    payload = {
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
        "mfa_code": MFA,
    }
    payload.update(overrides)
    return inv.purchase(**payload)


# ---------------------------------------------------------------- 收益公式


def test_accrued_interest_follows_the_documented_formula():
    assert accrued_interest_minor(100000, Decimal("0.03500"), 365) == 3500
    assert accrued_interest_minor(100000, Decimal("0.03500"), 180) == 1726
    assert accrued_interest_minor(100000, Decimal("0.03500"), 0) == 0
    assert accrued_interest_minor(1000000, Decimal("0.04500"), 365) == 45000


# ---------------------------------------------------------------- 产品


def test_list_products_can_be_filtered(inv):
    all_products = inv.list_products()
    assert {item["product_id"] for item in all_products} == {
        "p_yuebao",
        "p_money_fund",
        "p_fixed_term",
    }
    low = inv.list_products(risk_level="low")
    assert {item["product_id"] for item in low} == {"p_yuebao", "p_money_fund"}
    fixed = inv.list_products(product_type="fixed_term")
    assert [item["product_id"] for item in fixed] == ["p_fixed_term"]


def test_get_product_and_unknown_product(inv):
    product = inv.get_product("p_yuebao")
    assert product["name"] == "余额宝"
    assert product["expected_return"] == 0.035
    with pytest.raises(NotFoundError):
        inv.get_product("p_missing")


def test_recommend_filters_by_risk_and_amount(inv):
    recommended = inv.recommend(risk_level="low", amount_minor=100)
    assert {item["product_id"] for item in recommended} == {"p_yuebao", "p_money_fund"}
    # 1000000 分低于定期理财起购点以外的产品都能覆盖。
    expensive = inv.recommend(amount_minor=50)
    assert [item["product_id"] for item in expensive] == ["p_yuebao"]


def test_compare_preserves_the_requested_order(inv):
    compared = inv.compare(["p_fixed_term", "p_yuebao"])
    assert [item["product_id"] for item in compared] == ["p_fixed_term", "p_yuebao"]
    with pytest.raises(ValidationError):
        inv.compare(["p_yuebao"])
    with pytest.raises(NotFoundError):
        inv.compare(["p_yuebao", "p_missing"])


# ---------------------------------------------------------------- 申购


def test_purchase_creates_order_holding_and_debit(inv, seeded_database):
    result = _purchase(inv)
    assert result["status"] == "completed"
    assert result["principal_minor"] == 100000
    assert result["order_id"].startswith("order_")
    assert result["holding_id"].startswith("hold_")
    assert result["transaction_id"].startswith("txn_")
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000

    with seeded_database.sessions() as session:
        entry = session.get_one(Transaction, result["transaction_id"])
        order = session.get_one(Order, result["order_id"])
        holding = session.get_one(Holding, result["holding_id"])
    assert entry.direction == "debit"
    assert entry.transaction_type == "purchase"
    assert order.order_type == "product_purchase"
    assert order.transaction_id == entry.id
    assert holding.principal_minor == holding.current_value_minor == 100000
    assert holding.expected_return == Decimal("0.03500")


def test_purchase_below_the_minimum_is_rejected(inv):
    with pytest.raises(ValidationError):
        _purchase(inv, product_id="p_money_fund", amount_minor=50)


def test_purchase_above_the_maximum_is_rejected(inv):
    with pytest.raises(ValidationError):
        _purchase(inv, amount_minor=100000001)


def test_purchase_below_the_product_minimum_for_fixed_term(inv):
    with pytest.raises(ValidationError):
        _purchase(inv, product_id="p_fixed_term", amount_minor=999999)


def test_purchase_rejects_insufficient_balance(inv, seeded_database):
    with pytest.raises(InsufficientBalanceError):
        _purchase(
            inv,
            amount_minor=20000000,
            confirm=True,
            mfa_code=MFA,
        )
    assert _balance(seeded_database, "a_zhang_current") == 1250050


def test_large_purchase_requires_confirmation(inv):
    with pytest.raises(ConfirmationRequiredError):
        _purchase(inv, confirm=False)


def test_purchase_from_a_fixed_account_is_rejected(inv):
    with pytest.raises(BusinessError):
        _purchase(inv, from_account_id="a_zhang_fixed")


def test_purchase_is_idempotent(inv, seeded_database):
    payload = {
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
        "mfa_code": MFA,
        "idempotent_key": "purchase-001",
    }
    first = inv.purchase(**payload)
    second = inv.purchase(**payload)
    assert first["holding_id"] == second["holding_id"]
    assert second["replayed"] is True
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000
    with seeded_database.sessions() as session:
        assert session.execute(select(func.count()).select_from(Holding)).scalar_one() == 1


# ---------------------------------------------------------------- 持仓与收益


def test_holdings_are_listed_and_can_be_filtered(inv):
    purchased = _purchase(inv)
    holdings = inv.list_holdings(user_id="u_zhang")
    assert [item["holding_id"] for item in holdings] == [purchased["holding_id"]]
    assert inv.list_holdings(user_id="u_zhang", status="holding")[0]["holding_id"] == (
        purchased["holding_id"]
    )
    assert inv.list_holdings(user_id="u_zhang", status="redeemed") == []


def test_a_holding_view_carries_the_product_name(inv):
    purchased = _purchase(inv)

    holding = inv.get_holding(purchased["holding_id"], user_id="u_zhang")

    assert holding["product_id"] == "p_yuebao"
    assert holding["product_name"] == "余额宝"


def test_holding_view_tolerates_a_missing_product(inv, seeded_database):
    """持仓指向已不存在的产品时，视图不抛异常且产品名回退为 None。"""

    purchased = _purchase(inv)

    with seeded_database.sessions() as session:
        holding = session.get_one(Holding, purchased["holding_id"])
        # 外键约束不允许把悬空的 product_id 落库，因此改用脱离会话的对象模拟脏数据。
        session.expunge(holding)
        holding.product_id = "p_missing"

        view = inv._holding_view(session, holding, datetime.now(timezone.utc))

    assert view["product_id"] == "p_missing"
    assert view["product_name"] is None


def test_simulate_interest_updates_current_value_by_holding_days(inv):
    purchased = _purchase(inv)
    result = inv.simulate_interest(purchased["holding_id"], as_of=A_YEAR_LATER, user_id="u_zhang")
    assert result["days"] == 365
    assert result["accrued_interest_minor"] == 3500
    assert result["current_value_minor"] == 103500
    assert result["principal_minor"] == 100000


def test_simulate_interest_defaults_to_the_simulation_clock(inv):
    purchased = _purchase(inv)
    result = inv.simulate_interest(purchased["holding_id"], user_id="u_zhang")
    # 模拟时钟停在买入当天，因此没有收益。
    assert result["days"] == 0
    assert result["accrued_interest_minor"] == 0


def test_unknown_holding_raises_not_found(inv):
    with pytest.raises(NotFoundError):
        inv.get_holding("hold_missing", user_id="u_zhang")


# ---------------------------------------------------------------- 赎回


def test_full_redemption_returns_principal_plus_interest(inv, seeded_database):
    purchased = _purchase(inv)
    inv.simulate_interest(purchased["holding_id"], as_of=A_YEAR_LATER, user_id="u_zhang")

    result = inv.redeem(
        user_id="u_zhang",
        holding_id=purchased["holding_id"],
        amount_minor=103500,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["holding_status"] == "redeemed"
    assert result["principal_redeemed_minor"] == 100000
    assert result["interest_minor"] == 3500
    assert result["remaining_value_minor"] == 0
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000 + 103500

    with seeded_database.sessions() as session:
        entry = session.get_one(Transaction, result["transaction_id"])
    assert entry.direction == "credit"
    assert entry.transaction_type == "redeem"


def test_partial_redemption_keeps_the_rest_of_the_holding(inv, seeded_database):
    purchased = _purchase(inv)
    inv.simulate_interest(purchased["holding_id"], as_of=A_YEAR_LATER, user_id="u_zhang")

    result = inv.redeem(
        user_id="u_zhang",
        holding_id=purchased["holding_id"],
        amount_minor=50000,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["holding_status"] == "partial"
    assert result["remaining_value_minor"] == 103500 - 50000
    assert result["remaining_principal_minor"] > 0
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000 + 50000

    holding = inv.get_holding(purchased["holding_id"], user_id="u_zhang")
    assert holding["status"] == "partial"


def test_redemption_above_the_holding_value_is_rejected(inv, seeded_database):
    purchased = _purchase(inv)
    with pytest.raises(BusinessError):
        inv.redeem(
            user_id="u_zhang",
            holding_id=purchased["holding_id"],
            amount_minor=100001,
            confirm=True,
            mfa_code=MFA,
        )
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


def test_a_redeemed_holding_cannot_be_redeemed_again(inv):
    purchased = _purchase(inv)
    inv.redeem(
        user_id="u_zhang",
        holding_id=purchased["holding_id"],
        amount_minor=100000,
        confirm=True,
        mfa_code=MFA,
    )
    with pytest.raises(BusinessError):
        inv.redeem(
            user_id="u_zhang",
            holding_id=purchased["holding_id"],
            amount_minor=1,
            confirm=True,
            mfa_code=MFA,
        )


def test_redemption_to_another_users_account_is_rejected(inv, seeded_database):
    """到账账户必须属于本人：不能用 A 的身份把钱赎到 B 的账户。"""

    purchased = _purchase(inv)
    with pytest.raises(NotFoundError):
        inv.redeem(
            user_id="u_zhang",
            holding_id=purchased["holding_id"],
            amount_minor=100000,
            to_account_id="a_li_current",
            confirm=True,
            mfa_code=MFA,
        )
    assert _balance(seeded_database, "a_li_current") == 850000
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


def test_redemption_is_idempotent(inv, seeded_database):
    purchased = _purchase(inv)
    payload = {
        "user_id": "u_zhang",
        "holding_id": purchased["holding_id"],
        "amount_minor": 50000,
        "confirm": True,
        "mfa_code": MFA,
        "idempotent_key": "redeem-001",
    }
    first = inv.redeem(**payload)
    second = inv.redeem(**payload)
    assert first["order_id"] == second["order_id"]
    assert second["replayed"] is True
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000 + 50000
    with seeded_database.sessions() as session:
        count = session.execute(
            select(func.count()).select_from(Order).where(Order.order_type == "product_redeem")
        ).scalar_one()
    assert count == 1
