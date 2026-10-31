"""The Gateway contract: what it refuses, and what it refuses to read.

No scenario here names a bank, an account or a business field. The catalog is built
from invented tools, because the Gateway's job is the binding, not the domain.
"""

import logging

import pytest

from app.modules.base import ERROR_MESSAGES
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ErrorCode, ToolResult, ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")

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
NOTIFY = ToolSpec(
    name="notify",
    description="Send one notification",
    parameters={
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
        "additionalProperties": False,
    },
)


def gateway_with(*grants: tuple[str, list[ToolSpec]], replies=None):
    specs = [spec for _agent, group in grants for spec in group]
    adapter = RecordingAdapter(replies)
    catalog = ToolCatalog.build(
        specs, {agent: [spec.name for spec in group] for agent, group in grants}
    )
    return ToolGateway(catalog, adapter), adapter


def _denials(caplog) -> list[str]:
    """The Gateway's own log lines, so a test can read the reason the user must not see.

    Filtered by logger name rather than taking every captured record: `len(caplog.records)`
    would also count whatever any other library happened to log during the call.
    """

    return [record.getMessage() for record in caplog.records if record.name == "app.tools.gateway"]


def test_a_handle_carries_exactly_the_tools_its_agent_was_granted():
    gateway, _ = gateway_with(("alpha", [LOOKUP]), ("beta", [NOTIFY]))

    assert gateway.handle("alpha", CONTEXT).tool_names == {"lookup"}
    assert gateway.handle("beta", CONTEXT).tool_names == {"notify"}


def test_a_granted_tool_reaches_the_adapter_with_the_trusted_context():
    gateway, adapter = gateway_with(("alpha", [LOOKUP]), replies={"lookup": {"found": "yes"}})

    result = gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k1"}})

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {"found": "yes"}
    assert adapter.tool_names == ["lookup"]
    # Identity came from the handle, not from the request.
    assert adapter.invocations[0].context == CONTEXT
    assert adapter.invocations[0].agent_id == "alpha"


def test_a_forged_agent_field_cannot_reach_another_agents_tool():
    """The model may claim any agent. The handle's own catalog still decides."""

    gateway, adapter = gateway_with(("bill", [LOOKUP]), ("transfer", [NOTIFY]))

    result = gateway.handle("bill", CONTEXT).call(
        {"agent": "transfer", "tool": "notify", "arguments": {"target": "t"}}
    )

    assert result.status is ToolStatus.DENIED
    assert adapter.invocations == []


def test_a_forged_agent_field_cannot_widen_the_same_tool_grant():
    gateway, adapter = gateway_with(("alpha", [LOOKUP]), ("beta", [NOTIFY]))

    # `alpha` claims to be `beta` while calling its own granted tool: still alpha.
    result = gateway.handle("alpha", CONTEXT).call(
        {"agent": "beta", "tool": "lookup", "arguments": {"key": "k"}}
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert adapter.invocations[0].agent_id == "alpha"


def test_an_unknown_tool_is_denied_without_reaching_the_adapter(caplog):
    gateway, adapter = gateway_with(("alpha", [LOOKUP]))

    with caplog.at_level(logging.WARNING):
        result = gateway.handle("alpha", CONTEXT).call({"tool": "vanish", "arguments": {}})

    assert result.status is ToolStatus.DENIED
    # The catalog's own words are a diagnostic, not a sentence for the user. `error` is the
    # shared message the client shows (contract 4.6.2), and the specific reason is logged.
    assert result.error == ERROR_MESSAGES[ErrorCode.TOOL_DENIED]
    assert "vanish" in _denials(caplog)[0]
    assert adapter.invocations == []


def test_an_undeclared_agent_gets_an_empty_handle_rather_than_an_error():
    gateway, adapter = gateway_with(("alpha", [LOOKUP]))

    handle = gateway.handle("ghost", CONTEXT)

    assert handle.tool_names == frozenset()
    assert handle.call({"tool": "lookup", "arguments": {}}).status is ToolStatus.DENIED
    assert adapter.invocations == []


def test_arguments_are_checked_against_the_declared_schema(caplog):
    gateway, adapter = gateway_with(("alpha", [LOOKUP]))

    with caplog.at_level(logging.WARNING):
        missing = gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {}})
        extra = gateway.handle("alpha", CONTEXT).call(
            {"tool": "lookup", "arguments": {"key": "k", "surprise": 1}}
        )

    assert missing.status is ToolStatus.DENIED and extra.status is ToolStatus.DENIED
    # jsonschema's complaint is the reason, and the reason is a log line: the user-facing
    # `error` is the same shared sentence for both, and the two complaints stay distinct.
    assert missing.error == extra.error == ERROR_MESSAGES[ErrorCode.TOOL_DENIED]
    assert len(_denials(caplog)) == 2
    assert "key" in _denials(caplog)[0]
    assert "surprise" in _denials(caplog)[1]
    assert adapter.invocations == []


