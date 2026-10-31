"""Who owns the conversation a read is about, checked where the read happens.

Every entry point in `app/api` verifies ownership before it names a conversation, and until
P3 that was the *only* thing standing between one account's history and another's: the
ledger read, the summary chain and the assembler all took a bare `conversation_id` and
trusted the caller to have checked. That is one forgotten line away from a cross-account
read, and the callers most likely to forget are exactly the ones that are easy to overlook --
a background titling pass, a report mailer, a summary job that reuses the conversation id of
the request that happened to run before it (design §7.1, §7.3).

So the check moved down: the readers of history now take the user the read is for and verify
it themselves, against the conversation row, in the same session as the read. A miss raises
the same `NOT_FOUND` the service raises, which is deliberate -- "not yours" and "not there"
must be indistinguishable, or the 404 becomes an oracle for which ids exist.

This is a second line, not a replacement. The API still checks ownership first, so an
ordinary request pays one extra primary-key lookup and nothing else; what changes is that a
future caller which forgets can now fail closed instead of reading someone else's history.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.errors import conversation_not_found
from app.storage.models import Conversation


def require_owner(db, conversation_id: str, user_id: str) -> None:
    """Raise `NOT_FOUND` unless `conversation_id` belongs to `user_id`.

    Runs on the caller's session: the ownership check and the read it guards must see the
    same snapshot, and a second checkout would only add a round trip. `db` is deliberately
    untyped here -- `app/storage/database` owns that type and importing it from the context
    package would drag the storage layer into every module that reads history.
    """

    owner = db.scalar(
        select(Conversation.user_id).where(Conversation.conversation_id == conversation_id)
    )
    if owner is None or owner != user_id:
        raise conversation_not_found()


__all__ = ["require_owner"]
