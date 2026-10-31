"""Appending audit records, and reading them back under a caller's own identity.

Two classes, split by what they may do:

- `AuditService` only appends. It has no update and no delete, and that is the design rather
  than an omission: an audit record that can be edited is not evidence. A correction is
  another row.
- `AuditReader` only reads, always filtered by the caller's own `user_id`, so "another
  user's audit" is indistinguishable from "no audit" without a second check.

The payloads are stored as JSONB and go through `jsonable`, which is strict on purpose: a
value it does not understand raises instead of being turned into a `repr`. A prompt that
silently became `"<Foo object at 0x...>"` would look like a complete record while being
useless, which is worse than a loud failure.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from app.audit.context import AuditContext, current_context
from app.core.errors import AppError
from app.core.types import new_id, utcnow
from app.storage.models import AgentRunEvent, BusinessAuditEvent, LlmCallAudit

logger = logging.getLogger(__name__)

# The two outcomes one model call can have. A retry is not a third: it is another call.
RESULT_SUCCEEDED = "SUCCEEDED"
RESULT_FAILED = "FAILED"

# The outcomes one *business* change can have, which is a different vocabulary from a model
# call's. The set is closed and the database enforces it (`ck_business_audit_event_result`).
# Two of the three are written: `ok` for a change that happened, and `failed` for a fund write
# the bank refused (see `app/audit/fund_writes.py`) -- the money did not move, but "this write
# was attempted and did not go through" is what the trail is asked to answer. `rejected` stays
# unused: no code path declines a business change yet.
BUSINESS_RESULT_OK = "ok"
BUSINESS_RESULT_REJECTED = "rejected"
BUSINESS_RESULT_FAILED = "failed"
BUSINESS_RESULTS: frozenset[str] = frozenset(
    {BUSINESS_RESULT_OK, BUSINESS_RESULT_REJECTED, BUSINESS_RESULT_FAILED}
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

CHAT_COMPLETIONS_FORMAT = "chat_completions"


class RunEventType(str, Enum):
    """The events a run appends. These are evidence, never a state machine.

    The list is closed on purpose: a reader has to be able to branch on the event type, so a
    new one is a change to the audit contract rather than a string somebody typed.
    """

    RUN_STARTED = "RUN_STARTED"
    AGENT_STARTED = "AGENT_STARTED"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    INTERRUPTED = "INTERRUPTED"
    RESUMED = "RESUMED"
    RETRY = "RETRY"
    CONTEXT_ASSEMBLED = "CONTEXT_ASSEMBLED"
    GROUNDING_BLOCKED = "GROUNDING_BLOCKED"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_ABORTED = "RUN_ABORTED"
    RUN_FAILED = "RUN_FAILED"


def jsonable(value: Any) -> Any:
    """A JSON-compatible copy of `value`, or a `TypeError` naming the type it cannot store.

    Supported: the JSON scalars, `Enum`, `Decimal`, `UUID`, dates and times, dataclasses,
    pydantic models, mappings and sequences. Everything else fails loudly -- see the module
    docstring for why a `repr` fallback would be the worst available answer.
    """

    if value is None or isinstance(value, bool):
        return value
    # Before `str` and `int`: a `StrEnum` member is a `str`, and storing the member itself
    # would leave a Python object in a payload that claims to be JSON.
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        # Never a float: `0.1 + 0.2` is not a rounding error a bank may accept, and the
        # string keeps every digit the caller had.
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        # Stored as UTC: a naive timestamp is ambiguous, and the audit trail is read later.
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return jsonable(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    raise TypeError(f"cannot store {type(value).__name__} in an audit record")


def _resolve(context: AuditContext | None) -> AuditContext:
    """The context a record is written under: the explicit one, else the current turn's.

    The fallback is what lets the Gateway write a tool event without being handed anything:
    it runs inside a model call's turn, so the turn's conversation and run are simply there.
    """

    if context is not None:
        return context
    return current_context() or AuditContext()


class AuditService:
    """The append-only writer. Construct one per process and share it."""

    def __init__(self, database) -> None:
        self.database = database

    def record_llm_call(
        self,
        *,
        call_id: str,
        request: Mapping[str, Any],
        result: str,
        context: AuditContext | None = None,
        response: Mapping[str, Any] | None = None,
        response_text: str | None = None,
        error_code: str | None = None,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        agent_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        stream: bool = False,
        request_format: str = CHAT_COMPLETIONS_FORMAT,
    ) -> str:
        """Append one attempt at one model call, and return its audit id.

        A success carries the whole answer; a failure carries the whole request and the
        classified reason, because the request is the only thing that says what was being
        attempted. Either way the row is complete enough to answer "what did we actually
        send, and what came back". `stream` says how the answer arrived, which the other
        columns cannot say.
        """

        if result not in (RESULT_SUCCEEDED, RESULT_FAILED):
            raise ValueError(f"unknown audit result: {result!r}")
        where = _resolve(context)
        audit_id = new_id("aud")
        row = LlmCallAudit(
            audit_id=audit_id,
            call_id=call_id,
            conversation_id=where.conversation_id,
            run_id=where.run_id,
            user_id=where.user_id,
            source=where.source,
            agent_id=agent_id,
            provider=provider,
            model=model,
            stream=stream,
            request_format=request_format,
            prompt_json=jsonable(dict(request)),
            response_json=None if response is None else jsonable(dict(response)),
            response_text=response_text,
            result=result,
            error_code=error_code,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=utcnow(),
        )
        with self.database.sessions.begin() as db:
            db.add(row)
        return audit_id

    def record_agent_event(
        self,
        *,
        event_type: RunEventType | str,
        payload: Mapping[str, Any] | None = None,
        context: AuditContext | None = None,
        status: str | None = None,
        agent_id: str | None = None,
        tool_name: str | None = None,
        call_id: str | None = None,
        error_code: str | None = None,
    ) -> str:
        """Append one run fact, and return its event id.

        The payload is stored whole. It is the only place a tool's complete result -- the
        module-private `facts` included -- is kept, so nothing here projects or trims.
        """

        name = event_type.value if isinstance(event_type, RunEventType) else str(event_type)
        where = _resolve(context)
        event_id = new_id("evt")
        row = AgentRunEvent(
            event_id=event_id,
            conversation_id=where.conversation_id,
            run_id=where.run_id,
            call_id=call_id,
            event_type=name,
            agent_id=agent_id,
            tool_name=tool_name,
            user_id=where.user_id,
            source=where.source,
            status=status,
            payload_json=jsonable(dict(payload or {})),
            error_code=error_code,
            created_at=utcnow(),
        )
        with self.database.sessions.begin() as db:
            db.add(row)
        return event_id

    def record_business_event(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        result: str = BUSINESS_RESULT_OK,
        reason: str | None = None,
        context: AuditContext | None = None,
        db: Any = None,
    ) -> str:
        """Append one business or configuration change, and return its event id.

        The row records *who* changed *what*, so it insists on an author: a business event
        with no `user_id` would be invisible to the reader, whose every query starts from the
        caller's own id, and an invisible audit row is worse than a loud refusal.

        `db` lets a caller that is already in a transaction write the event into it, so the
        change and its record commit together or not at all. Without one, the write gets its
        own transaction and is committed by the time this returns.
        """

        if result not in BUSINESS_RESULTS:
            raise ValueError(f"unknown business audit result: {result!r}")
        where = _resolve(context)
        if where.user_id is None:
            raise ValueError("business audit event requires a user_id")
        event_id = new_id("baud")
        row = BusinessAuditEvent(
            event_id=event_id,
            user_id=where.user_id,
            conversation_id=where.conversation_id,
            run_id=where.run_id,
            request_id=where.request_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            reason=reason,
            created_at=utcnow(),
        )
        try:
            if db is not None:
                db.add(row)
                db.flush()
            else:
                with self.database.sessions.begin() as session:
                    session.add(row)
                    session.flush()
        except Exception:
            # Deliberately no exception text: it can carry the row's values, and this line
            # has to be safe to log wherever the trail itself may not be.
            logger.error(
                "business_audit_write_failed event_id=%s action=%s resource_id=%s",
                event_id,
                action,
                resource_id,
            )
            raise
        return event_id

    @property
    def reader(self) -> AuditReader:
        """The read side, bound to the same database."""

        return AuditReader(self.database)


@dataclass(frozen=True, slots=True)
class LlmCallView:
    """One stored model call, as the audit query publishes it."""

    audit_id: str
    call_id: str
    conversation_id: str | None
    run_id: str | None
    user_id: str | None
    source: str | None
    agent_id: str | None
    provider: str | None
    model: str | None
    request_format: str
    prompt: dict[str, Any]
    response: dict[str, Any] | None
    response_text: str | None
    result: str
    error_code: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "call_id": self.call_id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "source": self.source,
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "request_format": self.request_format,
            "request": self.prompt,
            "response": self.response,
            "response_text": self.response_text,
            "result": self.result,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RunEventView:
    """One stored run event, as the audit query publishes it."""

    event_id: str
    conversation_id: str | None
    run_id: str | None
    call_id: str | None
    event_type: str
    agent_id: str | None
    tool_name: str | None
    status: str | None
    payload: dict[str, Any]
    error_code: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "event_type": self.event_type,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "payload": self.payload,
            "error_code": self.error_code,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BusinessEventView:
    """One stored business event, as the audit query publishes it.

    The fields are the table's audit columns. The inherited `id` is not among them: it is the
    row's internal primary key, not something the audit trail claims.
    """

    event_id: str
    user_id: str | None
    conversation_id: str | None
    run_id: str | None
    request_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    reason: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "result": self.result,
            "reason": self.reason,
            "created_at": self.created_at,
        }


def _encode_cursor(row) -> str:
    payload = {"t": row.created_at.isoformat(), "i": _row_id(row)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Read a page cursor. A cursor nobody issued is a bad request, not an empty page."""

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        when = datetime.fromisoformat(payload["t"])
        identifier = payload["i"]
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("bad cursor payload")
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise AppError("INVALID_PARAMETER", "分页游标无效", 422) from exc
    return when, identifier


