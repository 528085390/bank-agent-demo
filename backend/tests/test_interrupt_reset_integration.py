"""The LangGraph reset sequence the new-goal path depends on.

Task 1 of `docs/superpowers/plans/2026-09-22-deterministic-interrupt-resume.md`, which makes
this a gate: before the deterministic three-node flow is built on top of it, prove on the
pinned LangGraph version that `aupdate_state(..., as_node="reset_goal")` abandons a pending
native interrupt and clears the real `AgentState` channels, and that the next `astream` still
delivers a complete event stream through the same `_adapt` adapter the API uses.

It runs the production graph, the production state and the production adapter, so what it
proves is the real sequence rather than a toy one. The only thing it adds is the
`reset_goal` node itself -- exactly the two lines Task 4 adds inside `build_supervisor`, and
it adds them only while they are still missing, so the test keeps passing once they land.

The three cases below are deliberately adjacent, because the middle one is a trap:

- with the reset, the pause is gone and the new goal starts from a clean state;
- with a bare `[]` instead of `Overwrite([])`, the stale results survive;
- with no reset at all, the event stream *still looks correct* (`final` arrives), while the
  old interrupt stays pending and the old transcript and results leak into the new goal.

That last case is why the reset exists, and why an assertion on the event stream alone
would not have caught any of this.
"""

import asyncio

import pytest
from langgraph.types import Overwrite

from app.agents.domain import build_agent_graph
from tests.support.domain_harness import generic_roster
from app.agents.runtime import AgentRuntime, thread_id_for_session
from app.core.errors import AppError
from app.storage.checkpointer import LockedAsyncPostgresSaver
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.conftest import resolve_test_database_url
from tests.support.domain_harness import field_names
from tests.support.scripted_model import ScriptedModel

pytestmark = pytest.mark.skipif(
    resolve_test_database_url() is None,
    reason="Provide database.txt or set TEST_DATABASE_URL to run these integration tests",
)

SESSION = "s_reset"
USER = "u_1"
CONTEXT = TrustedContext(user_id=USER, session_id=SESSION)

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


def pausable_gateway() -> ToolGateway:
    """A gateway with one tool granted to `bill` and to `transfer`.

    The domains this graph drives carry no tool in this test's gateway, and a domain with an
    empty menu no longer asks at all -- it states the capability boundary instead, whatever
    its model replied. Reaching a native pause therefore needs a tool in the menu, which is
    what this grants to `bill`; `transfer`
    carries the same tool so its scripted report is recorded verbatim as the stale result
    these tests track through the reset, rather than being replaced by the boundary notice.
    """

    return ToolGateway(
        ToolCatalog.build([ECHO], {"bill": ["echo"], "transfer": ["echo"]}),
        RecordingAdapter({"echo": {"value": "v"}}),
    )


# The two facts the reset has to erase: a domain result, and the message that produced it.
STALE_RESULT = "转账领域结论A"
STALE_MESSAGE = "查一下账单"

# One script for both phases: the turn that parks, then the new goal.
#
# The parking turn routes to two domains on purpose. A fresh turn always resets
# `domain_results` (`new_agent_state` writes `Overwrite([])`), so a result can only be
# stale *within* a turn -- one domain has to conclude before the next one asks. That is
# exactly the state a new goal must not inherit.
SCRIPT = [
    ("call", "handoff", '{"agent_id":"transfer","instruction":"处理转账"}'),
    STALE_RESULT,
    ("call", "handoff", '{"agent_id":"bill","instruction":"处理账单"}'),
    ("call", "ask_user", '{"fields":["account_id"],"display":"请提供账号"}'),
    "新目标已完成",
]

# What the reset writes. `overwrite` is the sequence Task 4 must use; the other two are
# the near-misses the assertions below have to be able to tell apart.
RESET_MODES = ("overwrite", "bare_list", "none")

# The new goal routes back into the SAME domain that parked, and asks a different
# question. A subgraph node gets a fresh checkpoint namespace per invocation, so the
# abandoned question cannot resurface -- this is the case the reset really protects, and
# the one an "the event stream looks fine" assertion would miss.
REENTRY_SCRIPT = [
    ("call", "handoff", '{"agent_id":"bill","instruction":"处理账单"}'),
    ("call", "ask_user", '{"fields":["account_id"],"display":"请提供账号"}'),
    ("call", "handoff", '{"agent_id":"bill","instruction":"再次处理账单"}'),
    ("call", "ask_user", '{"fields":["period"],"display":"请提供周期"}'),
    "结束",
]


class SupervisorScriptedModel(ScriptedModel):
    """The shared scripted model, plus the Supervisor-only prompt views this file reads.

    What the reset protects is what the *Supervisor* is shown, so these tests have to pick
    the Supervisor's turns out of every prompt the script recorded. The filter lives here
    rather than in the shared model because "总调度" is this file's subject, not the model's.
    """

    def supervisor_prompts(self) -> list[str]:
        """Only the Supervisor's turns; a domain prompt carries a different preamble."""

        return [prompt for prompt in self.prompts if "总调度" in prompt]

    def last_supervisor_prompt(self) -> str:
        return self.supervisor_prompts()[-1]


