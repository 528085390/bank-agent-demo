"""A failure has to be machine-readable, not just readable.

Before this, every failure was one `status=FAILED` plus a Chinese sentence, so a test
could only assert the wording -- and the frontend could not tell "you are not bound to a
bank customer" from "insufficient balance".
"""

import asyncio

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.integration.bank_client import BankClient
from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError, BankErrorKind
from app.modules.base import ERROR_MESSAGES, ModuleSpec, ToolData, ToolFailure
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import (
    ErrorCode,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder


def test_error_code_is_appended_after_retryable():
    """Position matters: `ToolResult` is frozen, so inserting a field in the middle
    would silently shift every positional construction site."""

    result = ToolResult(tool_name="t", status=ToolStatus.FAILED, error_code="X")

    assert result.error_code == "X"
    # The five original fields keep their positions.
    assert ToolResult("t", ToolStatus.FAILED, "c1", {"a": 1}, "boom").error == "boom"
    # Positional construction with six arguments: if `error_code` were inserted before
    # `retryable`, the sixth positional argument would land on the wrong field.
    assert ToolResult("t", ToolStatus.FAILED, "c1", {"a": 1}, "boom", True).retryable is True


def test_error_code_defaults_to_none():
    assert ToolResult(tool_name="t", status=ToolStatus.SUCCEEDED).error_code is None


def test_every_error_code_has_a_message():
    # Both directions, as `test_bank_client.py` does for `BankErrorKind`: a missing key is
    # wording the user would never see, and a stale extra key is a code nothing can carry.
    assert set(ERROR_MESSAGES) == set(ErrorCode)
    for code in ErrorCode:
        assert ERROR_MESSAGES[code].strip()


def test_a_tool_failure_rejects_an_unknown_code():
    """A typo would otherwise become a code the frontend silently cannot branch on."""

    with pytest.raises(ValueError, match="unknown error code: NO_SUCH_CODE"):
        ToolFailure("NO_SUCH_CODE", "随便什么")


def test_a_tool_failure_accepts_a_registered_code():
    failure = ToolFailure(ErrorCode.NO_CUSTOMER_BINDING, "当前用户未绑定银行客户号")

    assert failure.code == "NO_CUSTOMER_BINDING"
    assert failure.retryable is False


def test_the_binding_failure_is_not_retryable_and_a_transient_bank_one_is():
    """Retrying the binding failure is pointless -- it is a data problem, not a blip. A
    transient bank failure is the one case worth retrying, and the adapter has to say so
    rather than leaving every failure non-retryable by default."""

    def binding(inv, client):
        raise ToolFailure(ErrorCode.NO_CUSTOMER_BINDING, "当前用户未绑定银行客户号")

    assert adapter_with(binding).invoke(call()).retryable is False

    def transient(inv, client):
        raise BankCallError(BankErrorKind.CONNECT, "连接失败", retryable=True)

    result = adapter_with(transient).invoke(call())

    assert result.retryable is True
    assert result.error_code == ErrorCode.UPSTREAM_UNAVAILABLE


ECHO = ToolSpec(name="echo", description="e")
CONTEXT = TrustedContext(user_id="u", session_id="s", bank_customer_id="u_zhang")


def adapter_with(handler, *, client=None):
    declaration = DomainDeclaration(agent_id="alpha", display="A", purpose="p")
    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=declaration,
                tools={"echo": ECHO},
                handlers={"echo": handler},
            )
        ]
    )
    return BankToolAdapter(registry, client or BankClient("http://bank.test"))


def call():
    return ToolInvocation(
        agent_id="alpha",
        call=ToolCall(tool_name="echo", arguments={}, call_id="c1"),
        context=CONTEXT,
    )


def test_a_declared_failure_carries_its_code():
    def handler(inv, client):
        raise ToolFailure(ErrorCode.NO_CUSTOMER_BINDING, "当前用户未绑定银行客户号")

    result = adapter_with(handler).invoke(call())

    assert result.error_code == "NO_CUSTOMER_BINDING"


def test_an_unexpected_exception_is_internal():
    def handler(inv, client):
        raise RuntimeError("psycopg: password=secret")

    result = adapter_with(handler).invoke(call())

    assert result.error_code == "INTERNAL"
    assert "secret" not in (result.error or "")


def test_a_missing_tool_is_reported_as_such():
    result = adapter_with(
        lambda inv, client: ToolData(summary="s", display={"blocks": []}, facts={})
    ).invoke(
        ToolInvocation(
            agent_id="alpha", call=ToolCall(tool_name="ghost", arguments={}), context=CONTEXT
        )
    )

    assert result.error_code == "NOT_FOUND"


def test_every_bank_error_kind_maps_to_a_code():
    """An unmapped kind would reach the frontend as a bare FAILED, which is the thing this
    task exists to prevent."""

    assert set(KIND_TO_ERROR_CODE) == set(BankErrorKind), "a new kind was added and not mapped"
    for kind in BankErrorKind:
        assert KIND_TO_ERROR_CODE[kind] in {member.value for member in ErrorCode}


