"""The model boundary the Supervisor and the domain Agents talk to.

This is an independent adapter. It reuses the Chat Completions *configuration* the
settings panel already manages, but it shares nothing with the retired structured plan
protocol: no plan schema, no capability menu, no `RuntimeControl` call chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from app.core.errors import AppError

# Failures that can plausibly succeed if the same call is made again a moment later.
# Everything else is a caller or contract problem, and retrying it repeats the failure.
RETRYABLE_STATUS = frozenset({408, 429})
TIMEOUT_SHAPED = ("timeout", "timedout")
CONNECTION_SHAPED = ("connection", "connecterror", "protocolerror", "networkerror")

FEATURE_TOOLS = "tools"
FEATURE_STRICT = "strict"

# Phrases a provider uses when it does not implement a parameter it was sent. Matched against
# the lowercased exception, like CONTEXT_OVERFLOW_MARKERS and for the same reason: no provider
# promises a stable error code for "I do not do tools".
UNSUPPORTED_MARKERS: dict[str, tuple[str, ...]] = {
    FEATURE_STRICT: ("strict",),
    FEATURE_TOOLS: ("tools", "tool_choice", "tool choice", "function calling", "functions"),
}
# A provider that does not implement a parameter says so with a client-side status; a 500 is
# the provider breaking, which is not a capability verdict.
UNSUPPORTED_STATUS = frozenset({400, 404, 422})

# Phrases providers use when the request did not fit the model's context window. Matched
# against the lowercased exception, because providers disagree on the status they report for
# it -- the same condition arrives as 400 from one and 413 from another -- and none of them
# promises a stable error code to branch on instead. This is best-effort recognition, not a
# contract: a provider whose wording is not here is classified as the bad request it looks
# like, which is the pre-existing behaviour.
CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "too many tokens",
    "reduce the length",
)


class ModelClientError(Exception):
    """The model boundary failed, carrying a stable code and a retryability verdict.

    `code` is the contract: callers branch on it and it is safe to log. The provider's own
    class name is kept in `detail` for an operator, never used for control flow -- provider
    wording is not stable enough to decide whether to spend another request.
    """

    def __init__(
        self, code: str, *, retryable: bool, detail: str = "", status: int | None = None
    ) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.retryable = retryable
        self.detail = detail
        # The provider's HTTP status, kept for the client: a turn that ends mid-stream reports
        # it on its terminal event. None when the failure never reached a response.
        self.status = status


class FeatureUnsupported(ModelClientError):
    """The endpoint rejected a parameter it does not implement, and said so.

    `retryable` is True because the same call without that one parameter may well succeed:
    the caller is expected to drop the feature and ask once more, not to give up. `feature`
    is what to drop.
    """

    def __init__(self, feature: str, detail: str = "") -> None:
        super().__init__("LLM_FEATURE_UNSUPPORTED", retryable=True, detail=detail or feature)
        self.feature = feature


def unsupported_feature(exc: BaseException, *, sent: tuple[str, ...]) -> str | None:
    """Which of the parameters we sent the provider says it cannot do, if any."""

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status not in UNSUPPORTED_STATUS:
        return None
    text = str(exc).lower()
    for feature in sent:
        if any(marker in text for marker in UNSUPPORTED_MARKERS[feature]):
            return feature
    return None


def classify_provider_error(exc: BaseException) -> ModelClientError:
    """Map a provider exception onto a stable code and a retryability verdict.

    The status code decides when the provider gave one, because it is the most specific
    fact available; the exception's own name is the fallback, which is what catches
    transport failures that never reached an HTTP response.

    One status is not enough to decide: a prompt that overflows the context window arrives as
    a 400 or a 413, the same statuses an ordinary malformed request uses, so those two are
    read for the provider's wording before they are called bad requests. That reading is
    best-effort, and it is deliberately one-way -- a request that overflows is never retried
    because the identical request cannot fit next time either, while an overflow nobody
    recognised keeps the old verdict rather than gaining a new one.
    """

    status = getattr(exc, "status_code", None)
    if status is None:
        # Not every provider puts the status on the exception; some wrap it on a response.
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return ModelClientError(
                "LLM_RATE_LIMITED", retryable=True, detail=f"HTTP {status}", status=status
            )
        if status in RETRYABLE_STATUS:
            return ModelClientError(
                "LLM_TIMEOUT", retryable=True, detail=f"HTTP {status}", status=status
            )
        if status >= 500:
            return ModelClientError(
                "LLM_UPSTREAM_ERROR", retryable=True, detail=f"HTTP {status}", status=status
            )
        if status == 401:
            return ModelClientError(
                "LLM_AUTH", retryable=False, detail=f"HTTP {status}", status=status
            )
        if status == 403:
            return ModelClientError(
                "LLM_PERMISSION", retryable=False, detail=f"HTTP {status}", status=status
            )
        if status in (400, 413) and _mentions_context_overflow(exc):
            return ModelClientError(
                "LLM_CONTEXT_OVERFLOW", retryable=False, detail=f"HTTP {status}", status=status
            )
        return ModelClientError(
            "LLM_BAD_REQUEST", retryable=False, detail=f"HTTP {status}", status=status
        )

    name = type(exc).__name__
    lowered = name.lower()
    if any(token in lowered for token in TIMEOUT_SHAPED):
        return ModelClientError("LLM_TIMEOUT", retryable=True, detail=name)
    if any(token in lowered for token in CONNECTION_SHAPED):
        return ModelClientError("LLM_CONNECTION", retryable=True, detail=name)
    return ModelClientError("LLM_UNKNOWN", retryable=False, detail=name)


def _mentions_context_overflow(exc: BaseException) -> bool:
    """Whether the provider's own text says the request did not fit the context window."""

    text = str(exc).lower()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One assistant turn. A decision is JSON text; there is no second channel.

    `text` is the normalized answer the agents parse, and it stays the whole contract for
    them. `raw` is the provider's own response, kept as JSON so the audit boundary can store
    what actually came back instead of a projection of it -- an adapter that returned only
    `text` would make the audit record a summary of itself. It is `None` for a client that
    has no provider response to offer, which is every in-memory test double.
    """

    text: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the model asked for, with its arguments already parsed."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One slice of a streamed answer.

    The three channels are separate fields rather than one string because they have different
    destinations: `text_delta` may be shown to the user, `tool_call_delta` is accumulated and
    never shown, and `reasoning_delta` never leaves this module at all. `tool_name` is a whole
    name rather than a delta because the provider sends it once, in the first tool-call delta.
    `tool_index` says which parallel call a delta belongs to: a provider that asks for two
    handoffs in one turn sends both argument streams interleaved, and without the index they
    would be spliced into one. `tool_call_id` is the upstream's own identifier for that call --
    the index is only positional and does not survive the turn, so anything that has to name a
    call after the fact needs this; it is `None` when the provider does not supply one.
    `finish_reason` is the upstream's stated reason for stopping, carried through verbatim
    (and `None` when the provider states none): `finish` records that a reason arrived, this
    records what it was.
    """

    text_delta: str = ""
    tool_call_delta: str = ""
    tool_name: str = ""
    tool_index: int | None = None
    tool_call_id: str | None = None
    reasoning_delta: str = ""
    finish: bool = False
    finish_reason: str | None = None
    truncated: bool = False


