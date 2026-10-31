"""End-to-end acceptance for the task graph: the lifecycle, over HTTP and SSE.

Both turns run scripted whole-turn domains. A whole-turn domain is the only thing that
publishes `turn_status` in `domain_results`, and that status is the only thing the supervisor
closes a node on (spec §4.4); a domain driven by the generic tool loop would leave its node
`running` until the graph was cleared, so these domains are the real conclusion path and
nothing else. `handler_module` wires them through production `conclude_turn`, which is what
makes the node transitions the ones a user would get.

The domains are scripted on purpose: what this file tests is the lifecycle -- dispatch, pause,
resume, cascade -- so when it fails the failure belongs to the graph and the HTTP layer that
carried its frames, never to a model's wording.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.turn import TurnAction, TurnConfirmation, TurnResult, TurnStatus
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.recording import RecordingAdapter
from tests.support.handler_graph import handler_module
from tests.support.scripted_model import StreamingModel
from tests.test_multi_agent_api import EVENT_FIELDS, client_for, frames, post

BILL = DomainDeclaration(agent_id="bill", display="账单查询", purpose="查账单")
TRANSFER = DomainDeclaration(agent_id="transfer", display="资金转账", purpose="转账")

HANDOFF_BILL = '{"agent_id":"bill","instruction":"查账单"}'
HANDOFF_TRANSFER = '{"agent_id":"transfer","instruction":"转账"}'
HANDOFF_TRANSFER_AFTER_BILL = (
    '{"agent_id":"transfer","instruction":"按上一步结论继续：{{previous_summary}}"}'
)
MESSAGE = "先查账单，再转账"

BILL_SUMMARY = "上个月共 12 笔，合计 3560 元"
FAILED_SUMMARY = "账单查询失败，请稍后再试"
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


def failed(summary: str) -> TurnResult:
    return TurnResult(status=TurnStatus.FAILED, summary=summary)


def parked(summary: str) -> TurnResult:
    return TurnResult(
        status=TurnStatus.WAITING_CONFIRMATION, summary=summary, confirmation=CONFIRMATION
    )


class ScriptedDomain:
    """One whole-turn domain: the result it concludes with, and what it was asked to do."""

    def __init__(
        self, result: TurnResult | None = None, *, action: TurnResult | None = None
    ) -> None:
        self.result = result
        self.action = action
        self.calls: list[str] = []
        self.actions: list[TurnAction] = []

    def __call__(self, context: Any, model: Any, message: str) -> TurnResult:
        self.calls.append(message)
        if self.result is None:
            raise AssertionError("this domain was not meant to run")
        return self.result

    def act(self, context: Any, model: Any, action: TurnAction) -> TurnResult:
        self.actions.append(action)
        if self.action is None:
            raise AssertionError("this domain was not meant to confirm anything")
        return self.action


def domain_module(
    declaration: DomainDeclaration,
    domain: ScriptedDomain,
    *,
    request: Any = None,
) -> ModuleSpec:
    """The declaration as a module, with the placeholder tool named after its own domain.

    `handler_module` declares the same placeholder tool name in every module, and the
    registry refuses two modules that declare one tool, so the name is made per-domain here
    before the modules are registered together.
    """

    module = handler_module(declaration.agent_id, declaration, domain, request=request)
    tool = replace(module.tools["handler_noop"], name=f"{declaration.agent_id}_noop")
    return replace(
        module,
        tools={tool.name: tool},
        handlers={tool.name: module.handlers["handler_noop"]},
    )


def agent_for(model: Any, *modules: ModuleSpec) -> AgentRuntime:
    """The production graph over scripted domains, behind the same factory the app uses."""

    registry = build_registry(list(modules))
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}))

    def factory(model: Any, context: Any, assembler: Any = None, turn: Any = None) -> Any:
        return build_agent_graph(
            model, gateway, context, assembler=assembler, turn=turn, roster=registry.roster
        )

    return AgentRuntime(model, graph_factory=factory)


def updates(parsed: list[tuple[str | None, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [data for name, data in parsed if name == "task_update"]


def assert_plan_frame(plan: dict[str, Any]) -> None:
    """The plan frame, closed: one generated id and the two nodes, every key spelled out.

    Both scenarios share this assertion so a cascade test cannot pass on a plan that only
    looks right: the shape is exact, and a node list that lost a key, gained one or named a
    different owner fails here rather than only in the flow that happened to check more.
    """

    assert plan["plan_id"].startswith("plan_")
    assert plan["nodes"] == [
        {
            "id": "node_1",
            "label": "账单查询与分析 · 查账单",
            "owner": "bill",
            "depends_on": [],
            "status": "pending",
            "artifact": None,
            "error": None,
        },
        {
            "id": "node_2",
            "label": "资金转账办理 · 转账",
            "owner": "transfer",
            "depends_on": ["node_1"],
            "status": "pending",
            "artifact": None,
            "error": None,
        },
    ]


def assert_event_contract(
    parsed: list[tuple[str | None, dict[str, Any]]], *, interrupt_extra: frozenset[str]
) -> None:
    """Every frame carries exactly the fields the client contract declares for it.

    The interrupt is the one frame with an optional extra key, and *which* key is allowed is
    a property of the question the stream carries, not of the frame: a confirmation box
    carries `confirmation`, an authorization question `authorization`. The caller passes the
    one its own stream is allowed, so a card is never validated by a rule written for an
    authorization question; a stream with no interrupt passes the empty set.
    """

    for name, data in parsed:
        assert name in EVENT_FIELDS, f"unknown event {name!r}"
        expected = EVENT_FIELDS[name]
        assert expected <= set(data), f"{name} lost {sorted(expected - set(data))}"
        optional = interrupt_extra if name == "interrupt" else frozenset()
        assert set(data) - expected <= optional, f"{name} carried {sorted(set(data) - expected)}"


def test_task_graph_multi_domain_execution_flow(rt, session):
    """Two handoffs: node_1 runs to done, node_2 parks, the resumed click closes it.

    The stream opens with the plan, dispatches node_1, closes it on the bill result, then
    dispatches node_2 and parks it on its confirmation card. An ordinary message sent while
    parked is refused without redrawing the graph, and the resume -- which the client that
    comes back needs because it has no plan -- re-sends the same plan, runs node_2 again and
    closes it with the click's own summary before the final.
    """

    bill = ScriptedDomain(completed(BILL_SUMMARY))
    transfer = ScriptedDomain(parked(CONFIRM_SUMMARY), action=completed(DONE_SUMMARY))
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_BILL),
                ("call", "handoff", HANDOFF_TRANSFER),
            ],
            ["两笔都办好了"],
        ]
    )
    client = client_for(
        rt,
        session,
        agent_for(model, domain_module(BILL, bill), domain_module(TRANSFER, transfer)),
    )

    paused = frames(post(client, session, MESSAGE))

    assert [name for name, _ in paused] == [
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
    assert_event_contract(paused, interrupt_extra=frozenset({"confirmation"}))

    plan = paused[0][1]
    assert_plan_frame(plan)

    assert updates(paused) == [
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_1",
            "status": "done",
            "artifact": {"summary": BILL_SUMMARY},
            "error": None,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_2",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_2",
            "status": "waiting_confirm",
            "artifact": None,
            "error": None,
        },
    ]
    assert [data for name, data in paused if name == "agent_start"] == [
        {"agent_id": "bill"},
        {"agent_id": "transfer"},
    ]
    assert [data for name, data in paused if name == "subtask_end"] == [
        {
            "agent_id": "bill",
            "status": "SUCCEEDED",
            "summary": BILL_SUMMARY,
            "turn_status": "completed",
            "grounding_blocked": None,
        },
        {
            "agent_id": "transfer",
            "status": "SUCCEEDED",
            "summary": CONFIRM_SUMMARY,
            "turn_status": "waiting_confirmation",
            "grounding_blocked": None,
        },
    ]

    card = paused[-1][1]
    assert set(card) == EVENT_FIELDS["interrupt"] | {"confirmation"}
    assert card["kind"] == "confirmation"
    assert card["agent_id"] == "transfer"
    assert card["display"] == CONFIRM_SUMMARY
    assert card["attempt"] == 1
    assert card["question_id"].startswith("q_")
    assert card["fields"] == [
        {
            "name": "decision",
            "label": "操作决定",
            "type": "select",
            "required": True,
            "options": ["确认", "取消"],
        }
    ]
    assert card["confirmation"] == CONFIRMATION.as_payload()

    # AC-D2-5, the paused leg: an ordinary message while the turn is parked must not redraw
    # the graph. The conversation path refuses it in admission, before a response type is
    # chosen and before anything is written (runtime.py:330-340), so no plan and no
    # task_update can be replayed -- and the refusal names the very question the client has
    # to answer, which is the checkpoint's own statement that the pause is still that one.
    while_paused = post(client, session, "先别转账，我还想改一下")
    assert "event:" not in while_paused.text
    assert "plan_id" not in while_paused.text
    assert "task_update" not in while_paused.text
    assert while_paused.status_code == 409
    refusal = while_paused.json()["error"]
    assert refusal["code"] == "INVALID_INTERRUPT_ANSWER"
    assert refusal["details"] == {"question_id": card["question_id"]}

    resumed = frames(
        post(
            client,
            session,
            "确认",
            resume={"question_id": card["question_id"], "value": {"decision": "confirm"}},
        )
    )

    assert [name for name, _ in resumed] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "reply_delta",
        "final",
    ]
    assert_event_contract(resumed, interrupt_extra=frozenset())

    # The resumed client has no plan, so the stream opens with the same one it missed.
    assert resumed[0][1] == plan
    assert resumed[0][1]["plan_id"] == plan["plan_id"]
    assert updates(resumed) == [
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_2",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_2",
            "status": "done",
            "artifact": {"summary": DONE_SUMMARY},
            "error": None,
        },
    ]
    assert [data for name, data in resumed if name == "agent_start"] == [{"agent_id": "transfer"}]
    assert [data for name, data in resumed if name == "subtask_end"] == [
        {
            "agent_id": "transfer",
            "status": "SUCCEEDED",
            "summary": DONE_SUMMARY,
            "turn_status": "completed",
            "grounding_blocked": None,
        }
    ]
    assert (
        "".join(data["text"] for name, data in resumed if name == "reply_delta") == "两笔都办好了"
    )
    assert resumed[-1][1] == {
        "text": "两笔都办好了",
        "business_ids": [],
        "terminal_reason": None,
        "error_status": None,
    }

    assert bill.calls == [MESSAGE]
    assert bill.actions == []
    assert transfer.calls == [MESSAGE]
    assert [action.kind for action in transfer.actions] == [TurnAction.CONFIRM]
    assert transfer.actions[0].pending_id == CONFIRMATION.pending_id


def test_a_queued_handoff_reads_the_previous_domains_result(rt, session):
    """一次模型回复排好的第二个领域，拿到的是第一个领域的实际结论，不是占位符。"""

    bill = ScriptedDomain(completed(BILL_SUMMARY))
    transfer = ScriptedDomain(completed(DONE_SUMMARY))
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_BILL),
                ("call", "handoff", HANDOFF_TRANSFER_AFTER_BILL),
            ],
            ["两件事都办好了"],
        ]
    )
    client = client_for(
        rt,
        session,
        agent_for(
            model,
            domain_module(BILL, bill, request=lambda state: str(state.get("goal") or "")),
            domain_module(TRANSFER, transfer, request=lambda state: str(state.get("goal") or "")),
        ),
    )

    events = frames(post(client, session, MESSAGE))

    assert bill.calls == ["查账单"]
    assert transfer.calls == [f"按上一步结论继续：{BILL_SUMMARY}"]
    assert "{{" not in events[0][1]["nodes"][1]["label"]
    assert events[-1][1]["text"] == "两件事都办好了"


def test_task_graph_cascade_skip_on_node_error(rt, session):
    """A failed node ends the plan: its queued successor is broadcast skipped, never run.

    node_1's domain returns a failed result, so the node is closed with that summary as its
    error and node_2 is skipped before it is dispatched. The turn still ends with a final,
    because the user is owed an answer even when the work failed.
    """

    bill = ScriptedDomain(failed(FAILED_SUMMARY))
    transfer = ScriptedDomain()
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_BILL),
                ("call", "handoff", HANDOFF_TRANSFER),
            ],
            ["好的"],
        ]
    )
    client = client_for(
        rt,
        session,
        agent_for(model, domain_module(BILL, bill), domain_module(TRANSFER, transfer)),
    )

    events = frames(post(client, session, MESSAGE))

    assert [name for name, _ in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "task_update",
        "reply_delta",
        "final",
    ]
    assert_event_contract(events, interrupt_extra=frozenset())

    plan = events[0][1]
    assert_plan_frame(plan)
    assert updates(events) == [
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_1",
            "status": "running",
            "artifact": None,
            "error": None,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_1",
            "status": "failed",
            "artifact": None,
            "error": FAILED_SUMMARY,
        },
        {
            "plan_id": plan["plan_id"],
            "node_id": "node_2",
            "status": "skipped",
            "artifact": None,
            "error": None,
        },
    ]
    assert [data for name, data in events if name == "agent_start"] == [{"agent_id": "bill"}]
    assert [data for name, data in events if name == "subtask_end"] == [
        {
            "agent_id": "bill",
            "status": "FAILED",
            "summary": FAILED_SUMMARY,
            "turn_status": "failed",
            "grounding_blocked": None,
        }
    ]
    assert events[-1][1] == {
        "text": "好的",
        "business_ids": [],
        "terminal_reason": None,
        "error_status": None,
    }

    assert bill.calls == [MESSAGE]
    assert transfer.calls == []
    assert transfer.actions == []
