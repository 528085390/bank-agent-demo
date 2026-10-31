"""The renewal-reminder surface.

A reminder is only useful if it is *before* the charge and only mentions subscriptions that
will actually charge: paused subscriptions and manual renewals are not reminders, and a due
date outside the requested horizon is not due yet. These tests pin those rules, the customer
binding, and the bank-failure translation at the HTTP seam.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.integration.bank_client import BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.main import create_app


class FakeBank:
    def __init__(self, reply=None, *, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        self.calls.append({"method": method, "path": path, "user_id": user_id})
        if self.error is not None:
            raise self.error
        return BankResponse(code=0, message="success", data=self.reply)


def client_for(rt) -> TestClient:
    issued = rt.identity.create_demo_session()
    return TestClient(
        create_app(rt.settings, runtime=rt),
        headers={"Authorization": "Bearer " + issued["token"]},
    )


def subscription(**overrides):
    row = {
        "subscription_id": "sub_x",
        "name": "视频会员",
        "merchant_name": "示例视频",
        "amount_minor": 2500,
        "frequency": "monthly",
        "next_due_date": "2026-11-01",
        "status": "active",
        "auto_renew": True,
    }
    row.update(overrides)
    return row


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.api.reminders.SystemClock.today", lambda: date(2026, 10, 30))


def test_lists_only_active_auto_renewals_due_within_the_horizon(rt, monkeypatch, fixed_today):
    fake = FakeBank(
        reply=[
            subscription(subscription_id="sub_soon", next_due_date="2026-10-31", amount_minor=1999),
            subscription(subscription_id="sub_today", next_due_date="2026-10-30", amount_minor=500),
            subscription(subscription_id="sub_paused", status="paused", next_due_date="2026-10-31"),
            subscription(subscription_id="sub_manual", auto_renew=False, next_due_date="2026-10-31"),
            subscription(subscription_id="sub_later", next_due_date="2026-11-20"),
            subscription(subscription_id="sub_undated", next_due_date=None),
        ]
    )
    monkeypatch.setattr(rt, "bank_client", fake)

    response = client_for(rt).get("/api/reminders/subscriptions")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["count"] == 2
    assert [item["subscription_id"] for item in data["items"]] == ["sub_today", "sub_soon"]
    assert [item["days_until_due"] for item in data["items"]] == [0, 1]
    assert data["total_minor"] == 2499
    assert data["horizon_days"] == 14
    assert fake.calls == [
        {
            "method": "GET",
            "path": "/subscriptions",
            "user_id": rt.identity.bank_customer_id("user_0001"),
        }
    ]


def test_the_horizon_is_a_query_parameter(rt, monkeypatch, fixed_today):
    fake = FakeBank(
        reply=[
            subscription(subscription_id="sub_soon", next_due_date="2026-10-31"),
            subscription(subscription_id="sub_later", next_due_date="2026-11-20"),
        ]
    )
    monkeypatch.setattr(rt, "bank_client", fake)

    response = client_for(rt).get("/api/reminders/subscriptions", params={"horizon_days": 1})

    assert response.status_code == 200, response.text
    assert [item["subscription_id"] for item in response.json()["data"]["items"]] == ["sub_soon"]


def test_an_unbound_customer_is_refused_before_the_bank_is_called(rt, monkeypatch):
    fake = FakeBank(reply=[])
    monkeypatch.setattr(rt, "bank_client", fake)
    monkeypatch.setattr(rt.identity, "bank_customer_id", lambda user_id: None)

    response = client_for(rt).get("/api/reminders/subscriptions")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CUSTOMER_BINDING_REQUIRED"
    assert fake.calls == []


def test_a_bank_failure_is_a_502_not_a_500(rt, monkeypatch, fixed_today):
    fake = FakeBank(error=BankCallError(BankErrorKind.SERVER, "银行暂时不可用", retryable=True))
    monkeypatch.setattr(rt, "bank_client", fake)

    response = client_for(rt).get("/api/reminders/subscriptions")

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "BANK_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True


def test_a_non_list_subscription_response_is_refused(rt, monkeypatch, fixed_today):
    fake = FakeBank(reply={"items": []})
    monkeypatch.setattr(rt, "bank_client", fake)

    response = client_for(rt).get("/api/reminders/subscriptions")

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "BANK_UNAVAILABLE"
