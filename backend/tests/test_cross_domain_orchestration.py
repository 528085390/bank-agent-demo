"""Cross-domain orchestration: one domain's structured result reaches the next.

The values here are invented, because this phase declares no business tool. What the
cases prove is the hand-off itself: a result the Gateway verified is what the next
domain is given, and the Supervisor never substitutes its own prose for it.
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.decisions import DecisionError
from app.agents.domain import DOMAIN_ROSTER, build_agent_graph
from tests.support.domain_harness import generic_roster
from app.agents.runtime import AgentRuntime
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
SESSION, USER = "s_1", "u_1"

COLLECT = ToolSpec(
    name="collect",
    description="Produce one reference",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
APPLY = ToolSpec(
    name="apply",
    description="Consume one reference",
    parameters={
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
        "additionalProperties": False,
    },
)

# A reply in the shape a real handler must produce. The seam requires a handler to return
# `ToolData`, whose `summary` is the field the Supervisor puts in front of the next domain;
# `facts` is the module's own and never leaves the server. A bare `{"ref": "A-1"}` is a shape
# no real module produces, and once the envelope is projected it reaches the model as
# nothing -- so a test that wants an artifact in a prompt has to put it in `summary`.
COLLECT_ARTIFACT = "已生成引用 A-1。"
COLLECT_REPLY = {
    "summary": COLLECT_ARTIFACT,
    "display": {},
    "facts": {},
    "business_ids": {},
}
HAND_OFF_REPLIES = {
    "collect": COLLECT_REPLY,
    "apply": {"summary": "引用 A-1 已应用。", "display": {}, "facts": {}, "business_ids": {}},
}


def two_domain_run(replies, *, replies_for=None, grants=None):
    recorder = RecordingAdapter(replies_for or {})
    gateway = ToolGateway(
        ToolCatalog.build([COLLECT, APPLY], grants or {"bill": ["collect"], "transfer": ["apply"]}),
        recorder,
    )
    model = ScriptedModel(replies)
    graph = build_agent_graph(model, gateway, CONTEXT, roster=generic_roster())
    runtime = AgentRuntime(graph=graph.compile(checkpointer=InMemorySaver()))

    async def run():
        return [event async for event in runtime.invoke(SESSION, USER, "帮我处理", {})]

    return asyncio.run(run()), model, recorder


HAND_OFF = [
    ("call", "handoff", '{"agent_id":"bill","instruction":"先生成一个引用"}'),
    ("call", "collect", "{}"),
    # Each tool round is fed back to the domain, whose report ends its activation -- and
    # the reports echo what the tools returned, because that is what domain_results carries.
    "已生成引用 A-1。",
    ("call", "handoff", '{"agent_id":"transfer","instruction":"用上一步的引用继续"}'),
    ("call", "apply", '{"ref":"A-1"}'),
    "引用 A-1 已应用。",
    "两步都完成了",
]


def test_the_second_domain_is_handed_the_first_domains_verified_result():
    events, model, recorder = two_domain_run(HAND_OFF, replies_for=HAND_OFF_REPLIES)

    # The artifact the Gateway returned is what the next domain's prompt carries: bill's
    # conclusion -- the model's report of what its tool told it -- is the row the Supervisor
    # shows transfer.
    transfer_prompt = model.prompts[3]
    assert "A-1" in transfer_prompt
    assert "bill" in transfer_prompt

    assert [event.data["agent_id"] for event in events if event.event == "agent_start"] == [
        "bill",
        "transfer",
    ]
    assert recorder.tool_names == ["collect", "apply"]


def test_the_artifact_comes_from_the_tool_result_not_from_the_models_prose():
    """The domain's result row is the tool's data; the model's own wording is not a fact."""

    replies = [
        ("call", "handoff", '{"agent_id":"bill","instruction":"生成引用"}'),
        ("call", "collect", "{}"),
        "已生成引用 A-1。",
        "我编了一个引用 B-9",
    ]
    events, model, _recorder = two_domain_run(replies, replies_for={"collect": COLLECT_REPLY})

    results = [event.data for event in events if event.event == "tool_result"]
    calls = [event.data for event in events if event.event == "tool_call"]
    assert len(results) == 1 and len(calls) == 1
    # The result names its own call, so the pair matches without trusting the model's prose.
    assert results[0]["call_id"] == calls[0]["call_id"]
    assert results[0] == {
        "agent_id": "bill",
        "tool_name": "collect",
        "call_id": calls[0]["call_id"],
        "status": "SUCCEEDED",
        # The envelope is projected by field, not forwarded: the frame carries the three
        # fields the client has an audience for, and `facts` is not one of them.
        "data": {"summary": COLLECT_ARTIFACT, "display": {}, "business_ids": {}},
        "error": None,
        # `RecordingAdapter` answers from a fixed reply and never classifies a failure, so
        # the code is absent on success -- which is exactly what the client must see.
        "error_code": None,
    }
    # The Supervisor's next prompt carries the tool's own `summary` -- the envelope field the
    # seam defines as the model-facing one -- and not the model's invented reference.
    assert "A-1" in model.prompts[3]
    assert "B-9" not in model.prompts[3]


