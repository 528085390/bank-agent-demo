"""The domain loop (spec §4.8.3, P5).

A domain activation runs the model, runs a tool, feeds the result back and runs the model
again -- all inside one node visit. Asking the user is a return from that loop, and the wait
node that interrupts never touches the model. Both halves of that sentence are load-bearing:
the first keeps a multi-step business request from costing one Supervisor round trip per step,
the second is the only reason a parked question survives being replayed.
"""

from __future__ import annotations

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_domain_specs
from app.agents.sdk import MAX_ASK_ATTEMPTS, MAX_TOOL_ROUNDS, DomainOutcome, awaiting
from app.agents.state import idle_work_state
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.domain_harness import CUSTOMER, SESSION, USER, run_domain
from tests.support.scripted_model import StreamingModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

READ = ("call", "card_read", '{"card_no": "6222"}')
ASK = ("call", "ask_user", '{"fields": ["card_no"], "display": "请提供卡号"}')
CARD_OK = {"card_read": {"summary": "卡片状态：正常", "business_ids": {"card": "C-1"}}}

CARD_READ = ToolSpec(
    name="card_read",
    description="查询卡片状态",
    parameters={
        "type": "object",
        "properties": {"card_no": {"type": "string"}},
        "required": ["card_no"],
    },
)


def _handler(invocation, bank):  # pragma: no cover - the recorder answers, not the handler
    raise AssertionError("the recording adapter answers; a handler must not be reached")


CARD_MODULE = ModuleSpec(
    name="card_demo",
    declaration=DomainDeclaration(
        agent_id="card_demo",
        display="卡片",
        purpose="卡片状态查询",
        fields={},
        graph_builder=tool_domain_graph_builder,
    ),
    tools={"card_read": CARD_READ},
    handlers={"card_read": _handler},
)

# `_awaiting` only reads the declaration, but the spec is built the way the harness builds
# one -- through the real registry and gateway -- so the test never hand-rolls a handle.
_registry = build_registry([CARD_MODULE])
CARD_SPEC = build_domain_specs(
    ToolGateway(_registry.catalog, RecordingAdapter(dict(CARD_OK))),
    TrustedContext(user_id=USER, session_id=SESSION, bank_customer_id=CUSTOMER),
    roster=_registry.roster,
)[0]


def test_two_tool_calls_run_inside_one_activation():
    model = StreamingModel([[READ], [READ], ["两件事都办好了。"]])
    adapter = RecordingAdapter(dict(CARD_OK))
    run = run_domain(CARD_MODULE, model=model, adapter=adapter)

    assert len(model.messages) == 3
    assert adapter.tool_names == ["card_read", "card_read"]
    assert len(run.events_named("tool_call")) == 2
    assert len(run.events_named("tool_result")) == 2
    assert run.status == ToolStatus.SUCCEEDED.value
    assert "两件事都办好了。" in run.final_text


def test_the_second_call_sees_the_first_result():
    """The point of the loop: the model is handed what the tool said, not asked to guess."""

    model = StreamingModel([[READ], ["卡片正常。"]])
    run_domain(
        CARD_MODULE,
        model=model,
        adapter=RecordingAdapter(dict(CARD_OK)),
    )

    second = model.messages[1]
    assert any("卡片状态：正常" in str(message.get("content")) for message in second)
    assert any(message.get("role") == "assistant" for message in second)


def test_an_ask_user_leaves_the_loop_and_the_answer_resumes_it():
    model = StreamingModel([[READ], [ASK], ["已挂失。"]])
    adapter = RecordingAdapter(dict(CARD_OK))
    run = run_domain(
        CARD_MODULE,
        model=model,
        adapter=adapter,
        answer={"card_no": "6222"},
    )

    assert len(run.events_named("interrupt")) == 1
    assert len(model.messages) == 3
    # The resumed call must still see the result the tool returned before the question.
    assert any("卡片状态：正常" in str(message.get("content")) for message in model.messages[2])
    assert run.status == ToolStatus.SUCCEEDED.value


