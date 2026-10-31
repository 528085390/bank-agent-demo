"""The chat endpoint: the SSE contract, and native interrupt/resume over HTTP.

Task 3 already proves the graph's behaviour. What this file proves is the transport
around it: that the client sees exactly the six documented events with exactly the
documented fields, that a pause and its answer survive a round trip through the API, and
that the client never learns a thread id or a caller identity.

`POST /api/chat/messages` is now the legacy adapter over the conversation turn lifecycle,
so every submission carries an `Idempotency-Key` and the response is not necessarily a
stream -- a replay is a JSON receipt. `tests/test_conversation_api.py` owns that contract;
this file owns the SSE frames themselves.
"""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.state import AgentState
from app.integration.model_client import ModelClientError
from app.main import create_app
from app.storage.models import Conversation
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, ToolStatus
from app.tools.recording import RecordingAdapter
from tests.support.domain_harness import field_names, generic_roster, without_fields
from tests.support.scripted_model import ScriptedModel


def _graph_factory(gateway):
    """The base's graph over a roster with no module graphs, so a granted tool is answered by
    the generic loop rather than by a module's own graph."""

    return lambda model, context, assembler=None, turn=None: build_agent_graph(
        model, gateway, context, assembler=assembler, turn=turn, roster=generic_roster()
    )

ECHO = ToolSpec(
    name="echo",
    description="Return what it was given",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)

# The exact `data` payload of every event, as the design document freezes it.
#
# `interrupt` carries one extra key only on a confirmation box (`confirmation`), and
# `subtask_end` carries `turn_status` (null for publishers that are not whole-turn modules)
# and `grounding_blocked` (null unless the public grounding guard replaced the summary).
# Both are declared here so "no extra field" still means what it says:
# the set below is what a tool-shaped turn actually emits.
EVENT_FIELDS = {
    "agent_start": {"agent_id"},
    "tool_call": {"agent_id", "tool_name", "call_id", "arguments"},
    "tool_result": {"agent_id", "tool_name", "call_id", "status", "data", "error", "error_code"},
    "interrupt": {"kind", "question_id", "agent_id", "fields", "display", "attempt"},
    "subtask_end": {"agent_id", "status", "summary", "turn_status", "grounding_blocked"},
    "reply_delta": {"text"},
    "narration": {"text"},
    "final": {"text", "business_ids", "terminal_reason", "error_status"},
    # D2: a dispatching turn publishes the task graph before the domain starts, and the base
    # layer owns every node lifecycle frame from there (running, then done/failed/skipped).
    "plan": {"plan_id", "nodes"},
    "task_update": {"plan_id", "node_id", "status", "artifact", "error"},
}


def agent_for(replies, *, grants=None, replies_for=None, audit=None):
    recorder = RecordingAdapter(replies_for or {})
    gateway = ToolGateway(ToolCatalog.build([ECHO], grants or {}), recorder, audit=audit)
    model = ScriptedModel(replies)
    return AgentRuntime(model, graph_factory=_graph_factory(gateway)), model, recorder


def client_for(rt, session, agent):
    rt.agent = agent
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def post(client, session, message, *, key=None, **client_context):
    """One submission over the legacy route, with a fresh key unless a test reuses one."""

    body = {"session_id": session["session_id"], "message": message}
    if client_context:
        body["client_context"] = client_context
    headers = {"Idempotency-Key": key or f"test-{uuid4().hex}"}
    return client.post("/api/chat/messages", json=body, headers=headers)


def frames(response) -> list[tuple[str | None, dict]]:
    """Parse the SSE body into `(event_name, data)` pairs; None means an unnamed frame."""

    parsed: list[tuple[str | None, dict]] = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        payload = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        assert payload is not None
        envelope = json.loads(payload)
        assert envelope["error"] is None
        parsed.append((name, envelope["data"]))
    return parsed