def _row_id(row) -> str:
    return row.audit_id if isinstance(row, LlmCallAudit) else row.event_id


def _bounded(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(limit), MAX_PAGE_SIZE))


class AuditReader:
    """Paged, owner-filtered reads of the audit trail.

    Every query carries the caller's `user_id`. A conversation-scoped read is checked against
    the conversation's owner by the caller (the API does it), and a run-, call- or
    time-scoped read is filtered here, so no query can return another user's record.
    """

    def __init__(self, database) -> None:
        self.database = database

    def llm_calls(
        self,
        user_id: str,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page of model calls, oldest first."""

        query = select(LlmCallAudit).where(LlmCallAudit.user_id == user_id)
        if conversation_id is not None:
            query = query.where(LlmCallAudit.conversation_id == conversation_id)
        if run_id is not None:
            query = query.where(LlmCallAudit.run_id == run_id)
        if call_id is not None:
            query = query.where(LlmCallAudit.call_id == call_id)
        if since is not None:
            query = query.where(LlmCallAudit.created_at >= since)
        if until is not None:
            query = query.where(LlmCallAudit.created_at <= until)
        if cursor:
            when, identifier = _decode_cursor(cursor)
            query = query.where(
                or_(
                    LlmCallAudit.created_at > when,
                    and_(LlmCallAudit.created_at == when, LlmCallAudit.audit_id > identifier),
                )
            )
        size = _bounded(limit)
        query = query.order_by(LlmCallAudit.created_at.asc(), LlmCallAudit.audit_id.asc())
        with self.database.sessions() as db:
            rows = db.scalars(query.limit(size + 1)).all()
        page = rows[:size]
        return {
            "items": [_llm_call_view(row).as_dict() for row in page],
            "next_cursor": _encode_cursor(page[-1]) if len(rows) > size else None,
        }

    def run_events(
        self,
        user_id: str,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page of run events, oldest first."""

        query = select(AgentRunEvent).where(AgentRunEvent.user_id == user_id)
        if conversation_id is not None:
            query = query.where(AgentRunEvent.conversation_id == conversation_id)
        if run_id is not None:
            query = query.where(AgentRunEvent.run_id == run_id)
        if call_id is not None:
            query = query.where(AgentRunEvent.call_id == call_id)
        if event_type is not None:
            query = query.where(AgentRunEvent.event_type == event_type)
        if since is not None:
            query = query.where(AgentRunEvent.created_at >= since)
        if until is not None:
            query = query.where(AgentRunEvent.created_at <= until)
        if cursor:
            when, identifier = _decode_cursor(cursor)
            query = query.where(
                or_(
                    AgentRunEvent.created_at > when,
                    and_(AgentRunEvent.created_at == when, AgentRunEvent.event_id > identifier),
                )
            )
        size = _bounded(limit)
        query = query.order_by(AgentRunEvent.created_at.asc(), AgentRunEvent.event_id.asc())
        with self.database.sessions() as db:
            rows = db.scalars(query.limit(size + 1)).all()
        page = rows[:size]
        return {
            "items": [_run_event_view(row).as_dict() for row in page],
            "next_cursor": _encode_cursor(page[-1]) if len(rows) > size else None,
        }

    def business_events(
        self,
        user_id: str,
        *,
        conversation_id: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page of business events, oldest first."""

        query = select(BusinessAuditEvent).where(BusinessAuditEvent.user_id == user_id)
        if conversation_id is not None:
            query = query.where(BusinessAuditEvent.conversation_id == conversation_id)
        if action is not None:
            query = query.where(BusinessAuditEvent.action == action)
        if since is not None:
            query = query.where(BusinessAuditEvent.created_at >= since)
        if until is not None:
            query = query.where(BusinessAuditEvent.created_at <= until)
        if cursor:
            when, identifier = _decode_cursor(cursor)
            query = query.where(
                or_(
                    BusinessAuditEvent.created_at > when,
                    and_(
                        BusinessAuditEvent.created_at == when,
                        BusinessAuditEvent.event_id > identifier,
                    ),
                )
            )
        size = _bounded(limit)
        query = query.order_by(
            BusinessAuditEvent.created_at.asc(), BusinessAuditEvent.event_id.asc()
        )
        with self.database.sessions() as db:
            rows = db.scalars(query.limit(size + 1)).all()
        page = rows[:size]
        return {
            "items": [_business_event_view(row).as_dict() for row in page],
            "next_cursor": _encode_cursor(page[-1]) if len(rows) > size else None,
        }


def _llm_call_view(row: LlmCallAudit) -> LlmCallView:
    return LlmCallView(
        audit_id=row.audit_id,
        call_id=row.call_id,
        conversation_id=row.conversation_id,
        run_id=row.run_id,
        user_id=row.user_id,
        source=row.source,
        agent_id=row.agent_id,
        provider=row.provider,
        model=row.model,
        request_format=row.request_format,
        prompt=dict(row.prompt_json or {}),
        response=None if row.response_json is None else dict(row.response_json),
        response_text=row.response_text,
        result=row.result,
        error_code=row.error_code,
        latency_ms=row.latency_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        created_at=row.created_at,
    )


def _run_event_view(row: AgentRunEvent) -> RunEventView:
    return RunEventView(
        event_id=row.event_id,
        conversation_id=row.conversation_id,
        run_id=row.run_id,
        call_id=row.call_id,
        event_type=row.event_type,
        agent_id=row.agent_id,
        tool_name=row.tool_name,
        status=row.status,
        payload=dict(row.payload_json or {}),
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _business_event_view(row: BusinessAuditEvent) -> BusinessEventView:
    return BusinessEventView(
        event_id=row.event_id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        run_id=row.run_id,
        request_id=row.request_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        result=row.result,
        reason=row.reason,
        created_at=row.created_at,
    )