class ModelClient(Protocol):
    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply: ...

    def stream(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        tools: tuple[dict[str, Any], ...] = (),
    ) -> Iterator[StreamChunk]: ...

    def close(self) -> None: ...


def is_configured(client: Any) -> bool:
    """Whether a client -- possibly wrapped in an audit decorator -- has a real model.

    The audit wrapper is transparent for every other purpose, so this is the one question
    that has to look through it: `AgentRuntime.model_configured` decides whether the process
    can answer at all, and a wrapper must not make an unconfigured runtime look configured.

    The unwrapping is by the `inner` attribute rather than by importing the decorator, so
    this module stays free of the audit package it is wrapped by.
    """

    seen = 0
    while hasattr(client, "inner") and seen < 8:
        client = client.inner
        seen += 1
    return not isinstance(client, UnconfiguredModelClient)


class UnconfiguredModelClient:
    """The default: fail loudly instead of degrading into keyword routing."""

    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply:
        del messages
        raise AppError(
            "MODEL_NOT_CONFIGURED",
            "尚未配置模型。请先在设置中填写 Base URL、API Key 和模型名。",
            503,
        )

    def stream(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        tools: tuple[dict[str, Any], ...] = (),
    ) -> Iterator[StreamChunk]:
        del messages, tools
        raise AppError(
            "MODEL_NOT_CONFIGURED",
            "尚未配置模型。请先在设置中填写 Base URL、API Key 和模型名。",
            503,
        )

    def close(self) -> None:
        return None


