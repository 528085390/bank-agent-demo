"""The token budget, the round boundaries, and the two cuts the context manager makes.

Every compression decision is arithmetic on one number: what a message costs
(`app.context.estimator`). This module turns the configured window into the three derived
budgets, and answers the two structural questions an assembly asks.

**Where may a summary stop?** Only on an assistant message. A *round* is a user message plus
the reply that closes it, and both cuts below move in whole rounds, so `covered_to_sequence`
can never land on a user message (spec §5.4) and the message the user just sent is never
summarized away (INV-1).

**How much may be cut?** `leading_strip` removes the fewest whole rounds that bring the tail
under a target; `chunk_for_summary` packs whole rounds into summary-sized chunks, which is
what bounds the input of a summary call (spec §5.2).

The budget is validated once, at process start: `Settings` calls `Budget.validate`, so a
combination that could never shrink the view below its own trigger is a startup failure
rather than a surprise on the first long conversation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.context.estimator import estimate_message_tokens
from app.context.ledger import LedgerMessage

if TYPE_CHECKING:
    from app.core.config import Settings


# Absolute ceilings: a guardrail, not a freeze.
#
# `model_context_limit` is the one field a deployer writes that directly drives memory and
# call cost, and 2_000_000 is the scale of the largest publicly available window (1M-2M).
# That is 15.6x the current default of 128000, so a real tuning change cannot trip it while a
# quantity error still can. The other two are its derived ceilings, each deliberately loose
# but finite: writing them at today's defaults would freeze the current values and turn any
# reasonable retune red.
MAX_MODEL_CONTEXT_LIMIT = 2_000_000
MAX_RECENT_MESSAGE_BUDGET = 1_000_000
MAX_SUMMARY_MAX_TOKENS = 100_000


@dataclass(frozen=True, slots=True)
class Budget:
    """The configured window, and the numbers the spec derives from it.

    The fields are the settings themselves; `input_budget`, `summary_trigger` and
    `chunk_budget` are computed rather than stored, so there is one formula in one place and
    no copy to drift from the fields it claims to come from.
    """

    model_context_limit: int
    reserved_output_tokens: int
    safety_margin: int
    summary_trigger_ratio: float
    recent_message_budget: int
    summary_max_tokens: int

    @property
    def input_budget(self) -> int:
        """What is left for the prompt once the reply and the estimator's error are set aside."""

        return self.model_context_limit - self.reserved_output_tokens - self.safety_margin

    @property
    def summary_trigger(self) -> int:
        """The estimate above which an assembly tries to compress, truncated as the spec says."""

        return int(self.summary_trigger_ratio * self.input_budget)

    @property
    def chunk_budget(self) -> int:
        """The estimate one summary chunk may carry. The retained tail is sized the same way."""

        return self.recent_message_budget

    @classmethod
    def from_settings(cls, settings: Settings) -> Budget:
        """The budget `settings` describe.

        `Settings` is imported for typing only: `app.core.config` imports this module, so
        importing it back at run time would close a cycle.
        """

        return cls(
            model_context_limit=settings.model_context_limit,
            reserved_output_tokens=settings.reserved_output_tokens,
            safety_margin=settings.safety_margin,
            summary_trigger_ratio=settings.summary_trigger_ratio,
            recent_message_budget=settings.recent_message_budget,
            summary_max_tokens=settings.summary_max_tokens,
        )

    def validate(self) -> None:
        """Reject a budget that contradicts itself or exceeds an absolute ceiling.

        The relational checks are about a compression step that cannot achieve what it exists
        for. A retained budget at or above the trigger leaves the view above the trigger after
        every strip, so each decision summarizes again and the version chain grows without the
        view ever shrinking (INV-11). A summary longer than the input budget cannot be sent in
        the prompt it belongs to (INV-9).

        The ceilings below are a different property: the relational constraints keep the
        budget *self-consistent*, the ceilings keep it *bounded*. A budget can satisfy every
        relational constraint and still be large enough to exhaust memory and the provider's
        window -- and only a quantity error can produce one -- so the ceilings are a guardrail
        rather than a freeze of today's values.
        """

        if self.recent_message_budget >= self.summary_trigger:
            raise ValueError(
                f"recent_message_budget ({self.recent_message_budget}) must stay below the "
                f"summary trigger ({self.summary_trigger}): a view stripped to the retained "
                f"budget would still exceed the trigger, so every decision would summarize "
                f"again without ever getting smaller"
            )
        if self.summary_max_tokens >= self.input_budget:
            raise ValueError(
                f"summary_max_tokens ({self.summary_max_tokens}) must stay below the input "
                f"budget ({self.input_budget}): a summary that cannot fit the prompt it "
                f"belongs to can never be sent"
            )
        if self.model_context_limit > MAX_MODEL_CONTEXT_LIMIT:
            raise ValueError(
                f"model_context_limit ({self.model_context_limit}) must not exceed the "
                f"absolute ceiling ({MAX_MODEL_CONTEXT_LIMIT}): beyond the largest real "
                f"window this can only be a configuration error"
            )
        if self.recent_message_budget > MAX_RECENT_MESSAGE_BUDGET:
            raise ValueError(
                f"recent_message_budget ({self.recent_message_budget}) must not exceed the "
                f"absolute ceiling ({MAX_RECENT_MESSAGE_BUDGET})"
            )
        if self.summary_max_tokens > MAX_SUMMARY_MAX_TOKENS:
            raise ValueError(
                f"summary_max_tokens ({self.summary_max_tokens}) must not exceed the "
                f"absolute ceiling ({MAX_SUMMARY_MAX_TOKENS})"
            )


