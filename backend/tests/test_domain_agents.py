"""The six domain Agents: graph construction, tool isolation, and the event contract.

Nothing here reaches a bank. The tools are invented names with invented arguments, and
the adapter only echoes what a test registered, because what this phase proves is the
orchestration boundary -- not a business result.
"""

import asyncio
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.decisions import DecisionError
from app.agents.declaration import DomainDeclaration, DomainSpec, FieldMeta
from app.agents.domain import DOMAIN_ROSTER, build_agent_graph, build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import MAX_ASK_ATTEMPTS, MAX_DOMAIN_TURNS, NO_TOOLS_SUMMARY
from app.core.errors import AppError
from app.integration.model_client import ModelClientError
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolResult, ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.domain_harness import field_names, generic_roster, without_fields
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
SESSION = "s_1"
USER = "u_1"

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
PING = ToolSpec(name="ping", description="Answer without arguments")


def compiled(
    *, grants=None, replies, replies_for=None, roster=None, adapter=None, model=None
):
    """A compiled Supervisor graph plus the model script and the recording adapter."""

    recorder = adapter or RecordingAdapter(replies_for or {})
    gateway = ToolGateway(ToolCatalog.build([ECHO, PING], grants or {}), recorder)
    model = model or ScriptedModel(replies)
    graph = build_agent_graph(model, gateway, CONTEXT, roster=roster or generic_roster())
    return graph.compile(checkpointer=InMemorySaver()), model, recorder


def collect(graph, message="帮我", context=None):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke(SESSION, USER, message, context or {})]

    return asyncio.run(run())


def domain_prompts(model):
    """Only the domain Agents' turns. A Supervisor prompt has a different preamble.

    The marker is the DOMAIN_PROMPT's own words ("办事 Agent"): the JSON shapes the old
    template carried are gone now, so the old marker would match nothing and every
    assertion built on this selector would silently rot.
    """

    return [prompt for prompt in model.prompts if "办事 Agent" in prompt]


def domain_calls(model):
    """The domain's model calls with everything each one was handed.

    Since the activation loop, the answers and the tool results ride the domain *thread*,
    not the system prompt -- so an assertion about what the model saw has to read whole
    messages, not just the first one.
    """

    return [call for call in model.messages if "办事 Agent" in call[0]["content"]]


def domain_tools(model):
    """The tool lists the domain's own calls were handed, aligned with `domain_calls`.

    The menu a domain is offered *is* the set of names the protocol accepts, so a test about
    the capability boundary reads it here rather than restating the resolver.
    """

    return [
        tools
        for call, tools in zip(model.messages, model.tools, strict=True)
        if "办事 Agent" in call[0]["content"]
    ]


def transfer_with_an_optional_note():
    """A roster whose `note` field is declared optional, so the UI lets it stay blank."""

    return (
        DomainDeclaration(
            agent_id="transfer",
            display="转账汇款",
            purpose="转账汇款、查询转账记录",
            fields={"note": FieldMeta(label="备注", required=False)},
            graph_builder=tool_domain_graph_builder,
        ),
    )


def test_the_roster_is_exactly_the_six_named_domains():
    assert [declaration.agent_id for declaration in DOMAIN_ROSTER] == [
        "transfer",
        "bill",
        "wealth",
        "card",
        "subscription",
        "life_event",
    ]


def test_the_supervisor_compiles_with_every_domain_as_a_subgraph_node():
    graph, _model, _recorder = compiled(replies=["你好"])

    nodes = graph.get_graph().nodes
    for declaration in DOMAIN_ROSTER:
        assert declaration.agent_id in nodes
    assert "supervisor" in nodes


def test_the_first_phase_declares_no_business_tools():
    """The agents exist; the business menu deliberately does not."""

    gateway = ToolGateway(ToolCatalog.build([ECHO, PING], {}), RecordingAdapter())
    for spec in build_domain_specs(gateway, CONTEXT):
        assert spec.handle.tools == ()
        assert spec.tool_menu() == []


def test_a_granted_tool_reaches_the_agent_prompt_but_never_the_supervisor():
    graph, model, recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            # The loop feeds the tool's summary back, and the model's report concludes.
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "v1"}},
    )

    collect(graph)

    assert "echo" in model.prompts[1]
    assert "echo" not in model.prompts[0]
    assert recorder.tool_names == ["echo"]