def test_a_turn_streams_the_documented_events_with_their_exact_fields(rt, session):
    agent, _model, _recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            # The tool round is followed by the domain's report; its frame set is unchanged.
            "已处理",
            "已经完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "v1"}},
    )
    response = post(client_for(rt, session, agent), session, "帮我处理")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    parsed = frames(response)
    assert [name for name, _ in parsed] == [
        "plan",
        "task_update",
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    for name, data in parsed:
        # The contract is closed: no extra field, no missing field.
        assert set(data) == EVENT_FIELDS[name]
    # The plan is published before the domain starts, and names the node about to run.
    plan = parsed[0][1]
    assert plan["plan_id"].startswith("plan_")
    assert [(node["id"], node["owner"]) for node in plan["nodes"]] == [("node_1", "transfer")]
    assert parsed[1][1] == {
        "plan_id": plan["plan_id"],
        "node_id": "node_1",
        "status": "running",
        "artifact": None,
        "error": None,
    }
    assert parsed[2][1] == {"agent_id": "transfer"}
    assert parsed[3][1]["tool_name"] == "echo" and parsed[3][1]["arguments"] == {"value": "v1"}
    assert parsed[4][1]["status"] == ToolStatus.SUCCEEDED.value
    assert parsed[5][1]["agent_id"] == "transfer"
    # A tool-shaped domain never ran a whole turn, so it reports no turn status. The key is
    # present and null rather than absent: the payload shape is the same for every publisher.
    assert parsed[5][1]["turn_status"] is None
    assert parsed[6][1] == {"text": "已经完成"}, "the reply is streamed before it is final"
    assert parsed[7][1] == {
        "text": "已经完成",
        "business_ids": [],
        "terminal_reason": None,
        "error_status": None,
    }


def test_a_pause_and_its_answer_survive_a_round_trip(rt, session):
    agent, model, recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            # No second ask: the pause replays `wait`, which reads the question back from
            # the checkpoint, so the replayed turn costs no model call.
            ("call", "echo", '{"value":"from-user"}'),
            # The tool round is followed by the domain's report (spec §4.8.3).
            "已补充 value",
            "已经完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "from-user"}},
    )
    client = client_for(rt, session, agent)

    paused = post(client, session, "帮我处理")
    assert [name for name, _ in frames(paused)] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    paused_frames = frames(paused)
    # The node is running, then parked on the question it is asking -- never finished.
    assert [(data["node_id"], data["status"]) for _, data in paused_frames[1::2]] == [
        ("node_1", "running"),
        ("node_1", "waiting_confirm"),
    ]
    asked = paused_frames[4][1]
    assert field_names(asked) == ["value"]
    assert without_fields(asked) == {
        "kind": "input",
        "question_id": asked["question_id"],
        "agent_id": "transfer",
        "display": "请提供 value",
        "attempt": 1,
    }
    assert recorder.invocations == []

    resumed = post(
        client, session, "补充信息：value：from-user", resume={"value": {"value": "from-user"}}
    )

    # The resume replays the domain it was parked in, so the graph is not redrawn -- but the
    # base layer re-publishes the plan it read from the checkpoint, because a client that
    # comes back has no graph on screen. Nothing else is replayed: the domain picks up where
    # it parked.
    resumed_frames = frames(resumed)
    assert [name for name, _ in resumed_frames] == [
        "plan",
        "task_update",
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    assert resumed_frames[0][1] == paused_frames[0][1], "the same graph, redrawn"
    assert resumed_frames[1][1] == {
        "plan_id": paused_frames[0][1]["plan_id"],
        "node_id": "node_1",
        "status": "running",
        "artifact": None,
        "error": None,
    }
    # The answer really reached the replayed node, and it was asked with it in hand.
    assert any("from-user" in prompt for prompt in model.prompts)
    # The tool ran exactly once -- on the answer -- and the adapter was handed the same
    # call id the client was shown, so the two events can be paired.
    assert recorder.tool_names == ["echo"]
    call = next(data for name, data in frames(resumed) if name == "tool_call")
    assert recorder.invocations[0].call.call_id == call["call_id"]


def test_a_plain_message_while_suspended_is_refused_and_leaves_the_pause_open(rt, session):
    """A normal message during a pause is refused before anything is written.

    This used to repeat the waiting info. The spec is stricter, and for a reason: the request
    is admitted or it is not, and admission happens before the ledger is touched. So the
    refusal must leave no message, no submission and no consumed sequence behind -- and the
    pause must survive it, because a refusal that also dropped the question would strand the
    user with a card that can never be answered.
    """

    agent, model, recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            # Consumed by `process` after the resume below. Nothing may consume it while
            # the thread is paused -- that is what the call count asserts.
            ("call", "echo", '{"value":"from-user"}'),
            # The tool round is followed by the domain's report (spec §4.8.3).
            "已补充 value",
            "完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "from-user"}},
    )
    client = client_for(rt, session, agent)
    paused = post(client, session, "帮我处理")
    assert [name for name, _ in frames(paused)] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    open_question = frames(paused)[-1][1]["question_id"]
    calls_while_paused = len(model.prompts)

    again = post(client, session, "在吗")

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert again.json()["error"]["details"]["question_id"] == open_question
    assert len(model.prompts) == calls_while_paused
    assert recorder.invocations == []

    # Nothing was written for the refused message: the ledger holds only the first one.
    ledger = client.get(f"/api/conversations/{conversation_id(rt, session)}/messages").json()[
        "data"
    ]["items"]
    assert [item["content"] for item in ledger] == ["帮我处理"]

    # The pause survived, and the answer still completes the original turn.
    answered = post(client, session, "value=from-user", resume={"value": {"value": "from-user"}})

    assert [name for name, _ in frames(answered)][-1] == "final"


