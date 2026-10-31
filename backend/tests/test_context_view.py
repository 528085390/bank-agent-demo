"""Assembling the view one model call sees: what is shown, what is compressed, what is reported.

The assembler is the only place that decides how much history reaches the Supervisor, so the
properties pinned here are the ones a long conversation depends on: a short view is sent
untouched, a long one is compressed and keeps its recent rounds verbatim, a view that has already
been compressed is not compressed again, an empty strip is not a failure, and a summary that
fails degrades instead of failing the turn.

Every fixture is hand-computed against `TIGHT` (input_budget 4000, summary_trigger 2000,
chunk_budget 500), because the estimator is exact for the character counts used here and a
fixture that never reaches the budget it claims to test would pass for the wrong reason. One
round of 300 CJK characters costs `2 * (300 + 4) = 608` tokens, which is over `chunk_budget`, so
spec §5.2's oversized-round exception makes every such round a chunk of its own -- that is why a
single assembly can write several versions, and why the stubs below are scripted with one text
per chunk rather than one per assembly.
"""

import json
from contextlib import nullcontext

import pytest
from sqlalchemy import select

from app.context.budget import Budget
from app.context.view import (
    REASON_AGGRESSIVE_CROP,
    REASON_NO_CONVERSATION,
    REASON_NO_VALID_SUMMARY,
    REASON_OVER_BUDGET,
    REASON_SUMMARIZED,
    REASON_SUMMARY_FAILED,
    REASON_WITHIN_BUDGET,
    ContextAssembler,
)
from app.core.errors import AppError
from app.core.types import utcnow
from app.integration.model_client import ModelClientError, ModelReply
from app.storage.models import ContextSummary

# A legal configuration that triggers a summary within a handful of turns. The default budget
# needs ~85k tokens to trigger, which no test can afford.
TIGHT = Budget(
    model_context_limit=4000,
    reserved_output_tokens=0,
    safety_margin=0,
    summary_trigger_ratio=0.5,
    recent_message_budget=500,
    summary_max_tokens=100,
)


class SummaryModel:
    def __init__(self, texts=("摘要",)):
        self._texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, messages):
        # The user turn is the one that carries `{previous}` and `{conversation}`; the system
        # message is the fixed `SUMMARY_PROMPT`, the same text on every call, so recording it
        # would say nothing about which chunk a call was for.
        self.prompts.append(messages[1]["content"])
        if not self._texts:
            raise AssertionError("the script ran out of summaries")
        return ModelReply(text=json.dumps({"summary": self._texts.pop(0)}, ensure_ascii=False))

    def close(self):
        return None


class ExplodingModel:
    def complete(self, messages):
        raise ModelClientError("LLM_TIMEOUT", retryable=True, detail="timeout")

    def close(self):
        return None


def seed_turns(rt, user_id, conversation_id, count, *, size):
    for index in range(count):
        rt.conversations.append_user_message(
            user_id,
            conversation_id,
            idempotency_key=f"k{index}",
            expected_hash=f"h{index}",
            content="问" * size,
        )
        rt.conversations.append_assistant_message(
            user_id,
            conversation_id,
            run_id=None,
            content="答" * size,
        )


# The demo archive every conversation in this file belongs to (`tests/conftest.py:session`).
OWNER = "user_0001"


def assembler_for(rt, model, *, record=None, budget=TIGHT, user_id=OWNER):
    """One assembler, bound to one owner -- the identity is part of the constructor since P3,
    because every ledger and summary read it makes verifies that owner itself."""

    return ContextAssembler(
        rt.database,
        model,
        budget,
        user_id=user_id,
        record=record if record is not None else (lambda payload: None),
        summary_scope=nullcontext,
    )


def versions(rt, conversation_id):
    with rt.database.sessions() as db:
        return db.scalars(
            select(ContextSummary.version)
            .where(ContextSummary.conversation_id == conversation_id)
            .order_by(ContextSummary.version.asc())
        ).all()


def test_a_short_conversation_is_assembled_without_summarizing(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 1, size=20)
    model = SummaryModel()

    view = assembler_for(rt, model).assemble(
        conversation_id=created.conversation_id,
        current_sequence=2,
        prompt_overhead="系统",
    )

    assert view.reason == REASON_WITHIN_BUDGET
    assert view.summary is None
    assert model.prompts == [], "no summary call when the view already fits"
    assert view.estimated_tokens <= TIGHT.input_budget


def test_the_transcript_renders_role_and_content_exactly_as_before(rt, session):
    """The only thing compression changes is which history is shown, never how it reads."""

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 1, size=1)

    view = assembler_for(rt, SummaryModel()).assemble(
        conversation_id=created.conversation_id,
        current_sequence=2,
        prompt_overhead="系统",
    )

    assert view.transcript == "user: 问\nassistant: 答"