def test_a_domain_cannot_call_a_tool_granted_to_another_domain():
    """`bill` carries `ping`; `echo` is not in the menu `bill` is handed.

    Under native tool calling the schema list *is* the menu, so the other domain's tool is
    not merely refused -- it is never offered, and naming it fails as a protocol error
    before the gateway is consulted. `bill` has to carry *some* tool for this to be
    reachable: with a completely empty menu the request is turned into a capability
    conclusion before the model is asked, and that boundary is covered by
    `test_an_empty_tool_menu_also_concludes_on_a_tool_request`.
    """

    graph, model, recorder = compiled(
        grants={"transfer": ["echo"], "bill": ["ping"]},
        replies=[
            ("call", "handoff", '{"agent_id":"bill","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
        ],
        replies_for={"echo": {"value": "v1"}},
    )

    with pytest.raises(DecisionError, match="未知工具"):
        collect(graph)

    # The domain's own menu is what the model was handed; the other domain's tool is absent.
    offered = {tool["function"]["name"] for call in model.tools for tool in call}
    assert "ping" in offered
    assert "echo" not in offered
    assert recorder.invocations == []


@pytest.mark.parametrize("declaration", DOMAIN_ROSTER, ids=lambda item: item.agent_id)
def test_each_domain_can_be_selected_and_reports_its_own_events(declaration):
    graph, _model, _recorder = compiled(
        replies=[
            (
                "call",
                "handoff",
                f'{{"agent_id":"{declaration.agent_id}","instruction":"处理一下"}}',
            ),
            "已经处理好了",
        ],
    )

    events = collect(graph)

    assert [event.data["agent_id"] for event in events if event.event == "agent_start"] == [
        declaration.agent_id
    ]
    assert [event.data["agent_id"] for event in events if event.event == "subtask_end"] == [
        declaration.agent_id
    ]
    assert [event.data["text"] for event in events if event.event == "final"] == ["已经处理好了"]


def test_a_tool_call_publishes_the_call_then_its_verified_result():
    graph, _model, recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            "echo 已完成",
            "完成",
        ],
        # The shape a real handler must produce: the seam requires `ToolData`, and a bare
        # `{"value": "v1"}` is a reply no real module makes. It would also be projected away
        # before reaching the client, since none of the envelope's fields would be present.
        #
        # `display` and `facts` carry deliberately different values. The frame assertion
        # below is what proves `display` is forwarded and `facts` is not; giving the two
        # fields the same value would make "display forwarded" and "facts forwarded under
        # the `display` key" indistinguishable, which is the one thing it exists to tell
        # apart.
        replies_for={
            "echo": {
                "summary": "echoed v1",
                "display": {"value": "shown-to-the-user"},
                "facts": {"value": "PRIVATE_FACT_4b7c"},
                "business_ids": {"echo_id": "e1"},
            }
        },
    )

    events = collect(graph)

    calls = [event for event in events if event.event == "tool_call"]
    results = [event for event in events if event.event == "tool_result"]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0].data["agent_id"] == "transfer"
    assert calls[0].data["tool_name"] == "echo"
    assert calls[0].data["arguments"] == {"value": "v1"}
    # The id is this call's own; the result names the same one so the two can be paired.
    assert calls[0].data["call_id"]
    assert results[0].data["call_id"] == calls[0].data["call_id"]
    assert results[0].data["status"] == ToolStatus.SUCCEEDED.value
    # The frame carries the envelope's three public fields and nothing else: the whole
    # envelope used to be forwarded, which is what put a module's private `facts` in front
    # of the browser. An exact comparison is what pins the absence of the fourth key -- and
    # because `display` and `facts` hold different values, it also pins that the `display`
    # key carries the *display* value rather than the facts one.
    assert results[0].data["data"] == {
        "summary": "echoed v1",
        "display": {"value": "shown-to-the-user"},
        "business_ids": {"echo_id": "e1"},
    }
    assert "PRIVATE_FACT_4b7c" not in repr(results[0].data)
    assert recorder.tool_names == ["echo"]


def test_a_failed_tool_is_reported_and_the_turn_still_ends():
    recorder = RecordingAdapter()
    recorder.fail("echo", "upstream unavailable")
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            # The failure is fed back; the model reports and ends the activation.
            "echo 未能完成",
            "未能完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    results = [event for event in events if event.event == "tool_result"]
    assert results[0].data["status"] == ToolStatus.FAILED.value
    assert results[0].data["error"] == "upstream unavailable"
    assert [event.data["text"] for event in events if event.event == "final"] == ["未能完成"]


def test_a_domain_asks_the_user_and_the_replay_keeps_one_stable_call():
    recorder = RecordingAdapter({"echo": {"value": "v1"}})
    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            # No second ask: the pause replays `wait`, which reads the question back from
            # the checkpoint instead of asking the model again.
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        adapter=recorder,
    )

    paused = collect(graph, "帮我")
    resumed = collect(graph, "value=from-user", {"resume": {"value": {"value": "from-user"}}})

    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    # Which fields were asked for is the assertion; how they are described is not this test's
    # subject, and asserting the whole metadata block here would restate the resolver.
    assert field_names(paused[4].data) == ["value"]
    assert without_fields(paused[4].data) == {
        "kind": "input",
        "question_id": paused[4].data["question_id"],
        "agent_id": "transfer",
        "display": "请提供 value",
        "attempt": 1,
    }
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
    resumed_tool_call = next(event.data for event in resumed if event.event == "tool_call")
    # Exactly one adapter call: the replay re-enters `wait`, which calls nothing, and the
    # tool runs once, on the answer.
    assert recorder.tool_names == ["echo"]
    assert recorder.invocations[0].call.call_id == resumed_tool_call["call_id"]
    # Three domain model calls: the question, the answer's tool round, and the report that
    # ends the activation. The pause itself costs nothing, because the node that interrupts
    # never calls the model.
    assert len(domain_prompts(model)) == 3


def test_the_supervisor_stops_delegating_after_its_bound():
    """A model that never concludes still gets an answer instead of an abort."""

    cycles = [
        (("call", "handoff", '{"agent_id":"transfer","instruction":"再来一次"}'), "处理过了"),
    ] * (MAX_DOMAIN_TURNS + 1)
    graph, _model, _recorder = compiled(
        # A granted tool keeps the domain's own summary, so this loop is bounded by
        # MAX_DOMAIN_TURNS rather than cut by the repeated-conclusion guard. With the empty
        # catalog every domain summary becomes the capability boundary, and that ending is
        # covered by `test_a_repeated_capability_boundary_ends_the_loop_early`.
        grants={"transfer": ["echo"]},
        replies=[item for pair in cycles for item in pair],
    )

    events = collect(graph)

    finals = [event.data["text"] for event in events if event.event == "final"]
    # Four laps, one conclusion: the repeats collapse and no agent id reaches the user.
    assert finals and finals[-1] == "处理过了"
    assert len([event for event in events if event.event == "agent_start"]) == MAX_DOMAIN_TURNS


