"""卡片、订阅与生日联动的 API 测试。"""

BASE = "/api"


def _ok(response) -> dict:
    assert response.status_code < 400, response.text
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def _error(response, status: int, code: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["data"] is None
    return body


def _advance(client, moment: str) -> dict:
    return _ok(client.post(f"{BASE}/clock", json={"current_time": moment}))


# ---------------------------------------------------------------- Cards


def test_list_and_get_cards(seeded_client):
    listed = _ok(seeded_client.get(f"{BASE}/cards"))
    assert {item["card_id"] for item in listed} == {"card_zhang_debit", "card_zhang_virtual"}
    detail = _ok(seeded_client.get(f"{BASE}/cards/card_zhang_debit"))
    assert detail["status"] == "active"
    assert "****" in detail["card_number"]


def test_apply_card_route_is_not_shadowed_by_card_id(seeded_client):
    response = seeded_client.post(f"{BASE}/cards/apply", json={"card_type": "credit"})
    _error(response, 422, 1012)


def test_apply_card_with_a_bad_type_returns_422(seeded_client):
    _error(seeded_client.post(f"{BASE}/cards/apply", json={"card_type": "platinum"}), 422, 1012)


def test_card_freeze_unfreeze_and_block(seeded_client):
    frozen = _ok(seeded_client.post(f"{BASE}/cards/card_zhang_debit/freeze"))
    assert frozen["status"] == "frozen"
    assert _ok(seeded_client.post(f"{BASE}/cards/card_zhang_debit/unfreeze"))["status"] == (
        "active"
    )
    assert _ok(seeded_client.post(f"{BASE}/cards/card_zhang_debit/block"))["status"] == "blocked"
    _error(seeded_client.post(f"{BASE}/cards/card_zhang_debit/unfreeze"), 409, 1005)


def test_update_card_limit_and_restrictions(seeded_client):
    updated = _ok(
        seeded_client.post(
            f"{BASE}/cards/card_zhang_debit/limit",
            json={"daily_limit_minor": 200000, "monthly_limit_minor": 2000000},
        )
    )
    assert updated["daily_limit_minor"] == 200000

    restricted = _ok(
        seeded_client.post(
            f"{BASE}/cards/card_zhang_debit/restrictions",
            json={"merchant_blacklist": ["赌场", "赌场", "高风险商户"]},
        )
    )
    assert restricted["merchant_blacklist"] == ["赌场", "高风险商户"]

    fetched = _ok(seeded_client.get(f"{BASE}/cards/card_zhang_debit/restrictions"))
    assert fetched["daily_limit_minor"] == 200000
    assert fetched["merchant_blacklist"] == ["赌场", "高风险商户"]


def test_card_limit_validation_returns_422(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/cards/card_zhang_debit/limit",
            json={"daily_limit_minor": 5000, "monthly_limit_minor": 1000},
        ),
        422,
        1012,
    )


def test_unknown_card_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/cards/card_missing"), 404, 1004)


# ---------------------------------------------------------------- Subscriptions


def test_list_and_get_subscriptions(seeded_client):
    listed = _ok(seeded_client.get(f"{BASE}/subscriptions"))
    assert {item["subscription_id"] for item in listed} == {"sub_netflix", "sub_spotify"}
    detail = _ok(seeded_client.get(f"{BASE}/subscriptions/sub_netflix"))
    assert detail["amount_minor"] == 5900
    assert detail["next_due_date"] == "2026-10-10"
    assert detail["frequency"] == "monthly"


def test_create_pause_resume_and_cancel_subscription(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/subscriptions",
            json={
                "name": "iCloud",
                "merchant_name": "Apple",
                "amount_minor": 1200,
                "frequency": "monthly",
                "from_account_id": "a_zhang_current",
                "start_date": "2026-10-05",
            },
        )
    )
    subscription_id = created["subscription_id"]

    assert _ok(seeded_client.post(f"{BASE}/subscriptions/{subscription_id}/pause"))[
        "status"
    ] == "paused"
    assert _ok(seeded_client.post(f"{BASE}/subscriptions/{subscription_id}/resume"))[
        "status"
    ] == "active"
    assert _ok(seeded_client.post(f"{BASE}/subscriptions/{subscription_id}/cancel"))[
        "status"
    ] == "cancelled"
    _error(seeded_client.post(f"{BASE}/subscriptions/{subscription_id}/resume"), 409, 1005)


def test_charge_subscription_over_the_api(seeded_client):
    charged = _ok(seeded_client.post(f"{BASE}/subscriptions/sub_netflix/charge"))
    assert charged["status"] == "completed"
    assert charged["due_date"] == "2026-10-10"
    assert charged["transaction_id"].startswith("txn_")

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 5900

    detail = _ok(seeded_client.get(f"{BASE}/subscriptions/sub_netflix"))
    assert detail["next_due_date"] == "2026-11-10"


