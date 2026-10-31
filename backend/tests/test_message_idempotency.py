"""One key, one request, one message.

The submission table is the only thing standing between a client retry and a duplicated
turn, so what these tests pin is the whole promise: the same key with the same request
returns the original receipt and appends nothing, the same key with a *different* request is
a conflict rather than a silent answer to a question nobody asked, and a race between two
first submissions leaves exactly one set of resources behind.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.conversation.service import ConversationService, request_hash
from app.core.errors import AppError
from app.storage.models import AppUser, Conversation, ConversationMessage, ConversationSubmission


def conversations(rt) -> ConversationService:
    return ConversationService(rt.database)


def counts(rt, conversation_id):
    with rt.database.sessions() as db:
        messages = db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        )
        submissions = db.scalar(
            select(func.count())
            .select_from(ConversationSubmission)
            .where(ConversationSubmission.conversation_id == conversation_id)
        )
        next_sequence = db.scalar(
            select(Conversation.next_message_sequence).where(
                Conversation.conversation_id == conversation_id
            )
        )
    return int(messages), int(submissions), int(next_sequence)


def submit(service, user_id, conversation_id, content, key, context=None):
    return service.append_user_message(
        user_id,
        conversation_id,
        idempotency_key=key,
        expected_hash=request_hash(conversation_id, content, context),
        content=content,
    )


def test_the_same_key_and_request_returns_the_original_message_and_appends_nothing(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]

    first = submit(service, user, created.conversation_id, "转账 100 元", "key-1")
    replay = submit(service, user, created.conversation_id, "转账 100 元", "key-1")

    assert replay.message_id == first.message_id
    assert replay.submission_id == first.submission_id
    assert replay.sequence == first.sequence
    assert replay.duplicate is True
    assert counts(rt, created.conversation_id) == (1, 1, 2)


def test_a_replay_before_any_dispatch_reports_a_null_run(rt, session):
    """The receipt says the message is stored; it never claims a graph call happened."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    submit(service, user, created.conversation_id, "你好", "key-1")

    replay = service.find_submission(
        user, "key-1", request_hash(created.conversation_id, "你好", None)
    )

    assert replay.run_id is None
    assert replay.duplicate is True


def test_reusing_a_key_for_a_different_body_is_a_conflict(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    submit(service, user, created.conversation_id, "转账 100 元", "key-1")

    with pytest.raises(AppError) as raised:
        submit(service, user, created.conversation_id, "转账 200 元", "key-1")

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    assert raised.value.status == 409
    assert counts(rt, created.conversation_id) == (1, 1, 2)


def test_reusing_a_key_across_conversations_is_a_conflict(rt, session):
    """The key is scoped to the user, so a second conversation reusing it is detectable."""

    service = conversations(rt)
    user = session["user_id"]
    first = service.create(user, "会话一")
    second = service.create(user, "会话二")
    submit(service, user, first.conversation_id, "你好", "key-1")

    with pytest.raises(AppError) as raised:
        submit(service, user, second.conversation_id, "你好", "key-1")

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    assert counts(rt, second.conversation_id) == (0, 0, 1)


def test_reusing_a_key_with_a_different_context_is_a_conflict(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    submit(
        service, user, created.conversation_id, "补充", "key-1", {"resume": {"value": {"v": "1"}}}
    )

    with pytest.raises(AppError) as raised:
        submit(
            service,
            user,
            created.conversation_id,
            "补充",
            "key-1",
            {"resume": {"value": {"v": "2"}}},
        )

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


def test_naming_the_question_is_a_different_request_from_leaving_it_open(rt, session):
    """`question_id` is addressing, not decoration: binding it changes what the request means."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    submit(
        service, user, created.conversation_id, "补充", "key-1", {"resume": {"value": {"v": "1"}}}
    )

    with pytest.raises(AppError) as raised:
        submit(
            service,
            user,
            created.conversation_id,
            "补充",
            "key-1",
            {"resume": {"question_id": "q_1", "value": {"v": "1"}}},
        )

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


def test_two_users_may_spend_the_same_key(rt, session):
    """The key is the client's, not a global namespace: two clients collide harmlessly."""

    service = conversations(rt)
    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id="user_0002"))
    mine = service.create(session["user_id"])
    theirs = service.create("user_0002")
    first = submit(service, session["user_id"], mine.conversation_id, "你好", "shared-key")
    second = submit(service, "user_0002", theirs.conversation_id, "你好", "shared-key")

    assert first.message_id != second.message_id
    assert counts(rt, mine.conversation_id) == (1, 1, 2)
    assert counts(rt, theirs.conversation_id) == (1, 1, 2)


def test_a_blank_key_is_refused_before_anything_is_written(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])

    with pytest.raises(ValueError):
        service.append_user_message(
            session["user_id"],
            created.conversation_id,
            idempotency_key="   ",
            expected_hash="h",
            content="你好",
        )

    assert counts(rt, created.conversation_id) == (0, 0, 1)


def test_concurrent_first_submissions_leave_one_set_of_resources(rt, session):
    """Both callers must end up with the same message, and the loser must leave no trace.

    The loser's candidate message and its sequence increment roll back with the transaction
    that lost the unique key, so the ledger holds one row and the next sequence is untouched
    -- which is what makes the retry safe to answer with the winner's receipt.
    """

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]

    def race(_index: int):
        return submit(service, user, created.conversation_id, "只发一次", "key-race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(race, range(2)))

    assert receipts[0].message_id == receipts[1].message_id
    assert receipts[0].submission_id == receipts[1].submission_id
    assert {receipt.duplicate for receipt in receipts} == {False, True}
    assert counts(rt, created.conversation_id) == (1, 1, 2)


def test_the_losers_candidate_message_is_not_left_behind(rt, session):
    """The same race, read from the ledger: one row, sequence 1, nothing at sequence 2."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]

    def race(_index: int):
        return submit(service, user, created.conversation_id, "只发一次", "key-race")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(race, range(4)))

    page = service.messages(user, created.conversation_id)
    assert [item["sequence"] for item in page["items"]] == [1]
    assert counts(rt, created.conversation_id) == (1, 1, 2)


def test_an_archived_conversation_refuses_the_first_submission_and_stores_nothing(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    service.update(session["user_id"], created.conversation_id, archived=True)

    with pytest.raises(AppError) as raised:
        submit(service, session["user_id"], created.conversation_id, "你好", "key-1")

    assert raised.value.code == "CONVERSATION_NOT_WRITABLE"
    assert counts(rt, created.conversation_id) == (0, 0, 1)


def test_the_key_is_not_released_by_finishing_the_turn(rt, session):
    """A spent key stays spent: a late retry of a completed turn is still a replay."""

    service = conversations(rt)
    created = service.create(session["user_id"])
    user = session["user_id"]
    first = submit(service, user, created.conversation_id, "你好", "key-1")
    service.attach_run(user, first, "run_abc")
    service.append_assistant_message(
        user, created.conversation_id, run_id="run_abc", content="已经完成"
    )

    replay = submit(service, user, created.conversation_id, "你好", "key-1")

    assert replay.message_id == first.message_id
    assert replay.run_id == "run_abc"
    assert counts(rt, created.conversation_id) == (2, 1, 3)
