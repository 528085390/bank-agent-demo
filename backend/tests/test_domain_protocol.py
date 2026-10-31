"""The domain side of the tool-calling protocol (spec §4.8.2).

Six modules declare no tools today, so this path has no production traffic to test against:
everything here drives a synthetic module that does declare them, through the real registry,
the real Gateway and the real subgraph. The adapter is a recorder, so the model's decisions
are what is under test -- not a bank.
"""

from __future__ import annotations

import pytest

from app.agents.decisions import DecisionError, parse_domain_decision
from app.agents.declaration import DomainDeclaration
from app.agents.prompts import DOMAIN_PROMPT, render
from app.integration.model_client import ToolCall
from app.modules.base import ModuleSpec
from app.tools.protocol import ToolSpec
from app.tools.recording import RecordingAdapter
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema
from tests.support.domain_harness import run_domain
from tests.support.scripted_model import StreamingModel
from tests.support.tool_domain_graph import domain_user_turn, tool_domain_graph_builder

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

# No business tools at all: the shape the five undeveloped modules have today.
EMPTY_MODULE = ModuleSpec(
    name="empty_demo",
    declaration=DomainDeclaration(
        agent_id="empty_demo", display="空领域", purpose="尚未接入", fields={}
    ),
)


def test_parameters_become_a_strict_function_schema():
    schema = function_schema(CARD_READ)

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "card_read"
    assert schema["function"]["description"] == "查询卡片状态"
    assert schema["function"]["strict"] is True
    parameters = schema["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["card_no"]
    assert parameters["properties"]["card_no"] == {"type": "string"}


def test_a_declared_name_that_is_not_an_identifier_is_refused():
    """A dot in `function.name` is refused upstream (code 11152), so a declared name

    that is not an identifier has to fail here, where the message can name the tool,
    rather than as an opaque 503 once a turn is already in flight.
    """

    with pytest.raises(ValueError, match="identifier"):
        function_schema(ToolSpec(name="card.cards"))


def test_an_optional_property_is_made_nullable_and_required():
    """Strict mode has no notion of an optional property, so it becomes a required nullable one."""

    schema = function_schema(
        ToolSpec(
            name="card_list",
            parameters={
                "type": "object",
                "properties": {"status": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["limit"],
            },
        )
    )

    parameters = schema["function"]["parameters"]
    assert set(parameters["required"]) == {"status", "limit"}
    assert parameters["properties"]["status"]["type"] == ["string", "null"]
    assert parameters["properties"]["limit"]["type"] == "integer"


def test_empty_parameters_become_an_empty_object_schema():
    """The default `{}` is not a schema; sending it as one would be a malformed declaration."""

    schema = function_schema(ToolSpec(name="card_nothing"))

    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_a_declaration_strict_cannot_express_is_sent_unstrict():
    """A property with no type cannot be made nullable, so the tool is not sent as strict."""

    schema = function_schema(
        ToolSpec(name="card_raw", parameters={"type": "object", "properties": {"payload": {}}})
    )

    assert schema["function"]["strict"] is False
    assert schema["function"]["parameters"]["additionalProperties"] is False


def test_the_framework_tool_is_a_declared_tool_like_any_other():
    spec = ask_user_spec()

    assert spec.name == ASK_USER_TOOL_NAME
    schema = function_schema(spec)
    assert schema["function"]["strict"] is True
    assert set(schema["function"]["parameters"]["properties"]) == {"fields", "display"}
    assert schema["function"]["parameters"]["required"] == ["fields", "display"]


def test_a_business_tool_call_is_a_tool_decision():
    decision = parse_domain_decision(
        "",
        [ToolCall(name="card_read", arguments={"card_no": "6222"})],
        known={"card_read", ASK_USER_TOOL_NAME},
    )

    assert decision.tool_name == "card_read"
    assert decision.arguments == {"card_no": "6222"}
    assert decision.ask is None
    assert decision.summary == ""


def test_an_ask_user_call_is_a_question_not_a_tool():
    decision = parse_domain_decision(
        "",
        [
            ToolCall(
                name=ASK_USER_TOOL_NAME, arguments={"fields": ["card_no"], "display": "请提供卡号"}
            )
        ],
        known={"card_read", ASK_USER_TOOL_NAME},
    )

    assert decision.tool_name is None
    assert decision.ask == {"kind": "input", "fields": ["card_no"], "display": "请提供卡号"}
    assert decision.summary == ""


def test_plain_text_is_the_report():
    decision = parse_domain_decision("卡已挂失。", [], known={"card_read", ASK_USER_TOOL_NAME})

    assert decision.summary == "卡已挂失。"
    assert decision.tool_name is None
    assert decision.ask is None


def test_text_next_to_a_tool_call_is_narration_not_the_report():
    """The same rule the supervisor follows: the text is a preamble, the call is the decision."""

    decision = parse_domain_decision(
        "我先查一下。",
        [ToolCall(name="card_read", arguments={})],
        known={"card_read", ASK_USER_TOOL_NAME},
    )

    assert decision.tool_name == "card_read"
    assert decision.summary == ""


def test_an_unknown_tool_is_refused():
    with pytest.raises(DecisionError):
        parse_domain_decision("", [ToolCall(name="card_nope", arguments={})], known={"card_read"})


def test_an_ask_without_fields_is_refused():
    with pytest.raises(DecisionError):
        parse_domain_decision(
            "",
            [ToolCall(name=ASK_USER_TOOL_NAME, arguments={"display": "?"})],
            known={ASK_USER_TOOL_NAME},
        )


def test_an_ask_without_a_display_is_refused():
    with pytest.raises(DecisionError):
        parse_domain_decision(
            "",
            [ToolCall(name=ASK_USER_TOOL_NAME, arguments={"fields": ["card_no"]})],
            known={ASK_USER_TOOL_NAME},
        )


def test_two_tool_calls_in_one_turn_are_refused():
    """P4 is still single-shot: the loop that can hold two calls is P5 (Task 8)."""

    with pytest.raises(DecisionError):
        parse_domain_decision(
            "",
            [ToolCall(name="card_read", arguments={}), ToolCall(name="card_read", arguments={})],
            known={"card_read"},
        )


def _streaming(*decisions: list) -> StreamingModel:
    return StreamingModel(list(decisions))


def test_the_whole_chain_runs_through_a_module_that_declares_tools():
    """parameters -> function schema -> tool_call -> Gateway -> report. The only end-to-end proof."""

    model = _streaming(
        [("call", "card_read", '{"card_no": "6222"}')], ["卡片状态：正常，已为你查好。"]
    )
    adapter = RecordingAdapter(
        {"card_read": {"summary": "卡片状态：正常", "business_ids": {"card": "C-1"}}}
    )
    run = run_domain(
        CARD_MODULE,
        model=model,
        adapter=adapter,
    )

    assert adapter.tool_names == ["card_read"]
    assert adapter.invocations[0].call.arguments == {"card_no": "6222"}
    names = [schema["function"]["name"] for schema in model.tools[0]]
    assert names == ["card_read", ASK_USER_TOOL_NAME]
    assert model.tools[0][0]["function"]["strict"] is True
    assert [event["tool_name"] for event in run.events_named("tool_call")] == ["card_read"]
    assert [event["status"] for event in run.events_named("tool_result")] == ["SUCCEEDED"]
    end = run.events_named("subtask_end")[0]
    assert end["status"] == "SUCCEEDED"
    # The report is the model's own concluding turn, fed the tool's summary through the loop.
    assert "卡片状态：正常，已为你查好。" in end["summary"]


def test_a_tool_round_is_now_followed_by_a_report_call():
    """P5's loop: the tool round and the report are two model calls in one activation.

    The one-call-per-activation pin this replaces was explicitly provisional ("the loop is
    P5"); the loop has landed, and the second call is what turns the tool's summary into
    the domain's own report.
    """

    model = _streaming([("call", "card_read", '{"card_no": "6222"}')], ["卡片状态：正常。"])
    adapter = RecordingAdapter({"card_read": {"summary": "卡片状态：正常"}})
    run = run_domain(CARD_MODULE, model=model, adapter=adapter)

    assert len(model.prompts) == 2
    assert adapter.tool_names == ["card_read"]
    assert "卡片状态：正常。" in run.final_text


def test_an_ask_user_call_becomes_a_question_card():
    model = _streaming(
        [("call", ASK_USER_TOOL_NAME, '{"fields": ["card_no"], "display": "请提供卡号"}')]
    )
    run = run_domain(CARD_MODULE, model=model)

    question = run.events_named("interrupt")[0]
    assert question["agent_id"] == "card_demo"
    assert question["display"] == "请提供卡号"
    assert [field["name"] for field in question["fields"]] == ["card_no"]
    assert run.status == "WAITING_INPUT"


def test_a_domain_without_business_tools_never_reaches_the_model():
    """The gate stays on the *business* menu, and now it is stated before any model call.

    An empty menu means the capability is not connected, so the tool-less shell template
    concludes directly. The old base loop asked the model anyway and then overwrote whatever
    it answered, which is why this test used to assert about the menu the model was handed:
    now there is no call at all, so ask_user cannot even be offered. Injecting ask_user and
    judging the gate on business-or-framework tools would turn "not implemented yet" into a
    questionnaire about a capability that does not exist.
    """

    model = _streaming(["这句不会被采纳。"])
    run = run_domain(EMPTY_MODULE, model=model)

    assert model.tools == []
    assert run.prompts == []
    assert "尚未接入" in run.final_text
    assert run.status == "SUCCEEDED"


def test_the_tool_calling_prompt_does_not_talk_about_json():
    rendered = render(
        DOMAIN_PROMPT,
        display="卡片",
        agent_id="card_demo",
        instruction="查一下卡片",
        results="（暂无）",
        tools="card_read：查询卡片状态",
    )

    assert "JSON" not in rendered
    assert ASK_USER_TOOL_NAME not in rendered
    assert "card_read" in rendered


def test_the_turn_carries_no_shape():
    rendered = domain_user_turn("查一下卡片", [("card_read", "查询卡片状态")])

    assert "JSON" not in rendered
    assert "card_read" in rendered
    assert ASK_USER_TOOL_NAME not in rendered
