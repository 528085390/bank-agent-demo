"""The number a user calls a conversation by: assigned per owner, once, and never reused.

The number is what the list shows and what a person says out loud ("the third one"), so the
properties that make it usable are pinned here against the real database rather than a mock:
it counts from 1 within each owner, a hidden conversation keeps the number it was given, and
two conversations created at the same instant still get distinct numbers.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from app.conversation.service import ConversationService
from app.storage.models import AppUser

OTHER_USER = "user_0002"


def conversations(rt) -> ConversationService:
    return ConversationService(rt.database)


def test_a_users_conversations_are_numbered_from_one(rt, session):
    service = conversations(rt)

    first = service.create(session["user_id"], "第一个")
    second = service.create(session["user_id"], "第二个")

    assert first.serial_no == 1
    assert second.serial_no == 2
    # The number is published, or the list could not show it.
    assert first.as_dict()["serial_no"] == 1
    assert second.as_dict()["serial_no"] == 2


def test_each_owner_numbers_their_own_conversations(rt, session):
    """Two users both have a number 1: the sequence is per owner, not global."""

    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id=OTHER_USER))
    service = conversations(rt)

    mine = service.create(session["user_id"], "我的")
    theirs = service.create(OTHER_USER, "他的")

    assert (mine.serial_no, theirs.serial_no) == (1, 1)
    assert service.create(OTHER_USER, "他的第二个").serial_no == 2
    assert service.create(session["user_id"], "我的第二个").serial_no == 2


def test_a_soft_deleted_conversation_keeps_its_number(rt, session):
    """Numbers are not recycled: the deleted row still holds 1, so the next one is 2."""

    service = conversations(rt)
    first = service.create(session["user_id"], "第一个")

    deleted = service.soft_delete(session["user_id"], first.conversation_id)
    second = service.create(session["user_id"], "第二个")

    assert deleted.serial_no == 1
    assert second.serial_no == 2


def test_an_archived_conversation_keeps_its_number(rt, session):
    service = conversations(rt)
    first = service.create(session["user_id"], "第一个")
    second = service.create(session["user_id"], "第二个")

    archived = service.update(session["user_id"], first.conversation_id, archived=True)
    third = service.create(session["user_id"], "第三个")

    assert archived.serial_no == 1
    assert second.serial_no == 2
    assert third.serial_no == 3


def test_two_conversations_created_at_once_still_get_distinct_numbers(rt, session):
    """The race the owner-row lock exists to lose gracefully.

    A user's first two conversations have no conversation row to lock, so two simultaneous
    creates could read the same maximum and both write it -- one of them then hitting
    `uq_conversation_serial_no_owner`. The service locks the *owner* row instead, so both
    succeed and the numbers are 1 and 2.
    """

    service = conversations(rt)
    barrier = threading.Barrier(2)

    def create(index: int) -> int:
        barrier.wait(timeout=30)
        return service.create(session["user_id"], f"并发 {index}").serial_no

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = sorted(pool.map(create, range(2)))

    assert numbers == [1, 2]
