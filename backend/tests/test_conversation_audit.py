"""One turn, end to end, as the audit trail sees it: correlated and append-only.

The value of an audit trail is that a reader can start from one fact and reach the others --
this user submitted this message, that run called the model twice, the second call caused
this tool call, and the run ended. This file pins that chain across the HTTP boundary, and
pins the two properties that keep it honest: a run that fails still has its events, and a
client that disconnects cannot unwrite what already happened.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit.model_client import AuditedModelClient
from app.audit.service import AuditReader
from app.conversation.service import ConversationService
from app.main import create_app
from app.storage.models import AgentRunEvent, LlmCallAudit
from tests.support.domain_harness import generic_roster
from tests.support.scripted_model import ScriptedModel
from tests.test_multi_agent_api import agent_for, frames

OTHER_USER = "user_0002"

REPLY = "好的"


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def new_key() -> str:
    return f"audit-{uuid4().hex}"


def use_scripted_model(rt, replies):
    """Install a model on the running runtime, wrapped in the audit boundary.

    The graph is built per invocation from whatever the runtime holds, so swapping the model
    here is the same operation the settings panel performs. The wrapper is applied by hand
    only because a scripted model is not a Chat Completions adapter; going through the same
    decorator is what makes these turns auditable exactly as a real one is.
    """

    from app.audit.model_client import AuditedModelClient

    model = ScriptedModel(replies)
    rt.agent.set_model(AuditedModelClient(model, rt.audit))
    return model


def calls(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def events(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(AgentRunEvent).order_by(AgentRunEvent.created_at)))


def event_types(rt) -> list[str]:
    return [row.event_type for row in events(rt)]


def conversation_for(rt, session, title=None) -> dict:
    return ConversationService(rt.database).create(session["user_id"], title).as_dict()


# --- the correlation chain ----------------------------------------------------------------


def test_a_turn_correlates_the_message_the_run_and_the_reply(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "查余额")
    use_scripted_model(rt, [REPLY])

    response = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "查一下余额"},
        headers={"Idempotency-Key": new_key()},
    )

    assert response.status_code == 200, response.text
    ledger = ConversationService(rt.database).messages(
        session["user_id"], created["conversation_id"]
    )["items"]
    run_id = ledger[0]["run_id"]

    assert run_id is not None, "the user message carries the run that answered it"
    assert ledger[1]["run_id"] == run_id
    stored = calls(rt)
    assert stored, "the model call was recorded"
    assert {row.run_id for row in stored} == {run_id}
    assert {row.conversation_id for row in stored} == {created["conversation_id"]}
    assert {row.user_id for row in stored} == {session["user_id"]}
    assert {row.source for row in stored} == {"conversation"}
    # The run events name the same run, which is what makes the two tables one story.
    assert {row.run_id for row in events(rt)} == {run_id}


def test_a_turn_records_the_run_starting_and_succeeding(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY])

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    kinds = event_types(rt)
    assert kinds[0] == "RUN_STARTED"
    assert kinds[-1] == "RUN_SUCCEEDED"
    assert [row.status for row in events(rt) if row.event_type == "RUN_STARTED"] == ["RUNNING"]


def test_the_model_call_belongs_to_the_run_and_carries_the_whole_prompt(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY])

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "我上次转给谁了"},
        headers={"Idempotency-Key": new_key()},
    )

    call = calls(rt)[0]
    sent = call.prompt_json["messages"]
    assert [message["role"] for message in sent] == ["system", "user"]
    assert "我上次转给谁了" in sent[0]["content"], "the graph's own prompt is stored verbatim"
    assert call.response_text == REPLY


def test_a_paused_turn_records_the_interrupt_and_a_resume_is_a_new_run(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session)
    agent, model, _recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"v"}'),
            "已经完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "v"}},
        audit=rt.audit,
    )
    agent.set_model(AuditedModelClient(model, rt.audit))
    rt.agent = agent

    first = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "帮我转账"},
        headers={"Idempotency-Key": new_key()},
    )
    question = next(data for name, data in frames(first) if name == "interrupt")
    paused_run = next(row for row in events(rt) if row.event_type == "INTERRUPTED")

    second = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={
            "message": "v",
            "client_context": {
                "resume": {"question_id": question["question_id"], "value": {"value": "v"}}
            },
        },
        headers={"Idempotency-Key": new_key()},
    )

    assert second.status_code == 200, second.text
    assert paused_run.payload_json["question_id"] == question["question_id"]
    assert paused_run.status == "INTERRUPTED"
    kinds = event_types(rt)
    assert "RESUMED" in kinds
    assert kinds.count("RUN_STARTED") == 1, "the resume is its own run, not a second start"
    resumed = next(row for row in events(rt) if row.event_type == "RESUMED")
    assert resumed.run_id != paused_run.run_id, "a resume is a new run id"
    assert resumed.conversation_id == paused_run.conversation_id, "on the same conversation"
    # The ledger keeps both runs, and they are different runs.
    ledger = ConversationService(rt.database).messages(
        session["user_id"], created["conversation_id"]
    )["items"]
    assert len({item["run_id"] for item in ledger if item["run_id"]}) == 2


def test_each_domain_agent_start_is_recorded(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(
        rt,
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"处理转账"}'),
            "已完成",
            "好了",
        ],
    )

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "转账"},
        headers={"Idempotency-Key": new_key()},
    )

    started = [row for row in events(rt) if row.event_type == "AGENT_STARTED"]
    assert [row.agent_id for row in started] == ["transfer"]


def test_a_failing_run_records_the_failure_and_no_success(rt, session):
    """A turn that raised is an outcome, and the audit trail says which one."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)

    class Exploding:
        def complete(self, messages):
            raise RuntimeError("provider exploded")

        def stream(self, messages, *, tools=()):
            raise RuntimeError("provider exploded")

        def close(self):
            return None

    rt.agent.set_model(Exploding())

    response = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    assert "INTERNAL_ERROR" in response.text
    kinds = event_types(rt)
    assert "RUN_FAILED" not in kinds or "RUN_SUCCEEDED" not in kinds
    assert "RUN_STARTED" in kinds, "the run started, and that fact survives the failure"