def test_charge_subscription_is_idempotent(seeded_client):
    headers = {"Idempotency-Key": "charge-header-001"}
    first = _ok(seeded_client.post(f"{BASE}/subscriptions/sub_netflix/charge", headers=headers))
    second = _ok(seeded_client.post(f"{BASE}/subscriptions/sub_netflix/charge", headers=headers))
    assert first["charge_id"] == second["charge_id"]
    assert second["replayed"] is True

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 5900


def test_charge_a_paused_subscription_returns_409(seeded_client):
    _ok(seeded_client.post(f"{BASE}/subscriptions/sub_netflix/pause"))
    _error(seeded_client.post(f"{BASE}/subscriptions/sub_netflix/charge"), 409, 1005)


def test_run_due_subscriptions_over_the_api(seeded_client):
    # 暂停 Spotify，把断言限定在 Netflix 的 5900 分上。
    _ok(seeded_client.post(f"{BASE}/subscriptions/sub_spotify/pause"))
    _advance(seeded_client, "2026-10-10T10:00:00+08:00")

    run = _ok(seeded_client.post(f"{BASE}/subscriptions/run-due"))
    assert run["completed_count"] == 1

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 5900


def test_run_due_subscriptions_ignores_transfer_tasks(seeded_client):
    _ok(
        seeded_client.post(
            f"{BASE}/transfers/scheduled",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "execute_at": "2026-09-25T10:00:00+08:00",
            },
        )
    )
    _advance(seeded_client, "2026-09-26T10:00:00+08:00")
    assert _ok(seeded_client.post(f"{BASE}/subscriptions/run-due"))["processed_count"] == 0
    # 定时转账随后仍由 /clock/run-tasks 执行。
    assert _ok(seeded_client.post(f"{BASE}/clock/run-tasks"))["completed_count"] == 1


def test_unknown_subscription_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/subscriptions/sub_missing"), 404, 1004)


# ---------------------------------------------------------------- Birthdays


def test_mock_products_are_exposed_over_the_api(seeded_client):
    products = _ok(seeded_client.get(f"{BASE}/mock-products"))
    assert {item["product_code"] for item in products} == {"flowers", "cake"}
    assert {item["price_minor"] for item in products} == {19900, 12900}


def test_birthday_contacts_over_the_api(seeded_client):
    contacts = _ok(seeded_client.get(f"{BASE}/birthdays"))
    assert [item["contact_id"] for item in contacts] == ["c_partner"]
    assert contacts[0]["birthday"] == "1995-09-25"


def test_birthday_order_lifecycle_over_the_api(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/birthdays/orders",
            json={
                "contact_id": "c_partner",
                "product_code": "flowers",
                "from_account_id": "a_zhang_current",
                "birthday_date": "2026-09-25",
            },
        )
    )
    assert created["status"] == "pending"
    assert created["amount_minor"] == 19900

    order_id = created["order_id"]
    assert _ok(seeded_client.get(f"{BASE}/birthdays/orders/{order_id}"))["order_id"] == order_id

    _advance(seeded_client, "2026-09-23T08:00:00+08:00")
    run = _ok(seeded_client.post(f"{BASE}/clock/run-tasks"))
    assert run["completed_count"] == 1

    executed = _ok(seeded_client.get(f"{BASE}/birthdays/orders/{order_id}"))
    assert executed["status"] == "completed"
    assert executed["transaction_id"].startswith("txn_")

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 19900


def test_birthday_order_mock_failure_over_the_api(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/birthdays/orders",
            json={
                "contact_id": "c_partner",
                "product_code": "cake",
                "from_account_id": "a_zhang_current",
                "birthday_date": "2026-09-25",
            },
        )
    )
    _advance(seeded_client, "2026-09-23T08:00:00+08:00")
    run = _ok(
        seeded_client.post(f"{BASE}/clock/run-tasks", json={"birthday_outcome": "fail"})
    )
    assert run["failed_count"] == 1

    order = _ok(seeded_client.get(f"{BASE}/birthdays/orders/{created['order_id']}"))
    assert order["status"] == "failed"
    assert order["transaction_id"] is None

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050


def test_cancel_birthday_order_over_the_api(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/birthdays/orders",
            json={
                "contact_id": "c_partner",
                "product_code": "flowers",
                "from_account_id": "a_zhang_current",
                "birthday_date": "2026-09-25",
            },
        )
    )
    cancelled = _ok(seeded_client.post(f"{BASE}/birthdays/orders/{created['order_id']}/cancel"))
    assert cancelled["status"] == "cancelled"

    _advance(seeded_client, "2026-09-23T08:00:00+08:00")
    assert _ok(seeded_client.post(f"{BASE}/clock/run-tasks"))["processed_count"] == 0
    _error(seeded_client.post(f"{BASE}/birthdays/orders/{created['order_id']}/cancel"), 409, 1005)


def test_unknown_birthday_order_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/birthdays/orders/order_missing"), 404, 1004)


def test_birthday_order_with_a_bad_product_code_returns_422(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/birthdays/orders",
            json={
                "contact_id": "c_partner",
                "product_code": "balloon",
                "from_account_id": "a_zhang_current",
                "birthday_date": "2026-09-25",
            },
        ),
        422,
        1012,
    )
