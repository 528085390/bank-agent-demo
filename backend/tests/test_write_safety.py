"""A write must not be retried into a second write.

The retry in `_run_tool` predates any notion of a side effect: it retries anything the
adapter calls transient. For a read that is fine. For a transfer it means one timeout can
become two transfers -- so a tool has to say what it is, and the retry has to ask.
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.core.errors import AppError
from app.integration.bank_client import BankClient, BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.modules.base import ModuleSpec, ToolData, ToolFailure
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import (
    ErrorCode,
    SideEffect,
    ToolCall,
    ToolInvocation,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

DECLARATION = DomainDeclaration(
    agent_id="alpha",
    display="A",
    purpose="p",
    graph_builder=tool_domain_graph_builder,
)
CONTEXT = TrustedContext(user_id="u", session_id="s1", bank_customer_id="u_zhang")
SCRIPT = [
    ("call", "handoff", '{"agent_id":"alpha","instruction":"做"}'),
    ("call", "echo", "{}"),
    # Every tool round costs a following report call (spec §4.8.3).
    "办好了",
    "完成",
]


def handler(invocation, client):
    return ToolData(summary="ok", display={"blocks": []}, facts={})


def registry_with(spec):
    return build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={spec.name: spec},
                handlers={spec.name: handler},
            )
        ]
    )


def test_a_tool_is_a_read_unless_it_says_otherwise():
    assert ToolSpec(name="echo").side_effect == SideEffect.READ


def test_a_tool_may_declare_itself_a_write():
    assert ToolSpec(name="echo", side_effect=SideEffect.WRITE).side_effect == "WRITE"


def test_a_misspelled_side_effect_is_refused_at_registry_build():
    """A trailing space would otherwise be read as "not WRITE" and silently regain the
    retry that write safety exists to remove."""

    with pytest.raises(ValueError, match="invalid side_effect"):
        registry_with(ToolSpec(name="echo", side_effect="WRITE "))


def test_an_unknown_side_effect_is_refused():
    with pytest.raises(ValueError, match="invalid side_effect"):
        registry_with(ToolSpec(name="echo", side_effect="MUTATE"))


def test_both_declared_effects_are_accepted():
    for effect in (SideEffect.READ, SideEffect.WRITE):
        assert registry_with(ToolSpec(name="echo", side_effect=effect)).catalog.names() == ("echo",)


class FlakyClient(BankClient):
    """Fails the first call transiently, then succeeds. Records every request."""

    def __init__(self, failures=1):
        super().__init__("http://bank.test")
        self.requests = []
        self._failures = failures

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        if len(self.requests) <= self._failures:
            raise BankCallError(BankErrorKind.TIMEOUT, "银行服务暂时不可用。", retryable=True)
        return BankResponse(code=0, message="ok", data={"transfer_id": "T-1"})


def write_runtime(spec, handler, client):
    """The whole graph around one tool spec and one bank client."""

    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={"echo": spec},
                handlers={"echo": handler},
            )
        ]
    )
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, client))
    graph = build_agent_graph(
        ScriptedModel(SCRIPT), gateway, CONTEXT, roster=registry.roster
    ).compile(checkpointer=InMemorySaver())
    return AgentRuntime(graph=graph)


def run_write(effect, *, failures=1):
    spec = ToolSpec(name="echo", description="e", side_effect=effect)

    def handler(invocation, client):
        response = client.request(
            "POST",
            "/transfers",
            user_id=invocation.context.bank_customer_id,
            idempotency_key=invocation.call.idempotency_key,
        )
        return ToolData(
            summary="ok", display={"blocks": []}, facts={"id": response.data["transfer_id"]}
        )

    client = FlakyClient(failures=failures)
    runtime = write_runtime(spec, handler, client)

    async def go():
        return [e async for e in runtime.invoke("s1", "u", "帮我", {})]

    events = asyncio.run(go())
    return events, client


def test_a_retried_write_reuses_one_idempotency_key():
    """The retry is a new attempt at the same intent. If the key changed with it, the bank
    would see two transfers -- which is the whole reason the key exists."""

    events, client = run_write(SideEffect.WRITE)

    assert len(client.requests) == 2, "expected one failed attempt and one retry"
    keys = {r["idempotency_key"] for r in client.requests}
    assert len(keys) == 1, f"the retry changed the key: {client.requests}"
    assert keys != {None}


def test_the_two_attempts_still_have_different_call_ids():
    """The key is stable across a retry; the call id is not. They answer different
    questions -- "is this the same intent" versus "which attempt is this"."""

    events, client = run_write(SideEffect.WRITE)
    call_ids = [e.data["call_id"] for e in events if e.event == "tool_call"]

    assert len(call_ids) == 2
    assert len(set(call_ids)) == 2


def test_a_write_without_a_key_is_refused_before_any_request():
    """Fail closed: an unkeyed write cannot be retried safely, so it is not attempted.

    Driven through the adapter, not the graph: `_run_tool` always mints a key, so the only way
    to construct this call is directly -- which is exactly the caller the adapter must defend
    against.
    """

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.WRITE)
    ran = []

    def handler(invocation, client):
        ran.append(invocation)
        return ToolData(summary="ok", display={}, facts={})

    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={"echo": spec},
                handlers={"echo": handler},
            )
        ]
    )
    client = FlakyClient(failures=0)
    result = BankToolAdapter(registry, client).invoke(
        ToolInvocation(
            agent_id="alpha",
            call=ToolCall(tool_name="echo", arguments={}, call_id="c1"),  # no key
            context=CONTEXT,
        )
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "IDEMPOTENCY_KEY_REQUIRED"
    assert result.retryable is False
    assert ran == [], "the handler ran for a write that carried no key"
    assert client.requests == [], "an unkeyed write reached the bank"


def test_a_write_with_a_key_is_allowed_through():
    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.WRITE)
    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={"echo": spec},
                handlers={"echo": lambda i, c: ToolData(summary="ok", display={}, facts={})},
            )
        ]
    )
    result = BankToolAdapter(registry, FlakyClient(failures=0)).invoke(
        ToolInvocation(
            agent_id="alpha",
            call=ToolCall(tool_name="echo", arguments={}, call_id="c1", idempotency_key="k1"),
            context=CONTEXT,
        )
    )

    assert result.status is ToolStatus.SUCCEEDED


def test_a_read_needs_no_key():
    """Only writes need one: a read has no effect to repeat."""

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.READ)
    registry = build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=DECLARATION,
                tools={"echo": spec},
                handlers={"echo": lambda i, c: ToolData(summary="ok", display={}, facts={})},
            )
        ]
    )
    result = BankToolAdapter(registry, FlakyClient(failures=0)).invoke(
        ToolInvocation(
            agent_id="alpha",
            call=ToolCall(tool_name="echo", arguments={}, call_id="c1"),  # no key
            context=CONTEXT,
        )
    )

    assert result.status is ToolStatus.SUCCEEDED


def test_a_read_is_still_retried_once():
    """The existing behaviour must survive: reads have no effect to repeat."""

    events, client = run_write(SideEffect.READ)

    assert len(client.requests) == 2


def test_a_business_refusal_is_never_retried():
    """Retrying does not turn "insufficient balance" into "sufficient balance"."""

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.READ)

    def handler(invocation, client):
        raise ToolFailure(ErrorCode.INSUFFICIENT_BALANCE, "账户余额不足。")

    client = FlakyClient(failures=0)
    runtime = write_runtime(spec, handler, client)

    async def go():
        return [e async for e in runtime.invoke("s1", "u", "帮我", {})]

    events = asyncio.run(go())
    calls = [e for e in events if e.event == "tool_call"]

    assert len(calls) == 1, "a business refusal was retried"


def test_a_write_that_keeps_failing_transiently_stops_after_one_retry():
    """A keyed write is retried once, and a second failure ends the turn loudly.

    The plan wrote this as `events, client = run_write(SideEffect.WRITE, failures=5)`, which
    cannot pass: with no question to re-present, a surviving transient failure is a stream
    error, so `asyncio.run` raises `UpstreamUnavailable` before `run_write` returns and the
    plan's own assertions never run. `test_domain_agents.py`'s
    `test_a_retryable_tool_failure_that_survives_the_retry_ends_the_turn_loudly` pins that
    behaviour, so the events are collected from inside the generator instead -- every
    assertion the plan made is kept, plus the raise the graph actually performs.
    """

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.WRITE)

    def handler(invocation, client):
        client.request(
            "POST",
            "/transfers",
            user_id=invocation.context.bank_customer_id,
            idempotency_key=invocation.call.idempotency_key,
        )
        return ToolData(summary="ok", display={"blocks": []}, facts={})

    client = FlakyClient(failures=5)
    runtime = write_runtime(spec, handler, client)
    seen = []

    async def go():
        async for event in runtime.invoke("s1", "u", "帮我", {}):
            seen.append(event)

    with pytest.raises(AppError) as failure:
        asyncio.run(go())

    assert failure.value.details["upstream_code"] == "TOOL_UNAVAILABLE"
    assert len(client.requests) == 2
    result = next(e for e in seen if e.event == "tool_result")
    assert result.data["error_code"] == "UPSTREAM_UNAVAILABLE"


def test_a_write_failing_for_a_missing_key_is_never_retried():
    """The one `_may_retry` branch unique to this task.

    The adapter's own refusal is `retryable=False`, so this branch is unreachable through
    it; it is reachable only when a handler raises the code as transient. Retrying that
    would repeat the very call the code says had no key to repeat safely.
    """

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.WRITE)

    def handler(invocation, client):
        raise ToolFailure(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "系统繁忙，请稍后重试", retryable=True
        )

    client = FlakyClient(failures=0)
    runtime = write_runtime(spec, handler, client)

    async def go():
        return [e async for e in runtime.invoke("s1", "u", "帮我", {})]

    events = asyncio.run(go())
    calls = [e for e in events if e.event == "tool_call"]
    result = next(e for e in events if e.event == "tool_result")

    assert len(calls) == 1, "a write refused for a missing key was retried"
    assert result.data["error_code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_read_failing_for_a_missing_key_is_still_retried():
    """The companion, and the discriminator: that code is refused only for a *write*.

    A read needs no key, so its absence is no reason to deny a read its retry. This pins the
    check inside the `WRITE` branch -- hoisting it above the side-effect test would refuse a
    read its retry, which is the regression this pair exists to catch.
    """

    spec = ToolSpec(name="echo", description="e", side_effect=SideEffect.READ)

    def handler(invocation, client):
        raise ToolFailure(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "系统繁忙，请稍后重试", retryable=True
        )

    client = FlakyClient(failures=0)
    runtime = write_runtime(spec, handler, client)
    seen = []

    async def go():
        async for event in runtime.invoke("s1", "u", "帮我", {}):
            seen.append(event)

    # The read spends its one retry and that attempt fails too, so the turn ends loudly --
    # the same path as any read whose transient failure survives. What is under test is that
    # it *got* the retry, which the pair of published calls shows.
    with pytest.raises(AppError):
        asyncio.run(go())

    assert len([e for e in seen if e.event == "tool_call"]) == 2, "a read lost its retry"
