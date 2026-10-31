"""The audit query surface: what it returns, and who is allowed to see it.

The audit trail holds the complete prompt, the complete provider response and the complete
tool payload. This file pins the two things that make publishing it safe: a caller can only
ever read their own records, and the ordinary conversation surface still returns none of it.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.audit.context import AuditContext
from app.audit.service import RESULT_FAILED, RESULT_SUCCEEDED, RunEventType
from app.conversation.service import ConversationService
from app.main import create_app
from app.storage.models import AppUser

OTHER_USER = "user_0002"


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def conversation_for(rt, session, title=None) -> dict:
    return ConversationService(rt.database).create(session["user_id"], title).as_dict()


def seed_call(rt, user_id, *, conversation_id=None, run_id=None, call_id=None, source=None):
    """One stored model call, written the way the boundary writes it."""

    call_id = call_id or f"call_{uuid4().hex}"
    rt.audit.record_llm_call(
        call_id=call_id,
        request={
            "messages": [
                {"role": "system", "content": "只输出一个 JSON 对象。"},
                {"role": "user", "content": '请输出 {"ok":true}'},
            ],
            "parameters": {"model": "mock-model"},
        },
        response={"id": "chatcmpl_1", "choices": [{"message": {"content": '{"ok":true}'}}]},
        response_text='{"ok":true}',
        result=RESULT_SUCCEEDED,
        context=AuditContext(
            conversation_id=conversation_id, run_id=run_id, user_id=user_id, source=source
        ),
        latency_ms=7,
        input_tokens=11,
        output_tokens=3,
        provider="openai",
        model="mock-model",
    )
    return call_id


def seed_tool_event(rt, user_id, *, conversation_id=None, run_id=None, call_id="tool_1"):
    rt.audit.record_agent_event(
        event_type=RunEventType.TOOL_RESULT,
        status="SUCCEEDED",
        agent_id="card_agent",
        tool_name="card_list",
        call_id=call_id,
        context=AuditContext(conversation_id=conversation_id, run_id=run_id, user_id=user_id),
        payload={
            "agent_id": "card_agent",
            "tool": "card_list",
            "call_id": call_id,
            "arguments": {"account_id": "acc_1"},
            "result": {
                "tool_name": "card_list",
                "status": "SUCCEEDED",
                "data": {
                    "summary": "共两张卡",
                    "facts": {"cards": [{"card_id": "card_1"}]},
                },
            },
        },
    )


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- what a permitted caller can reconstruct ------------------------------------------------


def test_an_authorized_caller_can_reconstruct_the_whole_model_interaction(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "查余额")
    call_id = seed_call(
        rt, session["user_id"], conversation_id=created["conversation_id"], run_id="run_1"
    )

    page = get(client, "/api/audit/llm-calls", call_id=call_id)

    assert len(page["items"]) == 1
    item = page["items"][0]
    # The whole request, message by message.
    assert [message["role"] for message in item["request"]["messages"]] == ["system", "user"]
    assert item["request"]["parameters"] == {"model": "mock-model"}
    # The whole provider response, and the normalized text beside it.
    assert item["response"]["choices"][0]["message"]["content"] == '{"ok":true}'
    assert item["response_text"] == '{"ok":true}'
    assert (item["model"], item["provider"]) == ("mock-model", "openai")
    assert (item["latency_ms"], item["input_tokens"], item["output_tokens"]) == (7, 11, 3)
    assert item["result"] == RESULT_SUCCEEDED


def test_the_complete_tool_payload_is_readable_including_facts(rt, session):
    """`facts` is exactly what the SSE frame drops, so this is what the endpoint is for."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    seed_tool_event(rt, session["user_id"], conversation_id=created["conversation_id"], run_id="r")

    page = get(client, "/api/audit/run-events", event_type="TOOL_RESULT")

    assert len(page["items"]) == 1
    payload = page["items"][0]["payload"]
    assert payload["arguments"] == {"account_id": "acc_1"}
    assert payload["result"]["data"]["facts"] == {"cards": [{"card_id": "card_1"}]}
    assert page["items"][0]["tool_name"] == "card_list"


def test_a_failed_call_is_readable_with_its_reason(rt, session):
    client = client_for(rt, session)
    rt.audit.record_llm_call(
        call_id="call_failed",
        request={"messages": [{"role": "user", "content": "查余额"}]},
        result=RESULT_FAILED,
        error_code="LLM_TIMEOUT",
        context=AuditContext(user_id=session["user_id"], source="configuration_test"),
    )

    item = get(client, "/api/audit/llm-calls", call_id="call_failed")["items"][0]

    assert item["result"] == RESULT_FAILED
    assert item["error_code"] == "LLM_TIMEOUT"
    assert item["request"]["messages"] == [{"role": "user", "content": "查余额"}]
    assert item["response"] is None and item["response_text"] is None


def test_a_probe_with_no_conversation_is_readable(rt, session):
    """The configuration probe belongs to no conversation and is still the caller's record."""

    client = client_for(rt, session)
    seed_call(rt, session["user_id"], source="configuration_test")

    page = get(client, "/api/audit/llm-calls", source=None)

    assert len(page["items"]) == 1
    item = page["items"][0]
    assert (item["conversation_id"], item["run_id"]) == (None, None)
    assert item["source"] == "configuration_test"


