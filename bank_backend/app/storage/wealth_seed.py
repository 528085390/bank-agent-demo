"""财富演示数据：专用用户、产品、账户和一笔样例持仓。

与 `seed.py` 的基础种子分开，避免改动 `p_yuebao` 等固定数据而影响其他域的断言。
本模块只写数据，不做清理：调用方（`scripts/seed_wealth_demo.py` 或测试）负责事务与
目标库的选择，重复执行不会重复入金——已存在完整演示数据时校验并复用，
只存在一部分时明确拒绝，不静默覆盖。

账户资金一律经过 `money_service.post_entry`，因此余额与流水始终可对账；
样例持仓建仓也走同一条扣款路径，不会被误当成新购持仓。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.domain.money_service import CREDIT, DEBIT, post_entry, reconcile
from app.storage.models import Account, Holding, Order, Product, User

WEALTH_USER = "u_wealth"
WEALTH_ALT_USER = "u_wealth_other"

WEALTH_USERS = (
    {
        "id": WEALTH_USER,
        "name": "演示理财客户",
        "phone": "13700000001",
        "risk_level": "low",
    },
    {
        "id": WEALTH_ALT_USER,
        "name": "演示其他客户",
        "phone": "13700000002",
        "risk_level": "low",
    },
)

# 四款虚构产品，数值与需求文档 §8.1 一致。
WEALTH_PRODUCTS = (
    {
        "id": "p_demo_cash",
        "name": "演示灵活理财",
        "product_type": "money_fund",
        "min_amount_minor": 10000,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.02000"),
        "risk_level": "low",
        "duration_months": 0,
        "redeem_rule": "flexible",
        "status": "available",
    },
    {
        "id": "p_demo_stable",
        "name": "演示稳健理财",
        "product_type": "fixed_term",
        "min_amount_minor": 100000,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.03500"),
        "risk_level": "medium",
        "duration_months": 3,
        "redeem_rule": "maturity_only",
        "status": "available",
    },
    {
        "id": "p_demo_growth",
        "name": "演示进取理财",
        "product_type": "fixed_term",
        "min_amount_minor": 500000,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.06000"),
        "risk_level": "high",
        "duration_months": 12,
        "redeem_rule": "maturity_only",
        "status": "available",
    },
    {
        "id": "p_demo_closed",
        "name": "演示停售产品",
        "product_type": "money_fund",
        "min_amount_minor": 10000,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.09000"),
        "risk_level": "low",
        "duration_months": 0,
        "redeem_rule": "flexible",
        "status": "sold_out",
    },
)

# 期初入金金额：主账户先入 21000 元，样例建仓扣 1000 元后余额为 20000 元。
WEALTH_ACCOUNTS = (
    {
        "id": "a_wealth_main",
        "user_id": WEALTH_USER,
        "account_type": "current",
        "opening_credit_minor": 2100000,
        "interest_rate": Decimal("0.00100"),
        "opening_transaction_id": "txn_wealth_open_main",
    },
    {
        "id": "a_wealth_backup",
        "user_id": WEALTH_USER,
        "account_type": "current",
        "opening_credit_minor": 500000,
        "interest_rate": Decimal("0.00100"),
        "opening_transaction_id": "txn_wealth_open_backup",
    },
    {
        "id": "a_wealth_other",
        "user_id": WEALTH_ALT_USER,
        "account_type": "current",
        "opening_credit_minor": 100000,
        "interest_rate": Decimal("0.00100"),
        "opening_transaction_id": "txn_wealth_open_other",
    },
)

EXPECTED_BALANCES = {
    "a_wealth_main": 2000000,
    "a_wealth_backup": 500000,
    "a_wealth_other": 100000,
}

SAMPLE_HOLDING_ID = "h_wealth_sample"
SAMPLE_ORDER_ID = "order_wealth_sample"
SAMPLE_PURCHASE_TRANSACTION_ID = "txn_wealth_sample_buy"
SAMPLE_PRINCIPAL_MINOR = 100000
SAMPLE_VALUE_MINOR = 100500
SAMPLE_AGE_DAYS = 30

PRODUCT_IDS = tuple(spec["id"] for spec in WEALTH_PRODUCTS)
ACCOUNT_IDS = tuple(spec["id"] for spec in WEALTH_ACCOUNTS)
USER_IDS = tuple(spec["id"] for spec in WEALTH_USERS)


def demo_status(session: Session) -> str:
    """`missing`／`partial`／`seeded`：判断能否安全初始化。"""

    present = sum(
        1
        for model, ids in (
            (User, USER_IDS),
            (Product, PRODUCT_IDS),
            (Account, ACCOUNT_IDS),
        )
        for identifier in ids
        if session.get(model, identifier) is not None
    )
    total = len(USER_IDS) + len(PRODUCT_IDS) + len(ACCOUNT_IDS)
    if present == 0:
        return "missing"
    if present == total:
        return "seeded"
    return "partial"


def apply_wealth_seed(session: Session, *, now: datetime) -> dict:
    """在调用方事务内写入演示数据；已存在则校验后复用。"""

    state = demo_status(session)
    if state == "seeded":
        return _verify_existing(session)
    if state == "partial":
        raise BusinessError(
            "财富演示数据只存在一部分（用户／产品／账户不齐全），请人工确认后再初始化，"
            "脚本不会覆盖或补齐已有数据"
        )

    for spec in WEALTH_USERS:
        session.add(User(status="active", **spec))
    for spec in WEALTH_PRODUCTS:
        session.add(Product(**spec))
    session.flush()

    accounts: dict[str, Account] = {}
    for spec in WEALTH_ACCOUNTS:
        account = Account(
            id=spec["id"],
            user_id=spec["user_id"],
            account_type=spec["account_type"],
            balance_minor=0,
            available_minor=0,
            interest_rate=spec["interest_rate"],
            status="active",
        )
        session.add(account)
        accounts[account.id] = account
    session.flush()

    # 期初入金与样例建仓都走统一资金写入，余额可由流水推导。
    for spec in WEALTH_ACCOUNTS:
        post_entry(
            session,
            account=accounts[spec["id"]],
            direction=CREDIT,
            amount_minor=spec["opening_credit_minor"],
            transaction_type="transfer_in",
            category="investment",
            remark="财富演示期初入金",
            created_at=now,
            transaction_id=spec["opening_transaction_id"],
        )

    cash = next(spec for spec in WEALTH_PRODUCTS if spec["id"] == "p_demo_cash")
    purchased_at = now - timedelta(days=SAMPLE_AGE_DAYS)
    entry = post_entry(
        session,
        account=accounts["a_wealth_main"],
        direction=DEBIT,
        amount_minor=SAMPLE_PRINCIPAL_MINOR,
        transaction_type="purchase",
        category="investment",
        remark=f"申购 {cash['name']}（演示样例持仓）",
        reference_type="order",
        reference_id=SAMPLE_ORDER_ID,
        created_at=purchased_at,
        transaction_id=SAMPLE_PURCHASE_TRANSACTION_ID,
    )
    session.add(
        Holding(
            id=SAMPLE_HOLDING_ID,
            user_id=WEALTH_USER,
            product_id="p_demo_cash",
            source_account_id="a_wealth_main",
            principal_minor=SAMPLE_PRINCIPAL_MINOR,
            # 样例持仓的价值快照用于收益查询演示，不由计息接口推导。
            current_value_minor=SAMPLE_VALUE_MINOR,
            expected_return=cash["expected_return"],
            purchased_at=purchased_at,
            maturity_date=None,
            status="holding",
        )
    )
    session.add(
        Order(
            id=SAMPLE_ORDER_ID,
            user_id=WEALTH_USER,
            order_type="product_purchase",
            product_id="p_demo_cash",
            holding_id=SAMPLE_HOLDING_ID,
            from_account_id="a_wealth_main",
            amount_minor=SAMPLE_PRINCIPAL_MINOR,
            status="completed",
            details={
                "product_name": cash["name"],
                "expected_return": float(cash["expected_return"]),
                "term_months": 0,
                "redeem_rule": cash["redeem_rule"],
                "sample": True,
            },
            transaction_id=entry.id,
            created_at=purchased_at,
        )
    )
    session.flush()

    _assert_expected_state(session)
    return {**_summary(session), "status": "created"}


def _verify_existing(session: Session) -> dict:
    """已有演示数据时只校验不写入；不一致则拒绝，不静默覆盖。"""

    _assert_expected_state(session)
    return {**_summary(session), "status": "reused"}


def _assert_expected_state(session: Session) -> None:
    for account_id, expected in EXPECTED_BALANCES.items():
        account = session.get(Account, account_id)
        if account is None:
            raise BusinessError(f"财富演示账户缺失: {account_id}")
        actual = reconcile(session, account, opening_balance_minor=0)
        if actual != expected:
            raise BusinessError(
                f"财富演示账户 {account_id} 余额 {actual} 与预期 {expected} 不一致，"
                "请人工确认演示库状态"
            )

    holding = session.get(Holding, SAMPLE_HOLDING_ID)
    if holding is None:
        raise BusinessError(f"财富演示样例持仓缺失: {SAMPLE_HOLDING_ID}")
    if holding.principal_minor != SAMPLE_PRINCIPAL_MINOR:
        raise BusinessError(
            f"样例持仓本金 {holding.principal_minor} 与预期 {SAMPLE_PRINCIPAL_MINOR} 不一致"
        )


def _summary(session: Session) -> dict:
    holding = session.get(Holding, SAMPLE_HOLDING_ID)
    return {
        "users": list(USER_IDS),
        "products": list(PRODUCT_IDS),
        "accounts": {
            account_id: session.get(Account, account_id).balance_minor
            for account_id in ACCOUNT_IDS
        },
        "sample_holding": {
            "holding_id": SAMPLE_HOLDING_ID,
            "order_id": SAMPLE_ORDER_ID,
            "principal_minor": holding.principal_minor if holding else None,
            "current_value_minor": holding.current_value_minor if holding else None,
        },
    }
