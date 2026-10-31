"""The message ledger: append-only, monotonically sequenced, and honest about `run_id`.

The ledger is the record of what the user and the assistant actually saw, so two properties
carry the whole design: a row is never rewritten, and `sequence` is allocated under a lock
so two concurrent appends cannot share one. Both are tested against the real database --
the lock is the database's, and a mock cannot have it.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.conversation.service import ConversationService, request_hash
from app.core.errors import AppError
from app.storage.models import Conversation, ConversationMessage


def conversations(rt) -> ConversationService:
    return ConversationService(rt.database)


def test_the_ledger_has_no_way_to_rewrite_a_row():
    """Immutability is a schema fact, not a convention: there is no `updated_at` to write."""

    columns = set(ConversationMessage.__table__.columns.keys())

    assert "updated_at" not in columns
    assert {"message_id", "conversation_id", "run_id", "sequence", "role", "content"} <= columns
    assert "message_kind" in columns and "question_id" in columns and "metadata" in columns


def test_messages_are_appended_in_sequence_and_read_back_unchanged(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]

    first = service.append_user_message(
        user,
        created.conversation_id,
        idempotency_key="k1",
        expected_hash=request_hash(created.conversation_id, "第一句", None),
        content="第一句",
    )
    second = service.append_user_message(
        user,
        created.conversation_id,
        idempotency_key="k2",
        expected_hash=request_hash(created.conversation_id, "第二句", None),
        content="第二句",
    )

    page = service.messages(user, created.conversation_id)

    assert [item["sequence"] for item in page["items"]] == [1, 2]
    assert [item["content"] for item in page["items"]] == ["第一句", "第二句"]
    assert [item["message_id"] for item in page["items"]] == [first.message_id, second.message_id]
    assert {item["role"] for item in page["items"]} == {"user"}
    # Reading does not change what was read.
    assert service.messages(user, created.conversation_id)["items"] == page["items"]


def test_a_second_read_of_the_same_page_is_identical(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    for index in range(3):
        service.append_user_message(
            user,
            created.conversation_id,
            idempotency_key=f"k{index}",
            expected_hash=request_hash(created.conversation_id, f"m{index}", None),
            content=f"m{index}",
        )

    first = service.messages(user, created.conversation_id, limit=2)
    second = service.messages(user, created.conversation_id, limit=2, after=first["next_cursor"])

    assert first["next_cursor"] == 2
    assert [item["sequence"] for item in second["items"]] == [3]
    assert second["next_cursor"] is None


def test_concurrent_appends_never_share_a_sequence(rt, session):
    """Four writers, one conversation: the row lock is the only thing that can order them."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]

    def write(index: int) -> int:
        return service.append_user_message(
            user,
            created.conversation_id,
            idempotency_key=f"k{index}",
            expected_hash=request_hash(created.conversation_id, f"m{index}", None),
            content=f"m{index}",
        ).sequence

    with ThreadPoolExecutor(max_workers=4) as pool:
        allocated = sorted(pool.map(write, range(4)))

    assert allocated == [1, 2, 3, 4]
    with rt.database.sessions() as db:
        stored = db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == created.conversation_id)
        )
        next_sequence = db.scalar(
            select(Conversation.next_message_sequence).where(
                Conversation.conversation_id == created.conversation_id
            )
        )
    assert stored == 4
    assert next_sequence == 5


def test_an_interrupt_answer_must_name_its_question(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])

    with pytest.raises(ValueError):
        service.append_user_message(
            session["user_id"],
            created.conversation_id,
            idempotency_key="k1",
            expected_hash="h",
            content="答案",
            message_kind="interrupt_answer",
        )


def test_only_an_interrupt_answer_carries_a_question_id(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])

    with pytest.raises(ValueError):
        service.append_user_message(
            session["user_id"],
            created.conversation_id,
            idempotency_key="k1",
            expected_hash="h",
            content="普通消息",
            question_id="q_1",
        )


def test_the_database_refuses_an_answer_without_a_question(rt, session):
    """The Python guard is a courtesy; the constraint is the contract."""

    service = conversations(rt)
    created = service.create(session["user_id"])

    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.add(
                ConversationMessage(
                    message_id="msg_bad",
                    conversation_id=created.conversation_id,
                    run_id=None,
                    sequence=99,
                    role="user",
                    content="没有问题的答案",
                    message_kind="interrupt_answer",
                    question_id=None,
                    created_at=created.created_at,
                    meta={},
                )
            )


def test_an_interrupt_answer_is_stored_with_its_question(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])

    receipt = service.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h",
        content="金额：100",
        message_kind="interrupt_answer",
        question_id="q_abc",
    )

    stored = service.messages(session["user_id"], created.conversation_id)["items"][0]
    assert stored["message_kind"] == "interrupt_answer"
    assert stored["question_id"] == "q_abc"
    assert stored["message_id"] == receipt.message_id


def test_a_migrated_message_may_have_no_run(rt, session):
    """A history import cannot always recover the run it belonged to, and must not invent one."""

    service = conversations(rt)
    created = service.create(session["user_id"])

    service.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h",
        content="旧会话里的消息",
    )

    stored = service.messages(session["user_id"], created.conversation_id)["items"][0]
    assert stored["run_id"] is None


def test_attaching_a_run_marks_the_submission_and_its_message_together(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    receipt = service.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash=request_hash(created.conversation_id, "转账", None),
        content="转账",
    )

    attached = service.attach_run(session["user_id"], receipt, "run_abc")

    assert attached.run_id == "run_abc"
    assert attached.message_id == receipt.message_id
    assert (
        service.messages(session["user_id"], created.conversation_id)["items"][0]["run_id"]
        == "run_abc"
    )
    # The receipt the key would return now carries the run, so a replay is answered with the
    # same correlation id rather than a null.
    replay = service.find_submission(
        session["user_id"], "k1", request_hash(created.conversation_id, "转账", None)
    )
    assert replay.run_id == "run_abc"
    assert replay.duplicate is True


def test_attaching_a_run_to_someone_elses_submission_fails_closed(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    receipt = service.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h",
        content="转账",
    )

    with pytest.raises(AppError) as raised:
        service.attach_run("user_0002", receipt, "run_abc")

    assert raised.value.code == "NOT_FOUND"


def test_the_assistant_reply_lands_in_the_same_ledger_with_its_run(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])

    written = service.append_assistant_message(
        session["user_id"],
        created.conversation_id,
        run_id="run_abc",
        content="已经完成",
        metadata={"business_ids": ["T-1"]},
    )

    stored = service.messages(session["user_id"], created.conversation_id)["items"][0]
    assert written.sequence == 1
    assert stored["role"] == "assistant"
    assert stored["run_id"] == "run_abc"
    assert stored["metadata"] == {"business_ids": ["T-1"]}


def test_an_archived_conversation_still_records_the_reply_of_a_running_turn(rt, session):
    """Archiving stops new submissions; it does not cancel a turn already in flight."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    service.update(session["user_id"], created.conversation_id, archived=True)

    service.append_assistant_message(
        session["user_id"], created.conversation_id, run_id="run_abc", content="迟到的答复"
    )

    assert (
        service.messages(session["user_id"], created.conversation_id)["items"][0]["content"]
        == "迟到的答复"
    )
    assert service.get(session["user_id"], created.conversation_id).last_message_at is not None


def test_appending_updates_last_message_at(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    assert created.last_message_at is None

    service.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h",
        content="你好",
    )

    assert service.get(session["user_id"], created.conversation_id).last_message_at is not None
