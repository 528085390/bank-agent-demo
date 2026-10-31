"""The streaming half of the model boundary.

The boundary turns a provider's chunk soup into one shape the graph can consume, and keeps
the third channel -- the model's own reasoning -- inside this module. A test that only
checked the happy path would miss both.
"""

from __future__ import annotations

import pytest

from app.integration.model_client import (
    FEATURE_TOOLS,
    ChatCompletionsModelClient,
    FeatureUnsupported,
    ModelClientError,
)
from app.integration.model_config import ChatCompletionsConfig

MESSAGES = (
    {"role": "system", "content": "你是总调度。"},
    {"role": "user", "content": "查一下我的持仓"},
)

TOOLS = (
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": "把请求交给某个领域 Agent",
            "strict": True,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
)


def config(**overrides) -> ChatCompletionsConfig:
    values = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model": "mock-model",
        "response_format": "json_object",
    }
    values.update(overrides)
    return ChatCompletionsConfig(**values)


class _Chunk:
    def __init__(self, *, content=None, reasoning=None, tool_calls=None, finish_reason=None):
        self.choices = [_Choice(content, reasoning, tool_calls, finish_reason)]


class _Choice:
    def __init__(self, content, reasoning, tool_calls, finish_reason):
        self.delta = _Delta(content, reasoning, tool_calls)
        self.finish_reason = finish_reason


class _Delta:
    def __init__(self, content, reasoning, tool_calls):
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls


class _Function:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, name=None, arguments=None, index=0):
        self.index = index
        self.function = _Function(name, arguments)


class _BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status_code = status


class _Sdk:
    """A fake OpenAI SDK: one fixed chunk list, one captured request."""

    def __init__(self, chunks, *, error=None) -> None:
        self._chunks = chunks
        self._error = error
        self.captured: dict = {}
        self.chat = _Chat(self)

    def close(self) -> None:
        return None


class _Chat:
    def __init__(self, sdk) -> None:
        self.completions = _Completions(sdk)


class _Completions:
    def __init__(self, sdk) -> None:
        self._sdk = sdk

    def create(self, **kwargs):
        self._sdk.captured.update(kwargs)
        if self._sdk._error is not None:
            raise self._sdk._error
        return iter(self._sdk._chunks)


def adapter(sdk, **overrides) -> ChatCompletionsModelClient:
    return ChatCompletionsModelClient(config(**overrides), client_factory=lambda **_: sdk)


def test_text_arrives_as_deltas_in_order():
    sdk = _Sdk(
        [_Chunk(content="你"), _Chunk(content="好"), _Chunk(content="！", finish_reason="stop")]
    )

    chunks = list(adapter(sdk).stream(MESSAGES))

    assert "".join(chunk.text_delta for chunk in chunks) == "你好！"
    assert chunks[-1].finish is True
    assert all(chunk.tool_call_delta == "" for chunk in chunks)
    assert all(chunk.truncated is False for chunk in chunks)


def test_the_reasoning_channel_never_leaves_the_module():
    """A red line: reasoning is not the answer, and it is not for the user."""

    sdk = _Sdk(
        [
            _Chunk(reasoning="我先想想", content="好的"),
            _Chunk(reasoning="再想想", finish_reason="stop"),
        ]
    )

    chunks = list(adapter(sdk).stream(MESSAGES))

    assert "".join(chunk.reasoning_delta for chunk in chunks) == "我先想想再想想"
    assert "".join(chunk.text_delta for chunk in chunks) == "好的"
    assert "想想" not in "".join(chunk.text_delta for chunk in chunks)


