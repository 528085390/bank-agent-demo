"""The harness a module team uses to prove its own tools, without the Supervisor.

A fictional domain is used throughout: the harness knows no business noun, so what it
proves is the wiring, not any one module's behaviour.
"""

import pytest

from app.agents.decisions import DecisionError
from app.agents.declaration import DomainDeclaration, FieldMeta
from app.integration.bank_client import BankResponse
from app.modules.base import ModuleSpec, ToolData, ToolFailure
from app.tools.protocol import SideEffect, ToolSpec
from tests.support.domain_harness import FakeBankClient, field_names, run_domain
from tests.support.tool_domain_graph import tool_domain_graph_builder

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

TRANSFER = ToolSpec(
    name="transfer",
    description="Move money to another account",
    side_effect=SideEffect.WRITE,
    parameters={
        "type": "object",
        "properties": {"amount_minor": {"type": "integer"}},
        "required": ["amount_minor"],
        "additionalProperties": False,
    },
)


def echo_handler(invocation, client):
    return ToolData(
        summary=f"echoed {invocation.call.arguments['value']}",
        display={"value": invocation.call.arguments["value"]},
        facts={"value": invocation.call.arguments["value"]},
        business_ids={"echo_id": "e1"},
    )


def module(*, tools=None, handlers=None):
    resolved_tools = tools if tools is not None else {"echo": ECHO}
    resolved_handlers = handlers if handlers is not None else {"echo": echo_handler}
    # A domain with business tools owns its graph; a tool-less one runs the minimal shell
    # template. This helper drives the generic loop, so it declares the test-support builder
    # exactly when there is a tool round to drive.
    declaration = DomainDeclaration(
        agent_id="alpha",
        display="Alpha",
        purpose="测试领域",
        graph_builder=tool_domain_graph_builder if resolved_tools else None,
    )
    return ModuleSpec(
        name="alpha",
        declaration=declaration,
        tools=resolved_tools,
        handlers=resolved_handlers,
    )


def test_a_tool_call_is_published_in_order_with_its_result():
    run = run_domain(
        module(),
        replies=[("call", "echo", '{"value":"hi"}'), "done"],
    )

    assert [event.event for event in run.events] == [
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
    ]
    assert run.events[1].data["tool_name"] == "echo"
    assert run.events[1].data["arguments"] == {"value": "hi"}
    assert run.events[2].data["status"] == "SUCCEEDED"
    assert run.status == "SUCCEEDED"


def test_the_modules_own_handler_is_what_runs():
    """A recording adapter would answer from a fixed table and never call this.

    The handler's own wording now surfaces on the tool_result frame: the loop's following
    report is what ends the subtask, so the tool's summary no longer rides `subtask_end`.
    """

    run = run_domain(
        module(),
        replies=[("call", "echo", '{"value":"hi"}'), "done"],
    )

    assert "echoed hi" in run.events_named("tool_result")[0]["data"]["summary"]
    assert run.events_named("tool_result")[0]["data"]["business_ids"] == {"echo_id": "e1"}


def test_the_domain_prompt_advertises_exactly_the_tools_the_module_declared():
    run = run_domain(module(), replies=["done"])

    assert "echo: Return what it was given" in run.prompts[0]


def test_a_tool_the_module_never_declared_is_refused_before_the_bank():
    """The module's own menu is the model's whole menu.

    There is no schema for `ghost`, so the model was never offered one: the name is refused
    as a protocol error before any tool runs, and the bank never hears about it.
    """

    bank = FakeBankClient()
    with pytest.raises(DecisionError, match="未知工具"):
        run_domain(module(), replies=[("call", "ghost", "{}")], bank=bank)

    assert bank.requests == []


def test_a_domain_with_no_tools_concludes_instead_of_pausing():
    run = run_domain(
        module(tools={}, handlers={}),
        replies=[("call", "ask_user", '{"fields":["x"],"display":"请提供 x"}')],
    )

    # No `final`: that event is the Supervisor's own reply, and this harness drives the
    # domain subgraph alone. What this proves is that the domain concluded -- SUCCEEDED, with
    # a summary -- instead of parking on a question no tool could have answered.
    assert [event.event for event in run.events] == ["agent_start", "subtask_end"]
    assert run.events[1].data["status"] == "SUCCEEDED"