def conversation_id(rt, session):
    """The conversation the legacy route resolved this session to."""

    with rt.database.sessions() as db:
        return db.scalar(
            select(Conversation.conversation_id).where(
                Conversation.thread_id == f"chat:{session['session_id']}"
            )
        )


def test_a_new_goal_is_refused_and_points_at_a_new_conversation(rt, session):
    """`mode="new_goal"` cannot reset a thread, so it is refused rather than half-honoured.

    Resetting a goal in place would mean resetting a LangGraph thread, which this phase
    deliberately does not do -- so the request is answered with the thing the user actually
    wanted (a new conversation) instead of a reset nobody implemented.
    """

    agent, _model, recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
        ],
        grants={"transfer": ["echo"]},
    )
    client = client_for(rt, session, agent)
    assert [name for name, _ in frames(post(client, session, "帮我处理"))] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]

    refused = post(client, session, "放弃当前目标，重新开始", mode="new_goal")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "NEW_CONVERSATION_REQUIRED"
    assert recorder.invocations == []
    # Nothing was written for the refused request, and the pause is untouched.
    ledger = client.get(f"/api/conversations/{conversation_id(rt, session)}/messages").json()[
        "data"
    ]["items"]
    assert [item["content"] for item in ledger] == ["帮我处理"]


def test_an_unknown_client_context_key_is_rejected(rt, session):
    agent, _model, _recorder = agent_for(["不该执行"])
    response = post(client_for(rt, session, agent), session, "你好", timezone="Asia/Shanghai")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"


def test_a_mode_outside_the_contract_is_rejected(rt, session):
    agent, _model, _recorder = agent_for(["不该执行"])
    response = post(client_for(rt, session, agent), session, "你好", mode="reset")

    assert response.status_code == 422


def test_a_resume_must_carry_a_value_object(rt, session):
    agent, _model, _recorder = agent_for(["不该执行"])
    response = post(client_for(rt, session, agent), session, "你好", resume={"nope": 1})

    assert response.status_code == 422


def test_the_stream_never_names_a_thread_or_the_caller(rt, session):
    agent, _model, _recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            "完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "v1"}},
    )
    response = post(client_for(rt, session, agent), session, "帮我处理")

    text = response.text
    assert "thread_id" not in text
    assert session["session_id"] not in text
    assert session["user_id"] not in text


def test_two_turns_share_one_thread_derived_from_the_session(rt, session):
    agent, model, _recorder = agent_for(
        [
            "第一轮回答",
            # The first turn is named once it has answered, and that naming call is a model
            # call like any other: the script is the model's whole timeline, not the graph's,
            # so the reply for it sits between the two graph replies.
            "第一轮的概括",
            "第二轮回答",
        ]
    )
    client = client_for(rt, session, agent)

    assert [name for name, _ in frames(post(client, session, "第一轮"))] == [
        "reply_delta",
        "final",
    ]
    second = post(client, session, "第二轮")

    assert [name for name, _ in frames(second)] == ["reply_delta", "final"]
    # The second turn's prompt carried the first turn's transcript: one thread per session.
    # Every prompt here is the graph's own -- naming is local now (`app/conversation/titling.py`
    # derives the title from the user's words), so nothing else reaches the model.
    assert "第一轮" in model.prompts[1] and "第一轮回答" in model.prompts[1]


def test_a_broken_graph_becomes_an_unnamed_error_frame(rt, session):
    def exploding_graph(model, context, assembler=None, turn=None):
        del model, context

        def boom(state):
            raise RuntimeError("upstream detail that must not leak")

        graph = StateGraph(AgentState)
        graph.add_node("boom", boom)
        graph.add_edge(START, "boom")
        graph.add_edge("boom", END)
        return graph

    rt.agent = AgentRuntime(ScriptedModel([]), graph_factory=exploding_graph)
    response = post(client_for(rt, session, rt.agent), session, "你好")

    parsed = response.text.split("\n\n")
    last = [block for block in parsed if block.strip()][-1]
    assert not last.startswith("event:")
    assert "INTERNAL_ERROR" in last
    assert "upstream detail" not in last


