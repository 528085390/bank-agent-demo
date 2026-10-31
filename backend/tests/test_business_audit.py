"""The business audit trail: what one row records, and who may read it back.

A business event names a change this service made -- renaming, pinning, archiving a
conversation, editing the LLM configuration -- and the person who made it. Two properties
carry the weight: a row that cannot name its author is refused rather than stored, because
the reader can never see it again, and a caller can only ever read their own rows.
"""

import json
import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.runtime import EVENT_FIELDS
from app.audit.context import AuditContext
from app.audit.service import (
    BUSINESS_RESULT_OK,
    BUSINESS_RESULT_REJECTED,
    AuditService,
)
from app.conversation.service import ConversationService
from app.core.types import new_id
from app.integration.llm_config import LlmConfigManager
from app.main import create_app
from app.storage.models import AppUser, BusinessAuditEvent, LlmCallAudit
from tests.conftest import payload

OTHER_USER = "user_0002"

ACTION_ARCHIVE = "conversation.archive"
ACTION_PIN = "conversation.pin"


def client_for(rt, session):
    """The app under test, authenticated as `session`'s user."""

    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def conversation_for(rt, session, title=None) -> dict:
    return ConversationService(rt.database).create(session["user_id"], title).as_dict()


def another_user(rt) -> str:
    """A second real user, for the tests that read across the ownership boundary."""

    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id=OTHER_USER))
    return OTHER_USER


def seed_event(
    rt,
    user_id,
    *,
    action=ACTION_ARCHIVE,
    resource_type="conversation",
    resource_id=None,
    result=BUSINESS_RESULT_OK,
    reason=None,
    conversation_id=None,
    run_id=None,
    request_id=None,
    source=None,
) -> str:
    """One stored business event, written the way a route writes it."""

    return rt.audit.record_business_event(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        reason=reason,
        context=AuditContext(
            conversation_id=conversation_id,
            run_id=run_id,
            user_id=user_id,
            source=source,
            request_id=request_id,
        ),
    )


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- what one row records ---------------------------------------------------------------------


def test_a_business_event_round_trips_every_field(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "查余额")
    event_id = seed_event(
        rt,
        session["user_id"],
        action=ACTION_ARCHIVE,
        resource_type="conversation",
        resource_id=created["conversation_id"],
        result=BUSINESS_RESULT_REJECTED,
        reason="还有未完成的回合",
        conversation_id=created["conversation_id"],
        run_id="run_1",
        request_id="req_abc123",
    )

    page = get(client, "/api/audit/business-events", action=ACTION_ARCHIVE)

    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["event_id"] == event_id
    assert item["user_id"] == session["user_id"]
    assert item["conversation_id"] == created["conversation_id"]
    assert item["run_id"] == "run_1"
    assert item["request_id"] == "req_abc123"
    assert item["action"] == ACTION_ARCHIVE
    assert item["resource_type"] == "conversation"
    assert item["resource_id"] == created["conversation_id"]
    assert item["result"] == BUSINESS_RESULT_REJECTED
    assert item["reason"] == "还有未完成的回合"
    assert item["created_at"]
    # The internal primary key is not part of the published shape.
    assert "id" not in item


def test_the_event_id_is_minted_in_the_house_shape(rt, session):
    event_id = seed_event(rt, session["user_id"])

    assert event_id.startswith("baud_")
    assert len(event_id) == len("baud_") + 32
    assert all(character in "0123456789abcdef" for character in event_id[len("baud_") :])

    page = get(client_for(rt, session), "/api/audit/business-events")

    assert page["items"][0]["event_id"] == event_id
    assert page["items"][0]["created_at"]


# --- what the writer refuses ------------------------------------------------------------------


def test_an_unknown_result_is_refused_rather_than_stored(rt, session):
    """`SUCCEEDED` is the *model call* vocabulary; a business event is `ok`."""

    with pytest.raises(ValueError):
        seed_event(rt, session["user_id"], result="SUCCEEDED")

    assert get(client_for(rt, session), "/api/audit/business-events")["items"] == []


def test_a_business_event_without_a_user_is_refused(rt, session):
    """A row with `user_id IS NULL` would be invisible to every reader, so it never lands."""

    with pytest.raises(ValueError):
        rt.audit.record_business_event(
            action=ACTION_ARCHIVE,
            resource_type="conversation",
            context=AuditContext(),
        )

    assert get(client_for(rt, session), "/api/audit/business-events")["items"] == []


