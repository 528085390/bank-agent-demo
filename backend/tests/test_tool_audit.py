"""The Gateway's audit rows: the full arguments, the full result, and every outcome.

The SSE frame the browser sees is a projection -- `facts` is deliberately dropped, and so is
anything else the envelope does not publish. This file pins the other half: the audit row
keeps the model's own arguments and the adapter's complete `ToolResult`, for successes,
failures, retries and refusals alike. The two are written by the same call and neither is
derived from the other.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.audit.context import AuditContext, audit_context
from app.audit.service import AuditService
from app.storage.models import AgentRunEvent
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolResult, ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1", bank_customer_id="cust_1")
CONVERSATION = "conv_tool_audit"
RUN = "run_tool_audit"

LOOKUP = ToolSpec(
    name="lookup",
    description="Look one record up",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
)
WRITE = ToolSpec(
    name="record",
    description="Record one thing",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    side_effect="WRITE",
)


class FactsAdapter:
    """An adapter whose result carries a module-private `facts` the frame never shows."""

    def __init__(self, facts: dict, *, status: ToolStatus = ToolStatus.SUCCEEDED, **extra) -> None:
        self._facts = facts
        self._status = status
        self._extra = extra

    def invoke(self, invocation) -> ToolResult:
        return ToolResult(
            tool_name=invocation.call.tool_name,
            status=self._status,
            call_id=invocation.call.call_id,
            data={"summary": "已完成", "display": {"n": 1}, "facts": self._facts},
            **self._extra,
        )


def events(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(AgentRunEvent).order_by(AgentRunEvent.created_at)))


def gateway_with(rt, adapter, *grants: tuple[str, list[ToolSpec]]) -> ToolGateway:
    specs = [spec for _agent, group in grants for spec in group]
    catalog = ToolCatalog.build(
        specs, {agent: [spec.name for spec in group] for agent, group in grants}
    )
    return ToolGateway(catalog, adapter, audit=AuditService(rt.database))


def in_turn():
    return audit_context(
        AuditContext(conversation_id=CONVERSATION, run_id=RUN, user_id="u_1", source="conversation")
    )


def test_a_successful_call_writes_the_request_and_the_whole_result(rt):
    adapter = RecordingAdapter({"lookup": {"summary": "找到了", "facts": {"n": 1}}})
    gateway = gateway_with(rt, adapter, ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.handle("alpha", CONTEXT).call(
            {
                "tool": "lookup",
                "arguments": {"key": "k1"},
                "call_id": "c-1",
                "idempotency_key": "alpha:lookup:abc",
            }
        )

    assert result.status is ToolStatus.SUCCEEDED
    rows = events(rt)
    assert [row.event_type for row in rows] == ["TOOL_CALL", "TOOL_RESULT"]
    call, outcome = rows
    assert call.payload_json["arguments"] == {"key": "k1"}
    assert call.payload_json["tool"] == "lookup"
    assert call.payload_json["call_id"] == "c-1"
    assert call.payload_json["idempotency_key"] == "alpha:lookup:abc"
    # The identity the server injected is part of what the call was.
    assert call.payload_json["trusted_context"] == {
        "user_id": "u_1",
        "session_id": "s_1",
        "bank_customer_id": "cust_1",
    }
    assert outcome.status == "SUCCEEDED"
    assert outcome.payload_json["result"]["data"]["facts"] == {"n": 1}
    assert (outcome.conversation_id, outcome.run_id) == (CONVERSATION, RUN)


def test_the_module_private_facts_survive_in_the_persisted_payload(rt):
    """`facts` is what the frame drops. It is the reason this table exists."""

    facts = {"cards": [{"card_id": "card_1", "limit": "5000.00"}], "nested": {"deep": [1, 2, 3]}}
    gateway = gateway_with(rt, FactsAdapter(facts), ("alpha", [LOOKUP]))

    with in_turn():
        gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    stored = events(rt)[1].payload_json["result"]["data"]
    assert stored["facts"] == facts
    assert stored["summary"] == "已完成"
    assert stored["display"] == {"n": 1}


def test_a_failed_result_is_stored_with_its_status_and_code(rt):
    adapter = RecordingAdapter()
    adapter.fail("lookup", "上游不可用", retryable=True)
    gateway = gateway_with(rt, adapter, ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.handle("alpha", CONTEXT).call(
            {"tool": "lookup", "arguments": {"key": "k"}, "call_id": "c-2"}
        )

    assert result.status is ToolStatus.FAILED
    outcome = events(rt)[1]
    assert outcome.status == "FAILED"
    assert outcome.payload_json["result"]["error"] == "上游不可用"
    assert outcome.payload_json["result"]["retryable"] is True


def test_a_retry_leaves_two_separate_call_and_result_pairs(rt):
    """Each attempt is its own call, and the table says so without a counter."""

    adapter = RecordingAdapter()
    adapter.fail("lookup", "暂时不可用", retryable=True)
    gateway = gateway_with(rt, adapter, ("alpha", [LOOKUP]))
    handle = gateway.handle("alpha", CONTEXT)

    with in_turn():
        first = handle.call(
            {
                "tool": "lookup",
                "arguments": {"key": "k"},
                "call_id": "c-a",
                "idempotency_key": "alpha:lookup:same-intent",
            }
        )
        adapter.register("lookup", {"summary": "第二次成功"})
        second = handle.call(
            {
                "tool": "lookup",
                "arguments": {"key": "k"},
                "call_id": "c-b",
                "idempotency_key": "alpha:lookup:same-intent",
            }
        )

    assert (first.status, second.status) == (ToolStatus.FAILED, ToolStatus.SUCCEEDED)
    rows = events(rt)
    assert [row.event_type for row in rows] == [
        "TOOL_CALL",
        "TOOL_RESULT",
        "TOOL_CALL",
        "TOOL_RESULT",
    ]
    assert [row.call_id for row in rows] == ["c-a", "c-a", "c-b", "c-b"]
    assert [row.status for row in rows] == [None, "FAILED", None, "SUCCEEDED"]
    # The intent is the same on both attempts, which is what the bank needs to see.
    assert rows[0].payload_json["idempotency_key"] == rows[2].payload_json["idempotency_key"]
    assert rows[0].payload_json["idempotency_key"] is not None


def test_an_unauthorized_tool_writes_a_refusal(rt):
    gateway = gateway_with(rt, RecordingAdapter(), ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.handle("alpha", CONTEXT).call(
            {"tool": "ghost", "arguments": {"key": "k"}, "call_id": "c-3"}
        )

    assert result.status is ToolStatus.DENIED
    rows = events(rt)
    assert len(rows) == 1
    assert rows[0].event_type == "TOOL_RESULT"
    assert rows[0].status == "DENIED"
    assert rows[0].error_code == "TOOL_DENIED"
    assert rows[0].tool_name == "ghost"
    assert rows[0].call_id == "c-3"
    assert "may not call" in rows[0].payload_json["reason"]


def test_an_argument_schema_refusal_writes_the_arguments_it_rejected(rt):
    gateway = gateway_with(rt, RecordingAdapter(), ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.handle("alpha", CONTEXT).call(
            # The required key is present, so the complaint is about the extra field -- which
            # is the one whose name the assertion below looks for.
            {"tool": "lookup", "arguments": {"key": "k", "surprise": 1}, "call_id": "c-4"}
        )

    assert result.status is ToolStatus.DENIED
    row = events(rt)[0]
    assert row.status == "DENIED"
    # The arguments are kept even though they were rejected: they are what the model asked
    # for, and "the model keeps sending the wrong shape" is exactly what an operator looks for.
    assert row.payload_json["arguments"] == {"key": "k", "surprise": 1}
    assert "surprise" in row.payload_json["reason"]


@pytest.mark.parametrize(
    "bad_request",
    [
        {"arguments": {"key": "k"}},
        {"tool": "  ", "arguments": {"key": "k"}},
        {"tool": "lookup", "arguments": "not-an-object"},
    ],
    ids=["no-tool", "blank-tool", "bad-arguments"],
)
def test_every_structural_refusal_is_recorded(rt, bad_request):
    gateway = gateway_with(rt, RecordingAdapter(), ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.handle("alpha", CONTEXT).call(bad_request)

    assert result.status is ToolStatus.DENIED
    assert events(rt)[0].status == "DENIED"


def test_a_request_that_is_not_an_object_is_recorded(rt):
    """The one refusal with nothing at all to read: it is still a refusal that happened."""

    gateway = gateway_with(rt, RecordingAdapter(), ("alpha", [LOOKUP]))

    with in_turn():
        result = gateway.invoke(gateway.handle("alpha", CONTEXT), "not-an-object")

    assert result.status is ToolStatus.DENIED
    assert events(rt)[0].event_type == "TOOL_RESULT"


def test_a_write_refused_for_a_missing_key_is_recorded_with_its_code(rt):
    """`IDEMPOTENCY_KEY_REQUIRED` is the code the retry policy reads; it must survive."""

    class Refusing:
        def invoke(self, invocation):
            return ToolResult(
                tool_name=invocation.call.tool_name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error="缺少幂等键",
                retryable=True,
                error_code="IDEMPOTENCY_KEY_REQUIRED",
            )

    gateway = gateway_with(rt, Refusing(), ("alpha", [WRITE]))

    with in_turn():
        gateway.handle("alpha", CONTEXT).call(
            {"tool": "record", "arguments": {"value": "v"}, "call_id": "c-5"}
        )

    outcome = events(rt)[1]
    assert outcome.error_code == "IDEMPOTENCY_KEY_REQUIRED"
    assert outcome.payload_json["result"]["retryable"] is True


def test_the_call_event_is_written_before_the_adapter_runs(rt):
    """A crash inside the adapter must still leave the attempt behind."""

    seen: list[int] = []

    class Exploding:
        def invoke(self, invocation):
            with rt.database.sessions() as db:
                seen.append(len(list(db.scalars(select(AgentRunEvent)))))
            raise RuntimeError("adapter exploded")

    gateway = gateway_with(rt, Exploding(), ("alpha", [LOOKUP]))

    with in_turn():
        with pytest.raises(RuntimeError):
            gateway.handle("alpha", CONTEXT).call(
                {"tool": "lookup", "arguments": {"key": "k"}, "call_id": "c-6"}
            )

    assert seen == [1], "the call was already stored when the adapter ran"
    assert events(rt)[0].event_type == "TOOL_CALL"


def test_a_gateway_with_no_audit_service_still_works(rt):
    """A hand-built gateway in a unit test keeps its old behaviour exactly."""

    adapter = RecordingAdapter({"lookup": {"ok": True}})
    catalog = ToolCatalog.build([LOOKUP], {"alpha": ["lookup"]})
    gateway = ToolGateway(catalog, adapter)

    result = gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    assert result.status is ToolStatus.SUCCEEDED
    assert events(rt) == []


def test_the_events_carry_the_turn_they_happened_in(rt):
    gateway = gateway_with(rt, RecordingAdapter({"lookup": {}}), ("alpha", [LOOKUP]))

    with in_turn():
        gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    for row in events(rt):
        assert (row.conversation_id, row.run_id, row.user_id) == (CONVERSATION, RUN, "u_1")
        assert row.source == "conversation"


def test_a_tool_event_with_no_turn_records_no_conversation(rt):
    gateway = gateway_with(rt, RecordingAdapter({"lookup": {}}), ("alpha", [LOOKUP]))

    gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    for row in events(rt):
        assert (row.conversation_id, row.run_id) == (None, None)


def test_an_audit_write_that_fails_fails_the_call(rt, monkeypatch):
    """The spec forbids a silent fallback to "only the log knows"."""

    gateway = gateway_with(rt, RecordingAdapter({"lookup": {}}), ("alpha", [LOOKUP]))

    def explode(**_kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(gateway._audit, "record_agent_event", explode)

    with in_turn():
        with pytest.raises(RuntimeError):
            gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    assert gateway._adapter.invocations == [], "nothing was performed untraceably"


def test_a_deserialized_result_is_plain_json(rt):
    """The payload has to survive JSONB: an enum or a Decimal would not."""

    @dataclass(frozen=True)
    class Amount:
        value: str

    gateway = gateway_with(rt, FactsAdapter({"amount": Amount("10.00")}), ("alpha", [LOOKUP]))

    with in_turn():
        gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    assert events(rt)[1].payload_json["result"]["data"]["facts"] == {"amount": {"value": "10.00"}}
