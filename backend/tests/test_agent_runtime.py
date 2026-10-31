"""Session runtime: one thread per session, serialized invocations, no identity leak.

Task 1 drives a minimal append-only graph so the checkpointer wiring and the
per-session lock can be proven without the Supervisor. The graph is a test fixture,
not a second runtime path.
"""

import asyncio
import threading

import pytest
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.runtime import AgentEvent, AgentRuntime, thread_id_for_session
from app.agents.state import AgentState, message_dicts, new_agent_state
from app.core.errors import AppError
from app.integration.model_client import ModelReply, UnconfiguredModelClient
from app.tools.protocol import TrustedContext
from tests.support.scripted_model import ScriptedModel

SESSION = "s1"
USER = "u1"


class RecordingModel:
    """Answers with fixed text but records every prompt it was given."""

    def __init__(self, *, reply: str = "好的") -> None:
        self.reply = reply
        self.conversations: list[tuple] = []

    def complete(self, messages: tuple) -> ModelReply:
        self.conversations.append(messages)
        return ModelReply(text=self.reply)

    def close(self) -> None:
        return None


def echo_graph(model, context, assembler=None, turn=None) -> StateGraph:
    """The minimal graph: one user turn in, one assistant turn out.

    It publishes its reply through the custom stream, which is the only way a node
    reaches the client: the runtime never reads state to build an event. `context` is
    the caller's trusted identity, which this fixture has no use for.
    """

    del context

    def respond(state) -> dict:
        writer = get_stream_writer()
        reply = model.complete(tuple(state["messages"]))
        writer({"event": "final", "text": reply.text, "business_ids": []})
        return {"messages": [{"role": "assistant", "content": reply.text}]}

    graph = StateGraph(AgentState)
    graph.add_node("echo", respond)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph


def make_runtime(model=None) -> AgentRuntime:
    return AgentRuntime(model or RecordingModel(), graph_factory=echo_graph)


async def collect(events) -> list[AgentEvent]:
    return [event async for event in events]


def contents(messages: tuple) -> list:
    return [getattr(item, "content", None) for item in messages]


def test_thread_id_is_derived_from_the_session():
    assert thread_id_for_session("sess_abc") == "chat:sess_abc"


def test_a_new_state_carries_only_the_new_user_turn():
    assert new_agent_state()["messages"] == []
    assert new_agent_state("你好")["messages"] == [{"role": "user", "content": "你好"}]


async def test_two_turns_share_one_thread_and_read_the_first_turn():
    model = RecordingModel(reply="第一轮")
    runtime = make_runtime(model)

    first = await collect(runtime.invoke(SESSION, USER, "你好", {}))
    second = await collect(runtime.invoke(SESSION, USER, "再见", {}))

    assert [event.event for event in first] == ["final"]
    assert first[0].data["text"] == "第一轮"
    assert second[0].data["text"] == "第一轮"
    assert contents(model.conversations[0]) == ["你好"]
    # The second turn resumed the same thread: the earlier turns are still there.
    assert contents(model.conversations[1]) == ["你好", "第一轮", "再见"]


async def test_a_different_session_does_not_see_the_first_thread():
    model = RecordingModel()
    runtime = make_runtime(model)

    await collect(runtime.invoke("s1", USER, "你好", {}))
    await collect(runtime.invoke("s2", USER, "你好", {}))

    assert contents(model.conversations[1]) == ["你好"]


async def test_events_never_carry_thread_or_caller_identity():
    runtime = make_runtime()

    events = await collect(runtime.invoke(SESSION, USER, "你好", {}))

    assert events
    for event in events:
        assert event.event in {
            "agent_start",
            "tool_call",
            "tool_result",
            "interrupt",
            "subtask_end",
            "final",
        }
        assert "thread_id" not in event.data
        assert "session_id" not in event.data
        assert "user_id" not in event.data


async def test_a_synchronous_model_call_never_runs_on_the_event_loop():
    """Why a blocking model client is safe here, stated as an enforced invariant.

    The Chat Completions client is synchronous, and a blocking call made *on the loop*
    would stall every other request in the worker. It is safe because a synchronous
    LangGraph node is executed in an executor thread, so the loop stays free. If a node
    is ever changed to `async def` while the client stays synchronous, the call lands on
    the loop and this test fails -- which is exactly when the client must become async.
    """

    loop_thread = threading.current_thread().name
    called_on: list[str] = []

    class ThreadRecordingModel(RecordingModel):
        def complete(self, messages: tuple) -> ModelReply:
            called_on.append(threading.current_thread().name)
            return super().complete(messages)

    runtime = AgentRuntime(ThreadRecordingModel(), graph_factory=echo_graph)

    await collect(runtime.invoke(SESSION, USER, "你好", {}))

    assert len(called_on) == 1
    assert called_on[0] != loop_thread


