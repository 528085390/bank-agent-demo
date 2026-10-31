"""账单与异常分析的 API 测试。"""

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


def test_monthly_bill_over_the_api(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/bills/month", params={"month": "2026-09"}))
    assert data["period"] == "2026-09"
    assert data["expense_minor"] == 1220900
    assert data["income_minor"] == 5000
    assert data["categories"]["dining"] == 10000


def test_monthly_bill_rejects_a_bad_month_with_422(seeded_client):
    _error(seeded_client.get(f"{BASE}/bills/month", params={"month": "2026-13"}), 422, 1012)
    _error(seeded_client.get(f"{BASE}/bills/month", params={"month": "202609"}), 422, 1012)


def test_monthly_bill_requires_the_month_parameter(seeded_client):
    _error(seeded_client.get(f"{BASE}/bills/month"), 422, 1012)


def test_yearly_bill_over_the_api(seeded_client):
    data = _ok(seeded_client.get(f"{BASE}/bills/year", params={"year": 2026}))
    assert data["period"] == "2026"
    assert data["expense_minor"] == 1220900


def test_bill_categories_over_the_api(seeded_client):
    data = _ok(
        seeded_client.get(
            f"{BASE}/bills/categories",
            params={"start_date": "2026-09-01", "end_date": "2026-09-30"},
        )
    )
    assert data["categories"] == {
        "shopping": 1200000,
        "dining": 10000,
        "subscription": 5900,
        "transfer": 5000,
    }


def test_bill_categories_requires_both_dates(seeded_client):
    _error(seeded_client.get(f"{BASE}/bills/categories"), 422, 1012)


def test_analyze_hits_all_three_rules_over_the_api(seeded_client):
    data = _ok(
        seeded_client.post(
            f"{BASE}/bills/analyze",
            json={"start_date": "2026-09-01", "end_date": "2026-09-30"},
        )
    )
    assert {item["rule_id"] for item in data["findings"]} == {
        "large_amount",
        "frequent_small",
        "repeated_transaction",
    }
    for finding in data["findings"]:
        assert finding["transaction_ids"]
        assert finding["amount_minor"] > 0
        assert finding["explanation"]


def test_analyze_respects_the_category_filter_over_the_api(seeded_client):
    data = _ok(
        seeded_client.post(
            f"{BASE}/bills/analyze",
            json={
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "category": "dining",
            },
        )
    )
    assert data["analyzed_count"] == 5
    assert {item["rule_id"] for item in data["findings"]} == {
        "frequent_small",
        "repeated_transaction",
    }


def test_analyze_rejects_a_missing_body_range(seeded_client):
    _error(seeded_client.post(f"{BASE}/bills/analyze", json={"category": "dining"}), 422, 1012)


def test_analyze_rejects_an_inverted_range(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/bills/analyze",
            json={"start_date": "2026-09-30", "end_date": "2026-09-01"},
        ),
        422,
        1012,
    )


def test_analyze_empty_range_returns_no_findings(seeded_client):
    data = _ok(
        seeded_client.post(
            f"{BASE}/bills/analyze",
            json={"start_date": "2020-01-01", "end_date": "2020-01-31"},
        )
    )
    assert data["findings"] == []
    assert data["analyzed_count"] == 0
