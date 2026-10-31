"""Public module SDK: the Host API subgraph authors may depend on.

Modules under ``app.modules`` must import wait / call_tool / conclude / awaiting and
related helpers from here — never from ``app.agents.supervisor``. Supervisor and
tests may re-export these symbols for compatibility.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from langgraph.types import interrupt

from app.agents.decisions import DecisionError, DomainDecision
from app.agents.declaration import FREE_TEXT_FIELD, DomainSpec, resolve_fields
from app.agents.prompts import describe_transcript
from app.agents.runtime import TurnContext
from app.agents.state import AgentState, PendingField, idle_work_state, message_dicts
from app.agents.turn import (
    CONFIRMATION_KIND,
    TurnAction,
    TurnConfirmation,
    TurnResult,
    TurnStatus,
)
from app.agents.upstream import UpstreamUnavailable
from app.audit.context import AuditContext, audit_context, current_context
from app.context.handoff import (
    MAX_CONTEXT_RECORDS,
    Handoff,
    conversation_scope,
    validate_context_update,
)
from app.context.view import ContextAssembler
from app.core.errors import AppError
from app.tools.protocol import ErrorCode, SideEffect, ToolResult, ToolStatus

logger = logging.getLogger(__name__)

# The public surface: everything a module may import from this SDK. Names with a leading
# underscore are supervisor-internal machinery carried over verbatim (roadmap A6-a trims
# it); they stay importable for supervisor.py but are not part of the module contract.
__all__ = [
    "CONFIRMATION_KIND",
    "INVALID_DECISION_SUMMARY",
    "MAX_ASK_ATTEMPTS",
    "MAX_TOOL_ROUNDS",
    "DomainOutcome",
    "ToolOutcome",
    "TurnAction",
    "TurnConfirmation",
    "TurnResult",
    "TurnStatus",
    "action_kind",
    "awaiting",
    "call_tool",
    "conclude_domain",
    "conclude_turn",
    "conclude_without_tool",
    "current_request",
    "domain_node",
    "domain_outcome",
    "domain_scope",
    "may_retry",
    "next_attempt",
    "overflow_outcome",
    "pending_context",
    "read_answer",
    "run_tool",
    "tool_envelope",
    "tool_outcome",
    "wait_for_user",
]

# How many times one domain may ask the user before it has to conclude. Bounded so a
# model that keeps asking cannot pause forever.
MAX_ASK_ATTEMPTS = 3

# How many tool rounds one domain activation may spend before it must report what it has.
# Layered beside the ask bound, not shared with it: one `ask_user` costs no tool round, and
# running out of tool rounds costs no ask attempt (spec §4.8.3).
MAX_TOOL_ROUNDS = 6


# A click that named neither of the two options the card showed. It cannot be treated as a
# confirmation, and it is not the user's chat text either -- it is a malformed button event.
INVALID_DECISION_SUMMARY = "本次确认没有指向预览上的选项，未办理任何操作，请重新确认。"

# The whole-turn status, translated into the vocabulary `subtask_end` already publishes.
# `docs/ARCHITECTURE.md` freezes SUCCEEDED/FAILED/DENIED for that event, and the frontend
# renders `subtask_end` and `tool_result` in one activity stream -- a second vocabulary
# there would read like a second system. The real turn status still travels in
# `domain_results` as `turn_status`, which is what the Supervisor's model reads.
#
# `UNKNOWN` is the one entry where that frozen vocabulary is genuinely too small: a module can
# report that a side-effecting request's outcome is unknown, and the activity stream has no word
# for "neither succeeded nor failed". It is mapped to FAILED because that is the only available
# value that does not claim success -- and the honest wording travels in `summary` and in
# `turn_status`, which is what the Supervisor's model reads and repeats to the user. Widening
# `ToolStatus` would change the shared tool protocol and the frozen event contract, so it is
# recorded here as a known limitation rather than done silently.
_TURN_EVENT_STATUS: Mapping[TurnStatus, ToolStatus] = {
    TurnStatus.COMPLETED: ToolStatus.SUCCEEDED,
    TurnStatus.WAITING_INPUT: ToolStatus.SUCCEEDED,
    # A preview that is waiting for a click did its job: the pause it opened is a normal
    # step, not a failure. The card itself travels as an `interrupt`, not as this event.
    TurnStatus.WAITING_CONFIRMATION: ToolStatus.SUCCEEDED,
    TurnStatus.UNSUPPORTED: ToolStatus.DENIED,
    TurnStatus.FAILED: ToolStatus.FAILED,
    TurnStatus.UNKNOWN: ToolStatus.FAILED,
}


@dataclass(frozen=True, slots=True)
class DomainOutcome:
    """A domain's decision: a tool request, a question for the user, or a conclusion."""

    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    ask: dict[str, Any] | None = None
    summary: str = ""

    @property
    def wants_tool(self) -> bool:
        return self.tool_name is not None


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What one tool call contributes: prose for the model, ids for the client."""

    summary: str
    status: ToolStatus
    business_ids: dict[str, str] = field(default_factory=dict)


def _history_turns(state: AgentState, message: str) -> list[dict[str, str]]:
    """The checkpoint transcript without the message being answered.

    The session path has no ledger, so this transcript is the whole history a domain may see.
    The current message is already in it -- `new_agent_state` appends it -- and it is also
    handed to the handler as its own argument, so leaving it in would show the same sentence
    twice in one prompt. Only the trailing turn is dropped, and only when it *is* the current
    message: an earlier identical sentence is a real earlier sentence.
    """

    turns = message_dicts(state.get("messages"))
    if turns:
        last = turns[-1]
        if (
            last.get("role") == "user"
            and isinstance(last.get("content"), str)
            and last["content"].strip() == message.strip()
        ):
            return turns[:-1]
    return turns


def _handoff_for(
    spec: DomainSpec,
    state: AgentState,
    assembler: ContextAssembler | None,
    turn: TurnContext | None,
    message: str,
) -> Handoff:
    """The memory scope one whole-turn call runs in.

    `turn` is the only thing that knows which conversation this is and which ledger sequence
    the prompt belongs to; a session-path request has none, and the handoff reports that as
    `no_conversation` rather than inventing an identity out of `session_id`.
    """

    stored = state.get("domain_context")
    own = stored.get(spec.agent_id) if isinstance(stored, Mapping) else None
    records = own.get("records") if isinstance(own, Mapping) else None
    return Handoff(
        conversation_id=turn.conversation_id if turn is not None else None,
        current_message_id=turn.message_id if turn is not None else None,
        current_sequence=turn.sequence if turn is not None else 0,
        current_user_text=message,
        agent_id=spec.agent_id,
        assembler=assembler,
        transcript_fallback=describe_transcript(_history_turns(state, message)),
        domain_records=records if isinstance(records, list) else (),
    )


@contextmanager
def domain_scope(
    spec: DomainSpec,
    state: AgentState,
    assembler: ContextAssembler | None,
    turn: TurnContext | None,
    message: str,
) -> Iterator[Handoff]:
    """The memory scope a module-owned graph node runs in.

    A module that builds its own graph owns its nodes, so the base cannot open this scope
    for it. The node opens it itself: `handoff.view()` assembles the conversation and
    `handoff.publish()` writes the memory back, both inside the context.

    It is a real context manager, not a wrapper that calls `subgraph.invoke()`: the scope has
    to open and close around the node's own work, and a nested invoke would move the module
    outside LangGraph's interrupt, stream and checkpoint semantics.

    It is also where a model call made by this node becomes attributable: the node's own
    `agent_id` is merged into the audit context, so a domain's calls are stored as that
    domain's rather than as whoever the turn named. The merge starts from the current
    context, so the conversation and run survive it; `merged` ignores `None`, and the empty
    fallback keeps this total for a node running outside any turn.
    """

    handoff = _handoff_for(spec, state, assembler, turn, message)
    carries = (current_context() or AuditContext()).merged(agent_id=spec.agent_id)
    with audit_context(carries):
        with conversation_scope(handoff):
            yield handoff


def domain_node(
    spec: DomainSpec,
    assembler: ContextAssembler | None,
    turn: TurnContext | None,
    node: Callable[[AgentState], Any],
) -> Callable[[AgentState], Any]:
    """A module-owned node that names its own agent on the rows its model calls write.

    'domain_scope' is what attributes a call, and a graph node is where it has to open: a
    module that builds its own graph owns its nodes, so the base cannot open it for them.
    Wrapping the node at registration keeps the scope around the node's own work, which a
    nested 'subgraph.invoke()' would not -- that would move the module outside LangGraph's
    interrupt, stream and checkpoint semantics.

    A module that needs the memory the scope carries opens it itself and uses the handoff;
    wealth publishes structured context through it. A module that only needs its calls
    attributed wraps its node here, and the handoff goes unused.

    Only the registered node is wrapped. A node that calls another one directly runs inside
    the scope it already has, so the inner call does not open a second one.
    """

    # Deliberately unannotated. `StateGraph.add_node` infers a node's input schema from the
    # annotation on its first parameter (`langgraph/graph/state.py`, the
    # `inferred_input_schema` branch), and annotating this parameter with the base
    # `AgentState` makes the node read only the channels that schema declares. A module's
    # own state carries more than that -- the card's `card_action_id`, for one -- so a
    # resumed node would lose the values it wrote on the previous step. Left bare, the graph
    # falls back to its own `state_schema`, which is the module's. The module's node keeps
    # whatever annotation it has: LangGraph reads this wrapper's signature, not the inner one.
    def in_scope(state):
        message = current_request(state)
        with domain_scope(spec, state, assembler, turn, message):
            out = node(state)
        return out

    return in_scope


def _new_question_id() -> str:
    """A name for a freshly created question.

    Generated only where a question is created, never on replay: the pause reads its
    question back from the checkpoint, so the id a client is shown is the id it must echo.
    """

    return f"q_{uuid4().hex}"


def _confirmation_pause(spec: DomainSpec, result: TurnResult) -> dict[str, Any]:
    """The checkpointed pause behind one confirmation box.

    The card is the module's own projection, carried through unchanged: the base neither
    reads nor rewrites the amount, the product or the account, so what the user confirms is
    exactly what the module froze. The two button labels are the module's too, and the base
    maps the chosen one back to confirm/cancel with the same values it built the card from.
    """

    confirmation = result.confirmation
    assert confirmation is not None  # TurnResult refuses this status without one
    return {
        "pending_question": {
            "kind": "confirmation",
            "question_id": _new_question_id(),
            "agent_id": spec.agent_id,
            "fields": [
                {
                    "name": "decision",
                    "label": "操作决定",
                    "type": "select",
                    "required": True,
                    "options": [confirmation.confirm_label, confirmation.cancel_label],
                }
            ],
            "display": result.summary,
            "attempt": 1,
            "confirmation": confirmation.as_payload(),
        },
        "pending_answer": None,
        "collected_inputs": {},
        "turn_phase": "waiting_for_user",
    }


def _merged_domain_context(
    state: AgentState | None, agent_id: str, updates: tuple[dict[str, Any], ...]
) -> dict[str, Any] | None:
    """The whole `domain_context` channel with this agent's slice extended, or `None` to write nothing.

    The full map is returned, not just this agent's key: `domain_context` is a last-value
    channel, so a partial write would replace the channel and erase every other domain's
    memory. Returning the merged map is what keeps one domain's turn from clearing another's.

    Only the newest `MAX_CONTEXT_RECORDS` of this agent's records are kept. A record that fails
    validation is dropped rather than raised: memory is an optimisation, and losing one clue
    must not fail a turn whose answer is already correct.
    """

    stored = state.get("domain_context") if state is not None else None
    merged: dict[str, Any] = dict(stored) if isinstance(stored, Mapping) else {}
    own = merged.get(agent_id)
    previous = own.get("records") if isinstance(own, Mapping) else None
    kept: list[dict[str, Any]] = (
        [dict(record) for record in previous if isinstance(record, Mapping)]
        if isinstance(previous, list)
        else []
    )

    for update in updates:
        payload = update.get("payload") if isinstance(update, Mapping) else None
        try:
            accepted = validate_context_update(payload) if isinstance(payload, Mapping) else {}
        except ValueError:
            continue
        if not accepted:
            continue
        source = update.get("source")
        kept.append(
            {
                "payload": accepted,
                "source": dict(source) if isinstance(source, Mapping) else {},
            }
        )

    if not kept:
        return None
    merged[agent_id] = {"records": kept[-MAX_CONTEXT_RECORDS:]}
    return merged


def _conclude_turn(
    spec: DomainSpec,
    result: TurnResult,
    writer: Any,
    state: AgentState | None = None,
    updates: tuple[dict[str, Any], ...] = (),
    *,
    grounding_blocked: str | None = None,
) -> dict[str, Any]:
    """Publish a whole-turn module's ending, then hand its result back to the Supervisor.

    The event carries the mapped `ToolStatus` because that is the vocabulary the activity
    stream already speaks; `domain_results` carries the module's own `TurnStatus`, because
    that is the one the Supervisor's model reads.

    `updates` are the memory records the module published during its call. They are validated
    again here -- the module's own scope already refused anything off the whitelist, and this
    is the second gate before a record reaches a checkpoint -- merged into the agent's slice of
    `domain_context`, and trimmed to the newest `MAX_CONTEXT_RECORDS`.
    """

    event = {
        "event": "subtask_end",
        "agent_id": spec.agent_id,
        "status": _TURN_EVENT_STATUS[result.status].value,
        "summary": result.summary,
        # The module's own status, next to the frozen event vocabulary: it is what lets
        # the page keep "待核实" for an unknown fund write instead of the "FAILED" the
        # three-value event vocabulary can only offer.
        "turn_status": result.status.value,
    }
    entry = {
        "agent_id": spec.agent_id,
        "summary": result.summary,
        "turn_status": result.status.value,
        "facts": dict(result.facts),
        "business_ids": dict(result.business_ids),
    }
    if grounding_blocked:
        event["grounding_blocked"] = grounding_blocked
        entry["grounding_blocked"] = grounding_blocked
    writer(event)
    conclusion: dict[str, Any] = {
        "domain_results": [entry],
        "active_agent": None,
        "awaiting_agent": spec.agent_id if result.status is TurnStatus.WAITING_INPUT else None,
    }
    if result.status is TurnStatus.WAITING_CONFIRMATION:
        # The preview is neither withdrawn nor answered in chat: the turn pauses on the card.
        # The pause is a real checkpoint interrupt, which is what makes the confirmation
        # survive a reload and what binds a click to the preview the user actually saw.
        conclusion.update(_confirmation_pause(spec, result))
    else:
        conclusion.update(idle_work_state())
    memory = _merged_domain_context(state, spec.agent_id, updates)
    if memory is not None:
        conclusion["domain_context"] = memory
    return conclusion


def _ledger_answer(answer: Any) -> dict[str, str] | None:
    """The ledger message for a resume answer, when the runtime named one.

    A resume that carries no ledger id is the pre-persistence path -- a direct runtime call
    with a bare `{value}` envelope. Writing a message for it would mean inventing an id, and
    the whole point of carrying the id is that the transcript and the ledger agree.
    """

    if not isinstance(answer, Mapping):
        return None
    message_id = answer.get("message_id")
    content = answer.get("content")
    if not isinstance(message_id, str) or not message_id or not isinstance(content, str):
        return None
    return {"role": "user", "content": content, "id": message_id}


def wait_for_user(state: AgentState) -> dict[str, Any]:
    """Pause on the question the previous node persisted.

    This node must never call the model. LangGraph replays the node that interrupted, so
    the question has to come back from the checkpoint: generating it again could produce a
    different question, and the user's answer to the first one would be handed to it. The
    answer is stored exactly as it arrived and validated where it is consumed.

    The answer also joins the transcript, under the ledger's own message id when the runtime
    supplied one. The ledger is the user-visible history, so an answer that only lived in
    `pending_answer` would be a message the user sent that the conversation never contained.
    """

    question = state.get("pending_question")
    if not question:
        raise DecisionError("等待节点没有可回答的问题")
    answer = interrupt(dict(question))
    update: dict[str, Any] = {"pending_answer": answer, "turn_phase": "process_answer"}
    written = _ledger_answer(answer)
    if written is not None:
        update["messages"] = [written]
    return update


def _action_kind(question: dict[str, Any], answer: dict[str, Any]) -> str | None:
    """Which button the answer names, or `None` when it names neither.

    Two spellings are accepted for the same click, and both are machine-checked against the
    card: the machine value (`confirm`/`cancel`) that the button route sends, and the label
    the card itself showed, which is what a client that submits the rendered form sends. The
    labels come from the module, so the base never guesses at wording.
    """

    confirmation = question.get("confirmation") or {}
    decision = answer.get("decision") if isinstance(answer, Mapping) else None
    if decision is None or not isinstance(confirmation, Mapping):
        return None
    if decision in (TurnAction.CONFIRM, confirmation.get("confirm_label") or "确认"):
        return TurnAction.CONFIRM
    if decision in (TurnAction.CANCEL, confirmation.get("cancel_label") or "取消"):
        return TurnAction.CANCEL
    return None


def _conclude_without_tool(
    spec: DomainSpec, summary: str, status: ToolStatus, writer: Any
) -> dict[str, Any]:
    """A domain ending that needed no tool call, published the way a tool's ending is."""

    writer(
        {
            "event": "subtask_end",
            "agent_id": spec.agent_id,
            "status": status.value,
            "summary": summary,
        }
    )
    return {
        "active_agent": None,
        "awaiting_agent": None,
        "domain_results": [
            {
                "agent_id": spec.agent_id,
                "summary": summary,
                "instruction": "",
                "business_ids": {},
            }
        ],
        **idle_work_state(),
    }


