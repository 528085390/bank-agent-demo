"""Reading a structured decision out of a model's text turn.

The model boundary is Chat Completions text: the decision is a JSON object the model
was asked to emit. Parsing is deliberately lenient about the surrounding prose and
strict about the shape, so a chatty model does not become a silent failure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.prompts import HANDOFF_TOOL_NAME
from app.integration.model_client import ToolCall
from app.tools.schema import ASK_USER_DISPLAY, ASK_USER_FIELDS, ASK_USER_TOOL_NAME


class DecisionError(ValueError):
    """The model's turn was not a usable decision object."""


@dataclass(frozen=True, slots=True)
class Handoff:
    """One domain agent to hand the request to, with what to tell it."""

    agent_id: str
    instruction: str


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """What the Supervisor decided this turn, on the tool-calling protocol.

    `agent_id` is the agent to dispatch to now; `queued` is what the model also asked for,
    in order. A turn is either a dispatch (`agent_id` set, `reply` empty) or an answer
    (`agent_id` None, `reply` set) -- never both, which is the whole point of moving the
    routing decision into a tool call.
    """

    agent_id: str | None = None
    instruction: str = ""
    reply: str = ""
    narration: str = ""
    queued: tuple[Handoff, ...] = ()


def parse_decision(
    text: str, tool_calls: Sequence[ToolCall], known: set[str]
) -> SupervisorDecision:
    """Read a tool-calling turn: where the request goes, and what to say about it.

    Text alongside a handoff is narration, not the answer. That is the shape the providers
    document, and it is also the honest reading: a model that says "我先查一下" and then calls
    the tool has not answered anything yet. Only the text of a turn with no handoff is an
    answer.

    The empty-reply fallback is deliberately *not* applied here: it lives in supervisor.py
    (NO_REPLY_FALLBACK), which imports this module, and a constant imported the other way would
    make a cycle out of a string.
    """

    handoffs: list[Handoff] = []
    for call in tool_calls:
        if call.name != HANDOFF_TOOL_NAME:
            raise DecisionError(f"Supervisor 调用了未知工具: {call.name!r}")
        arguments = call.arguments if isinstance(call.arguments, Mapping) else {}
        agent_id = arguments.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise DecisionError("handoff 没有指明领域 Agent")
        agent_id = agent_id.strip()
        if agent_id not in known:
            raise DecisionError(f"Supervisor 选择了未知 Agent: {agent_id!r}")
        instruction = arguments.get("instruction")
        handoffs.append(
            Handoff(
                agent_id=agent_id,
                instruction=instruction if isinstance(instruction, str) else "",
            )
        )
    spoken = text.strip()
    if not handoffs:
        return SupervisorDecision(reply=spoken)
    return SupervisorDecision(
        agent_id=handoffs[0].agent_id,
        instruction=handoffs[0].instruction,
        narration=spoken,
        queued=tuple(handoffs[1:]),
    )


def extract_json_object(text: str | None) -> dict[str, Any]:
    """Return the first JSON object in the text, ignoring anything around it."""

    if not isinstance(text, str) or not text.strip():
        raise DecisionError("模型没有返回内容")
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise DecisionError("模型返回的不是 JSON 对象")


@dataclass(frozen=True, slots=True)
class DomainDecision:
    """What one domain model call decided. At most one of the three endings is set."""

    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    ask: dict[str, Any] | None = None
    summary: str = ""


def parse_domain_decision(
    text: str | None,
    tool_calls: Sequence[ToolCall],
    known: set[str] | frozenset[str],
) -> DomainDecision:
    """Turn one domain turn's output into a decision.

    The three endings are expressed by the protocol, not by prose: a business tool call is a
    tool call, asking the user is a tool call, and a report is text with no call. Text next to
    a call is narration and must not be mistaken for the report -- otherwise a domain that
    says "let me look that up" finishes the turn without looking anything up.

    Two calls in one turn is refused here. The loop that can hold two is P5 (Task 8), and it
    decides that by looping, not by letting one turn carry a queue.
    """

    if len(tool_calls) > 1:
        raise DecisionError("领域 Agent 一次只能请求一个工具调用")

    if not tool_calls:
        summary = (text or "").strip()
        if not summary:
            raise DecisionError("模型没有返回内容")
        return DomainDecision(summary=summary)

    call = tool_calls[0]
    if call.name not in known:
        raise DecisionError(f"领域 Agent 请求了未知工具: {call.name!r}")

    if call.name == ASK_USER_TOOL_NAME:
        declared = call.arguments.get(ASK_USER_FIELDS)
        fields = [
            name
            for name in (declared if isinstance(declared, list) else [])
            if isinstance(name, str) and name.strip()
        ]
        display = call.arguments.get(ASK_USER_DISPLAY)
        if not fields:
            raise DecisionError("ask_user 没有指定要问的字段")
        if not isinstance(display, str) or not display.strip():
            raise DecisionError("ask_user 没有给出要显示的问题")
        return DomainDecision(ask={"kind": "input", "fields": fields, "display": display.strip()})

    return DomainDecision(tool_name=call.name, arguments=dict(call.arguments))
