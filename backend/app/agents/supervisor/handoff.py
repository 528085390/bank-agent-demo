"""The handoff loop: one streamed Supervisor decision turn.

`supervisor_node` (in `graph.py`) asks the model which domain runs next. What lives here is
everything that turns one streamed answer into a decision: the user-turn rule, the delta
coalescer that publishes text while the turn is still being decided, the tool-calling turn
itself, and the decision's translation into `SupervisorOutcome`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.decisions import SupervisorDecision, parse_decision
from app.agents.prompts import handoff_tool, user_turn
from app.agents.toolcalls import collect_stream
from app.integration.model_client import ModelClient

# What the Supervisor says when the model returned neither an agent nor a reply. The
# decision contract requires one of the two, so this is the model failing the contract
# rather than a finished turn -- and it must not read as if work had been done.
NO_REPLY_FALLBACK = "抱歉，我没能理解这个请求。请说明你想办理的银行业务。"


# How text reaches the client while a turn is still being decided (spec §4.3). Both thresholds
# coalesce rather than pace: whichever comes first flushes the buffer, and the end of the
# stream flushes whatever is left. A frame per token is a frame per token on the wire; a frame
# per sentence is no better than the final event this phase exists to replace.
DELTA_FLUSH_SECONDS = 0.04
DELTA_FLUSH_CHARS = 24


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    """The Supervisor's decision for one turn: one agent, or the user-facing reply."""

    agent_id: str | None
    instruction: str = ""
    reply: str = ""


def supervisor_user_turn(
    request: str,
    menu: list[tuple[str, str]],
    awaiting: str | None = None,
) -> str:
    ids = "、".join(agent_id for agent_id, _ in menu)
    rules = [
        "你是总调度，不直接办理业务：需要办理时调用 handoff 工具，不需要时直接回答。"
        f"handoff 的 agent_id 只能从这几个里选：{ids}。",
    ]
    if awaiting:
        rules.append(
            f"上一轮 {awaiting} 正在等用户补充信息；如果用户这句话是在回答它，请仍然交给 {awaiting}。"
        )
    return user_turn(request, rule="\n".join(rules))


class _DeltaBatch:
    """Coalesces text deltas into frames, and remembers every character that went by.

    The full text is kept whether or not it was flushed, because the terminal event has to
    carry it: a client that missed a frame still ends up with the whole reply (spec §4.3).
    """

    def __init__(self, writer: Callable[[dict[str, Any]], Any]) -> None:
        self._writer = writer
        self._pending: list[str] = []
        self._all: list[str] = []
        self._last = time.monotonic()
        self._started = False

    @property
    def text(self) -> str:
        return "".join(self._all)

    @property
    def started(self) -> bool:
        """Whether any text has arrived. This is the watershed for retrying (spec §4.6)."""

        return self._started

    def add(self, text: str) -> None:
        if not text:
            return
        self._started = True
        self._pending.append(text)
        self._all.append(text)
        if (
            sum(len(part) for part in self._pending) >= DELTA_FLUSH_CHARS
            or time.monotonic() - self._last >= DELTA_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self._writer({"event": "reply_delta", "text": "".join(self._pending)})
        self._pending = []
        self._last = time.monotonic()


def _stream_decision(
    model: ModelClient,
    prompt: str,
    user_content: str,
    menu: list[tuple[str, str]],
    known: set[str],
    *,
    awaiting: str | None = None,
    emit: Callable[[str], None] | None = None,
) -> SupervisorDecision:
    """One streamed Supervisor turn, read as a decision.

    `emit` is called with text as it arrives, before the decision is known: at the start of a
    stream nobody can tell whether this is the answer or a narration next to a handoff
    (spec §4.3).
    """

    messages = (
        {"role": "system", "content": prompt},
        {"role": "user", "content": supervisor_user_turn(user_content, menu, awaiting)},
    )
    text, calls = collect_stream(model, messages, (handoff_tool(menu),), emit=emit)
    return parse_decision(text, calls, known)


def _outcome_from(decision: SupervisorDecision) -> SupervisorOutcome:
    """The tool-calling decision in the shape the dispatch code already speaks.

    The empty-reply fallback is applied here rather than in decisions.py, because the constant
    is this module's and decisions.py must not import it back.
    """

    if decision.agent_id is None:
        return SupervisorOutcome(agent_id=None, reply=decision.reply or NO_REPLY_FALLBACK)
    return SupervisorOutcome(agent_id=decision.agent_id, instruction=decision.instruction)