def test_a_repeated_capability_boundary_ends_the_loop_early():
    """A domain that has already said it cannot act is not asked the same thing twice.

    The Supervisor reads the capability boundary as work still to do and delegates to `card`
    again: one request became four laps around `card`, and the four identical conclusions then
    reached the user as one wall of repeated prose. The conclusion is already the answer, so the
    turn ends there. The tool-less template spends no model call of its own, so the script holds
    the two Supervisor decisions and nothing else.
    """

    graph, _model, _recorder = compiled(
        replies=[
            ("call", "handoff", '{"agent_id":"card","instruction":"查余额"}'),
            ("call", "handoff", '{"agent_id":"card","instruction":"查余额"}'),
        ],
    )

    events = collect(graph, "查看一下余额")

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "final",
    ]
    assert events[-1].data["text"] == NO_TOOLS_SUMMARY


def test_a_second_turn_reads_the_first_turn_from_the_same_thread():
    graph, model, _recorder = compiled(
        replies=["第一轮", "第二轮"],
    )

    collect(graph, "你好")
    collect(graph, "再见")

    assert "你好" in model.prompts[1] and "第一轮" in model.prompts[1]
    assert "再见" in model.prompts[1]


def test_each_domain_spec_carries_its_own_handle():
    gateway = ToolGateway(
        ToolCatalog.build([ECHO], {"transfer": ["echo"], "bill": ["echo"]}), RecordingAdapter()
    )

    specs = build_domain_specs(gateway, CONTEXT)
    transfer = next(spec for spec in specs if spec.agent_id == "transfer")
    bill = next(spec for spec in specs if spec.agent_id == "bill")

    assert transfer.handle is not bill.handle
    assert transfer.handle.trusted_context == bill.handle.trusted_context == CONTEXT


def test_a_domain_declaration_refuses_a_blank_identity():
    with pytest.raises(ValueError):
        DomainDeclaration(agent_id="  ", display="空", purpose="无")


def test_a_declaration_does_not_carry_tools():
    """Tools have one source of truth: the module. A second field on the declaration
    would drift from it, and nothing would notice -- `tool_menu()` reads the handle."""

    assert not hasattr(DomainDeclaration(agent_id="a", display="A", purpose="p"), "tools")


def test_a_declaration_rejects_an_unknown_keyword():
    """A deleted field must not be silently reintroducible by a caller."""

    with pytest.raises(TypeError):
        DomainDeclaration(agent_id="a", display="A", purpose="p", tools=())


def test_a_replayed_pause_does_not_ask_the_model_for_the_question_again():
    """The regression the node split exists for.

    LangGraph replays the node that interrupted. That node is now `wait`, which reads the
    question back from the checkpoint, so the replay costs no model call -- and, more to
    the point, cannot come back with a different question than the one the user answered.
    The old single-node version re-decided here, which is what mismatched question and
    answer and then failed the whole request.
    """

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    paused = collect(graph, "帮我")
    calls_at_pause = len(domain_prompts(model))
    resumed = collect(graph, "补充", {"resume": {"value": {"value": "from-user"}}})

    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    # Asking cost one call. The replay cost none; `process` spent the other two on the
    # tool round and the report that ends the activation.
    assert calls_at_pause == 1
    assert len(domain_prompts(model)) == 3
    assert [event.event for event in resumed][-1] == "final"


def test_the_answer_is_given_to_the_model_as_json_beside_its_question():
    """The answer is bound to the question that was asked, as parseable JSON.

    Since the activation loop, "beside" means adjacent in the domain thread: the model's
    own question (the assistant turn it spoke) is immediately followed by the user's
    answer, so the model cannot mistake the answer for some other field.
    """

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    collect(graph, "帮我")
    collect(graph, "补充", {"resume": {"value": {"value": "from-user"}}})

    # The call that ends on the answer is the tool decision it drove; the call after it (the
    # report) ends on the tool's envelope instead.
    resumed = next(
        call for call in domain_calls(model) if '"user_answer"' in str(call[-1].get("content"))
    )
    # The answer is the thread's last turn, and it is legal JSON carrying only the answer.
    answer_turn = resumed[-1]
    assert answer_turn["role"] == "user"
    assert json.loads(answer_turn["content"]) == {"user_answer": {"value": "from-user"}}
    # The turn before it is the model's own question.
    ask_turn = resumed[-2]
    assert ask_turn["role"] == "assistant"
    assert '"display": "请提供 value"' in ask_turn["content"]
    # Legal JSON, not the Python repr the old prompt embedded.
    assert "'value'" not in answer_turn["content"]


def test_a_second_question_is_a_normal_step_not_a_failure():
    """Asking twice used to raise `DecisionError` and fail the request. It is now a step."""

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "ask_user", '{"fields":["extra"],"display":"还需要 extra"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    first = collect(graph, "帮我")
    second = collect(graph, "补充", {"resume": {"value": {"value": "from-user"}}})
    third = collect(graph, "补充", {"resume": {"value": {"extra": "e"}}})

    assert [event.event for event in first] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    assert field_names(first[4].data) == ["value"]
    # The second question is a fresh pause, not an error, and a resume rebuilds no graph.
    assert [event.event for event in second] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    assert field_names(second[4].data) == ["extra"]
    assert [event.event for event in third][-1] == "final"
    # The answer to the second question was given both answers, so nothing was lost: the
    # thread the tool round saw carries the first answer's turn and the second one.
    visible = "".join(str(message.get("content")) for message in domain_calls(model)[-1])
    assert '"extra": "e"' in visible
    assert '"value": "from-user"' in visible