def test_tool_name_and_arguments_arrive_on_separate_deltas():
    """The provider sends the name once, then the arguments in pieces."""

    sdk = _Sdk(
        [
            _Chunk(tool_calls=[_ToolCallDelta(name="handoff", arguments='{"agent_id":')]),
            _Chunk(tool_calls=[_ToolCallDelta(arguments='"wealth"}')], finish_reason="tool_calls"),
        ]
    )

    chunks = list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    assert [chunk.tool_name for chunk in chunks if chunk.tool_name] == ["handoff"]
    assert "".join(chunk.tool_call_delta for chunk in chunks) == '{"agent_id":"wealth"}'
    assert [chunk.tool_index for chunk in chunks] == [0, 0]
    assert all(chunk.text_delta == "" for chunk in chunks)


def test_two_parallel_tool_calls_keep_their_own_arguments_apart():
    """Interleaved argument streams: without the index they splice into one broken JSON."""

    sdk = _Sdk(
        [
            _Chunk(
                tool_calls=[
                    _ToolCallDelta(name="handoff", arguments='{"agent_id":"trans', index=0),
                    _ToolCallDelta(name="handoff", arguments='{"agent_id":"wealth', index=1),
                ]
            ),
            _Chunk(
                tool_calls=[
                    _ToolCallDelta(arguments='fer"}', index=0),
                    _ToolCallDelta(arguments='"}', index=1),
                ],
                finish_reason="tool_calls",
            ),
        ]
    )

    chunks = list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    by_index: dict[int | None, str] = {}
    for chunk in chunks:
        if chunk.tool_call_delta:
            by_index[chunk.tool_index] = by_index.get(chunk.tool_index, "") + chunk.tool_call_delta
    assert by_index == {0: '{"agent_id":"transfer"}', 1: '{"agent_id":"wealth"}'}


def test_tools_are_sent_and_response_format_is_not():
    """Structure comes from the tool schema, so the weaker second constraint must not ride along."""

    sdk = _Sdk([_Chunk(content="好", finish_reason="stop")])

    list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    assert sdk.captured["tools"] == list(TOOLS)
    assert sdk.captured["stream"] is True
    assert "response_format" not in sdk.captured
    assert "tool_choice" not in sdk.captured


def test_without_tools_the_request_looks_exactly_as_it_did_before():
    sdk = _Sdk([_Chunk(content="好", finish_reason="stop")])

    list(adapter(sdk).stream(MESSAGES))

    assert sdk.captured["response_format"] == {"type": "json_object"}
    assert "tools" not in sdk.captured


def test_a_stream_that_ends_without_a_finish_reason_is_reported_as_truncated():
    sdk = _Sdk([_Chunk(content="前半句")])

    chunks = list(adapter(sdk).stream(MESSAGES))

    assert chunks[-1].truncated is True
    assert "".join(chunk.text_delta for chunk in chunks) == "前半句"


def test_a_provider_that_refuses_tools_is_reported_as_a_missing_feature():
    sdk = _Sdk([], error=_BadRequest("Invalid parameter: tools is not supported"))

    with pytest.raises(FeatureUnsupported) as raised:
        list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    assert raised.value.feature == FEATURE_TOOLS
    assert raised.value.code == "LLM_FEATURE_UNSUPPORTED"
    assert raised.value.retryable is True


def test_a_provider_that_refuses_strict_is_reported_as_a_missing_strict():
    """The distinction matters: strict is the one feature that can be dropped on its own."""

    sdk = _Sdk([], error=_BadRequest("strict is not supported for this model"))

    with pytest.raises(FeatureUnsupported) as raised:
        list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    assert raised.value.feature == "strict"


def test_an_ordinary_bad_request_is_still_a_bad_request():
    sdk = _Sdk([], error=_BadRequest("bad request"))

    with pytest.raises(ModelClientError) as raised:
        list(adapter(sdk).stream(MESSAGES, tools=TOOLS))

    assert not isinstance(raised.value, FeatureUnsupported)
    assert raised.value.code == "LLM_BAD_REQUEST"


def test_a_failure_before_the_first_chunk_is_not_a_truncated_stream():
    sdk = _Sdk([], error=_BadRequest("bad request"))

    with pytest.raises(ModelClientError):
        list(adapter(sdk).stream(MESSAGES))