async def test_a_concurrent_turn_waits_for_the_session_lock():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[int] = []

    async def slow_node(state) -> dict:
        seen.append(len(seen))
        if len(seen) == 1:
            entered.set()
            # Hold the lock until the test says the second request has arrived.
            await release.wait()
        return {}

    def blocking_graph(model, context, assembler=None, turn=None):
        del context
        graph = StateGraph(dict)
        graph.add_node("slow", slow_node)
        graph.add_edge(START, "slow")
        graph.add_edge("slow", END)
        return graph

    runtime = AgentRuntime(RecordingModel(), graph_factory=blocking_graph)

    first = asyncio.create_task(collect(runtime.invoke("s1", USER, "一", {})))
    await entered.wait()
    second = asyncio.create_task(collect(runtime.invoke("s1", USER, "二", {})))
    await asyncio.sleep(0.05)

    # The second request is parked on the lock: the node has run exactly once.
    assert len(seen) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(seen) == 2


async def test_a_second_session_is_not_blocked_by_the_first():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_node(state) -> dict:
        seen.append("run")
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return {}

    def blocking_graph(model, context, assembler=None, turn=None):
        del context
        graph = StateGraph(dict)
        graph.add_node("slow", slow_node)
        graph.add_edge(START, "slow")
        graph.add_edge("slow", END)
        return graph

    runtime = AgentRuntime(RecordingModel(), graph_factory=blocking_graph)

    first = asyncio.create_task(collect(runtime.invoke("s1", USER, "一", {})))
    await entered.wait()
    second = asyncio.create_task(collect(runtime.invoke("s2", USER, "二", {})))
    await asyncio.sleep(0.05)

    # A different session has its own lock, so it does not queue behind s1.
    assert len(seen) == 2
    release.set()
    await asyncio.gather(first, second)


def test_an_unconfigured_model_fails_loudly_instead_of_routing():
    runtime = AgentRuntime(UnconfiguredModelClient(), graph_factory=echo_graph)

    async def run():
        async for _ in runtime.invoke(SESSION, USER, "你好", {}):
            pass

    with pytest.raises(AppError) as failure:
        asyncio.run(run())
    assert failure.value.code == "MODEL_NOT_CONFIGURED"
    assert failure.value.status == 503