def test_an_answer_that_does_not_match_the_question_is_asked_again():
    """A mismatched answer re-asks. It is never passed to the model as an answer."""

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    collect(graph, "帮我")
    calls = len(domain_prompts(model))

    wrong = collect(graph, "补充", {"resume": {"value": {"other": "x"}}})
    assert [event.event for event in wrong] == ["plan", "task_update", "task_update", "interrupt"]
    assert field_names(wrong[3].data) == ["value"]
    assert "不匹配" in wrong[3].data["display"]
    # The bad answer never reached the model.
    assert len(domain_prompts(model)) == calls

    # The same question still accepts the right answer afterwards.
    ok = collect(graph, "补充", {"resume": {"value": {"value": "from-user"}}})
    assert [event.event for event in ok][-1] == "final"


def test_a_blank_answer_is_not_treated_as_an_answer():
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    collect(graph, "帮我")
    blank = collect(graph, "补充", {"resume": {"value": {"value": "   "}}})

    assert [event.event for event in blank] == ["plan", "task_update", "task_update", "interrupt"]
    assert "不能为空" in blank[3].data["display"]


def test_a_question_that_names_no_fields_is_answered_as_a_reply_not_a_dead_pause():
    """A pause is answered by filling in fields, so a field-less question cannot be shown.

    The client drops an unanswerable interrupt, which would leave the graph paused with no
    card, no error and no way forward. The domain concludes instead, handing the model's own
    wording back so the user can answer in the composer.
    """

    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            "请问您想转给谁？",
            "好的",
        ],
    )

    events = collect(graph, "帮我")

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "reply_delta",
        "final",
    ]


def test_a_blank_optional_answer_is_accepted():
    """The client lets an optional field stay blank, so the server must not reject it --
    doing so would burn one of the three ask attempts on an answer already accepted."""

    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value","note"],"display":"请提供"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
        roster=transfer_with_an_optional_note(),
    )

    collect(graph, "帮我")
    events = collect(
        graph,
        "补充",
        {"resume": {"value": {"value": "from-user", "note": ""}}},
    )

    assert [event.event for event in events][-1] == "final"


def test_a_domain_that_keeps_asking_is_bounded():
    """A model that never stops asking still ends, with a readable reason.

    Every new question is a new activation and costs one model call, so the script hands out
    four of them: the ask budget is MAX_ASK_ATTEMPTS, and the fourth question is refused
    rather than parked -- the run ends on a failure the user can read, not a fifth pause.
    """

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            *[("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}')] * 4,
            "结束",
        ],
    )

    collect(graph, "帮我")
    events = collect(graph, "补充", {"resume": {"value": {"value": "v"}}})
    # Answer again while it keeps asking, with a hard cap so a regression fails loudly
    # instead of hanging the suite.
    for _ in range(MAX_ASK_ATTEMPTS + 2):
        if not events or events[-1].event != "interrupt":
            break
        events = collect(graph, "补充", {"resume": {"value": {"value": "v"}}})

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    ended = payloads(events, "subtask_end")[0]
    assert ended["status"] == ToolStatus.FAILED.value
    assert "未能获得有效" in ended["summary"]
    # Four activations each asked once; the fifth was refused before it could ask.
    assert len(domain_prompts(model)) == MAX_ASK_ATTEMPTS + 1


def test_an_empty_tool_menu_concludes_instead_of_asking_for_identifiers():
    """The capability boundary: with no tools there is no question worth asking.

    Asking for an account number the agent could not act on is what made every business
    request park on a pause, so with an empty menu the domain states the boundary instead.
    """

    graph, _model, _recorder = compiled(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"处理转账"}'),
            "已说明",
        ],
    )

    events = collect(graph, "给我爱人转200块钱")

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    summaries = [event.data["summary"] for event in events if event.event == "subtask_end"]
    assert summaries == [NO_TOOLS_SUMMARY]


def test_a_tool_less_domain_never_calls_the_model_at_all():
    """No capability connected means no model call, not a menu to guess at.

    The old base loop asked the model anyway and then overwrote whatever it answered with the
    capability boundary -- a call spent on a decision the domain could not act on. The minimal
    template states the boundary directly, so a tool-less domain never reaches the model; a
    tool call the model would have invented cannot even be a protocol error.
    """

    graph, model, _recorder = compiled(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"处理转账"}'),
            "已说明",
        ],
    )

    events = collect(graph, "给我爱人转200块钱")

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    # The domain's own turn is the one the old loop spent; the Supervisor's turns are not it.
    assert domain_calls(model) == []
    assert domain_prompts(model) == []

    # No domain call means no menu was ever offered, empty or otherwise.
    assert domain_tools(model) == []
    assert _recorder.tool_names == []


def test_a_granted_tool_menu_still_allows_a_question():
    """The boundary is the tool menu, not a blanket ban on asking."""

    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
        ],
    )

    events = collect(graph, "帮我")

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]


def test_a_domain_spec_reads_its_menu_from_the_handle_not_the_declaration():
    declaration = DomainDeclaration(agent_id="transfer", display="转账 Agent", purpose="转账")
    gateway = ToolGateway(ToolCatalog.build([ECHO], {"transfer": ["echo"]}), RecordingAdapter())
    spec = DomainSpec(declaration=declaration, handle=gateway.handle("transfer", CONTEXT))

    # The handle is what the Gateway will actually allow.
    assert spec.tool_menu() == [("echo", "Return what it was given")]

    empty = DomainSpec(declaration=declaration, handle=gateway.handle("bill", CONTEXT))
    assert empty.tool_menu() == []


