"""The task graph's node lifecycle: dispatch, interrupt, resume and finish.

A plan is only a picture of the work if each node's status keeps up with it, so this file
drives one whole turn per edge: a dispatch marks its node running, a pause marks it
waiting_confirm before the question reaches the user, a finish marks it done -- or failed,
which also skips what the same plan had queued -- and a resume re-sends the plan and the
running node, because the client that comes back has neither.

The two layers own different statuses on purpose. The graph owns running, done, failed and
skipped: it is the only layer that knows a round started or ended. The runtime owns
waiting_confirm and the plan+running pair a resume re-sends: they are read from the
checkpoint, which is the only place a suspended round still exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph, build_domain_specs
from app.agents.runtime import AgentEvent, AgentRuntime, thread_id_for_session
from app.agents.state import AgentState
from app.agents.supervisor.graph import supervisor_node
from app.agents.task_dag import TaskNode, TaskPlan, label_for_domain_task
from app.agents.turn import TurnAction, TurnConfirmation, TurnResult, TurnStatus
from app.modules.base import ModuleSpec, ToolData
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.handler_graph import handler_graph_builder
from tests.support.scripted_model import ScriptedModel, StreamingModel

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
SESSION = "s_1"
USER = "u_1"
MESSAGE = "先查账单，再转账"
THREAD = {"configurable": {"thread_id": "t_1"}}

BILL = DomainDeclaration(agent_id="bill", display="账单查询", purpose="查账单")
TRANSFER = DomainDeclaration(agent_id="transfer", display="资金转账", purpose="转账")

HANDOFF_BILL = '{"agent_id":"bill","instruction":"查账单"}'
HANDOFF_TRANSFER = '{"agent_id":"transfer","instruction":"转账"}'

BILL_SUMMARY = "上个月共 12 笔，合计 3560 元"
FAILED_SUMMARY = "账单查询失败，请稍后再试"
WAITING_SUMMARY = "还需要您补充账单月份"
CONFIRM_SUMMARY = "请确认向张三转账 500 元"
DONE_SUMMARY = "转账已提交，等待银行处理"

CONFIRMATION = TurnConfirmation(
    pending_id="p_1",
    operation="transfer",
    title="转账确认",
    amount_minor=50000,
    product_name="储蓄卡",
    account_display="6222 **** 1234",
)


def completed(summary: str) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, summary=summary)


def parked(summary: str) -> TurnResult:
    """A domain that ends its call by asking for a click, not by finishing the work."""

    return TurnResult(
        status=TurnStatus.WAITING_CONFIRMATION,
        summary=summary,
        confirmation=CONFIRMATION,
    )


class ScriptedDomain:
    """A whole-turn domain: one scripted result per call, plus one for a confirmed action.

    It is deliberately not a bank module. What is under test is the base's bookkeeping, and
    a domain whose only behaviour is "return this" keeps a failure attributable to the
    lifecycle rather than to a prompt or a tool.
    """

    def __init__(
        self,
        result: TurnResult | None = None,
        *,
        action: TurnResult | None = None,
    ) -> None:
        self._result = result
        self._action = action
        self.calls: list[str] = []
        self.actions: list[TurnAction] = []

    def __call__(self, context: TrustedContext, model: Any, message: str) -> TurnResult:
        del context, model
        self.calls.append(message)
        if self._result is None:
            raise AssertionError("this domain was not meant to run")
        return self._result

    def act(self, context: TrustedContext, model: Any, action: TurnAction) -> TurnResult:
        del context, model
        self.actions.append(action)
        if self._action is None:
            raise AssertionError("this domain was not meant to confirm anything")
        return self._action


def _noop_handler(invocation: Any, client: Any) -> ToolData:
    del invocation, client
    return ToolData(summary="ok", display={}, facts={})


def domain_module(declaration: DomainDeclaration, handler: Any) -> ModuleSpec:
    """A one-domain module whose own graph answers from `handler`.

    The placeholder tool is named per domain: the registry refuses two modules that declare
    the same tool, and a two-node plan needs both domains in one graph.
    """

    tool = ToolSpec(
        name=f"{declaration.agent_id}_noop", description="Placeholder for a handler module"
    )
    return ModuleSpec(
        name=declaration.agent_id,
        declaration=replace(declaration, graph_builder=handler_graph_builder(handler)),
        tools={tool.name: tool},
        handlers={tool.name: _noop_handler},
    )


def lifecycle_graph(*modules: ModuleSpec, model: Any) -> Any:
    """The production graph over these modules, checkpointed like a real thread."""

    registry = build_registry(list(modules))
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}))
    graph = build_agent_graph(model, gateway, CONTEXT, roster=registry.roster)
    return graph.compile(checkpointer=InMemorySaver())


def collect(
    graph: Any, message: str = MESSAGE, context: dict[str, Any] | None = None
) -> list[AgentEvent]:
    """One session turn, the way the legacy adapter drives it."""

    runtime = AgentRuntime(graph=graph)

    async def run() -> list[AgentEvent]:
        return [event async for event in runtime.invoke(SESSION, USER, message, context or {})]

    return asyncio.run(run())


def names(events: list[AgentEvent]) -> list[str]:
    return [event.event for event in events]


def task_updates(events: list[AgentEvent]) -> list[dict[str, Any]]:
    return [event.data for event in events if event.event == "task_update"]


def plan_id_of(events: list[AgentEvent]) -> str:
    return next(event.data["plan_id"] for event in events if event.event == "plan")


def checkpoint_values(graph: Any) -> dict[str, Any]:
    return graph.get_state({"configurable": {"thread_id": thread_id_for_session(SESSION)}}).values


# --------------------------------------------------------------- 派发：节点开始运行


def supervisor_only(model: Any) -> Any:
    """Just the Supervisor node, so one dispatch can be read without running a domain."""

    gateway = ToolGateway(build_registry().catalog, RecordingAdapter())
    specs = build_domain_specs(gateway, CONTEXT, roster=(BILL, TRANSFER))
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node(model, specs))
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", END)
    return graph.compile(checkpointer=InMemorySaver())


def run_once(graph: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    """What one pass published to the custom stream."""

    custom: list[dict[str, Any]] = []
    for chunk in graph.stream(state, THREAD, stream_mode=["updates", "custom"], version="v2"):
        if chunk["type"] == "custom":
            custom.append(chunk["data"])
    return custom


def test_task_update_running_emitted_on_dispatch():
    """A dispatch is what starts a node, so it is also what says the node is running."""

    graph = supervisor_only(ScriptedModel([("call", "handoff", HANDOFF_BILL)]))
    custom = run_once(graph, {"messages": [{"role": "user", "content": MESSAGE}]})

    plan = next(event for event in custom if event.get("event") == "plan")
    assert [event for event in custom if event.get("event") == "task_update"] == [
        {
            "event": "task_update",
            "plan_id": plan["plan_id"],
            "node_id": "node_1",
            "status": "running",
        }
    ]
    # The node id is not only broadcast: the checkpoint holds it, so the round that runs
    # next -- and a page that reconnects -- can name the node the thread is on.
    values = graph.get_state(THREAD).values
    assert values["active_task_node_id"] == "node_1"
    assert values["task_plan"]["plan_id"] == plan["plan_id"]


def test_task_update_waiting_confirm_emitted_on_interrupt():
    """A parked node is not finished, and the frame says so before the question arrives."""

    bill = ScriptedDomain(parked(CONFIRM_SUMMARY))
    graph = lifecycle_graph(
        domain_module(BILL, bill), model=ScriptedModel([("call", "handoff", HANDOFF_BILL)])
    )

    events = collect(graph)

    assert names(events) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "interrupt",
    ]
    assert task_updates(events) == [
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "waiting_confirm",
            "artifact": None,
            "error": None,
        },
    ]
    assert events[-1].data["kind"] == "confirmation"


def test_the_fast_path_marks_the_second_node_running():
    """R11-REV: the queue fast-path advances the checkpoint past node_1."""

    bill = ScriptedDomain(completed(BILL_SUMMARY))
    transfer = ScriptedDomain(parked(CONFIRM_SUMMARY))
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_BILL),
                ("call", "handoff", HANDOFF_TRANSFER),
            ]
        ]
    )
    graph = lifecycle_graph(
        domain_module(BILL, bill), domain_module(TRANSFER, transfer), model=model
    )

    events = collect(graph)

    assert names(events) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "interrupt",
    ]
    assert task_updates(events) == [
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "done",
            "artifact": {"summary": BILL_SUMMARY},
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_2",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_2",
            "status": "waiting_confirm",
            "artifact": None,
            "error": None,
        },
    ]
    # Both domains really ran -- a domain answers the user's request, not the handoff line --
    # and the checkpoint moved on with the second dispatch.
    assert bill.calls == [MESSAGE]
    assert transfer.calls == [MESSAGE]
    values = checkpoint_values(graph)
    assert values["active_task_node_id"] == "node_2"
    assert [node["id"] for node in values["task_plan"]["nodes"]] == ["node_1", "node_2"]


# --------------------------------------------------------------- 结束：节点被标记


def test_task_update_done_emitted_on_subtask_end():
    """The round after the domain is the one that closes its node, with its own summary."""

    bill = ScriptedDomain(completed(BILL_SUMMARY))
    model = ScriptedModel([("call", "handoff", HANDOFF_BILL), "账单已经查好了", "不客气"])
    graph = lifecycle_graph(domain_module(BILL, bill), model=model)

    events = collect(graph)

    assert names(events) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "reply_delta",
        "final",
    ]
    assert task_updates(events) == [
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "done",
            "artifact": {"summary": BILL_SUMMARY},
            "error": None,
        },
    ]
    # Closed once, not once per round: the turn's ending cleared the plan, so the next turn
    # has nothing left to close and says nothing about the graph at all.
    later = collect(graph, "谢谢")
    assert names(later) == ["reply_delta", "final"]
    assert task_updates(later) == []


@pytest.mark.parametrize(
    "status",
    [TurnStatus.FAILED, TurnStatus.UNSUPPORTED, TurnStatus.UNKNOWN],
    ids=["failed", "unsupported", "unknown"],
)
def test_remaining_nodes_marked_skipped_on_failure(status: TurnStatus):
    """A failed node ends the plan it belongs to: what it queued is skipped, not run.

    All three failing statuses close the node the same way. The base has one word for "this
    node did not deliver"; the module's own honest wording travels in the frame's `error`.
    """

    bill = ScriptedDomain(TurnResult(status=status, summary=FAILED_SUMMARY))
    transfer = ScriptedDomain(completed(DONE_SUMMARY))
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_BILL),
                ("call", "handoff", HANDOFF_TRANSFER),
            ],
            ["好的"],
        ]
    )
    graph = lifecycle_graph(
        domain_module(BILL, bill), domain_module(TRANSFER, transfer), model=model
    )

    events = collect(graph)

    assert names(events) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "task_update",
        "reply_delta",
        "final",
    ]
    assert task_updates(events) == [
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_1",
            "status": "failed",
            "artifact": None,
            "error": FAILED_SUMMARY,
        },
        {
            "plan_id": plan_id_of(events),
            "node_id": "node_2",
            "status": "skipped",
            "artifact": None,
            "error": None,
        },
    ]
    # The queue was cleared, so the node the plan had drawn never started: no running frame
    # for node_2, and the domain it named was never called.
    assert bill.calls == [MESSAGE]
    assert transfer.calls == []


def test_a_result_from_another_domain_does_not_close_the_node():
    """The finish step closes a node on its own owner's result, and on nobody else's.

    A round can end holding a different domain's answer -- a resumed pause, or a handoff the
    plan queued earlier. Closing node_1 on that answer would report work its owner never did,
    and clearing the queue with it would cancel the plan. Handed exactly that state, the
    supervisor has to say nothing about node_1 and still dispatch what node_1 queued.
    """

    plan = TaskPlan(
        plan_id="plan_mismatch",
        nodes=(
            TaskNode(id="node_1", label=label_for_domain_task("bill", "查账单"), owner="bill"),
            TaskNode(
                id="node_2",
                label=label_for_domain_task("transfer", "转账"),
                owner="transfer",
                depends_on=("node_1",),
            ),
        ),
    )
    graph = supervisor_only(ScriptedModel([]))

    updates = run_once(
        graph,
        {
            "messages": [{"role": "user", "content": MESSAGE}],
            "turns": 1,
            "domain_results": [
                {"agent_id": "transfer", "turn_status": "completed", "summary": BILL_SUMMARY}
            ],
            "pending_handoffs": [
                {"node_id": "node_2", "agent_id": "transfer", "instruction": "转账"}
            ],
            "task_plan": plan.as_dict(),
            "active_task_node_id": "node_1",
        },
    )

    # Exactly one frame, and it is node_2 starting. Nothing closes node_1, and nothing is
    # skipped: the queue survived the finish step, so the fast-path still dispatched it.
    assert updates == [
        {
            "event": "task_update",
            "plan_id": plan.plan_id,
            "node_id": "node_2",
            "status": "running",
        }
    ]
    values = graph.get_state(THREAD).values
    assert values["active_task_node_id"] == "node_2"
    assert values["pending_handoffs"] == []


def test_a_parked_node_is_not_marked_done_or_failed():
    """R15: waiting_input is a pause, not an ending, so the node stays as it was."""

    bill = ScriptedDomain(TurnResult(status=TurnStatus.WAITING_INPUT, summary=WAITING_SUMMARY))
    model = ScriptedModel([("call", "handoff", HANDOFF_BILL), "请补充账单月份"])
    graph = lifecycle_graph(domain_module(BILL, bill), model=model)

    events = collect(graph)

    assert names(events) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    assert [data["status"] for data in task_updates(events)] == ["running"]
    assert (
        next(event.data for event in events if event.event == "subtask_end")["turn_status"]
        == "waiting_input"
    )


# --------------------------------------------------------------- 恢复：重发计划与运行节点


def test_a_resume_redraws_the_plan_and_the_running_node():
    """R13: a client that comes back has no plan, so the resume re-sends it first."""

    bill = ScriptedDomain(parked(CONFIRM_SUMMARY), action=completed(DONE_SUMMARY))
    model = ScriptedModel([("call", "handoff", HANDOFF_BILL), "已经办好了"])
    graph = lifecycle_graph(domain_module(BILL, bill), model=model)

    paused = collect(graph)

    assert names(paused) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "interrupt",
    ]
    question = paused[-1].data
    assert question["kind"] == "confirmation"

    resumed = collect(
        graph,
        "确认",
        {"resume": {"question_id": question["question_id"], "value": {"decision": "confirm"}}},
    )

    assert names(resumed) == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "reply_delta",
        "final",
    ]
    # The same graph, re-sent: the same plan id and nodes, and the node the thread is on.
    assert resumed[0].data == paused[0].data
    assert resumed[1].data == {
        "plan_id": plan_id_of(paused),
        "node_id": "node_1",
        "status": "running",
        "artifact": None,
        "error": None,
    }
    # The continuation of the domain comes after the redraw, and the click closes the node.
    assert resumed[2].event == "agent_start"
    assert resumed[4].data["status"] == "done"
    assert resumed[4].data["artifact"] == {"summary": DONE_SUMMARY}
    assert [action.kind for action in bill.actions] == [TurnAction.CONFIRM]
