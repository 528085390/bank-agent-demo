"""The model boundary's audit decorator: what it records, and what it refuses to change.

The decorator has one job -- leave the model contract exactly as it was -- and one
obligation: a row per attempt, written before the answer is handed back. Both halves are
pinned here. The transparency half matters because every caller in the graph goes through
this object, and the recording half matters because the audit spec's first acceptance
criterion is that a failed call and each retry are individually traceable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agents.domain import build_agent_graph, build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.sdk import domain_scope
from app.agents.upstream import call_with_one_retry
from app.audit.context import AGENT_ID_SUPERVISOR, AuditContext, audit_context, current_context
from app.audit.model_client import AuditedModelClient, error_code_for
from app.audit.service import AuditService
from app.conversation.service import ConversationService
from app.core.errors import AppError
from app.integration.model_client import (
    ChatCompletionsModelClient,
    ModelClientError,
    ModelReply,
    StreamChunk,
    UnconfiguredModelClient,
    classify_provider_error,
    is_configured,
)
from app.integration.model_config import ChatCompletionsConfig
from app.storage.models import LlmCallAudit
from app.tools.protocol import TrustedContext
from tests.support.domain_harness import generic_roster
from tests.support.scripted_model import ScriptedModel

CONVERSATION = "conv_decorator"
RUN = "run_decorator"
USER = "user_0001"


class Scripted:
    """A model that answers from a script and remembers what it was asked."""

    def __init__(self, *replies: ModelReply) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[dict, ...]] = []
        self.closed = False

    def complete(self, messages):
        self.calls.append(tuple(messages))
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self) -> None:
        self.closed = True


class Streaming:
    """A model that answers in chunks, so a streamed call can be recorded.

    The chunks are handed over verbatim: this double exists to put a specific sequence of
    deltas on the boundary, not to model a provider.
    """

    def __init__(self, *chunks: StreamChunk) -> None:
        self._chunks = list(chunks)

    def stream(self, messages, *, tools=()):
        yield from self._chunks

    def close(self) -> None:
        return None


def stored(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def audited(rt, inner, **kwargs) -> AuditedModelClient:
    return AuditedModelClient(inner, AuditService(rt.database), **kwargs)


MESSAGES = (
    {"role": "system", "content": "只输出一个 JSON 对象。"},
    {"role": "user", "content": '请输出 {"ok":true}'},
)

# What a scripted model answers when a whole turn is driven, rather than one boundary call.
REPLY = "好的"


def test_the_request_is_stored_exactly_as_it_was_sent(rt):
    inner = Scripted(ModelReply(text='{"ok":true}'))
    client = audited(rt, inner)

    client.complete(MESSAGES)

    row = stored(rt)[0]
    assert row.prompt_json["messages"] == list(MESSAGES)
    assert row.result == "SUCCEEDED"
    assert row.response_text == '{"ok":true}'


def test_a_successful_call_stores_the_whole_provider_response(rt):
    """The normalized text is a projection; the audit record keeps the original."""

    raw = {
        "id": "chatcmpl_1",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"ok":true}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 21, "completion_tokens": 6, "total_tokens": 27},
    }
    client = audited(rt, Scripted(ModelReply(text='{"ok":true}', raw=raw)))

    client.complete(MESSAGES)

    row = stored(rt)[0]
    assert row.response_json == raw
    assert (row.input_tokens, row.output_tokens) == (21, 6)
    assert row.latency_ms is not None and row.latency_ms >= 0


def test_a_provider_that_reports_no_usage_stores_no_counts(rt):
    """A missing count is a fact, not a zero."""

    client = audited(rt, Scripted(ModelReply(text="ok", raw={"choices": []})))

    client.complete(MESSAGES)

    row = stored(rt)[0]
    assert (row.input_tokens, row.output_tokens) == (None, None)


def test_a_failed_call_is_recorded_and_the_failure_continues_unchanged(rt):
    """The graph branches on this exception, so its type and code must survive."""

    failure = ModelClientError("LLM_TIMEOUT", retryable=True, detail="HTTP 408")
    client = audited(rt, Scripted(failure))

    with pytest.raises(ModelClientError) as raised:
        client.complete(MESSAGES)

    assert raised.value is failure, "the very exception, not a copy of it"
    row = stored(rt)[0]
    assert row.result == "FAILED"
    assert row.error_code == "LLM_TIMEOUT"
    assert row.prompt_json["messages"] == list(MESSAGES), "the request is still evidence"
    assert row.response_json is None


def test_an_app_error_from_an_unconfigured_model_is_recorded_and_re_raised(rt):
    inner = UnconfiguredModelClient()
    client = audited(rt, inner)

    with pytest.raises(AppError) as raised:
        client.complete(MESSAGES)

    assert raised.value.code == "MODEL_NOT_CONFIGURED"
    assert stored(rt)[0].error_code == "MODEL_NOT_CONFIGURED"


def test_a_crash_that_is_not_a_boundary_failure_is_recorded_by_type(rt):
    """An unclassified failure is named by its type; its message can carry request contents."""

    client = audited(rt, Scripted(RuntimeError("secret prompt text")))

    with pytest.raises(RuntimeError):
        client.complete(MESSAGES)

    row = stored(rt)[0]
    assert row.error_code == "RuntimeError"
    assert "secret prompt text" not in (row.error_code or "")


def test_one_retry_leaves_two_independent_call_records(rt):
    """The number of attempts is read from the table, not inferred from a counter."""

    inner = Scripted(
        ModelClientError("LLM_TIMEOUT", retryable=True),
        ModelReply(text="重试成功"),
    )
    client = audited(rt, inner)

    reply = call_with_one_retry(lambda: client.complete(MESSAGES))

    assert reply.text == "重试成功"
    rows = stored(rt)
    assert [row.result for row in rows] == ["FAILED", "SUCCEEDED"]
    assert [row.error_code for row in rows] == ["LLM_TIMEOUT", None]
    assert len({row.call_id for row in rows}) == 2, "each attempt has its own id"


def test_every_call_gets_a_unique_id(rt):
    client = audited(rt, Scripted(ModelReply(text="一"), ModelReply(text="二")))

    client.complete(MESSAGES)
    client.complete(MESSAGES)

    rows = stored(rt)
    assert len(rows) == 2
    assert len({row.call_id for row in rows}) == 2
    assert len({row.audit_id for row in rows}) == 2


def test_close_reaches_the_wrapped_client(rt):
    inner = Scripted(ModelReply(text="ok"))
    client = audited(rt, inner)

    client.close()

    assert inner.closed is True


def test_the_decorator_is_transparent_about_being_configured(rt):
    """A wrapped unconfigured model is still unconfigured: the health check must say so."""

    assert is_configured(audited(rt, UnconfiguredModelClient())) is False
    assert is_configured(UnconfiguredModelClient()) is False


def test_a_call_inside_a_turn_inherits_the_conversation_and_run(rt):
    client = audited(rt, Scripted(ModelReply(text="ok")))

    with audit_context(
        AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER, source="conversation")
    ):
        client.complete(MESSAGES)

    row = stored(rt)[0]
    assert (row.conversation_id, row.run_id, row.user_id) == (CONVERSATION, RUN, USER)
    assert row.source == "conversation"


def test_a_call_with_no_turn_records_no_conversation(rt):
    """A probe belongs to nothing, and must not borrow the previous turn's ids."""

    client = audited(rt, Scripted(ModelReply(text="ok")))

    with audit_context(
        AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER, source="conversation")
    ):
        pass
    client.complete(MESSAGES)

    row = stored(rt)[0]
    assert (row.conversation_id, row.run_id, row.user_id, row.source) == (None, None, None, None)


