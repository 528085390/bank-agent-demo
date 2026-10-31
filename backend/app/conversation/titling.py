"""A conversation's name is the user's own first words, whole and local.

It used to be a model call: one extra round trip that asked a provider to summarise the first
message in under twenty characters. That bought a shorter label and cost three things -- it only
worked while a model was configured, it could time out, and it had to be exempted from the audit
rule so the label would not show up as work done for the user. A label is not worth any of that.

What replaced it is not a summary either: the name is the user's words themselves, so a message
is never reworded or cleverly shortened to fit the sidebar -- only made one line, and only cut
when it would not fit in the row at all. The text is what the user will recognise the
conversation by, and a paraphrase loses that.

So the name is derived here, from the text itself, with no network and no dependency on the
model. It is deterministic: the same first message always names the same conversation, which is
also what makes it testable.

The "is it still the first turn, is the title still the default" guard does not live here -- it
lives in `ConversationService.auto_title_candidate` / `apply_auto_title`, because it is about the
row, not the text. This module only answers "what would this text be called".
"""

from __future__ import annotations

from app.conversation.service import DEFAULT_TITLE, MAX_TITLE_LENGTH


def derive_title(user_text: str | None) -> str | None:
    """The conversation's name, or `None` when the text cannot name anything.

    `None` rather than `DEFAULT_TITLE`: the caller treats "no name" as "leave the conversation
    alone", and a title that equals the default is indistinguishable from one never written.
    """

    if not user_text:
        return None
    # One line, always: a pasted multi-line prompt must not turn into a multi-line sidebar row.
    # Wrapping is the sidebar's job, so the name itself keeps every word the user typed.
    collapsed = " ".join(user_text.split())
    if not collapsed:
        return None
    if len(collapsed) <= MAX_TITLE_LENGTH:
        return collapsed
    # Only a message too long for the row is cut, and the ellipsis says so. Cutting here rather
    # than letting `clean_title` chop it means the last character the user sees is a signal, not
    # a word that happens to end mid-sentence.
    return f"{collapsed[:MAX_TITLE_LENGTH - 1]}…"


def is_usable_title(title: str | None) -> bool:
    """Whether a derived title is worth writing. The default means "nothing was named"."""

    return bool(title) and title != DEFAULT_TITLE
