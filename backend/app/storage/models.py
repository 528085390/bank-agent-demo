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
    Uuid,
    false,
    func,
    text,
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
    # No `display_name`: an account has exactly one name, and it is the login name held in
    # `auth_credential`. A second free-text name could disagree with the first and nothing
    # could validate either, so it was dropped in 0019.
    email: Mapped[str | None] = mapped_column(String(255))
    # The identity bank_backend knows this user by. Nullable: a user may exist here before
    # it exists there, and a tool must fail closed on the gap rather than let the bank
    # answer for its own default customer.
    bank_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # `disabled` is a kill switch, not a soft delete: every login session of a disabled user
    # is refused at authentication time, so an account can be stopped without deleting the
    # conversations that reference it.
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    # Only `admin` may touch global controls (model configuration, the simulated clock).
    # Nothing in the API can promote a user: the column is written by ops scripts alone.
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuthCredential(Record, Base):
    """The one password a user logs in with.

    Separate from `AppUser` because a user can exist without a credential: the demo archive
    `user_0001` keeps its conversations and bank binding while having no way to log in until
    ops gives it a password. Keeping the hash in its own row also means an authentication
    lookup never loads the user profile.
    """

    __tablename__ = "auth_credential"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.user_id"), unique=True, nullable=False
    )
    # Normalised (stripped, casefolded) before it is stored, and unique on that form, so two
    # users cannot both register "Alice" and "alice".
    login_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoginSession(Record, Base):
    __tablename__ = "session"
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The browser holds a random token; this is its digest. Storing only the digest means a
    # database read cannot be replayed as a login. Nullable because rows written before
    # credentials existed have no token and must stay unusable rather than fall back to
    # authenticating by `session_id`.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Bound to this login, not to the user: a token stolen from one session is useless in
    # another.
    csrf_hash: Mapped[str | None] = mapped_column(String(64))
    # Idle timeout baseline. Written at most once a minute per session (see the service), so
    # a busy conversation does not turn every request into an UPDATE.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Non-null means revoked. Checked before expiry, so logout takes effect on the next
    # request rather than whenever the row happens to expire.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The hard ceiling a session cannot outlive, whatever the user does. `expires_at` stays
    # for the legacy rows and readers; new sessions set both to the same instant.
    absolute_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BankCustomerPool(Base):
    """Pre-provisioned bank customers, claimed one per registration.

    The customer itself lives in bank_backend; this table only tracks who took which id.
    Claiming is a single UPDATE with `FOR UPDATE SKIP LOCKED`, so two concurrent
    registrations cannot take the same customer and neither has to wait for the other.

    The customer id *is* the primary key -- there is no surrogate `id` here, unlike the
    `Record`-derived tables: the row has no identity of its own beyond which customer it
    describes, and a second unique column would only add a way for the two to disagree.
    """

    __tablename__ = "bank_customer_pool"
    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claimed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.user_id"), unique=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Record, Base):
    """A shared failure counter for login throttling.

    In the database rather than in memory because the limit must hold across workers: an
    attacker who gets five tries per worker gets five times the budget from a memory-only
    counter. One row per (scope, key), where scope is `account` or `ip`.
    """

    __tablename__ = "login_attempt"
    __table_args__ = (UniqueConstraint("scope", "key"),)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
            "state IN ('RUNNING','WAITING_USER','REPLANNING','SUCCEEDED','FAILED','CANCELLED')"
        ),
        CheckConstraint("plan_version > 0"),
    )
    task_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.session_id"))
    state: Mapped[str] = mapped_column(String(24), default="RUNNING")
    intent: Mapped[str] = mapped_column(Text)
    original_message: Mapped[str] = mapped_column(Text, default="")
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrchestrationStep(Record, Base):
    __tablename__ = "orchestration_step"
    __table_args__ = (
        UniqueConstraint("task_id", "plan_version", "step_id"),
        CheckConstraint("state IN ('READY','STARTED','SUCCEEDED','FAILED','CANCELLED')"),
        CheckConstraint("side_effect IN ('READ','WRITE')"),
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    capability: Mapped[str] = mapped_column(Text)
    side_effect: Mapped[str] = mapped_column(String(5))
    depends_on: Mapped[list] = mapped_column(JSONB, default=list)
    input_json: Mapped[dict] = mapped_column(JSONB)
    output_json: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), default="READY")
    has_side_effect: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Record, Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (CheckConstraint("amount > 0"),)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("orchestration_task.task_id"))
    step_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payee: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailNotification(Record, Base):
    __tablename__ = "email_notification"
    __table_args__ = (
        UniqueConstraint("notification_id"),
        Index("ix_email_notification_task", "task_id"),
        Index("ix_email_notification_status", "status", "created_at"),
    )
    notification_id: Mapped[str] = mapped_column(String(64))
    #: 关联号，不是主键。分析报告的拼法是 ``analysis_{user_id}_{period}``，真实账号的
    #: user_id 有 37 字符，整串会超过 64（见迁移 0021）。
    task_id: Mapped[str] = mapped_column(String(128))
    recipient_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500))
    email_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Conversation(Record, Base):
    """A user-visible chat container, and the thread its graph state lives on.

    `thread_id` is stored rather than derived so that a conversation migrated from the old
    `chat:{session_id}` scheme keeps its checkpoint: the thread is a fact about this row,
    not a formula. `next_message_sequence` lives on the row because the sequence is
    per-conversation, and allocating it means locking this row.

    `serial_no` is the small per-owner number the user sees (1, 2, 3): assigned once, never
    recycled, kept by archiving and soft-deleting, and unique within its owner by
    `uq_conversation_serial_no_owner`.
    """

    __tablename__ = "conversation"
    __table_args__ = (
        Index("ix_conversation_owner_state", "user_id", "deleted_at", "archived_at"),
        UniqueConstraint("user_id", "serial_no", name="uq_conversation_serial_no_owner"),
    )
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True)
    serial_no: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_message_sequence: Mapped[int] = mapped_column(BigInteger, default=1)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True)


