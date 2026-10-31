"""Summarization: the chunking, the model boundary, and the version chain.

Every test here is about a claim the context manager later relies on. A summary is the only
text the runtime reads twice -- it is written once and then prepended to every subsequent
prompt -- so the properties worth pinning are the ones that would silently corrupt a long
conversation if they broke: which messages a version claims to cover, that the text is
cumulative, that a failure writes nothing, and that reading the chain back refuses to trust a
hole in it.
"""

import json
from contextlib import nullcontext

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.context import summary as summary_module
from app.context.ledger import LedgerMessage
from app.context.summary import (
    generate_summaries,
    latest_valid_summary,
    source_hash,
)
from app.core.types import utcnow
from app.integration.model_client import ModelClientError, ModelReply
from app.storage.models import ContextSummary
from tests.support.scripted_model import ScriptedModel

# The demo archive every conversation in this file belongs to (`tests/conftest.py:session`).
# Since P3 a read of the ledger or the summary chain has to say who it is reading for.
OWNER = "user_0001"


def message(sequence, role, content, kind="message", question_id=None):
    return LedgerMessage(sequence, role, content, kind, question_id)


def strip_of(*pairs):
    rows = []
    for index, (user, assistant) in enumerate(pairs, start=1):
        rows.append(message(index * 2 - 1, "user", user))
        rows.append(message(index * 2, "assistant", assistant))
    return rows


class SummaryModel:
    """Answers with a JSON summary object and records the prompts it saw."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, messages):
        # The user message is what the two content assertions below read: the system message is
        # the fixed `SUMMARY_PROMPT`, and the chunk and the previous summary travel together in
        # the user turn.
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


def generate(rt, conversation_id, model, **overrides):
    kwargs = dict(
        conversation_id=conversation_id,
        user_id=OWNER,
        base=None,
        strip=strip_of(("甲", "甲答")),
        chunk_budget=32768,
        max_tokens=1024,
        summary_scope=nullcontext,
    )
    kwargs.update(overrides)
    return generate_summaries(model, rt.database, **kwargs)


def rows(rt, conversation_id):
    with rt.database.sessions() as db:
        return list(
            db.scalars(
                select(ContextSummary)
                .where(ContextSummary.conversation_id == conversation_id)
                .order_by(ContextSummary.version.asc())
            )
        )


def stored_hashes(rt, conversation_id):
    """What was written, not what was returned: `Summary` carries no `source_hash`."""

    with rt.database.sessions() as db:
        return {
            row.version: row.source_hash
            for row in db.scalars(
                select(ContextSummary).where(ContextSummary.conversation_id == conversation_id)
            )
        }


def test_the_first_version_covers_from_sequence_one(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")

    result = generate(rt, created.conversation_id, SummaryModel(["第一版摘要"]))

    assert (result.version, result.covered_from_sequence, result.covered_to_sequence) == (1, 1, 2)
    assert result.summary_text == "第一版摘要"
    assert result.base_summary_id is None


def test_the_next_version_is_fed_the_previous_summary_and_covers_only_the_new_range(rt, session):
    """Cumulative text, incremental range: the model merges, the row records what it read."""

    created = rt.conversations.create(session["user_id"], "甲")
    model = SummaryModel(["第一版", "第二版"])

    first = generate(rt, created.conversation_id, model)
    second = generate(
        rt,
        created.conversation_id,
        model,
        base=first,
        strip=[message(3, "user", "乙"), message(4, "assistant", "乙答")],
    )

    assert (second.version, second.covered_from_sequence, second.covered_to_sequence) == (2, 3, 4)
    assert second.base_summary_id == first.summary_id
    assert "第一版" in model.prompts[1], (
        "the previous summary is the input that makes it cumulative"
    )


def test_a_long_strip_is_summarized_in_consecutive_chunks(rt, session):
    """This is what makes the summary call's own input provably bounded."""

    created = rt.conversations.create(session["user_id"], "甲")
    model = SummaryModel(["块一", "块二"])
    strip = [
        message(1, "user", "甲" * 900),
        message(2, "assistant", "答一"),
        message(3, "user", "乙"),
        message(4, "assistant", "答二"),
    ]

    result = generate(rt, created.conversation_id, model, strip=strip, chunk_budget=100)

    stored = rows(rt, created.conversation_id)
    assert [(r.version, r.covered_from_sequence, r.covered_to_sequence) for r in stored] == [
        (1, 1, 2),
        (2, 3, 4),
    ]
    assert result.version == 2, "the newest committed version is returned"
    assert "块一" in model.prompts[1], "the second chunk sees the first chunk's summary"


def test_the_summary_prompt_carries_no_caller_identity(rt, session):
    """Identity must not be copied into a long-lived, repeatedly-read derived text."""

    created = rt.conversations.create(session["user_id"], "甲")
    model = SummaryModel(["摘要"])

    generate(rt, created.conversation_id, model)

    prompt = model.prompts[0]
    assert session["user_id"] not in prompt
    assert created.conversation_id not in prompt