# --- Question binding: the resume has to answer the question that was actually asked ---
#
# A resume used to be accepted on the strength of its fields alone, so an answer meant for
# an earlier question of the same shape would be consumed by a later one. The question now
# carries an id, and the resume has to name it.


def asking_graph(*, replies, replies_for=None, adapter=None, model=None):
    """A graph parked on one question, with the interrupt that parked it."""

    graph, model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=replies,
        replies_for=replies_for,
        adapter=adapter,
        model=model,
    )
    paused = collect(graph, "帮我")
    assert [event.event for event in paused] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ], paused
    return graph, model, paused[-1]


def answer(question_id, value):
    return {"resume": {"question_id": question_id, "value": value}}


def test_the_interrupt_event_carries_the_full_question():
    """Every field the client needs to answer, and to prove which question it is answering."""

    _graph, _model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
        ]
    )

    assert isinstance(interrupt.data["question_id"], str) and interrupt.data["question_id"]
    assert field_names(interrupt.data) == ["value"]
    assert without_fields(interrupt.data) == {
        "kind": "input",
        "question_id": interrupt.data["question_id"],
        "agent_id": "transfer",
        "display": "请提供 value",
        "attempt": 1,
    }


def test_a_resume_naming_the_wrong_question_is_a_protocol_error():
    """An answer to a question this thread is not waiting on is not an illegal answer.

    Re-asking would be wrong: the client is not confused about the fields, it is answering
    a question that is gone. The domain must not quietly treat it as an answer either.
    """

    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )
    calls = len(domain_prompts(model))

    with pytest.raises(AppError) as failure:
        collect(graph, "补充", answer("q_not_the_one", {"value": "from-user"}))

    assert failure.value.code == "INVALID_INTERRUPT_ANSWER"
    # And it never reached the model as if it were an answer.
    assert len(domain_prompts(model)) == calls
    assert interrupt.data["question_id"]


def test_an_illegal_answer_re_asks_the_same_question_under_the_same_id():
    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )
    question_id = interrupt.data["question_id"]
    calls = len(domain_prompts(model))

    wrong = collect(graph, "补充", answer(question_id, {"other": "x"}))

    assert [event.event for event in wrong] == ["plan", "task_update", "task_update", "interrupt"]
    # Same question, same id -- only the attempt moved. A new id here would tell the client
    # it is answering a different question than the one it was shown.
    assert wrong[3].data["question_id"] == question_id
    assert field_names(wrong[3].data) == ["value"]
    assert wrong[3].data["attempt"] == 2
    assert "不匹配" in wrong[3].data["display"]
    assert len(domain_prompts(model)) == calls


@pytest.mark.parametrize("value", ["just-a-string", ["a", "b"], None, 7])
def test_a_resume_value_that_is_not_an_object_is_not_an_answer(value):
    """The envelope is validated before its contents, so a bad shape never reaches a field."""

    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )
    question_id = interrupt.data["question_id"]
    calls = len(domain_prompts(model))

    wrong = collect(graph, "补充", answer(question_id, value))

    assert [event.event for event in wrong] == ["plan", "task_update", "task_update", "interrupt"]
    assert wrong[3].data["question_id"] == question_id
    assert wrong[3].data["attempt"] == 2
    assert len(domain_prompts(model)) == calls


def test_a_legacy_resume_without_the_envelope_still_answers_the_open_question():
    """The old `{value: ...}` shape keeps working while the client is upgraded.

    Runtime is what makes it work: it reads the single open question and names it, so the
    graph still sees a bound resume.
    """

    graph, _model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    events = collect(graph, "补充", {"resume": {"value": {"value": "from-user"}}})

    assert [event.event for event in events][-1] == "final"
    assert interrupt.data["question_id"]


def test_only_the_resume_value_is_collected_never_the_envelope():
    """`question_id` is addressing, not data: it must not reach the model as an answer."""

    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "echo 已完成",
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    events = collect(graph, "补充", answer(interrupt.data["question_id"], {"value": "from-user"}))

    assert [event.event for event in events][-1] == "final"
    # The thread's answer turn is the whole envelope the model is handed, so "no envelope
    # in the data" is readable straight off it. The call picked is the one that ends on the
    # answer -- the tool decision it drove, not the report that follows it.
    answered = next(
        call for call in domain_calls(model) if '"user_answer"' in str(call[-1].get("content"))
    )
    decoded = json.loads(answered[-1]["content"])
    assert decoded == {"user_answer": {"value": "from-user"}}
    assert "question_id" not in decoded["user_answer"]


def test_a_new_question_gets_a_new_id_but_keeps_counting_attempts():
    """A new question is a new id; the ask counter still runs across the whole domain run.

    The counter cannot restart per question: a model that answered every question with a
    fresh one would then never reach the bound, which is the loop the bound exists for.
    """

    graph, _model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "ask_user", '{"fields":["extra"],"display":"还需要 extra"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    second = collect(graph, "补充", answer(interrupt.data["question_id"], {"value": "from-user"}))

    # The answer was accepted, so the domain ran a model turn -- and asked a new question.
    assert [event.event for event in second] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    asked = second[-1].data
    # A genuinely new question: new id, so the client cannot answer the old one by mistake.
    assert asked["question_id"] != interrupt.data["question_id"]
    assert field_names(asked) == ["extra"]
    # But the bound still counts it, so a model that keeps asking new questions still ends.
    assert asked["attempt"] == 2


