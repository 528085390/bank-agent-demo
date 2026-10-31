import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class AppUser(Record, Base):
    __tablename__ = "app_user"
    user_id: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_reason: Mapped[str | None] = mapped_column(Text)


class LoginSession(Record, Base):
    __tablename__ = "session"
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Contact(Record, Base):
    __tablename__ = "contact"
    __table_args__ = (UniqueConstraint("user_id", "alias"),)
    contact_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    alias: Mapped[str] = mapped_column(Text)
    birthday: Mapped[date] = mapped_column(Date)
    account_ref: Mapped[str] = mapped_column(Text)


class Account(Record, Base):
    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("user_id", "account_type", "currency"),
        CheckConstraint("balance >= 0 AND locked_amount >= 0 AND balance >= locked_amount"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    account_type: Mapped[str] = mapped_column(String(20), default="CURRENT")
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    locked_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))


class OrchestrationTask(Record, Base):
    __tablename__ = "orchestration_task"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RUNNING','WAITING_USER','WAITING_TIME','REPLANNING','SUCCEEDED','FAILED','CANCELLED','HANDED_OVER')"
        ),
        CheckConstraint(
            "(state = 'WAITING_USER' AND wait_reason IS NOT NULL AND wait_reason IN ('CONFIRMATION','MFA','NEED_INPUT','HANDOVER')) OR (state <> 'WAITING_USER' AND wait_reason IS NULL)"
        ),
        CheckConstraint("plan_version > 0"),
    )
    task_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    state: Mapped[str] = mapped_column(String(24), default="RUNNING")
    wait_reason: Mapped[str | None] = mapped_column(String(24))
    intent: Mapped[str] = mapped_column(Text)
    original_message: Mapped[str] = mapped_column(Text, default="")
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_json: Mapped[dict] = mapped_column(JSONB)
    domain_ref: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrchestrationStep(Record, Base):
    __tablename__ = "orchestration_step"
    __table_args__ = (
        UniqueConstraint("task_id", "plan_version", "step_id"),
        CheckConstraint(
            "state IN ('READY','STARTED','WAITING_CONFIRMATION','WAITING_MFA','SUCCEEDED','FAILED','CANCELLED')"
        ),
        CheckConstraint("side_effect IN ('READ','WRITE')"),
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(24), default="capability")
    capability: Mapped[str | None] = mapped_column(Text)
    side_effect: Mapped[str] = mapped_column(String(5))
    depends_on: Mapped[list] = mapped_column(JSONB, default=list)
    input_json: Mapped[dict] = mapped_column(JSONB)
    output_json: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), default="READY")
    has_side_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Record, Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"), CheckConstraint("amount > 0"))
    entry_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"))
    step_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payee: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScheduledJob(Record, Base):
    __tablename__ = "scheduled_job"
    __table_args__ = (
        UniqueConstraint("task_id", "plan_version", "step_id", "kind"),
        Index("ix_scheduled_job_due", "state", "due_at"),
    )
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"))
    step_id: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)


class ChallengeMixin(Record):
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"), index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[int] = mapped_column(Integer)
    binding_hash: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB)
    call_json: Mapped[dict] = mapped_column(JSONB)
    job_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Confirmation(ChallengeMixin, Base):
    __tablename__ = "confirmation"
    confirmation_id: Mapped[str] = mapped_column(String(64), unique=True)


class MfaChallenge(ChallengeMixin, Base):
    __tablename__ = "mfa_challenge"
    mfa_id: Mapped[str] = mapped_column(String(64), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class IdempotencyKey(Record, Base):
    __tablename__ = "idempotency_key"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    scope: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Record, Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_task_time", "task_id", "created_at"),)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    request_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("orchestration_task.task_id"))
    step_id: Mapped[str | None] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClockState(Record, Base):
    __tablename__ = "clock_state"
    name: Mapped[str] = mapped_column(String(24), unique=True, default="demo")
    now: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskDailyTotal(Record, Base):
    __tablename__ = "risk_daily_total"
    __table_args__ = (UniqueConstraint("user_id", "business_date"), CheckConstraint("amount >= 0"))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    business_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
