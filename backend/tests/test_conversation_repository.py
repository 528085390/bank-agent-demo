"""Conversation lifecycle: ownership, filtering and a page order that cannot drift.

The ordering `pinned DESC, COALESCE(last_message_at, created_at) DESC, conversation_id DESC` is a
promise to the client, and a cursor that pages through it is the only thing that makes the
promise usable. Both are pinned here against a service backed by the real database, because
the failure this file exists to catch -- a page that repeats or skips a row -- is a
property of the query, not of a mock.
"""

import base64
import json

import pytest

from app.agents.runtime import thread_id_for_session
from app.conversation.service import (
    ConversationService,
    clean_title,
    legacy_thread_id_for_session,
    request_hash,
    thread_id_for_conversation,
)
from app.core.errors import AppError
from app.storage.models import AppUser

OTHER_USER = "user_0002"


def conversations(rt) -> ConversationService:
    return ConversationService(rt.database)


def say(service, user_id, conversation_id, text, key):
    return service.append_user_message(
        user_id,
        conversation_id,
        idempotency_key=key,
        expected_hash=request_hash(conversation_id, text, None),
        content=text,
    )


def test_a_new_conversation_is_owned_and_owns_a_thread(rt, session):
    service = conversations(rt)

    created = service.create(session["user_id"], "  转账   记录  ")

    assert created.title == "转账 记录", "a title is one line, collapsed"
    assert (created.pinned, created.archived, created.deleted) == (False, False, False)
    assert created.message_count == 0
    assert created.last_message_at is None
    assert created.thread_id == thread_id_for_conversation(created.conversation_id)
    assert created.thread_id.startswith("conv:")
    # The thread is the server's business: publishing it would invite a client to name one.
    assert "thread_id" not in created.as_dict()


def test_two_users_conversations_are_distinct_and_are_not_their_session_ids(rt, session):
    """`conversation_id` is minted, not derived: it must not equal the session id.

    The spec's words are "unguessable"; a test cannot assert that. What it can assert is
    that the id is not the session id and that two users never collide -- the two ways a
    derived id would actually fail.
    """

    service = conversations(rt)
    # A second real user: `conversation.user_id` is a foreign key into `app_user`, so a
    # conversation cannot be owned by a user row that does not exist.
    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id=OTHER_USER))

    mine = service.create(session["user_id"], "我的会话")
    theirs = service.create(OTHER_USER, "他的会话")

    assert mine.conversation_id != theirs.conversation_id
    # Only the user that actually holds a session can be compared with one: the demo session
    # hardcodes `user_0001` (`app/identity/service.py:31`), so `OTHER_USER` has no session id.
    assert mine.conversation_id != session["session_id"]


def test_an_empty_or_absent_title_falls_back_to_the_default(rt, session):
    service = conversations(rt)

    assert service.create(session["user_id"]).title == clean_title(None)
    assert service.create(session["user_id"], "   ").title == clean_title(None)


def test_a_title_is_bounded(rt, session):
    service = conversations(rt)

    created = service.create(session["user_id"], "长" * 500)

    assert len(created.title) == 120


@pytest.mark.parametrize(
    "action",
    [
        lambda service, user, cid: service.get(user, cid),
        lambda service, user, cid: service.require_writable(user, cid),
        lambda service, user, cid: service.update(user, cid, title="改名"),
        lambda service, user, cid: service.soft_delete(user, cid),
        lambda service, user, cid: service.restore(user, cid),
        lambda service, user, cid: service.messages(user, cid),
    ],
)
def test_another_users_conversation_is_indistinguishable_from_a_missing_one(rt, session, action):
    service = conversations(rt)
    mine = service.create(session["user_id"], "我的会话")

    with pytest.raises(AppError) as raised:
        action(service, OTHER_USER, mine.conversation_id)

    assert raised.value.code == "NOT_FOUND"
    assert raised.value.status == 404
    # A miss and a denial must read the same, or the error itself enumerates other users'
    # conversation ids.
    with pytest.raises(AppError) as missing:
        action(service, session["user_id"], "conv_does_not_exist")
    assert missing.value.code == raised.value.code
    assert missing.value.message == raised.value.message


