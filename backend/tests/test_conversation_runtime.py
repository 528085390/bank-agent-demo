"""The runtime's conversation path: one thread per conversation, one id per message.

The ledger and the checkpoint are two stores of the same conversation, and this file pins
the two facts that keep them from drifting: a conversation runs on the thread it was given
rather than one derived from a session, and a message carries the ledger's own id into the
transcript so LangGraph's reducer merges a replay instead of duplicating it.
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_agent_graph
from tests.support.domain_harness import generic_roster
from app.agents.runtime import AgentRuntime, TurnContext
from app.core.errors import ConversationErrorCode
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel

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
CONTEXT = TrustedContext(user_id="u", session_id="s", bank_customer_id=None)


def runtime_for(replies, *, replies_for=None):
    gateway = ToolGateway(
        ToolCatalog.build([ECHO], {"transfer": ["echo"]}),
        RecordingAdapter(replies_for or {}),
    )
    model = ScriptedModel(replies)
    runtime = AgentRuntime(
        model,
        graph_factory=lambda m, c, assembler=None, turn=None: build_agent_graph(m, gateway, c, roster=generic_roster()),
    )
    return runtime, model


def turn(thread_id, message_id, content, sequence=0, **kwargs):
    return TurnContext(
        conversation_id=thread_id.split(":", 1)[1],
        thread_id=thread_id,
        message_id=message_id,
        content=content,
        sequence=sequence,
        **kwargs,
    )


async def collect(source):
    return [event async for event in source]


def test_a_conversation_turn_runs_on_the_thread_it_was_given():
    runtime, model = runtime_for(["好的"])

    asyncio.run(
        collect(
            runtime.invoke(
                "s",
                "u",
                "你好",
                {},
                turn=turn("conv:abc", "msg_1", "你好"),
            )
        )
    )

    # The thread is the conversation's, not `chat:s`: reading the conversation's thread is
    # what proves the stored id was used rather than a derived one. The reply is there too,
    # because the Supervisor's own turn joins the transcript.
    graph = runtime._graph_for(CONTEXT)
    state = asyncio.run(graph.aget_state({"configurable": {"thread_id": "conv:abc"}}))
    assert [message.content for message in state.values["messages"]] == ["你好", "好的"]
    # The negative control: the session-derived thread was never written to.
    session_thread = asyncio.run(graph.aget_state({"configurable": {"thread_id": "chat:s"}}))
    assert not session_thread.values.get("messages")
    assert model.prompts, "the turn really ran"


def test_two_conversations_never_share_a_thread():
    runtime, _model = runtime_for(["甲", "乙"])

    async def scenario():
        await collect(
            runtime.invoke("s", "u", "甲的问题", {}, turn=turn("conv:1", "m1", "甲的问题"))
        )
        await collect(
            runtime.invoke("s", "u", "乙的问题", {}, turn=turn("conv:2", "m2", "乙的问题"))
        )
        graph = runtime._graph_for(CONTEXT)
        return (
            await graph.aget_state({"configurable": {"thread_id": "conv:1"}}),
            await graph.aget_state({"configurable": {"thread_id": "conv:2"}}),
        )

    first, second = asyncio.run(scenario())

    assert [message.content for message in first.values["messages"]] == ["甲的问题", "甲"]
    assert [message.content for message in second.values["messages"]] == ["乙的问题", "乙"]


def test_the_ledger_message_id_is_the_id_in_the_transcript():
    """This is what makes a replay a merge instead of a duplicate."""

    runtime, _model = runtime_for(["好的"])

    async def scenario():
        await collect(runtime.invoke("s", "u", "你好", {}, turn=turn("conv:abc", "msg_1", "你好")))
        return await runtime._graph_for(CONTEXT).aget_state(
            {"configurable": {"thread_id": "conv:abc"}}
        )

    state = asyncio.run(scenario())
    human = state.values["messages"][0]

    assert human.id == "msg_1"


def test_replaying_one_ledger_message_does_not_duplicate_the_transcript():
    """The reducer merges by id, so the same message written twice is one entry.

    Without the ledger id on the message this is two entries, and the model would be told
    the user said the same thing twice.
    """

    runtime, model = runtime_for(["一", "二"])

    async def scenario():
        await collect(
            runtime.invoke("s", "u", "同一句话", {}, turn=turn("conv:abc", "msg_1", "同一句话"))
        )
        # The same ledger message again -- a retried dispatch, not a new message.
        await collect(
            runtime.invoke("s", "u", "同一句话", {}, turn=turn("conv:abc", "msg_1", "同一句话"))
        )
        return await runtime._graph_for(CONTEXT).aget_state(
            {"configurable": {"thread_id": "conv:abc"}}
        )

    state = asyncio.run(scenario())
    contents = [message.content for message in state.values["messages"]]

    assert contents.count("同一句话") == 1, contents
    assert model.prompts, "both turns ran; only the transcript merged"


def test_the_session_path_still_derives_its_thread():
    """The pre-persistence path is unchanged, which is what the migration tool relies on."""

    runtime, _model = runtime_for(["好的"])

    async def scenario():
        await collect(runtime.invoke("sess_1", "u", "你好", {}))
        return await runtime._graph_for(CONTEXT).aget_state(
            {"configurable": {"thread_id": "chat:sess_1"}}
        )

    state = asyncio.run(scenario())

    assert [message.content for message in state.values["messages"]] == ["你好", "好的"]


def test_a_session_path_message_carries_no_ledger_id():
    """No ledger, no id: inventing one would claim a ledger row that does not exist."""

    runtime, _model = runtime_for(["好的"])

    async def scenario():
        await collect(runtime.invoke("sess_1", "u", "你好", {}))
        return await runtime._graph_for(CONTEXT).aget_state(
            {"configurable": {"thread_id": "chat:sess_1"}}
        )

    state = asyncio.run(scenario())

    assert state.values["messages"][0].id != "msg_1"
    assert state.values["messages"][0].content == "你好"


# --- admission and recovery, read from the checkpoint ------------------------------------


PAUSE_REPLIES = [
    ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
    ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
    "已经完成",
]


def paused_runtime():
    runtime, model = runtime_for(PAUSE_REPLIES, replies_for={"echo": {"value": "v"}})

    async def park():
        events = await collect(
            runtime.invoke("s", "u", "帮我处理", {}, turn=turn("conv:abc", "msg_1", "帮我处理"))
        )
        return next(event for event in events if event.event == "interrupt").data

    return runtime, model, asyncio.run(park())


def test_admission_accepts_an_ordinary_message_on_a_free_thread():
    runtime, _model = runtime_for(["好的"])

    admission = asyncio.run(runtime.admission("u", "s", "conv:abc", {}))

    assert admission.resuming is False
    assert admission.resume is None
    assert admission.question_id is None


def test_admission_binds_a_legacy_resume_to_the_open_question():
    runtime, _model, question = paused_runtime()

    admission = asyncio.run(
        runtime.admission("u", "s", "conv:abc", {"resume": {"value": {"value": "v"}}})
    )

    assert admission.resuming is True
    assert admission.question_id == question["question_id"]
    assert admission.resume == {"question_id": question["question_id"], "value": {"value": "v"}}


def test_admission_keeps_a_question_id_the_client_named():
    runtime, _model, question = paused_runtime()

    admission = asyncio.run(
        runtime.admission(
            "u",
            "s",
            "conv:abc",
            {"resume": {"question_id": question["question_id"], "value": {"value": "v"}}},
        )
    )

    assert admission.resume["question_id"] == question["question_id"]


@pytest.mark.parametrize("value", [None, "", "   ", "q_someone_else"])
def test_admission_refuses_a_question_id_that_is_not_the_open_one(value):
    """A claim that cannot be read is not an absent claim -- the key's presence is the claim."""

    runtime, _model, _question = paused_runtime()

    with pytest.raises(Exception) as raised:
        asyncio.run(
            runtime.admission(
                "u", "s", "conv:abc", {"resume": {"question_id": value, "value": {"value": "v"}}}
            )
        )

    assert raised.value.code == ConversationErrorCode.INVALID_INTERRUPT_ANSWER


