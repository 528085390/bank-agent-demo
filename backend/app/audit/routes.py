"""The controlled audit query.

The audit trail holds the complete model request, the complete model response and the
complete tool result -- material that the ordinary conversation API deliberately never
returns. This router is the one way to read it, and it is built so that reading it is a
deliberate act rather than a side effect of loading a chat.

Three rules shape every route here:

- **The caller's own identity is the filter.** Every query is scoped to the authenticated
  `user_id`, so another user's audit is indistinguishable from no audit at all. Naming a
  conversation id that belongs to somebody else narrows the result to nothing; it never
  widens it.
- **Ownership is checked against the conversation, not assumed.** A conversation-scoped read
  resolves the conversation under the caller's own id first, which is what turns "I passed
  someone else's id" into the same `NOT_FOUND` any other conversation route gives.
- **Nothing here is a chat surface.** The response shape names model calls and run events as
  what they are, and no credential is ever in it: the API key is not stored, so it cannot be
  returned.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.api.routes import envelope, identity, runtime, service
from app.core.errors import AppError

router = APIRouter(prefix="/api/audit")

# The two things a caller may ask for. Spelled out so a typo is a bad request rather than an
# empty page that reads as "nothing was ever audited".
MAX_LIMIT = 200


def _limit(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1 or value > MAX_LIMIT:
        raise AppError("INVALID_PARAMETER", f"limit 必须在 1 到 {MAX_LIMIT} 之间", 422)
    return value


def _audit(request: Request):
    reader = getattr(runtime(request), "audit", None)
    if reader is None:
        raise AppError("AUDIT_UNAVAILABLE", "审计服务不可用", 503)
    return reader.reader


async def _own_conversation(request: Request, who: dict, conversation_id: str) -> None:
    """Prove the caller owns the conversation before narrowing a query to it.

    Without this a caller could pass someone else's conversation id and learn, from the
    difference between an empty page and a 404, that the id exists.
    """

    await run_in_threadpool(service(request).get, who["user_id"], conversation_id)


@router.get("/llm-calls")
async def list_llm_calls(
    request: Request,
    conversation_id: str | None = None,
    run_id: str | None = None,
    call_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    cursor: str | None = None,
):
    """Model calls, oldest first, each with the request that was sent and the answer.

    A `call_id` narrows to exactly one attempt, which is how a retry is told from the call it
    retried: the two share nothing but the run they happened in.
    """

    who = await identity(request)
    if conversation_id is not None:
        await _own_conversation(request, who, conversation_id)
    page = await run_in_threadpool(
        _audit(request).llm_calls,
        who["user_id"],
        conversation_id=conversation_id,
        run_id=run_id,
        call_id=call_id,
        since=since,
        until=until,
        limit=_limit(limit),
        cursor=cursor,
    )
    return envelope(request, page)


@router.get("/business-events")
async def list_business_events(
    request: Request,
    conversation_id: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    cursor: str | None = None,
):
    """Business and configuration changes, oldest first, each naming who made it.

    `action` narrows to one kind of change, which is what turns "what happened to this
    conversation" into a list rather than a guess at the strings.
    """

    who = await identity(request)
    if conversation_id is not None:
        await _own_conversation(request, who, conversation_id)
    page = await run_in_threadpool(
        _audit(request).business_events,
        who["user_id"],
        conversation_id=conversation_id,
        action=action,
        since=since,
        until=until,
        limit=_limit(limit),
        cursor=cursor,
    )
    return envelope(request, page)


@router.get("/run-events")
async def list_run_events(
    request: Request,
    conversation_id: str | None = None,
    run_id: str | None = None,
    call_id: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    cursor: str | None = None,
):
    """Run events, oldest first, each with its complete payload.

    A tool event's payload is the adapter's whole `ToolResult`, `facts` included. That is the
    material the SSE frame projects away, and the reason this endpoint exists at all.
    """

    who = await identity(request)
    if conversation_id is not None:
        await _own_conversation(request, who, conversation_id)
    page = await run_in_threadpool(
        _audit(request).run_events,
        who["user_id"],
        conversation_id=conversation_id,
        run_id=run_id,
        call_id=call_id,
        event_type=event_type,
        since=since,
        until=until,
        limit=_limit(limit),
        cursor=cursor,
    )
    return envelope(request, page)
