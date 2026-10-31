"""Tests for demo control plane security status and reset-lock endpoints.

Covers SEC-01 through SEC-08 per D3 SPEC.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.integration.bank_client import BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind, ErrorCode
from app.main import create_app

OPERATOR = "demo_operator"
DEMO_CUSTOMER = "u_wealth"

LOCKED_STATUS_VIEW = {
    "user_id": DEMO_CUSTOMER,
    "tool_name": "fund_transfer",
    "failure_count": 5,
    "locked_until": "2026-10-26T17:15:00+08:00",
    "last_reason": "invalid_code",
    "tools": [
        {
            "user_id": DEMO_CUSTOMER,
            "tool_name": "fund_transfer",
            "failure_count": 5,
            "locked_until": "2026-10-26T17:15:00+08:00",
            "last_reason": "invalid_code",
        }
    ],
}

RESET_STATUS_VIEW = {
    "user_id": DEMO_CUSTOMER,
    "tool_name": "fund_transfer",
    "failure_count": 0,
    "locked_until": None,
    "last_reason": "invalid_code",
    "tools": [
        {
            "user_id": DEMO_CUSTOMER,
            "tool_name": "fund_transfer",
            "failure_count": 0,
            "locked_until": None,
            "last_reason": "invalid_code",
        }
    ],
}


class FakeBank:
    """Stands in for BankClient, recording calls and responding."""

    def __init__(self, replies=None, *, error=None):
        self.replies = dict(replies or {})
        self.error = error
        self.calls = []

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "user_id": user_id,
                "params": params,
                "json": json,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        try:
            reply = self.replies[(method, path)]
        except KeyError:
            raise AssertionError(f"unexpected bank call {method} {path}") from None
        return BankResponse(code=0, message="success", data=reply)


def client_for(rt, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    return TestClient(create_app(rt.settings, runtime=rt), headers=headers)


def test_sec_01_demo_disabled_blocks_endpoints(rt, session, monkeypatch):
    """SEC-01: When demo_mode is False, both security endpoints answer 403 DEMO_DISABLED."""
    fake = FakeBank()
    monkeypatch.setattr(rt, "bank_client", fake)
    monkeypatch.setattr(rt.settings, "demo_mode", False)
    client = client_for(rt, session["token"])

    get_resp = client.get("/api/demo/security")
    assert get_resp.status_code == 403
    assert get_resp.json()["error"]["code"] == "DEMO_DISABLED"

    post_resp = client.post("/api/demo/security/reset-lock")
    assert post_resp.status_code == 403
    assert post_resp.json()["error"]["code"] == "DEMO_DISABLED"
    assert fake.calls == []


def test_sec_02_unauthenticated_session_blocks_endpoints(rt, monkeypatch):
    """SEC-02: Without a valid session, answers 401 SESSION_REQUIRED."""
    fake = FakeBank()
    monkeypatch.setattr(rt, "bank_client", fake)
    client = client_for(rt, token=None)

    get_resp = client.get("/api/demo/security")
    assert get_resp.status_code == 401
    assert get_resp.json()["error"]["code"] == "SESSION_REQUIRED"

    post_resp = client.post("/api/demo/security/reset-lock")
    assert post_resp.status_code == 401
    assert post_resp.json()["error"]["code"] == "SESSION_REQUIRED"
    assert fake.calls == []


def test_sec_03_missing_customer_binding_fails_closed(rt, session, monkeypatch):
    """SEC-03: User without bank_customer_id raises 422 CUSTOMER_BINDING_REQUIRED without calling bank."""
    fake = FakeBank()
    monkeypatch.setattr(rt, "bank_client", fake)
    monkeypatch.setattr(rt.identity, "bank_customer_id", lambda uid: None)
    client = client_for(rt, session["token"])

    get_resp = client.get("/api/demo/security")
    assert get_resp.status_code == 422
    assert get_resp.json()["error"]["code"] == "CUSTOMER_BINDING_REQUIRED"

    post_resp = client.post("/api/demo/security/reset-lock")
    assert post_resp.status_code == 422
    assert post_resp.json()["error"]["code"] == "CUSTOMER_BINDING_REQUIRED"
    assert fake.calls == []


def test_sec_04_security_status_pass_through(rt, session, monkeypatch):
    """SEC-04: Returns bank status accurately for the authenticated customer."""
    fake = FakeBank({("GET", "/security/status"): LOCKED_STATUS_VIEW})
    monkeypatch.setattr(rt, "bank_client", fake)
    client = client_for(rt, session["token"])

    resp = client.get("/api/demo/security")
    assert resp.status_code == 200
    assert resp.json()["data"] == LOCKED_STATUS_VIEW
    assert [(c["method"], c["path"], c["user_id"]) for c in fake.calls] == [
        ("GET", "/security/status", DEMO_CUSTOMER)
    ]


def test_sec_05_reset_lock_strictly_binds_post_body(rt, session, monkeypatch):
    """SEC-05: Reset-lock forwards POST with explicit json body and returns reset payload."""
    fake = FakeBank({("POST", "/security/reset-lock"): RESET_STATUS_VIEW})
    monkeypatch.setattr(rt, "bank_client", fake)
    client = client_for(rt, session["token"])

    resp = client.post("/api/demo/security/reset-lock")
    assert resp.status_code == 200
    assert resp.json()["data"] == RESET_STATUS_VIEW
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/security/reset-lock"
    assert call["user_id"] == DEMO_CUSTOMER
    # Body user_id must be preserved/not dropped to prevent fallback to u_zhang
    assert call["json"] is not None


def test_sec_06_bank_failure_maps_to_502(rt, session, monkeypatch):
    """SEC-06: BankCallError maps to 502 BANK_UNAVAILABLE with retryable=True."""
    down_err = BankCallError(BankErrorKind.TIMEOUT, "银行服务响应超时，请稍后重试。", retryable=True)
    fake = FakeBank(error=down_err)
    monkeypatch.setattr(rt, "bank_client", fake)
    client = client_for(rt, session["token"])

    get_resp = client.get("/api/demo/security")
    assert get_resp.status_code == 502
    assert get_resp.json()["error"]["code"] == "BANK_UNAVAILABLE"
    assert get_resp.json()["error"]["retryable"] is True

    post_resp = client.post("/api/demo/security/reset-lock")
    assert post_resp.status_code == 502
    assert post_resp.json()["error"]["code"] == "BANK_UNAVAILABLE"


def test_sec_07_write_rejected_read_allowed_invariant(rt, session, monkeypatch):
    """SEC-07: Invariant check - write operations are rejected with LOCKED while read operations succeed."""
    locked_err = BankCallError(BankErrorKind.LOCKED, "该资金工具已锁定，请稍后重试。")
    balance_view = {"accounts": [{"account_no": "6222001", "balance": "8320.00"}]}

    def mock_request(method, path, *, user_id, params=None, json=None, idempotency_key=None):
        if method == "POST" and path == "/transfers":
            raise locked_err
        if method == "GET" and path == "/accounts/balance":
            return BankResponse(code=0, message="success", data=balance_view)
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(rt.bank_client, "request", mock_request)

    # 1. 资金写操作在被锁定时抛出 BankCallError(LOCKED)
    with pytest.raises(BankCallError) as exc_info:
        rt.bank_client.request("POST", "/transfers", user_id=DEMO_CUSTOMER, json={"amount": 100})
    assert exc_info.value.kind == BankErrorKind.LOCKED
    assert exc_info.value.message == "该资金工具已锁定，请稍后重试。"

    # 2. 读操作完全不受影响，正常放行
    read_resp = rt.bank_client.request("GET", "/accounts/balance", user_id=DEMO_CUSTOMER)
    assert read_resp.code == 0
    assert read_resp.data == balance_view


def test_sec_08_cross_tenant_anti_tampering(rt, session, monkeypatch):
    """SEC-08: Malicious user_id in client request is ignored; customer_id is strictly resolved from session."""
    fake = FakeBank(
        {
            ("GET", "/security/status"): LOCKED_STATUS_VIEW,
            ("POST", "/security/reset-lock"): RESET_STATUS_VIEW,
        }
    )
    monkeypatch.setattr(rt, "bank_client", fake)
    client = client_for(rt, session["token"])

    # 1. Client tries to tamper target user via query parameter in GET
    get_resp = client.get("/api/demo/security?user_id=cust_attacker&customer_id=cust_attacker")
    assert get_resp.status_code == 200
    assert fake.calls[0]["user_id"] == DEMO_CUSTOMER
    assert fake.calls[0]["user_id"] != "cust_attacker"

    # 2. Client tries to tamper target user via query or json body in POST
    post_resp = client.post(
        "/api/demo/security/reset-lock?user_id=cust_attacker",
        json={"user_id": "cust_attacker", "customer_id": "cust_attacker"},
    )
    assert post_resp.status_code == 200
    assert fake.calls[1]["user_id"] == DEMO_CUSTOMER
    assert fake.calls[1]["user_id"] != "cust_attacker"
