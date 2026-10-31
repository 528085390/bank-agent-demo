"""理财产品、持仓、收益与赎回的 API 测试。

理财写固定要求有效测评、`confirm=true` 和演示验证码，因此本模块用 autouse
fixture 准备测评，请求里显式带上演示码。
"""

import pytest

from app.domain.security_service import LOCK_THRESHOLD
from tests.conftest import mfa_code

BASE = "/api"
MFA = mfa_code()


@pytest.fixture(autouse=True)
def _wealth_profile(wealth_risk_profile):
    """u_zhang 的有效测评：没有它申购会被适当性校验拒绝。"""
    return wealth_risk_profile


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


def _purchase(client, **overrides):
    payload = {
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
        "mfa_code": MFA,
    }
    payload.update(overrides)
    return _ok(client.post(f"{BASE}/products/purchase", json=payload))


def test_list_products_and_get_detail(seeded_client):
    products = _ok(seeded_client.get(f"{BASE}/products"))
    assert {item["product_id"] for item in products} == {
        "p_yuebao",
        "p_money_fund",
        "p_fixed_term",
    }
    detail = _ok(seeded_client.get(f"{BASE}/products/p_yuebao"))
    assert detail["name"] == "余额宝"
    assert detail["expected_return"] == 0.035


def test_products_recommend_route_is_not_shadowed_by_product_id(seeded_client):
    data = _ok(
        seeded_client.post(f"{BASE}/products/recommend", json={"risk_level": "low"})
    )
    assert {item["product_id"] for item in data} == {"p_yuebao", "p_money_fund"}


def test_products_compare_route(seeded_client):
    data = _ok(
        seeded_client.post(
            f"{BASE}/products/compare", json={"product_ids": ["p_fixed_term", "p_yuebao"]}
        )
    )
    assert [item["product_id"] for item in data] == ["p_fixed_term", "p_yuebao"]


def test_compare_with_one_product_returns_422(seeded_client):
    _error(
        seeded_client.post(f"{BASE}/products/compare", json={"product_ids": ["p_yuebao"]}),
        422,
        1012,
    )


def test_unknown_product_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/products/p_missing"), 404, 1004)


def test_purchase_then_read_holding_and_simulate_interest(seeded_client):
    purchased = _purchase(seeded_client)
    assert purchased["status"] == "completed"

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 100000

    holdings = _ok(seeded_client.get(f"{BASE}/holdings"))
    assert [item["holding_id"] for item in holdings] == [purchased["holding_id"]]

    detail = _ok(seeded_client.get(f"{BASE}/holdings/{purchased['holding_id']}"))
    assert detail["principal_minor"] == 100000
    assert detail["accrued_interest_minor"] == 0

    simulated = _ok(
        seeded_client.post(
            f"{BASE}/holdings/{purchased['holding_id']}/simulate-interest",
            json={"as_of": "2027-09-20"},
        )
    )
    assert simulated["days"] == 365
    assert simulated["accrued_interest_minor"] == 3500
    assert simulated["current_value_minor"] == 103500


def test_purchase_below_minimum_returns_422(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_money_fund",
                "amount_minor": 50,
            },
        ),
        422,
        1012,
    )


def test_purchase_without_confirmation_is_rejected(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 100000,
            },
        ),
        409,
        1007,
    )


def test_purchase_with_a_wrong_verification_code_is_rejected_as_1008(seeded_client):
    """错码有自己的线路码 1008，不再复用 1007 的「待确认」语义。"""

    body = _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 100000,
                "confirm": True,
                "mfa_code": "654321",
            },
        ),
        409,
        1008,
    )
    # 文案是用户可见契约：没有钉子的话打错字也能全绿。
    assert body["message"] == "验证码不正确"


def test_repeated_wrong_codes_lock_the_purchase_tool_with_its_own_code(seeded_client):
    """理财写路径也走 require_not_locked：连续 5 次错码后第 6 次是 1009，不是通用 1005。

    锁的银行侧 message 保持英文稳定 token "locked"，中文文案只在对话侧。
    """

    for _ in range(LOCK_THRESHOLD):
        _error(
            seeded_client.post(
                f"{BASE}/products/purchase",
                json={
                    "from_account_id": "a_zhang_current",
                    "product_id": "p_yuebao",
                    "amount_minor": 100000,
                    "confirm": True,
                    "mfa_code": "654321",
                },
            ),
            409,
            1008,
        )

    # 锁定期内即使验证码正确也被拒，并且给出锁自己的码。
    locked = _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 100000,
                "confirm": True,
                "mfa_code": MFA,
            },
        ),
        409,
        1009,
    )
    assert locked["message"] == "locked"


@pytest.mark.parametrize("missing", [None, ""])
def test_purchase_without_a_verification_code_is_still_pending_confirmation(
    seeded_client, missing
):
    """缺码/空码是「还没确认」：HTTP 层就拦成 1007，不落到领域层当验证失败。"""

    _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 100000,
                "confirm": True,
                "mfa_code": missing,
            },
        ),
        409,
        1007,
    )