def test_a_business_event_with_no_context_at_all_is_refused(rt, session):
    """The ambient fallback is an empty context too: no turn means no author, not a free pass."""

    with pytest.raises(ValueError):
        rt.audit.record_business_event(action=ACTION_PIN, resource_type="conversation")

    assert get(client_for(rt, session), "/api/audit/business-events")["items"] == []


def test_a_caller_supplied_session_is_written_into_rather_than_around(rt, session):
    """A caller mid-transaction passes its own session, so the row lands with its other work."""

    with rt.database.sessions.begin() as db:
        event_id = rt.audit.record_business_event(
            action=ACTION_PIN,
            resource_type="conversation",
            db=db,
            context=AuditContext(user_id=session["user_id"]),
        )

    page = get(client_for(rt, session), "/api/audit/business-events")

    assert [item["event_id"] for item in page["items"]] == [event_id]


# --- who may read what ------------------------------------------------------------------------


def test_the_reader_only_returns_the_callers_own_events(rt, session):
    client = client_for(rt, session)
    mine = seed_event(rt, session["user_id"], action=ACTION_PIN)
    seed_event(rt, OTHER_USER, action=ACTION_ARCHIVE, resource_id="conv_theirs")

    page = get(client, "/api/audit/business-events")

    assert [item["event_id"] for item in page["items"]] == [mine]
    assert all(item["user_id"] == session["user_id"] for item in page["items"])


