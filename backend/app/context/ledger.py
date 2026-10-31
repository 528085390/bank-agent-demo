"""Reading the message ledger, with the rules every reader of history shares.

The ledger itself is append-only (`app.conversation.service`): a correction is a new row and
the old one stays where it is. What the model may see is a different question, and this module
answers it once, for every reader:

- **Only the part after a sequence.** `read_ledger` starts at `after_sequence`, which is how
  the summarized prefix is left out of an assembly.
- **No superseded interrupt answers.** One `question_id` can carry several `interrupt_answer`
  rows, because the user answered again after an interrupt; only the last one says what the
  user settled on. `drop_superseded_answers` drops the rest.

The two rules are one operation, not a step a caller may forget, so `read_ledger` applies the
second before it returns. Every reader of history must go through it -- **including the
summary input** (spec §7). A summary built from unfiltered rows writes a stale answer into
`summary_text`, and from then on that answer returns to the model on every assembly, which
undoes the filter for the rest of the conversation's life.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app.context.ownership import require_owner
from app.storage.models import ConversationMessage

# The `message_kind` an answer to an interrupt is written with.
_INTERRUPT_ANSWER_KIND = "interrupt_answer"


@dataclass(frozen=True, slots=True)
class LedgerMessage:
    """One ledger row, carrying as much of it as the prompt layer needs."""

    sequence: int
    role: str
    content: str
    message_kind: str
    question_id: str | None
    # The ledger's own id for the row. It defaults so a caller that only needs the prompt text
    # (the budget tests) can keep building rows positionally; a domain view that carries memory
    # back to the checkpoint wants it, because a reference without a source cannot be audited.
    message_id: str | None = None


def drop_superseded_answers(messages: Sequence[LedgerMessage]) -> list[LedgerMessage]:
    """Drop every `interrupt_answer` that a later answer to the same question replaced.

    Only a row that names its question can be proven superseded, so an answer with no
    `question_id` is kept. Rows are removed, never reordered.
    """

    last_answer: dict[str, int] = {}
    for row in messages:
        if row.message_kind != _INTERRUPT_ANSWER_KIND or row.question_id is None:
            continue
        previous = last_answer.get(row.question_id)
        if previous is None or row.sequence > previous:
            last_answer[row.question_id] = row.sequence

    def superseded(row: LedgerMessage) -> bool:
        return (
            row.message_kind == _INTERRUPT_ANSWER_KIND
            and row.question_id is not None
            and last_answer[row.question_id] != row.sequence
        )

    return [row for row in messages if not superseded(row)]


def read_ledger(
    database, conversation_id: str, *, after_sequence: int, user_id: str
) -> list[LedgerMessage]:
    """One conversation's messages after `after_sequence`, stale answers already dropped.

    `user_id` is not a filter the caller may skip: the conversation is checked to belong to
    that user in the same session as the read, and a mismatch raises `NOT_FOUND` rather than
    returning an empty page. Returning nothing would let an assembly answer without the
    history it was supposed to read, which is the silent version of the same leak
    (`app.context.ownership` says why the check lives here and not only at the API edge).
    """

    query = (
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.sequence > after_sequence,
        )
        .order_by(ConversationMessage.sequence.asc())
    )
    with database.sessions() as db:
        require_owner(db, conversation_id, user_id)
        rows = db.scalars(query).all()
    return drop_superseded_answers(
        [
            LedgerMessage(
                sequence=row.sequence,
                role=row.role,
                content=row.content,
                message_kind=row.message_kind,
                question_id=row.question_id,
                message_id=row.message_id,
            )
            for row in rows
        ]
    )