def test_the_runtime_builds_no_manual_checkpoint_namespace():
    import app.agents.runtime as runtime_module

    with open(runtime_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "checkpoint_ns=" not in source
    assert "subtask_id" not in source


# --- new_goal: abandoning a goal has to abandon what the goal left behind ---

# What makes the fixture graph park: a turn whose message says it needs more input.
PAUSE_MARKER = "需要补充"
OLD_MESSAGE = "旧目标留下的消息"


def reset_goal_node(state):
    """The anchor the runtime attributes a goal reset to; no side effects."""

    del state
    return {}


def goal_graph(model, context, assembler=None, turn=None):
    """Parks on a native interrupt when asked to, otherwise answers through the model."""

    del context

    def work(state):
        writer = get_stream_writer()
        turns = message_dicts(state.get("messages"))
        if turns and PAUSE_MARKER in turns[-1]["content"]:
            interrupt(
                {
                    "kind": "input",
                    "question_id": "q_fixture",
                    "agent_id": "fixture",
                    "fields": ["x"],
                    "display": "请提供 x",
                    "attempt": 1,
                }
            )
            return {}
        reply = model.complete(tuple(state["messages"]))
        writer({"event": "final", "text": reply.text, "business_ids": []})
        return {"messages": [{"role": "assistant", "content": reply.text}]}

    graph = StateGraph(AgentState)
    graph.add_node("reset_goal", reset_goal_node)
    graph.add_node("work", work)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    graph.add_edge("reset_goal", "work")
    return graph


def goal_runtime(model=None) -> AgentRuntime:
    return AgentRuntime(model or RecordingModel(), graph_factory=goal_graph)


def thread_config() -> dict:
    return {"configurable": {"thread_id": thread_id_for_session(SESSION)}}


def graph_of(runtime: AgentRuntime):
    """The compiled graph this runtime drives, for reading the thread it wrote to."""

    return runtime._graph_for(TrustedContext(user_id=USER, session_id=SESSION))


async def seed_thread(runtime: AgentRuntime, **values) -> None:
    """Write state straight into the thread, standing in for an older checkpoint."""

    await graph_of(runtime).aupdate_state(thread_config(), values, as_node="reset_goal")


async def read_thread(runtime: AgentRuntime):
    return await graph_of(runtime).aget_state(thread_config())


def transcript_of(model: RecordingModel) -> str:
    return "\n".join(str(getattr(item, "content", "")) for item in model.conversations[-1])


def test_a_new_goal_abandons_a_pause_nobody_answered():
    """New input alone does not consume a pending interrupt -- it has to be replaced."""

    model = RecordingModel()
    runtime = goal_runtime(model)

    paused = asyncio.run(collect(runtime.invoke(SESSION, USER, f"{PAUSE_MARKER}信息", {})))
    assert [event.event for event in paused] == ["interrupt"]

    restarted = asyncio.run(
        collect(runtime.invoke(SESSION, USER, "重新开始", {"mode": "new_goal"}))
    )
    assert [event.event for event in restarted] == ["final"]

    # The pause is really gone. Had it survived, this ordinary message would be answered
    # with the interrupt again instead of running a turn.
    after = asyncio.run(collect(runtime.invoke(SESSION, USER, "普通消息", {})))
    assert [event.event for event in after] == ["final"]
    assert PAUSE_MARKER not in transcript_of(model)


@pytest.mark.parametrize(
    "values",
    [
        {"goal": "旧目标", "turns": 2, "active_agent": "transfer", "turn_phase": "decide"},
        {"goal": "旧目标", "turns": 0, "turn_phase": "process_answer"},
        {"goal": "旧目标", "turns": 1},
    ],
    ids=["in-progress", "mid-answer", "no-turn-phase"],
)
def test_a_new_goal_clears_what_the_old_goal_left_behind(values):
    model = RecordingModel()
    runtime = goal_runtime(model)

    async def scenario():
        await seed_thread(runtime, messages=[{"role": "user", "content": OLD_MESSAGE}], **values)
        events = await collect(runtime.invoke(SESSION, USER, "新目标", {"mode": "new_goal"}))
        return events, await read_thread(runtime)

    events, state = asyncio.run(scenario())

    assert [event.event for event in events] == ["final"]
    # The abandoned transcript is gone, and the new turn is the only thing the model saw.
    transcript = transcript_of(model)
    assert OLD_MESSAGE not in transcript
    assert "新目标" in transcript

    assert state.values["messages"][0].content == "新目标"
    assert state.values["goal"] is None
    assert state.values["turns"] == 0
    assert state.values["active_agent"] is None
    assert state.values["domain_results"] == []
    # No assertion on the waiting channels: a parked question lives in the domain
    # subgraph's checkpoint namespace, so the parent's copies are idle either way and
    # writing them would prove nothing. What abandons a pause is the replaced task, and
    # `test_a_new_goal_abandons_a_pause_nobody_answered` is what covers that.


def test_an_ordinary_message_keeps_the_transcript_a_new_goal_drops():
    """The difference the reset makes, so the test above is not passing by accident."""

    model = RecordingModel()
    runtime = goal_runtime(model)

    async def scenario():
        await seed_thread(runtime, messages=[{"role": "user", "content": OLD_MESSAGE}])
        return await collect(runtime.invoke(SESSION, USER, "下一轮", {}))

    events = asyncio.run(scenario())

    assert [event.event for event in events] == ["final"]
    # An ordinary turn extends the conversation; only a new goal replaces it.
    transcript = transcript_of(model)
    assert OLD_MESSAGE in transcript
    assert "下一轮" in transcript


# --- the caller's bank customer: resolved once per turn, or absent ---


class StubGraph:
    """A compiled graph that publishes nothing: these tests are about the context.

    It answers `compile()` because the runtime compiles whatever a factory hands it, and
    `astream()` is an async generator rather than a coroutine -- an `async def` without a
    `yield` would be awaited instead of iterated.
    """

    def compile(self, **kwargs):
        return self

    async def astream(self, graph_input, config=None, **kwargs):
        if False:  # makes this an async generator rather than a coroutine
            yield None


def factory_capturing(contexts):
    def factory(model, context, assembler=None, turn=None):
        contexts.append(context)
        return StubGraph()

    return factory


def test_the_runtime_resolves_the_bank_customer_once_per_turn():
    seen: list[str] = []
    contexts: list[TrustedContext] = []

    def resolver(user_id: str) -> str | None:
        seen.append(user_id)
        return "u_zhang"

    runtime = AgentRuntime(
        model=ScriptedModel([]),
        graph_factory=factory_capturing(contexts),
        customer_resolver=resolver,
    )

    async def run():
        return [event async for event in runtime.invoke("sess_1", "user_0001", "你好")]

    assert asyncio.run(run()) == []
    assert seen == ["user_0001"]
    assert contexts[0].bank_customer_id == "u_zhang"


def test_a_resolver_that_fails_leaves_the_binding_absent_rather_than_guessing():
    contexts: list[TrustedContext] = []

    def resolver(user_id: str) -> str | None:
        raise RuntimeError("database is down")

    runtime = AgentRuntime(
        model=ScriptedModel([]),
        graph_factory=factory_capturing(contexts),
        customer_resolver=resolver,
    )

    async def run():
        return [event async for event in runtime.invoke("sess_1", "user_0001", "你好")]

    assert asyncio.run(run()) == []
    # The direction is fail closed: no binding refuses a bank call later, rather than
    # letting the bank answer for its own default customer.
    assert contexts[0].bank_customer_id is None


def test_a_runtime_without_a_resolver_binds_nothing():
    contexts: list[TrustedContext] = []
    runtime = AgentRuntime(model=ScriptedModel([]), graph_factory=factory_capturing(contexts))

    async def run():
        return [event async for event in runtime.invoke("sess_1", "user_0001", "你好")]

    assert asyncio.run(run()) == []
    assert contexts[0].bank_customer_id is None
