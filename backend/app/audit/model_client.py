"""The one place every model call passes through, so every one of them is recorded.

`AuditedModelClient` is a transparent decorator over the adapter the runtime actually uses.
It is transparent in the only sense that matters: `complete()` returns exactly what the
adapter returned, `close()` closes it, and every failure is re-raised unchanged. What it adds
is a row per *attempt* in `llm_call_audit`, written before the answer is handed back.

Three properties are deliberate.

- **One attempt, one record.** A retry inside one invocation calls `complete()` again, and
  that is a second call with its own `call_id` -- never an update of the first. The number of
  attempts is therefore readable from the table rather than inferred from a counter.
- **The request is recorded as sent.** The whole message array goes in verbatim, together
  with the parameters that changed it. Nothing is summarized, because the question this table
  exists to answer is "what exactly did we send".
- **A failure is still a record.** The provider may reject the call, time out, or return
  nothing parseable; the request that was attempted is the only evidence of what was being
  tried, so the failure row carries the same request and a stable error code.

The API key never reaches this module: the decorator is handed the messages and the
request parameters, and the credential lives in the adapter's own client object.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from app.audit.context import AuditContext, current_context
from app.audit.service import (
    RESULT_FAILED,
    RESULT_SUCCEEDED,
    AuditService,
    jsonable,
)
from app.core.errors import AppError
from app.core.types import new_id
from app.integration.model_client import ModelClientError, ModelReply, StreamChunk

logger = logging.getLogger(__name__)

# Who the adapter talks to. The value names the SDK in use, which is a fact about this
# process; the endpoint itself is configuration and is not part of an audit row.
PROVIDER_OPENAI_SDK = "openai"
REQUEST_FORMAT_CHAT_COMPLETIONS = "chat_completions"

# The caller closed the stream before the provider finished. The call happened, so it is
# recorded -- as a failure, because nothing about it says the answer was complete.
CLIENT_CLOSED = "CLIENT_CLOSED"


class AuditedModelClient:
    """Wrap a model client so every call it makes is persisted before it is returned."""

    def __init__(
        self,
        inner,
        audit: AuditService,
        *,
        context_provider: Callable[[], AuditContext | None] = current_context,
        provider: str | None = PROVIDER_OPENAI_SDK,
        model: str | None = None,
        request_format: str = REQUEST_FORMAT_CHAT_COMPLETIONS,
        agent_id: str | None = None,
    ) -> None:
        self.inner = inner
        self.audit = audit
        self._context_provider = context_provider
        self._provider = provider
        self._model = model
        self._request_format = request_format
        self._agent_id = agent_id

    # --- the ModelClient interface ----------------------------------------------------

    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply:
        call_id = new_id("call")
        where = self._where()
        request = self._request(messages)
        started = time.perf_counter()
        try:
            reply = self.inner.complete(messages)
        except BaseException as error:
            self._record_failure(call_id, where, request, started, error)
            raise
        self._record_success(call_id, where, request, started, reply)
        return reply

    def stream(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        tools: tuple[dict[str, Any], ...] = (),
    ) -> Iterator[StreamChunk]:
        """The streaming call, recorded exactly once.

        The row is written when the stream *ends*, not when it starts, because its job is to
        say what came back. A caller that walks away mid-stream still gets a row: the call
        happened, and an audit that only records the streams somebody finished has a hole in
        it exactly where the expensive failures are.

        Tool calls are accumulated alongside the text, keyed by `tool_index`, because the
        provider sends one call as several deltas and only the end of the stream can state it
        whole. A stream that asked for no tool records a null response, exactly as before.
        """

        call_id = new_id("call")
        where = self._where()
        request = self._request(messages, tools=tools)
        started = time.perf_counter()
        parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        try:
            for chunk in self.inner.stream(messages, tools=tools):
                if chunk.text_delta:
                    parts.append(chunk.text_delta)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.tool_name or chunk.tool_call_delta:
                    slot = chunk.tool_index if chunk.tool_index is not None else 0
                    call = calls.setdefault(
                        slot,
                        {
                            "id": chunk.tool_call_id,
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if chunk.tool_name:
                        call["function"]["name"] = chunk.tool_name
                    if chunk.tool_call_delta:
                        call["function"]["arguments"] += chunk.tool_call_delta
                yield chunk
        except BaseException as error:  # GeneratorExit included: a closed stream is a record
            self._record_failure(call_id, where, request, started, error, stream=True)
            raise
        raw: dict[str, Any] | None = None
        if calls:
            raw = {
                "choices": [
                    {
                        "finish_reason": finish_reason or "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "".join(parts) or None,
                            "tool_calls": [calls[key] for key in sorted(calls)],
                        },
                    }
                ]
            }
        self._record_success(
            call_id,
            where,
            request,
            started,
            ModelReply(text="".join(parts), raw=raw),
            stream=True,
        )

    def close(self) -> None:
        self.inner.close()

    # --- what the record says ---------------------------------------------------------

    def _where(self) -> AuditContext:
        """The turn this call belongs to, or an empty context when it belongs to none.

        A configuration probe runs with no conversation and no run, and must record that
        rather than inherit whatever the last turn left in the ContextVar.
        """

        return self._context_provider() or AuditContext()

    def _request(
        self, messages: tuple[dict[str, Any], ...], *, tools: tuple[dict[str, Any], ...] = ()
    ) -> dict[str, Any]:
        """The request as sent: the whole message array, and what else changed the answer.

        `request_parameters()` is asked of the adapter when it offers one, so the record says
        what was really on the wire rather than what this decorator assumed. An adapter that
        offers nothing records the messages alone. The tools the call carried, if any, are
        recorded alongside the parameters.
        """

        parameters = getattr(self.inner, "request_parameters", None)
        described = parameters() if callable(parameters) else {}
        if not described and self._model is not None:
            described = {"model": self._model}
        if tools:
            described["tools"] = jsonable(list(tools))
        return {"messages": jsonable(list(messages)), "parameters": jsonable(described)}

    def _record_success(
        self,
        call_id: str,
        where: AuditContext,
        request: dict[str, Any],
        started: float,
        reply: ModelReply,
        *,
        stream: bool = False,
    ) -> None:
        raw = reply.raw if isinstance(reply.raw, dict) else None
        input_tokens, output_tokens = _usage(raw)
        self.audit.record_llm_call(
            call_id=call_id,
            request=request,
            result=RESULT_SUCCEEDED,
            context=where,
            response=raw,
            response_text=reply.text,
            latency_ms=_elapsed_ms(started),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            agent_id=where.agent_id or self._agent_id,
            provider=self._provider,
            model=self._model,
            stream=stream,
            request_format=self._request_format,
        )

    def _record_failure(
        self,
        call_id: str,
        where: AuditContext,
        request: dict[str, Any],
        started: float,
        error: BaseException,
        *,
        stream: bool = False,
    ) -> None:
        """Store the attempt, then let the original failure continue unchanged.

        An audit write that itself fails is not swallowed: it is raised with the provider
        failure chained onto it, so neither fact is lost. Recording that a call happened is
        not optional, and a silent fallback to "only the log knows" is the one outcome the
        audit spec rules out.
        """

        try:
            self.audit.record_llm_call(
                call_id=call_id,
                request=request,
                result=RESULT_FAILED,
                context=where,
                error_code=error_code_for(error),
                latency_ms=_elapsed_ms(started),
                agent_id=where.agent_id or self._agent_id,
                provider=self._provider,
                model=self._model,
                stream=stream,
                request_format=self._request_format,
            )
        except Exception as audit_error:
            logger.error(
                "audit_write_failed call_id=%s type=%s", call_id, type(audit_error).__name__
            )
            raise audit_error from error


def error_code_for(error: BaseException) -> str:
    """A stable, machine-readable reason for a failed call.

    A boundary failure carries its own code, and that code is the contract callers branch on
    and operators read, so it is used verbatim. Anything else has no stable code to offer and
    is named by its type -- never by its message, which can carry request contents.
    """

    if isinstance(error, GeneratorExit):
        return CLIENT_CLOSED
    if isinstance(error, (ModelClientError, AppError)):
        return error.code
    return type(error).__name__


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _usage(raw: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """The token counts the provider reported, or two `None`s when it reported none.

    A missing count is stored as missing rather than as zero: "the provider said nothing" and
    "the provider said none were used" are different facts, and only one of them is a
    measurement. Both spellings the Chat Completions shape has used are accepted.
    """

    usage = (raw or {}).get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _count(usage, "prompt_tokens", "input_tokens"), _count(
        usage, "completion_tokens", "output_tokens"
    )


def _count(usage: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None