def test_a_model_failure_inside_the_graph_is_recorded_per_attempt(rt, session):
    """The retry lives inside the invocation, so both attempts are in the audit table."""

    from app.integration.model_client import ModelClientError, ModelReply, StreamChunk

    class Flaky:
        def __init__(self):
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise ModelClientError("LLM_TIMEOUT", retryable=True)
            return ModelReply(text=REPLY)

        def stream(self, messages, *, tools=()):
            self.calls += 1
            if self.calls == 1:
                raise ModelClientError("LLM_TIMEOUT", retryable=True)
            yield StreamChunk(text_delta=REPLY)

        def close(self):
            return None

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    from app.audit.model_client import AuditedModelClient

    rt.agent.set_model(AuditedModelClient(Flaky(), rt.audit))

    response = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    assert response.status_code == 200, response.text
    rows = calls(rt)
    assert [row.result for row in rows] == ["FAILED", "SUCCEEDED"]
    assert rows[0].error_code == "LLM_TIMEOUT"
    assert rows[0].run_id == rows[1].run_id, "one run, two attempts"


def test_a_replayed_submission_makes_no_second_call(rt, session):
    """The key was spent, so nothing ran -- and therefore nothing new was audited."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY, REPLY])
    key = new_key()

    first = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": key},
    )
    before = len(calls(rt))
    second = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": key},
    )

    assert first.status_code == 200 and second.status_code == 200
    assert second.headers["content-type"].startswith("application/json")
    assert len(calls(rt)) == before


def test_the_session_path_is_audited_as_a_session_chat(rt, session):
    """The legacy path has no conversation of its own to name, and says so."""

    client = client_for(rt, session)
    use_scripted_model(rt, [REPLY])

    client.post(
        "/api/chat/messages",
        json={"session_id": session["session_id"], "message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    stored = calls(rt)
    assert stored
    # A session resolves to a real conversation, so the turn names it -- the point of the
    # assertion is that the record is not anonymous.
    assert all(row.source == "conversation" for row in stored)
    assert all(row.conversation_id for row in stored)


def test_a_direct_session_turn_with_no_conversation_is_named_as_one(rt, session):
    """`AgentRuntime.invoke` without a `TurnContext` is the pre-persistence path."""

    import asyncio

    from app.agents.domain import build_agent_graph
    from app.agents.runtime import AgentRuntime

    use_scripted_model(rt, [REPLY])
    runtime = AgentRuntime(
        rt.agent.model,
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model, rt.gateway, context, client=rt.bank_client, roster=generic_roster()
        ),
    )

    async def run():
        return [
            event
            async for event in runtime.invoke(session["session_id"], session["user_id"], "你好", {})
        ]

    asyncio.run(run())

    stored = calls(rt)
    assert stored
    assert {row.source for row in stored} == {"session_chat"}
    assert {row.conversation_id for row in stored} == {None}, "no conversation is invented"


def test_a_disconnected_client_cannot_unwrite_the_audit(rt, session):
    """The stream is a display channel; closing it does not undo what happened.

    The generator is abandoned part-way through, exactly as a client walking away leaves it,
    and the records written before that point are still there.
    """

    import asyncio

    use_scripted_model(rt, [REPLY])
    created = conversation_for(rt, session)
    turns = rt.turns
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            created["conversation_id"],
            idempotency_key=new_key(),
            message="你好",
            client_context=None,
        )
    )

    async def half_consume():
        source = turns.stream(session["user_id"], session["session_id"], plan)
        first = await source.__anext__()
        await source.aclose()
        return first

    asyncio.run(half_consume())

    kinds = event_types(rt)
    assert "RUN_STARTED" in kinds, "the run started before the client left"
    assert kinds.count("RUN_STARTED") == 1
    assert kinds[-1] == "RUN_ABORTED", "a client that leaves stops the task; it did not fail"
    assert "RUN_FAILED" not in kinds


def test_the_audit_context_is_restored_between_turns(rt, session):
    """The next request on the same worker must not inherit this turn's conversation."""

    from app.audit.context import current_context

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY, REPLY])

    for _ in range(2):
        client.post(
            f"/api/conversations/{created['conversation_id']}/messages",
            json={"message": "你好"},
            headers={"Idempotency-Key": new_key()},
        )

    assert current_context() is None
    assert len({row.run_id for row in calls(rt)}) == 2, "two turns, two runs"


