"""The append-only audit store: what one record actually holds, and what it refuses.

The point of these tests is completeness and immutability. An audit trail is only worth
keeping if a record answers "what exactly was sent, and what exactly came back" -- including
for the calls that failed and for the ones no conversation asked for -- and if nothing in the
service can rewrite one afterwards. Both are pinned here against the real database, because
"the payload survived the round trip through JSONB" is a property of the column type.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.audit.context import AuditContext, audit_context
from app.audit.service import (
    RESULT_FAILED,
    RESULT_SUCCEEDED,
    AuditService,
    RunEventType,
    jsonable,
)
from app.storage.models import AgentRunEvent, LlmCallAudit

CONVERSATION = "conv_audit_fixture"
RUN = "run_audit_fixture"
USER = "user_0001"


def audit(rt) -> AuditService:
    return AuditService(rt.database)


def stored_calls(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def stored_events(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(AgentRunEvent).order_by(AgentRunEvent.created_at)))


def test_a_successful_call_keeps_the_whole_request_and_the_whole_answer(rt):
    service = audit(rt)
    messages = [
        {"role": "system", "content": "只输出一个 JSON 对象。"},
        {"role": "user", "content": '请输出 {"ok":true}'},
    ]

    audit_id = service.record_llm_call(
        call_id="call_success",
        request={"messages": messages, "parameters": {"model": "mock-model"}},
        response={"id": "chatcmpl_1", "choices": [{"message": {"content": '{"ok":true}'}}]},
        response_text='{"ok":true}',
        result=RESULT_SUCCEEDED,
        context=AuditContext(
            conversation_id=CONVERSATION, run_id=RUN, user_id=USER, source="conversation"
        ),
        latency_ms=12,
        input_tokens=31,
        output_tokens=7,
        provider="chat_completions",
        model="mock-model",
    )

    row = stored_calls(rt)[0]
    assert row.audit_id == audit_id
    assert row.prompt_json["messages"] == messages, "the prompt is stored verbatim"
    assert row.response_json["choices"][0]["message"]["content"] == '{"ok":true}'
    assert row.response_text == '{"ok":true}'
    assert (row.result, row.error_code) == (RESULT_SUCCEEDED, None)
    assert (row.latency_ms, row.input_tokens, row.output_tokens) == (12, 31, 7)
    assert (row.conversation_id, row.run_id, row.user_id) == (CONVERSATION, RUN, USER)
    assert row.source == "conversation"


def test_a_failed_call_keeps_the_request_and_a_stable_reason(rt):
    """The request is the only evidence of what was being attempted when it failed."""

    service = audit(rt)

    service.record_llm_call(
        call_id="call_failed",
        request={"messages": [{"role": "user", "content": "查余额"}]},
        result=RESULT_FAILED,
        error_code="LLM_TIMEOUT",
        latency_ms=30000,
        context=AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER),
    )

    row = stored_calls(rt)[0]
    assert row.result == RESULT_FAILED
    assert row.error_code == "LLM_TIMEOUT"
    assert row.prompt_json["messages"] == [{"role": "user", "content": "查余额"}]
    assert row.response_json is None and row.response_text is None
    # A missing count is a fact, not a zero: the provider never reported one.
    assert (row.input_tokens, row.output_tokens) == (None, None)


def test_a_tool_event_keeps_the_full_arguments_and_the_full_result(rt):
    """`facts` is projected away from SSE, and kept here in full. That is the whole point."""

    service = audit(rt)
    result = {
        "tool_name": "card_list",
        "status": "SUCCEEDED",
        "call_id": "tool_call_1",
        "data": {
            "summary": "共两张卡",
            "display": {"cards": 2},
            "business_ids": {"card_id": "card_1"},
            "facts": {"cards": [{"card_id": "card_1", "limit": "5000.00"}]},
        },
        "error": None,
        "retryable": False,
        "error_code": None,
    }

    event_id = service.record_agent_event(
        event_type=RunEventType.TOOL_RESULT,
        payload=result,
        status="SUCCEEDED",
        agent_id="card_agent",
        tool_name="card_list",
        call_id="tool_call_1",
        context=AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER),
    )

    row = stored_events(rt)[0]
    assert row.event_id == event_id
    assert row.event_type == "TOOL_RESULT"
    assert row.payload_json["data"]["facts"]["cards"][0]["card_id"] == "card_1"
    assert row.payload_json["data"]["business_ids"] == {"card_id": "card_1"}


def test_one_conversation_and_run_append_many_records(rt):
    """Nothing is overwritten: a retry is another row, and so is the next event."""

    service = audit(rt)
    context = AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER)
    for attempt in ("call_1", "call_2"):
        service.record_llm_call(
            call_id=attempt,
            request={"messages": []},
            result=RESULT_FAILED,
            error_code="LLM_TIMEOUT",
            context=context,
        )
    service.record_agent_event(event_type=RunEventType.RUN_STARTED, context=context)
    service.record_agent_event(event_type=RunEventType.RUN_FAILED, context=context)

    assert [row.call_id for row in stored_calls(rt)] == ["call_1", "call_2"]
    assert [row.event_type for row in stored_events(rt)] == ["RUN_STARTED", "RUN_FAILED"]
    with rt.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LlmCallAudit)) == 2


def test_a_call_with_no_conversation_is_still_recorded(rt):
    """A configuration probe belongs to no conversation, and is recorded anyway."""

    service = audit(rt)

    service.record_llm_call(
        call_id="call_probe",
        request={"messages": [{"role": "user", "content": "probe"}]},
        response={"choices": [{"message": {"content": "ok"}}]},
        response_text="ok",
        result=RESULT_SUCCEEDED,
        context=AuditContext(source="configuration_probe"),
    )

    row = stored_calls(rt)[0]
    assert row.conversation_id is None and row.run_id is None
    assert row.source == "configuration_probe"


def test_a_record_with_no_context_at_all_is_recorded_as_unrelated(rt):
    """A boundary that knows nothing must not borrow the last turn's ids."""

    service = audit(rt)

    service.record_agent_event(event_type=RunEventType.RUN_STARTED)

    row = stored_events(rt)[0]
    assert (row.conversation_id, row.run_id, row.user_id, row.source) == (None, None, None, None)