def test_an_explicit_context_wins_over_the_turn(rt):
    client = audited(rt, Scripted(ModelReply(text="ok")))

    with audit_context(AuditContext(conversation_id=CONVERSATION, user_id=USER)):
        client.complete(MESSAGES)

    assert stored(rt)[0].conversation_id == CONVERSATION


def test_the_context_is_restored_after_the_turn(rt):
    """A worker reused for the next request must not still be in the last conversation."""

    with audit_context(AuditContext(conversation_id=CONVERSATION, user_id=USER)):
        assert current_context().conversation_id == CONVERSATION

    assert current_context() is None


def test_a_nested_turn_restores_the_outer_one(rt):
    outer = AuditContext(conversation_id="conv_outer", user_id=USER)
    inner = AuditContext(conversation_id="conv_inner", user_id=USER)

    with audit_context(outer):
        with audit_context(inner):
            assert current_context().conversation_id == "conv_inner"
        assert current_context().conversation_id == "conv_outer"


def test_the_context_is_restored_even_when_the_turn_raises(rt):
    with pytest.raises(RuntimeError):
        with audit_context(AuditContext(conversation_id=CONVERSATION)):
            raise RuntimeError("boom")

    assert current_context() is None


def test_the_recorded_parameters_are_what_the_adapter_actually_sends(rt):
    """Not what the decorator assumed: the adapter is asked."""

    inner = Scripted(ModelReply(text="ok"))
    inner.request_parameters = lambda: {"model": "mock-model", "response_format": {"type": "x"}}
    client = audited(rt, inner)

    client.complete(MESSAGES)

    assert stored(rt)[0].prompt_json["parameters"] == {
        "model": "mock-model",
        "response_format": {"type": "x"},
    }