class ConversationMessage(Record, Base):
    """The append-only ledger of what the user and the assistant actually saw.

    A row is never edited: a correction is a new message. `metadata` is spelled `meta` on
    the class because `metadata` is reserved on a declarative class (it is `Base.metadata`),
    so the column keeps the documented name while the attribute does not collide with it.

    `run_id` is a correlation id, not a foreign key and not a lifecycle: nothing here says
    whether the graph call it names ran, finished or failed.
    """

    __tablename__ = "conversation_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_conversation_message_sequence"),
        CheckConstraint("role IN ('user','assistant')", name="ck_conversation_message_role"),
        CheckConstraint(
            "(message_kind = 'interrupt_answer' AND question_id IS NOT NULL)"
            " OR (message_kind <> 'interrupt_answer' AND question_id IS NULL)",
            name="ck_conversation_message_question",
        ),
        Index("ix_conversation_message_run", "run_id"),
    )
    message_id: Mapped[str] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.conversation_id"))
    run_id: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    message_kind: Mapped[str] = mapped_column(String(32), default="message")
    question_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class ConversationSubmission(Record, Base):
    """The idempotency receipt for one client write.

    It records that a key was spent, not that a run is happening: there is no state column,
    no attempt and no lease. The unique key is per user, so two users may use the same
    string, and a key reused across conversations is caught by comparing `request_hash`
    rather than by the constraint.
    """

    __tablename__ = "conversation_submission"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "operation_type",
            "idempotency_key",
            name="uq_conversation_submission_key",
        ),
    )
    submission_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    operation_type: Mapped[str] = mapped_column(String(32), default="conversation_message")
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.conversation_id"))
    message_id: Mapped[str] = mapped_column(ForeignKey("conversation_message.message_id"))
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- the audit domain (migration 0008) -------------------------------------------------
#
# These two tables are deliberately not part of the conversation above. They hold what the
# model was actually sent and returned, and what the run actually did, which is a different
# fact from what the user and the assistant saw. Neither table is ever updated: a correction
# is another row, and the ordinary conversation API never reads either one.