def production_graph(model, gateway, context):
    """The production graph, which owns the `reset_goal` anchor the reset writes through."""

    graph = build_agent_graph(model, gateway, context, roster=generic_roster())
    # Asserted rather than added: if the anchor ever disappears, this test must fail
    # instead of quietly re-creating it and reporting a healthy reset.
    assert "reset_goal" in graph.nodes, "the goal reset needs its anchor in the graph"
    return graph


async def collect_stream(graph, config, graph_input):
    """Drive `astream` the way `AgentRuntime.invoke` does, through the same adapter."""

    events = []
    seen: set = set()
    async for chunk in graph.astream(
        graph_input,
        config,
        stream_mode=["updates", "custom"],
        subgraphs=True,
        version="v2",
    ):
        events.extend(AgentRuntime._adapt(chunk, seen))
    return events


async def run_reset(schema_url: str, *, mode: str, script: list[str] | None = None):
    """Park a thread on a native interrupt, reset it, then start a new goal.

    Returns what each phase observed, so a caller can assert on the pause, on the state
    either side of the reset, and on the new goal's event stream and prompts.
    """

    assert mode in RESET_MODES, mode
    model = SupervisorScriptedModel(script or SCRIPT)
    gateway = pausable_gateway()

    def factory(inner_model, context, assembler=None, turn=None):
        return production_graph(inner_model, gateway, context)

    async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
        await saver.setup()
        runtime = AgentRuntime(model, graph_factory=factory, checkpointer=saver)

        paused = [event async for event in runtime.invoke(SESSION, USER, STALE_MESSAGE, {})]

        # The runtime does not expose the compiled graph, and the reset needs `aget_state`
        # and `aupdate_state`, so build the same graph over the same saver and thread.
        graph = factory(model, CONTEXT).compile(checkpointer=saver)
        config = {
            "configurable": {"thread_id": thread_id_for_session(SESSION)},
            "recursion_limit": 50,
        }

        before = await graph.aget_state(config)
        if mode != "none":
            await graph.aupdate_state(
                config,
                {
                    "messages": Overwrite([]),
                    "goal": None,
                    "active_agent": None,
                    "turns": 0,
                    "domain_results": Overwrite([]) if mode == "overwrite" else [],
                },
                as_node="reset_goal",
            )
        after = await graph.aget_state(config)

        new_goal = await collect_stream(
            graph, config, {"messages": [{"role": "user", "content": "你好"}]}
        )

    return paused, before, after, new_goal, model


def test_a_pending_interrupt_is_abandoned_and_the_append_channels_are_cleared(schema_url):
    paused, before, after, new_goal, model = asyncio.run(run_reset(schema_url, mode="overwrite"))

    # One domain concluded, then the next parked on a real interrupt.
    # Two dispatches, so two graphs: the first domain concluded, the Supervisor then decided
    # a fresh handoff rather than draining a queue, and each decision publishes its own plan.
    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    assert field_names(paused[-1].data) == ["account_id"]

    assert before.interrupts, "the pause must be a native interrupt, or there is nothing to reset"
    assert before.values["domain_results"] == [
        {
            "agent_id": "transfer",
            "summary": STALE_RESULT,
            "instruction": "处理转账",
            "business_ids": {},
        }
    ]
    # Evidence for where a parked question actually lives. The question travels inside the
    # interrupt and is written in the *domain subgraph's* checkpoint namespace, so the
    # parent-level waiting channels are idle even while a real question is parked. Writing
    # them in the reset would therefore be a no-op that reads like proof the subgraph was
    # cleared -- which is why this test asserts on `interrupts` and never on those channels.
    assert before.values["pending_question"] is None
    assert before.values["pending_answer"] is None
    assert before.values["collected_inputs"] == {}
    assert before.values["turn_phase"] == "decide"
    assert field_names(before.interrupts[0].value) == ["account_id"]

    # The gate: the pause is gone, and it is gone without having been answered.
    assert after.interrupts == ()
    assert after.values["domain_results"] == []
    assert after.values["goal"] is None
    assert after.values["turns"] == 0
    assert after.values["messages"] == []

    # The new goal still streams normally through the same adapter.
    assert [event.event for event in new_goal] == ["reply_delta", "final"]
    assert new_goal[0].data["text"] == "新目标已完成"

    # And the Supervisor was shown a clean slate: neither the old message nor the old result.
    prompt = model.last_supervisor_prompt()
    assert STALE_RESULT not in prompt
    assert STALE_MESSAGE not in prompt


