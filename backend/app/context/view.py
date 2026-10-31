"""Assembling one model call's view of a conversation, and saying why it looks the way it does.

The Supervisor's prompt is built from three parts: the fixed prompt, the newest usable summary,
and the recent ledger rounds that follow it (spec §3). This module decides the third part and,
when the whole thing is too large, buys a smaller one by summarizing the oldest rounds.

Three properties of spec §6.1 are the reason this is not simply "summarize when it does not fit":

- **The trigger and the limit are different numbers.** A view above `summary_trigger` is worth
  compressing; a view above `input_budget` cannot be sent at all. Landing between the two is a
  success, and the next assembly of the same conversation must not compress again -- which is why
  the strip is the *minimum* that reaches `budget_target` rather than a fixed number of rounds.
- **An empty strip is not a failure.** When nothing older than the last round is left, there is
  nothing to compress, and refusing the request would be wrong.
- **A failed summary degrades, it does not fail.** The old version stays, the view is assembled
  from what is there, and only a view that still does not fit raises.

`reason` is the one word that says which of those happened, and it is the same word that travels
in the `CONTEXT_ASSEMBLED` payload and in `CONTEXT_LIMIT_EXCEEDED.details` -- so two of the
constants below are the error module's, aliased rather than retyped: two copies of one word can
drift apart, and then the event and the error would disagree about the same assembly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from app.agents.prompts import describe_transcript
from app.context.budget import Budget, leading_strip
from app.context.estimator import ESTIMATOR_VERSION, estimate_message_tokens, estimate_tokens
from app.context.ledger import LedgerMessage, read_ledger
from app.context.summary import (
    Summary,
    SummaryScope,
    generate_summaries,
    latest_valid_summary,
)
from app.core.errors import (
    CONTEXT_LIMIT_REASON_OVER_BUDGET,
    CONTEXT_LIMIT_REASON_SUMMARY_FAILED,
    context_limit_error,
)
from app.storage.database import Database
from app.storage.models import ContextSummary

# The closed set of `reason` values (spec §8.2). `summary_failed` and `over_budget` are the error
# module's constants: the same word decides `retryable` on the raised `CONTEXT_LIMIT_EXCEEDED`,
# and a second literal here could disagree with it.
REASON_WITHIN_BUDGET = "within_budget"
REASON_SUMMARIZED = "summarized"
REASON_AGGRESSIVE_CROP = "aggressive_crop"
REASON_NO_VALID_SUMMARY = "no_valid_summary"
REASON_SUMMARY_FAILED = CONTEXT_LIMIT_REASON_SUMMARY_FAILED
REASON_OVER_BUDGET = CONTEXT_LIMIT_REASON_OVER_BUDGET
REASON_NO_CONVERSATION = "no_conversation"


class CompletionModel(Protocol):
    """The slice of a model client this module uses: one synchronous completion."""

    def complete(self, messages: tuple[dict[str, Any], ...]) -> Any: ...


@dataclass(frozen=True, slots=True)
class AssembledView:
    """What one assembly decided, and the text it will be rendered into.

    `estimated_tokens` covers the whole prompt -- the fixed overhead, the summary and the retained
    rows -- so it is directly comparable with `input_budget`. `retained_from_sequence` and
    `retained_to_sequence` bound what the transcript shows, and are `None` only when no ledger
    range was retained at all. `summary` is the version the assembly used, if any, and
    `summary_text` is its text, so a caller can render the prompt without knowing about versions.

    `rows` are those same retained rows as objects, not as text. `transcript` is what the
    Supervisor's prompt renders; a domain that publishes its own memory handoff needs the rows
    themselves, because a role/content pair plus a ledger id is what a reference can point back
    at. Carrying them here rather than re-reading the ledger keeps one assembly per call.
    """

    transcript: str
    summary_text: str | None
    estimated_tokens: int
    summary: Summary | None
    retained_from_sequence: int | None
    retained_to_sequence: int | None
    reason: str
    rows: tuple[LedgerMessage, ...] = ()


class ContextAssembler:
    """Builds the Supervisor's view of one conversation, within one budget.

    Built per turn, because both collaborators belong to the turn that is running: `record`
    appends the run event, and `summary_scope` lends each summary call its audit source. Nothing
    here writes the ledger or the graph state -- the view is derived, and `conversation_message`
    is read-only for this module.

    `user_id` is the caller the assembly is for, and it is part of the constructor rather than
    of `assemble()`: an assembler is built per invocation, so the identity it was built with is
    the only one it can ever read for. The ledger and summary reads verify it themselves
    (`app.context.ownership`), which means a background job that reuses an assembler across
    users fails closed instead of reading the previous caller's history.
    """

    def __init__(
        self,
        database: Database,
        model: CompletionModel,
        budget: Budget,
        *,
        user_id: str,
        record: Callable[[dict], None],
        summary_scope: SummaryScope,
    ) -> None:
        self._database = database
        self._model = model
        self._budget = budget
        self._user_id = user_id
        self._record = record
        self._summary_scope = summary_scope

    def assemble(
        self,
        *,
        conversation_id: str | None,
        current_sequence: int,
        prompt_overhead: str,
        transcript_fallback: str = "",
        aggressive: bool = False,
    ) -> AssembledView:
        """Assemble the view, compressing the oldest rounds when it does not fit.

        `prompt_overhead` is everything in the prompt that is not the transcript or the summary,
        already rendered, so this method can price the whole thing without knowing the template.
        `transcript_fallback` is what the session path shows instead of a ledger.

        The reason rules, in the order they are decided:

        - no conversation at all (the session path) -> `no_conversation`; the caller's transcript
          is returned and no ledger is read (INV-7);
        - a new version was written -> `summarized`, or `aggressive_crop` in the aggressive pass;
        - no new version, but a summary call was attempted and failed -> `summary_failed`;
        - no new version and the chain on disk was judged unusable -> `no_valid_summary`;
        - anything else, including an empty strip that still fits -> `within_budget`;
        - on the error that ends an assembly that cannot fit: `summary_failed` when this pass
          attempted a summary call and got nothing, otherwise `over_budget`. `retryable` on that
          error follows the reason, which is why the two words must stay the error module's.
        """

        if conversation_id is None:
            # No ledger at all: the caller's transcript is the whole view, and its cost is what
            # the prompt will really carry.
            estimated = estimate_tokens(prompt_overhead) + estimate_tokens(transcript_fallback)
            return self._emit(
                AssembledView(
                    transcript=transcript_fallback,
                    summary_text=None,
                    estimated_tokens=estimated,
                    summary=None,
                    retained_from_sequence=None,
                    retained_to_sequence=None,
                    reason=REASON_NO_CONVERSATION,
                ),
                summary_call_made=False,
                aggressive=aggressive,
            )

        summary = latest_valid_summary(
            self._database,
            conversation_id,
            current_sequence=current_sequence,
            user_id=self._user_id,
        )
        # A chain that is on disk but unusable is worth telling apart from no chain at all: the
        # first means the memory is being rebuilt, the second that this is a new conversation.
        chain_unusable = summary is None and self._has_versions(conversation_id)
        rows = self._read(conversation_id, summary)
        estimated = self._estimate(prompt_overhead, summary, rows)

        if estimated <= self._budget.summary_trigger:
            return self._emit(
                self._view(rows, summary, estimated, REASON_WITHIN_BUDGET),
                summary_call_made=False,
                aggressive=aggressive,
            )

        target = self._budget.recent_message_budget
        strip = leading_strip(rows, target // 2 if aggressive else target)
        summary_call_made = bool(strip)
        wrote_version = False
        if strip:
            updated = generate_summaries(
                self._model,
                self._database,
                conversation_id=conversation_id,
                user_id=self._user_id,
                base=summary,
                strip=strip,
                chunk_budget=self._budget.chunk_budget,
                max_tokens=self._budget.summary_max_tokens,
                summary_scope=self._summary_scope,
            )
            # Identity, not equality: the summarizer hands back the base summary itself when it
            # committed nothing, which is how a failure is told apart from a version that happens
            # to read the same (Task 6's contract).
            wrote_version = updated is not summary
            if wrote_version:
                summary = updated
                rows = self._read(conversation_id, summary)
                estimated = self._estimate(prompt_overhead, summary, rows)

        if estimated <= self._budget.input_budget:
            reason = self._fitting_reason(
                wrote_version=wrote_version,
                summary_call_made=summary_call_made,
                chain_unusable=chain_unusable,
                aggressive=aggressive,
            )
            return self._emit(
                self._view(rows, summary, estimated, reason),
                summary_call_made=summary_call_made,
                aggressive=aggressive,
            )

        if not aggressive:
            # One retry with half the retained budget. The aggressive pass reports itself -- one
            # assembly, one event -- so this call neither records nor rewrites its reason.
            return self.assemble(
                conversation_id=conversation_id,
                current_sequence=current_sequence,
                prompt_overhead=prompt_overhead,
                transcript_fallback=transcript_fallback,
                aggressive=True,
            )

        reason = (
            REASON_SUMMARY_FAILED if summary_call_made and not wrote_version else REASON_OVER_BUDGET
        )
        self._emit(
            self._view(rows, summary, estimated, reason),
            summary_call_made=summary_call_made,
            aggressive=aggressive,
        )
        raise context_limit_error(reason)

    def _fitting_reason(
        self,
        *,
        wrote_version: bool,
        summary_call_made: bool,
        chain_unusable: bool,
        aggressive: bool,
    ) -> str:
        """Why a view that fits came out the way it did (spec §6.1, the reason table)."""

        if wrote_version:
            return REASON_AGGRESSIVE_CROP if aggressive else REASON_SUMMARIZED
        if summary_call_made:
            return REASON_SUMMARY_FAILED
        if chain_unusable:
            return REASON_NO_VALID_SUMMARY
        return REASON_WITHIN_BUDGET

    def _read(self, conversation_id: str, summary: Summary | None) -> list[LedgerMessage]:
        """The ledger rows after whatever the summary already covers.

        The stale-answer filter lives inside `read_ledger`, so the transcript and the summary
        input are filtered by the same rule (spec §7).
        """

        after = summary.covered_to_sequence if summary is not None else 0
        return read_ledger(
            self._database, conversation_id, after_sequence=after, user_id=self._user_id
        )

    def _estimate(
        self, prompt_overhead: str, summary: Summary | None, rows: Sequence[LedgerMessage]
    ) -> int:
        """What the whole prompt costs: the fixed part, the summary, and the retained rows."""

        total = estimate_tokens(prompt_overhead)
        if summary is not None:
            total += estimate_tokens(summary.summary_text)
        return total + sum(estimate_message_tokens(row.content) for row in rows)

    def _view(
        self,
        rows: Sequence[LedgerMessage],
        summary: Summary | None,
        estimated: int,
        reason: str,
    ) -> AssembledView:
        """The view the ledger rows and the summary describe."""

        return AssembledView(
            transcript=self._transcript(rows),
            summary_text=summary.summary_text if summary is not None else None,
            estimated_tokens=estimated,
            summary=summary,
            retained_from_sequence=rows[0].sequence if rows else None,
            retained_to_sequence=rows[-1].sequence if rows else None,
            reason=reason,
            rows=tuple(rows),
        )

    @staticmethod
    def _transcript(rows: Sequence[LedgerMessage]) -> str:
        """The retained rows as `role: content` lines, in the format used before this module.

        The ledger row is rendered into the same two-key dict `describe_transcript` has always
        taken, so compression changes which history is shown and never how it reads.
        """

        return describe_transcript([{"role": row.role, "content": row.content} for row in rows])

    def _emit(
        self, view: AssembledView, *, summary_call_made: bool, aggressive: bool
    ) -> AssembledView:
        """Report one assembly and hand the view back.

        Every return path goes through here, which is what makes "one assembly, one event" a
        property of the code rather than a promise: there is no second place that records.
        """

        summary = view.summary
        self._record(
            {
                "estimated_tokens": view.estimated_tokens,
                "input_budget": self._budget.input_budget,
                "summary_trigger": self._budget.summary_trigger,
                "estimator": ESTIMATOR_VERSION,
                "summary_id": summary.summary_id if summary is not None else None,
                "summary_version": summary.version if summary is not None else None,
                "covered_from_sequence": (
                    summary.covered_from_sequence if summary is not None else None
                ),
                "covered_to_sequence": summary.covered_to_sequence if summary is not None else None,
                "retained_from_sequence": view.retained_from_sequence,
                "retained_to_sequence": view.retained_to_sequence,
                "reason": view.reason,
                "summary_call_made": summary_call_made,
                "aggressive": aggressive,
            }
        )
        return view

    def _has_versions(self, conversation_id: str) -> bool:
        """Whether any version exists for this conversation, usable or not.

        `latest_valid_summary` answers `None` both for "never summarized" and for "the chain on
        disk cannot be trusted"; only the second is worth reporting as `no_valid_summary`.

        No ownership check here on purpose: this is private to `assemble()`, and the call before
        it (`latest_valid_summary`) has already refused a conversation that is not the caller's.
        """

        with self._database.sessions() as db:
            return (
                db.scalar(
                    select(ContextSummary.summary_id)
                    .where(ContextSummary.conversation_id == conversation_id)
                    .limit(1)
                )
                is not None
            )


__all__ = [
    "REASON_AGGRESSIVE_CROP",
    "REASON_NO_CONVERSATION",
    "REASON_NO_VALID_SUMMARY",
    "REASON_OVER_BUDGET",
    "REASON_SUMMARIZED",
    "REASON_SUMMARY_FAILED",
    "REASON_WITHIN_BUDGET",
    "AssembledView",
    "ContextAssembler",
]