def test_a_schema_refusal_logs_the_rule_and_the_name_but_never_the_value(caplog):
    """jsonschema's message embeds a `repr` of the offending value.

    The audit row already carries the whole `arguments` object (see `_refuse`), so the log
    loses nothing an operator needs by dropping the value -- and the parameter *name* has to
    stay, because that is what makes the line actionable.
    """

    secret = "SECRET_AMOUNT_9911"
    gateway, adapter = gateway_with(("alpha", [LOOKUP]))

    with caplog.at_level(logging.WARNING):
        result = gateway.handle("alpha", CONTEXT).call(
            {"tool": "lookup", "arguments": {"key": {"nested": secret}}, "call_id": "c-7"}
        )

    assert result.status is ToolStatus.DENIED
    assert secret not in caplog.text
    assert "key" in _denials(caplog)[0]
    assert "call_id=c-7" in _denials(caplog)[0]
    assert adapter.invocations == []


def test_a_tool_with_no_declared_parameters_accepts_any_object():
    gateway, adapter = gateway_with(("alpha", [ToolSpec(name="ping")]), replies={"ping": {}})

    result = gateway.handle("alpha", CONTEXT).call({"tool": "ping", "arguments": {"x": 1}})

    assert result.status is ToolStatus.SUCCEEDED
    assert len(adapter.invocations) == 1


def test_malformed_arguments_and_missing_names_are_denied_not_raised():
    gateway, adapter = gateway_with(("alpha", [LOOKUP]))

    handle = gateway.handle("alpha", CONTEXT)
    for request in (
        {"arguments": {"key": "k"}},
        {"tool": "   ", "arguments": {"key": "k"}},
        {"tool": "lookup", "arguments": "not-an-object"},
    ):
        result = handle.call(request)
        assert result.status is ToolStatus.DENIED

    assert adapter.invocations == []


def test_call_id_and_tool_name_are_echoed_back_on_every_outcome():
    gateway, adapter = gateway_with(("alpha", [LOOKUP]), replies={"lookup": {"ok": True}})

    handle = gateway.handle("alpha", CONTEXT)
    ok = handle.call({"tool": "lookup", "arguments": {"key": "k"}, "call_id": "c-1"})
    denied = handle.call({"tool": "ghost", "arguments": {}, "call_id": "c-2"})

    assert (ok.call_id, ok.tool_name) == ("c-1", "lookup")
    assert (denied.call_id, denied.tool_name) == ("c-2", "ghost")
    assert adapter.invocations[0].call.call_id == "c-1"


def test_an_adapter_failure_is_reported_as_a_failed_result():
    gateway, _ = gateway_with(("alpha", [LOOKUP]))
    gateway._adapter.fail("lookup", "upstream unavailable")

    result = gateway.handle("alpha", CONTEXT).call({"tool": "lookup", "arguments": {"key": "k"}})

    assert result.status is ToolStatus.FAILED
    assert result.error == "upstream unavailable"