def test_a_bare_empty_list_would_leave_the_stale_results_in_place(schema_url):
    """Why the reset must write `Overwrite`: `domain_results` is an append channel.

    Without this case the `Overwrite` looks like decoration, and a later edit could drop it
    without any test objecting.
    """

    _paused, before, after, new_goal, model = asyncio.run(run_reset(schema_url, mode="bare_list"))

    assert before.values["domain_results"], "the first turn must have recorded a result"
    assert after.values["domain_results"] == before.values["domain_results"], (
        "a bare [] appends nothing, so the old results survive the reset"
    )

    # The Supervisor is told it already holds a result it never asked for -- the exact
    # confusion `new_agent_state` writes `Overwrite([])` to prevent.
    assert STALE_RESULT in model.last_supervisor_prompt()
    assert [event.event for event in new_goal] == ["reply_delta", "final"]


def test_without_the_reset_the_old_pause_survives_and_leaks_into_the_new_goal(schema_url):
    """The case that makes the whole mechanism necessary, and the trap in testing it.

    Passing new input to a suspended thread does not consume or clear its interrupt: the
    graph simply starts again from START. So the event stream looks perfectly healthy --
    `final` arrives either way -- while the thread is left with a pending interrupt that
    the next ordinary message would be treated as an answer to, and with the abandoned
    goal's transcript and results still in front of the Supervisor.
    """

    _paused, _before, after, new_goal, model = asyncio.run(run_reset(schema_url, mode="none"))

    # The pause is still pending: nothing consumed it.
    assert after.interrupts, "without the reset the interrupt stays pending"

    # The event stream is not evidence: it looks the same as the correct case.
    assert [event.event for event in new_goal] == ["reply_delta", "final"]

    # But the new goal inherited the abandoned one.
    prompt = model.last_supervisor_prompt()
    assert STALE_MESSAGE in prompt, "the old message leaked into the new goal's transcript"
    assert STALE_RESULT in prompt, "the old result leaked into the new goal"
    assert after.values["messages"], "the old transcript is still checkpointed"


def test_a_new_goal_re_entering_the_same_domain_asks_a_fresh_question(schema_url):
    """The abandoned question must not resurface when the same domain runs again.

    The parked question lives in the `bill` subgraph's checkpoint namespace, so clearing
    the parent's channels cannot reach it. What protects the new goal is that a subgraph
    node is handed a fresh namespace per invocation -- so the only way to prove it is to
    route the new goal back into the very domain that parked, and read the question it
    asks. A new goal that ends without delegating would never exercise this.
    """

    paused, before, after, new_goal, _model = asyncio.run(
        run_reset(schema_url, mode="overwrite", script=REENTRY_SCRIPT)
    )

    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    assert field_names(before.interrupts[0].value) == ["account_id"]
    abandoned = before.interrupts[0].value
    assert after.interrupts == (), "the reset must abandon the parked question"

    # The new goal re-enters `bill` and parks on its own, different question. This stream
    # comes straight from the graph (`collect_stream`), not from the runtime, so the
    # runtime-owned `waiting_confirm` frame is not part of it.
    assert [event.event for event in new_goal] == [
        "plan",
        "task_update",
        "agent_start",
        "interrupt",
    ]
    fresh = new_goal[-1].data
    assert field_names(fresh) == ["period"], (
        "the new goal inherited the abandoned question's fields"
    )
    assert fresh["display"] == "请提供周期"
    # A genuinely new question, so a client cannot answer the abandoned one by mistake.
    assert fresh["question_id"] != abandoned["question_id"]


def test_the_abandoned_question_cannot_be_answered_after_the_reset(schema_url):
    """The old subgraph stays in the checkpoint as history, but it is not executable.

    This is the other half of the namespace fact: abandoning a pause is not the same as
    deleting it, so the thing that has to be true is that its question is unreachable. The
    runtime refuses the resume before the graph runs, because no question is open.
    """

    model = SupervisorScriptedModel(SCRIPT)
    gateway = pausable_gateway()

    def factory(inner_model, context, assembler=None, turn=None):
        return production_graph(inner_model, gateway, context)

    async def scenario():
        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
            runtime = AgentRuntime(model, graph_factory=factory, checkpointer=saver)

            paused = [event async for event in runtime.invoke(SESSION, USER, STALE_MESSAGE, {})]
            abandoned = paused[-1].data["question_id"]

            # Abandon it, then try to answer the question that no longer exists.
            [event async for event in runtime.invoke(SESSION, USER, "你好", {"mode": "new_goal"})]

            with pytest.raises(AppError) as failure:
                [
                    event
                    async for event in runtime.invoke(
                        SESSION,
                        USER,
                        "补充",
                        {"resume": {"question_id": abandoned, "value": {"account_id": "1"}}},
                    )
                ]
        return failure.value

    failure = asyncio.run(scenario())

    assert failure.code == "INVALID_INTERRUPT_ANSWER"