def test_an_oversized_summary_is_a_failure_and_writes_nothing(rt, session):
    """A summary that blows its own budget would break the invariant it exists to serve."""

    created = rt.conversations.create(session["user_id"], "甲")

    result = generate(rt, created.conversation_id, SummaryModel(["长" * 5000]))

    assert result is None
    assert rows(rt, created.conversation_id) == []


def test_a_non_json_reply_is_a_failure_and_writes_nothing(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")

    result = generate(rt, created.conversation_id, ScriptedModel(["这不是 JSON"]))

    assert result is None
    assert rows(rt, created.conversation_id) == []


def test_a_reply_missing_the_summary_field_is_a_failure(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")

    result = generate(rt, created.conversation_id, ScriptedModel(['{"other":"x"}']))

    assert result is None
    assert rows(rt, created.conversation_id) == []


def test_a_provider_failure_does_not_escape_the_generator(rt, session):
    """INV-15: the runtime promises to keep the old summary, not to fail the run."""

    created = rt.conversations.create(session["user_id"], "甲")

    result = generate(rt, created.conversation_id, ExplodingModel())

    assert result is None
    assert rows(rt, created.conversation_id) == []


def test_a_later_chunk_failing_keeps_the_earlier_committed_versions(rt, session):
    """The chain's every prefix is valid, so partial progress is worth keeping."""

    created = rt.conversations.create(session["user_id"], "甲")

    class FailsOnSecond(SummaryModel):
        def complete(self, messages):
            if len(self.prompts) == 1:
                raise ModelClientError("LLM_TIMEOUT", retryable=True, detail="timeout")
            return super().complete(messages)

    strip = [
        message(1, "user", "甲" * 900),
        message(2, "assistant", "答一"),
        message(3, "user", "乙"),
        message(4, "assistant", "答二"),
    ]

    result = generate(
        rt, created.conversation_id, FailsOnSecond(["块一"]), strip=strip, chunk_budget=100
    )

    assert [(r.version, r.covered_to_sequence) for r in rows(rt, created.conversation_id)] == [
        (1, 2)
    ]
    assert result.version == 1


def test_a_failure_with_a_base_keeps_the_base(rt, session):
    """The base is still the newest committed version, and Task 7 branches on identity."""

    created = rt.conversations.create(session["user_id"], "甲")
    first = generate(rt, created.conversation_id, SummaryModel(["第一版"]))

    second = generate(
        rt,
        created.conversation_id,
        ExplodingModel(),
        base=first,
        strip=[message(3, "user", "乙"), message(4, "assistant", "乙答")],
    )

    assert second is first
    assert [r.version for r in rows(rt, created.conversation_id)] == [1]


class RecordingScope:
    """A `summary_scope` that records the calls that ran inside the context it opens."""

    def __init__(self):
        self.calls = []

    def __call__(self):
        return self

    def __enter__(self):
        self.calls.append("enter")
        return self

    def __exit__(self, kind, value, traceback):
        self.calls.append("exit")
        return False


class RecordingModel(SummaryModel):
    def __init__(self, texts, scope):
        super().__init__(texts)
        self._scope = scope

    def complete(self, messages):
        self._scope.calls.append("model")
        return super().complete(messages)


def test_a_rejected_chain_is_repaired_by_a_rebuild(rt, session):
    """A broken chain must cost one rebuild, not summarization forever.

    The rebuild is committed as a second root above the rows it replaced, so the reader has to
    judge the chain the newest version belongs to and leave the older ones where they are --
    otherwise every later read fails and the conversation grows versions it can never use.

    The orphaned root starts at 5 rather than 1 on purpose: a reader that walked the chain but
    still demanded the oldest row in the table be a root at `covered_from_sequence == 1` would
    pass against a root at 1, so the repair below is only evidence of the real fix when the root
    is nowhere near the start.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_a",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=5,
                covered_to_sequence=6,
                summary_text="一",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            ContextSummary(
                summary_id="sum_b",
                conversation_id=created.conversation_id,
                version=2,
                base_summary_id="sum_a",
                covered_from_sequence=9,
                covered_to_sequence=10,
                summary_text="二",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()
    before = [
        (
            row.version,
            row.summary_id,
            row.covered_from_sequence,
            row.covered_to_sequence,
            row.summary_text,
        )
        for row in rows(rt, created.conversation_id)
    ]

    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=20, user_id=OWNER
    ) is None

    rebuilt = generate(
        rt,
        created.conversation_id,
        SummaryModel(["重建"]),
        strip=strip_of(("甲", "甲答"), ("乙", "乙答")),
    )
    found = latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=9, user_id=OWNER
    )

    assert (rebuilt.version, rebuilt.base_summary_id, rebuilt.covered_from_sequence) == (3, None, 1)
    assert found == rebuilt
    after = [
        (
            row.version,
            row.summary_id,
            row.covered_from_sequence,
            row.covered_to_sequence,
            row.summary_text,
        )
        for row in rows(rt, created.conversation_id)
    ]
    assert after[:2] == before and [version for version, *_ in after] == [1, 2, 3]

    extended = generate(
        rt,
        created.conversation_id,
        SummaryModel(["继续"]),
        base=rebuilt,
        strip=[message(5, "user", "丙"), message(6, "assistant", "丙答")],
    )

    assert (extended.version, extended.base_summary_id) == (4, rebuilt.summary_id)


def test_a_version_covers_the_messages_it_was_actually_given(rt, session):
    """The row's range is read off the chunk, never inferred from the base summary.

    The base stopped at sequence 4 and this call's strip is messages 5 and 7 -- a gap inside the
    strip -- so the range the chunk actually carries is (5, 7). The forbidden derivation from the
    base (`covered_from = base.covered_to_sequence + 1`, plus one sequence for each message read)
    would write (5, 6) instead: a range the summary never read. A strip continuing directly from
    the base is exactly the shape where the two readings coincide, which is why the gap is what
    lets this test fail under the derivation the module forbids; production strips continue
    directly, so nothing else would notice the difference -- and a range the code never verified
    would make the chain check vacuous for every row this module writes.

    `current_sequence=8` is the smallest value that leaves the row valid: the reader rejects a
    version whose coverage reaches the message the user just sent, and this row covers through 7.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    full = strip_of(("甲", "甲答"), ("乙", "乙答"))
    first = generate(rt, created.conversation_id, SummaryModel(["第一版"]), strip=full)
    boundary = next(entry for entry in full if entry.sequence == first.covered_to_sequence)
    strip = [message(5, "user", "丙"), message(7, "assistant", "丙丁")]

    second = generate(
        rt, created.conversation_id, SummaryModel(["第二版"]), base=first, strip=strip
    )
    found = latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=8, user_id=OWNER
    )
    stored_hash = stored_hashes(rt, created.conversation_id)[second.version]

    assert boundary.role == "assistant"
    assert (first.covered_from_sequence, first.covered_to_sequence) == (1, 4)
    assert (second.version, second.covered_from_sequence, second.covered_to_sequence) == (2, 5, 7)
    assert stored_hash == source_hash(strip)
    assert found == second


def test_a_colliding_version_number_fails_loudly(rt, session, monkeypatch):
    """Spec §5.1: a taken version number is a programming error, not 'no summary this time'.

    The version number is read before the insert's own transaction opens, so a collision needs a
    second writer to take the number in that window -- a window no test double can enter, since
    it exists between two statements of this module. Stubbing the read to hand the insert a
    number that is already taken puts the collision where it is observable; what the test then
    pins is the insert's behaviour, which is the part the spec speaks about: the constraint must
    raise out of the assembly rather than be reported as 'no summary this time'.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_taken",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="抢到的版本",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
    monkeypatch.setattr(
        summary_module,
        "_next_version",
        lambda database, conversation_id, *, user_id: 1,
    )

    with pytest.raises(IntegrityError):
        generate(rt, created.conversation_id, SummaryModel(["第一版"]))

    assert [(row.version, row.summary_id) for row in rows(rt, created.conversation_id)] == [
        (1, "sum_taken")
    ]


def test_the_summary_call_runs_inside_the_summary_scope(rt, session):
    """INV-15: the summarizer's model call has to be audited as a summary call."""

    created = rt.conversations.create(session["user_id"], "甲")
    scope = RecordingScope()

    result = generate(
        rt, created.conversation_id, RecordingModel(["第一版"], scope), summary_scope=scope
    )

    assert scope.calls == ["enter", "model", "exit"]
    assert result.covered_to_sequence == 2


def test_the_latest_valid_summary_is_read_back(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    generate(rt, created.conversation_id, SummaryModel(["第一版"]))

    found = latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=5, user_id=OWNER
    )

    assert (found.version, found.summary_text) == (1, "第一版")


def test_a_summary_is_read_only_from_its_own_conversation(rt, session):
    """Isolation: one conversation's memory must never answer for another's.

    Both conversations belong to the same user, which is the case a missing filter would
    conflate -- the conversation id, not the owner, is what separates two memories.
    """

    first = rt.conversations.create(session["user_id"], "甲")
    second = rt.conversations.create(session["user_id"], "乙")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_first",
                conversation_id=first.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="甲的摘要",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    found = latest_valid_summary(
        rt.database, first.conversation_id, current_sequence=9, user_id=OWNER
    )
    other = latest_valid_summary(
        rt.database, second.conversation_id, current_sequence=9, user_id=OWNER
    )

    assert (found.version, found.summary_text) == (1, "甲的摘要")
    assert other is None


def test_a_summary_read_is_not_disturbed_by_another_conversations_versions(rt, session):
    """Each conversation reads its own coverage, not the other's version of it."""

    first = rt.conversations.create(session["user_id"], "甲")
    second = rt.conversations.create(session["user_id"], "乙")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_own_first",
                conversation_id=first.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="甲的摘要",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.add(
            ContextSummary(
                summary_id="sum_own_second",
                conversation_id=second.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=4,
                summary_text="乙的摘要",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    found = latest_valid_summary(
        rt.database, first.conversation_id, current_sequence=9, user_id=OWNER
    )
    other = latest_valid_summary(
        rt.database, second.conversation_id, current_sequence=9, user_id=OWNER
    )

    assert found.summary_text == "甲的摘要"
    assert (found.covered_from_sequence, found.covered_to_sequence) == (1, 2)
    assert other.summary_text == "乙的摘要"
    assert (other.covered_from_sequence, other.covered_to_sequence) == (1, 4)


def test_a_summary_covering_the_current_message_is_not_valid(rt, session):
    """INV-1: the user's newest message must never be summarized away."""

    created = rt.conversations.create(session["user_id"], "甲")
    generate(rt, created.conversation_id, SummaryModel(["第一版"]))

    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=2, user_id=OWNER
    ) is None


def test_a_chain_that_does_not_start_at_sequence_one_is_not_valid(rt, session):
    """A hole in the chain means the summary remembers a history that never happened."""

    created = rt.conversations.create(session["user_id"], "甲")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_broken",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=5,
                covered_to_sequence=6,
                summary_text="凭空开始",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=9, user_id=OWNER
    ) is None


def test_a_gap_between_two_versions_is_not_valid(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_a",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="一",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            ContextSummary(
                summary_id="sum_b",
                conversation_id=created.conversation_id,
                version=2,
                base_summary_id="sum_a",
                covered_from_sequence=9,
                covered_to_sequence=10,
                summary_text="二",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=20, user_id=OWNER
    ) is None


def test_a_version_number_gap_in_the_chain_is_not_valid(rt, session):
    """The descent check that makes the walk terminate.

    Coverage here is contiguous and adjacent (1-2, then 3-4) and the root starts at sequence 1,
    so every condition but the version descent holds. Requiring a link's version to be exactly one
    above its parent's is what makes each step of the walk move to a strictly smaller version;
    with `(conversation_id, version)` unique, no cycle of `base_summary_id` links can be followed,
    which is why the reader needs no visited set. With the check removed this chain reads back as
    a valid summary instead of `None`.
    """

    created = rt.conversations.create(session["user_id"], "甲")
    with rt.database.sessions.begin() as db:
        db.add(
            ContextSummary(
                summary_id="sum_root",
                conversation_id=created.conversation_id,
                version=1,
                base_summary_id=None,
                covered_from_sequence=1,
                covered_to_sequence=2,
                summary_text="一",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()
        db.add(
            ContextSummary(
                summary_id="sum_jumped",
                conversation_id=created.conversation_id,
                version=3,
                base_summary_id="sum_root",
                covered_from_sequence=3,
                covered_to_sequence=4,
                summary_text="二",
                source_hash="h",
                model="m",
                created_at=utcnow(),
            )
        )
        db.flush()

    # 20 is far past this row's coverage, so the refusal cannot come from INV-1's rule that a
    # summary must not reach the message the user just sent: only the walk can reject the chain.
    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=20, user_id=OWNER
    ) is None


def test_a_dangling_base_link_is_not_valid(rt, session):
    """The truncated-chain case: a link naming a summary this read cannot see.

    The row is otherwise entirely valid -- it starts at sequence 1 and covers through 2, its
    version is 1, and `current_sequence=20` is far past its coverage, so neither the
    start-at-one check nor INV-1's rule can refuse it. `base_summary_id` names a summary that
    does not exist in this conversation, which is what a chain loses when the version it
    descends from was never committed or has fallen outside the read: the parent lookup finds
    nothing, and a reader that treated a missing link as the end of the chain (breaking out of
    the loop, or returning `_from_row(current)`) would hand back a summary whose history is
    unverified. Only the walk's missing-parent branch can turn this row into `None`.
    """

    created = rt.conversations.create(session["user_id"], "甲")
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

    assert latest_valid_summary(
        rt.database, created.conversation_id, current_sequence=20, user_id=OWNER
    ) is None


def test_the_source_hash_is_stable_and_content_sensitive():
    rows_ = strip_of(("甲", "甲答"))

    assert source_hash(rows_) == source_hash(list(rows_))
    assert source_hash(rows_) != source_hash([*rows_[:-1], message(2, "assistant", "改过的答")])