def test_transient_kinds_collapse_into_one_code_and_business_ones_keep_their_meaning():
    """By the time a client sees the code, `_run_tool` has already spent the one retry it is
    allowed, so a transient failure must not advertise "try again" as a distinct action."""

    for kind in (
        BankErrorKind.CONNECT,
        BankErrorKind.TIMEOUT,
        BankErrorKind.RATE_LIMITED,
        BankErrorKind.SERVER,
    ):
        assert KIND_TO_ERROR_CODE[kind] == ErrorCode.UPSTREAM_UNAVAILABLE

    for kind in (
        BankErrorKind.NOT_FOUND,
        BankErrorKind.INVALID_STATE,
        BankErrorKind.INSUFFICIENT_BALANCE,
        BankErrorKind.CONFIRMATION_REQUIRED,
        BankErrorKind.VALIDATION,
        BankErrorKind.CLIENT,
        BankErrorKind.PROTOCOL,
    ):
        assert KIND_TO_ERROR_CODE[kind] != ErrorCode.UPSTREAM_UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (404, 1004, "NOT_FOUND"),
        (409, 1006, "INSUFFICIENT_BALANCE"),
        (409, 1009, "LOCKED"),
        (422, 1012, "VALIDATION"),
    ],
)
def test_a_bank_refusal_keeps_its_meaning_in_the_code(status, code, expected):
    def handler(inv, client):
        return ToolData(
            summary=str(client.request("GET", "/x", user_id="u_zhang").data),
            display={"blocks": []},
            facts={},
        )

    def transport(request):
        return httpx.Response(status, json={"code": code, "message": "e", "data": None})

    client = BankClient("http://bank.test", transport=httpx.MockTransport(transport))
    result = adapter_with(handler, client=client).invoke(call())

    assert result.error_code == expected


def gateway_for(specs, grants):
    return ToolGateway(ToolCatalog.build(specs, grants), RecordingAdapter())


def test_a_denied_call_carries_a_code():
    """A refusal never reaches an adapter, so the code has to be attached at the Gateway."""

    handle = gateway_for([ECHO], {"alpha": ["echo"]}).handle("alpha", CONTEXT)
    result = handle.call({"tool": "ghost", "arguments": {}, "call_id": "c1"})

    assert result.status is ToolStatus.DENIED
    assert result.error_code == "TOOL_DENIED"


def test_a_denied_argument_schema_also_carries_a_code():
    strict = ToolSpec(
        name="strict",
        description="s",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    )
    handle = gateway_for([strict], {"alpha": ["strict"]}).handle("alpha", CONTEXT)
    result = handle.call({"tool": "strict", "arguments": {"n": "not-a-number"}})

    assert result.status is ToolStatus.DENIED
    assert result.error_code == "TOOL_DENIED"


def failing_tool_events(handler) -> list:
    """Drive one full turn whose only tool raises, and return every published event.

    The whole graph is used rather than the adapter alone because what these two tests pin
    is what a *failed* call publishes to the client -- and the frame is written by
    `_call_tool`, which only runs inside the graph.
    """

    declaration = DomainDeclaration(
        agent_id="alpha",
        display="A",
        purpose="p",
        graph_builder=tool_domain_graph_builder,
    )
    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=declaration,
                tools={"echo": ECHO},
                handlers={"echo": handler},
            )
        ]
    )
    adapter = BankToolAdapter(registry, BankClient("http://bank.test"))
    gateway = ToolGateway(registry.catalog, adapter)
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"alpha","instruction":"做"}'),
            ("call", "echo", "{}"),
            # A failed round is fed back and still costs a following report call (spec §4.8.3).
            "余额不足，办不了。",
            "完成",
        ]
    )
    graph = build_agent_graph(model, gateway, CONTEXT, roster=registry.roster).compile(
        checkpointer=InMemorySaver()
    )
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke("s1", "u", "帮我", {})]

    return asyncio.run(run())


def test_the_tool_result_event_carries_the_code():
    """The client branches on this; a code that stops at the adapter is invisible."""

    def handler(inv, client):
        raise ToolFailure(ErrorCode.INSUFFICIENT_BALANCE, "账户余额不足。")

    events = failing_tool_events(handler)
    tool_result = next(event for event in events if event.event == "tool_result")

    assert tool_result.data["error_code"] == "INSUFFICIENT_BALANCE"


def test_a_failed_tool_result_publishes_no_envelope_and_keeps_its_code_at_the_top_level():
    """A failure has no envelope, and saying so is the frame's job.

    `data` is what tells the client an envelope is present. A failed call must therefore
    publish `data: null` rather than a projection whose three fields are all null -- the
    latter would claim a tool returned an envelope, and would make a failure look like a
    success that carried nothing.

    The second half pins where the code lives. `error` and `error_code` describe *this
    attempt*, like `status`, so they are siblings of `data` and never keys inside it: a
    client that had to reach into the envelope for its error code could not read one for a
    failure at all, since a failure has no envelope.
    """

    def handler(inv, client):
        raise ToolFailure(ErrorCode.INSUFFICIENT_BALANCE, "账户余额不足。")

    events = failing_tool_events(handler)
    tool_result = next(event for event in events if event.event == "tool_result")

    assert tool_result.data["status"] == ToolStatus.FAILED.value
    assert tool_result.data["data"] is None
    assert tool_result.data["error"] == "账户余额不足。"
    assert tool_result.data["error_code"] == "INSUFFICIENT_BALANCE"
    # The exact key set is what makes "siblings, not nested" checkable.
    assert set(tool_result.data) == {
        "agent_id",
        "tool_name",
        "call_id",
        "status",
        "data",
        "error",
        "error_code",
    }
