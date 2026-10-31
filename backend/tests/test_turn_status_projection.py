"""What a finished card turn leaves behind, and what a question with no fields becomes.

Two projections of one ending. The client sees `subtask_end.turn_status`; the Supervisor
reads `domain_results[].turn_status` back out of the checkpoint, and that is what stops it
from dispatching the same finished card turn a second time. They have to agree, and card's
`finish()` used to write neither.

The last case is the other end of a turn: a question whose field list came back empty used to
be written into the checkpoint with no fields at all, which the client renders as no input
box behind an enabled submit button. One free-text field is what makes it answerable.

The three cases after it are the same projections over the wealth channel: a turn that stops
on a confirmation card is terminal too (the click is a new turn, not a re-dispatch), the event
layer keeps its frozen uppercase vocabulary while `turn_status` stays a `TurnStatus`, and
one ending still has to report one value in both places.
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_agent_graph, build_domain_specs
from app.agents.runtime import AgentRuntime, thread_id_for_session
from app.agents.sdk import DomainOutcome, awaiting, read_answer
from app.agents.supervisor import _TURN_EVENT_STATUS, _already_concluded
from app.agents.turn import TurnStatus
from app.modules.card import MODULE
from app.modules.registry import build_registry
from app.runtime import build_tool_gateway
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from app.tools.schema import ASK_USER_TOOL_NAME
from tests.support.domain_harness import FakeBankClient, run_domain
from tests.support.scripted_model import ScriptedModel
from tests.wealth import support

CONTEXT = TrustedContext("user_0001", "sess_card", "u_zhang")
CARD = {
    "card_id": "private_card_marker",
    "user_id": "u_zhang",
    "account_id": "private_account_marker",
    "card_type": "debit",
    "card_number": "6222 **** **** 8801",
    "status": "active",
    "freeze_reason": None,
    "daily_limit_minor": 500000,
    "monthly_limit_minor": 5000000,
    "merchant_blacklist": [],
}

# The one field a question with no fields gets. Spelled out rather than imported: this is
# what a client renders, and a constant that moved would still have to be exactly this.
FREE_TEXT_FIELD = {
    "name": "answer",
    "label": "补充说明",
    "type": "text",
    "required": True,
    "options": [],
}


class CardBank(FakeBankClient):
    def __init__(self):
        super().__init__()
        self.register("/cards", [CARD])


def runtime_for(replies):
    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, CardBank()))
    model = ScriptedModel(replies)
    graph = build_agent_graph(model, gateway, CONTEXT, roster=registry.roster).compile(
        checkpointer=InMemorySaver()
    )
    return AgentRuntime(graph=graph), model, graph


def invoke(runtime, message="帮我看看我的银行卡"):
    async def collect():
        return [event async for event in runtime.invoke("sess_card", "user_0001", message, {})]

    return asyncio.run(collect())


def test_the_card_prompt_no_longer_asks_for_json():
    model = ScriptedModel([("call", "card_cards", "{}"), "已经为您查询。"])
    run_domain(MODULE, bank=CardBank(), model=model)
    prompt = model.prompts[0]
    assert "只输出一个 JSON 对象" not in prompt
    assert '{"tool"' not in prompt
    assert '"arguments"' not in prompt
    # What the prompt used to print as a JSON menu now arrives as tool definitions.
    handed = {item["function"]["name"] for item in model.tools[0]}
    assert {"card_cards", "card_freeze", ASK_USER_TOOL_NAME} <= handed
    # The two tools the model may never call are still withheld from the schema, which is
    # what makes "the model cannot authorize its own write" a property of the wiring.
    assert handed.isdisjoint({"card_execute", "card_cancel"})


def test_a_finished_card_turn_is_not_dispatched_again():
    runtime, _, graph = runtime_for(
        [
            ("call", "handoff", '{"agent_id":"card","instruction":"查询银行卡"}'),
            ("call", "card_cards", "{}"),
            "已经为您查询。",
            # A Supervisor that repeats itself: without a turn_status on card's result,
            # nothing marks the card turn as concluded and this second handoff runs again.
            ("call", "handoff", '{"agent_id":"card","instruction":"查询银行卡"}'),
            "已经为您查询。",
        ]
    )
    events = invoke(runtime)
    starts = [
        event
        for event in events
        if event.event == "agent_start" and event.data.get("agent_id") == "card"
    ]
    assert len(starts) == 1
    ended = next(event.data for event in events if event.event == "subtask_end")
    assert ended["turn_status"] == "completed"
    final = next(event.data for event in events if event.event == "final")
    assert final.get("terminal_reason") != "max_turns"
    state = asyncio.run(
        graph.aget_state({"configurable": {"thread_id": thread_id_for_session("sess_card")}})
    )
    assert [item["turn_status"] for item in state.values["domain_results"]] == [
        ended["turn_status"]
    ]


def test_an_empty_field_list_gets_one_free_text_field():
    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, CardBank()))
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]

    def writer(payload):
        return None

    # A blank name takes the same path: `resolve_fields` skips it, so the list is empty too.
    for names in ([], ["  "]):
        state = awaiting(
            spec,
            DomainOutcome(ask={"fields": names, "display": "请补充说明。"}),
            writer,
        )
        question = state["pending_question"]
        assert question["fields"] == [FREE_TEXT_FIELD]
        # The fallback field is answerable by the same envelope any other field is.
        assert read_answer(
            {"question_id": question["question_id"], "value": {"answer": "补充"}}, question
        ) == ({"answer": "补充"}, None)


# ---------------------------------------------------------------- the wealth channel
#
# These three run the real wealth graph behind the base whole-turn graph. The driver lives in
# tests/test_wealth_graph_base.py: the assembly (wealth graph + BankDouble + the base graph) is
# already there and green, and a second copy here would only give the two a chance to drift.

WEALTH_SESSION = "sess_wealth_status"
WEALTH_CONTEXT = support.bound_context(session_id=WEALTH_SESSION)
WEALTH_MESSAGE = "申购1000元演示灵活理财，6个月内不用，用a_wealth_main扣款"
WEALTH_ANSWER = "6个月内不用，不需要随时赎回"
WEALTH_HORIZON_QUESTION = "请问您计划什么时候用这笔钱？"
WEALTH_PRODUCT = "p_demo_cash"
WEALTH_ACCOUNT = "a_wealth_main"
WEALTH_AMOUNT_MINOR = 100000


def wealth_double():
    """The reads a purchase preview needs: candidates, the account and the risk assessment."""

    double = support.BankDouble()
    double.on("GET", "/products", data=support.wealth_demo_products())
    double.on("GET", f"/products/{WEALTH_PRODUCT}", data=support.product_payload())
    double.on_sequence(
        "GET",
        "/accounts",
        [support.account_payload(balance_minor=2000000, available_minor=2000000)],
    )
    double.on(
        "GET",
        "/wealth/risk-assessments/latest",
        data=support.risk_profile_payload(risk_level="low"),
    )
    return double


def wealth_runtime_for(replies):
    """The real wealth graph, with the bank and the model scripted. No HTTP, no database."""

    double = wealth_double()
    model = support.ScriptModel(*replies)
    registry = build_registry()
    gateway = build_tool_gateway(registry, client=double.client())
    graph = build_agent_graph(
        model,
        gateway,
        WEALTH_CONTEXT,
        roster=registry.roster,
        client=double.client(),
    ).compile(checkpointer=InMemorySaver())
    return AgentRuntime(graph=graph), model, graph, double


def invoke_wealth(runtime, message, envelope=None):
    async def collect():
        return [
            event async for event in runtime.invoke(WEALTH_SESSION, "user_0001", message, envelope)
        ]

    return asyncio.run(collect())


def wealth_state(graph):
    """The checkpointed wealth state, walked the way the base walks it.

    A turn that is waiting on a click has not returned to the Supervisor yet, so its
    `domain_results` entry is still in the subgraph's namespace and the parent checkpoint shows
    an empty list there. `domain_state` is the walk the wealth acceptance file uses for exactly
    that reason.
    """

    async def read():
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": thread_id_for_session(WEALTH_SESSION)}}
        )
        return await support.domain_state(graph, snapshot)

    return asyncio.run(read())


def wealth_parent_state(graph):
    async def read():
        return await graph.aget_state(
            {"configurable": {"thread_id": thread_id_for_session(WEALTH_SESSION)}}
        )

    return asyncio.run(read())


def paused_wealth_replies():
    """One turn that fills every slot and stops on the confirmation card."""

    return [
        support.call("handoff", {"agent_id": "wealth", "instruction": "买一笔"}),
        support.set_slots(
            {
                "horizon_months": 6,
                "liquidity": "flexible",
                "product_id": WEALTH_PRODUCT,
                "account_id": WEALTH_ACCOUNT,
                "amount_minor": WEALTH_AMOUNT_MINOR,
            }
        ),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {
                "product_id": WEALTH_PRODUCT,
                "account_id": WEALTH_ACCOUNT,
                "amount_minor": WEALTH_AMOUNT_MINOR,
            }
        ),
    ]


def completed_wealth_replies():
    """Two turns of an answered question: ask for the horizon, then list the candidates."""

    return [
        support.call("handoff", {"agent_id": "wealth", "instruction": "买一笔"}),
        support.ask_user(WEALTH_HORIZON_QUESTION, ["horizon_months"]),
        support.set_slots({"horizon_months": 6, "liquidity": "flexible"}),
        support.call_read("wealth_products"),
        support.finish("候选产品：演示灵活理财（p_demo_cash）、演示稳健理财（p_demo_stable）。"),
        "候选产品：演示灵活理财、演示稳健理财。",
    ]


def answer_horizon(runtime, first):
    """Answer the horizon question of the first turn, the way a client submits a field."""

    question = next(event.data for event in first if event.event == "interrupt")
    return invoke_wealth(
        runtime,
        WEALTH_ANSWER,
        {"resume": {"question_id": question["question_id"], "value": {"horizon_months": 6}}},
    )


def test_wealth_waiting_confirmation_is_not_dispatched_again():
    """A confirmation pause is a conclusion: the same handoff must not run the turn again."""

    runtime, _, graph, _ = wealth_runtime_for(paused_wealth_replies())

    events = invoke_wealth(runtime, WEALTH_MESSAGE)

    ended = next(event.data for event in events if event.event == "subtask_end")
    assert ended["agent_id"] == "wealth"
    assert ended["turn_status"] == "waiting_confirmation"
    results = wealth_state(graph)["domain_results"]
    assert [item["agent_id"] for item in results] == ["wealth"]
    assert results[0]["turn_status"] == "waiting_confirmation"
    # One dispatch, one ending: the pause ended the invocation before the Supervisor could
    # decide again, so the turn was not run a second time.
    assert [event.data["agent_id"] for event in events if event.event == "agent_start"] == [
        "wealth"
    ]
    assert [event.data["agent_id"] for event in events if event.event == "subtask_end"] == [
        "wealth"
    ]
    # supervisor/graph.py:240 asks exactly this predicate before it turns a repeated handoff
    # into the closing reply. It is agent-scoped: the wealth entry concludes, an agent that
    # never ran in this thread does not.
    assert _already_concluded(results, "wealth") is True
    assert _already_concluded(results, "card") is False


def test_turn_status_values_come_from_the_turn_status_enum():
    """The event layer keeps its frozen three uppercase values; `turn_status` is the enum."""

    card_runtime, _, _ = runtime_for(
        [
            ("call", "handoff", '{"agent_id":"card","instruction":"查询银行卡"}'),
            ("call", "card_cards", "{}"),
            "已经为您查询。",
            "已经为您查询。",
        ]
    )
    card_events = invoke(card_runtime)
    wealth_runtime, _, _, _ = wealth_runtime_for(completed_wealth_replies())
    wealth_first = invoke_wealth(wealth_runtime, "我想申购1000元理财产品")
    wealth_events = answer_horizon(wealth_runtime, wealth_first)

    seen = set()
    for events in (card_events, wealth_first, wealth_events):
        ended = next(event.data for event in events if event.event == "subtask_end")
        assert ended["status"] in {"SUCCEEDED", "FAILED", "DENIED"}
        # Not a `TurnStatus` value would raise here rather than pass silently.
        status = TurnStatus(ended["turn_status"])
        assert status.value == ended["turn_status"]
        # The uppercase value is the enum's projection, not a second spelling of the status.
        assert _TURN_EVENT_STATUS[status].value == ended["status"]
        seen.add(ended["turn_status"])
    assert seen == {"completed", "waiting_input"}
    # Guard, not evidence: these two compare the frozen mapping to the enum it is derived from,
    # so they can only fail if someone edits them apart. The live round-trip above is the
    # evidence that a real turn's wire status and `turn_status` agree.
    assert set(_TURN_EVENT_STATUS) == set(TurnStatus)
    assert {status.value for status in _TURN_EVENT_STATUS.values()} == {
        "SUCCEEDED",
        "FAILED",
        "DENIED",
    }


def test_a_wealth_turn_reports_the_same_status_to_the_supervisor():
    """One ending, two projections: the event and `domain_results` carry the same value."""

    runtime, _, graph, _ = wealth_runtime_for(completed_wealth_replies())

    first = invoke_wealth(runtime, "我想申购1000元理财产品")
    events = answer_horizon(runtime, first)

    ended = next(event.data for event in events if event.event == "subtask_end")
    # The wealth channel, not the card: the whole production roster is registered, and only
    # wealth reports in these two turns.
    assert ended["agent_id"] == "wealth"
    # The dispatch is in the first turn; the answer turn enters the wealth graph at its wait
    # node, so it has no `agent_start` of its own (that is the base's card path, not this one).
    assert [event.data["agent_id"] for event in first if event.event == "agent_start"] == ["wealth"]
    for turn in (first, events):
        assert not [event for event in turn if event.data.get("agent_id") == "card"]
    # Read the way the Supervisor reads it: straight off the parent checkpoint, which the
    # returned subgraph has already written back into.
    results = wealth_parent_state(graph).values["domain_results"]
    assert [item["agent_id"] for item in results] == ["wealth", "wealth"]
    first_ended = next(event.data for event in first if event.event == "subtask_end")
    assert [item["turn_status"] for item in results] == [
        first_ended["turn_status"],
        ended["turn_status"],
    ]
    assert results[-1]["turn_status"] == ended["turn_status"] == "completed"
    assert results[-1]["summary"] == ended["summary"]