def test_no_credential_is_ever_in_the_response(rt, session):
    """The API key is never stored, so it cannot be returned -- asserted on the wire anyway."""

    client = client_for(rt, session)
    seed_call(rt, session["user_id"])

    body = client.get("/api/audit/llm-calls").text

    assert "api_key" not in body
    assert "Authorization" not in body
    assert "sk-" not in body


# --- who may read what -----------------------------------------------------------------------


def test_another_users_audit_is_invisible(rt, session):
    client = client_for(rt, session)
    seed_call(rt, OTHER_USER, conversation_id="conv_someone_else", run_id="run_theirs")
    seed_tool_event(rt, OTHER_USER, conversation_id="conv_someone_else")

    assert get(client, "/api/audit/llm-calls")["items"] == []
    assert get(client, "/api/audit/run-events")["items"] == []
    # Naming their run explicitly does not help: the owner filter is unconditional.
    assert get(client, "/api/audit/llm-calls", run_id="run_theirs")["items"] == []


def test_querying_another_users_conversation_is_a_not_found(rt, session):
    """Otherwise the difference between an empty page and a 404 would enumerate ids."""

    client = client_for(rt, session)
    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id=OTHER_USER))
    other = ConversationService(rt.database).create(OTHER_USER, "别人的会话")
    seed_call(rt, OTHER_USER, conversation_id=other.conversation_id)

    for path in ("/api/audit/llm-calls", "/api/audit/run-events"):
        response = client.get(path, params={"conversation_id": other.conversation_id})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_missing_session_is_refused(rt):
    client = TestClient(create_app(rt.settings, runtime=rt))

    response = client.get("/api/audit/llm-calls")

    assert response.status_code == 401


# --- filtering and paging --------------------------------------------------------------------


def test_a_conversation_filter_narrows_to_that_conversation(rt, session):
    client = client_for(rt, session)
    mine = conversation_for(rt, session, "我的")
    seed_call(rt, session["user_id"], conversation_id=mine["conversation_id"], run_id="run_a")
    seed_call(rt, session["user_id"], conversation_id=None, run_id="run_b")

    page = get(client, "/api/audit/llm-calls", conversation_id=mine["conversation_id"])

    assert len(page["items"]) == 1
    assert page["items"][0]["conversation_id"] == mine["conversation_id"]


def test_a_run_filter_narrows_to_that_run(rt, session):
    client = client_for(rt, session)
    seed_call(rt, session["user_id"], run_id="run_a")
    seed_call(rt, session["user_id"], run_id="run_a")
    seed_call(rt, session["user_id"], run_id="run_b")

    page = get(client, "/api/audit/llm-calls", run_id="run_a")

    assert len(page["items"]) == 2
    assert {item["run_id"] for item in page["items"]} == {"run_a"}


def test_a_time_window_is_inclusive_at_both_ends(rt, session):
    client = client_for(rt, session)
    seed_call(rt, session["user_id"])
    page = get(client, "/api/audit/llm-calls")
    created = page["items"][0]["created_at"]

    assert len(get(client, "/api/audit/llm-calls", since=created)["items"]) == 1
    assert len(get(client, "/api/audit/llm-calls", until=created)["items"]) == 1
    later = (datetime.fromisoformat(created) + timedelta(seconds=1)).isoformat()
    assert get(client, "/api/audit/llm-calls", since=later)["items"] == []


def test_paging_walks_the_whole_trail_exactly_once(rt, session):
    client = client_for(rt, session)
    for _ in range(5):
        seed_call(rt, session["user_id"])

    whole = [item["call_id"] for item in get(client, "/api/audit/llm-calls", limit=10)["items"]]
    walked, cursor, pages = [], None, 0
    while True:
        page = get(
            client, "/api/audit/llm-calls", limit=2, **({"cursor": cursor} if cursor else {})
        )
        walked.extend(item["call_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "paging did not terminate"

    assert walked == whole
    assert len(set(walked)) == len(walked) == 5


def test_a_cursor_nobody_issued_is_a_bad_request(rt, session):
    client = client_for(rt, session)

    response = client.get("/api/audit/llm-calls", params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.parametrize("limit", [0, -1, 5000])
def test_a_limit_outside_the_bound_is_refused(rt, session, limit):
    client = client_for(rt, session)

    response = client.get("/api/audit/llm-calls", params={"limit": limit})

    assert response.status_code == 422


def test_run_events_can_be_filtered_by_type(rt, session):
    client = client_for(rt, session)
    seed_tool_event(rt, session["user_id"], run_id="r")
    rt.audit.record_agent_event(
        event_type=RunEventType.RUN_STARTED,
        context=AuditContext(user_id=session["user_id"], run_id="r"),
    )

    page = get(client, "/api/audit/run-events", event_type="RUN_STARTED")

    assert [item["event_type"] for item in page["items"]] == ["RUN_STARTED"]


# --- the ordinary surface stays ordinary ------------------------------------------------------


def test_the_conversation_message_api_returns_no_audit_material(rt, session):
    """The ledger is what the user saw; the audit trail is not reachable from it."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    seed_call(rt, session["user_id"], conversation_id=created["conversation_id"])
    seed_tool_event(rt, session["user_id"], conversation_id=created["conversation_id"])

    body = client.get(f"/api/conversations/{created['conversation_id']}/messages").text

    assert body.strip()
    assert "只输出一个 JSON 对象" not in body
    assert "facts" not in body
    assert "chatcmpl_1" not in body