class MisreportingAdapter:
    """An adapter that does not echo the call id back, as a real one may not.

    Nothing in the tool contract requires an adapter to echo it, so the Gateway cannot
    assume it does. The client pairs `tool_call` with `tool_result` by that id, and the
    server is what minted it -- so the server is what has to guarantee it.
    """

    def __init__(self, returned):
        self._returned = returned

    def invoke(self, invocation):
        return ToolResult(
            tool_name=invocation.call.tool_name,
            status=ToolStatus.SUCCEEDED,
            call_id=self._returned,
            data={"ok": True},
        )


@pytest.mark.parametrize(
    "returned",
    [None, "", "someone-elses-id"],
    ids=["missing", "empty", "wrong"],
)
def test_the_gateway_stamps_the_call_id_it_sent_regardless_of_the_adapter(returned):
    """A result must be pair-able with its call, whatever the adapter reports back."""

    gateway = ToolGateway(
        ToolCatalog.build([LOOKUP], {"alpha": ["lookup"]}), MisreportingAdapter(returned)
    )
    handle = gateway.handle("alpha", CONTEXT)

    result = handle.call({"tool": "lookup", "arguments": {"key": "k"}, "call_id": "c-1"})

    assert result.call_id == "c-1"
    # And the adapter really was handed the id the server minted.
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {"ok": True}


def test_a_denied_call_keeps_the_call_id_the_request_carried():
    """Denials never reach an adapter, so the id comes straight from the request."""

    gateway, _ = gateway_with(("alpha", [LOOKUP]))

    result = gateway.handle("alpha", CONTEXT).call(
        {"tool": "ghost", "arguments": {}, "call_id": "c-9"}
    )

    assert result.status is ToolStatus.DENIED
    assert result.call_id == "c-9"


def test_a_catalog_refuses_a_grant_for_an_undeclared_tool():
    with pytest.raises(ValueError) as failure:
        ToolCatalog.build([LOOKUP], {"alpha": ["lookup", "nowhere"]})

    assert "nowhere" in str(failure.value)


def test_a_catalog_refuses_a_duplicate_declaration():
    with pytest.raises(ValueError) as failure:
        ToolCatalog.build([LOOKUP, LOOKUP], {"alpha": ["lookup"]})

    assert "lookup" in str(failure.value)


def test_the_gateway_source_names_no_business_domain():
    """The boundary stays generic: no account, bill, transfer or subscription vocabulary."""

    import app.tools.catalog as catalog_module
    import app.tools.gateway as gateway_module
    import app.tools.handle as handle_module
    import app.tools.protocol as protocol_module

    forbidden = (
        "account",
        "bill",
        "transfer",
        "subscription",
        "refund",
        "balance",
        "card",
        "wealth",
    )
    for module in (protocol_module, catalog_module, gateway_module, handle_module):
        with open(module.__file__, encoding="utf-8") as handle_:
            source = handle_.read().lower()
        if module is protocol_module:
            # `ErrorCode` is the boundary's own vocabulary and lives here on purpose, so
            # `gateway.py` need not import `app.modules` (contract §4.4.3). Its identifiers
            # are protocol, not domain logic: `INSUFFICIENT_BALANCE` is precisely the thing
            # a client branches on instead of reading Chinese. Exempt that one identifier --
            # and only it: `balance`, `account_balance` and every other line here are still
            # scanned, and the three other modules are untouched.
            source = source.replace("insufficient_balance", "")
        for word in forbidden:
            assert word not in source, f"{module.__name__} mentions {word}"


def test_denied_tool_carries_explicit_interception_prefix():
    """断言：网关阻断文案统一采用「已拦截：...」显式安全话术。"""
    assert ERROR_MESSAGES[ErrorCode.TOOL_DENIED].startswith("已拦截：")
    assert "已被安全策略网关阻断" in ERROR_MESSAGES[ErrorCode.TOOL_DENIED]
    assert ERROR_MESSAGES[ErrorCode.VALIDATION].startswith("已拦截：")
    assert ERROR_MESSAGES[ErrorCode.CONFIRMATION_REQUIRED].startswith("已拦截：")

