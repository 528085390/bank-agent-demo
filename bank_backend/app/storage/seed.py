"""固定种子数据。

数据取自 `bank_backend/数据库设计文档.md` §3。历史流水一律通过
`app.domain.money_service.post_entry/post_transfer` 生成，因此账户余额由流水
推导而来：期初余额 + Σ(credit) − Σ(debit) 必然等于文档规定的期末余额。

期初余额按文档的期末余额反推：
  a_zhang_current   2465950 − 1200000 − 10000 − 5000 − 5900 + 5000 = 1250050
                    （1200000 大额消费、5×2000 餐饮、5000 转账、5900 订阅代扣、+5000 理财收益）
  a_partner_current 1195000 + 5000                                  = 1200000
消费、订阅代扣和理财收益按文档 §4 只写付款方记录（单边流水）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.money_service import CREDIT, DEBIT, post_entry, post_transfer
from app.storage.models import (
    Account,
    Card,
    Contact,
    Product,
    SecurityState,
    SimulationState,
    Subscription,
    SubscriptionCharge,
    User,
)

SEED_VERSION = "1.2"

CST = timezone(timedelta(hours=8))
# 演示基准时间：所有历史流水都落在这个时间之前的 2026-09 月内，便于账单演示。
DEMO_TIME = datetime(2026, 9, 20, 10, 0, 0, tzinfo=CST)
DEMO_DATE = date(2026, 9, 20)

DEFAULT_USER_ID = "u_zhang"

USERS = (
    {"id": "u_zhang", "name": "张三", "phone": "13800138000", "risk_level": "medium"},
    {"id": "u_li", "name": "李四", "phone": "13800138001", "risk_level": "low"},
    {"id": "u_partner", "name": "爱人", "phone": "13800138002", "risk_level": "low"},
    {"id": "u_merchant", "name": "模拟商户", "phone": "mock-merchant", "risk_level": "low"},
)

# 期初余额（整数分）。
ACCOUNTS = (
    {
        "id": "a_zhang_current",
        "user_id": "u_zhang",
        "account_type": "current",
        "opening_minor": 2465950,
        "interest_rate": Decimal("0.00300"),
    },
    {
        "id": "a_zhang_fixed",
        "user_id": "u_zhang",
        "account_type": "fixed",
        "opening_minor": 5000000,
        "interest_rate": Decimal("0.02500"),
    },
    {
        "id": "a_li_current",
        "user_id": "u_li",
        "account_type": "current",
        "opening_minor": 850000,
        "interest_rate": Decimal("0.00300"),
    },
    {
        "id": "a_partner_current",
        "user_id": "u_partner",
        "account_type": "current",
        "opening_minor": 1195000,
        "interest_rate": Decimal("0.00300"),
    },
    {
        "id": "a_merchant",
        "user_id": "u_merchant",
        "account_type": "current",
        "opening_minor": 0,
        "interest_rate": Decimal("0.00300"),
    },
)

# 文档规定的期末余额，seed 结束后必须与之相等。
EXPECTED_CLOSING_BALANCES = {
    "a_zhang_current": 1250050,
    "a_zhang_fixed": 5000000,
    "a_li_current": 850000,
    "a_partner_current": 1200000,
    "a_merchant": 0,
}

CARDS = (
    {
        "id": "card_zhang_debit",
        "user_id": "u_zhang",
        "account_id": "a_zhang_current",
        "card_type": "debit",
        "card_number": "6222 **** **** 8801",
        "daily_limit_minor": 500000,
        "monthly_limit_minor": 5000000,
        "status": "active",
    },
    {
        "id": "card_partner_debit",
        "user_id": "u_partner",
        "account_id": "a_partner_current",
        "card_type": "debit",
        "card_number": "6222 **** **** 8802",
        "daily_limit_minor": 300000,
        "monthly_limit_minor": 3000000,
        "status": "active",
    },
    {
        "id": "card_zhang_virtual",
        "user_id": "u_zhang",
        "account_id": "a_zhang_current",
        "card_type": "virtual",
        "card_number": "6222 **** **** 8803",
        "daily_limit_minor": 100000,
        "monthly_limit_minor": 1000000,
        "status": "active",
    },
)

CONTACTS = (
    {
        "id": "c_partner",
        "user_id": "u_zhang",
        "name": "爱人",
        "phone": "13800138002",
        "remark": "家人",
        "recipient_user_id": "u_partner",
        "recipient_account_id": "a_partner_current",
        # 生日 09-25，供生日联动演示使用。
        "birthday": date(1995, 9, 25),
    },
    {
        "id": "c_colleague",
        "user_id": "u_zhang",
        "name": "同事1",
        "phone": "13800138001",
        "remark": "同事",
        "recipient_user_id": "u_li",
        "recipient_account_id": "a_li_current",
        "birthday": None,
    },
    {
        "id": "c_friend",
        "user_id": "u_zhang",
        "name": "朋友1",
        "phone": "13800138009",
        "remark": "朋友",
        "recipient_user_id": None,
        "recipient_account_id": None,
        "birthday": None,
    },
)

PRODUCTS = (
    {
        "id": "p_yuebao",
        "name": "余额宝",
        "product_type": "money_fund",
        "min_amount_minor": 1,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.03500"),
        "risk_level": "low",
        "duration_months": 0,
        "redeem_rule": "flexible",
        "status": "available",
    },
    {
        "id": "p_money_fund",
        "name": "货币基金",
        "product_type": "money_fund",
        "min_amount_minor": 100,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.04200"),
        "risk_level": "low",
        "duration_months": 0,
        "redeem_rule": "flexible",
        "status": "available",
    },
    {
        "id": "p_fixed_term",
        "name": "定期理财",
        "product_type": "fixed_term",
        "min_amount_minor": 1000000,
        "max_amount_minor": 100000000,
        "expected_return": Decimal("0.04500"),
        "risk_level": "medium",
        "duration_months": 12,
        "redeem_rule": "maturity_only",
        "status": "available",
    },
)

# 历史流水使用的固定 ID，便于演示与测试直接断言。
SEED_TRANSACTION_IDS = {
    "shopping": "txn_seed_shopping",
    "dining": [f"txn_seed_dining_{index}" for index in range(1, 6)],
    "transfer_out": "txn_seed_transfer_out",
    "transfer_in": "txn_seed_transfer_in",
    "subscription": "txn_seed_netflix_20260910",
    "interest": "txn_seed_interest",
}

SEED_CHARGE_ID = "chg_seed_netflix_20260910"

SUBSCRIPTIONS = (
    {
        "id": "sub_netflix",
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "name": "Netflix",
        "merchant_name": "Netflix",
        "amount_minor": 5900,
        "frequency": "monthly",
        "start_date": date(2026, 9, 10),
        # 2026-09-10 的账期已扣，下次到期在 10 月。
        "next_due_date": date(2026, 10, 10),
        "status": "active",
        "auto_renew": True,
    },
    {
        "id": "sub_spotify",
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "name": "Spotify",
        "merchant_name": "Spotify",
        "amount_minor": 2900,
        "frequency": "monthly",
        "start_date": date(2026, 9, 1),
        "next_due_date": date(2026, 10, 1),
        "status": "active",
        "auto_renew": True,
    },
)


def _insert_users(session: Session) -> None:
    for spec in USERS:
        session.add(User(status="active", **spec))


def _insert_accounts(session: Session) -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for spec in ACCOUNTS:
        account = Account(
            id=spec["id"],
            user_id=spec["user_id"],
            account_type=spec["account_type"],
            balance_minor=spec["opening_minor"],
            available_minor=spec["opening_minor"],
            interest_rate=spec["interest_rate"],
            status="active",
        )
        session.add(account)
        accounts[account.id] = account
    session.flush()
    return accounts


def _insert_cards(session: Session) -> None:
    for spec in CARDS:
        session.add(Card(expiry_date=date(2030, 12, 31), cvv_masked="***", **spec))


def _insert_contacts(session: Session) -> None:
    for spec in CONTACTS:
        session.add(Contact(**spec))


def _insert_products(session: Session) -> None:
    for spec in PRODUCTS:
        session.add(Product(**spec))


def _insert_history(session: Session, accounts: dict[str, Account]) -> None:
    """用统一资金写入函数生成历史流水，余额随之推导。"""
    zhang = accounts["a_zhang_current"]
    partner = accounts["a_partner_current"]

    # 大额消费（异常分析的大额规则命中点）。
    post_entry(
        session,
        account=zhang,
        direction=DEBIT,
        amount_minor=1200000,
        transaction_type="consumption",
        category="shopping",
        remark="家电采购",
        transaction_id=SEED_TRANSACTION_IDS["shopping"],
        created_at=datetime(2026, 9, 18, 20, 15, tzinfo=CST),
    )

    # 10 分钟内 5 笔各 2000 分消费（高频小额 + 短时间重复规则命中点）。
    for index, transaction_id in enumerate(SEED_TRANSACTION_IDS["dining"], start=1):
        post_entry(
            session,
            account=zhang,
            direction=DEBIT,
            amount_minor=2000,
            transaction_type="consumption",
            category="dining",
            remark=f"午餐 {index}",
            transaction_id=transaction_id,
            created_at=datetime(2026, 9, 19, 12, 0, tzinfo=CST) + timedelta(minutes=2 * (index - 1)),
        )

    # 给爱人的双边转账。
    post_transfer(
        session,
        from_account=zhang,
        to_account=partner,
        amount_minor=5000,
        category="transfer",
        remark="生活费",
        reference_type="transfer",
        reference_id="ref_seed_transfer",
        created_at=datetime(2026, 9, 15, 9, 30, tzinfo=CST),
        outgoing_transaction_id=SEED_TRANSACTION_IDS["transfer_out"],
        incoming_transaction_id=SEED_TRANSACTION_IDS["transfer_in"],
    )

    # 已完成的订阅扣款（单边）。
    post_entry(
        session,
        account=zhang,
        direction=DEBIT,
        amount_minor=5900,
        transaction_type="subscription",
        category="subscription",
        remark="Netflix 订阅扣款",
        reference_type="subscription",
        reference_id="sub_netflix",
        transaction_id=SEED_TRANSACTION_IDS["subscription"],
        created_at=datetime(2026, 9, 10, 8, 0, tzinfo=CST),
    )

    # 已完成的理财收益（单边）。
    post_entry(
        session,
        account=zhang,
        direction=CREDIT,
        amount_minor=5000,
        transaction_type="interest",
        category="investment",
        remark="定期理财收益",
        reference_type="holding",
        reference_id="hold_seed_fixed",
        transaction_id=SEED_TRANSACTION_IDS["interest"],
        created_at=datetime(2026, 9, 1, 0, 5, tzinfo=CST),
    )


def _insert_subscriptions(session: Session) -> None:
    for spec in SUBSCRIPTIONS:
        session.add(Subscription(**spec))
    session.add(
        SubscriptionCharge(
            id=SEED_CHARGE_ID,
            subscription_id="sub_netflix",
            due_date=date(2026, 9, 10),
            amount_minor=5900,
            transaction_id=SEED_TRANSACTION_IDS["subscription"],
            status="completed",
        )
    )
    session.flush()
    session.get(Subscription, "sub_netflix").last_charge_id = SEED_CHARGE_ID


def _insert_simulation_state(session: Session, run_id: uuid.UUID, now: datetime) -> None:
    session.add(SimulationState(id=1, current_time=now, run_id=run_id))


def _insert_security_state(session: Session) -> None:
    # 文档未规定 security_state 种子；为让 /security/status 有稳定初值，预置零失败态。
    session.add(
        SecurityState(
            user_id=DEFAULT_USER_ID,
            tool_name="fund_transfer",
            failure_count=0,
            locked_until=None,
            last_reason="ok",
        )
    )


def apply_seed(session: Session, *, run_id: uuid.UUID, now: datetime = DEMO_TIME) -> None:
    """把固定种子写入当前事务。调用方负责 commit/rollback。"""
    _insert_users(session)
    accounts = _insert_accounts(session)
    _insert_cards(session)
    _insert_contacts(session)
    _insert_products(session)
    _insert_history(session, accounts)
    _insert_subscriptions(session)
    _insert_simulation_state(session, run_id, now)
    _insert_security_state(session)
    session.flush()