def conclude_domain(
    spec: DomainSpec,
    state: AgentState,
    summary: str,
    status: ToolStatus,
    writer: Any,
    *,
    business_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Publish a domain's ending and hand its result back to the Supervisor.

    `business_ids` is what the activation's tool rounds collected. The report is the
    ending now, so the ids would otherwise never reach `domain_results` and the
    Supervisor's final event.
    """

    writer(
        {
            "event": "subtask_end",
            "agent_id": spec.agent_id,
            "status": status.value,
            "summary": summary,
        }
    )
    return {
        "active_agent": None,
        "awaiting_agent": None,
        "domain_results": [
            {
                "agent_id": spec.agent_id,
                "summary": summary,
                "instruction": state.get("goal") or "",
                "business_ids": dict(business_ids or {}),
            }
        ],
        **idle_work_state(),
    }


def _exhausted(spec: DomainSpec, fields: list[PendingField], writer: Any) -> dict[str, Any]:
    """Conclude with a readable reason instead of pausing forever."""

    # The label, not the name: this sentence is shown to the user, and a name is the key the
    # client posts back, not something written for a person to read.
    wanted = "、".join(field["label"] for field in fields) if fields else "必要信息"
    summary = f"未能获得有效的{wanted}，本次处理已结束。"
    # Published like any other conclusion, so the client sees the domain end instead of
    # the stream simply stopping after the last interrupt. The status is FAILED: running
    # out of attempts is a failure to obtain the input, not a successful domain run.
    writer(
        {
            "event": "subtask_end",
            "agent_id": spec.agent_id,
            "status": ToolStatus.FAILED.value,
            "summary": summary,
        }
    )
    return {
        "active_agent": None,
        # A domain that ran out of asks is no longer waiting: without this, an
        # `awaiting_agent` set by an earlier turn would survive and the next turn's
        # Supervisor would be told a domain is waiting that is not.
        "awaiting_agent": None,
        "domain_results": [
            {
                "agent_id": spec.agent_id,
                "summary": summary,
                "instruction": "",
                "business_ids": {},
            }
        ],
        **idle_work_state(),
    }


def _next_attempt(question: dict[str, Any]) -> int:
    return int(question.get("attempt") or 1) + 1


def _awaiting(
    spec: DomainSpec,
    outcome: DomainOutcome,
    writer: Any,
    *,
    question: dict[str, Any] | None = None,
    reask: str | None = None,
    collected: dict[str, Any] | None = None,
    attempt: int | None = None,
    question_id: str | None = None,
    domain_thread: list[dict[str, Any]] | None = None,
    tool_rounds: int | None = None,
) -> dict[str, Any]:
    """Record the question to wait on, or conclude if the agent has asked too often.

    `reask` is set only when re-presenting a question the user already saw, and carries the
    reason it is being shown again. `question_id` is passed only in the same case: a
    question created here is a new one and is named accordingly.

    `domain_thread` and `tool_rounds` are the activation's working memory at the moment it
    paused. Both are optional because a re-presented question (`reask`) is not a new
    activation: omitting them is what lets the checkpoint keep the values it already holds.
    """

    # The model names the fields; the domain says how they look. Enriching here, at the one
    # place a question is created, is what keeps every later reader -- the client, the model,
    # the exhausted path -- reading objects rather than having to know both shapes.
    names = list(outcome.ask.get("fields") or []) if outcome.ask else []
    # A question with no fields is unanswerable, so it gets one free-text field rather than
    # being written to the checkpoint as an empty list. Blank names land here too: they are
    # dropped by `resolve_fields`, so the list they leave is just as empty.
    fields = resolve_fields(spec.declaration, names) or [dict(FREE_TEXT_FIELD)]
    display = str(outcome.ask.get("display") or "请补充信息。") if outcome.ask else ""
    if reask is not None:
        # Same question, shown again, saying why. The checkpoint already holds objects, so
        # this is not re-enriched -- re-enriching could silently re-label a question the
        # user is looking at.
        fields = list((question or {}).get("fields") or fields)
        display = f"{reask}原问题：{(question or {}).get('display') or display}"
    attempt = attempt if attempt is not None else 1
    if attempt > MAX_ASK_ATTEMPTS:
        # Bounded, like the delegation bound: a model that keeps asking must still end.
        return _exhausted(spec, fields, writer)

    next_state: dict[str, Any] = {
        "pending_question": {
            "kind": "input",
            "question_id": question_id or _new_question_id(),
            "agent_id": spec.agent_id,
            "fields": fields,
            "display": display,
            "attempt": attempt,
        },
        "pending_answer": None,
        "turn_phase": "waiting_for_user",
    }
    if collected is not None:
        next_state["collected_inputs"] = collected
    if domain_thread is not None:
        next_state["domain_thread"] = domain_thread
    if tool_rounds is not None:
        next_state["tool_rounds"] = tool_rounds
    return next_state


def _read_answer(raw: Any, question: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Check the resume envelope against the question that was actually asked.

    Two different failures, deliberately told apart:

    - the envelope does not name *this* question -- it answers one this thread is no longer
      waiting on, or it is not an envelope at all. Re-asking would be wrong, because the
      client is not confused about the fields; it is answering a question that is gone.
      That is a recovery protocol error, and it is raised as one.
    - the envelope names this question but its `value` is unusable. The question is still
      open, so it is re-asked in the domain's own words with a readable reason.

    The envelope itself is never merged into the collected inputs: `question_id` is
    addressing, not data.
    """

    if not isinstance(raw, Mapping) or raw.get("question_id") != question.get("question_id"):
        raise AppError("INVALID_INTERRUPT_ANSWER", "补充信息没有指向当前问题", 409)
    value = raw.get("value")
    if not isinstance(value, Mapping):
        return None, "补充信息的格式不正确。"
    fields = list(question.get("fields") or [])
    # By name: the fields are objects now, so comparing the answer's keys against the objects
    # themselves would never match.
    if set(value.keys()) != {field["name"] for field in fields}:
        return None, "补充信息与当前问题不匹配。"
    # Only a required field must carry something: a field the module marked optional is one
    # the UI deliberately lets the user leave blank, so rejecting it here would burn an ask
    # attempt on an answer the client had already accepted as complete.
    blank = [
        field["name"]
        for field in fields
        if field.get("required", True) and not str(value.get(field["name"], "")).strip()
    ]
    if blank:
        return None, "必填的补充信息不能为空。"
    return dict(value), None


def _domain_outcome(decision: DomainDecision) -> DomainOutcome:
    """The protocol's decision in the shape the domain nodes already speak."""

    if decision.ask is not None:
        return DomainOutcome(ask=decision.ask)
    if decision.tool_name is not None:
        return DomainOutcome(tool_name=decision.tool_name, arguments=decision.arguments)
    return DomainOutcome(summary=decision.summary)


def _call_tool(
    spec: DomainSpec, outcome: DomainOutcome, writer: Any, *, idempotency_key: str | None
) -> ToolResult:
    """One attempt: publish the call, hand it to the handle, publish what came back."""

    call_id = f"{spec.agent_id}:{outcome.tool_name}:{uuid4().hex}"
    writer(
        {
            "event": "tool_call",
            "agent_id": spec.agent_id,
            "tool_name": outcome.tool_name,
            "call_id": call_id,
            "arguments": outcome.arguments,
        }
    )
    result = spec.handle.call(
        {
            "tool": outcome.tool_name,
            "arguments": outcome.arguments,
            "call_id": call_id,
            "idempotency_key": idempotency_key,
        }
    )
    data = result.data if isinstance(result.data, dict) else None
    # A result with no envelope -- a failure or a refusal -- keeps `data: null` rather than
    # an all-null projection. `data` being present is what says "a tool returned an
    # envelope", so inventing one with three empty fields would make a failed call
    # indistinguishable from a successful call that returned nothing, and would leave the
    # client's `data === null` branch unreachable for exactly the case it is there for.
    envelope = (
        None
        if data is None
        else {
            # The envelope is projected, not forwarded: `facts` is the module's own
            # material and never leaves the server. A frame is read by the browser, the
            # devtools network tab and any screenshot.
            "summary": data.get("summary"),
            "display": data.get("display"),
            "business_ids": data.get("business_ids"),
        }
    )
    writer(
        {
            "event": "tool_result",
            "agent_id": spec.agent_id,
            "tool_name": result.tool_name,
            "call_id": result.call_id,
            "status": result.status.value,
            "data": envelope,
            # Metadata about *this attempt*, alongside `status` -- deliberately a sibling of
            # `data` rather than a key inside it, because it describes the call and not the
            # envelope. Gate 2's client branches on this position.
            "error": result.error,
            "error_code": result.error_code,
        }
    )
    return result


def _may_retry(spec: DomainSpec, result: ToolResult) -> bool:
    """Whether one more attempt is both useful and safe.

    Useful: only a transient failure can succeed on a second try.
    Safe: a write is not retried when the failure itself is `IDEMPOTENCY_KEY_REQUIRED` --
    the retry is the same intent and the bank has to be able to tell, and that code means
    the call carried no key to tell it by. Without a key the adapter already refused it, so
    there is nothing to retry anyway.
    """

    if result.status is not ToolStatus.FAILED or not result.retryable:
        return False
    declared = spec.handle.spec(result.tool_name)
    if declared is None:
        return False
    if declared.side_effect == SideEffect.WRITE:
        return result.error_code != ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    return True


def run_tool(spec: DomainSpec, outcome: DomainOutcome, writer: Any) -> ToolOutcome:
    """Call the agent's own handle, once, retrying one transient failure.

    The idempotency key is minted here, once, *before* the retry loop: the retry is a new
    attempt at the same intent, so it has to carry the same key. The `call_id` is minted
    per attempt instead (`_call_tool`), because "which attempt is this" is a different
    question from "is this the same intent" -- and a key derived from the attempt would make
    the bank see two transfers.
    """

    # Minted for every tool, reads included, even though only a write needs it. The key has
    # to exist *before* the retry loop, and at that moment the tool may not be in the handle
    # at all -- which is precisely the case `_may_retry` defends against.
    key = f"{spec.agent_id}:{outcome.tool_name}:{uuid4().hex}"
    result = _call_tool(spec, outcome, writer, idempotency_key=key)
    if _may_retry(spec, result):
        # One retry, and it is published as its own call/result pair: the failed attempt
        # really happened, so rewriting it would hide a call the adapter was handed.
        result = _call_tool(spec, outcome, writer, idempotency_key=key)
        if _may_retry(spec, result):
            raise UpstreamUnavailable("TOOL_UNAVAILABLE")

    return tool_outcome(result)


def pending_context(question: dict[str, Any], collected: dict[str, Any]) -> str:
    """The answer, as legal JSON, alongside the question it belongs to.

    The model is told which question was asked so it cannot mistake the answer for some
    other field, and the payload is `json.dumps` rather than a Python repr so a strict
    parser can read it.
    """

    return "用户刚补充的信息（JSON）：\n" + json.dumps(
        {
            "pending_question": {
                "agent_id": question.get("agent_id"),
                # Name and label both: the model has to answer with the name, but the label
                # is what the user was shown, and the two are what tie the answer to the ask.
                "fields": [
                    {"name": field["name"], "label": field["label"]}
                    for field in question.get("fields") or []
                ],
                "display": question.get("display"),
            },
            "user_answer": collected,
        },
        ensure_ascii=False,
    )


def _current_request(state: AgentState) -> str:
    """The user message this turn answers: the transcript's last non-empty user line."""

    for message in reversed(message_dicts(state.get("messages"))):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def tool_outcome(result: ToolResult) -> ToolOutcome:
    """A tool result projected the way a loop hands it back to the model.

    One definition, because there are two loops: the Supervisor's generic one and the card
    graph's read round. The envelope's `summary` is the field the seam defines as the
    model-facing one; `facts` is the module's own, `display` belongs to the frontend, and
    `business_ids` travels to the client separately.
    """

    if result.status is ToolStatus.SUCCEEDED:
        # A tool that returns no readable summary still has to say something, hence the
        # fallback.
        data = result.data if isinstance(result.data, dict) else {}
        prose = data.get("summary")
        ids = data.get("business_ids")
        return ToolOutcome(
            summary=prose
            if isinstance(prose, str) and prose
            else f"工具 {result.tool_name} 已完成。",
            status=result.status,
            business_ids=dict(ids) if isinstance(ids, dict) else {},
        )
    # A refusal and an upstream failure both end the domain without a result, but they are
    # different facts, so the status the client sees is the tool's own.
    return ToolOutcome(
        summary=f"工具 {result.tool_name} 未成功：{result.error}",
        status=result.status,
    )


def tool_envelope(outcome: DomainOutcome, tool: ToolOutcome) -> dict[str, str]:
    """What a tool's answer looks like inside the domain thread.

    Only the summary the seam already wrote for the model goes back. facts, display and
    business_ids have their own channels and must not widen just because there is a loop.
    """

    return {
        "role": "user",
        "content": json.dumps(
            {"tool": outcome.tool_name, "status": tool.status.value, "result": tool.summary},
            ensure_ascii=False,
        ),
    }


def overflow_outcome(spec: DomainSpec, summaries: list[str]) -> DomainOutcome:
    """Out of rounds: report what was actually done instead of dropping it silently."""

    done = "；".join(summary for summary in summaries if summary) or "未获得任何结果"
    logger.warning("领域 %s 已达工具轮次上限 %d", spec.agent_id, MAX_TOOL_ROUNDS)
    return DomainOutcome(summary=f"已达本领域本轮处理上限，已完成的部分：{done}")


# Stable public names (roadmap A2). Underscore names remain the call-site identifiers
# supervisor.py already uses; both spellings resolve to the same implementation.
read_answer = _read_answer
call_tool = _call_tool
conclude_turn = _conclude_turn
awaiting = _awaiting
action_kind = _action_kind
domain_outcome = _domain_outcome
may_retry = _may_retry
conclude_without_tool = _conclude_without_tool
current_request = _current_request
next_attempt = _next_attempt
# The tool-round helpers moved here (roadmap A6-a) so module code never reaches into the
# Supervisor for them; the underscore spellings stay for existing call sites.
_tool_envelope = tool_envelope
_overflow_outcome = overflow_outcome
