"""模拟银行系统 18 张领域表的 SQLAlchemy 模型。

字段、外键、索引、唯一约束和固定 ID 以 `bank_backend/数据库设计文档.md` 为准。
所有外键使用 ON DELETE RESTRICT；金额统一 BIGINT 整数分（CNY），时间统一 TIMESTAMPTZ。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.card_password import initial_password_hash


class Base(DeclarativeBase):
    """领域表基类。"""


# 枚举值集合。数据库用 CHECK 约束固化，应用层复用同一份定义，避免两处漂移。
ACCOUNT_TYPES = ("current", "fixed")
ACCOUNT_STATUSES = ("active", "closed")
CARD_TYPES = ("debit", "credit", "virtual")
CARD_STATUSES = ("active", "frozen", "blocked")
DIRECTIONS = ("debit", "credit")
TRANSACTION_TYPES = (
    "transfer",
    "transfer_in",
    "purchase",
    "redeem",
    "interest",
    "subscription",
    "aa",
    "consumption",
    "birthday",
)
TRANSACTION_STATUSES = ("pending", "completed", "failed", "cancelled")
PRODUCT_STATUSES = ("available", "sold_out")
# 产品赎回规则：flexible 随时可赎回，maturity_only 只在到期后可赎回。
REDEEM_RULES = ("flexible", "maturity_only")
RISK_LEVELS = ("low", "medium", "high")
HOLDING_STATUSES = ("holding", "redeemed", "partial")
ORDER_TYPES = ("product_purchase", "product_redeem", "birthday", "card_application")
ORDER_STATUSES = ("pending", "completed", "failed", "cancelled")
TASK_TYPES = ("scheduled_transfer", "birthday_order", "subscription_charge")
TASK_STATUSES = ("scheduled", "running", "completed", "failed", "cancelled", "expired")
AA_STATUSES = ("open", "partial", "completed", "cancelled", "expired")
AA_PARTICIPANT_STATUSES = ("pending", "paid", "refused")
FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
SUBSCRIPTION_STATUSES = ("active", "paused", "cancelled")
CHARGE_STATUSES = ("completed", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    """生成 `column IN ('a', 'b')` 形式的 CHECK 表达式。"""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(128))
    birthday: Mapped[date | None] = mapped_column(Date)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    accounts: Mapped[list[Account]] = relationship(back_populates="user")
    cards: Mapped[list[Card]] = relationship(back_populates="user")
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="owner", foreign_keys="Contact.user_id"
    )
    transactions: Mapped[list[Transaction]] = relationship(back_populates="user")
    holdings: Mapped[list[Holding]] = relationship(back_populates="user")
    orders: Mapped[list[Order]] = relationship(back_populates="user")
    scheduled_tasks: Mapped[list[ScheduledTask]] = relationship(back_populates="user")
    aa_requests: Mapped[list[AaRequest]] = relationship(back_populates="user")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user")
    security_states: Mapped[list[SecurityState]] = relationship(back_populates="user")
    risk_assessments: Mapped[list[WealthRiskAssessment]] = relationship(back_populates="user")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(_in("account_type", ACCOUNT_TYPES), name="ck_accounts_type"),
        CheckConstraint(_in("status", ACCOUNT_STATUSES), name="ck_accounts_status"),
        CheckConstraint("balance_minor >= 0", name="ck_accounts_balance_non_negative"),
        CheckConstraint("available_minor >= 0", name="ck_accounts_available_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="CNY", server_default="CNY"
    )
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 冗余读模型字段：每次资金事务必须与 balance_minor 一起更新。
    available_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    interest_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 5), nullable=False, default=Decimal("0")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="accounts")
    cards: Mapped[list[Card]] = relationship(back_populates="account")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="account", foreign_keys="Transaction.account_id"
    )
    counterparty_transactions: Mapped[list[Transaction]] = relationship(
        back_populates="counterparty_account",
        foreign_keys="Transaction.counterparty_account_id",
    )


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (
        CheckConstraint(_in("card_type", CARD_TYPES), name="ck_cards_type"),
        CheckConstraint(_in("status", CARD_STATUSES), name="ck_cards_status"),
        CheckConstraint("daily_limit_minor >= 0", name="ck_cards_daily_limit"),
        CheckConstraint("monthly_limit_minor >= 0", name="ck_cards_monthly_limit"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    # 仅保存脱敏卡号，绝不写入完整卡号。
    card_number: Mapped[str] = mapped_column(String(32), nullable=False)
    card_type: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    cvv_masked: Mapped[str | None] = mapped_column(String(8))
    daily_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    monthly_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    merchant_blacklist: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    freeze_reason: Mapped[str | None] = mapped_column(String(16))
    # Private bank-only credentials: never add to card_view, snapshots, or audit values.
    transaction_password_hash: Mapped[str | None] = mapped_column(
        String(256), default=lambda: initial_password_hash()
    )
    password_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    password_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    password_code_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    password_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="cards")
    account: Mapped[Account] = relationship(back_populates="cards")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 姓名可重复，用于模糊匹配演示。
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    remark: Mapped[str | None] = mapped_column(Text)
    recipient_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    recipient_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    birthday: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    owner: Mapped[User] = relationship(back_populates="contacts", foreign_keys=[user_id])
    recipient_user: Mapped[User | None] = relationship(foreign_keys=[recipient_user_id])
    recipient_account: Mapped[Account | None] = relationship(foreign_keys=[recipient_account_id])


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(_in("direction", DIRECTIONS), name="ck_transactions_direction"),
        CheckConstraint(_in("transaction_type", TRANSACTION_TYPES), name="ck_transactions_type"),
        CheckConstraint(_in("status", TRANSACTION_STATUSES), name="ck_transactions_status"),
        CheckConstraint("amount_minor > 0", name="ck_transactions_amount_positive"),
        Index(
            "ix_transactions_user_created_type",
            "user_id",
            text("created_at DESC"),
            "transaction_type",
        ),
        Index("ix_transactions_account_created", "account_id", text("created_at DESC")),
        Index("ix_transactions_reference", "reference_type", "reference_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    counterparty_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    remark: Mapped[str | None] = mapped_column(Text)
    # 一笔双边资金操作生成两条 transaction，共享 reference_id。
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="transactions")
    account: Mapped[Account] = relationship(
        back_populates="transactions", foreign_keys=[account_id]
    )
    counterparty_account: Mapped[Account | None] = relationship(
        back_populates="counterparty_transactions", foreign_keys=[counterparty_account_id]
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(_in("status", PRODUCT_STATUSES), name="ck_products_status"),
        CheckConstraint(_in("redeem_rule", REDEEM_RULES), name="ck_products_redeem_rule"),
        CheckConstraint("min_amount_minor >= 0", name="ck_products_min_amount"),
        CheckConstraint("max_amount_minor >= min_amount_minor", name="ck_products_amount_range"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_type: Mapped[str] = mapped_column(String(32), nullable=False)
    min_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_return: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_months: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 赎回规则由产品定义，不由客户端指定；旧数据在 0002 迁移中按期限回填。
    redeem_rule: Mapped[str] = mapped_column(
        String(16), nullable=False, default="flexible", server_default="flexible"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="available")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    holdings: Mapped[list[Holding]] = relationship(back_populates="product")


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (
        CheckConstraint(_in("status", HOLDING_STATUSES), name="ck_holdings_status"),
        CheckConstraint("principal_minor >= 0", name="ck_holdings_principal"),
        CheckConstraint("current_value_minor >= 0", name="ck_holdings_current_value"),
        Index("ix_holdings_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    source_account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    principal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_return: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    maturity_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="holding")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="holdings")
    product: Mapped[Product] = relationship(back_populates="holdings")
    source_account: Mapped[Account] = relationship(foreign_keys=[source_account_id])


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("order_type", ORDER_TYPES), name="ck_orders_type"),
        CheckConstraint(_in("status", ORDER_STATUSES), name="ck_orders_status"),
        CheckConstraint("amount_minor >= 0", name="ck_orders_amount_non_negative"),
        Index("ix_orders_user_created", "user_id", text("created_at DESC")),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    holding_id: Mapped[str | None] = mapped_column(ForeignKey("holdings.id", ondelete="RESTRICT"))
    from_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    to_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    details: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    transaction_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="orders")
    product: Mapped[Product | None] = relationship(foreign_keys=[product_id])
    holding: Mapped[Holding | None] = relationship(foreign_keys=[holding_id])
    from_account: Mapped[Account | None] = relationship(foreign_keys=[from_account_id])
    to_account: Mapped[Account | None] = relationship(foreign_keys=[to_account_id])


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint(_in("task_type", TASK_TYPES), name="ck_scheduled_tasks_type"),
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_scheduled_tasks_status"),
        Index("ix_scheduled_tasks_status_execute_at", "status", "execute_at"),
        Index("ix_scheduled_tasks_user_created", "user_id", text("created_at DESC")),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    execute_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="scheduled_tasks")


class AaRequest(Base):
    __tablename__ = "aa_requests"
    __table_args__ = (
        CheckConstraint(_in("status", AA_STATUSES), name="ck_aa_requests_status"),
        CheckConstraint("total_amount_minor > 0", name="ck_aa_requests_total_positive"),
        CheckConstraint("participant_count >= 2", name="ck_aa_requests_participant_count"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="aa_requests")
    from_account: Mapped[Account] = relationship(foreign_keys=[from_account_id])
    participants: Mapped[list[AaParticipant]] = relationship(
        back_populates="aa_request", order_by="AaParticipant.position"
    )


class AaParticipant(Base):
    __tablename__ = "aa_participants"
    __table_args__ = (
        CheckConstraint(_in("status", AA_PARTICIPANT_STATUSES), name="ck_aa_participants_status"),
        CheckConstraint("amount_minor > 0", name="ck_aa_participants_amount_positive"),
        UniqueConstraint("aa_request_id", "id", name="uq_aa_participants_request_id"),
        UniqueConstraint("aa_request_id", "position", name="uq_aa_participants_position"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aa_request_id: Mapped[str] = mapped_column(
        ForeignKey("aa_requests.id", ondelete="RESTRICT"), nullable=False
    )
    # 参与者在 AA 请求内的序号，接口以 participant_index 定位分摊项。
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    transaction_id: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    aa_request: Mapped[AaRequest] = relationship(back_populates="participants")
    account: Mapped[Account | None] = relationship(foreign_keys=[account_id])


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(_in("frequency", FREQUENCIES), name="ck_subscriptions_frequency"),
        CheckConstraint(_in("status", SUBSCRIPTION_STATUSES), name="ck_subscriptions_status"),
        CheckConstraint("amount_minor > 0", name="ck_subscriptions_amount_positive"),
        Index("ix_subscriptions_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    from_account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    next_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_charge_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    from_account: Mapped[Account] = relationship(foreign_keys=[from_account_id])
    charges: Mapped[list[SubscriptionCharge]] = relationship(back_populates="subscription")


class SubscriptionCharge(Base):
    __tablename__ = "subscription_charges"
    __table_args__ = (
        CheckConstraint(_in("status", CHARGE_STATUSES), name="ck_subscription_charges_status"),
        CheckConstraint("amount_minor > 0", name="ck_subscription_charges_amount_positive"),
        # 每个账期只生成一次扣款。
        UniqueConstraint("subscription_id", "due_date", name="uq_subscription_charges_due_date"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    subscription: Mapped[Subscription] = relationship(back_populates="charges")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_created", "user_id", text("created_at DESC")),
        Index("ix_audit_logs_request_id", "request_id"),
        Index("ix_audit_logs_action", "action"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 审计不设外键：失败或未识别用户的写操作也必须留痕。
    user_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    permission_level: Mapped[str | None] = mapped_column(String(16))
    confirm_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mfa_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CardAction(Base):
    """An immutable card-operation intent and its user authorization."""

    __tablename__ = "card_actions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_card_actions_user_key"),
        CheckConstraint(
            "status IN ('pending', 'authorized', 'cancelled', 'completed')",
            name="ck_card_actions_status",
        ),
        CheckConstraint("permission_level IN ('yellow', 'red')", name="ck_card_actions_permission"),
        CheckConstraint("verification_failures >= 0", name="ck_card_actions_failures"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    before: Mapped[dict] = mapped_column(JSONB, nullable=False)
    permission_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    verification_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_password_version: Mapped[int | None] = mapped_column(Integer)
    pending_password_hash: Mapped[str | None] = mapped_column(String(256))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (Index("ix_idempotency_records_created", text("created_at DESC")),)

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotent_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SimulationState(Base):
    __tablename__ = "simulation_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_simulation_state_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SecurityState(Base):
    __tablename__ = "security_state"
    __table_args__ = (
        UniqueConstraint("user_id", "tool_name", name="uq_security_state_user_tool"),
        CheckConstraint("failure_count >= 0", name="ck_security_state_failure_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="security_states")


class WealthRiskAssessment(Base):
    """风险测评记录。

    每次完成问卷追加一条，不覆盖历史。等级由银行按 `wealth-risk-v1` 规则计算，
    客户端只能提交五题答案，不能指定最终等级。
    """

    __tablename__ = "wealth_risk_assessments"
    __table_args__ = (
        CheckConstraint(_in("risk_level", RISK_LEVELS), name="ck_wealth_risk_level"),
        CheckConstraint("score >= 0 AND score <= 10", name="ck_wealth_risk_score"),
        Index(
            "ix_wealth_risk_user_assessed",
            "user_id",
            text("assessed_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 五题编号 -> 选项（0/1/2），原样保存问卷答案。
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="risk_assessments")


# 18 张领域表，顺序与数据库设计文档一致。
DOMAIN_TABLES = (
    User,
    Account,
    Card,
    CardAction,
    Contact,
    Transaction,
    Product,
    Holding,
    Order,
    ScheduledTask,
    AaRequest,
    AaParticipant,
    Subscription,
    SubscriptionCharge,
    AuditLog,
    IdempotencyRecord,
    SimulationState,
    SecurityState,
    WealthRiskAssessment,
)