def test_an_illegal_answer_does_not_reset_the_count_when_the_model_changes_question():
    """The exact sequence the counter has to survive, and why it cannot restart per question.

    A is asked (attempt=1), a bad answer re-asks A (attempt=2, same id), then the model asks
    a different question: it is a new question with a new id, but attempt=3. Restarting at 1
    would let a model that meets every answer with a fresh question ask forever.
    """

    graph, _model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "ask_user", '{"fields":["extra"],"display":"还需要 extra"}'),
            "结束",
        ]
    )
    first = interrupt.data["question_id"]

    # A's bad answer: same question, same id, next attempt.
    bad = collect(graph, "补充", answer(first, {"other": "x"}))
    assert [event.event for event in bad] == ["plan", "task_update", "task_update", "interrupt"]
    assert bad[3].data["question_id"] == first
    assert bad[3].data["attempt"] == 2

    # A usable answer now, and the model asks something else entirely.
    asked = collect(graph, "补充", answer(first, {"value": "v"}))
    assert [event.event for event in asked] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    fresh = asked[-1].data
    assert field_names(fresh) == ["extra"]
    assert fresh["question_id"] != first
    assert fresh["attempt"] == 3


def test_the_third_illegal_answer_ends_the_domain_instead_of_asking_forever():
    """The bound is reached on the third bad answer, so a client cannot loop the domain.

    Each illegal answer increments the attempt. Attempts 1 and 2 re-ask; the third pushes
    the count past `MAX_ASK_ATTEMPTS`, so the domain concludes and the Supervisor answers.
    """

    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            "结束",
        ]
    )
    bad = answer(interrupt.data["question_id"], {"other": "x"})
    calls = len(domain_prompts(model))

    second = collect(graph, "补充", bad)
    assert [event.event for event in second] == ["plan", "task_update", "task_update", "interrupt"]
    assert second[3].data["attempt"] == 2

    third = collect(graph, "补充", bad)
    assert [event.event for event in third] == ["plan", "task_update", "task_update", "interrupt"]
    assert third[3].data["attempt"] == 3

    # The third illegal answer is the one that ends it. No `agent_start`: the domain never
    # got as far as a model turn, it simply stopped asking.
    fourth = collect(graph, "补充", bad)
    assert [event.event for event in fourth] == [
        "plan",
        "task_update",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    assert "未能获得有效" in fourth[2].data["summary"]
    # Illegal answers are rejected without consulting the model at all.
    assert len(domain_prompts(model)) == calls


def test_an_answer_is_still_bound_to_the_question_when_the_script_offers_another():
    """The question comes back from the checkpoint, never from a fresh model call.

    The script holds a different question after the ask. If the pause re-decided on replay,
    the user would be shown that one instead -- and the answer they gave would be attached
    to a question they never saw.
    """

    graph, model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            # What a re-deciding pause would surface. It must never be shown for this answer.
            ("call", "ask_user", '{"fields":["something_else"],"display":"另一个问题"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "完成",
        ],
        replies_for={"echo": {"value": "from-user"}},
    )

    wrong = collect(graph, "补充", answer(interrupt.data["question_id"], {"other": "x"}))

    assert [event.event for event in wrong] == ["plan", "task_update", "task_update", "interrupt"]
    assert wrong[3].data["question_id"] == interrupt.data["question_id"]
    assert field_names(wrong[3].data) == ["value"]
    assert wrong[3].data["display"] != "另一个问题"
    # The re-ask cost no model call at all, which is the whole reason the split exists.
    assert len(domain_prompts(model)) == 1


# --- Tool event semantics: calls pair with their results, and the status means something ---


def payloads(events, name):
    return [event.data for event in events if event.event == name]


def test_two_calls_to_the_same_tool_do_not_share_a_call_id():
    """A derived id would make two different calls look like retries of each other.

    `transfer:echo` was the same string for every `echo` call this domain ever made, so a
    business service keying on it would collapse two legitimate calls into one.
    """

    recorder = RecordingAdapter({"echo": {"value": "v1"}})
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"第一次"}'),
            ("call", "echo", '{"value":"v1"}'),
            "第一次已完成",
            ("call", "handoff", '{"agent_id":"transfer","instruction":"第二次"}'),
            ("call", "echo", '{"value":"v2"}'),
            "第二次已完成",
            "完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    calls = payloads(events, "tool_call")
    results = payloads(events, "tool_result")
    assert len(calls) == 2 and len(results) == 2
    assert calls[0]["call_id"] != calls[1]["call_id"]
    # Each result names its own call, not the other's.
    assert [result["call_id"] for result in results] == [call["call_id"] for call in calls]
    # And the adapter was handed the same ids the client was shown.
    assert [invocation.call.call_id for invocation in recorder.invocations] == [
        call["call_id"] for call in calls
    ]


class MisreportingAdapter(RecordingAdapter):
    """Reports a call id that is not the one it was handed, as a real adapter may.

    The tool contract does not require an adapter to echo the id, so the client's ability
    to pair `tool_call` with `tool_result` cannot depend on it doing so.
    """

    def invoke(self, invocation):
        result = super().invoke(invocation)
        return ToolResult(
            tool_name=result.tool_name,
            status=result.status,
            call_id="an-id-nobody-minted",
            data=result.data,
            error=result.error,
        )