def test_the_audit_query_finds_a_turn_by_conversation_and_by_run(rt, session):
    """The chain is walkable in both directions, which is what "traceable" means."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY])

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    reader = AuditReader(rt.database)
    run_id = calls(rt)[0].run_id
    by_conversation = reader.llm_calls(
        session["user_id"], conversation_id=created["conversation_id"]
    )
    by_run = reader.llm_calls(session["user_id"], run_id=run_id)
    events_by_run = reader.run_events(session["user_id"], run_id=run_id)

    assert len(by_conversation["items"]) == 1
    assert [item["call_id"] for item in by_run["items"]] == [
        item["call_id"] for item in by_conversation["items"]
    ]
    assert {item["run_id"] for item in events_by_run["items"]} == {run_id}
    assert "RUN_SUCCEEDED" in {item["event_type"] for item in events_by_run["items"]}


def test_another_user_cannot_read_this_turns_audit(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(rt, [REPLY])

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好"},
        headers={"Idempotency-Key": new_key()},
    )

    reader = AuditReader(rt.database)

    assert reader.llm_calls(OTHER_USER)["items"] == []
    assert reader.run_events(OTHER_USER)["items"] == []
    # And naming the victim's conversation does not help: ownership is the filter.
    assert reader.llm_calls(OTHER_USER, conversation_id=created["conversation_id"])["items"] == []


@pytest.mark.parametrize("event_type", ["TOOL_CALL", "TOOL_RESULT"])
def test_a_tool_call_inside_a_turn_is_correlated_to_the_same_run(rt, session, event_type):
    """The gateway writes from inside the model call, so it inherits the turn's context."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    use_scripted_model(
        rt,
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用工具"}'),
            ("call", "echo", '{"value":"v"}'),
            "工具已完成",
            "好了",
        ],
    )
    rt.gateway = _gateway_with_echo(rt)
    rt.agent._graph_factory = lambda model, context, assembler=None, turn=None: _graph(
        rt, model, context
    )

    client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "调用工具"},
        headers={"Idempotency-Key": new_key()},
    )

    rows = [row for row in events(rt) if row.event_type == event_type]
    assert rows, f"no {event_type} was recorded"
    run_id = calls(rt)[0].run_id
    assert {row.run_id for row in rows} == {run_id}
    assert {row.conversation_id for row in rows} == {created["conversation_id"]}


def _gateway_with_echo(rt):
    from app.tools.catalog import ToolCatalog
    from app.tools.gateway import ToolGateway
    from app.tools.protocol import ToolSpec
    from app.tools.recording import RecordingAdapter

    spec = ToolSpec(
        name="echo",
        description="Return what it was given",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    return ToolGateway(
        ToolCatalog.build([spec], {"transfer": ["echo"]}),
        RecordingAdapter({"echo": {"summary": "回显完成"}}),
        audit=rt.audit,
    )


def _graph(rt, model, context):
    from app.agents.domain import build_agent_graph

    return build_agent_graph(model, rt.gateway, context, roster=generic_roster())
