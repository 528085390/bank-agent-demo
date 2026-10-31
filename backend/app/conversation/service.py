"""Conversations, the message ledger and idempotent submissions.

Three facts live here and nothing else: a conversation (a user-visible container that owns
one LangGraph thread), an append-only ledger of what the user and the assistant saw, and one
receipt per client write. There is deliberately no run table, no state machine and no
projection of the graph: execution state is the checkpoint's, and this module never guesses
at it.

Two rules shape every method below.

- **Ownership is a filter, not a check afterwards.** A conversation the caller does not own
  is indistinguishable from one that does not exist, which is why every query carries
  `user_id` and a miss raises the same `NOT_FOUND`.
- **A message is never edited and a sequence is never reused.** `sequence` is allocated by
  locking the conversation row, and the whole candidate write -- message, sequence and
  submission -- commits or rolls back as one. A unique-key collision therefore leaves no
  trace of the loser, which is what makes a retried request safe to answer with the winner's
  receipt.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    AppError,
    ConversationErrorCode,
    conversation_error,
    conversation_not_found,
)
from app.core.types import fingerprint, new_id, utcnow
from app.storage.models import (
    AppUser,
    Conversation,
    ConversationMessage,
    ConversationSubmission,
)

# The only operation this phase accepts. A second one would need its own hash inputs and
# its own receipt shape, so it is named rather than implied.
MESSAGE_OPERATION = "conversation_message"

DEFAULT_TITLE = "新会话"
MAX_TITLE_LENGTH = 120
MAX_IDEMPOTENCY_KEY_LENGTH = 255

# The five changes this service makes to a conversation, named for the audit trail. An action
# records that something happened, never what it said: a rename leaves the fact, not the title.
ACTION_RENAME = "conversation.rename"
ACTION_PIN = "conversation.pin"
ACTION_ARCHIVE = "conversation.archive"
ACTION_SOFT_DELETE = "conversation.soft_delete"
ACTION_RESTORE = "conversation.restore"

# New conversations get a thread of their own. The legacy prefix belongs to the migration
# bridge: a conversation created for an old `session_id` keeps that session's checkpoint.
THREAD_PREFIX = "conv:"
LEGACY_THREAD_PREFIX = "chat:"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 200


def thread_id_for_conversation(conversation_id: str) -> str:
    """The thread a new conversation runs on. Derived once, at creation, then stored."""

    return f"{THREAD_PREFIX}{conversation_id}"


def legacy_thread_id_for_session(session_id: str) -> str:
    """The thread the old runtime derived, kept so an old checkpoint stays reachable."""

    return f"{LEGACY_THREAD_PREFIX}{session_id}"


def canonical_client_context(client_context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The request-identifying part of the client context, in a stable shape.

    Only what changes the meaning of the request is kept: a context key that does not affect
    the graph must not make two identical requests hash differently, and `question_id` is
    kept only when it was actually sent, because "answer the open question" and "answer
    *this* question" are different requests even when the value is the same.
    """

    context = client_context or {}
    canonical: dict[str, Any] = {}
    mode = context.get("mode")
    if mode is not None:
        canonical["mode"] = mode
    resume = context.get("resume")
    if isinstance(resume, Mapping):
        entry: dict[str, Any] = {}
        if isinstance(resume.get("question_id"), str):
            entry["question_id"] = resume["question_id"]
        entry["value"] = resume.get("value")
        canonical["resume"] = entry
    return canonical


def request_hash(
    conversation_id: str, content: str, client_context: Mapping[str, Any] | None
) -> str:
    """SHA-256 over the normalized request: conversation, body and the canonical context."""

    return fingerprint(
        {
            "conversation_id": conversation_id,
            "message": content,
            "client_context": canonical_client_context(client_context),
        }
    )


def clean_title(title: str | None) -> str:
    """A title the list can render: one line, bounded, never empty."""

    if title is None:
        return DEFAULT_TITLE
    collapsed = " ".join(title.split())
    return collapsed[:MAX_TITLE_LENGTH] or DEFAULT_TITLE