def test_a_question_pauses_the_domain():
    run = run_domain(
        module(),
        replies=[("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'), "done"],
    )

    assert "interrupt" in [event.event for event in run.events]


def test_field_names_reads_the_question_whichever_shape_it_is_in():
    run = run_domain(
        module(),
        replies=[("call", "ask_user", '{"fields":["value","extra"],"display":"请提供"}'), "done"],
    )

    interrupt = next(event for event in run.events if event.event == "interrupt")

    assert field_names(interrupt.data) == ["value", "extra"]


def test_a_failing_handler_is_published_as_failed_and_the_report_ends_the_domain():
    """The FAILED attempt stays on the activity stream; the loop's report is the ending.

    Before the activation loop, the tool call's failure was the subtask's own ending. Now
    the failure goes back to the model, whose report concludes -- so the subtask ends with
    the report, and the FAILED status is what the tool_result frame carries.
    """

    def broken(invocation, client):
        raise RuntimeError("boom")

    run = run_domain(
        module(handlers={"echo": broken}),
        replies=[("call", "echo", '{"value":"x"}'), "办砸了"],
    )

    assert run.events[2].data["status"] == "FAILED"
    assert run.status == "SUCCEEDED"
    assert run.events_named("subtask_end")[0]["summary"] == "办砸了"


def test_the_harness_hands_the_module_the_callers_customer():
    seen = {}

    def handler(invocation, client):
        seen["customer"] = invocation.context.bank_customer_id
        return ToolData(summary="ok", display={}, facts={})

    run_domain(
        module(handlers={"echo": handler}),
        replies=[("call", "echo", '{"value":"x"}'), "done"],
    )

    assert seen["customer"] == "u_zhang"


def test_a_handler_that_asks_the_bank_records_exactly_what_it_asked_for():
    """A read. It sends no idempotency key, and that is honest for a read -- the write
    example below is the one to copy for a tool that changes something."""

    def handler(invocation, client):
        client.request("GET", "/accounts/balance", user_id=invocation.context.bank_customer_id)
        return ToolData(summary="ok", display={}, facts={})

    bank = FakeBankClient(
        {"/accounts/balance": BankResponse(code=0, message="success", data={"balance_minor": 100})}
    )
    run = run_domain(
        module(handlers={"echo": handler}),
        replies=[("call", "echo", '{"value":"x"}'), "done"],
        bank=bank,
    )

    assert bank.requests == [
        {
            "method": "GET",
            "path": "/accounts/balance",
            "user_id": "u_zhang",
            "params": {},
            "json": None,
            "idempotency_key": None,
        }
    ]
    assert run.bank_requests == bank.requests


def test_a_write_handler_forwards_the_idempotency_key_and_reuses_it_on_a_retry():
    """The half the read example above cannot show, and the reason this test exists.

    A read may legitimately send no key, so `idempotency_key: None` there is honest. A write
    is different: the Supervisor mints one key per intent, outside its retry loop, and the
    bank only sees one transfer if the *handler* passes `invocation.call.idempotency_key` on
    to `client.request`. Nothing does it for the module -- `BankToolAdapter` never reads the
    key, it only refuses a write that arrives without one -- so this call is the line every
    module's write handler has to copy.
    """

    attempts = {"n": 0}

    def handler(invocation, client):
        client.request(
            "POST",
            "/transfers",
            user_id=invocation.context.bank_customer_id,
            idempotency_key=invocation.call.idempotency_key,
            json={"amount_minor": invocation.call.arguments["amount_minor"]},
        )
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Transient, so the Supervisor retries -- the second attempt is the same intent
            # and therefore has to carry the same key.
            raise ToolFailure(
                "UPSTREAM_UNAVAILABLE", "银行服务暂时不可用，请稍后重试。", retryable=True
            )
        return ToolData(
            summary="转账已受理。", display={}, facts={}, business_ids={"transfer_id": "t1"}
        )

    bank = FakeBankClient(
        {"/transfers": BankResponse(code=0, message="success", data={"transfer_id": "t1"})}
    )
    run = run_domain(
        module(tools={"transfer": TRANSFER}, handlers={"transfer": handler}),
        replies=[("call", "transfer", '{"amount_minor":100}'), "done"],
        bank=bank,
    )

    keys = [request["idempotency_key"] for request in bank.requests]
    # Positive control: the retry really happened, so "the keys match" cannot be vacuously
    # true of a single request.
    assert len(keys) == 2, "the retry never happened, so this proves nothing about the key"
    assert keys[0] is not None and keys[0] == keys[1]
    assert run.status == "SUCCEEDED"


def test_an_unbound_customer_never_reaches_the_bank():
    """The direction that matters: no binding means no request, not the bank's default."""

    def handler(invocation, client):
        if not invocation.context.bank_customer_id:
            raise ToolFailure("NO_CUSTOMER_BINDING", "当前用户未绑定银行客户号")
        client.request("GET", "/accounts/balance", user_id=invocation.context.bank_customer_id)
        return ToolData(summary="ok", display={}, facts={})

    bank = FakeBankClient()
    run = run_domain(
        module(handlers={"echo": handler}),
        replies=[("call", "echo", '{"value":"x"}'), "办不了"],
        bank=bank,
        customer=None,
    )

    assert bank.requests == []
    assert run.events_named("tool_result")[0]["status"] == "FAILED"
    assert run.events_named("tool_result")[0]["error"] == "当前用户未绑定银行客户号"


def test_an_unregistered_bank_path_fails_loudly_instead_of_returning_nothing():
    """The loud failure is the FAILED tool_result frame; the report still ends the subtask."""

    def handler(invocation, client):
        client.request("GET", "/accounts/balance", user_id=invocation.context.bank_customer_id)
        return ToolData(summary="ok", display={}, facts={})

    run = run_domain(
        module(handlers={"echo": handler}),
        replies=[("call", "echo", '{"value":"x"}'), "路径没注册"],
    )

    assert run.events_named("tool_result")[0]["status"] == "FAILED"


def test_a_declared_field_reaches_the_client_with_its_label():
    """The model names a field; the module is what says how it looks to a user."""

    declaration = DomainDeclaration(
        agent_id="alpha",
        display="Alpha",
        purpose="测试领域",
        fields={"value": FieldMeta(label="值", type="number", required=False)},
        graph_builder=tool_domain_graph_builder,
    )
    spec = ModuleSpec(
        name="alpha",
        declaration=declaration,
        tools={"echo": ECHO},
        handlers={"echo": echo_handler},
    )

    run = run_domain(
        spec,
        replies=[("call", "ask_user", '{"fields":["value"],"display":"请提供"}'), "done"],
    )
    interrupt = next(event for event in run.events if event.event == "interrupt")

    assert interrupt.data["fields"] == [
        {"name": "value", "label": "值", "type": "number", "required": False, "options": []}
    ]


def test_an_undeclared_field_still_reaches_the_client():
    """Declaring fields is incremental: an undeclared one still renders and is answerable."""

    run = run_domain(
        module(),
        replies=[("call", "ask_user", '{"fields":["mystery"],"display":"请提供"}'), "done"],
    )
    interrupt = next(event for event in run.events if event.event == "interrupt")

    assert interrupt.data["fields"] == [
        {"name": "mystery", "label": "mystery", "type": "text", "required": True, "options": []}
    ]


def test_answering_a_question_continues_the_same_run_into_the_tool():
    """The answer-to-tool leg: the half a module team most needs to prove.

    It has to run on one checkpointer, because an answer can only bind to a question this
    thread actually asked.
    """

    run = run_domain(
        module(),
        replies=[
            ("call", "ask_user", '{"fields":["value"],"display":"请提供"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "done",
        ],
        answer={"value": "from-user"},
    )

    assert [event.event for event in run.events] == [
        "agent_start",
        "interrupt",
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
    ]
    assert run.status == "SUCCEEDED"


def test_answering_without_a_question_is_a_harness_error():
    """Silently ignoring the answer would make a module's test pass while proving nothing."""

    with pytest.raises(AssertionError, match="never asked a question"):
        run_domain(
            module(),
            replies=[("call", "echo", '{"value":"x"}'), "done"],
            answer={"value": "from-user"},
        )