class ChatCompletionsModelClient:
    """Chat Completions adapter with no runtime privileges."""

    def __init__(self, config, *, client_factory=None) -> None:
        import openai

        factory = client_factory or openai.OpenAI
        self._config = config
        self._client = factory(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply:
        try:
            response = self._client.chat.completions.create(
                model=self._config.model,
                messages=list(messages),
                **self._response_format(),
            )
        except Exception as exc:  # provider failures are boundary failures, not crashes
            raise classify_provider_error(exc) from exc
        return ModelReply(text=response.choices[0].message.content, raw=_response_json(response))

    def stream(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        tools: tuple[dict[str, Any], ...] = (),
    ) -> Iterator[StreamChunk]:
        """The same call as `complete`, one chunk at a time.

        A provider that refuses a parameter is reported as `FeatureUnsupported` rather than as
        a bad request, because the caller can act on that distinction: drop the feature and ask
        again. Everything else keeps the classification it already had.
        """

        sent = tuple(
            feature
            for feature, present in (
                (FEATURE_TOOLS, bool(tools)),
                (FEATURE_STRICT, any("strict" in tool.get("function", {}) for tool in tools)),
            )
            if present
        )
        try:
            response = self._client.chat.completions.create(
                model=self._config.model,
                messages=list(messages),
                stream=True,
                **self._stream_parameters(tools),
            )
        except Exception as exc:  # provider failures are boundary failures, not crashes
            feature = unsupported_feature(exc, sent=sent)
            if feature is not None:
                raise FeatureUnsupported(feature, str(exc)[:200]) from exc
            raise classify_provider_error(exc) from exc
        return self._chunks(response)

    def _chunks(self, response: Any) -> Iterator[StreamChunk]:
        finished = False
        try:
            for chunk in response:
                for current in _stream_chunk(chunk):
                    finished = finished or current.finish
                    yield current
        except Exception as exc:  # a stream that dies mid-flight is still a boundary failure
            raise classify_provider_error(exc) from exc
        if not finished:
            # The provider closed the stream without saying why. Reporting a normal end here
            # would make a truncated answer look complete, and the caller would publish it.
            yield StreamChunk(truncated=True)

    def _stream_parameters(self, tools: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        """Structure comes from the tools when there are any, and from nothing otherwise.

        A tool schema and `response_format` are two ways to constrain the same answer; sending
        both would leave the provider to choose between them, so the tool schema wins and
        `response_format` is dropped. That is a semantic rule, not something the provider
        reports as an error (spec §4.1 design point 4).
        """

        if tools:
            return {"tools": list(tools)}
        return self._response_format()

    def request_parameters(self) -> dict[str, Any]:
        """What this adapter sends alongside the messages, for the audit record.

        Deliberately not the whole configuration: the API key is a connection credential and
        has no business in an audit row, and the base URL names an endpoint rather than a
        request. What is here is what changes the answer -- the model, and whether a JSON
        object was demanded.
        """

        parameters: dict[str, Any] = {"model": self._config.model}
        parameters.update(self._response_format())
        return parameters

    def _response_format(self) -> dict[str, Any]:
        """Ask for a JSON object only when the configured mode is JSON Object.

        The default mode keeps the provider's plain text behaviour: the agents parse the
        first JSON object out of the reply themselves, so no schema has to be published.
        """

        if self._config.response_format == "json_object":
            return {"response_format": {"type": "json_object"}}
        return {}

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def _stream_chunk(chunk: Any) -> Iterator[StreamChunk]:
    """Project one provider chunk onto the boundary's shape, ignoring what it does not know.

    A chunk that announces parallel calls carries one tool-call delta per call, so it projects
    to one StreamChunk per call: the argument streams stay apart, keyed by `tool_index`,
    instead of being spliced into one broken JSON document.
    """

    choices = getattr(chunk, "choices", None) or ()
    if not choices:
        yield StreamChunk()
        return
    choice = choices[0]
    delta = getattr(choice, "delta", None)
    finish = getattr(choice, "finish_reason", None) is not None
    finish_reason = _finish_reason(choice)
    calls = getattr(delta, "tool_calls", None) or ()
    if not calls:
        yield StreamChunk(
            text_delta=_as_text(getattr(delta, "content", None)),
            reasoning_delta=_as_text(getattr(delta, "reasoning_content", None)),
            finish=finish,
            finish_reason=finish_reason,
        )
        return
    for call in calls:
        yield StreamChunk(
            tool_call_delta=_tool_arguments(call),
            tool_name=_tool_name(call),
            tool_index=_tool_index(call),
            tool_call_id=_tool_call_id(call),
            finish=finish,
            finish_reason=finish_reason,
        )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _tool_name(call: Any) -> str:
    name = getattr(getattr(call, "function", None), "name", None)
    return name if isinstance(name, str) else ""


def _tool_index(call: Any) -> int | None:
    index = getattr(call, "index", None)
    return index if isinstance(index, int) else None


def _tool_call_id(call: Any) -> str | None:
    identifier = getattr(call, "id", None)
    return identifier if isinstance(identifier, str) and identifier else None


def _finish_reason(choice: Any) -> str | None:
    reason = getattr(choice, "finish_reason", None)
    return reason if isinstance(reason, str) and reason else None


def _tool_arguments(call: Any) -> str:
    arguments = getattr(getattr(call, "function", None), "arguments", None)
    return arguments if isinstance(arguments, str) else ""


def _response_json(response: Any) -> dict[str, Any] | None:
    """The provider's response as JSON, or `None` when it cannot offer one.

    The openai SDK returns a pydantic model, whose `model_dump(mode="json")` is exactly the
    wire shape; anything else that happens to be JSON-shaped is passed through. A response
    that is neither is reported as unavailable rather than guessed at, because a fabricated
    audit payload is worse than an absent one.
    """

    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump(mode="json")
        except TypeError:  # not a pydantic model, but shaped like one
            dumped = dump()
        return dumped if isinstance(dumped, dict) else None
    if isinstance(response, dict):
        return response
    return None
