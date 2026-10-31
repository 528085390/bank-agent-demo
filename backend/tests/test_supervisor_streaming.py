"""What the user sees while a Supervisor turn is still being decided.

The events are the whole product here: a correct decision nobody can watch arriving is the
problem this phase exists to fix, and a frame that arrives twice, or after the final, is
worse than no frame at all.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import DOMAIN_ROSTER, build_agent_graph
from app.modules.registry import build_registry
from tests.support.domain_harness import generic_roster
from tests.support.handler_graph import handler_module
from app.agents.runtime import EVENT_FIELDS, AgentEvent, AgentRuntime
from app.agents.supervisor import (
    DELTA_FLUSH_CHARS,
    MAX_DOMAIN_TURNS,
    _DeltaBatch,
    build_domain_graph,
    build_supervisor,
)
from app.agents.turn import TurnResult, TurnStatus
from app.api.sse import EVENT_NAMES, encode_event
from app.context.view import (
    REASON_AGGRESSIVE_CROP,
    REASON_OVER_BUDGET,
    REASON_WITHIN_BUDGET,
    AssembledView,
)
from app.core.errors import AppError, ConversationErrorCode
from app.integration.model_client import FEATURE_STRICT, FeatureUnsupported, ModelClientError
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import StreamingModel

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
SESSION, USER = "s_1", "u_1"
HANDOFF_WEALTH = '{"agent_id":"wealth","instruction":"查一下持仓"}'
HANDOFF_CARD = '{"agent_id":"card","instruction":"查一下银行卡"}'
SUMMARY = '{"summary":"持仓如下"}'


def compiled(model, *, roster=None, assembler=None):
    # The production catalog, not an empty one: a dispatched domain only streams its own turn
    # when it has tools to drive (a tool-less domain runs the model-free shell template, A5 §1).
    # These tests pin what the Supervisor does *around* a domain turn, so the domain must have one.
    registry = build_registry()
    gateway = ToolGateway(registry.catalog, RecordingAdapter())
    graph = build_agent_graph(
        model,
        gateway,
        CONTEXT,
        assembler=assembler,
        roster=roster or generic_roster(registry.roster),
    )
    return graph.compile(checkpointer=InMemorySaver())


def compiled_handler(model, handler):
    """A graph whose wealth domain answers from `handler` through its own graph builder."""

    declaration = next(item for item in DOMAIN_ROSTER if item.agent_id == "wealth")
    registry = build_registry([handler_module("wealth", declaration, handler)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter())
    graph = build_agent_graph(model, gateway, CONTEXT, roster=registry.roster)
    return graph.compile(checkpointer=InMemorySaver())


def collect(graph, message="帮我看看持仓"):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke(SESSION, USER, message, {})]

    return asyncio.run(run())


def named(events, name):
    return [event.data for event in events if event.event == name]


def final_of(events) -> dict:
    finals = named(events, "final")
    assert len(finals) == 1, finals
    return finals[0]


def streamed_text(events) -> str:
    return "".join(item["text"] for item in named(events, "reply_delta"))


def text_before(events, name: str) -> str:
    """The text streamed before `name`, which is how a narration is told from a reply."""

    index = [event.event for event in events].index(name)
    return "".join(event.data["text"] for event in events[:index] if event.event == "reply_delta")


def text_after(events, name: str) -> str:
    index = [event.event for event in events].index(name)
    return "".join(event.data["text"] for event in events[index:] if event.event == "reply_delta")


class RecordingAssembler:
    """Stands in for `ContextAssembler`, recording how each assembly was asked for.

    The real one reads a ledger; what the overflow retry has to prove is only that it asked
    for the trimmed view the second time, and the view itself can be the fallback transcript.
    """

    def __init__(self) -> None:
        self.aggressive: list[bool] = []

    def assemble(self, **kwargs: Any) -> AssembledView:
        aggressive = bool(kwargs["aggressive"])
        self.aggressive.append(aggressive)
        return AssembledView(
            transcript=kwargs["transcript_fallback"],
            summary_text=None,
            estimated_tokens=0,
            summary=None,
            retained_from_sequence=None,
            retained_to_sequence=None,
            reason=REASON_AGGRESSIVE_CROP if aggressive else REASON_WITHIN_BUDGET,
        )


def concluding_handler(context, model, request) -> TurnResult:
    """A module that answers a whole turn without asking the model anything."""

    return TurnResult(status=TurnStatus.COMPLETED, summary="已经办好了。")


def test_the_reply_reaches_the_wire_before_the_turn_ends():
    model = StreamingModel([["你", "好"]])

    events = collect(compiled(model), message="你好")

    names = [event.event for event in events]
    assert names[-1] == "final"
    assert "reply_delta" in names
    assert streamed_text(events) == "你好"
    assert final_of(events)["text"] == "你好"


def test_a_burst_of_small_deltas_is_batched_into_frames():
    model = StreamingModel([[f"第{index}句。" for index in range(60)]])

    events = collect(compiled(model), message="你好")

    frames = named(events, "reply_delta")
    assert 1 < len(frames) < 60, "the wire carries frames, not tokens"
    assert all(len(item["text"]) <= DELTA_FLUSH_CHARS + 5 for item in frames)
    assert streamed_text(events) == final_of(events)["text"]


def test_the_last_delta_is_flushed_even_below_the_threshold():
    """A trailing delta smaller than either threshold would otherwise sit in the buffer."""

    model = StreamingModel([["好"]])

    events = collect(compiled(model), message="你好")

    assert len(named(events, "reply_delta")) == 1
    assert streamed_text(events) == "好"


def test_reasoning_never_reaches_the_wire():
    """The model's third channel is not the user's. This is a red line, not a style."""

    model = StreamingModel([[("reasoning", "用户可能想问持仓"), "你好，有什么可以帮你的？"]])

    events = collect(compiled(model), message="你好")

    assert final_of(events)["text"] == "你好，有什么可以帮你的？"
    assert streamed_text(events) == "你好，有什么可以帮你的？"
    assert "用户可能想问持仓" not in repr(events)


def test_a_direct_answer_sends_no_narration():
    model = StreamingModel([["你好，有什么可以帮你的？"]])

    events = collect(compiled(model), message="你好")

    assert named(events, "narration") == []


def test_a_handoff_turns_the_streamed_text_into_a_narration():
    """Text next to a handoff is the model saying what it is about to do, not the answer."""

    # Three streamed turns under the one-switch protocol: the Supervisor narrates and
    # dispatches, the domain reports (a domain's words are never forwarded to the user),
    # and the Supervisor answers the user directly.
    model = StreamingModel(
        [["我看看持仓", ("call", "handoff", HANDOFF_WEALTH)], ["领域报告。"], ["持仓查好了。"]],
    )

    events = collect(compiled(model))

    assert named(events, "narration") == [{"text": "我看看持仓"}]
    assert text_before(events, "narration") == "我看看持仓"
    assert text_after(events, "narration") == "持仓查好了。"
    assert final_of(events)["text"] == "持仓查好了。", "the narration is not the answer"


def test_a_normal_ending_carries_no_terminal_reason():
    model = StreamingModel([["你好"]])

    events = collect(compiled(model), message="你好")

    assert final_of(events)["terminal_reason"] is None
    assert final_of(events)["error_status"] is None


def test_a_failure_after_the_first_delta_ends_the_round_with_what_the_user_saw():
    """Text that is out cannot be taken back, so the round ends instead of retrying."""

    model = StreamingModel(
        [["正在查询", ("fail", ModelClientError("LLM_UPSTREAM_ERROR", retryable=True, status=500))]]
    )

    events = collect(compiled(model))

    final = final_of(events)
    assert final["text"] == "正在查询"
    assert final["terminal_reason"] == "api_error"
    assert final["error_status"] == 500
    assert len(model.prompts) == 1, "a failure after the first delta must not be retried"


def test_a_truncated_stream_ends_with_the_partial_text():
    model = StreamingModel([["部分文本", ("truncated",)]])

    events = collect(compiled(model), message="你好")

    final = final_of(events)
    assert final["text"] == "部分文本"
    assert final["terminal_reason"] == "aborted_streaming"


def test_a_mid_stream_failure_before_any_text_is_an_upstream_error():
    """Nothing was shown, but the one allowed retry was spent starting the stream."""

    model = StreamingModel(
        [
            [
                ("call", "handoff", '{"agent_id"'),
                ("fail", ModelClientError("LLM_UPSTREAM_ERROR", retryable=True, status=500)),
            ]
        ]
    )

    with pytest.raises(AppError):
        collect(compiled(model))

    assert len(model.prompts) == 1


def test_an_overflow_is_retried_with_the_aggressive_view():
    """A prompt that no longer fits is trimmed and asked again, not turned into a 502."""

    assembler = RecordingAssembler()
    model = StreamingModel(
        [["你好"]], errors=[ModelClientError("LLM_CONTEXT_OVERFLOW", retryable=False)]
    )

    events = collect(compiled(model, assembler=assembler), message="你好")

    assert final_of(events)["text"] == "你好"
    assert assembler.aggressive == [False, True], "the retry is the trimmed view"
    assert len(model.prompts) == 2, "an overflow costs exactly one retry"


def test_two_overflows_end_in_the_context_limit_error():
    """The trimmed view is the last resort: a second overflow is arithmetic, not a glitch."""

    assembler = RecordingAssembler()
    overflow = ModelClientError("LLM_CONTEXT_OVERFLOW", retryable=False)
    model = StreamingModel([], errors=[overflow, overflow])

    with pytest.raises(AppError) as caught:
        collect(compiled(model, assembler=assembler), message="你好")

    assert caught.value.code == ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED.value
    assert caught.value.details["reason"] == REASON_OVER_BUDGET
    assert assembler.aggressive == [False, True]
    assert len(model.prompts) == 2, "the retry is the last call"


def test_an_overflow_with_an_existing_conclusion_closes_the_turn():
    """With no view to trim, a conclusion already in hand is the ending the user gets.

    The phrasing call is what failed; the work is done. Raising here would turn a finished
    card turn into a 502 because the Supervisor could not say it out loud.
    """

    model = StreamingModel(
        [
            [("call", "handoff", HANDOFF_WEALTH)],
            [("fail", ModelClientError("LLM_CONTEXT_OVERFLOW", retryable=False))],
        ]
    )

    events = collect(compiled_handler(model, concluding_handler))

    final = final_of(events)
    assert final["text"] == "已经办好了。", "the module's own conclusion is the ending"
    assert final["terminal_reason"] is None
    assert len(model.prompts) == 2, "without an assembler there is no view to retry with"


def test_an_overflow_retry_keeps_the_queue_the_retried_decision_carries():
    """The retry rebuilds the delta batch and nothing else, so the round's queue survives.

    The second attempt asks for two handoffs: the round dispatches the first and keeps the
    second. An overflow must not cost the user the rest of what the model decided.
    """

    assembler = RecordingAssembler()
    model = StreamingModel(
        [
            [("call", "handoff", HANDOFF_WEALTH), ("call", "handoff", HANDOFF_CARD)],
            ["领域报告。"],
            ["领域报告。"],
            ["都办好了。"],
        ],
        errors=[ModelClientError("LLM_CONTEXT_OVERFLOW", retryable=False)],
    )

    events = collect(compiled(model, assembler=assembler))

    assert assembler.aggressive == [False, True, False], "the queued round assembles nothing"
    assert [item["agent_id"] for item in named(events, "agent_start")] == ["wealth", "card"]
    assert final_of(events)["text"] == "都办好了。"
    supervisor_turns = [
        prompt for prompt in model.prompts if prompt.startswith("你是银行 AI 助手的总调度")
    ]
    assert len(supervisor_turns) == 3, "the overflow, the retried decision, then the closing turn"


def test_the_queue_overflow_ends_the_round_as_max_turns(caplog):
    # One supervisor turn queues MAX_DOMAIN_TURNS + 1 handoffs; each dispatched domain
    # activation is its own streamed turn now, the overflow dispatches nothing.
    model = StreamingModel(
        [[("call", "handoff", HANDOFF_WEALTH)] * (MAX_DOMAIN_TURNS + 1)]
        + [["领域报告。"]] * MAX_DOMAIN_TURNS,
    )

    events = collect(compiled(model))

    assert final_of(events)["terminal_reason"] == "max_turns"
    supervisor_turns = [
        prompt for prompt in model.prompts if prompt.startswith("你是银行 AI 助手的总调度")
    ]
    assert len(supervisor_turns) == 1, "a queued handoff must not cost a supervisor model call"
    assert len(model.prompts) == 1 + MAX_DOMAIN_TURNS, "each dispatched domain streams its own turn"
    assert "未派发" in caplog.text


def test_the_new_events_are_declared_in_both_tables():
    """Missing from EVENT_FIELDS raises at run time; missing from EVENT_NAMES raises on encode."""

    assert EVENT_FIELDS["reply_delta"] == ("text",)
    assert EVENT_FIELDS["narration"] == ("text",)
    assert {"reply_delta", "narration"} <= set(EVENT_NAMES)
    assert EVENT_FIELDS["final"] == ("text", "business_ids", "terminal_reason", "error_status")


def test_an_unknown_event_is_still_refused():
    with pytest.raises(ValueError):
        encode_event(AgentEvent(event="message", data={}), "r")


def test_the_batch_flushes_on_the_character_threshold():
    frames: list[str] = []
    batch = _DeltaBatch(lambda payload: frames.append(payload["text"]))

    batch.add("x" * (DELTA_FLUSH_CHARS - 1))
    assert frames == [], "below both thresholds nothing goes out"
    batch.add("x")
    assert frames == ["x" * DELTA_FLUSH_CHARS]

    batch.flush()
    assert frames == ["x" * DELTA_FLUSH_CHARS], "an empty buffer is not a frame"


def test_every_supervisor_stream_call_carries_the_handoff_tool():
    """One decision path means no switch to pick it: the tool travels with every call."""

    assert "protocol" not in inspect.signature(build_supervisor).parameters
    assert "protocol" not in inspect.signature(build_domain_graph).parameters
    model = StreamingModel(
        [["我看看持仓", ("call", "handoff", HANDOFF_WEALTH)], ["领域报告。"], ["持仓查好了。"]],
    )

    collect(compiled(model))

    supervisor_calls = [
        (prompt, tools)
        for prompt, tools in zip(model.prompts, model.tools, strict=True)
        if prompt.startswith("你是银行 AI 助手的总调度")
    ]
    assert len(supervisor_calls) == 2, "the dispatching turn and the closing turn"
    for _, tools in supervisor_calls:
        assert [item["function"]["name"] for item in tools] == ["handoff"]


def test_a_provider_that_refuses_strict_still_fails_terminally():
    """A refusal is an answer: no second protocol, no relaxed retry, one call."""

    model = StreamingModel([], errors=[FeatureUnsupported(FEATURE_STRICT)])

    with pytest.raises(AppError) as caught:
        collect(compiled(model))

    assert caught.value.status == 502
    assert caught.value.details["upstream_code"] == "LLM_TOOLS_UNSUPPORTED"
    assert len(model.prompts) == 1, "a refusal must not be retried"


def test_the_batch_keeps_the_whole_text_even_before_flushing():
    frames: list[str] = []
    batch = _DeltaBatch(lambda payload: frames.append(payload["text"]))

    batch.add("半")

    assert batch.started is True
    assert batch.text == "半"
    assert frames == []
