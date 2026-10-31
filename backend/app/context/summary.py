"""Turning stripped history into a cumulative summary, in bounded chunks, as a version chain.

The context manager's whole memory of a long conversation is one text field. Everything here
serves two promises about it:

**The input of every summary call is bounded.** A strip is cut by `chunk_for_summary` into
whole-round chunks no larger than `chunk_budget`, and each chunk becomes one version. The
model is asked to *merge* the chunk into the previous summary rather than to rewrite history,
so `summary_text` is cumulative while `covered_from_sequence`/`covered_to_sequence` describe
only what that version newly read (spec §5.1, §5.2). Those two numbers are taken from the
messages actually sent, never from the base summary plus one: a row is a claim about which
ledger messages the text covers, and a claim the code did not verify is a claim that can be
wrong.

**A failure costs at most the chunk it happened in.** Each version is committed in its own
transaction, and any prefix of the chain is still contiguous -- so a failure returns the
newest committed version and leaves the earlier ones in place instead of redoing the whole
history on the next request (spec §5.1). The one failure that is *not* tolerated is a
`(conversation_id, version)` collision: that means the version counter this module reads is
not the one it writes against, so it is a programming error and the caller's assembly must
fail loudly rather than be told "no summary this time".

`latest_valid_summary` is the reading half. It walks the base chain back from the highest
version and checks the three conditions of spec §5.3; a chain that fails any of them is
treated as absent, because a summary that remembers a history that never happened is worse
than no summary at all. `source_hash` is computed once, when a version is written, and never
recomputed here: the ledger is append-only, so a stored hash can only disagree with a recomputed
one if the ledger was changed behind the runtime's back, and verifying it on the hot path would
read the entire history on every model call (spec §5.3).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from app.agents.decisions import DecisionError, extract_json_object
from app.agents.prompts import SUMMARY_PROMPT, render
from app.context.budget import chunk_for_summary
from app.context.estimator import estimate_tokens
from app.context.ledger import LedgerMessage
from app.context.ownership import require_owner
from app.core.errors import AppError
from app.core.types import fingerprint, new_id, utcnow
from app.integration.model_client import ModelClientError
from app.storage.database import Database
from app.storage.models import ContextSummary

# The versions one conversation keeps. A chain legitimately grows by one per summarized
# assembly, and the read only needs the tail of it; the cap is here so that a runaway writer
# cannot turn the read into an unbounded scan. The fetch truncates, so a chain longer than this
# count loses its root and is reported as unusable like any other break.
_MAX_VERSIONS_READ = 4096

# The user turn of the summary call. `{previous}` and `{conversation}` are both derived text --
# a summary the runtime wrote and ledger messages -- so the prompt cannot carry caller identity
# (INV-13); `LedgerMessage` has no field to carry it in.
SUMMARY_USER_TEMPLATE = """已有摘要（可能为空，代表更早的历史）：
{previous}