def test_a_conversation_can_be_pinned_renamed_archived_and_restored(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"], "原标题")

    pinned = service.update(session["user_id"], created.conversation_id, pinned=True)
    renamed = service.update(session["user_id"], created.conversation_id, title="新标题")
    archived = service.update(session["user_id"], created.conversation_id, archived=True)
    restored = service.update(session["user_id"], created.conversation_id, archived=False)

    assert pinned.pinned is True
    assert renamed.title == "新标题"
    assert archived.archived is True
    assert restored.archived is False
    assert restored.deleted is False


def test_an_archived_conversation_still_reads_but_stops_accepting_messages(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    say(service, session["user_id"], created.conversation_id, "第一句", "k1")
    service.update(session["user_id"], created.conversation_id, archived=True)

    # Readable, ledger intact.
    assert [
        item["content"]
        for item in service.messages(session["user_id"], created.conversation_id)["items"]
    ] == ["第一句"]
    with pytest.raises(AppError) as raised:
        service.require_writable(session["user_id"], created.conversation_id)
    assert raised.value.code == "CONVERSATION_NOT_WRITABLE"
    with pytest.raises(AppError) as refused:
        say(service, session["user_id"], created.conversation_id, "第二句", "k2")
    assert refused.value.code == "CONVERSATION_NOT_WRITABLE"
    assert (
        service.messages(session["user_id"], created.conversation_id)["items"][-1]["content"]
        == "第一句"
    )


def test_soft_delete_hides_the_conversation_and_restore_brings_the_ledger_back(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    say(service, session["user_id"], created.conversation_id, "别弄丢了", "k1")

    deleted = service.soft_delete(session["user_id"], created.conversation_id)

    assert deleted.deleted is True
    assert [item["conversation_id"] for item in service.list(session["user_id"])["items"]] == []
    with pytest.raises(AppError) as refused:
        say(service, session["user_id"], created.conversation_id, "还在吗", "k2")
    assert refused.value.code == "CONVERSATION_NOT_WRITABLE"

    restored = service.restore(session["user_id"], created.conversation_id)

    assert restored.deleted is False
    assert [
        item["content"]
        for item in service.messages(session["user_id"], created.conversation_id)["items"]
    ] == ["别弄丢了"]


def test_the_default_list_hides_archived_and_deleted_ones(rt, session):
    service = conversations(rt)
    live = service.create(session["user_id"], "在用")
    archived = service.create(session["user_id"], "归档")
    deleted = service.create(session["user_id"], "删除")
    service.update(session["user_id"], archived.conversation_id, archived=True)
    service.soft_delete(session["user_id"], deleted.conversation_id)

    assert [item["conversation_id"] for item in service.list(session["user_id"])["items"]] == [
        live.conversation_id
    ]
    assert {
        item["conversation_id"] for item in service.list(session["user_id"], archived=True)["items"]
    } == {live.conversation_id, archived.conversation_id}
    assert {
        item["conversation_id"] for item in service.list(session["user_id"], deleted=True)["items"]
    } == {live.conversation_id, deleted.conversation_id}


def test_the_list_order_is_pinned_then_recent_then_id(rt, session):
    service = conversations(rt)
    user = session["user_id"]
    made = []
    for index in range(4):
        row = service.create(user, f"会话 {index}")
        say(service, user, row.conversation_id, f"第 {index} 句", f"k{index}")
        made.append(row.conversation_id)
    service.update(user, made[0], pinned=True)

    listed = [item["conversation_id"] for item in service.list(user)["items"]]

    # `made[0]` is pinned and oldest; the rest are newest first.
    assert listed == [made[0], made[3], made[2], made[1]]


def test_a_conversation_with_no_message_yet_sorts_by_when_it_was_created(rt, session):
    """一条消息都还没有的会话是页面上最新的东西，不是最旧的。

    它没有 `last_message_at`，按那一列 `NULLS LAST` 排序就会被每一个说过话的会话压在下面，
    沉出第一页 -- 于是「新建会话」打开的会话，侧栏根本显示不出来。
    """

    service = conversations(rt)
    user = session["user_id"]
    loud = service.create(user, "有消息")
    say(service, user, loud.conversation_id, "你好", "k1")
    quiet = service.create(user, "还没有消息")

    assert [item["conversation_id"] for item in service.list(user)["items"]] == [
        quiet.conversation_id,
        loud.conversation_id,
    ]


def test_paging_walks_the_whole_order_exactly_once(rt, session):
    service = conversations(rt)
    user = session["user_id"]
    made = []
    for index in range(5):
        row = service.create(user, f"会话 {index}")
        say(service, user, row.conversation_id, f"第 {index} 句", f"k{index}")
        made.append(row.conversation_id)
    service.update(user, made[1], pinned=True)

    whole = [item["conversation_id"] for item in service.list(user, limit=10)["items"]]
    walked, cursor, pages = [], None, 0
    while True:
        page = service.list(user, limit=2, cursor=cursor)
        walked.extend(item["conversation_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "paging did not terminate"
    assert walked == whole
    assert len(set(walked)) == len(walked) == 5


def test_paging_walks_an_order_that_starts_with_a_conversation_with_no_message(rt, session):
    """空会话排在第一行，于是第一个页边界正好落在它后面。"""

    service = conversations(rt)
    user = session["user_id"]
    made = []
    for index in range(3):
        row = service.create(user, f"会话 {index}")
        say(service, user, row.conversation_id, f"第 {index} 句", f"k{index}")
        made.append(row.conversation_id)
    quiet = service.create(user, "还没有消息")

    whole = [item["conversation_id"] for item in service.list(user, limit=10)["items"]]
    assert whole[0] == quiet.conversation_id

    walked, cursor, pages = [], None, 0
    while True:
        page = service.list(user, limit=2, cursor=cursor)
        walked.extend(item["conversation_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "paging did not terminate"
    assert walked == whole
    assert len(set(walked)) == len(walked) == 4


def test_a_cursor_issued_before_the_ordering_change_is_refused(rt, session):
    """旧排序发出来的游标一律 422：新排序的键是 COALESCE(last_message_at, created_at)。

    旧游标里没写过消息的会话 `t` 是 null，写过的会话 `t` 只是 last_message_at —— 后者在新排序
    里同样定位不到同一处。宁可 422，也不要静默跳行或重复一整页。
    """

    service = conversations(rt)

    def legacy(payload: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    for payload in (
        {"p": False, "t": None, "c": "conv_x"},
        {"p": False, "t": "2026-09-30T00:00:00+00:00", "c": "conv_x"},
    ):
        with pytest.raises(AppError) as raised:
            service.list(session["user_id"], cursor=legacy(payload))

        assert raised.value.code == "INVALID_PARAMETER"
        assert raised.value.status == 422


def test_a_cursor_nobody_issued_is_a_bad_request_not_an_empty_page(rt, session):
    service = conversations(rt)

    with pytest.raises(AppError) as raised:
        service.list(session["user_id"], cursor="not-a-cursor")

    assert raised.value.code == "INVALID_PARAMETER"
    assert raised.value.status == 422


def test_a_legacy_session_resolves_to_one_conversation_on_its_old_thread(rt, session):
    service = conversations(rt)

    first = service.conversation_for_session(session["user_id"], session["session_id"])
    again = service.conversation_for_session(session["user_id"], session["session_id"])

    assert first.conversation_id == again.conversation_id
    assert first.thread_id == legacy_thread_id_for_session(session["session_id"])
    # The bridge must agree with the runtime it is bridging: two spellings of one thread id
    # would silently orphan every existing checkpoint.
    assert first.thread_id == thread_id_for_session(session["session_id"])


def test_the_legacy_bridge_refuses_another_users_session_thread(rt, session):
    service = conversations(rt)
    service.conversation_for_session(session["user_id"], session["session_id"])

    with pytest.raises(AppError) as raised:
        service.conversation_for_session(OTHER_USER, session["session_id"])

    assert raised.value.code == "NOT_FOUND"


def test_message_count_tracks_the_ledger(rt, session):
    service = conversations(rt)
    created = service.create(session["user_id"])
    say(service, session["user_id"], created.conversation_id, "一", "k1")
    service.append_assistant_message(
        session["user_id"], created.conversation_id, run_id=None, content="二"
    )

    assert service.get(session["user_id"], created.conversation_id).message_count == 2
    assert service.list(session["user_id"])["items"][0]["message_count"] == 2