class LlmCallAudit(Record, Base):
    """One attempt at one model call, with the whole request and the whole answer.

    The calls that failed are here too, and so are the ones no conversation asked for -- a
    configuration probe, a startup probe. That is the point of the table: it is the record of
    every interaction with a provider, not only the ones that produced a visible reply.

    The correlation columns are plain strings rather than foreign keys, on purpose: an audit
    record must be writable for a conversation that has been deleted, and deleting a
    conversation must never delete the audit trail. Nothing here cascades.

    `prompt_json` and `response_json` are the raw material and are never served by the
    ordinary conversation API; `response_text` is the normalized text the agents parse.
    Secrets -- the API key above all -- are never written here.
    """

    __tablename__ = "llm_call_audit"
    __table_args__ = (
        CheckConstraint("result IN ('SUCCEEDED','FAILED')", name="ck_llm_call_audit_result"),
        Index("ix_llm_call_audit_conversation", "conversation_id", "created_at"),
        Index("ix_llm_call_audit_run", "run_id"),
        Index("ix_llm_call_audit_created", "created_at"),
    )
    audit_id: Mapped[str] = mapped_column(String(64), unique=True)
    # One attempt, one id. A retry is a second call with its own id, never an update.
    call_id: Mapped[str] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(String(64))
    # Where this call came from: a conversation, a legacy session, the settings panel's test
    # or install probe, or the environment bootstrap. Nullable for a caller that names none.
    source: Mapped[str | None] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    request_format: Mapped[str] = mapped_column(String(32), default="chat_completions")
    # Whether this attempt was streamed. The other columns say what was sent and what came
    # back; this one says how, which is the only thing that tells a streamed call apart from a
    # buffered one when both end up looking the same in response_text.
    stream: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    prompt_json: Mapped[dict] = mapped_column(JSONB)
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    response_text: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Null when the provider reported no usage. A missing count is a fact, not a zero.
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRunEvent(Record, Base):
    """One append-only fact about a run: started, a tool call, a pause, an outcome.

    These events are an audit trail and not a second execution state: the LangGraph
    checkpoint remains the only thing that says what a run may do next. Nothing here is
    updated, and nothing reads these rows to decide anything.

    A tool event carries the model's arguments and the adapter's complete `ToolResult`,
    including the module-private `facts` that the ordinary SSE frame projects away.
    """

    __tablename__ = "agent_run_event"
    __table_args__ = (
        Index("ix_agent_run_event_conversation", "conversation_id", "created_at"),
        Index("ix_agent_run_event_run", "run_id", "created_at"),
        Index("ix_agent_run_event_call", "call_id"),
        Index("ix_agent_run_event_type", "event_type", "created_at"),
    )
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    # The tool call this event is about, for a tool event. A model call id is a different
    # namespace and is never written here.
    #
    # 256 holds the id the Supervisor mints -- `agent_id:tool_name:uuid4().hex` -- at the
    # maxima this same table declares: agent_id 64 + ':' + tool_name 128 + ':' + 32 = 226.
    # At 64, `subscription_execute` minted 66 and its audit INSERT raised
    # StringDataRightTruncation before the adapter was ever handed the call (migration 0013).
    call_id: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(16))
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- prompt-layer compression (migration 0009) -----------------------------------------
#
# `context_summary` is a derived view of the ledger, not a second copy of it.
# `conversation_message` remains the single source of truth for what the user and the
# assistant saw; this table only records that a stretch of that ledger was summarised, by
# which summary, and what came out. Nothing about a run's lifecycle lives here -- no state,
# no turn pointer, no lease -- because a row here must never be able to disagree with the
# ledger about what happened. Its reader is the compression step, and a version that is
# found to be wrong is replaced by a later version rather than corrected in place.


class ContextSummary(Record, Base):
    """One derived summary of a contiguous stretch of a conversation's ledger.

    `covered_from_sequence` and `covered_to_sequence` are inclusive bounds into
    `ConversationMessage.sequence`, and they are plain `BigInteger` columns rather than
    foreign keys because they name a *range*, whose two ends are ordinary integers, not two
    particular rows.

    `base_summary_id` is the summary this one was derived from, and it is a plain string
    with no self-referencing foreign key. The chain's integrity is checked by the
    application layer, which writes the versions in order; a self-referencing key would
    make each row's insertability depend on its predecessor already being present, so a
    write that the application has already ordered would have to satisfy the database's
    order as well. It is nullable because version 1 summarises raw ledger messages and was
    derived from no summary at all.

    `source_hash` is the hash of the exact covered messages the summary was built from, so
    a reader can tell whether the summary still describes what the ledger now holds.
    """

    __tablename__ = "context_summary"
    __table_args__ = (
        UniqueConstraint("conversation_id", "version", name="uq_context_summary_version"),
        # Descending on `version`: spec §5.1 fixes the order, and it is the newest-first one.
        Index("ix_context_summary_conversation_version", "conversation_id", text("version DESC")),
    )
    summary_id: Mapped[str] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.conversation_id"))
    version: Mapped[int] = mapped_column(Integer)
    base_summary_id: Mapped[str | None] = mapped_column(String(64))
    covered_from_sequence: Mapped[int] = mapped_column(BigInteger)
    covered_to_sequence: Mapped[int] = mapped_column(BigInteger)
    summary_text: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- business audit (migration 0010) ---------------------------------------------------
#
# `business_audit_event` records the business and configuration changes *this service*
# makes: renaming, pinning, archiving, soft-deleting and restoring a conversation, editing
# the LLM configuration, and the fund writes it sends to the bank. The bank's own ledger
# stays in `bank_backend.audit_logs`: a row here records what *this* service did -- which
# write, for whom, and whether the bank took it -- not a second copy of the bank's books.
#
# The correlation columns are plain strings rather than foreign keys, for the reason
# `LlmCallAudit` gives: an audit row must stay writable and readable after the
# conversation it names has been deleted, and deleting a conversation must never delete
# the audit trail. Nothing here cascades.
#
# No old/new snapshot is stored. The row says what was done and why, not the whole state
# on either side of it -- a full snapshot would be a second copy of data that can drift
# from the tables it copies. Like the other audit tables, a row here is never updated.