@dataclass(frozen=True, slots=True)
class ConversationView:
    """One conversation as the API publishes it. `thread_id` stays server-side."""

    conversation_id: str
    serial_no: int
    title: str
    pinned: bool
    archived: bool
    deleted: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    message_count: int
    thread_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "serial_no": self.serial_no,
            "title": self.title,
            "pinned": self.pinned,
            "archived": self.archived,
            "deleted": self.deleted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message_at": self.last_message_at,
            "message_count": self.message_count,
        }


@dataclass(frozen=True, slots=True)
class MessageView:
    """One ledger row as the API publishes it."""

    message_id: str
    sequence: int
    role: str
    content: str
    message_kind: str
    question_id: str | None
    run_id: str | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sequence": self.sequence,
            "role": self.role,
            "content": self.content,
            "message_kind": self.message_kind,
            "question_id": self.question_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    """What one spent idempotency key bought.

    The four published keys are the contract; `conversation_id` and `sequence` are for the
    server's own use, because a duplicate must be answerable without a second query and a
    fresh submission needs to know where its message landed.
    """

    submission_id: str
    message_id: str
    run_id: str | None
    conversation_id: str
    sequence: int
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "duplicate": self.duplicate,
        }


def _not_found() -> AppError:
    """The service's 404 is the shared one, so `app/context` cannot word it differently."""

    return conversation_not_found()


def _view(row: Conversation, message_count: int) -> ConversationView:
    return ConversationView(
        conversation_id=row.conversation_id,
        serial_no=row.serial_no,
        title=row.title,
        pinned=row.pinned,
        archived=row.archived_at is not None,
        deleted=row.deleted_at is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_message_at=row.last_message_at,
        message_count=message_count,
        thread_id=row.thread_id,
    )


def next_serial_no(db, user_id: str) -> int:
    """The number the next conversation of this owner takes, decided under a lock.

    A user's first two conversations have no conversation row to lock -- there is nothing yet
    to take a row lock on -- so the *owner* row is the lock: two creates for one user
    serialize on it, and the second sees the first's committed number rather than the same
    maximum. `uq_conversation_serial_no_owner` is the backstop if that ever stops being true.

    A missing user row fails closed with NOT_FOUND instead of minting a number for an owner
    that does not exist -- the foreign key would refuse the insert anyway, and a 404 says so
    without leaking a database error.
    """

    owner = db.scalar(select(AppUser.user_id).where(AppUser.user_id == user_id).with_for_update())
    if owner is None:
        raise AppError("NOT_FOUND", "用户不存在", 404)
    highest = db.scalar(
        select(func.coalesce(func.max(Conversation.serial_no), 0)).where(
            Conversation.user_id == user_id
        )
    )
    return int(highest or 0) + 1


def _message_view(row: ConversationMessage) -> MessageView:
    return MessageView(
        message_id=row.message_id,
        sequence=row.sequence,
        role=row.role,
        content=row.content,
        message_kind=row.message_kind,
        question_id=row.question_id,
        run_id=row.run_id,
        created_at=row.created_at,
        metadata=dict(row.meta or {}),
    )


def _ordering_key():
    """The list's ordering key: when a conversation was last used, or when it was created.

    A conversation nobody has written in yet has no last_message_at, and ordering by that column
    alone -- with NULLS LAST -- buried it below every conversation that had ever been used: past
    the first page, which is the only page the sidebar reads, so a brand new conversation never
    appeared at all. Ordered from its creation, it is the newest row instead.
    """
    return func.coalesce(Conversation.last_message_at, Conversation.created_at)


def _key_of(row: Conversation) -> datetime:
    """The same key, read off one row: this is what a cursor carries."""
    return row.last_message_at or row.created_at


# Bumped when the ordering changed. A cursor is a position inside one particular ordering, so a
# cursor issued under another one is refused rather than silently paging to the wrong rows.
CURSOR_VERSION = 2


