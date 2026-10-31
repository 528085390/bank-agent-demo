"""The Supervisor's task graph: the plan one decision describes, and when it is published.

A multi-step request is only distinguishable from a single one if the plan is visible, so
this file pins three things: the plan's shape, the node ids the queue carries, and the two
paths that must not publish a plan (a reply-only turn, and the queue fast-path whose graph
is already in the checkpoint).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.decisions import Handoff, SupervisorDecision
from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_domain_specs
from app.agents.state import AgentState
from app.agents.supervisor.graph import _build_task_plan_from_decision, supervisor_node
from app.agents.task_dag import label_for_domain_task
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel, StreamingModel

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
MESSAGE = "先查账单，再转账"
BILL = DomainDeclaration(agent_id="bill", display="账单查询", purpose="查账单")
TRANSFER = DomainDeclaration(agent_id="transfer", display="资金转账", purpose="转账")


def test_build_task_plan_single_handoff():
    decision = SupervisorDecision(
        agent_id="transfer",
        instruction="转账 500 元",
    )
    plan = _build_task_plan_from_decision(decision)
    assert plan is not None
    assert len(plan.nodes) == 1
    assert plan.nodes[0].id == "node_1"
    assert plan.nodes[0].owner == "transfer"
    assert plan.nodes[0].depends_on == ()
    # Added to the brief's body: without these two, a builder that invented its own id and
    # its own label would pass, and R7/the spec's label rule would be untested.
    assert plan.plan_id.startswith("plan_")
    assert plan.nodes[0].label == label_for_domain_task("transfer", "转账 500 元")


def test_build_task_plan_multi_handoff():
    decision = SupervisorDecision(
        agent_id="bill",
        instruction="查账单",
        queued=(Handoff(agent_id="transfer", instruction="转账"),),
    )
    plan = _build_task_plan_from_decision(decision)
    assert plan is not None
    assert len(plan.nodes) == 2
    assert plan.nodes[0].id == "node_1"
    assert plan.nodes[0].owner == "bill"
    assert plan.nodes[1].id == "node_2"
    assert plan.nodes[1].owner == "transfer"
    assert plan.nodes[1].depends_on == ("node_1",)


def test_build_task_plan_no_handoff():
    decision = SupervisorDecision(reply="你好，请问有什么可以帮您？")
    plan = _build_task_plan_from_decision(decision)
    assert plan is None


def supervisor_only(model: Any):
    """Just the Supervisor node, so one decision can be read without running a domain."""

    gateway = ToolGateway(build_registry().catalog, RecordingAdapter())
    specs = build_domain_specs(gateway, CONTEXT, roster=(BILL, TRANSFER))
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node(model, specs))
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", END)
    return graph.compile()


def run(graph: Any, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """What the node published, and what it wrote to the state, from one pass."""

    custom: list[dict[str, Any]] = []
    update: dict[str, Any] = {}
    chunks = graph.stream(
        state,
        {"configurable": {"thread_id": "t_1"}},
        stream_mode=["updates", "custom"],
        version="v2",
    )
    for chunk in chunks:
        if chunk["type"] == "custom":
            custom.append(chunk["data"])
        elif chunk["type"] == "updates":
            update.update(chunk["data"].get("supervisor") or {})
    return custom, update


def test_a_dispatch_publishes_the_plan_and_keeps_it_in_the_state():
    model = ScriptedModel([("call", "handoff", '{"agent_id":"bill","instruction":"查账单"}')])

    custom, update = run(
        supervisor_only(model), {"messages": [{"role": "user", "content": MESSAGE}]}
    )

    plans = [event for event in custom if event.get("event") == "plan"]
    assert len(plans) == 1, custom
    plan = plans[0]
    assert plan["plan_id"].startswith("plan_")
    assert [(node["id"], node["owner"]) for node in plan["nodes"]] == [("node_1", "bill")]
    # The same graph is checkpointed, so a pause or a reconnect can rebuild what was shown.
    assert update["task_plan"] == {"plan_id": plan["plan_id"], "nodes": plan["nodes"]}
    assert update["active_task_node_id"] == "node_1"
    # The dispatch itself is unchanged: the keys the round already wrote are still there.
    assert update["active_agent"] == "bill"
    assert update["pending_handoffs"] == []


def test_each_queued_handoff_carries_the_node_id_it_will_run_as():
    model = StreamingModel(
        [
            [
                ("call", "handoff", '{"agent_id":"bill","instruction":"查账单"}'),
                ("call", "handoff", '{"agent_id":"transfer","instruction":"转账"}'),
            ]
        ]
    )

    custom, update = run(
        supervisor_only(model), {"messages": [{"role": "user", "content": MESSAGE}]}
    )

    plan = next(event for event in custom if event.get("event") == "plan")
    assert [(node["id"], node["owner"]) for node in plan["nodes"]] == [
        ("node_1", "bill"),
        ("node_2", "transfer"),
    ]
    assert plan["nodes"][1]["depends_on"] == ["node_1"]
    assert update["pending_handoffs"] == [
        {"node_id": "node_2", "agent_id": "transfer", "instruction": "转账"}
    ]
    assert update["active_task_node_id"] == "node_1"


def test_the_queue_fast_path_publishes_no_second_plan():
    """The graph is already checkpointed; re-emitting it would restart the picture (R8)."""

    queued = [{"node_id": "node_2", "agent_id": "transfer", "instruction": "转账"}]

    custom, update = run(
        supervisor_only(ScriptedModel([])),
        {"messages": [{"role": "user", "content": MESSAGE}], "pending_handoffs": queued},
    )

    assert [event for event in custom if event.get("event") == "plan"] == []
    assert "task_plan" not in update
    assert update["active_agent"] == "transfer"
    assert update["pending_handoffs"] == []


def test_a_handoff_to_a_concluded_domain_publishes_no_plan():
    """The closing reply replaces the dispatch, so the plan must come after that check (R8):
    a graph drawn first would describe work the round then decides not to run."""

    results = [{"agent_id": "bill", "summary": "账单已查询完毕", "turn_status": "completed"}]
    model = ScriptedModel([("call", "handoff", '{"agent_id":"bill","instruction":"查账单"}')])

    custom, update = run(
        supervisor_only(model),
        {"messages": [{"role": "user", "content": MESSAGE}], "domain_results": results},
    )

    assert [event for event in custom if event.get("event") == "plan"] == []
    assert update["active_agent"] is None
    assert update["task_plan"] is None
    assert update["pending_handoffs"] == []