class BusinessAuditEvent(Record, Base):
    """One business or configuration change made through this service.

    `event_id` is unique: one change, one id. A retry that is genuinely a new change gets
    its own id, never an update to an existing row.

    `user_id`, `conversation_id`, `run_id` and `request_id` are all nullable, because a
    change may arrive from a background job with no conversation and no request behind it.
    They are plain strings, not foreign keys -- see the note above.

    `result` is closed at `ok`, `rejected` or `failed`, and the database itself refuses any
    fourth value (`ck_business_audit_event_result`). Two of the three are written: `ok` for a
    change that happened, and `failed` for a fund write the bank refused -- the money did not
    move, but "this write was attempted and did not go through" is exactly what the trail is
    asked to answer. `rejected` keeps the field closed and leaves room for a later period
    that decides to record declined attempts.
    """

    __tablename__ = "business_audit_event"
    __table_args__ = (
        CheckConstraint(
            "result IN ('ok','rejected','failed')", name="ck_business_audit_event_result"
        ),
        Index("ix_business_audit_event_user_created", "user_id", "created_at"),
        Index("ix_business_audit_event_conversation", "conversation_id", "created_at"),
        Index("ix_business_audit_event_action", "action"),
    )
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- daily operation total (migrations 0016, 0017) -------------------------------------
#
# One row per (user, business date) holding the amount accumulated that day, in minor units.
# Migration 0016 created the table keyed by operation kind; 0017 merged those rows and
# dropped the column, because the read side summed across kinds anyway. Those two migrations
# are the authority for this table: the column names, types, constraint name and index name
# below are copied from them column for column, so `alembic revision --autogenerate` has
# nothing to say about it.
#
# The read/write path is `app.storage.daily_total.DailyTotalStore`; nothing else may touch
# the table, because the risk decision reads whatever these rows say.
#
# The primary key is `bigserial`, not the `Uuid` of `Record`, so this model inherits
# `Base` alone -- same reason as `BillTaskAudit`.


class DailyOperationTotal(Base):
    """The day's accumulated amount for one user.

    `total_minor` is minor units (分), like every amount in this codebase, and it only ever
    grows in place: a second write for the same (user, day) accumulates onto the same row
    rather than inserting a second one. `updated_at` moves with each accumulation.
    """

    __tablename__ = "daily_operation_total"
    __table_args__ = (
        UniqueConstraint("user_id", "business_date", name="uq_daily_op_user_date"),
        Index("ix_daily_op_user_date", "user_id", "business_date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    business_date: Mapped[date] = mapped_column(Date)
    total_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- IM channel bindings (migration 0018) ----------------------------------------------
#
# One row is one IM identity mapped onto one internal user: the chat platform knows the
# sender by its own id, every tool downstream knows the user by `user_id`, and this table is
# the only place the two are related. A binding carries no credential and no session of its
# own -- `conversation_id` names the conversation the channel last used, and the identity
# service remains the authority on whether a session is still valid.
#
# `(channel, platform_user_id)` is unique together because it is the platform-side identity
# that must not be claimed twice: the same Feishu account cannot be bound to two users, and
# the channel column keeps the key honest for a second channel added later. `user_id` is a
# real foreign key: a binding to a user that does not exist binds nobody. `conversation_id`
# is one too, and stays nullable because a binding exists before its conversation does.


class ChannelBinding(Record, Base):
    """Which internal user an IM identity speaks for."""

    __tablename__ = "channel_binding"
    __table_args__ = (
        UniqueConstraint("channel", "platform_user_id", name="uq_channel_platform_user"),
        Index("ix_channel_binding_user_id", "user_id"),
    )
    channel: Mapped[str] = mapped_column(String(32), default="feishu")
    # The platform's own id: a Feishu open_id, not a phone number and not a nickname.
    platform_user_id: Mapped[str] = mapped_column(String(128))
    platform_username: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.user_id"))
    # A foreign key, like the conversation pointers on ConversationMessage and
    # ConversationSubmission: a binding must not keep naming a conversation nobody has, or
    # the channel sends messages into a dead id and finds out only from the 404s. Nullable,
    # because a binding exists before its conversation does.
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversation.conversation_id"))
    # Deliberately the odd one out: both timestamps carry an ORM-side default (and
    # `updated_at` an onupdate), instead of the bare columns every other table in this
    # file declares, which the service layer fills by hand. The reason is the channel
    # layer writes this row from a callback thread that has no reason to know the storage
    # layer's conventions, and `updated_at` has to move by itself when a binding is
    # re-pointed at another conversation. There is no server default: the ORM supplies
    # the value, so an INSERT that bypasses the ORM still has to provide one.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