@dataclass(frozen=True, slots=True)
class Round:
    """One turn: what the user said, and the reply that closed it.

    `closed` is False for the round the user is still waiting on. The distinction is
    load-bearing -- a summary may never cover the message being answered (INV-1) -- so
    `leading_strip` never puts an unclosed round in the range it hands to the summarizer.
    """

    messages: tuple[LedgerMessage, ...]
    closed: bool


def iter_rounds(messages: Sequence[LedgerMessage]) -> list[Round]:
    """Cut the ledger into rounds, in order, with no message dropped or duplicated.

    A user message opens a round and an assistant message closes it. A user message with no
    reply yet ends the ledger as an unclosed round. A user message that follows another one
    with no reply between them opens a round too, and the earlier one is then reported as
    unclosed rather than being folded into its neighbour: a round is never a guess, so the
    only rounds that claim to be summarizable are the ones an assistant message closed.
    """

    rounds: list[Round] = []
    current: list[LedgerMessage] = []
    for row in messages:
        if row.role == "user":
            if current:
                rounds.append(Round(messages=tuple(current), closed=False))
            current = [row]
        else:
            current.append(row)
            if row.role == "assistant":
                rounds.append(Round(messages=tuple(current), closed=True))
                current = []
    if current:
        rounds.append(Round(messages=tuple(current), closed=False))
    return rounds


def leading_strip(messages: Sequence[LedgerMessage], budget_target: int) -> list[LedgerMessage]:
    """The fewest whole leading rounds whose removal brings the tail to `budget_target`.

    Returns the messages to strip, and an empty list when nothing has to go. Two constraints
    outrank the target, which is soft (spec §6.1 step 4b):

    - The last round is never stripped. It is the round being answered, and a view without it
      cannot answer the user.
    - No unclosed round is ever stripped. It has no reply to summarize, and a summary may only
      end on an assistant message (spec §5.4, INV-1).

    So a last round that alone exceeds the target is kept anyway: the hard constraint wins.
    """

    rounds = iter_rounds(messages)
    if len(rounds) <= 1:
        return []

    max_k = min(len(rounds) - 1, _leading_closed_rounds(rounds))

    # Walking the retained tail down one round at a time keeps this linear in the ledger,
    # which matters: it runs on every supervisor decision, over the whole conversation.
    round_tokens = [_round_tokens(rnd) for rnd in rounds]
    retained = sum(round_tokens)
    for k in range(max_k + 1):
        if retained <= budget_target:
            return _flatten(rounds[:k])
        retained -= round_tokens[k]
    return _flatten(rounds[:max_k])


def chunk_for_summary(
    messages: Sequence[LedgerMessage], chunk_budget: int
) -> list[list[LedgerMessage]]:
    """Pack `messages` into consecutive chunks of whole rounds, each within `chunk_budget`.

    Greedy and in order: a chunk takes rounds until the next one would push it over. A round
    that is over budget on its own becomes a chunk of its own rather than being split -- a
    split would put `covered_to_sequence` on a user message, which the boundary rule forbids,
    and that costs more than an oversized chunk (spec §5.2).
    """

    chunks: list[list[LedgerMessage]] = []
    current: list[LedgerMessage] = []
    current_tokens = 0
    for rnd in iter_rounds(messages):
        tokens = _round_tokens(rnd)
        if current and current_tokens + tokens > chunk_budget:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.extend(rnd.messages)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def _leading_closed_rounds(rounds: Sequence[Round]) -> int:
    """How many rounds at the head are closed, up to the first unclosed one."""

    count = 0
    for rnd in rounds:
        if not rnd.closed:
            break
        count += 1
    return count


def _round_tokens(rnd: Round) -> int:
    return sum(estimate_message_tokens(row.content) for row in rnd.messages)


def _flatten(rounds: Sequence[Round]) -> list[LedgerMessage]:
    return [row for rnd in rounds for row in rnd.messages]