def test_a_record_written_inside_a_context_inherits_it(rt):
    """This is how the Gateway writes a tool event without being handed anything."""

    service = audit(rt)

    with audit_context(
        AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER, source="conversation")
    ):
        service.record_agent_event(event_type=RunEventType.TOOL_CALL, tool_name="card_list")

    row = stored_events(rt)[0]
    assert (row.conversation_id, row.run_id, row.user_id) == (CONVERSATION, RUN, USER)
    assert row.tool_name == "card_list"


def test_call_ids_and_audit_ids_are_unique(rt):
    """The database is the last word on it: a duplicate call id is refused, not merged."""

    service = audit(rt)
    service.record_llm_call(call_id="call_once", request={}, result=RESULT_SUCCEEDED)

    with pytest.raises(Exception):
        service.record_llm_call(call_id="call_once", request={}, result=RESULT_SUCCEEDED)

    assert len(stored_calls(rt)) == 1


def test_the_service_can_only_append(rt):
    """An audit record that can be edited is not evidence."""

    service = audit(rt)

    for forbidden in ("update", "delete", "remove", "edit", "replace", "rewrite"):
        assert not hasattr(service, forbidden), forbidden


def test_an_unknown_result_is_refused(rt):
    with pytest.raises(ValueError):
        audit(rt).record_llm_call(call_id="call_x", request={}, result="MAYBE")


class Colour(Enum):
    RED = "red"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


def test_the_serializer_converts_what_the_boundaries_actually_pass():
    moment = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    identifier = uuid4()

    converted = jsonable(
        {
            "nothing": None,
            "enum": Colour.RED,
            "money": Decimal("100.00"),
            "uuid": identifier,
            "when": moment,
            "dataclass": Money(Decimal("1.5"), "CNY"),
            "nested": [{"amount": Decimal("0.10")}],
        }
    )

    assert converted == {
        "nothing": None,
        "enum": "red",
        "money": "100.00",
        "uuid": str(identifier),
        "when": "2026-09-28T12:00:00+00:00",
        "dataclass": {"amount": "1.5", "currency": "CNY"},
        "nested": [{"amount": "0.10"}],
    }


def test_a_naive_timestamp_is_stored_as_utc():
    assert jsonable(datetime(2026, 9, 28, 12, 0)) == "2026-09-28T12:00:00+00:00"


def test_the_serializer_refuses_what_it_cannot_store():
    """A `repr` fallback would look complete and be useless. It fails instead."""

    class Opaque:
        def __repr__(self) -> str:  # pragma: no cover - the point is that it is never used
            return "<Opaque object at 0x0>"

    with pytest.raises(TypeError) as raised:
        jsonable({"client": Opaque()})

    assert "Opaque" in str(raised.value)
    with pytest.raises(TypeError):
        jsonable(object())


def test_a_record_the_database_refuses_leaves_nothing_behind(rt):
    """An audit write is not best-effort: a failed insert must not be silently dropped."""

    service = audit(rt)
    with pytest.raises(TypeError):
        service.record_llm_call(
            call_id="call_bad", request={"bad": object()}, result=RESULT_SUCCEEDED
        )

    assert stored_calls(rt) == []


def test_a_tool_event_carries_the_trusted_context_and_idempotency_key(rt):
    """The ids the server injected are part of what the call actually was."""

    service = audit(rt)
    service.record_agent_event(
        event_type=RunEventType.TOOL_CALL,
        payload={
            "agent_id": "card_agent",
            "tool": "card_list",
            "call_id": "tool_call_1",
            "arguments": {"account_id": "acc_1"},
            "idempotency_key": "card_agent:card_list:abc",
            "trusted_context": {
                "user_id": USER,
                "session_id": "sess_1",
                "bank_customer_id": "cust_1",
            },
        },
        agent_id="card_agent",
        tool_name="card_list",
        call_id="tool_call_1",
        context=AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id=USER),
    )

    payload = stored_events(rt)[0].payload_json
    assert payload["trusted_context"]["bank_customer_id"] == "cust_1"
    assert payload["idempotency_key"] == "card_agent:card_list:abc"
    assert payload["arguments"] == {"account_id": "acc_1"}


def test_the_audit_tables_are_not_reachable_from_the_conversation_api(rt, session):
    """A stored audit row is not a message: the ledger stays the user-visible history."""

    from app.conversation.service import ConversationService

    service = ConversationService(rt.database)
    created = service.create(session["user_id"], "审计不在这里")
    audit(rt).record_llm_call(
        call_id="call_hidden",
        request={"messages": [{"role": "user", "content": "审计内容"}]},
        response_text="审计内容",
        result=RESULT_SUCCEEDED,
        context=AuditContext(conversation_id=created.conversation_id, user_id=session["user_id"]),
    )

    page = service.messages(session["user_id"], created.conversation_id)

    assert page["items"] == []


def test_the_uuid_primary_key_is_generated_by_the_row(rt):
    service = audit(rt)
    service.record_llm_call(call_id="call_pk", request={}, result=RESULT_SUCCEEDED)

    assert isinstance(stored_calls(rt)[0].id, UUID)