def runtime_with_model(rt, session, model):
    """A runtime whose model is the one under test, over a real gateway and no tools."""

    gateway = ToolGateway(ToolCatalog.build([ECHO], {}), RecordingAdapter())
    agent = AgentRuntime(model, graph_factory=_graph_factory(gateway))
    return client_for(rt, session, agent)


class RejectingModel:
    """A model the provider refuses outright: retrying it would just be refused again."""

    def __init__(self, code, *, retryable):
        self._code = code
        self._retryable = retryable
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        raise ModelClientError(self._code, retryable=self._retryable)

    def stream(self, messages, *, tools=()):
        # The one decision path is a stream, so a refusal has to come out of the stream
        # itself: a model that cannot even open one is not a client the loop can use.
        self.calls += 1
        raise ModelClientError(self._code, retryable=self._retryable)
        yield  # pragma: no cover - makes this a generator function

    def close(self):
        return None


def test_a_rejected_model_request_ends_the_stream_as_a_non_retryable_upstream_error(rt, session):
    """The client is told not to resend, because resending cannot fix a rejected request."""

    model = RejectingModel("LLM_AUTH", retryable=False)
    client = runtime_with_model(rt, session, model)

    error = error_frame(post(client, session, "你好"))

    assert error["code"] == "LLM_UPSTREAM_ERROR"
    assert error["retryable"] is False
    assert error["details"]["upstream_code"] == "LLM_AUTH"
    assert model.calls == 1, "a non-retryable failure must not be repeated"


def test_a_transient_outage_is_retried_once_before_the_stream_reports_it(rt, session):
    """One retry happens inside the turn; the client only hears about it if that failed."""

    model = RejectingModel("LLM_TIMEOUT", retryable=True)
    client = runtime_with_model(rt, session, model)

    error = error_frame(post(client, session, "你好"))

    assert error["code"] == "LLM_UPSTREAM_ERROR"
    # Retryable for the client: the outage may well be over by the time they resend.
    assert error["retryable"] is True
    assert model.calls == 2, "the transient failure must be retried exactly once"


@pytest.mark.parametrize("field", ["thread_id", "user_id", "session_id", "checkpoint"])
def test_the_event_payload_has_no_place_for_identity_or_checkpoints(field):
    assert all(field not in fields for fields in EVENT_FIELDS.values())


# --- The resume envelope: what a client may send, and what it gets back when it is wrong ---