def test_the_supervisor_is_shown_the_collected_result_before_it_chooses_again():
    events, model, _recorder = two_domain_run(HAND_OFF, replies_for=HAND_OFF_REPLIES)

    # Prompt 3 is the Supervisor's second decision: it must already see the artifact.
    assert "A-1" in model.prompts[3]
    assert "bill" in model.prompts[3]
    assert [event.data["text"] for event in events if event.event == "final"] == ["两步都完成了"]


# A result value distinctive enough that finding it in a prompt cannot be a coincidence.
STALE = "STALE-MARKER"


def stale_reply():
    """The first turn's tool result, in the shape a real handler must produce.

    `STALE` goes in `summary`, because that is the envelope field the Supervisor puts in
    front of the model. A bare `{"ref": STALE}` reply would be dropped by the projection
    before it could reach any prompt, and the two `not in` assertions below would then pass
    even if the reset were completely broken -- the projection, not the reset, would be what
    removed the marker.
    """

    return {"summary": f"引用 {STALE} 已生成。", "display": {}, "facts": {}, "business_ids": {}}


def two_turn_run(replies, *, replies_for=None, grants=None, second_context=None):
    """One runtime, two turns on one thread, with every prompt the model was shown."""

    recorder = RecordingAdapter(replies_for or {})
    gateway = ToolGateway(
        ToolCatalog.build([COLLECT, APPLY], grants or {"bill": ["collect"], "transfer": ["apply"]}),
        recorder,
    )
    model = ScriptedModel(replies)
    graph = build_agent_graph(model, gateway, CONTEXT, roster=generic_roster())
    runtime = AgentRuntime(graph=graph.compile(checkpointer=InMemorySaver()))

    async def run():
        first = [event async for event in runtime.invoke(SESSION, USER, "帮我处理", {})]
        second = [
            event
            async for event in runtime.invoke(SESSION, USER, "另一个请求", second_context or {})
        ]
        return first, second

    return asyncio.run(run()), model, recorder


# The first turn collects a result; the second turn must be planned without it. The report
# echoes the tool's summary, because the report is what domain_results carries.
TWO_TURNS = [
    ("call", "handoff", '{"agent_id":"bill","instruction":"先生成一个引用"}'),
    ("call", "collect", "{}"),
    f"引用 {STALE} 已生成。",
    "第一轮完成",
    "第二轮回答",
]


def test_a_later_turn_is_planned_without_the_previous_turns_results():
    """Collected results belong to the request that collected them.

    The state resets `goal` and `turns` for every request, so a carried-over result list
    would tell the next Supervisor it had already gathered things it never asked for --
    and its own instructions say not to re-ask for what it already has.
    """

    (first, _second), model, _recorder = two_turn_run(
        TWO_TURNS, replies_for={"collect": stale_reply()}
    )

    assert [event.event for event in first][-1] == "final"
    # The first turn really did put the marker in a prompt, so its absence below is the
    # reset's doing and not the projection's.
    assert any(STALE in prompt for prompt in model.prompts)
    # The last prompt is the second turn's Supervisor decision.
    assert STALE not in model.prompts[-1]


def test_a_new_goal_is_planned_without_the_abandoned_goals_results():
    """Abandoning a goal must leave nothing of it behind for the new one."""

    (_first, _second), model, _recorder = two_turn_run(
        TWO_TURNS,
        replies_for={"collect": stale_reply()},
        second_context={"mode": "new_goal"},
    )

    # The control, for the same reason as its sibling above: the first turn really did put
    # the marker in a prompt, so its absence below is the reset's doing and not the
    # projection's. Without this, the assertion passes even if the reset is broken.
    assert any(STALE in prompt for prompt in model.prompts)
    assert STALE not in model.prompts[-1]


