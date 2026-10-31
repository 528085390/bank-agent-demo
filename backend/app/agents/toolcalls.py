"""The one implementation that turns a streamed tool-calling exchange into a decision.

The Supervisor and the card ask the same question -- "what did the model decide, given these
tools?" -- and the answer must be assembled the same way in both, or the two paths drift and
only one of them is correct (spec §5). Everything protocol-shaped lives here: the single retry
a failure to *start* earns, the split between parallel calls, and the verdict a provider that
refuses tools outright produces.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from app.agents.decisions import DecisionError
from app.agents.upstream import (
    UpstreamUnavailable,
    tools_unsupported_error,
    upstream_app_error,
)
from app.integration.model_client import (
    FeatureUnsupported,
    ModelClient,
    ModelClientError,
    StreamChunk,
    ToolCall,
)


class StreamTruncated(DecisionError):
    """The provider closed the stream without a finish signal.

    It gets its own type because the two halves of the turn treat it differently: before any
    text arrived it is an upstream failure and the turn is retried or failed, while after the
    first delta the text is already on the user's screen and cannot be retracted, so the round
    ends with what they have (spec §4.6).
    """


def start_stream(
    model: ModelClient, messages: tuple[dict[str, Any], ...], tools: tuple[dict[str, Any], ...]
) -> tuple[StreamChunk | None, Iterator[StreamChunk]]:
    """The first chunk and the rest of the stream, retrying once if it fails to *start*.

    Retrying after the first chunk would restart an answer the user has already begun to see,
    so only a failure to start is retried (spec §4.6). A capability verdict is not retried at
    all: it is the caller's to act on.
    """

    for attempt in (1, 2):
        try:
            stream = model.stream(messages, tools=tools)
            return next(stream), stream
        except StopIteration:
            return None, iter(())
        except FeatureUnsupported:
            raise
        except ModelClientError as error:
            if attempt == 2 or not error.retryable:
                if error.retryable:
                    raise UpstreamUnavailable(error.code) from error
                raise upstream_app_error(error) from error
    raise AssertionError("unreachable")


def absorb(
    chunk: StreamChunk,
    text_parts: list[str],
    names: dict[int, str],
    arguments: dict[int, list[str]],
    emit: Callable[[str], None] | None = None,
) -> None:
    """Fold one chunk into the running decision, handing text out as it arrives.

    The model's reasoning never reaches here: the client drops it before it leaves the
    integration layer (spec §4.1).
    """

    index = chunk.tool_index if chunk.tool_index is not None else 0
    if chunk.text_delta:
        text_parts.append(chunk.text_delta)
        if emit is not None:
            emit(chunk.text_delta)
    if chunk.tool_name:
        names.setdefault(index, chunk.tool_name)
    if chunk.tool_call_delta:
        arguments.setdefault(index, []).append(chunk.tool_call_delta)


def arguments_or_error(parts: list[str]) -> dict[str, Any]:
    """Tool arguments, assembled from deltas. Half a JSON object never leaves this module."""

    raw = "".join(parts).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise DecisionError(f"handoff 的参数不是合法 JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise DecisionError("handoff 的参数不是 JSON 对象")
    return parsed


def collect_stream(
    model: ModelClient,
    messages: tuple[dict[str, Any], ...],
    tools: tuple[dict[str, Any], ...],
    *,
    emit: Callable[[str], None] | None = None,
) -> tuple[str, list[ToolCall]]:
    """One tool-calling exchange. Raises `StreamTruncated` when the stream ends early."""

    try:
        first, rest = start_stream(model, messages, tools)
        text_parts: list[str] = []
        names: dict[int, str] = {}
        arguments: dict[int, list[str]] = {}
        if first is not None:
            if first.truncated:
                raise StreamTruncated()
            absorb(first, text_parts, names, arguments, emit=emit)
        for chunk in rest:
            if chunk.truncated:
                raise StreamTruncated()
            absorb(chunk, text_parts, names, arguments, emit=emit)
    except FeatureUnsupported as error:
        # The provider refused the request *because* it carries tools. There is no second
        # protocol to fall back to and no retry that could help, so this ends the turn with a
        # message the user can act on (spec §4.5).
        raise tools_unsupported_error(error) from error
    indexes = sorted(set(names) | set(arguments))
    calls = [
        ToolCall(name=names.get(index, ""), arguments=arguments_or_error(arguments.get(index, [])))
        for index in indexes
    ]
    return "".join(text_parts), calls
