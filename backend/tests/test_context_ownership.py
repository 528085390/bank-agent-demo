"""History reads are checked against the owner, not merely against a conversation id.

Design §7.1 makes the API edge the place where a caller's identity becomes an owned
conversation, and until P3 that edge was the *only* thing keeping one account's history away
from another's: `read_ledger`, `latest_valid_summary` and `ContextAssembler.assemble` all took
a bare conversation id and trusted whoever called them. The failure this file pins is the one
that never shows up in a request-driven test -- a background job (titling, a report mailer, a
summary pass) that keeps a conversation id and loses the check in between.

Every test here is therefore about the same promise, from four sides: a reader that is handed
someone else's conversation fails closed with the same 404 the service raises, and it fails
closed *before* it returns rows, a summary, or a written version. The 404 is also the point --
"not yours" and "not there" must be indistinguishable, or the status code becomes an oracle
for which ids exist.
"""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from sqlalchemy import select

from app.context.budget import Budget
from app.context.ledger import read_ledger
from app.context.summary import generate_summaries, latest_valid_summary
from app.context.view import ContextAssembler
from app.core.errors import AppError
from app.storage.models import AppUser, ContextSummary

# The demo archive the `session` fixture speaks for.
OWNER = "user_0001"
STRANGER = "user_0002"

TIGHT = Budget(
    model_context_limit=4000,
    reserved_output_tokens=0,
    safety_margin=0,
    summary_trigger_ratio=0.5,
    recent_message_budget=500,
    summary_max_tokens=100,
)


class SummaryModel:
    """Answers every summary call with the same text; the text is not what these tests read."""

    def complete(self, messages):
        import json

        from app.integration.model_client import ModelReply

        return ModelReply(text=json.dumps({"summary": "摘要"}, ensure_ascii=False))

    def close(self):
        return None


@pytest.fixture
def stranger(rt):
    """A real second account. The FK on `conversation.user_id` needs the row to exist."""

    with rt.database.sessions.begin() as db:
        if not db.scalar(select(AppUser).where(AppUser.user_id == STRANGER)):
            db.add(AppUser(user_id=STRANGER))
    return STRANGER


def seeded(rt, session, *, turns=1):
    """One conversation owned by `OWNER`, with something in the ledger to leak."""

    created = rt.conversations.create(session["user_id"], "甲的会话")
    for index in range(turns):
        rt.conversations.append_user_message(
            session["user_id"],
            created.conversation_id,
            idempotency_key=f"k{index}",
            expected_hash=f"h{index}",
            content="甲的流水",
        )
        rt.conversations.append_assistant_message(
            session["user_id"], created.conversation_id, run_id=None, content="甲的分析"
        )
    return created


def assembler(rt, user_id, *, model=None):
    return ContextAssembler(
        rt.database,
        model or SummaryModel(),
        TIGHT,
        user_id=user_id,
        record=lambda payload: None,
        summary_scope=nullcontext,
    )


def assert_not_found(call):
    """The 404 the service raises, and no other failure: wording and status both matter."""

    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == "NOT_FOUND"
    assert caught.value.status == 404
    assert caught.value.message == "会话不存在"


def test_the_ledger_refuses_a_stranger(rt, session, stranger):
    created = seeded(rt, session)

    assert_not_found(
        lambda: read_ledger(
            rt.database, created.conversation_id, after_sequence=0, user_id=stranger
        )
    )


def test_the_ledger_refuses_an_id_that_does_not_exist(rt, session, stranger):
    """Same failure as "not yours": a different one would tell the caller the id is real."""

    assert_not_found(
        lambda: read_ledger(rt.database, "conv_missing", after_sequence=0, user_id=stranger)
    )


def test_the_ledger_still_reads_for_the_owner(rt, session):
    """The guard is not a blanket refusal -- the positive path is the reason it can exist."""

    created = seeded(rt, session)

    rows = read_ledger(
        rt.database, created.conversation_id, after_sequence=0, user_id=session["user_id"]
    )

    assert [row.content for row in rows] == ["甲的流水", "甲的分析"]


def test_the_summary_chain_refuses_a_stranger(rt, session, stranger):
    """A summary is a condensed copy of the conversation, so it is the same leak."""

    created = seeded(rt, session)
    strip = read_ledger(
        rt.database, created.conversation_id, after_sequence=0, user_id=session["user_id"]
    )
    generate_summaries(
        SummaryModel(),
        rt.database,
        conversation_id=created.conversation_id,
        user_id=session["user_id"],
        base=None,
        strip=strip,
        chunk_budget=32768,
        max_tokens=100,
        summary_scope=nullcontext,
    )

    assert_not_found(
        lambda: latest_valid_summary(
            rt.database,
            created.conversation_id,
            current_sequence=9,
            user_id=stranger,
        )
    )


def test_the_summary_writer_refuses_and_writes_nothing(rt, session, stranger):
    """The refusal has to land before the version counter is read, or a row exists already."""

    created = seeded(rt, session)
    strip = read_ledger(
        rt.database, created.conversation_id, after_sequence=0, user_id=session["user_id"]
    )

    assert_not_found(
        lambda: generate_summaries(
            SummaryModel(),
            rt.database,
            conversation_id=created.conversation_id,
            user_id=stranger,
            base=None,
            strip=strip,
            chunk_budget=32768,
            max_tokens=100,
            summary_scope=nullcontext,
        )
    )
    with rt.database.sessions() as db:
        written = db.scalars(
            select(ContextSummary.summary_id).where(
                ContextSummary.conversation_id == created.conversation_id
            )
        ).all()
    assert written == []


def test_the_assembler_is_bound_to_the_user_it_was_built_for(rt, session, stranger):
    """`assemble()` takes a conversation id, so the identity has to come from construction."""

    created = seeded(rt, session)

    assert_not_found(
        lambda: assembler(rt, stranger).assemble(
            conversation_id=created.conversation_id,
            current_sequence=9,
            prompt_overhead="",
        )
    )


def test_the_assembler_reports_the_degraded_session_path_without_reading(rt, session, stranger):
    """No conversation id, no ledger read, no ownership question -- and no 404 either.

    The session path is the one assembly that legitimately has no conversation; refusing it
    would break every request that has not been attached to a ledger yet.
    """

    view = assembler(rt, stranger).assemble(
        conversation_id=None,
        current_sequence=0,
        prompt_overhead="",
        transcript_fallback="用户: 你好",
    )

    assert view.reason == "no_conversation"
    assert view.transcript == "用户: 你好"