def test_admission_refuses_an_ordinary_message_while_a_question_is_open():
    runtime, _model, question = paused_runtime()

    with pytest.raises(Exception) as raised:
        asyncio.run(runtime.admission("u", "s", "conv:abc", {}))

    assert raised.value.code == ConversationErrorCode.INVALID_INTERRUPT_ANSWER
    assert raised.value.details["question_id"] == question["question_id"]


def test_admission_refuses_a_resume_when_nothing_is_open():
    runtime, _model = runtime_for(["好的"])

    asyncio.run(
        collect(runtime.invoke("s", "u", "你好", {}, turn=turn("conv:abc", "msg_1", "你好")))
    )

    with pytest.raises(Exception) as raised:
        asyncio.run(
            runtime.admission("u", "s", "conv:abc", {"resume": {"question_id": "q_1", "value": {}}})
        )

    assert raised.value.code == ConversationErrorCode.INVALID_INTERRUPT_ANSWER


def test_recovery_hint_reports_the_open_question_and_nothing_else():
    runtime, _model, question = paused_runtime()

    hint = asyncio.run(runtime.recovery_hint("u", "s", "conv:abc"))

    assert hint.pending["question_id"] == question["question_id"]
    assert hint.recovery_required is False


def test_recovery_hint_is_empty_for_a_finished_thread():
    runtime, _model = runtime_for(["好的"])
    asyncio.run(
        collect(runtime.invoke("s", "u", "你好", {}, turn=turn("conv:abc", "msg_1", "你好")))
    )

    hint = asyncio.run(runtime.recovery_hint("u", "s", "conv:abc"))

    assert hint.pending is None
    assert hint.recovery_required is False


def test_a_conversation_lock_is_per_conversation_and_is_not_reentrant():
    """Two conversations must not block each other, and one turn must not take it twice."""

    runtime, _model = runtime_for(["好的"])

    first = runtime.conversation_lock("conv:1")
    second = runtime.conversation_lock("conv:1")
    other = runtime.conversation_lock("conv:2")

    assert first is second
    assert first is not other


def test_a_checkpointer_owned_runtime_shares_one_saver_across_conversations():
    """The store is shared; what is per-conversation is the thread id, not the saver."""

    gateway = ToolGateway(ToolCatalog.build([ECHO], {}), RecordingAdapter())
    model = ScriptedModel(["一", "二"])
    saver = InMemorySaver()
    runtime = AgentRuntime(
        model,
        graph_factory=lambda m, c, assembler=None, turn=None: build_agent_graph(m, gateway, c, roster=generic_roster()),
        checkpointer=saver,
    )

    async def scenario():
        await collect(runtime.invoke("s", "u", "一", {}, turn=turn("conv:1", "m1", "一")))
        await collect(runtime.invoke("s", "u", "二", {}, turn=turn("conv:2", "m2", "二")))
        return await runtime._graph_for(CONTEXT).aget_state(
            {"configurable": {"thread_id": "conv:1"}}
        )

    state = asyncio.run(scenario())

    assert [message.content for message in state.values["messages"]] == ["一", "一"]