def test_a_long_conversation_triggers_a_summary_and_keeps_the_recent_rounds(rt, session):
    """6 rounds of 300 CJK chars: E = 2 + 12 * 304 = 3650 > 2000, so the oldest rounds go.

    The strip is every round but the last: the target is `recent_message_budget` = 500 and one
    round alone costs 608, so no smaller strip can reach it and `leading_strip` returns its
    maximum -- all but the last round, which is never stripped. Each of those 5 rounds is over
    `chunk_budget` on its own, so each becomes its own chunk: 5 chunks, 5 calls, 5 versions, and
    the newest version's text is what the view shows. The view keeps sequences 11-12 and costs
    E = 2 + 4 + 608 = 614.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 6, size=300)
    model = SummaryModel(["历史摘要"] * 5)

    view = assembler_for(rt, model).assemble(
        conversation_id=created.conversation_id,
        current_sequence=12,
        prompt_overhead="系统",
    )

    assert view.reason == REASON_SUMMARIZED
    assert view.summary_text == "历史摘要"
    assert view.retained_from_sequence == 11
    assert view.retained_to_sequence == 12, "the current message stays in the transcript"
    assert view.estimated_tokens <= TIGHT.input_budget


def test_compressed_to_the_limit_but_still_above_the_trigger_does_not_compress_again(rt, session):
    """`summary_trigger < E <= input_budget` is a legal resting place, not a reason to compress again.

    The overhead of 2500 tokens makes the first assembly compress everything it can: E = 2500 +
    12 * 304 = 6148, the same 5-round strip as above (5 calls, 5 versions), and it lands at
    E = 2500 + 4 + 608 = 3112 -- above the trigger, inside the budget. The second assembly reads
    one closed round, so the strip is empty and nothing is called or written again.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 6, size=300)
    model = SummaryModel(["历史摘要"] * 5)
    assembler = assembler_for(rt, model)

    first = assembler.assemble(
        conversation_id=created.conversation_id,
        current_sequence=12,
        prompt_overhead="系" * 2500,
    )
    calls_after_first = len(model.prompts)
    versions_after_first = versions(rt, created.conversation_id)
    second = assembler.assemble(
        conversation_id=created.conversation_id,
        current_sequence=12,
        prompt_overhead="系" * 2500,
    )

    assert first.estimated_tokens > TIGHT.summary_trigger
    assert second.estimated_tokens > TIGHT.summary_trigger, "still above the trigger"
    assert second.estimated_tokens <= TIGHT.input_budget
    assert len(model.prompts) == calls_after_first, "the second assembly must not summarize again"
    assert versions(rt, created.conversation_id) == versions_after_first


def test_an_empty_strip_is_not_a_failure(rt, session):
    """Nothing earlier to summarize means 'do not compress now', not 'refuse the request'."""

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 1, size=300)
    model = SummaryModel()

    view = assembler_for(rt, model).assemble(
        conversation_id=created.conversation_id,
        current_sequence=2,
        prompt_overhead="系" * 2500,
    )

    assert view.reason == REASON_WITHIN_BUDGET
    assert view.transcript == "user: " + "问" * 300 + "\nassistant: " + "答" * 300
    assert model.prompts == []


def test_a_view_that_cannot_fit_fails_with_over_budget(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 1, size=300)

    with pytest.raises(AppError) as raised:
        assembler_for(rt, SummaryModel()).assemble(
            conversation_id=created.conversation_id,
            current_sequence=2,
            prompt_overhead="系" * 5000,
        )

    assert raised.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert raised.value.details["reason"] == REASON_OVER_BUDGET
    assert raised.value.retryable is False


def test_a_failed_summary_falls_back_to_the_old_version_and_reports_it(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 6, size=300)
    record = []

    view = assembler_for(rt, ExplodingModel(), record=record.append).assemble(
        conversation_id=created.conversation_id,
        current_sequence=12,
        prompt_overhead="系统",
    )

    assert view.reason == REASON_SUMMARY_FAILED
    assert view.summary is None
    assert record[-1]["summary_call_made"] is True
    assert versions(rt, created.conversation_id) == []