def test_the_adapter_offers_the_parameters_that_change_the_answer():
    """The API key is a credential, not a request parameter: it is never in this dict."""

    config = ChatCompletionsConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-never-audited",
        model="mock-model",
        response_format="json_object",
    )
    adapter = ChatCompletionsModelClient(config, client_factory=lambda **_: _NullSdk())

    parameters = adapter.request_parameters()

    assert parameters == {"model": "mock-model", "response_format": {"type": "json_object"}}
    assert "sk-never-audited" not in str(parameters)


class _NullSdk:
    def close(self) -> None:
        return None


def test_the_provider_response_is_turned_into_json_by_the_adapter():
    """The SDK returns a pydantic model; the audit row needs the wire shape."""

    from app.integration.model_client import _response_json

    class Response:
        def model_dump(self, **kwargs):
            assert kwargs.get("mode") == "json"
            return {"id": "chatcmpl_1", "choices": [{"message": {"content": "ok"}}]}

    assert _response_json(Response()) == {
        "id": "chatcmpl_1",
        "choices": [{"message": {"content": "ok"}}],
    }
    assert _response_json({"id": "already-json"}) == {"id": "already-json"}
    # Nothing usable is reported as nothing, rather than fabricated.
    assert _response_json(object()) is None


def test_the_error_code_helper_prefers_the_stable_code():
    assert error_code_for(ModelClientError("LLM_AUTH", retryable=False)) == "LLM_AUTH"
    assert error_code_for(AppError("MODEL_NOT_CONFIGURED", "x", 503)) == "MODEL_NOT_CONFIGURED"
    assert error_code_for(ValueError("boom")) == "ValueError"


def test_an_unserializable_request_is_a_loud_failure(rt):
    """A prompt that silently became a repr would look complete and be useless."""

    client = audited(rt, Scripted(ModelReply(text="ok")))

    with pytest.raises(TypeError):
        client.complete(({"role": "user", "content": object()},))

    assert stored(rt) == []


