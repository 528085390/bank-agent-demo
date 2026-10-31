"""Pins Write Facade policy: bank_customer_id, idempotency key, code-not-in-audit/state."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integration.bank_client import BankClient
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.integration.write_facade import (
    SECRET_BODY_KEYS,
    WriteFacade,
    WritePolicyError,
    audit_safe_body,
    require_customer_id,
    require_idempotency_key,
)
from app.tools.protocol import ErrorCode, TrustedContext


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _client(handler) -> tuple[BankClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    return BankClient("http://bank.test", transport=transport), transport


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"transaction_id": "TX1"}})


def _bound(customer: str = "u_customer") -> TrustedContext:
    return TrustedContext(user_id="app_user", session_id="s1", bank_customer_id=customer)


def test_require_customer_id_fails_closed_when_unbound() -> None:
    with pytest.raises(WritePolicyError) as caught:
        require_customer_id(TrustedContext(user_id="u", session_id="s"))
    assert caught.value.code == ErrorCode.NO_CUSTOMER_BINDING


def test_require_idempotency_key_rejects_empty() -> None:
    for key in ("", "   ", None):
        with pytest.raises(WritePolicyError) as caught:
            require_idempotency_key(key)  # type: ignore[arg-type]
        assert caught.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED


def test_fund_write_injects_bank_customer_id_as_user_id() -> None:
    client, transport = _client(_ok)
    facade = WriteFacade(client)

    facade.send(
        "POST",
        "/transfers",
        context=_bound("u_real_customer"),
        json={"from_account_id": "A1", "amount_minor": 100, "confirm": True},
        idempotency_key="idem-transfer-1",
        operation="transfer",
    )

    request = transport.requests[0]
    assert "user_id=u_real_customer" in str(request.url)
    assert b'"user_id":"u_real_customer"' in request.content
    assert request.headers.get("Idempotency-Key") == "idem-transfer-1"


def test_fund_write_without_customer_is_not_sent() -> None:
    client, transport = _client(_ok)
    facade = WriteFacade(client)

    with pytest.raises(WritePolicyError) as caught:
        facade.send(
            "POST",
            "/transfers",
            context=TrustedContext(user_id="u", session_id="s", bank_customer_id=None),
            json={"confirm": True},
            idempotency_key="idem-1",
        )

    assert caught.value.code == ErrorCode.NO_CUSTOMER_BINDING
    assert transport.requests == []


def test_fund_write_without_idempotency_key_is_not_sent() -> None:
    client, transport = _client(_ok)
    facade = WriteFacade(client)

    with pytest.raises(WritePolicyError) as caught:
        facade.send(
            "POST",
            "/products/purchase",
            context=_bound(),
            json={"confirm": True, "mfa_code": "246810"},
            idempotency_key="",
        )

    assert caught.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    assert transport.requests == []


def test_verification_code_is_redacted_from_audit_and_not_retained_on_facade() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    client, _transport = _client(_ok)
    facade = WriteFacade(
        client, audit_hook=lambda name, payload: events.append((name, dict(payload)))
    )

    body = {
        "product_id": "p1",
        "amount_minor": 100,
        "confirm": True,
        "mfa_code": "246810",
        "verification_code": "should-also-hide",
    }
    facade.send(
        "POST",
        "/products/purchase",
        context=_bound(),
        json=body,
        idempotency_key="idem-wealth-1",
        operation="purchase",
    )

    assert not hasattr(facade, "mfa_code")
    assert not hasattr(facade, "verification_code")
    assert "246810" not in repr(facade.__dict__)

    assert events, "audit hook must fire"
    for _name, payload in events:
        assert "246810" not in str(payload)
        assert "should-also-hide" not in str(payload)
        audited_body = payload["body"]
        assert audited_body["mfa_code"] == "***"
        assert audited_body["verification_code"] == "***"
        assert audited_body["product_id"] == "p1"

    # Raw body for the HTTP call still carries the code (bank needs it); only audit is redacted.
    safe = audit_safe_body(body)
    assert safe["mfa_code"] == "***"
    assert set(SECRET_BODY_KEYS) <= {"mfa_code", "verification_code"}


def test_audit_payload_never_looks_like_checkpoint_state() -> None:
    """Facade audit is a flat event dict: no graph state, no pending snapshot, no code field."""

    events: list[dict[str, Any]] = []
    client, _ = _client(_ok)
    facade = WriteFacade(client, audit_hook=lambda _n, payload: events.append(dict(payload)))

    facade.send(
        "POST",
        "/transfers",
        context=_bound(),
        json={"confirm": True, "mfa_code": "999999"},
        idempotency_key="idem-2",
    )

    payload = events[0]
    forbidden = {"messages", "checkpoint", "pending", "verification_code", "mfa_code"}
    assert forbidden.isdisjoint(payload)
    assert payload["body"]["mfa_code"] == "***"
    assert "999999" not in str(payload)


def test_bank_errors_pass_through_unchanged() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": 1006, "message": "nope", "data": None})

    client, _ = _client(boom)
    facade = WriteFacade(client)

    with pytest.raises(BankCallError) as caught:
        facade.send(
            "POST",
            "/transfers",
            context=_bound(),
            json={"confirm": True},
            idempotency_key="idem-3",
        )

    assert caught.value.kind is BankErrorKind.INSUFFICIENT_BALANCE


def test_wealth_adapter_routes_purchase_through_facade() -> None:
    """HttpWealthBank fund writes use the facade: customer_id + key pinned on the wire."""
    from app.modules.wealth.contracts.bank import PurchaseIntent
    from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
    from tests.wealth import support

    double = support.BankDouble()
    double.on("POST", "/products/purchase", data=support.purchase_receipt_payload(), status=201)
    audits: list[str] = []
    bank = HttpWealthBank(
        double.client(),
        writes=WriteFacade(double.client(), audit_hook=lambda n, _p: audits.append(n)),
    )

    bank.purchase(
        support.bound_context(),
        PurchaseIntent(
            product_id="p_demo_cash",
            from_account_id="a_wealth_main",
            amount_minor=200000,
        ),
        "246810",
        "idem-facade-1",
    )

    call = double.only_call()
    assert call.body is not None
    assert call.body["user_id"] == support.DEMO_CUSTOMER
    assert call.idempotency_key == "idem-facade-1"
    assert call.body["mfa_code"] == "246810"
    assert audits == ["fund_write_attempt", "fund_write_success"]
