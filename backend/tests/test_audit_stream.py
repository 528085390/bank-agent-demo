"""Streaming model calls, from the audit's side.

The audit's contract is one attempt, one record. A stream is one attempt that lasts a while,
so the interesting cases are the ones where it does not end the way the caller hoped: it
fails halfway, or the caller walks away. Both are still calls, and both are still recorded.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.audit.model_client import AuditedModelClient
from app.audit.service import AuditService
from app.integration.model_client import (
    ChatCompletionsModelClient,
    StreamChunk,
    UnconfiguredModelClient,
)
from app.storage.models import LlmCallAudit

MESSAGES = (
    {"role": "system", "content": "你是总调度。"},
    {"role": "user", "content": "查一下我的持仓"},
)
TOOLS = ({"type": "function", "function": {"name": "handoff", "parameters": {}}},)


class ScriptedStream:
    """A stream that yields what it was given, and can fail at the end."""

    def __init__(self, *chunks: StreamChunk, error: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._error = error
        self.calls: list[tuple[dict, ...]] = []
        self.tools: list[tuple[dict, ...]] = []
        self.closed = False

    def stream(self, messages, *, tools=()):
        self.calls.append(tuple(messages))
        self.tools.append(tuple(tools))
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    def complete(self, messages):  # pragma: no cover - the decorator must not call this
        raise AssertionError("the decorator streamed, so this must not be reached")

    def close(self) -> None:
        self.closed = True


def stored(rt) -> list[LlmCallAudit]:
    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def audited(rt, inner) -> AuditedModelClient:
    return AuditedModelClient(inner, AuditService(rt.database))


def test_a_finished_stream_leaves_exactly_one_row_that_says_it_streamed(rt):
    client = audited(rt, ScriptedStream(StreamChunk(text_delta="你"), StreamChunk(text_delta="好")))

    chunks = list(client.stream(MESSAGES))

    assert "".join(chunk.text_delta for chunk in chunks) == "你好"
    rows = stored(rt)
    assert len(rows) == 1
    assert rows[0].result == "SUCCEEDED"
    assert rows[0].stream is True
    assert rows[0].response_text == "你好"


def test_the_request_records_the_tools_that_were_sent(rt):
    client = audited(rt, ScriptedStream(StreamChunk(text_delta="好")))

    list(client.stream(MESSAGES, tools=TOOLS))

    row = stored(rt)[0]
    assert row.prompt_json["parameters"]["tools"] == list(TOOLS)
    assert row.prompt_json["messages"] == list(MESSAGES)


def test_a_stream_that_fails_midway_is_a_failed_row(rt):
    from app.integration.model_client import ModelClientError

    failure = ModelClientError("LLM_UPSTREAM_ERROR", retryable=True)
    client = audited(rt, ScriptedStream(StreamChunk(text_delta="前"), error=failure))

    with pytest.raises(ModelClientError):
        list(client.stream(MESSAGES))

    rows = stored(rt)
    assert len(rows) == 1
    assert rows[0].result == "FAILED"
    assert rows[0].error_code == "LLM_UPSTREAM_ERROR"
    assert rows[0].stream is True


def test_a_caller_that_walks_away_mid_stream_still_leaves_a_row(rt):
    """An audit that only records the streams somebody finished is an audit with a hole."""

    client = audited(rt, ScriptedStream(StreamChunk(text_delta="前"), StreamChunk(text_delta="后")))

    stream = client.stream(MESSAGES)
    next(stream)
    stream.close()

    rows = stored(rt)
    assert len(rows) == 1
    assert rows[0].result == "FAILED"
    assert rows[0].error_code == "CLIENT_CLOSED"
    assert rows[0].stream is True


def test_a_stream_that_never_started_leaves_no_row(rt):
    """Nothing was sent, so there is nothing to record."""

    client = audited(rt, ScriptedStream(StreamChunk(text_delta="前")))

    stream = client.stream(MESSAGES)
    stream.close()

    assert stored(rt) == []


def test_a_row_written_without_the_flag_reads_back_as_not_streamed(rt):
    AuditService(rt.database).record_llm_call(
        call_id="call_unflagged", request={"messages": []}, result="SUCCEEDED"
    )

    assert stored(rt)[0].stream is False


def test_the_column_is_not_nullable():
    """The default covers the rows that existed before the column did; NOT NULL covers the rest."""

    column = LlmCallAudit.__table__.c.stream
    assert column.nullable is False
    assert column.server_default is not None


def test_every_client_in_the_process_implements_the_whole_protocol():
    """A client missing stream fails with AttributeError in production, at the worst moment."""

    for client in (ChatCompletionsModelClient, UnconfiguredModelClient, AuditedModelClient):
        for name in ("complete", "stream", "close"):
            assert callable(getattr(client, name, None)), f"{client.__name__} lacks {name}"