def test_another_users_conversation_is_indistinguishable_from_no_conversation(rt, session):
    """Otherwise the difference between an empty page and a 404 would enumerate ids."""

    client = client_for(rt, session)
    other = another_user(rt)
    theirs = ConversationService(rt.database).create(other, "别人的会话")
    seed_event(rt, other, conversation_id=theirs.conversation_id)

    response = client.get(
        "/api/audit/business-events", params={"conversation_id": theirs.conversation_id}
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- the read route ---------------------------------------------------------------------------


def test_the_route_pages_and_filters_by_action(rt, session):
    client = client_for(rt, session)
    archived = [seed_event(rt, session["user_id"], action=ACTION_ARCHIVE) for _ in range(3)]
    pinned = seed_event(rt, session["user_id"], action=ACTION_PIN)
    seed_event(rt, OTHER_USER, action=ACTION_ARCHIVE)

    # Rows are published oldest-first and tie-break on `event_id`, so which of two rows
    # written in the same clock tick comes first is not fixed. Membership is what matters
    # here; the walk below asserts the exact published order against itself.
    whole = [item["event_id"] for item in get(client, "/api/audit/business-events")["items"]]
    assert sorted(whole) == sorted(archived + [pinned])

    filtered = get(client, "/api/audit/business-events", action=ACTION_ARCHIVE)
    assert sorted(item["event_id"] for item in filtered["items"]) == sorted(archived)

    walked, cursor, pages = [], None, 0
    while True:
        page = get(
            client,
            "/api/audit/business-events",
            limit=2,
            **({"cursor": cursor} if cursor else {}),
        )
        walked.extend(item["event_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "paging did not terminate"

    assert walked == whole
    assert len(set(walked)) == len(walked) == 4


@pytest.mark.parametrize("limit", [0, -1, 5000])
def test_a_limit_outside_the_bound_is_refused(rt, session, limit):
    client = client_for(rt, session)

    response = client.get("/api/audit/business-events", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"


def test_a_cursor_nobody_issued_is_a_bad_request(rt, session):
    client = client_for(rt, session)

    response = client.get("/api/audit/business-events", params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"


def test_a_missing_session_is_refused(rt):
    client = TestClient(create_app(rt.settings, runtime=rt))

    response = client.get("/api/audit/business-events")

    assert response.status_code == 401


def test_the_writer_is_append_only(rt, session):
    """A correction is another row; there is no way to edit the one that is there."""

    assert not hasattr(AuditService, "update")
    assert not hasattr(AuditService, "delete")

    seed_event(rt, session["user_id"])
    item = get(client_for(rt, session), "/api/audit/business-events")["items"][0]

    # The published shape is exactly the audit columns, with no updated-at and no row id.
    assert set(item) == {
        "event_id",
        "user_id",
        "conversation_id",
        "run_id",
        "request_id",
        "action",
        "resource_type",
        "resource_id",
        "result",
        "reason",
        "created_at",
    }


# --- the conversation write points ------------------------------------------------------------
#
# The five changes `ConversationService` itself makes: rename, pin, archive, soft delete and
# restore. Each one is written into the *same* transaction as the change it records, so the
# pair commits or rolls back together, and each names the action rather than the content.

ACTION_RENAME = "conversation.rename"
ACTION_SOFT_DELETE = "conversation.soft_delete"
ACTION_RESTORE = "conversation.restore"

# The eleven columns a business audit row publishes.
AUDIT_FIELDS = {column.name for column in BusinessAuditEvent.__table__.columns} - {"id"}

# Three of those names are part of the conversation API's own vocabulary as well: the payload
# addresses a `conversation_id`, a message carries a `run_id`, and every row carries a
# `created_at`. Intersecting all eleven with a conversation response therefore matches on a
# correct implementation too, so the leak test uses the eight names that exist *only* on an
# audit row: if one of these appears in a conversation response, an audit row leaked into it.
AUDIT_ONLY_FIELDS = AUDIT_FIELDS - {"conversation_id", "created_at", "run_id"}


def events(client, **params) -> list[dict]:
    return get(client, "/api/audit/business-events", **params)["items"]


def keys_of(value):
    """Every dict key anywhere in a decoded response payload."""

    if isinstance(value, dict):
        yield from value
        for item in value.values():
            yield from keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_of(item)


def test_renaming_a_conversation_leaves_one_row_with_the_new_name_absent(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "旧标题")
    conversation_id = created["conversation_id"]

    response = client.patch(f"/api/conversations/{conversation_id}", json={"title": "改过的标题"})

    assert response.status_code == 200, response.text
    rows = events(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == ACTION_RENAME
    assert row["resource_type"] == "conversation"
    assert row["resource_id"] == conversation_id
    assert row["user_id"] == session["user_id"]
    assert row["conversation_id"] == conversation_id
    assert row["result"] == "ok"
    # The route establishes the request id, and it reaches the row through the ambient context.
    assert row["request_id"].startswith("req_")
    # The row names the change, never its content: neither the old nor the new title is in it.
    assert "改过的标题" not in json.dumps(row, ensure_ascii=False)
    assert "旧标题" not in json.dumps(row, ensure_ascii=False)


def test_a_patch_that_changes_nothing_writes_nothing(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "同")
    conversation_id = created["conversation_id"]

    response = client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "同", "pinned": False, "archived": False},
    )

    assert response.status_code == 200, response.text
    assert events(client) == []

    # A title that differs only in whitespace cleans to the value already stored, so it is not
    # a change either: the comparison is made on the cleaned title, not the raw one.
    spaced = client.patch(f"/api/conversations/{conversation_id}", json={"title": "  同  "})

    assert spaced.status_code == 200, spaced.text
    assert events(client) == []


def test_one_patch_writes_one_row_for_each_field_it_actually_changes(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "原标题")
    conversation_id = created["conversation_id"]

    response = client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "新标题", "pinned": True, "archived": True},
    )

    assert response.status_code == 200, response.text
    rows = events(client)
    assert sorted(row["action"] for row in rows) == sorted(
        [ACTION_RENAME, ACTION_PIN, ACTION_ARCHIVE]
    )
    assert all(row["conversation_id"] == conversation_id for row in rows)
    assert all(row["resource_id"] == conversation_id for row in rows)
    assert all(row["result"] == "ok" for row in rows)

    # The action filter narrows to one kind of change.
    renamed = events(client, action=ACTION_RENAME)
    assert [row["action"] for row in renamed] == [ACTION_RENAME]
    assert len(renamed) == 1

    # One field changed is one row, not three.
    single = client.patch(f"/api/conversations/{conversation_id}", json={"pinned": False})

    assert single.status_code == 200, single.text
    assert len(events(client)) == 4


def test_a_pin_change_is_its_own_action(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "置顶")
    conversation_id = created["conversation_id"]

    response = client.patch(f"/api/conversations/{conversation_id}", json={"pinned": True})

    assert response.status_code == 200, response.text
    rows = events(client)
    assert [row["action"] for row in rows] == [ACTION_PIN]
    assert rows[0]["resource_id"] == conversation_id


def test_archiving_and_restoring_are_two_rows(rt, session):
    """Un-archiving is a second `conversation.archive`, not a `restore`: restore is the
    soft-delete's inverse, and archiving never deleted anything."""

    client = client_for(rt, session)
    created = conversation_for(rt, session, "归档")
    conversation_id = created["conversation_id"]

    client.patch(f"/api/conversations/{conversation_id}", json={"archived": True})
    client.patch(f"/api/conversations/{conversation_id}", json={"archived": False})

    assert [row["action"] for row in events(client)] == [ACTION_ARCHIVE, ACTION_ARCHIVE]


def test_soft_delete_and_restore_are_two_rows(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "删除")
    conversation_id = created["conversation_id"]

    deleted = client.delete(f"/api/conversations/{conversation_id}")
    assert deleted.status_code == 200, deleted.text
    assert [row["action"] for row in events(client)] == [ACTION_SOFT_DELETE]

    restored = client.post(f"/api/conversations/{conversation_id}/restore")
    assert restored.status_code == 200, restored.text
    rows = events(client)
    assert [row["action"] for row in rows] == [ACTION_SOFT_DELETE, ACTION_RESTORE]
    assert all(row["resource_id"] == conversation_id for row in rows)


def test_bulk_soft_delete_writes_one_audit_row_per_conversation(rt, session):
    """A batch wipe leaves the same per-row trail as deleting each conversation by hand."""

    client = client_for(rt, session)
    first = conversation_for(rt, session, "一")["conversation_id"]
    second = conversation_for(rt, session, "二")["conversation_id"]

    response = client.post(
        "/api/conversations/bulk-delete", json={"ids": [first, second]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted_count"] == 2

    rows = events(client)
    assert [row["action"] for row in rows] == [ACTION_SOFT_DELETE, ACTION_SOFT_DELETE]
    assert {row["resource_id"] for row in rows} == {first, second}


def test_restoring_does_not_rewrite_the_history_rows(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "历史")
    conversation_id = created["conversation_id"]
    client.patch(f"/api/conversations/{conversation_id}", json={"title": "改过"})

    client.delete(f"/api/conversations/{conversation_id}")
    # A soft delete hides the conversation from the ordinary list, not its audit trail.
    before = events(client, conversation_id=conversation_id)
    assert [row["action"] for row in before] == [ACTION_RENAME, ACTION_SOFT_DELETE]
    ids_before = [row["event_id"] for row in before]
    times_before = [row["created_at"] for row in before]

    client.post(f"/api/conversations/{conversation_id}/restore")

    after = events(client, conversation_id=conversation_id)
    assert len(after) == len(before) + 1
    assert [row["action"] for row in after] == [
        ACTION_RENAME,
        ACTION_SOFT_DELETE,
        ACTION_RESTORE,
    ]
    # The history is neither replayed nor rewritten: the same ids in the same order, with the
    # same timestamps. Comparing only the count would let a re-write of the old rows pass.
    assert [row["event_id"] for row in after][: len(ids_before)] == ids_before
    assert [row["created_at"] for row in after][: len(times_before)] == times_before
    assert set(ids_before) <= {row["event_id"] for row in after}


def test_a_refused_rename_writes_nothing(rt, session):
    client = client_for(rt, session)
    other = another_user(rt)
    theirs = ConversationService(rt.database).create(other, "别人的会话")

    response = client.patch(
        f"/api/conversations/{theirs.conversation_id}", json={"title": "越权改名"}
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert events(client) == []


def test_messaging_an_archived_conversation_writes_nothing(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "归档")
    conversation_id = created["conversation_id"]
    client.patch(f"/api/conversations/{conversation_id}", json={"archived": True})
    before = events(client)
    assert [row["action"] for row in before] == [ACTION_ARCHIVE]

    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"message": "还在吗"},
        headers={"Idempotency-Key": f"test-{uuid4().hex}"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_WRITABLE"
    assert events(client) == before


def overlong_event_id(monkeypatch) -> None:
    """Make the audit writer mint an id longer than its column.

    The insert then fails inside Postgres at `flush()`, which is the real database refusing
    the row rather than a stand-in for it.
    """

    monkeypatch.setattr("app.audit.service.new_id", lambda prefix: prefix + "_" + "x" * 200)


def test_a_failed_audit_write_rolls_the_conversation_change_back(rt, session, monkeypatch, caplog):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "原标题")
    conversation_id = created["conversation_id"]
    overlong_event_id(monkeypatch)

    with caplog.at_level(logging.ERROR):
        response = client.patch(
            f"/api/conversations/{conversation_id}", json={"title": "改过的标题"}
        )

    # The endpoint fails rather than reporting a change it did not record.
    assert response.status_code >= 500, response.text
    assert events(client) == []
    # And the change it would have recorded rolled back with the row.
    reread = client.get(f"/api/conversations/{conversation_id}").json()["data"]["conversation"]
    assert reread["title"] == "原标题"

    failures = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("business_audit_write_failed")
    ]
    assert len(failures) == 1
    assert "action=conversation.rename" in failures[0]
    assert f"resource_id={conversation_id}" in failures[0]
    # The line carries ids only -- no exception text, which can quote the row's values.
    assert "Traceback" not in failures[0]
    assert "StringDataRightTruncation" not in failures[0]
    assert "value too long" not in failures[0]


def test_a_failed_audit_write_rolls_back_the_soft_delete(rt, session, monkeypatch):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "删除")
    conversation_id = created["conversation_id"]
    overlong_event_id(monkeypatch)

    response = client.delete(f"/api/conversations/{conversation_id}")

    assert response.status_code >= 500, response.text
    assert events(client) == []
    reread = client.get(f"/api/conversations/{conversation_id}").json()["data"]["conversation"]
    assert reread["deleted"] is False


def test_a_failed_audit_write_rolls_back_the_restore(rt, session, monkeypatch):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "恢复")
    conversation_id = created["conversation_id"]
    # Soft-deleted through the service, so the setup itself writes no audit row to compare to.
    ConversationService(rt.database).soft_delete(session["user_id"], conversation_id)
    overlong_event_id(monkeypatch)

    response = client.post(f"/api/conversations/{conversation_id}/restore")

    assert response.status_code >= 500, response.text
    assert events(client) == []
    reread = client.get(f"/api/conversations/{conversation_id}").json()["data"]["conversation"]
    assert reread["deleted"] is True


def test_the_conversation_api_never_returns_audit_fields(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "查余额")
    conversation_id = created["conversation_id"]
    # Real ledger rows, appended through the service layer. Without them the message page is
    # empty, `MessageView.as_dict()` is never serialized, and the message half of this check
    # would pass over nothing at all.
    user_id = session["user_id"]
    rt.conversations.append_user_message(
        user_id,
        conversation_id,
        idempotency_key="leak-check-1",
        expected_hash="leak-check-hash-1",
        content="查余额",
    )
    rt.conversations.append_assistant_message(
        user_id, conversation_id, run_id=None, content="余额是 100 元"
    )
    client.patch(f"/api/conversations/{conversation_id}", json={"title": "改过", "pinned": True})
    client.delete(f"/api/conversations/{conversation_id}")
    client.post(f"/api/conversations/{conversation_id}/restore")

    read = client.get(f"/api/conversations/{conversation_id}").json()["data"]
    page = client.get(f"/api/conversations/{conversation_id}/messages").json()["data"]
    # The check is only worth anything if both payloads were really serialized.
    assert page["items"], "the leak check must run over a real message row"

    # The audit trail is its own surface: none of the eight names that only an audit row
    # carries may appear anywhere in an ordinary conversation response.
    assert set(keys_of(read)) & AUDIT_ONLY_FIELDS == set()
    assert set(keys_of(page)) & AUDIT_ONLY_FIELDS == set()
    # No old/new snapshot travels with a change either.
    snapshot = {"old", "new", "before", "after", "snapshot"}
    assert not (set(keys_of(read)) | set(keys_of(page))) & snapshot

    # The streamed event vocabulary is a closed set, and it has no room for one of these.
    streamed = {field for fields in EVENT_FIELDS.values() for field in fields}
    assert not streamed & AUDIT_FIELDS
    assert not streamed & snapshot


# --- the LLM configuration's own write points ------------------------------------------------
#
# Installing or clearing the model changes memory and nothing else: there is no stored row to
# commit the change alongside. The trail is kept by *ordering* instead -- the write runs in its
# own transaction before the swap, and its failure is allowed to propagate -- so that is what
# these tests pin down.

ACTION_LLM_UPDATE = "llm_config.update"
ACTION_LLM_CLEAR = "llm_config.clear"

RESOURCE_LLM_CONFIG = "llm_config"


def settings_client_for(rt, session):
    """`client_for`, plus the settings manager the panel's routes need.

    Those routes answer 503 while `runtime.llm` is None, so the manager is installed before
    the app is built. The conversation tests need none of this.
    """

    rt.llm = LlmConfigManager(rt)
    return client_for(rt, session)


def llm_calls(rt) -> list[LlmCallAudit]:
    """Every recorded model call, oldest first."""

    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def overlong_business_event_id(monkeypatch) -> None:
    """`overlong_event_id`, narrowed to the *business* writer.

    Installing a model probes it first, and the probe is itself an audited model call. Making
    every minted id too long would fail that call's row inside the probe, where
    `_build_and_probe` turns the failure into a 502 before the endpoint ever reaches the
    business write under test. Only the business id is made too long here, so the row that
    Postgres refuses is the one this test is about.
    """

    real = new_id

    def minted(prefix: str) -> str:
        if prefix == "baud":
            return prefix + "_" + "x" * 200
        return real(prefix)

    monkeypatch.setattr("app.audit.service.new_id", minted)


def test_installing_a_model_leaves_one_ok_row(rt, session, provider):
    client = settings_client_for(rt, session)

    response = client.put("/api/settings/llm", json=payload(provider.server_port))

    assert response.status_code == 200, response.text
    rows = events(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == ACTION_LLM_UPDATE
    assert row["resource_type"] == RESOURCE_LLM_CONFIG
    assert row["resource_id"] == RESOURCE_LLM_CONFIG
    assert row["user_id"] == session["user_id"]
    assert row["result"] == BUSINESS_RESULT_OK
    assert row["request_id"].startswith("req_")

    # The row names the change, never its content: no endpoint, no model name, no secret.
    published = json.dumps(row, ensure_ascii=False)
    assert "mock-model" not in published
    assert "sk-local-protocol" not in published
    assert "127.0.0.1" not in published

    # The probe really was a model call, and it is recorded as one -- in the other table,
    # under its own id. One change is one business row, not one row per table it touched.
    calls = llm_calls(rt)
    assert len(calls) == 1
    assert calls[0].source == "configuration_probe"
    assert calls[0].user_id == session["user_id"]
    assert row["event_id"].startswith("baud_")
    assert calls[0].audit_id.startswith("aud_")
    assert row["event_id"] != calls[0].audit_id


def test_a_probe_that_fails_leaves_no_row(rt, session, provider):
    """A candidate that cannot answer never becomes a change to record.

    The probe runs first, so a failure there leaves the trail exactly as it was -- and the
    installed configuration too.
    """

    client = settings_client_for(rt, session)

    # Nothing installed yet: a refused probe writes nothing at all.
    refused = client.put("/api/settings/llm", json=payload(1))

    assert refused.status_code == 502, refused.text
    assert refused.json()["error"]["code"] == "LLM_CONNECTION_FAILED"
    assert events(client) == []

    # With a model already installed, a refused probe adds no row and changes nothing.
    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200
    before = client.get("/api/settings/llm").json()["data"]
    installed = events(client)
    assert len(installed) == 1

    refused = client.put("/api/settings/llm", json=payload(1, model="unreachable-model"))

    assert refused.status_code == 502, refused.text
    assert events(client) == installed
    assert client.get("/api/settings/llm").json()["data"] == before


def test_a_failed_audit_write_leaves_the_config_untouched(
    rt, session, provider, monkeypatch, caplog
):
    """A change that cannot be recorded is not made.

    The write is its own transaction and the hook runs before the swap, so the only thing
    keeping the two in step is the exception travelling out of `configure` before the swap.
    """

    client = settings_client_for(rt, session)
    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200
    before = client.get("/api/settings/llm").json()["data"]
    recorded = events(client)
    assert len(recorded) == 1
    overlong_business_event_id(monkeypatch)

    with caplog.at_level(logging.ERROR):
        response = client.put(
            "/api/settings/llm", json=payload(provider.server_port, model="second-model")
        )

    # The endpoint fails rather than reporting a change it did not record.
    assert response.status_code >= 500, response.text
    # And the model is still the one the last recorded change installed.
    assert client.get("/api/settings/llm").json()["data"] == before
    assert rt.agent.model_configured is True
    assert events(client) == recorded

    failures = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.audit.service"
        and record.getMessage().startswith("business_audit_write_failed")
    ]
    assert len(failures) == 1
    assert "action=llm_config.update" in failures[0]
    assert "resource_id=llm_config" in failures[0]
    assert "event_id=baud_" in failures[0]
    # The line carries ids only -- no exception text, which can quote the row's values.
    assert "Traceback" not in failures[0]
    assert "StringDataRightTruncation" not in failures[0]
    assert "value too long" not in failures[0]


def test_clearing_the_model_is_recorded_and_is_its_own_action(rt, session, provider):
    client = settings_client_for(rt, session)
    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200

    cleared = client.delete("/api/settings/llm")

    assert cleared.status_code == 200, cleared.text
    rows = events(client)
    # Two changes, two rows, and the action says which change each one was.
    assert [row["action"] for row in rows] == [ACTION_LLM_UPDATE, ACTION_LLM_CLEAR]
    row = rows[-1]
    assert row["resource_type"] == RESOURCE_LLM_CONFIG
    assert row["resource_id"] == RESOURCE_LLM_CONFIG
    assert row["user_id"] == session["user_id"]
    assert row["result"] == BUSINESS_RESULT_OK
    assert row["request_id"].startswith("req_")


def test_testing_a_connection_writes_no_business_row(rt, session, provider):
    """`POST /settings/llm/test` changes nothing, so there is no change to record."""

    client = settings_client_for(rt, session)

    response = client.post("/api/settings/llm/test", json=payload(provider.server_port))

    assert response.status_code == 200, response.text
    assert events(client) == []
    # The probe is still a model call, and it is still recorded as one.
    calls = llm_calls(rt)
    assert len(calls) == 1
    assert calls[0].source == "configuration_test"


def test_the_settings_panel_payload_never_carries_audit_fields(rt, session):
    """The panel reads the live configuration, not the trail that records changing it."""

    client = settings_client_for(rt, session)

    snapshot = client.get("/api/settings/llm").json()["data"]

    # The check is only worth something over a payload that really was serialized.
    assert set(snapshot) == {
        "configured",
        "mode",
        "base_url",
        "model",
        "response_format",
        "timeout_seconds",
        "api_key_masked",
    }
    assert set(keys_of(snapshot)) & AUDIT_FIELDS == set()


def test_a_failed_audit_write_leaves_the_cleared_config_in_place(
    rt, session, provider, monkeypatch, caplog
):
    client = settings_client_for(rt, session)
    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200
    before = client.get("/api/settings/llm").json()["data"]
    recorded = events(client)
    assert len(recorded) == 1
    overlong_event_id(monkeypatch)

    with caplog.at_level(logging.ERROR):
        response = client.delete("/api/settings/llm")

    assert response.status_code >= 500, response.text
    # The model is still installed: the clear that could not be recorded did not happen.
    assert client.get("/api/settings/llm").json()["data"] == before
    assert rt.agent.model_configured is True
    assert events(client) == recorded

    failures = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.audit.service"
        and record.getMessage().startswith("business_audit_write_failed")
    ]
    assert len(failures) == 1
    assert "action=llm_config.clear" in failures[0]
    assert "resource_id=llm_config" in failures[0]
    assert "event_id=baud_" in failures[0]
    # The line carries ids only -- no exception text, which can quote the row's values.
    assert "Traceback" not in failures[0]
    assert "StringDataRightTruncation" not in failures[0]
    assert "value too long" not in failures[0]


def test_a_refused_audit_write_closes_the_candidate_client(rt, session, monkeypatch):
    """A hook that fails must not strand the client the probe just built.

    This is the one test allowed to replace `_build_and_probe` (R62 exempts it): "was the
    candidate closed" can only be observed by handing `configure` a client of our own, and
    every acceptance case above still goes through the real SDK path. The hook raises before
    the lock, so nothing is installed either -- the same fail-closed shape, one layer further
    in. Without the `try`/`except` in `configure` the stand-in's `close()` is never called.
    """

    closed: list[bool] = []

    class RecordingClient:
        def close(self) -> None:
            closed.append(True)

    panel = settings_client_for(rt, session)
    monkeypatch.setattr(
        LlmConfigManager,
        "_build_and_probe",
        lambda self, config, *, source: RecordingClient(),
    )

    def refuse() -> None:
        raise RuntimeError("audit write refused")

    with pytest.raises(RuntimeError, match="audit write refused"):
        rt.llm.configure(payload(1), on_change=refuse)

    assert closed == [True]
    assert panel.get("/api/settings/llm").json()["data"]["mode"] == "unconfigured"