def test_a_failed_audit_write_does_not_hide_the_provider_failure(rt, monkeypatch):
    """Both facts survive: the call failed, and the record could not be written."""

    client = audited(rt, Scripted(ModelClientError("LLM_TIMEOUT", retryable=True)))

    def explode(**_kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(client.audit, "record_llm_call", explode)

    with pytest.raises(RuntimeError) as raised:
        client.complete(MESSAGES)

    assert isinstance(raised.value.__cause__, ModelClientError)


def test_the_recorded_time_is_an_absolute_instant(rt):
    """`timestamptz` round-trips the instant; the zone it is displayed in is the driver's."""

    before = datetime.now(timezone.utc)
    client = audited(rt, Scripted(ModelReply(text="ok")))

    client.complete(MESSAGES)

    created = stored(rt)[0].created_at
    assert created.tzinfo is not None, "a naive timestamp would be ambiguous"
    # The instant, not the offset: the driver may hand it back in the connection's own zone,
    # which is the same moment and therefore the same fact.
    assert before <= created.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def test_classification_still_happens_before_the_decorator_sees_it(rt):
    """The decorator records the classified code, because the adapter classified first."""

    class Provider:
        status_code = 401

    assert classify_provider_error(Provider()).code == "LLM_AUTH"


def test_the_complete_path_records_a_call_that_did_not_stream(rt):
    client = audited(rt, Scripted(ModelReply(text="ok")))

    client.complete(MESSAGES)

    assert stored(rt)[0].stream is False


# --- what a streamed answer leaves behind ---------------------------------------------------
#
# A streamed call used to record its text and nothing else, so every tool call the graph ever
# made was missing from the audit table. The row is what matters here: the shape of
# `response_json` is read back out of the database, not out of the in-memory reply.
#
# The provider's own shape is the one reproduced below: the id and the name arrive with the
# first delta, the arguments stream in pieces after it, and the last chunk states no reason of
# its own -- which is why the fallback is what this test pins.


def test_a_streamed_tool_call_is_recorded_in_the_response_column(rt):
    inner = Streaming(
        StreamChunk(
            tool_call_id="call_upstream",
            tool_name="card_cards",
            tool_call_delta='{"card_suffix":',
            tool_index=0,
        ),
        StreamChunk(tool_call_delta='"3878"}', tool_index=0),
        StreamChunk(finish=True),
    )
    client = audited(rt, inner)

    list(client.stream(MESSAGES))

    row = stored(rt)[0]
    assert row.stream is True
    assert row.response_json["choices"][0]["message"]["tool_calls"][0]["id"] == "call_upstream"
    assert (
        row.response_json["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        == "card_cards"
    )
    assert (
        row.response_json["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        == '{"card_suffix":"3878"}'
    )
    assert row.response_json["choices"][0]["finish_reason"] == "tool_calls"


def test_a_plain_text_stream_still_records_a_null_response(rt):
    """Only the tool-call shape is widened; a text stream keeps the null it always had."""

    client = audited(rt, Streaming(StreamChunk(text_delta="好的"), StreamChunk(finish=True)))

    seen = list(client.stream(MESSAGES))

    row = stored(rt)[0]
    assert [chunk.text_delta for chunk in seen] == ["好的", ""]
    assert row.stream is True
    assert row.response_json is None
    assert row.response_text == "好的"


# --- which agent asked ---------------------------------------------------------------------
#
# `source` says what kind of call it was; `agent_id` says which agent asked. This half of the
# file pins the second question where it is actually read -- on the persisted row. Asserting
# only `current_context()` would not be evidence: `domain_scope` publishes the agent through
# the very ContextVar the turn's ids travel in, so the scope can be perfectly correct while
# every domain call is still stored with no agent at all.


def test_a_call_that_belongs_to_no_agent_records_none(rt):
    """A probe has no agent, and must not borrow the previous turn's."""

    client = audited(rt, Scripted(ModelReply(text="ok")))

    with audit_context(AuditContext(agent_id="card")):
        pass
    client.complete(MESSAGES)

    assert stored(rt)[0].agent_id is None


def test_the_constructor_names_the_agent_a_call_belongs_to(rt):
    """A client bound to one agent at construction is the fallback for a call made outside
    any domain scope, where nothing in the context says which agent asked."""

    client = audited(rt, Scripted(ModelReply(text="ok")), agent_id="card")

    client.complete(MESSAGES)

    assert stored(rt)[0].agent_id == "card"


def test_the_turn_wins_over_the_constructor(rt):
    """One shared client cannot be right about every agent, so the context is read first."""

    client = audited(rt, Scripted(ModelReply(text="ok")), agent_id="card")

    with audit_context(AuditContext(agent_id="wealth")):
        client.complete(MESSAGES)

    assert stored(rt)[0].agent_id == "wealth"


def test_a_failed_call_is_attributed_to_its_agent_too(rt):
    """The failure row is the one an operator reads when a domain misbehaves."""

    client = audited(rt, Scripted(ModelClientError("LLM_TIMEOUT", retryable=True)), agent_id="card")

    with pytest.raises(ModelClientError):
        client.complete(MESSAGES)

    assert stored(rt)[0].agent_id == "card"


def test_a_domain_scope_names_its_agent_on_the_row_it_writes(rt, session):
    """The load-bearing one: the row, not the ContextVar, is what a reader sees.

    The context assertion inside the scope is here only to show the scope did its job. It is
    the row assertion below that fails if the decorator never copies the agent across.
    """

    client = audited(rt, Scripted(ModelReply(text="ok")))
    context = TrustedContext(user_id=session["user_id"], session_id=session["session_id"])
    card = next(
        spec for spec in build_domain_specs(rt.gateway, context) if spec.agent_id == "card"
    )

    with domain_scope(card, {}, None, None, "查一下额度"):
        assert current_context().agent_id == "card"
        client.complete(MESSAGES)

    assert stored(rt)[0].agent_id == "card"


def test_a_card_turn_is_stored_under_the_card(rt, session):
    """The scope above is not enough: the domain's own graph has to open it.

    A module that builds its own graph owns its nodes, so nobody else can open the scope
    around them. If the graph forgets to, every card model call is stored under whoever
    the turn named -- the Supervisor -- and a reader can no longer tell the domain's calls
    from the dispatcher's (acceptance R-9).
    """

    rt.agent.set_model(
        AuditedModelClient(
            ScriptedModel(
                [
                    ("call", "handoff", '{"agent_id":"card","instruction":"查一下我的银行卡"}'),
                    "您名下有一张借记卡。",
                    "已经为您查询过了。",
                ]
            ),
            rt.audit,
        )
    )
    created = ConversationService(rt.database).create(session["user_id"], None).as_dict()
    plan = asyncio.run(
        rt.turns.prepare(
            session["user_id"],
            session["session_id"],
            created["conversation_id"],
            idempotency_key=f"agent-{uuid4().hex}",
            message="我有哪些银行卡？",
            client_context=None,
        )
    )

    async def consume():
        async for _ in rt.turns.stream(session["user_id"], session["session_id"], plan):
            pass

    asyncio.run(consume())

    rows = stored(rt)
    assert "card" in {row.agent_id for row in rows}, (
        "the card graph ran a model call but named nobody on the row"
    )
    assert {row.agent_id for row in rows} == {AGENT_ID_SUPERVISOR, "card"}


@pytest.mark.parametrize("domain", ["transfer", "subscription", "bill"])
def test_every_domain_graph_names_its_own_agent(rt, session, domain):
    """The card is not special: whichever domain owns the graph owns the attribution.

    These three are the same three-node shape as the card, built the same way, so they
    fail the same way -- and the audit trail is exactly where that is invisible until it
    is read back.
    """

    rt.agent.set_model(
        AuditedModelClient(
            ScriptedModel(
                [
                    ("call", "handoff", f'{{"agent_id":"{domain}","instruction":"看一下我的情况"}}'),
                    "已经看过了。",
                    "已经为您看过了。",
                ]
            ),
            rt.audit,
        )
    )
    created = ConversationService(rt.database).create(session["user_id"], None).as_dict()
    plan = asyncio.run(
        rt.turns.prepare(
            session["user_id"],
            session["session_id"],
            created["conversation_id"],
            idempotency_key=f"agent-{uuid4().hex}",
            message="看一下我的情况",
            client_context=None,
        )
    )

    async def consume():
        async for _ in rt.turns.stream(session["user_id"], session["session_id"], plan):
            pass

    asyncio.run(consume())

    assert domain in {row.agent_id for row in stored(rt)}, (
        f"the {domain} graph ran a model call but named nobody on the row"
    )


def test_the_dispatcher_turn_is_stored_under_the_dispatcher(rt, session):
    """The conversation path: the model call a user's message causes is the Supervisor's."""

    rt.agent.set_model(AuditedModelClient(ScriptedModel([REPLY]), rt.audit))
    created = ConversationService(rt.database).create(session["user_id"], None).as_dict()
    turns = rt.turns
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            created["conversation_id"],
            idempotency_key=f"agent-{uuid4().hex}",
            message="你好",
            client_context=None,
        )
    )

    async def consume():
        async for _ in turns.stream(session["user_id"], session["session_id"], plan):
            pass

    asyncio.run(consume())

    rows = stored(rt)
    assert rows, "the turn called the model"
    assert {row.agent_id for row in rows} == {AGENT_ID_SUPERVISOR}


def test_the_session_path_is_stored_under_the_dispatcher(rt, session):
    """`AgentRuntime.invoke` without a turn is the pre-persistence path, and the Supervisor
    is the agent that answers there too."""

    rt.agent.set_model(AuditedModelClient(ScriptedModel([REPLY]), rt.audit))
    runtime = AgentRuntime(
        rt.agent.model,
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model, rt.gateway, context, client=rt.bank_client, roster=generic_roster()
        ),
    )

    async def run():
        return [
            event
            async for event in runtime.invoke(
                session["session_id"], session["user_id"], "你好", {}
            )
        ]

    asyncio.run(run())

    rows = stored(rt)
    assert rows, "the session turn called the model"
    assert {row.agent_id for row in rows} == {AGENT_ID_SUPERVISOR}