def _encode_cursor(row: Conversation) -> str:
    payload = {
        "p": row.pinned,
        "t": _key_of(row).isoformat(),
        "c": row.conversation_id,
        "v": CURSOR_VERSION,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[bool, datetime, str]:
    """Read a list cursor. A cursor nobody issued -- or one issued under another ordering -- is
    a bad request, not an empty page."""

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        pinned = payload["p"]
        raw_time = payload["t"]
        conversation_id = payload["c"]
        version = payload["v"]
        if not isinstance(pinned, bool) or not isinstance(conversation_id, str):
            raise ValueError("bad cursor payload")
        if version != CURSOR_VERSION:
            raise ValueError("cursor issued under another ordering")
        if not isinstance(raw_time, str):
            raise ValueError("bad cursor timestamp")
        when = datetime.fromisoformat(raw_time)
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise AppError("INVALID_PARAMETER", "分页游标无效", 422) from exc
    return pinned, when, conversation_id


def _after_cursor(pinned: bool, when: datetime, conversation_id: str):
    """The rows strictly after one cursor position, in the list's own ordering.

    The ordering is `pinned DESC, COALESCE(last_message_at, created_at) DESC, conversation_id
    DESC`, so "after" is not a comparison of one column: the key above is what a row is ordered
    by, and the id is what breaks the tie within one key. The pinned group is matched with `IS`
    rather than `<`, because a boolean has no ordering to compare against: "the rows after the
    pinned ones" means "the unpinned ones", and only when the cursor is itself pinned is there
    anything below it.
    """

    key = _ordering_key()
    same_group = Conversation.pinned.is_(pinned)
    tail = and_(
        same_group,
        or_(
            key < when,
            and_(key == when, Conversation.conversation_id < conversation_id),
        ),
    )
    if not pinned:
        return tail
    return or_(Conversation.pinned.is_(False), tail)


class ConversationService:
    """Every read and write of conversation, message and submission state."""

    def __init__(self, database, audit=None) -> None:
        self.database = database
        # The audit writer is optional so a caller that has no request behind it -- a test, a
        # script -- can still use the service; without one, a change is simply not recorded.
        self.audit = audit

    # --- lifecycle -------------------------------------------------------------------

    def create(self, user_id: str, title: str | None = None) -> ConversationView:
        now = utcnow()
        conversation_id = new_id("conv")
        with self.database.sessions.begin() as db:
            row = Conversation(
                conversation_id=conversation_id,
                serial_no=next_serial_no(db, user_id),
                user_id=user_id,
                title=clean_title(title),
                pinned=False,
                archived_at=None,
                deleted_at=None,
                created_at=now,
                updated_at=now,
                last_message_at=None,
                next_message_sequence=1,
                thread_id=thread_id_for_conversation(conversation_id),
            )
            db.add(row)
        return _view(row, 0)

    def conversation_for_session(
        self, user_id: str, session_id: str, title: str | None = None
    ) -> ConversationView:
        """The conversation a legacy session chats in, created on first use.

        The thread id is the one the old runtime derived, so a session whose checkpoint
        already exists keeps its history instead of starting over on a new thread.
        """

        thread_id = legacy_thread_id_for_session(session_id)
        existing = self._by_thread(thread_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise _not_found()
            return self._view_of(existing)

        now = utcnow()
        conversation_id = new_id("conv")
        try:
            with self.database.sessions.begin() as db:
                row = Conversation(
                    conversation_id=conversation_id,
                    serial_no=next_serial_no(db, user_id),
                    user_id=user_id,
                    title=clean_title(title),
                    pinned=False,
                    archived_at=None,
                    deleted_at=None,
                    created_at=now,
                    updated_at=now,
                    last_message_at=None,
                    next_message_sequence=1,
                    thread_id=thread_id,
                )
                db.add(row)
        except IntegrityError:
            # Two first requests for one session raced. The winner's row is the answer, and
            # nothing of this attempt survived the rollback.
            winner = self._by_thread(thread_id)
            if winner is None or winner.user_id != user_id:
                raise
            return self._view_of(winner)
        return _view(row, 0)

    def list(
        self,
        user_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        archived: bool = False,
        deleted: bool = False,
    ) -> dict[str, Any]:
        size = _bounded(limit, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        count = (
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == Conversation.conversation_id)
            .scalar_subquery()
        )
        query = select(Conversation, count).where(Conversation.user_id == user_id)
        if not deleted:
            query = query.where(Conversation.deleted_at.is_(None))
        if not archived:
            query = query.where(Conversation.archived_at.is_(None))
        if cursor:
            query = query.where(_after_cursor(*_decode_cursor(cursor)))
        query = query.order_by(
            Conversation.pinned.desc(),
            _ordering_key().desc(),
            Conversation.conversation_id.desc(),
        ).limit(size + 1)
        with self.database.sessions() as db:
            rows = db.execute(query).all()
        page = rows[:size]
        return {
            "items": [_view(row, int(total or 0)).as_dict() for row, total in page],
            "next_cursor": _encode_cursor(page[-1][0]) if len(rows) > size else None,
        }

    def get(self, user_id: str, conversation_id: str) -> ConversationView:
        # 归属校验和消息计数本来就读同一行，共用一个 session：公网 RDS 上每次池 checkout
        # 都是固定开销，拆成两个 session 等于白付一次。发出的 SQL 与原来逐条一致。
        with self.database.sessions() as db:
            row = self._owned_in(db, conversation_id, user_id)
            if row is None:
                raise _not_found()
            return self._view_in(db, row)

    def update(
        self,
        user_id: str,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> ConversationView:
        now = utcnow()
        with self.database.sessions.begin() as db:
            row = self._locked(db, conversation_id, user_id)
            # What changed is decided before anything is touched: once a field holds its new
            # value, the comparison can no longer tell a change from a no-op. A title that
            # differs only in whitespace cleans to the value already stored, so it is no change.
            cleaned = clean_title(title) if title is not None else None
            renamed = cleaned is not None and cleaned != row.title
            repinned = pinned is not None and pinned != row.pinned
            rarchived = archived is not None and (now if archived else None) != row.archived_at
            if title is not None:
                row.title = cleaned
            if pinned is not None:
                row.pinned = pinned
            if archived is not None:
                row.archived_at = now if archived else None
            row.updated_at = now
            db.flush()
            if renamed:
                self._record_change(db, action=ACTION_RENAME, conversation_id=conversation_id)
            if repinned:
                self._record_change(db, action=ACTION_PIN, conversation_id=conversation_id)
            if rarchived:
                self._record_change(db, action=ACTION_ARCHIVE, conversation_id=conversation_id)
            count = _message_count(db, conversation_id)
        return _view(row, count)

    def soft_delete(self, user_id: str, conversation_id: str) -> ConversationView:
        now = utcnow()
        with self.database.sessions.begin() as db:
            row = self._locked(db, conversation_id, user_id)
            row.deleted_at = now
            row.updated_at = now
            db.flush()
            self._record_change(db, action=ACTION_SOFT_DELETE, conversation_id=conversation_id)
            count = _message_count(db, conversation_id)
        return _view(row, count)

    def restore(self, user_id: str, conversation_id: str) -> ConversationView:
        now = utcnow()
        with self.database.sessions.begin() as db:
            row = self._locked(db, conversation_id, user_id)
            row.deleted_at = None
            row.updated_at = now
            db.flush()
            self._record_change(db, action=ACTION_RESTORE, conversation_id=conversation_id)
            count = _message_count(db, conversation_id)
        return _view(row, count)

    def bulk_soft_delete(
        self,
        user_id: str,
        *,
        conversation_ids: list[str] | None = None,
        delete_all: bool = False,
    ) -> dict[str, Any]:
        """Soft-delete several conversations at once, scoped to this user.

        `delete_all` wipes every live conversation the user owns and ignores `conversation_ids`;
        otherwise only the named ids that actually belong to the user are touched, and ids that
        are missing, already deleted or owned by someone else are silently skipped. Each deletion
        gets its own audit row, so a bulk wipe leaves a trail as detailed as one done by hand.
        """

        if not delete_all and not conversation_ids:
            return {"deleted_count": 0, "conversation_ids": []}
        now = utcnow()
        with self.database.sessions.begin() as db:
            targets = (
                select(Conversation.conversation_id)
                .where(Conversation.user_id == user_id)
                .where(Conversation.deleted_at.is_(None))
            )
            if not delete_all:
                targets = targets.where(Conversation.conversation_id.in_(conversation_ids or []))
            ids = [row[0] for row in db.execute(targets).all()]
            if not ids:
                return {"deleted_count": 0, "conversation_ids": []}
            db.execute(
                update(Conversation)
                .where(Conversation.conversation_id.in_(ids))
                .values(deleted_at=now, updated_at=now)
            )
            for conversation_id in ids:
                self._record_change(db, action=ACTION_SOFT_DELETE, conversation_id=conversation_id)
            return {"deleted_count": len(ids), "conversation_ids": list(ids)}

    def require_writable(self, user_id: str, conversation_id: str) -> ConversationView:
        """The conversation, if this user may still append to it.

        Archived and soft-deleted conversations keep their messages and their checkpoint, so
        they stay readable; what they stop accepting is new submissions. The distinction is
        the whole reason this is not folded into `get`.
        """

        with self.database.sessions() as db:
            row = self._owned_in(db, conversation_id, user_id)
            if row is None:
                raise _not_found()
            if row.archived_at is not None or row.deleted_at is not None:
                raise conversation_error(ConversationErrorCode.CONVERSATION_NOT_WRITABLE)
            return self._view_in(db, row)

    # --- the ledger ------------------------------------------------------------------

    def messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
        after: int | None = None,
    ) -> dict[str, Any]:
        """One page of the ledger, oldest first, cursor by `sequence`."""

        # 归属校验与分页查询共用一个 session：分两次开只会让公网 RDS 多付一次 checkout，
        # 读到的行集完全相同。校验仍排在分页查询之前，SQL 顺序不变。
        size = _bounded(limit, DEFAULT_MESSAGE_PAGE_SIZE, MAX_MESSAGE_PAGE_SIZE)
        query = select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id
        )
        if after is not None:
            query = query.where(ConversationMessage.sequence > after)
        query = query.order_by(ConversationMessage.sequence.asc()).limit(size + 1)
        with self.database.sessions() as db:
            if self._owned_in(db, conversation_id, user_id) is None:
                raise _not_found()
            rows = db.scalars(query).all()
        page = rows[:size]
        return {
            "items": [_message_view(row).as_dict() for row in page],
            "next_cursor": page[-1].sequence if len(rows) > size else None,
        }

    def find_submission(
        self, user_id: str, idempotency_key: str, expected_hash: str
    ) -> Receipt | None:
        """The receipt this key already bought, or None.

        A key spent on a *different* request is a conflict rather than a miss: answering it
        with the stored receipt would hand the client a message it never asked for.
        """

        with self.database.sessions() as db:
            row = db.execute(
                select(ConversationSubmission, ConversationMessage.sequence)
                .join(
                    ConversationMessage,
                    ConversationMessage.message_id == ConversationSubmission.message_id,
                )
                .where(
                    ConversationSubmission.user_id == user_id,
                    ConversationSubmission.operation_type == MESSAGE_OPERATION,
                    ConversationSubmission.idempotency_key == idempotency_key,
                )
            ).first()
        if row is None:
            return None
        submission, sequence = row
        if submission.request_hash != expected_hash:
            raise conversation_error(ConversationErrorCode.IDEMPOTENCY_CONFLICT)
        return Receipt(
            submission_id=submission.submission_id,
            message_id=submission.message_id,
            run_id=submission.run_id,
            conversation_id=submission.conversation_id,
            sequence=int(sequence),
            duplicate=True,
        )

    def append_user_message(
        self,
        user_id: str,
        conversation_id: str,
        *,
        idempotency_key: str,
        expected_hash: str,
        content: str,
        message_kind: str = "message",
        question_id: str | None = None,
    ) -> Receipt:
        """Append one user message, its sequence and its submission in a single transaction.

        The conversation row is locked first, so the sequence this reads is the sequence it
        writes. If the unique key is already spent, the whole candidate transaction rolls
        back -- no message, no sequence consumed -- and the winner's receipt is returned
        instead, which is what makes a retried POST indistinguishable from the first one.
        """

        if message_kind == "interrupt_answer" and not question_id:
            raise ValueError("an interrupt answer must name its question")
        if message_kind != "interrupt_answer" and question_id is not None:
            raise ValueError("only an interrupt answer carries a question id")
        # A blank key would make every retry of this request look like a fresh one, which is
        # the one thing the key exists to prevent. It is a programming error, not a client
        # error: the HTTP layer refuses a missing header long before this.
        if not idempotency_key.strip():
            raise ValueError("an idempotency key must not be blank")
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("the idempotency key is longer than the column")

        now = utcnow()
        try:
            with self.database.sessions.begin() as db:
                conversation = self._locked(db, conversation_id, user_id)
                if conversation.archived_at is not None or conversation.deleted_at is not None:
                    raise conversation_error(ConversationErrorCode.CONVERSATION_NOT_WRITABLE)
                sequence = int(conversation.next_message_sequence)
                message_id = new_id("msg")
                submission_id = new_id("sub")
                db.add(
                    ConversationMessage(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        run_id=None,
                        sequence=sequence,
                        role="user",
                        content=content,
                        message_kind=message_kind,
                        question_id=question_id,
                        created_at=now,
                        meta={},
                    )
                )
                conversation.next_message_sequence = sequence + 1
                conversation.last_message_at = now
                conversation.updated_at = now
                db.add(
                    ConversationSubmission(
                        submission_id=submission_id,
                        user_id=user_id,
                        operation_type=MESSAGE_OPERATION,
                        idempotency_key=idempotency_key,
                        request_hash=expected_hash,
                        conversation_id=conversation_id,
                        message_id=message_id,
                        run_id=None,
                        created_at=now,
                    )
                )
        except IntegrityError:
            winner = self.find_submission(user_id, idempotency_key, expected_hash)
            if winner is None:
                raise
            return winner
        return Receipt(
            submission_id=submission_id,
            message_id=message_id,
            run_id=None,
            conversation_id=conversation_id,
            sequence=sequence,
        )

    def attach_run(self, user_id: str, receipt: Receipt, run_id: str) -> Receipt:
        """Record that a dispatch was prepared, on the submission and on its message.

        One short transaction, and only after admission has already succeeded. This is the
        last write before the graph is called, so a crash between the two leaves a message
        whose `run_id` names an attempt that never ran -- which is exactly what the field
        claims and no more.
        """

        with self.database.sessions.begin() as db:
            submission = db.scalar(
                select(ConversationSubmission)
                .where(
                    ConversationSubmission.submission_id == receipt.submission_id,
                    ConversationSubmission.user_id == user_id,
                )
                .with_for_update()
            )
            if submission is None:
                raise _not_found()
            message = db.scalar(
                select(ConversationMessage)
                .where(ConversationMessage.message_id == submission.message_id)
                .with_for_update()
            )
            submission.run_id = run_id
            if message is not None:
                message.run_id = run_id
        return Receipt(
            submission_id=receipt.submission_id,
            message_id=receipt.message_id,
            run_id=run_id,
            conversation_id=receipt.conversation_id,
            sequence=receipt.sequence,
        )

    def append_assistant_message(
        self,
        user_id: str,
        conversation_id: str,
        *,
        run_id: str | None,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageView:
        """Append the assistant's final reply and touch `last_message_at` in one transaction.

        Deliberately not gated on writability: archiving a conversation does not cancel a
        turn that has already started, and that turn still has to be able to record what it
        answered.
        """

        now = utcnow()
        with self.database.sessions.begin() as db:
            conversation = self._locked(db, conversation_id, user_id)
            sequence = int(conversation.next_message_sequence)
            row = ConversationMessage(
                message_id=new_id("msg"),
                conversation_id=conversation_id,
                run_id=run_id,
                sequence=sequence,
                role="assistant",
                content=content,
                message_kind="message",
                question_id=None,
                created_at=now,
                meta=dict(metadata or {}),
            )
            db.add(row)
            conversation.next_message_sequence = sequence + 1
            conversation.last_message_at = now
            conversation.updated_at = now
        return _message_view(row)

    # --- automatic titles ------------------------------------------------------------

    def auto_title_candidate(self, user_id: str, conversation_id: str) -> str | None:
        """The one user message a conversation may still be named from, or `None`.

        A conversation is named from its user's own first words, once. Anything else -- a
        title the user chose, a second request, a turn that only answered a question -- means
        the moment has passed, and the caller is told so with `None` rather than an error.
        Reading is deliberately not locking: the model call happens between this read and the
        write, and the write is what re-checks both conditions.
        """

        with self.database.sessions() as db:
            row = db.scalar(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
            if row is None or row.title != DEFAULT_TITLE:
                return None
            if _user_message_count(db, conversation_id) != 1:
                return None
            return db.scalar(
                select(ConversationMessage.content).where(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.role == "user",
                    ConversationMessage.message_kind == "message",
                )
            )

    def apply_auto_title(self, user_id: str, conversation_id: str, title: str) -> bool:
        """Write an automatic title, if the conversation is still exactly as it was read.

        Naming reads a candidate and writes it in two steps, and the turn is not the only
        writer: the user may rename the conversation, or send a second message, while the
        model is composing. The write therefore re-checks both conditions inside the
        transaction that holds the row lock -- the title is still the default and exactly one
        user request exists -- and reports `False` instead of overwriting a later decision.
        A title that cleans to nothing, or to the default, is a failed naming, not a write.
        """

        cleaned = clean_title(title)
        if cleaned == DEFAULT_TITLE:
            return False
        now = utcnow()
        with self.database.sessions.begin() as db:
            row = self._locked(db, conversation_id, user_id)
            if row.title != DEFAULT_TITLE or _user_message_count(db, conversation_id) != 1:
                return False
            row.title = cleaned
            row.updated_at = now
            db.flush()
            self._record_change(db, action=ACTION_RENAME, conversation_id=conversation_id)
        return True

    # --- internals -------------------------------------------------------------------

    def _by_thread(self, thread_id: str) -> Conversation | None:
        with self.database.sessions() as db:
            return db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))

    def _owned_in(self, db, conversation_id: str, user_id: str) -> Conversation | None:
        """归属查询，跑在调用方已有的 session 上，避免再多一次池 checkout。"""

        return db.scalar(
            select(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
        )

    def _locked(self, db, conversation_id: str, user_id: str) -> Conversation:
        """The caller's conversation, locked for the rest of the transaction."""

        row = db.scalar(
            select(Conversation)
            .where(
                Conversation.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
            .with_for_update()
        )
        if row is None:
            raise _not_found()
        return row

    def _record_change(
        self, db, *, action: str, conversation_id: str, resource_id: str | None = None
    ) -> None:
        """Write one audit row for a change already made in this transaction.

        The row goes through the caller's session, so it commits or rolls back with the change
        it records: an audit write that fails must not leave the change standing, and a change
        that fails must not leave an audit row claiming it happened. The user, conversation and
        request ids come from the ambient `AuditContext`, which the route establishes.
        """

        if self.audit is None:
            return
        self.audit.record_business_event(
            action=action,
            resource_type="conversation",
            resource_id=resource_id or conversation_id,
            db=db,
        )

    def _view_in(self, db, row: Conversation) -> ConversationView:
        """拼装视图，消息计数复用调用方已有的 session。"""

        return _view(row, _message_count(db, row.conversation_id))

    def _view_of(self, row: Conversation) -> ConversationView:
        """单次读取包装，只留给 conversation_for_session 这条遗留桥接路径。"""

        with self.database.sessions() as db:
            return self._view_in(db, row)


def _message_count(db, conversation_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        )
        or 0
    )


def _user_message_count(db, conversation_id: str) -> int:
    """How many user *requests* a conversation holds, which is not its message count.

    Only a `message` counts: an `interrupt_answer` answers a question the assistant asked, so
    it belongs to the first request's exchange rather than being a second one. This is the
    condition S-2.2 states, and the conditional write has to count exactly the same rows.
    """

    return int(
        db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.role == "user",
                ConversationMessage.message_kind == "message",
            )
        )
        or 0
    )


def _bounded(limit: int, default: int, maximum: int) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))