本次需要并入的对话：
{conversation}
"""

# What the model sees when there is no earlier history. A named constant rather than `""` so
# the prompt reads as "there is nothing older than this chunk" instead of an accidental blank.
NO_PREVIOUS_SUMMARY = "（无，这是最早的历史）"


@dataclass(frozen=True, slots=True)
class Summary:
    """One committed version of a conversation's memory.

    `summary_text` covers sequences 1..`covered_to_sequence`; the two sequence fields delimit
    what this version itself read. `model` is informational -- it names what produced the text
    for a later audit -- and is `None` when the client cannot say.
    """

    summary_id: str
    version: int
    covered_from_sequence: int
    covered_to_sequence: int
    summary_text: str
    base_summary_id: str | None
    model: str | None


class SummaryScope(Protocol):
    """How the caller lends a summarization call its audit context.

    Called with no arguments for each chunk, returning the context manager to run the model
    call inside.
    """

    def __call__(self) -> AbstractContextManager[Any]: ...


class _SummaryModel(Protocol):
    """The slice of a model client this module uses: one synchronous completion."""

    def complete(self, messages: tuple[dict[str, Any], ...]) -> Any: ...


class _ChainBroken(Exception):
    """A read-only signal that the version chain failed spec §5.3. Never escapes this module."""


def source_hash(messages: Sequence[LedgerMessage]) -> str:
    """The canonical fingerprint of the messages a version read.

    Exactly the four fields spec §5.3 names, in ledger order. It is stored with the version so
    an offline check can later ask whether the ledger still says what the summary claims it
    said; it is deliberately not recomputed on the read path.
    """

    return fingerprint(
        [(row.sequence, row.role, row.message_kind, row.content) for row in messages]
    )


def latest_valid_summary(
    database: Database, conversation_id: str, *, current_sequence: int, user_id: str
) -> Summary | None:
    """The newest version whose chain checks out, or `None` when the chain is not usable.

    Walking from the highest version backwards is what makes this O(versions) rather than
    O(history): every check below is answered by the summary rows alone. A chain that starts
    anywhere but sequence 1, that has a gap or an overlap, that skips a link, or whose newest
    version reaches the current user message is reported as no summary at all -- the caller
    then rebuilds from scratch, which is the only safe reading of a broken memory (INV-1).

    `user_id` is verified before a single row is read: a summary is a condensed copy of
    someone's conversation, so reading one by id alone is the same leak as reading the ledger
    by id alone (`app.context.ownership`).
    """

    with database.sessions() as db:
        require_owner(db, conversation_id, user_id)
        stored = list(
            db.scalars(
                select(ContextSummary)
                .where(ContextSummary.conversation_id == conversation_id)
                .order_by(ContextSummary.version.desc())
                .limit(_MAX_VERSIONS_READ)
            )
        )

    if not stored:
        return None

    try:
        newest = _walk_chain(stored)
    except _ChainBroken:
        return None
    if newest.covered_to_sequence >= current_sequence:
        return None
    return newest


def generate_summaries(
    model: _SummaryModel,
    database: Database,
    *,
    conversation_id: str,
    user_id: str,
    base: Summary | None,
    strip: Sequence[LedgerMessage],
    chunk_budget: int,
    max_tokens: int,
    summary_scope: SummaryScope,
) -> Summary | None:
    """Summarize `strip` into the chain, one committed version per bounded chunk.

    Returns the newest committed version: `base` itself when nothing new was committed
    (identity matters -- the caller branches on whether a new version exists), or `None` when
    the chain is still empty. A chunk that the model failed, refused, or answered unusably
    stops the loop and leaves every earlier version -- including `base` -- exactly as it was;
    a version collision is not a failure this function absorbs.

    `user_id` reaches the write through `_commit`, which verifies it inside the transaction
    that allocates the version: a summary is written for a conversation, and the writer is the
    one place that can prove the conversation is the caller's before the version number is
    read (`app.context.ownership`).
    """

    current = base
    for chunk in chunk_for_summary(strip, chunk_budget):
        try:
            text = _summarize_chunk(model, current, chunk, max_tokens, summary_scope)
        except (ModelClientError, DecisionError, AppError):
            break
        current = _commit(
            database, conversation_id, chunk, text, current, _model_name(model), user_id=user_id
        )
    return current


def _walk_chain(stored: list[ContextSummary]) -> Summary:
    """Check spec §5.3 over the chain that ends at `stored[0]` and return that version.

    The walk starts at the newest row and follows `base_summary_id` through the rows, which have
    to be indexed for it: what `stored` holds is every version this conversation keeps, not one
    chain. A chain is contiguous when each link names its predecessor, and adjacent in time when
    each link's coverage continues exactly where the one below it stopped. The version descent
    check is not a third condition but the reason the walk terminates -- every step moves to a
    strictly smaller version, so no cycle of links can be followed forever. Condition 3 is
    checked by the caller instead (the newest version's coverage must stay strictly below the
    message the user just sent).

    Rows that no longer belong to this chain are ignored rather than repaired or deleted: a
    rebuild after a rejected chain is committed as a second root, and rejecting the newest
    version for the older rows' sake would leave the conversation unable to summarize again.
    The chain is what is read, so it is what is checked; anything off it is not evidence either
    way (spec §5.3, §5.4).
    """

    by_id = {row.summary_id: row for row in stored}
    chain_head = stored[0]
    current = chain_head
    while current.base_summary_id is not None:
        parent = by_id.get(current.base_summary_id)
        if parent is None:
            raise _ChainBroken  # a link to a version this read cannot see proves nothing
        if parent.covered_to_sequence + 1 != current.covered_from_sequence:
            raise _ChainBroken
        if current.version != parent.version + 1:
            raise _ChainBroken
        current = parent
    if current.covered_from_sequence != 1:
        raise _ChainBroken  # a chain that starts anywhere but the first message is a hole
    return _from_row(chain_head)


def _from_row(row: ContextSummary) -> Summary:
    return Summary(
        summary_id=row.summary_id,
        version=row.version,
        covered_from_sequence=row.covered_from_sequence,
        covered_to_sequence=row.covered_to_sequence,
        summary_text=row.summary_text,
        base_summary_id=row.base_summary_id,
        model=row.model,
    )


def _summarize_chunk(
    model: _SummaryModel,
    base: Summary | None,
    chunk: Sequence[LedgerMessage],
    max_tokens: int,
    summary_scope: SummaryScope,
) -> str:
    """Ask the model to merge one chunk into the base summary, or raise for the caller to stop.

    Every failure mode of the call is a raise rather than a return value: a provider error, a
    reply that is not a JSON object, a missing or blank `summary` field, and a text that does
    not fit the budget the summary itself must respect (INV-9). The check is deliberately
    against the estimator, the same arithmetic that will later decide the prompt fits, and an
    over-long answer is a failure rather than a truncation -- a truncated memory is a silently
    wrong one.
    """

    previous = base.summary_text if base is not None else NO_PREVIOUS_SUMMARY
    user = render(SUMMARY_USER_TEMPLATE, previous=previous, conversation=_describe_ledger(chunk))
    messages = ({"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": user})
    with summary_scope():
        reply = model.complete(messages)
    decision = extract_json_object(getattr(reply, "text", None))
    text = decision.get("summary")
    if not isinstance(text, str) or not text.strip():
        raise DecisionError("摘要没有返回 summary 字段")
    if estimate_tokens(text) > max_tokens:
        raise DecisionError("摘要超过 summary_max_tokens")
    return text


def _commit(
    database: Database,
    conversation_id: str,
    chunk: Sequence[LedgerMessage],
    text: str,
    base: Summary | None,
    model_name: str | None,
    *,
    user_id: str,
) -> Summary:
    """Write one version in its own transaction and return it.

    The next version number is read first, in its own session, and the insert that uses it opens
    its own transaction afterwards; the `(conversation_id, version)` unique constraint -- not an
    application-level check -- is what makes two writers agree on the number. One process already
    holds the conversation lock for a whole turn (`app/api/routes.py:328-336`), so a collision
    means the counter read here is not the one this insert was written against: a programming
    error, left to fail loudly rather than be absorbed.

    The covered range comes from `chunk` -- the messages that were actually sent -- and not
    from `base.covered_to_sequence + 1`. In production the two agree, because the caller hands
    over a strip that continues directly from the base; taking it from the data is what keeps
    the written claim true even when they do not.
    """

    version = _next_version(database, conversation_id, user_id=user_id)
    stored = ContextSummary(
        summary_id=new_id("sum"),
        conversation_id=conversation_id,
        version=version,
        base_summary_id=base.summary_id if base is not None else None,
        covered_from_sequence=min(entry.sequence for entry in chunk),
        covered_to_sequence=max(entry.sequence for entry in chunk),
        summary_text=text,
        source_hash=source_hash(chunk),
        model=model_name,
        created_at=utcnow(),
    )
    with database.sessions.begin() as db:
        db.add(stored)
    return _from_row(stored)


def _next_version(database: Database, conversation_id: str, *, user_id: str) -> int:
    """One past the highest version this conversation has, read in its own transaction.

    The ownership check rides this read rather than getting a session of its own: the version
    counter *is* a read of that conversation's rows, so verifying it here means the first thing
    a write path touches is already refused when the conversation is not the caller's.
    """

    with database.sessions() as db:
        require_owner(db, conversation_id, user_id)
        highest = db.scalar(
            select(ContextSummary.version)
            .where(ContextSummary.conversation_id == conversation_id)
            .order_by(ContextSummary.version.desc())
            .limit(1)
        )
    return (highest or 0) + 1


def _model_name(model: _SummaryModel) -> str | None:
    """What the client calls the model it is talking to, when it can say.

    Read from the same place the audit decorator reads it -- the adapter's own
    `request_parameters()` -- rather than guessed from the settings, and `None` when the client
    offers nothing, because the column is informational and an invented name is worse than an
    absent one.
    """

    described: Callable[[], Any] | None = getattr(model, "request_parameters", None)
    if not callable(described):
        return None
    name = described().get("model")
    return name if isinstance(name, str) else None


def _describe_ledger(messages: Sequence[LedgerMessage]) -> str:
    """The chunk as `role: content` lines, in ledger order.

    Rendered here rather than by `describe_transcript` because that helper takes message dicts
    and lives in the prompt layer built for the agent graph; a summary is assembled from the
    context manager's own ledger rows. Nothing can leak identity either way -- a
    `LedgerMessage` carries a sequence, a role, a kind, a content and an optional question id.
    """

    return "\n".join(f"{row.role}: {row.content}" for row in messages)


__all__ = [
    "Summary",
    "generate_summaries",
    "latest_valid_summary",
    "source_hash",
]