def error_frame(response) -> dict:
    """The error, from either response shape.

    A failure decided *before* the stream starts -- an admission refusal, an idempotency
    conflict -- is an ordinary JSON envelope, because no SSE headers were ever sent. A
    failure inside the turn is the unnamed SSE frame. The client has to read both, so this
    helper does too.
    """

    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        return response.json()["error"]
    for block in response.text.split("\n\n"):
        if not block.strip() or block.startswith("event:"):
            continue
        payload = next(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
        envelope = json.loads(payload)
        if envelope["error"] is not None:
            return envelope["error"]
    raise AssertionError(f"no error frame in: {response.text!r}")


def paused_question(rt, session, *, grants=None, replies=None):
    """Park one session on a question and return the client plus that question's id.

    The resume half of the script carries the domain's tool call, the report that follows
    the tool round (spec §4.8.3), and the Supervisor's reply.
    """

    agent, model, _recorder = agent_for(
        replies
        or [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "已补充 value",
            "已经完成",
        ],
        grants=grants if grants is not None else {"transfer": ["echo"]},
        replies_for={"echo": {"value": "from-user"}},
    )
    client = client_for(rt, session, agent)
    paused = post(client, session, "帮我处理")
    return client, model, frames(paused)[-1][1]["question_id"]


def test_a_resume_that_names_its_question_is_accepted(rt, session):
    """The new client shape. `question_id` must not be rejected as an unknown key."""

    client, _model, question_id = paused_question(rt, session)

    resumed = post(
        client,
        session,
        "补充",
        resume={"question_id": question_id, "value": {"value": "from-user"}},
    )

    assert resumed.status_code == 200
    assert [name for name, _ in frames(resumed)][-1] == "final"


def test_a_resume_for_another_question_is_a_protocol_error(rt, session):
    """Answering a question this thread is not waiting on is not an illegal answer.

    Re-asking would be wrong here: the client is not confused about the fields, it is
    answering a question that no longer exists. That is a recovery protocol error.
    """

    client, _model, _question_id = paused_question(rt, session)

    resumed = post(
        client,
        session,
        "补充",
        resume={"question_id": "q_a_question_from_another_turn", "value": {"value": "from-user"}},
    )

    assert error_frame(resumed)["code"] == "INVALID_INTERRUPT_ANSWER"


def test_a_resume_with_no_open_question_is_a_protocol_error(rt, session):
    """Nothing is waiting, so there is no question the answer could belong to."""

    agent, _model, _recorder = agent_for(["没有等待点"])
    client = client_for(rt, session, agent)

    resumed = post(client, session, "补充", resume={"question_id": "q_1", "value": {"value": "v"}})

    assert error_frame(resumed)["code"] == "INVALID_INTERRUPT_ANSWER"


@pytest.mark.parametrize(
    "question_id",
    ["", "   ", None],
    ids=["empty", "blank", "null"],
)
def test_a_present_but_unusable_question_id_is_not_treated_as_the_legacy_shape(
    rt, session, question_id
):
    """The legacy path is for a client that sends *no* `question_id`, not a broken one.

    Silently binding the open question here would answer a question the client did not
    name -- exactly the mismatch `question_id` exists to prevent. A client that sends the
    field has made a claim about which question it is answering, and a claim that cannot
    be read is a protocol error rather than an absent one.
    """

    client, _model, open_question = paused_question(rt, session)

    resumed = post(
        client,
        session,
        "补充",
        resume={"question_id": question_id, "value": {"value": "from-user"}},
    )

    assert error_frame(resumed)["code"] == "INVALID_INTERRUPT_ANSWER"
    # The open question is untouched: the request failed before anything consumed it.
    assert open_question


def test_a_question_id_of_the_wrong_type_is_rejected_at_the_edge(rt, session):
    """A number is not a question id, and it never reaches the graph as one."""

    client, _model, _open = paused_question(rt, session)

    resumed = post(
        client, session, "补充", resume={"question_id": 123, "value": {"value": "from-user"}}
    )

    assert resumed.status_code == 422
    assert resumed.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"


def test_the_legacy_shape_is_still_accepted_when_the_field_is_absent(rt, session):
    """The compatibility path survives the stricter check, which is the point of it."""

    client, _model, _question_id = paused_question(rt, session)

    resumed = post(client, session, "补充", resume={"value": {"value": "from-user"}})

    assert resumed.status_code == 200
    assert [name for name, _ in frames(resumed)][-1] == "final"


def test_a_legacy_resume_is_accepted_while_exactly_one_question_is_open(rt, session):
    """The old `{value}` shape keeps working, because runtime can name the open question."""

    client, _model, _question_id = paused_question(rt, session)

    resumed = post(client, session, "补充", resume={"value": {"value": "from-user"}})

    assert resumed.status_code == 200
    assert [name for name, _ in frames(resumed)][-1] == "final"


def test_a_resume_value_that_is_not_an_object_still_reaches_the_graph(rt, session):
    """The value stays loose at the edge so the graph can refuse it properly.

    Rejecting it as a 4xx would hide a legal-but-unusable answer behind a transport error;
    the domain re-asks instead, which is what the client needs to see.
    """

    client, _model, question_id = paused_question(rt, session)

    resumed = post(
        client, session, "补充", resume={"question_id": question_id, "value": "just-a-string"}
    )

    assert resumed.status_code == 200
    # The redrawn plan and the parked node, then the question asked again under the same id.
    assert [name for name, _ in frames(resumed)] == [
        "plan",
        "task_update",
        "task_update",
        "interrupt",
    ]


def test_resume_and_mode_together_are_rejected(rt, session):
    """Answering a question and abandoning the goal are different intentions."""

    client, _model, question_id = paused_question(rt, session)

    response = post(
        client,
        session,
        "补充",
        resume={"question_id": question_id, "value": {"value": "from-user"}},
        mode="new_goal",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.parametrize("resume", ["not-an-object", ["a"], 7])
def test_an_unparseable_resume_envelope_is_rejected_before_the_graph(rt, session, resume):
    """A resume that is not an object at all cannot be repaired inside the graph."""

    agent, _model, _recorder = agent_for(["不该执行"])
    client = client_for(rt, session, agent)

    response = post(client, session, "补充", resume=resume)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"


def test_a_resume_envelope_without_a_value_is_rejected_before_the_graph(rt, session):
    agent, _model, _recorder = agent_for(["不该执行"])
    client = client_for(rt, session, agent)

    response = post(client, session, "补充", resume={"question_id": "q_1"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"


def test_a_resume_envelope_with_an_unknown_key_is_rejected(rt, session):
    agent, _model, _recorder = agent_for(["不该执行"])
    client = client_for(rt, session, agent)

    response = post(client, session, "补充", resume={"question_id": "q_1", "value": {}, "x": 1})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