def test_the_call_and_its_result_are_paired_by_the_id_the_server_sent():
    """The pairing is the server's guarantee, not the adapter's good behaviour."""

    recorder = MisreportingAdapter({"echo": {"value": "v1"}})
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            "echo 已完成",
            "完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    call = payloads(events, "tool_call")[0]
    result = payloads(events, "tool_result")[0]
    assert result["call_id"] == call["call_id"]
    # The adapter's own claim never reaches the client.
    assert result["call_id"] != "an-id-nobody-minted"


def test_a_failed_tool_is_fed_back_and_the_report_ends_the_subtask():
    """The failed attempt stays visible, and the loop's report is what ends the domain.

    Before the activation loop, the tool call itself was the decision, so its failure was
    the subtask's ending. Now the failure goes back to the model, whose report concludes --
    the FAILED attempt is still on the activity stream, where the user can see it.
    """

    recorder = RecordingAdapter()
    recorder.fail("echo", "upstream unavailable")
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v1"}'),
            "echo 未能完成",
            "未能完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    assert payloads(events, "tool_result")[0]["status"] == ToolStatus.FAILED.value
    # The report, not the tool, is the ending now: the status says the activation
    # concluded, and the summary is the report the model wrote about the failure.
    assert payloads(events, "subtask_end")[0]["status"] == ToolStatus.SUCCEEDED.value
    assert payloads(events, "subtask_end")[0]["summary"] == "echo 未能完成"


def test_a_denied_tool_is_fed_back_and_the_report_ends_the_subtask():
    """`bill` carries `echo`, but the arguments break its schema: refused, not attempted."""

    graph, _model, recorder = compiled(
        grants={"bill": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"bill","instruction":"调用 echo"}'),
            ("call", "echo", "{}"),
            "echo 被拒绝，无法完成",
            "未能完成",
        ],
    )

    events = collect(graph)

    assert payloads(events, "tool_result")[0]["status"] == ToolStatus.DENIED.value
    assert payloads(events, "subtask_end")[0]["status"] == ToolStatus.SUCCEEDED.value
    assert payloads(events, "subtask_end")[0]["summary"] == "echo 被拒绝，无法完成"
    assert recorder.tool_names == []


def test_an_exhausted_question_ends_the_subtask_as_failed():
    """Running out of attempts is not a success, even though the domain concluded cleanly."""

    graph, _model, interrupt = asking_graph(
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            "结束",
        ]
    )
    bad = answer(interrupt.data["question_id"], {"other": "x"})

    collect(graph, "补充", bad)
    collect(graph, "补充", bad)
    exhausted = collect(graph, "补充", bad)

    assert [event.event for event in exhausted] == [
        "plan",
        "task_update",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    assert exhausted[2].data["status"] == ToolStatus.FAILED.value
    assert "未能获得有效" in exhausted[2].data["summary"]


def test_the_exhausted_message_names_the_field_as_the_user_saw_it():
    """The sentence goes to a user, so it must read the label, not the internal name.

    Every other exhausted assertion uses an undeclared field, where the fallback label *is*
    the name -- so reading the name instead would stay green.
    """

    roster = (
        DomainDeclaration(
            agent_id="transfer",
            display="转账汇款",
            purpose="转账汇款、查询转账记录",
            fields={"recipient_name": FieldMeta(label="收款人姓名")},
            graph_builder=tool_domain_graph_builder,
        ),
    )
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
            ("call", "ask_user", '{"fields":["recipient_name"],"display":"请提供"}'),
            "结束",
        ],
        roster=roster,
    )
    paused = collect(graph, "帮我")
    bad = answer(paused[-1].data["question_id"], {"recipient_name": ""})

    collect(graph, "补充", bad)
    collect(graph, "补充", bad)
    exhausted = collect(graph, "补充", bad)

    summary = exhausted[2].data["summary"]
    assert "收款人姓名" in summary
    assert "recipient_name" not in summary


# --- upstream failures: one retry inside the invocation, then a re-ask or a loud error ---

# The parked-then-answered script every test below shares. Call 1 is the Supervisor's
# routing, call 2 the domain's question; the answer then drives call 3 (the tool round),
# call 4 the report that ends the activation, and call 5 the Supervisor's closing reply.
ASK_THEN_ANSWER = [
    ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充"}'),
    ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
    ("call", "echo", '{"value":"from-user"}'),
    "echo 已完成",
    "完成",
]


class FlakyModel:
    """A scripted model that fails the calls named in `fail_at`, then behaves normally.

    The call index counts *attempts*, not successful replies, which is what makes the
    retry observable: a failed attempt is not answered from the script, so the replies
    stay aligned whether or not the graph retried.
    """

    def __init__(self, replies, *, fail_at, error=None):
        self._inner = ScriptedModel(replies)
        self._fail_at = set(fail_at)
        self._error = error or ModelClientError("LLM_TIMEOUT", retryable=True)
        self.calls = 0

    @property
    def prompts(self):
        return self._inner.prompts

    def complete(self, messages):
        self.calls += 1
        if self.calls in self._fail_at:
            # Recorded like any other prompt: the model really was asked.
            self._inner.prompts.append(messages[0]["content"])
            raise self._error
        return self._inner.complete(messages)

    def stream(self, messages, *, tools=()):
        # The decision turns stream now, so the failing attempt has to be raised from inside
        # the generator: `start_stream` sees it on the first `next()`, before any delta has
        # reached the caller, which is what makes the retry possible.
        self.calls += 1
        if self.calls in self._fail_at:
            self._inner.prompts.append(messages[0]["content"])
            raise self._error
        yield from self._inner.stream(messages, tools=tools)

    def close(self):
        return None