def test_a_resume_keeps_the_results_collected_before_the_pause():
    """The reset is per request, so a resumed turn still sees its own earlier results."""

    recorder = RecordingAdapter(HAND_OFF_REPLIES)
    gateway = ToolGateway(
        ToolCatalog.build([COLLECT, APPLY], {"bill": ["collect"], "transfer": ["apply"]}), recorder
    )
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"bill","instruction":"先生成一个引用"}'),
            ("call", "collect", "{}"),
            "已生成引用 A-1。",
            ("call", "handoff", '{"agent_id":"transfer","instruction":"用上一步的引用继续"}'),
            ("call", "ask_user", '{"fields":["ref"],"display":"请提供引用"}'),
            # No second ask: the pause replays `wait`, which reads the question back from
            # the checkpoint, so the resume value reaches `process` without re-deciding.
            ("call", "apply", '{"ref":"A-1"}'),
            "引用 A-1 已应用。",
            "完成了",
        ]
    )
    graph = build_agent_graph(model, gateway, CONTEXT, roster=generic_roster())
    runtime = AgentRuntime(graph=graph.compile(checkpointer=InMemorySaver()))

    async def run():
        paused = [event async for event in runtime.invoke(SESSION, USER, "帮我处理", {})]
        resumed = [
            event
            async for event in runtime.invoke(
                SESSION, USER, "ref=A-1", {"resume": {"value": {"ref": "A-1"}}}
            )
        ]
        return paused, resumed

    paused, resumed = asyncio.run(run())

    # Bill collected its result, then transfer paused for the reference.
    assert [event.event for event in paused][-1] == "interrupt"
    assert "collect" in recorder.tool_names
    # The replayed domain node is the one carrying the user's answer; it must still see the
    # result this same request collected before the pause. The assertion is on the collected
    # result's own `summary`, not on `"A-1"`: the resume value the user sent contains that
    # too, so asserting it would pass even if the collected result had been dropped. The
    # resumed call is found by its thread ending on the answer -- the report call after it
    # ends on the tool's envelope instead.
    resumed_calls = [call for call in model.messages if "办事 Agent" in call[0]["content"]]
    replayed = [call for call in resumed_calls if '"user_answer"' in str(call[-1].get("content"))]
    assert len(replayed) == 1
    assert COLLECT_ARTIFACT in replayed[0][0]["content"]
    assert [event.event for event in resumed][-1] == "final"


def test_a_domain_cannot_borrow_the_other_domains_tool():
    """`transfer` carries `apply` only, so `collect` is not in the menu it is handed.

    Under native tool calling the schema list *is* the menu: the other domain's tool is not
    merely refused, it is never offered, and naming it fails as a protocol error before the
    gateway is consulted. The gateway keeps its own refusal as the second line of defence,
    which the card module's authorization cases cover. The run is spelled out rather than
    taken from `two_domain_run` because the recorder has to be read after the failure.
    """

    recorder = RecordingAdapter({"collect": {"ref": "A-1"}})
    gateway = ToolGateway(
        ToolCatalog.build([COLLECT, APPLY], {"bill": ["collect"], "transfer": ["apply"]}),
        recorder,
    )
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"试着调用 collect"}'),
            ("call", "collect", "{}"),
        ]
    )
    graph = build_agent_graph(model, gateway, CONTEXT, roster=generic_roster())
    runtime = AgentRuntime(graph=graph.compile(checkpointer=InMemorySaver()))

    async def run():
        return [event async for event in runtime.invoke(SESSION, USER, "帮我处理", {})]

    with pytest.raises(DecisionError, match="未知工具"):
        asyncio.run(run())

    offered = {tool["function"]["name"] for call in model.tools for tool in call}
    assert "apply" in offered
    assert "collect" not in offered
    assert recorder.invocations == []


def test_the_turn_ends_with_one_final_from_the_supervisor():
    events, _model, _recorder = two_domain_run(
        HAND_OFF, replies_for={"collect": {"ref": "A-1"}, "apply": {"applied": True}}
    )

    finals = [event.data for event in events if event.event == "final"]
    assert len(finals) == 1
    assert finals[0] == {
        "text": "两步都完成了",
        "business_ids": [],
        "terminal_reason": None,
        "error_status": None,
    }


def test_every_domain_keeps_its_own_place_in_the_roster_order():
    assert [declaration.agent_id for declaration in DOMAIN_ROSTER][:2] == ["transfer", "bill"]


def test_a_subgraph_pause_does_not_swallow_the_supervisors_reply():
    """The domain pauses, the user answers, and the Supervisor still concludes."""

    recorder = RecordingAdapter({"collect": {"ref": "A-1"}})
    gateway = ToolGateway(ToolCatalog.build([COLLECT, APPLY], {"bill": ["collect"]}), recorder)
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"bill","instruction":"生成引用"}'),
            ("call", "ask_user", '{"fields":["ref"],"display":"请确认引用"}'),
            ("call", "collect", "{}"),
            "已生成引用 A-1。",
            "已完成",
        ]
    )
    graph = build_agent_graph(model, gateway, CONTEXT, roster=generic_roster()).compile(checkpointer=InMemorySaver())
    runtime = AgentRuntime(graph=graph)

    async def run(message, context):
        return [event async for event in runtime.invoke(SESSION, USER, message, context)]

    paused = asyncio.run(run("帮我", {}))
    resumed = asyncio.run(run("确认", {"resume": {"value": {"ref": "A-1"}}}))

    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    # The resume replays the parked domain, so the graph is not drawn again -- but the base
    # layer re-publishes the plan from the checkpoint, because a client that comes back has
    # nothing on screen.
    assert [event.event for event in resumed] == [
        "plan",
        "task_update",
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    assert [event.data["text"] for event in resumed if event.event == "final"] == ["已完成"]