def test_redeem_verification_failures_are_split_into_1008_and_1007(seeded_client):
    purchased = _purchase(seeded_client)
    payload = {
        "holding_id": purchased["holding_id"],
        "amount_minor": 1000,
        "confirm": True,
    }
    _error(
        seeded_client.post(f"{BASE}/products/redeem", json={**payload, "mfa_code": "654321"}),
        409,
        1008,
    )
    _error(seeded_client.post(f"{BASE}/products/redeem", json=payload), 409, 1007)


def test_purchase_with_insufficient_balance_returns_409(seeded_client):
    _error(
        seeded_client.post(
            f"{BASE}/products/purchase",
            json={
                "from_account_id": "a_zhang_current",
                "product_id": "p_yuebao",
                "amount_minor": 20000000,
                "confirm": True,
                "mfa_code": "123456",
            },
        ),
        409,
        1006,
    )


def test_idempotent_purchase_retry_does_not_buy_twice(seeded_client):
    headers = {"Idempotency-Key": "purchase-header-001"}
    payload = {
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
        "mfa_code": MFA,
    }
    first = _ok(seeded_client.post(f"{BASE}/products/purchase", json=payload, headers=headers))
    second = _ok(seeded_client.post(f"{BASE}/products/purchase", json=payload, headers=headers))
    assert first["holding_id"] == second["holding_id"]
    assert second["replayed"] is True

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 100000
    assert len(_ok(seeded_client.get(f"{BASE}/holdings"))) == 1


def test_purchase_replay_with_the_code_dropped_still_returns_its_receipt(seeded_client):
    """同键重试时漏带验证码仍是原回执：缺码守卫不能挡在幂等重放前面。"""

    headers = {"Idempotency-Key": "purchase-code-dropped-001"}
    payload = {
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
    }
    first = _ok(
        seeded_client.post(
            f"{BASE}/products/purchase", json={**payload, "mfa_code": MFA}, headers=headers
        )
    )
    retry = _ok(seeded_client.post(f"{BASE}/products/purchase", json=payload, headers=headers))
    assert retry == {**first, "replayed": True}


def test_redeem_replay_with_the_code_dropped_still_returns_its_receipt(seeded_client):
    """赎回路径同理：已成交的重试漏带验证码也拿回原回执，而不是 1007。"""

    purchased = _purchase(seeded_client)
    headers = {"Idempotency-Key": "redeem-code-dropped-001"}
    payload = {
        "holding_id": purchased["holding_id"],
        "amount_minor": 40000,
        "confirm": True,
    }
    first = _ok(
        seeded_client.post(
            f"{BASE}/products/redeem", json={**payload, "mfa_code": MFA}, headers=headers
        )
    )
    retry = _ok(seeded_client.post(f"{BASE}/products/redeem", json=payload, headers=headers))
    assert retry == {**first, "replayed": True}


def test_full_redemption_over_the_api(seeded_client):
    purchased = _purchase(seeded_client)
    _ok(
        seeded_client.post(
            f"{BASE}/holdings/{purchased['holding_id']}/simulate-interest",
            json={"as_of": "2027-09-20"},
        )
    )
    redeemed = _ok(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "holding_id": purchased["holding_id"],
                "amount_minor": 103500,
                "confirm": True,
                "mfa_code": MFA,
            },
        )
    )
    assert redeemed["holding_status"] == "redeemed"
    assert redeemed["interest_minor"] == 3500

    balance = _ok(seeded_client.get(f"{BASE}/accounts/a_zhang_current/balance"))
    assert balance["balance_minor"] == 1250050 - 100000 + 103500
    assert _ok(seeded_client.get(f"{BASE}/holdings", params={"status": "redeemed"}))[0][
        "holding_id"
    ] == purchased["holding_id"]


def test_partial_redemption_over_the_api(seeded_client):
    purchased = _purchase(seeded_client)
    redeemed = _ok(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "holding_id": purchased["holding_id"],
                "amount_minor": 40000,
                "confirm": True,
                "mfa_code": MFA,
            },
        )
    )
    assert redeemed["holding_status"] == "partial"
    assert _ok(seeded_client.get(f"{BASE}/holdings/{purchased['holding_id']}"))["status"] == (
        "partial"
    )


def test_redemption_above_value_returns_409(seeded_client):
    purchased = _purchase(seeded_client)
    _error(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "holding_id": purchased["holding_id"],
                "amount_minor": 100001,
                "confirm": True,
                "mfa_code": MFA,
            },
        ),
        409,
        1005,
    )


def test_unknown_holding_returns_404(seeded_client):
    _error(seeded_client.get(f"{BASE}/holdings/hold_missing"), 404, 1004)
    _error(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "holding_id": "hold_missing",
                "amount_minor": 100,
                "confirm": True,
                "mfa_code": MFA,
            },
        ),
        404,
        1004,
    )