def test_a_transient_model_failure_is_retried_within_the_same_invocation():
    """The user already answered; a momentary upstream failure must not cost them that."""

    model = FlakyModel(ASK_THEN_ANSWER, fail_at=[3])
    graph, _model, interrupt = asking_graph(replies=ASK_THEN_ANSWER, model=model)

    events = collect(graph, "补充", answer(interrupt.data["question_id"], {"value": "v"}))

    # The turn completed as if nothing had happened: no extra question was asked.
    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "tool_call",
        "tool_result",
        "subtask_end",
        "reply_delta",
        "final",
    ]
    # Call 3 failed, call 4 was the retry. One retry, not a loop; calls 5 and 6 are the
    # report and the Supervisor's close.
    assert model.calls == 6


def test_a_transient_failure_that_survives_the_retry_re_asks_the_same_question():
    """The answer was fine and the upstream was not, so the answer is not thrown away."""

    model = FlakyModel(ASK_THEN_ANSWER, fail_at=[3, 4])
    graph, _model, interrupt = asking_graph(replies=ASK_THEN_ANSWER, model=model)
    asked = interrupt.data

    events = collect(graph, "补充", answer(asked["question_id"], {"value": "v"}))

    assert [event.event for event in events] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    again = events[-1].data
    # Same question, same id, same attempt: this is not a new round and it is not the
    # user's mistake, so it must not spend their ask budget.
    assert again["question_id"] == asked["question_id"]
    assert again["attempt"] == asked["attempt"]
    assert field_names(again) == field_names(asked)
    assert "暂时无法处理，请重试" in again["display"]
    assert model.calls == 4


def test_a_non_retryable_model_failure_is_not_retried_and_ends_the_turn_loudly():
    """An auth failure is not a hiccup; asking again would just fail again."""

    error = ModelClientError("LLM_AUTH", retryable=False)
    model = FlakyModel(ASK_THEN_ANSWER, fail_at=[3], error=error)
    graph, _model, interrupt = asking_graph(replies=ASK_THEN_ANSWER, model=model)

    with pytest.raises(AppError) as failure:
        collect(graph, "补充", answer(interrupt.data["question_id"], {"value": "v"}))

    assert failure.value.code == "LLM_UPSTREAM_ERROR"
    assert failure.value.retryable is False
    assert failure.value.details["upstream_code"] == "LLM_AUTH"
    # One attempt only: the failure was never repeated.
    assert model.calls == 3


class TransientAdapter(RecordingAdapter):
    """Fails its first `failures` calls, then answers normally from the registered replies."""

    def __init__(self, failures, *, retryable=True, replies=None):
        super().__init__(replies)
        self._remaining = failures
        self._retryable = retryable

    def invoke(self, invocation):
        if self._remaining > 0:
            self._remaining -= 1
            self._invocations.append(invocation)
            return ToolResult(
                tool_name=invocation.call.tool_name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error="工具服务暂时不可用",
                retryable=self._retryable,
            )
        return super().invoke(invocation)


def test_a_retryable_tool_failure_is_retried_as_a_new_call():
    """A retry is a new attempt, so it must be visible as one rather than hidden."""

    recorder = TransientAdapter(1, replies={"echo": {"value": "v"}})
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v"}'),
            "echo 已完成",
            "完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    calls = payloads(events, "tool_call")
    results = payloads(events, "tool_result")
    # Both attempts are published: the failed one happened and is not rewritten.
    assert len(calls) == 2 and len(results) == 2
    assert calls[0]["call_id"] != calls[1]["call_id"]
    assert [result["status"] for result in results] == [
        ToolStatus.FAILED.value,
        ToolStatus.SUCCEEDED.value,
    ]
    assert [result["call_id"] for result in results] == [call["call_id"] for call in calls]
    assert payloads(events, "subtask_end")[0]["status"] == ToolStatus.SUCCEEDED.value


def test_a_tool_failure_that_is_not_marked_retryable_is_never_retried():
    """Most `FAILED` results are not transient, and repeating a write can repeat its effect."""

    recorder = TransientAdapter(1, retryable=False, replies={"echo": {"value": "v"}})
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v"}'),
            "echo 未能完成",
            "未能完成",
        ],
        adapter=recorder,
    )

    events = collect(graph)

    # The failure was not repeated: exactly one attempt, and then the model was told.
    assert len(payloads(events, "tool_call")) == 1
    assert payloads(events, "tool_result")[0]["status"] == ToolStatus.FAILED.value
    # The report, not the tool, ends the activation now; the summary is the model's own
    # account of the failure it was handed.
    assert payloads(events, "subtask_end")[0]["status"] == ToolStatus.SUCCEEDED.value
    assert payloads(events, "subtask_end")[0]["summary"] == "echo 未能完成"


def test_a_retryable_tool_failure_that_survives_the_retry_ends_the_turn_loudly():
    """With no question to re-present, a surviving transient failure is a stream error."""

    recorder = TransientAdapter(2, replies={"echo": {"value": "v"}})
    graph, _model, _recorder = compiled(
        grants={"transfer": ["echo"]},
        replies=[
            ("call", "handoff", '{"agent_id":"transfer","instruction":"调用 echo"}'),
            ("call", "echo", '{"value":"v"}'),
            "完成",
        ],
        adapter=recorder,
    )

    with pytest.raises(AppError) as failure:
        collect(graph)

    assert failure.value.code == "LLM_UPSTREAM_ERROR"
    assert failure.value.details["upstream_code"] == "TOOL_UNAVAILABLE"
