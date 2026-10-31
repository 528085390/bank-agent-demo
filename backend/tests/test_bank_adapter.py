"""What the adapter does with a handler's answer, and with its misbehaviour."""

import httpx
import pytest

from app.agents.declaration import DomainDeclaration
from app.integration.bank_client import BankClient
from app.modules.base import ModuleSpec, ToolData, ToolFailure
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.protocol import (
    ErrorCode,
    ToolCall,
    ToolInvocation,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)

ECHO = ToolSpec(name="echo", description="Return what it was given")
CONTEXT = TrustedContext(user_id="user_0001", session_id="s_1", bank_customer_id="u_zhang")


def registry_with(handler):
    declaration = DomainDeclaration(agent_id="alpha", display="Alpha", purpose="测试领域")
    return build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=declaration,
                tools={"echo": ECHO},
                handlers={"echo": handler},
            )
        ]
    )


def adapter_for(handler, *, client=None) -> BankToolAdapter:
    return BankToolAdapter(registry_with(handler), client or BankClient("http://bank.test"))


def invocation() -> ToolInvocation:
    return ToolInvocation(
        agent_id="alpha",
        call=ToolCall(tool_name="echo", arguments={}, call_id="c1"),
        context=CONTEXT,
    )


def test_a_well_formed_envelope_becomes_a_successful_result():
    result = adapter_for(
        lambda inv, client: ToolData(summary="ok", display={"a": 1}, facts={})
    ).invoke(invocation())

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {"summary": "ok", "display": {"a": 1}, "facts": {}, "business_ids": {}}
    assert result.call_id == "c1"


def test_a_handler_that_returns_a_bare_dict_fails_loudly():
    with pytest.raises(TypeError, match="expected ToolData"):
        adapter_for(lambda inv, client: {"summary": "ok"}).invoke(invocation())


def test_a_declared_failure_becomes_a_failed_result_with_its_own_wording():
    def handler(inv, client):
        raise ToolFailure("NO_CUSTOMER_BINDING", "当前用户未绑定银行客户号")

    result = adapter_for(handler).invoke(invocation())

    assert result.status is ToolStatus.FAILED
    assert result.error == "当前用户未绑定银行客户号"
    assert result.retryable is False


def test_a_failure_the_module_calls_transient_stays_retryable():
    def handler(inv, client):
        raise ToolFailure(ErrorCode.UPSTREAM_UNAVAILABLE, "稍后重试", retryable=True)

    result = adapter_for(handler).invoke(invocation())

    assert result.retryable is True


def test_a_bank_failure_is_reported_in_chinese_not_the_banks_english():
    def handler(inv, client):
        response = client.request("GET", "/accounts", user_id="u_zhang")
        return ToolData(summary=str(response.data), display={}, facts={})

    def transport(request):
        return httpx.Response(
            404, json={"code": 1004, "message": "resource_not_found", "data": None}
        )

    client = BankClient("http://bank.test", transport=httpx.MockTransport(transport))
    result = adapter_for(handler, client=client).invoke(invocation())

    assert result.status is ToolStatus.FAILED
    assert result.error == "没有找到对应的银行记录。"
    assert "resource_not_found" not in (result.error or "")


def test_an_unexpected_exception_does_not_leak_its_text_to_the_client():
    def handler(inv, client):
        raise RuntimeError("psycopg: password=secret failed")

    result = adapter_for(handler).invoke(invocation())

    assert result.status is ToolStatus.FAILED
    assert result.error == "工具内部错误"
    assert "secret" not in (result.error or "")


def test_an_undeclared_tool_is_never_routed_to_a_handler():
    result = adapter_for(lambda inv, client: ToolData(summary="ok", display={}, facts={})).invoke(
        ToolInvocation(
            agent_id="alpha",
            call=ToolCall(tool_name="ghost", arguments={}, call_id="c1"),
            context=CONTEXT,
        )
    )

    assert result.status is ToolStatus.FAILED
    assert "ghost" in (result.error or "")
