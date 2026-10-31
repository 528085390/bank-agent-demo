"""需求文档 §5「固定演示主路径」的八条验收。

每个用例对应一条主路径，从 API 入口走完整流程并断言业务 ID 与余额变化。
这里用 FastAPI 的 TestClient —— 它就是 httpx 之上的一层 ASGI transport，
因此请求/响应完全走真实的 HTTP 语义与路由匹配，同时不需要独占端口。
"""

import pytest

from tests.conftest import mfa_code

BASE = "/api"

HIGH_RISK_ANSWERS = {"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2}


@pytest.fixture
def api(seeded_client):
    return seeded_client


def call(api, method: str, path: str, **kwargs) -> dict:
    response = api.request(method, f"{BASE}{path}", **kwargs)
    assert response.status_code < 400, f"{method} {path} -> {response.status_code} {response.text}"
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def open_risk_profile(api, user_id: str = "u_zhang") -> dict:
    """理财申购要求本人有效测评：先走银行接口保存一份高风险问卷。"""

    return call(
        api,
        "POST",
        "/wealth/risk-assessments",
        json={"user_id": user_id, "answers": HIGH_RISK_ANSWERS},
    )


def balance(api, account_id: str, user_id: str = "u_zhang") -> int:
    # 余额只能由账户主人来读：跨用户读取现在按「不存在」拒绝，所以对端余额要以
    # 对端自己的身份查，而不是借发起人的身份去偷看。
    return call(
        api, "GET", f"/accounts/{account_id}/balance", params={"user_id": user_id}
    )["balance_minor"]


def advance(api, moment: str) -> dict:
    return call(api, "POST", "/clock", json={"current_time": moment})


# 1
def test_main_path_1_search_contact_then_transfer_200_yuan(api):
    contacts = call(api, "GET", "/contacts", params={"q": "爱人"})
    assert len(contacts) == 1
    target = contacts[0]
    assert target["recipient_account_id"] == "a_partner_current"

    before_source = balance(api, "a_zhang_current")
    before_target = balance(api, target["recipient_account_id"], user_id="u_partner")

    result = call(
        api,
        "POST",
        "/transfers",
        json={
            "from_account_id": "a_zhang_current",
            "to_account_id": target["recipient_account_id"],
            "amount_minor": 20000,
            "remark": "生活费",
            "confirm": True,
        },
    )
    assert result["status"] == "completed"
    assert result["transaction_id"].startswith("txn_")
    assert balance(api, "a_zhang_current") == before_source - 20000
    assert balance(api, target["recipient_account_id"], user_id="u_partner") == before_target + 20000

    sent = call(
        api,
        "GET",
        "/accounts/a_zhang_current/transactions",
        params={"transaction_type": "transfer", "page_size": 1},
    )
    assert sent["items"][0]["counterparty_account_id"] == target["recipient_account_id"]


# 2
def test_main_path_2_scheduled_transfer_survives_a_clock_advance(api):
    task = call(
        api,
        "POST",
        "/transfers/scheduled",
        json={
            "from_account_id": "a_zhang_current",
            "to_account_id": "a_partner_current",
            "amount_minor": 20000,
            "execute_at": "2026-09-21T10:00:00+08:00",
            "remark": "第二天生活费",
        },
    )
    assert task["status"] == "scheduled"

    assert call(api, "POST", "/clock/run-tasks")["processed_count"] == 0

    advance(api, "2026-09-21T10:00:00+08:00")
    run = call(api, "POST", "/clock/run-tasks")
    assert run["completed_count"] == 1
    assert run["items"][0]["task_id"] == task["task_id"]

    detail = call(api, "GET", f"/transfers/scheduled/{task['task_id']}")
    assert detail["status"] == "completed"
    assert detail["result"]["transaction_id"].startswith("txn_")

    transactions = call(
        api,
        "GET",
        "/accounts/a_zhang_current/transactions",
        params={"transaction_type": "transfer", "page_size": 5},
    )
    assert transactions["total"] >= 2
    assert balance(api, "a_zhang_current") == 1250050 - 20000


# 3
def test_main_path_3_birthday_flowers_order_for_the_partner(api):
    products = call(api, "GET", "/mock-products")
    flowers = next(item for item in products if item["product_code"] == "flowers")

    order = call(
        api,
        "POST",
        "/birthdays/orders",
        json={
            "contact_id": "c_partner",
            "product_code": "flowers",
            "from_account_id": "a_zhang_current",
            "birthday_date": "2026-09-25",
        },
    )
    assert order["status"] == "pending"
    assert order["amount_minor"] == flowers["price_minor"]

    advance(api, "2026-09-23T08:00:00+08:00")
    run = call(api, "POST", "/clock/run-tasks")
    assert run["completed_count"] == 1

    executed = call(api, "GET", f"/birthdays/orders/{order['order_id']}")
    assert executed["status"] == "completed"
    assert executed["transaction_id"].startswith("txn_")
    assert executed["details"]["product_code"] == "flowers"
    assert balance(api, "a_zhang_current") == 1250050 - flowers["price_minor"]


# 4
def test_main_path_4_monthly_bill_categories_and_anomalies(api):
    bill = call(api, "GET", "/bills/month", params={"month": "2026-09"})
    assert bill["period"] == "2026-09"
    assert bill["expense_minor"] > 0

    categories = call(
        api,
        "GET",
        "/bills/categories",
        params={"start_date": "2026-09-01", "end_date": "2026-09-30"},
    )
    assert categories["categories"]["dining"] == 10000

    analysis = call(
        api,
        "POST",
        "/bills/analyze",
        json={"start_date": "2026-09-01", "end_date": "2026-09-30"},
    )
    rule_ids = {item["rule_id"] for item in analysis["findings"]}
    assert rule_ids == {"large_amount", "frequent_small", "repeated_transaction"}
    for finding in analysis["findings"]:
        assert finding["transaction_ids"]


# 5
def test_main_path_5_purchase_hold_simulate_interest_then_redeem(api):
    products = call(api, "GET", "/products")
    assert any(item["product_id"] == "p_yuebao" for item in products)
    open_risk_profile(api)

    purchased = call(
        api,
        "POST",
        "/products/purchase",
        json={
            "from_account_id": "a_zhang_current",
            "product_id": "p_yuebao",
            "amount_minor": 100000,
            "confirm": True,
            "mfa_code": mfa_code(),
        },
    )
    assert purchased["status"] == "completed"
    assert balance(api, "a_zhang_current") == 1250050 - 100000

    holdings = call(api, "GET", "/holdings")
    assert [item["holding_id"] for item in holdings] == [purchased["holding_id"]]

    simulated = call(
        api,
        "POST",
        f"/holdings/{purchased['holding_id']}/simulate-interest",
        json={"as_of": "2027-09-20"},
    )
    assert simulated["accrued_interest_minor"] == 3500
    assert simulated["current_value_minor"] == 103500

    redeemed = call(
        api,
        "POST",
        "/products/redeem",
        json={
            "holding_id": purchased["holding_id"],
            "amount_minor": 103500,
            "confirm": True,
            "mfa_code": mfa_code(),
        },
    )
    assert redeemed["holding_status"] == "redeemed"
    assert redeemed["interest_minor"] == 3500
    assert balance(api, "a_zhang_current") == 1250050 - 100000 + 103500


# 6
def test_main_path_6_freeze_unfreeze_and_relimit_a_card(api):
    cards = call(api, "GET", "/cards")
    card = next(item for item in cards if item["card_id"] == "card_zhang_debit")
    assert card["status"] == "active"

    assert call(api, "POST", "/cards/card_zhang_debit/freeze")["status"] == "frozen"
    assert call(api, "POST", "/cards/card_zhang_debit/unfreeze")["status"] == "active"

    updated = call(
        api,
        "POST",
        "/cards/card_zhang_debit/limit",
        json={"daily_limit_minor": 300000, "monthly_limit_minor": 3000000},
    )
    assert updated["daily_limit_minor"] == 300000

    restrictions = call(api, "GET", "/cards/card_zhang_debit/restrictions")
    assert restrictions["daily_limit_minor"] == 300000


# 7
def test_main_path_7_charge_netflix_then_cancel_the_subscription(api):
    subscriptions = call(api, "GET", "/subscriptions")
    netflix = next(item for item in subscriptions if item["subscription_id"] == "sub_netflix")
    assert netflix["status"] == "active"
    assert netflix["amount_minor"] == 5900

    charged = call(api, "POST", "/subscriptions/sub_netflix/charge")
    assert charged["status"] == "completed"
    assert charged["transaction_id"].startswith("txn_")
    assert balance(api, "a_zhang_current") == 1250050 - 5900

    cancelled = call(api, "POST", "/subscriptions/sub_netflix/cancel")
    assert cancelled["status"] == "cancelled"

    # 取消后不再有后续代扣。Spotify（10-01 到期）也一并取消，把断言限定在 Netflix 上。
    call(api, "POST", "/subscriptions/sub_spotify/cancel")
    advance(api, "2026-11-10T10:00:00+08:00")
    assert call(api, "POST", "/subscriptions/run-due")["processed_count"] == 0
    assert balance(api, "a_zhang_current") == 1250050 - 5900


# 8
def test_main_path_8_audit_trail_exposes_request_and_business_ids(api):
    transfer = call(
        api,
        "POST",
        "/transfers",
        json={
            "from_account_id": "a_zhang_current",
            "to_account_id": "a_partner_current",
            "amount_minor": 20000,
            "confirm": True,
        },
    )
    open_risk_profile(api)
    purchase = call(
        api,
        "POST",
        "/products/purchase",
        json={
            "from_account_id": "a_zhang_current",
            "product_id": "p_yuebao",
            "amount_minor": 100000,
            "confirm": True,
            "mfa_code": mfa_code(),
        },
    )

    logs = call(api, "GET", "/audit", params={"user_id": "u_zhang", "page_size": 50})
    assert logs["total"] >= 2
    for entry in logs["items"]:
        assert entry["request_id"].startswith("req_")
        assert entry["action"]
        assert entry["result"] in {"success", "failure"}

    by_resource = {
        entry["resource_id"]: entry
        for entry in logs["items"]
        if entry["action"] == "transfer"
    }
    assert transfer["transaction_id"] in by_resource

    orders = [entry for entry in logs["items"] if entry["action"] == "purchase_product"]
    assert any(entry["resource_id"] == purchase["order_id"] for entry in orders)

    detail = call(api, "GET", f"/audit/{logs['items'][0]['audit_id']}")
    assert detail["audit_id"] == logs["items"][0]["audit_id"]