def test_the_wait_node_never_calls_the_model():
    """The invariant, observed: nothing runs between the interrupt and the resume."""

    model = StreamingModel([[ASK], ["已挂失。"]])
    run_domain(
        CARD_MODULE,
        model=model,
        adapter=RecordingAdapter(dict(CARD_OK)),
        answer={"card_no": "6222"},
    )

    assert len(model.messages) == 2


def test_the_loop_stops_at_the_round_limit_without_raising():
    model = StreamingModel([[READ]] * (MAX_TOOL_ROUNDS + 1) + [["不会走到这里。"]])
    adapter = RecordingAdapter(dict(CARD_OK))
    run = run_domain(CARD_MODULE, model=model, adapter=adapter)

    assert len(adapter.tool_names) == MAX_TOOL_ROUNDS
    assert len(model.messages) == MAX_TOOL_ROUNDS + 1
    assert run.status == ToolStatus.SUCCEEDED.value
    assert "上限" in run.final_text
    assert "卡片状态：正常" in run.final_text


def test_a_narration_inside_the_loop_is_never_forwarded():
    """N2: the loop's chatter belongs to the domain thread, not to the user's stream."""

    model = StreamingModel([["我先查一下。", READ], ["办好了。"]])
    run = run_domain(
        CARD_MODULE,
        model=model,
        adapter=RecordingAdapter(dict(CARD_OK)),
    )

    for event in run.events:
        assert "我先查一下" not in str(event.data)


def test_a_finished_activation_clears_the_thread():
    model = StreamingModel([[READ], ["办好了。"], ["再查一次：正常。"]])
    run_domain(
        CARD_MODULE,
        model=model,
        adapter=RecordingAdapter(dict(CARD_OK)),
        again=True,
    )

    second_activation = model.messages[2]
    assert not any("卡片状态：正常" in str(message.get("content")) for message in second_activation)


def test_asking_does_not_spend_the_tool_budget():
    """Three budgets, three counters: a question is not a tool round, and vice versa."""

    state = awaiting(
        CARD_SPEC,
        DomainOutcome(ask={"kind": "input", "fields": ["card_no"], "display": "请提供卡号"}),
        lambda event: None,
        attempt=1,
        domain_thread=[{"role": "user", "content": "查卡"}],
        tool_rounds=4,
    )

    assert state["tool_rounds"] == 4
    assert state["domain_thread"] == [{"role": "user", "content": "查卡"}]
    assert state["pending_question"]["attempt"] == 1


def test_the_round_counter_survives_a_question():
    model = StreamingModel([[READ], [ASK], ["办好了。"]])
    adapter = RecordingAdapter(dict(CARD_OK))
    run = run_domain(
        CARD_MODULE,
        model=model,
        adapter=adapter,
        answer={"card_no": "6222"},
    )

    # One tool round before the question, and the budget after the question is 6 - 1, not 6.
    assert len(adapter.tool_names) == 1
    assert run.status == ToolStatus.SUCCEEDED.value


def test_the_ask_budget_is_still_three():
    """Three questions are answered; the fourth is refused and ends the domain as a failure."""

    model = StreamingModel(
        [[("call", "ask_user", '{"fields": ["card_no"], "display": "请提供卡号"}')]]
        * (MAX_ASK_ATTEMPTS + 1)
    )
    run = run_domain(
        CARD_MODULE,
        model=model,
        adapter=RecordingAdapter(dict(CARD_OK)),
        answer=[{"card_no": "6222"}] * MAX_ASK_ATTEMPTS,
    )

    assert run.status == ToolStatus.FAILED.value
    assert len(run.events_named("interrupt")) == MAX_ASK_ATTEMPTS
    assert "未能获得" in run.final_text


def test_the_thread_and_the_counter_are_cleared_together():
    cleared = idle_work_state()

    assert cleared["domain_thread"] == []
    assert cleared["tool_rounds"] == 0
