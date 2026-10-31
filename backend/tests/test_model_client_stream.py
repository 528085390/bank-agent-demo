"""The stream boundary carries the tool-call identity and the upstream finish reason.

`_stream_chunk` is the one place a provider chunk is projected onto `StreamChunk`. Two of the
audit-relevant facts never made it across that boundary: *which* tool call a delta belongs to
(the provider's own id, not the positional index) and *why* the upstream stopped. Without them a
streaming turn cannot be reconstructed after the fact -- the audit side can only see an index and
a boolean. These tests pin the projection, including the "upstream did not tell us" cases, which
must stay `None` rather than being guessed at.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.integration.model_client import _stream_chunk


def _call(*, identifier=None, name=None, arguments=None, index=None) -> SimpleNamespace:
    """One tool-call delta, shaped like the openai SDK's."""

    return SimpleNamespace(
        id=identifier,
        index=index,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(*, content=None, reasoning=None, tool_calls=None, finish_reason=None) -> SimpleNamespace:
    """One provider chunk, shaped like the openai SDK's."""

    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def test_two_parallel_tool_calls_carry_their_own_upstream_id_and_finish_reason():
    """The id is the provider's, and it rides on the same delta as the call it names."""

    fake = _chunk(
        tool_calls=[
            _call(identifier="call_a", name="handoff", arguments='{"agent_id":"wealth"}', index=0),
            _call(identifier="call_b", name="handoff", arguments='{"agent_id":"card"}', index=1),
        ],
        finish_reason="tool_calls",
    )

    chunks = list(_stream_chunk(fake))

    assert [c.tool_call_id for c in chunks] == ["call_a", "call_b"]
    assert [c.finish_reason for c in chunks] == ["tool_calls", "tool_calls"]


def test_a_text_chunk_has_no_tool_call_id_and_carries_its_own_finish_reason():
    fake = _chunk(content="好的", finish_reason="stop")

    chunks = list(_stream_chunk(fake))

    assert len(chunks) == 1
    assert chunks[0].tool_call_id is None
    assert chunks[0].finish_reason == "stop"


def test_a_chunk_without_an_upstream_finish_reason_stays_none():
    """Absence is information: a missing reason must not be invented on this side."""

    fake = _chunk(content="前半句")

    chunks = list(_stream_chunk(fake))

    assert chunks[0].finish_reason is None


def test_a_tool_call_without_an_upstream_id_reports_none_not_an_empty_string():
    fake = _chunk(
        tool_calls=[_call(name="handoff", arguments="{}", index=0)],
        finish_reason="tool_calls",
    )

    chunks = list(_stream_chunk(fake))

    assert chunks[0].tool_call_id is None
    assert chunks[0].finish_reason == "tool_calls"


def test_a_chunk_with_no_choices_reports_neither_id_nor_reason():
    chunks = list(_stream_chunk(SimpleNamespace(choices=[])))

    assert chunks[0].tool_call_id is None
    assert chunks[0].finish_reason is None
