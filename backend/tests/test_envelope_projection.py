"""What a tool returned is not the same as what the model gets to see.

The envelope is the seam's own vocabulary, and its four fields have four different
audiences. Before this, the whole envelope was serialised into `domain_results` -- which is
both the model's prompt and a checkpoint channel -- so a tool's raw facts, its display data
and its business ids all reached the model, and the whole envelope additionally reached the
browser.

Each audience gets its own marker, so that "this one leaked" and "that one arrived" are two
different assertions rather than one contradiction: a single shared marker cannot be both
forbidden from the frame and required in it.
"""

import asyncio
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.integration.bank_client import BankClient
from app.modules.base import ModuleSpec, ToolData
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

DECLARATION = DomainDeclaration(
    agent_id="alpha",
    display="A",
    purpose="p",
    graph_builder=tool_domain_graph_builder,
)
CONTEXT = TrustedContext(user_id="u", session_id="s1", bank_customer_id="u_zhang")

FACTS_SECRET = "SECRET_FACTS_9f3a"  # only ever in `facts` -- must not leave the server
DISPLAY_TEXT = "24 小时后到账。"  # only ever in `display` -- must reach the client
SUMMARY_TEXT = "活期账户余额 8,320.00 元。"
# The model's own report, which is what ends the activation and what `domain_results`
# carries; the tool's summary lives in the frame and in the loop's thread, not in the ledger.
REPORT_TEXT = "查询完成"

SCRIPT = [
    ("call", "handoff", '{"agent_id":"alpha","instruction":"做"}'),
    ("call", "echo", "{}"),
    # The loop feeds the tool's summary back; the report is what ends the activation.
    REPORT_TEXT,
    "完成",
]


def run():
    """The run's published events, the scripted model's prompts, and its checkpointer.

    The saver is handed back because the events and the persisted state are two different
    artefacts: an event is a frame written for the client, a checkpoint is the channel
    values the graph would resume from. A claim about what is *persisted* has to read the
    second, and an event's payload is not it.
    """

    spec = ToolSpec(name="echo", description="e")

    def handler(invocation, client):
        return ToolData(
            summary=SUMMARY_TEXT,
            display={"blocks": [{"type": "text", "text": DISPLAY_TEXT}]},
            facts={"raw": FACTS_SECRET},
            business_ids={"account_id": "acc_1"},
        )

    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={"echo": spec},
                handlers={"echo": handler},
            )
        ]
    )
    gateway = ToolGateway(
        registry.catalog, BankToolAdapter(registry, BankClient("http://bank.test"))
    )
    model = ScriptedModel(SCRIPT)
    saver = InMemorySaver()
    graph = build_agent_graph(model, gateway, CONTEXT, roster=registry.roster).compile(
        checkpointer=saver
    )
    runtime = AgentRuntime(graph=graph)

    async def go():
        return [e async for e in runtime.invoke("s1", "u", "帮我", {})]

    return asyncio.run(go()), model, saver


def persisted_values(saver: InMemorySaver) -> list[Any]:
    """Everything the checkpointer actually kept, as readable Python values.

    Two sources, because LangGraph keeps both: every checkpoint's channel values, and the
    pending writes that have not been folded into a checkpoint yet. A walk that reads only
    one of them could report "the marker is absent" about state it never looked at.
    """

    values: list[Any] = []
    for checkpoint in saver.list(None):
        values.append(dict(checkpoint.checkpoint.get("channel_values") or {}))
    for writes in saver.writes.values():
        for _task_id, _channel, typed, _task_path in writes.values():
            values.append(saver.serde.loads_typed(typed))
    return values


def tool_result_frame(events):
    """The `tool_result` event's projected envelope.

    `AgentEvent.data` is the event payload, whose own `data` key is the frame's envelope
    (`runtime.py` builds it from `EVENT_FIELDS`, and the client reads `data.data`).
    """

    result = next(e for e in events if e.event == "tool_result")
    return result.data["data"]


def handed_to_the_model(model):
    """Everything the model was shown: every system prompt plus every thread turn.

    Since the activation loop, a tool's summary reaches the model through the thread's
    envelope -- a user turn, not a system prompt -- so a leakage assertion that only read
    `prompts` would be reading half of what the model is handed.
    """

    return "".join(str(message.get("content")) for call in model.messages for message in call)


