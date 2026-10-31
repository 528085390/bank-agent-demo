"""Encoding agent events as Server-Sent Events.

The stream is a display channel: it carries what the current turn is doing and nothing
else. It is not persisted, not replayable and not resumable, so there is no event table,
no cursor and no delivery guarantee behind it.

Every frame names its event. Text arrives incrementally in named `reply_delta` frames and
once, complete, as `final`; there is no unnamed frame carrying text. The only frame without
an `event:` line is the transport failure envelope, which is an error and never a message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app.core.errors import AppError
from app.core.types import json_value

logger = logging.getLogger(__name__)


class StreamEvent(Protocol):
    """What the encoder needs from an event: a name and a payload."""

    event: str
    data: dict[str, Any]


# The whole event vocabulary of the first phase. Anything else is a bug in the graph.
EVENT_NAMES = frozenset(
    {
        "agent_start",
        "tool_call",
        "tool_result",
        "interrupt",
        "subtask_end",
        "reply_delta",
        "narration",
        "final",
        "plan",
        "task_update",
        "clock",
    }
)


def encode_event(event: StreamEvent, request_id: str) -> str:
    if event.event not in EVENT_NAMES:
        raise ValueError(f"unknown SSE event: {event.event!r}")
    envelope = {
        "request_id": request_id,
        "data": json_value(event.data),
        "error": None,
    }
    return (
        f"event: {event.event}\n"
        "data: " + json.dumps(envelope, ensure_ascii=False, allow_nan=False) + "\n\n"
    )


async def event_stream(source, request_id):
    try:
        async for event in source:
            yield encode_event(event, request_id)
    except Exception as exc:
        logger.error("stream_failed request_id=%s type=%s", request_id, type(exc).__name__)
        error = (
            exc.as_dict()
            if isinstance(exc, AppError)
            # An exception nobody classified is an internal fault, not a protocol event.
            # The provider's own text stays in the log: it can carry request contents.
            else {
                "code": "INTERNAL_ERROR",
                "message": "服务暂时异常，请稍后重试",
                "retryable": True,
                "details": {},
            }
        )
        # A transport failure is not a turn result: send it unnamed, without `event:`.
        yield (
            "data: "
            + json.dumps(
                {
                    "request_id": request_id,
                    "data": None,
                    "error": error,
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )
