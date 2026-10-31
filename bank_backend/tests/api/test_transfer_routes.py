"""账户、联系人、转账、定时任务、AA 与时钟的 API 测试。

覆盖计划要求：为每个写接口实现成功、基础失败和幂等重试测试。
"""

from datetime import timedelta

BASE = "/api"


def _ok(response) -> dict:
    assert response.status_code < 400, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "success"
    return body["data"]


def _error(response, status: int, code: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["data"] is None
    return body


# ---------------------------------------------------------------- Accounts


def test_list_accounts_returns_the_users_own_accounts(seeded_client):
    # /accounts 是"当前用户的账户列表"，默认用户 u_zhang 只有活期和定期两个账户。
    data = _ok(seeded_client.get(f"{BASE}/accounts"))
    assert {item["account_id"] for item in data} == {"a_zhang_current", "a_zhang_fixed"}
    current = next(item for item in data if item["account_id"] == "a_zhang_current")
    assert current["balance_minor"] == 1250050
    assert current["available_minor"] == 1250050
    assert current["currency"] == "CNY"
    assert current["account_type"] == "current"

    other = _ok(seeded_client.get(f"{BASE}/accounts", params={"user_id": "u_partner"}))
    assert {item["account_id"] for item in other} == {"a_partner_current"}


def test_list_balances_route_is_not_shadowed_by_account_id(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/accounts/balance"))
    assert isinstance(data, list)
    assert all("balance_minor" in item for item in data)


def test_get_account_and_balance(seeded_client):
    account = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current"))
    assert account["account_id"] == "a_zhang_current"
    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance == {
        "account_id": "a_zhang_current",
        "balance_minor": 1250050,
        "available_minor": 1250050,
        "currency": "CNY",
    }


def test_unknown_account_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/accounts/a_missing"), 404, 1004)


def test_unknown_user_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/accounts", params={"user_id": "u_nobody"}), 404, 1004)


def test_seeded_transactions_are_paginated_and_filtered(seeded_client):
    # 张三名下 9 笔：大额消费 1 + 餐饮 5 + 转出 1 + 订阅 1 + 理财收益 1。
    data = _ok(seeded_client.get(f"{BASE}/accounts/transactions", params={"page_size": 3}))
    assert data["page_size"] == 3
    assert len(data["items"]) == 3
    assert data["total"] == 9
    assert data["has_more"] is True

    dining = _ok(
        seeded_client.get(f"{BASE}/accounts/transactions", params={"category": "dining"})
    )
    assert dining["total"] == 5
    assert {item["category"] for item in dining["items"]} == {"dining"}

    interest = _ok(
        seeded_client.get(
            f"{BASE}/accounts/transactions", params={"transaction_type": "interest"}
        )
    )
    assert interest["total"] == 1
    assert interest["items"][0]["direction"] == "credit"


def test_transactions_can_be_filtered_by_date_range(seeded_client):
    data = _ok(
        seeded_client.get(
            f"{BASE}/accounts/a_zhang_current/transactions",
            params={"start_date": "2026-09-19", "end_date": "2026-09-19"},
        )
    )
    assert data["total"] == 5
    assert {item["category"] for item in data["items"]} == {"dining"}


def test_empty_date_range_returns_no_rows(seeded_client):
    data = _ok(
        seeded_client.get(
            f"{BASE}/accounts/transactions",
            params={"start_date": "2020-01-01", "end_date": "2020-01-31"},
        )
    )
    assert data["total"] == 0
    assert data["items"] == []


def test_invalid_page_size_returns_422(seeded_client):
    _error(seeded_client.get(f"{BASE}/accounts/transactions", params={"page_size": 0}), 422, 1012)


# ---------------------------------------------------------------- Contacts


def test_search_contacts_matches_name_phone_and_remark(seeded_client):
    by_name = _ok(seeded_client.get(f"{BASE}/contacts", params={"q": "爱人"}))
    assert [item["contact_id"] for item in by_name] == ["c_partner"]

    by_phone = _ok(seeded_client.get(f"{BASE}/contacts", params={"q": "13800138001"}))
    assert [item["contact_id"] for item in by_phone] == ["c_colleague"]

    by_remark = _ok(seeded_client.get(f"{BASE}/contacts", params={"q": "朋友"}))
    assert [item["contact_id"] for item in by_remark] == ["c_friend"]


def test_contacts_carry_the_recipient_account_and_birthday(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/contacts", params={"q": "爱人"}))
    assert data[0]["recipient_account_id"] == "a_partner_current"
    assert data[0]["birthday"] == "1995-09-25"


def test_contacts_without_a_query_lists_every_contact(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/contacts"))
    # 精确断言（含顺序）：seed 里 u_zhang 的三个联系人按 id 升序返回（字符串主键，字典序）。
    assert [item["contact_id"] for item in data] == ["c_colleague", "c_friend", "c_partner"]

    # 用户隔离：u_partner 没有联系人。若 user_id 过滤条件被去掉，这里会返回 u_zhang 的三条。
    assert _ok(seeded_client.get(f"{BASE}/contacts", params={"user_id": "u_partner"})) == []


# ---------------------------------------------------------------- Transfers


def test_instant_transfer_succeeds(seeded_client):
    data = _ok(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "user_id": "u_zhang",
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "remark": "生活费",
                "confirm": True,
            },
        )
    )
    assert data["status"] == "completed"
    assert data["permission_level"] == "green"

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1230050


def test_transfer_with_insufficient_balance_returns_409(seeded_client):
    # 先满足 red 级别的确认要求，才能走到余额不足这一层。
    _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 99999999,
                "confirm": True,
                "mfa_code": "123456",
            },
        ),
        409,
        1006,
    )


def test_transfer_to_unknown_account_returns_404(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_missing",
                "amount_minor": 100,
            },
        ),
        404,
        1004,
    )


def test_transfer_over_the_yellow_threshold_returns_pending_confirmation(seeded_client):
    body = _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 100000,
            },
        ),
        409,
        1007,
    )
    assert body["message"] == "pending_confirmation"


def test_transfer_with_a_negative_amount_returns_422(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/transfers",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": -1,
            },
        ),
        422,
        1012,
    )


def test_idempotent_retry_via_header_does_not_double_charge(seeded_client):
    payload = {
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 20000,
    }
    headers = {"Idempotency-Key": "transfer-header-001"}
    first = _ok(seeded_client.post(f"{BASE}/transfers", json=payload, headers=headers))
    second = _ok(seeded_client.post(f"{BASE}/transfers", json=payload, headers=headers))

    assert first["transaction_id"] == second["transaction_id"]
    assert first["replayed"] is False
    assert second["replayed"] is True
    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1230050


def test_idempotent_retry_via_body_key_does_not_double_charge(seeded_client):
    payload = {
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 20000,
        "idempotent_key": "transfer-body-001",
    }
    _ok(seeded_client.post(f"{BASE}/transfers", json=payload))
    _ok(seeded_client.post(f"{BASE}/transfers", json=payload))
    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1230050


# ---------------------------------------------------------------- Scheduled


def test_scheduled_transfer_lifecycle_over_the_api(seeded_client):
    execute_at = "2026-09-21T10:00:00+08:00"
    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/scheduled",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "execute_at": execute_at,
                "remark": "定时生活费",
            },
        )
    )
    assert created["status"] == "scheduled"
    task_id = created["task_id"]

    listed = _ok(seeded_client.get(f"{BASE}/transfers/scheduled"))
    assert [item["task_id"] for item in listed] == [task_id]

    detail = _ok(seeded_client.get(f"{BASE}/transfers/scheduled/{task_id}"))
    assert detail["task_id"] == task_id

    cancelled = _ok(seeded_client.post(f"{BASE}/transfers/scheduled/{task_id}/cancel"))
    assert cancelled["status"] == "cancelled"


def test_scheduled_transfer_executes_after_the_clock_advances(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/scheduled",
            json={
                "from_account_id": "a_zhang_current",
                "to_account_id": "a_partner_current",
                "amount_minor": 20000,
                "execute_at": "2026-09-21T10:00:00+08:00",
            },
        )
    )
    assert _ok(seeded_client.post(f"{BASE}/clock/run-tasks"))["processed_count"] == 0

    _ok(seeded_client.post(f"{BASE}/clock", json={"current_time": "2026-09-22T10:00:00+08:00"}))
    run = _ok(seeded_client.post(f"{BASE}/clock/run-tasks"))
    assert run["completed_count"] == 1

    detail = _ok(seeded_client.get(f"{BASE}/transfers/scheduled/{created['task_id']}"))
    assert detail["status"] == "completed"

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1230050


def test_unknown_task_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/transfers/scheduled/task_missing"), 404, 1004)


# ---------------------------------------------------------------- Clock


def test_clock_reports_the_seeded_time_and_run_id(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/clock"))
    assert data["current_time"].startswith("2026-09-20T10:00:00")
    assert data["due_task_count"] == 0


def test_clock_cannot_be_moved_backwards(seeded_client):
    _error(
        seeded_client.post(f"{BASE}/clock", json={"current_time": "2026-09-01T10:00:00+08:00"}),
        409,
        1005,
    )


def test_clock_accepts_a_naive_datetime_as_cst(seeded_client):
    data = _ok(seeded_client.post(f"{BASE}/clock", json={"current_time": "2026-09-25T10:00:00"}))
    assert data["current_time"].startswith("2026-09-25T10:00:00")


# ---------------------------------------------------------------- AA


def test_aa_create_get_and_pay_over_the_api(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/aa",
            json={
                "from_account_id": "a_zhang_current",
                "total_amount_minor": 10000,
                "participants": [
                    {"name": "同事1", "phone": "13800138001", "account_id": "a_li_current"},
                    {"name": "朋友1"},
                    {"name": "朋友2"},
                ],
                "remark": "聚餐",
            },
        )
    )
    assert created["status"] == "open"
    assert [item["amount_minor"] for item in created["participants"]] == [3333, 3333, 3334]

    aa_id = created["aa_request_id"]
    assert _ok(seeded_client.get(f"{BASE}/transfers/aa/{aa_id}"))["aa_request_id"] == aa_id
    # 参与者也能看到这笔 AA：他需要知道自己被分摊了多少。
    assert _ok(
        seeded_client.get(f"{BASE}/transfers/aa/{aa_id}", params={"user_id": "u_li"})
    )["aa_request_id"] == aa_id

    # 分摊项 0 绑的是李四的账户，所以只能由李四自己付。张三替他付必须被拒绝——
    # 否则「AA」就成了「谁都能动别人账户」。
    _error(
        seeded_client.post(
            f"{BASE}/transfers/aa/{aa_id}/pay",
            json={"participant_index": 0, "confirm": True, "user_id": "u_zhang"},
        ),
        404,
        1004,
    )

    paid = _ok(
        seeded_client.post(
            f"{BASE}/transfers/aa/{aa_id}/pay",
            json={"participant_index": 0, "confirm": True, "user_id": "u_li"},
        )
    )
    assert paid["status"] == "paid"
    assert paid["aa_status"] == "partial"

    # 同理，付完之后李四的余额只能由李四来读。
    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_li_current/balance", params={"user_id": "u_li"}))
    assert balance["balance_minor"] == 850000 - 3333


def test_aa_pay_without_confirm_returns_409(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/aa",
            json={
                "total_amount_minor": 10000,
                "participants": [
                    {"name": "同事1", "account_id": "a_li_current"},
                    {"name": "朋友1"},
                ],
            },
        )
    )
    _error(
        seeded_client.post(
            f"{BASE}/transfers/aa/{created['aa_request_id']}/pay",
            json={"participant_index": 0, "confirm": False},
        ),
        409,
        1005,
    )


def test_aa_with_a_single_participant_returns_422(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/transfers/aa",
            json={"total_amount_minor": 10000, "participants": [{"name": "同事1"}]},
        ),
        422,
        1012,
    )


def test_aa_cancel_returns_the_cancelled_request(seeded_client):
    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/aa",
            json={
                "total_amount_minor": 10000,
                "participants": [{"name": "同事1"}, {"name": "朋友1"}],
            },
        )
    )
    cancelled = _ok(
        seeded_client.post(f"{BASE}/transfers/aa/{created['aa_request_id']}/cancel")
    )
    assert cancelled["status"] == "cancelled"


def test_aa_expires_once_the_clock_passes_the_deadline(seeded_client):
    from datetime import datetime

    created = _ok(
        seeded_client.post(
            f"{BASE}/transfers/aa",
            json={
                "total_amount_minor": 10000,
                "participants": [
                    {"name": "同事1", "account_id": "a_li_current"},
                    {"name": "朋友1"},
                ],
            },
        )
    )
    assert created["status"] == "open"

    beyond = datetime.fromisoformat(created["expires_at"]) + timedelta(days=1)
    _ok(seeded_client.post(f"{BASE}/clock", json={"current_time": beyond.isoformat()}))

    assert _ok(seeded_client.get(f"{BASE}/transfers/aa/{created['aa_request_id']}"))[
        "status"
    ] == "expired"
    _error(
        seeded_client.post(
            f"{BASE}/transfers/aa/{created['aa_request_id']}/pay",
            json={"participant_index": 0, "confirm": True},
        ),
        409,
        1005,
    )