def test_the_facts_marker_reaches_none_of_the_model_calls():
    """Everything the model is handed is an outbound request to an external service."""

    _events, model, _saver = run()

    handed = handed_to_the_model(model)
    assert FACTS_SECRET not in handed, f"a tool's raw facts reached the model: {handed}"


def test_the_summary_does_reach_the_model():
    """The counterpart. Blocking everything would be easy and wrong: the model still has to
    be able to answer. The tool's own summary travels in the thread's envelope."""

    _events, model, _saver = run()

    assert "8,320.00" in handed_to_the_model(model)


def test_the_display_marker_reaches_none_of_the_model_calls():
    """`display` belongs to the frontend, so it must not travel with the summary.

    `facts` is not the only field with an audience: `run_tool` drops `display` too, and
    without this the only thing stopping a later change from putting it back would be the
    absence of an assertion. `DISPLAY_TEXT` is a distinct marker, so this costs nothing.
    """

    _events, model, _saver = run()

    handed = handed_to_the_model(model)
    assert DISPLAY_TEXT not in handed, f"a tool's display data reached the model: {handed}"


def test_the_business_ids_still_reach_the_model_through_domain_results():
    """A known, deliberate deviation from contract §4.6, pinned so it cannot stay silent.

    §4.6 says `business_ids` must not enter the model context. It still does, and *not*
    through the envelope projection this part added: `conclude_domain` writes
    `{agent_id, summary, instruction, business_ids}` into `domain_results`
    (`app/agents/sdk.py`), the domain turn renders that list through `describe_results`, and
    `describe_results` is `json.dumps(results, …)` -- the whole list (`prompts.py`). So the
    ids reach the prompt in the Supervisor's second decision, whatever `_run_tool` does.

    `business_ids` cannot simply be stripped from `domain_results`: `_collect_business_ids`
    reads that very key to build the `final` event, so removing it would break the client
    contract instead. Closing §4.6 properly means separating the model-facing result row
    from the client-facing one, which is Gate 2's work and outside this part.

    This test asserts the behaviour that exists, so the next change that closes the gap
    fails here loudly and updates this test on purpose -- rather than the leak persisting
    unnoticed because nothing described it.
    """

    _events, model, _saver = run()

    assert any("acc_1" in prompt for prompt in model.prompts)


def test_the_facts_marker_is_not_in_the_checkpoint_state():
    """`domain_results` is an AgentState channel, so whatever lands there is persisted.

    Read from the checkpointer, not from the events: `subtask_end`'s `summary` is a frame
    the run published, and a test that asserts on it proves nothing about the channel the
    graph would resume from.
    """

    _events, _model, saver = run()
    values = persisted_values(saver)
    persisted = repr(values)

    # Positive controls. A walk that found nothing to read -- no checkpoint, or a channel
    # whose blob was never written -- would satisfy the assertion below for free. Both
    # markers that *are* expected in the state have to be found first: the report is the
    # summary `domain_results` carries now, and the activation's accumulated business ids
    # ride the same row.
    assert len(values) > 1, "the walk read no persisted values at all"
    assert REPORT_TEXT in persisted, "the domain's report never reached the checkpoint state"
    assert "acc_1" in persisted, "the business id never reached the checkpoint state"

    assert FACTS_SECRET not in persisted


def test_the_facts_marker_is_not_in_any_sse_frame():
    """A frame is read by the browser, the devtools network tab and any screenshot."""

    events, _model, _saver = run()

    for event in events:
        assert FACTS_SECRET not in repr(event.data), f"raw facts reached the client: {event}"


def test_the_display_blocks_do_reach_the_client():
    """`display` is the field meant for the client, so the projection must not drop it."""

    events, _model, _saver = run()

    assert tool_result_frame(events)["display"]["blocks"][0]["text"] == DISPLAY_TEXT


def test_the_business_ids_do_reach_the_client():
    """They are references the user may need to quote; only the *keys* are internal."""

    events, _model, _saver = run()

    assert tool_result_frame(events)["business_ids"] == {"account_id": "acc_1"}


def test_the_frame_carries_the_summary_and_nothing_else():
    """The projection is by field, so the frame's key set is exactly the three public ones."""

    events, _model, _saver = run()

    assert tool_result_frame(events) == {
        "summary": SUMMARY_TEXT,
        "display": {"blocks": [{"type": "text", "text": DISPLAY_TEXT}]},
        "business_ids": {"account_id": "acc_1"},
    }