def test_an_aggressive_pass_keeps_fewer_rounds_than_a_normal_one(rt, session):
    """The scenario has to leave the normal pass just above the trigger, or there is nothing to crop.

    11 rounds of 100 CJK chars cost 208 tokens each and 2288 in total; with a 1600-token overhead
    E = 3888 > 2000, and the fewest whole rounds that leave the tail under 500 is 9 (the tail is
    then 416) -- those 9 rounds pack into 5 chunks (2+2+2+2+1), so 5 versions covering sequences
    1-18. The view keeps 19-22 and costs E = 1600 + 4 + 416 = 2020: above the trigger, so the
    aggressive pass runs, and with its halved target (250) it strips one more round, keeps 21-22
    and costs E = 1600 + 4 + 208 = 1812.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 11, size=100)
    assembler = assembler_for(rt, SummaryModel(["历史摘要"] * 6))

    normal = assembler.assemble(
        conversation_id=created.conversation_id,
        current_sequence=22,
        prompt_overhead="系" * 1600,
    )
    aggressive = assembler.assemble(
        conversation_id=created.conversation_id,
        current_sequence=22,
        prompt_overhead="系" * 1600,
        aggressive=True,
    )

    assert aggressive.retained_from_sequence > normal.retained_from_sequence
    assert aggressive.estimated_tokens < normal.estimated_tokens


def test_every_assembly_is_reported_once_with_its_budget_facts(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 6, size=300)
    record = []

    assembler_for(rt, SummaryModel(["历史摘要"] * 5), record=record.append).assemble(
        conversation_id=created.conversation_id,
        current_sequence=12,
        prompt_overhead="系统",
    )

    assert len(record) == 1
    payload = record[0]
    assert payload["reason"] == REASON_SUMMARIZED
    assert payload["estimator"] == "heuristic_v1"
    assert payload["input_budget"] == TIGHT.input_budget
    assert payload["summary_trigger"] == TIGHT.summary_trigger
    assert payload["retained_to_sequence"] == 12
    assert payload["aggressive"] is False
    assert payload["summary_call_made"] is True


def test_a_conversation_with_no_ledger_uses_the_transcript_path(rt, session):
    """The `turn=None` path has no ledger and must not try to summarize (INV-7)."""

    record = []

    view = assembler_for(rt, SummaryModel(), record=record.append).assemble(
        conversation_id=None,
        current_sequence=0,
        prompt_overhead="系统",
        transcript_fallback="user: 你好",
    )

    assert view.reason == REASON_NO_CONVERSATION
    assert view.transcript == "user: 你好"
    assert view.summary is None
    assert record[-1]["reason"] == REASON_NO_CONVERSATION


def test_a_fitting_aggressive_pass_reports_the_crop_and_only_once(rt, session):
    """The recursive pass is a crop, not a second assembly: one event, one reason.

    A refactor that emitted before recursing, or that let the recursive call report its own
    reason, would be invisible to every other test here -- the recursion only returns when the
    normal pass could not fit and the aggressive one could.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 11, size=100)
    record = []

    view = assembler_for(rt, SummaryModel(["历史摘要"] * 6), record=record.append).assemble(
        conversation_id=created.conversation_id,
        current_sequence=22,
        prompt_overhead="系" * 3700,
    )

    assert view.reason == REASON_AGGRESSIVE_CROP
    assert view.estimated_tokens <= TIGHT.input_budget
    assert len(record) == 1, "the crop is reported once, by the pass that produced it"
    assert record[0]["reason"] == REASON_AGGRESSIVE_CROP
    assert record[0]["aggressive"] is True


def test_a_chain_that_exists_but_cannot_be_trusted_is_reported_as_such(rt, session):
    """`no_valid_summary` is not `within_budget`: the difference is what is on disk."""

    created = rt.conversations.create(session["user_id"], "甲")
    seed_turns(rt, session["user_id"], created.conversation_id, 1, size=300)
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_orphan",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id="sum_never_committed",
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="悬空的一版",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()
    record = []

    view = assembler_for(rt, SummaryModel(), record=record.append).assemble(
        conversation_id=created.conversation_id,
        current_sequence=2,
        prompt_overhead="系" * 1600,
    )

    assert view.reason == REASON_NO_VALID_SUMMARY
    assert view.summary is None
    assert record[-1]["reason"] == REASON_NO_VALID_SUMMARY


def test_a_summary_that_already_covers_the_current_message_is_not_used(rt, session):
    """INV-1: the view must never be built from a summary that swallows the current turn.

    This is the second witness for the one guard at `app/context/summary.py:153`. The first
    (`tests/test_context_summary.py:475`) drives `latest_valid_summary` directly; this one
    drives it through the assembler, which is the only caller that decides what the model
    actually sees.

    Four conditions have to hold at once, and a construction that misses one is a false witness:
    the ledger holds a version (which is what makes `chain_unusable` True), that version's
    `covered_to_sequence` equals this assembly's `current_sequence` (or the guard is not what
    rejected it), the estimate sits in `(summary_trigger, input_budget]` (or the assembly
    short-circuits before the reason can be `no_valid_summary`), and the strip is empty (or a
    summary call happens and the reason becomes `summary_failed`). One unclosed user message of
    2500 CJK characters gives `estimated = 0 + 2500 + 4 = 2504`: above the 2000 trigger, inside
    the 4000 budget, and a lone unclosed round is never stripped. `ExplodingModel` is a fuse, not
    a proof: it guarantees only that if the strip were ever non-empty the assembly would fail
    loudly (`summary_failed`) instead of quietly returning a view that looks fine.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    content = "问" * 2500
    receipt = rt.conversations.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k0",
        expected_hash="h0",
        content=content,
    )
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_covering_the_current_turn",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=receipt.sequence,
                summary_text="把当前这条也吞掉的摘要",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    view = assembler_for(rt, ExplodingModel()).assemble(
        conversation_id=created.conversation_id,
        current_sequence=receipt.sequence,
        prompt_overhead="",
    )

    assert view.summary is None, "a summary that already covers the current message is not usable"
    assert view.reason == REASON_NO_VALID_SUMMARY, "and the view says why it went without one"
    assert view.estimated_tokens > TIGHT.summary_trigger, "above the trigger, or no reason to look"
    assert view.estimated_tokens <= TIGHT.input_budget
    assert view.transcript == "user: " + content, "the current message is still shown in full"
