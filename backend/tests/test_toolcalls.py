"""The one place a tool-calling stream becomes a decision.

Every rule about reading a stream lives here: how a failure to *start* differs from a failure
in the middle, how two parallel calls keep their arguments apart, and what a provider that
refuses `tools` outright produces. The Supervisor and the card both go through this module, so
a rule that is wrong here is wrong in both (spec §5, §6.1).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.decisions import DecisionError
from app.agents.toolcalls import StreamTruncated, collect_stream
from app.agents.upstream import UpstreamUnavailable
from app.core.errors import AppError
from app.integration.model_client import FeatureUnsupported, ModelClientError, StreamChunk

MESSAGES = ({"role": "user", "content": "帮我看看持仓"},)
TOOLS = ({"type": "function", "function": {"name": "handoff"}},)


def stream(*chunks: Any):
    """A stream that yields these chunks, raising any exception it finds among them."""

    def make():
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    return make


class FakeModel:
    """One scripted stream per call, counted, so a retry is visible as a second call."""

    def __init__(self, *streams) -> None:
        self._streams = list(streams)
        self.calls = 0
        self.tools: list[tuple[dict[str, Any], ...]] = []

    def stream(self, messages, *, tools=()):
        self.calls += 1
        self.tools.append(tuple(tools))
        yield from self._streams.pop(0)()


def collect(model):
    return collect_stream(model, MESSAGES, TOOLS)


def test_a_provider_that_refuses_tools_is_a_terminal_error():
    """No second protocol and no cache: a refusal ends the turn with something readable."""

    model = FakeModel(stream(FeatureUnsupported("tools")))

    with pytest.raises(AppError) as caught:
        collect(model)

    assert caught.value.details["upstream_code"] == "LLM_TOOLS_UNSUPPORTED"
    assert caught.value.details["feature"] == "tools"
    assert caught.value.retryable is False
    assert caught.value.status == 502
    assert "不支持工具调用" in caught.value.message
    assert model.calls == 1


def test_a_failure_to_start_is_retried_once_and_only_once():
    """Before the first chunk nothing has been shown, so one retry is safe -- and only one."""

    model = FakeModel(
        stream(ModelClientError("LLM_TIMEOUT", retryable=True)),
        stream(StreamChunk(text_delta="先看一眼"), StreamChunk(finish=True)),
    )

    text, calls = collect(model)

    assert (text, calls) == ("先看一眼", [])
    assert model.calls == 2

    spent = FakeModel(
        stream(ModelClientError("LLM_TIMEOUT", retryable=True)),
        stream(ModelClientError("LLM_TIMEOUT", retryable=True)),
    )

    with pytest.raises(UpstreamUnavailable):
        collect(spent)

    assert spent.calls == 2


def test_a_non_retryable_failure_to_start_is_not_retried():
    """A refusal on the merits is reported as itself, not as a transient outage."""

    model = FakeModel(stream(ModelClientError("LLM_AUTH", retryable=False)))

    with pytest.raises(AppError) as caught:
        collect(model)

    assert type(caught.value) is AppError
    assert model.calls == 1
    assert caught.value.status == 502
    assert caught.value.retryable is False
    assert caught.value.details["upstream_code"] == "LLM_AUTH"


def test_a_failure_after_the_first_chunk_is_not_retried():
    """Restarting would rewind an answer the user is already reading (spec §4.6)."""

    model = FakeModel(
        stream(
            StreamChunk(text_delta="已经显示了一半"),
            ModelClientError("LLM_TIMEOUT", retryable=True),
        )
    )

    with pytest.raises(ModelClientError):
        collect(model)

    assert model.calls == 1


def test_parallel_calls_in_one_chunk_keep_their_own_arguments():
    """Two calls interleaved on two indices must not splice into one broken document."""

    model = FakeModel(
        stream(
            StreamChunk(tool_name="handoff", tool_call_delta='{"agent_id":"wealth",', tool_index=0),
            StreamChunk(tool_name="handoff", tool_call_delta='{"agent_id":"card",', tool_index=1),
            StreamChunk(tool_call_delta='"instruction":"查持仓"}', tool_index=0),
            StreamChunk(tool_call_delta='"instruction":"办卡"}', tool_index=1),
            StreamChunk(finish=True),
        )
    )

    text, calls = collect(model)

    assert text == ""
    assert [call.name for call in calls] == ["handoff", "handoff"]
    assert [call.arguments for call in calls] == [
        {"agent_id": "wealth", "instruction": "查持仓"},
        {"agent_id": "card", "instruction": "办卡"},
    ]


def test_a_truncated_stream_raises_stream_truncated():
    """The provider closed without a finish signal, so there is no decision to read."""

    model = FakeModel(stream(StreamChunk(truncated=True)))

    with pytest.raises(StreamTruncated):
        collect(model)


def test_the_collector_passes_the_tools_through_unchanged():
    """The schemas are the caller's: this module never relaxes or rewrites them."""

    model = FakeModel(stream(StreamChunk(text_delta="好"), StreamChunk(finish=True)))

    collect(model)

    assert model.tools == [TOOLS]


def test_arguments_that_are_not_json_are_a_decision_error():
    """A provider that sends a broken document must not surface as a 500 (spec §3)."""

    model = FakeModel(
        stream(
            StreamChunk(tool_name="handoff", tool_call_delta='{"agent_id":', tool_index=0),
            StreamChunk(finish=True),
        )
    )

    with pytest.raises(DecisionError, match="不是合法 JSON"):
        collect(model)


def test_arguments_that_are_not_an_object_are_a_decision_error():
    model = FakeModel(
        stream(
            StreamChunk(tool_name="handoff", tool_call_delta='["wealth"]', tool_index=0),
            StreamChunk(finish=True),
        )
    )

    with pytest.raises(DecisionError, match="不是 JSON 对象"):
        collect(model)


def test_a_stream_that_ends_without_a_chunk_is_an_empty_decision():
    """A provider that returns nothing is a turn with nothing in it, not a crash."""

    assert collect(FakeModel(stream())) == ("", [])


def test_a_truncation_after_text_still_raises_stream_truncated():
    """The text already shown cannot be retracted, so the truncation is the caller's to close."""

    model = FakeModel(stream(StreamChunk(text_delta="已经说了一半"), StreamChunk(truncated=True)))

    with pytest.raises(StreamTruncated):
        collect(model)


def test_a_delta_without_a_name_keeps_the_empty_name_for_the_parser():
    """The collector does not guess a name: the parser is what refuses an unknown tool."""

    model = FakeModel(
        stream(
            StreamChunk(tool_call_delta='{"agent_id":"wealth"}', tool_index=0),
            StreamChunk(finish=True),
        )
    )

    text, calls = collect(model)

    assert text == ""
    assert [(call.name, call.arguments) for call in calls] == [("", {"agent_id": "wealth"})]
