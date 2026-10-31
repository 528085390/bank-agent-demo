"""A module-owned graph that answers from a whole-turn callable.

`ModuleSpec.turn` is gone: a module that answers a request itself declares a `graph_builder`
and owns its nodes. Tests that need to drive the *confirmation* protocol -- a pause recorded
in the checkpoint and a button event that reaches the module -- therefore build the three
nodes the base used to build, around a callable the test owns:

    decide  -> the handler answers the turn
    wait    -> the pause is checkpointed and the user is asked
    process -> the click reaches the handler's `act`

The module still needs a tool menu: `build_domain_graph` uses a `graph_builder` only when the
module also exposes at least one tool.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.declaration import DomainDeclaration
from app.agents.sdk import (
    INVALID_DECISION_SUMMARY,
    DomainOutcome,
    TurnAction,
    action_kind,
    awaiting,
    conclude_turn,
    conclude_without_tool,
    current_request,
    domain_scope,
    next_attempt,
    read_answer,
    wait_for_user,
)
from app.agents.state import AgentState
from app.agents.turn import TurnResult, TurnStatus
from app.modules.base import ModuleSpec, ToolData
from app.tools.protocol import ToolSpec, ToolStatus

logger = logging.getLogger(__name__)

FAILED_SUMMARY = "本次处理未能完成，请稍后重试。"

# A module-owned graph is only used when the module also exposes a tool menu, so every
# handler module carries this placeholder. The handler never calls it.
PLACEHOLDER_TOOL = ToolSpec(name="handler_noop", description="Placeholder for a handler module")


def _placeholder_handler(invocation: Any, client: Any) -> ToolData:
    return ToolData(summary="ok", display={}, facts={})


def handler_graph_builder(
    handler: Any,
    *,
    action: Callable[..., TurnResult] | None = None,
    request: Callable[[AgentState], str] | None = None,
):
    """A `graph_builder` that answers from `handler`, and from `action` for a button event."""

    act = action if action is not None else getattr(handler, "act", None)
    read_request = request or current_request

    def builder(model, spec, *, assembler=None, turn=None, client=None):
        del client

        def decide(state: AgentState) -> dict[str, Any]:
            writer = get_stream_writer()
            writer({"event": "agent_start", "agent_id": spec.agent_id})
            message = read_request(state)
            try:
                with domain_scope(spec, state, assembler, turn, message) as handoff:
                    result = handler(spec.handle.trusted_context, model, message)
                    updates = handoff.updates()
            except Exception:
                logger.exception("test handler crashed: agent=%s", spec.agent_id)
                return conclude_turn(
                    spec,
                    TurnResult(status=TurnStatus.FAILED, summary=FAILED_SUMMARY),
                    writer,
                    state,
                )
            return conclude_turn(spec, result, writer, state, updates)

        def process(state: AgentState) -> dict[str, Any]:
            writer = get_stream_writer()
            question = state.get("pending_question") or {}
            # A click that names a question this thread is no longer waiting on raises, which
            # is what the button route turns into INVALID_INTERRUPT_ANSWER.
            answer, problem = read_answer(state.get("pending_answer"), question)
            if problem is not None:
                return awaiting(
                    spec,
                    DomainOutcome(ask=question),
                    writer,
                    question=question,
                    reask=f"{problem}请重新提供。",
                    attempt=next_attempt(question),
                    question_id=question.get("question_id"),
                )
            kind = action_kind(question, answer)
            if act is None or kind is None:
                return conclude_without_tool(
                    spec, INVALID_DECISION_SUMMARY, ToolStatus.DENIED, writer
                )
            confirmation = question.get("confirmation") or {}
            writer({"event": "agent_start", "agent_id": spec.agent_id})
            clicked = TurnAction(
                kind=kind,
                pending_id=str(confirmation.get("pending_id") or ""),
                # The code travels on the turn, never through AgentState, so it cannot reach a
                # checkpoint or a ledger row. A cancel carries none.
                verification_code=(
                    turn.verification_code
                    if (turn is not None and kind == TurnAction.CONFIRM)
                    else None
                ),
            )
            message = read_request(state)
            try:
                with domain_scope(spec, state, assembler, turn, message) as handoff:
                    result = act(spec.handle.trusted_context, model, clicked)
                    updates = handoff.updates()
            except Exception:
                logger.exception("test action crashed: agent=%s", spec.agent_id)
                return conclude_turn(
                    spec,
                    TurnResult(status=TurnStatus.FAILED, summary=FAILED_SUMMARY),
                    writer,
                    state,
                )
            return conclude_turn(spec, result, writer, state, updates)

        def route(state: AgentState) -> str:
            return "wait" if state.get("turn_phase") == "waiting_for_user" else END

        graph = StateGraph(AgentState)
        graph.add_node("decide", decide)
        graph.add_node("wait", wait_for_user)
        graph.add_node("process", process)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
        graph.add_edge("wait", "process")
        graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
        return graph.compile()

    return builder


def handler_module(
    name: str,
    declaration: DomainDeclaration,
    handler: Any,
    *,
    action: Callable[..., TurnResult] | None = None,
    request: Callable[[AgentState], str] | None = None,
) -> ModuleSpec:
    """A one-domain module whose own graph answers from `handler`."""

    return ModuleSpec(
        name=name,
        declaration=replace(
            declaration,
            graph_builder=handler_graph_builder(handler, action=action, request=request),
        ),
        tools={PLACEHOLDER_TOOL.name: PLACEHOLDER_TOOL},
        handlers={PLACEHOLDER_TOOL.name: _placeholder_handler},
    )
