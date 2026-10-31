"""理财订单核实接口：订单详情、按幂等键查询、归属与只读副作用。

覆盖计划任务 2 的断言：返回真实数据库记录、三元组查询互不混用、
静态路由不被订单 ID 吞掉、本人资源归属、以及查询不触发计息或改余额。
"""

from sqlalchemy import func, select

from app.storage.models import DOMAIN_TABLES, Account, Holding, Order
from tests.conftest import mfa_code, submit_assessment

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


def _purchase(
    client,
    *,
    user_id: str = "u_zhang",
    product_id: str = "p_yuebao",
    account_id: str = "a_zhang_current",
    amount_minor: int = 100000,
    key: str | None = None,
) -> dict:
    headers = {"Idempotency-Key": key} if key else None
    return _ok(
        client.post(
            f"{BASE}/products/purchase",
            json={
                "user_id": user_id,
                "from_account_id": account_id,
                "product_id": product_id,
                "amount_minor": amount_minor,
                "confirm": True,
                "mfa_code": mfa_code(),
            },
            headers=headers,
        )
    )


def _snapshot(database, *, business_only: bool = False) -> dict:
    """业务表行数与余额快照。

    `business_only=True` 时排除审计与安全状态：失败路径允许留下独立保存的失败审计和
    失败计数，但那不代表资金发生了变化。
    """

    with database.sessions() as session:
        counts = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in DOMAIN_TABLES
        }
        balances = dict(session.execute(select(Account.id, Account.balance_minor)).all())
    if business_only:
        counts = {
            name: value
            for name, value in counts.items()
            if name not in {"audit_logs", "security_state"}
        }
    return {"counts": counts, "balances": balances}


def test_get_wealth_order_reads_database(seeded_client, seeded_database, wealth_risk_profile):
    purchased = _purchase(seeded_client, key="order-detail-1")
    data = _ok(seeded_client.get(f"{BASE}/wealth/orders/{purchased['order_id']}"))

    with seeded_database.sessions() as session:
        order = session.get_one(Order, purchased["order_id"])
        assert data["order_id"] == order.id
        assert data["transaction_id"] == order.transaction_id
        assert data["amount_minor"] == order.amount_minor == 100000
        assert data["holding_id"] == order.holding_id
        assert data["from_account_id"] == order.from_account_id == "a_zhang_current"
        assert data["product_id"] == order.product_id
        assert data["product_name"] == "余额宝"
        assert data["status"] == "completed"
        assert data["order_type"] == "product_purchase"

    # 订单号与流水号是两回事，不能混用。
    assert data["order_id"] != data["transaction_id"]
    assert data["transaction_id"].startswith("txn_")
    assert data["order_id"].startswith("order_")


def test_order_lookup_uses_user_operation_key(seeded_client, seeded_database, wealth_risk_profile):
    submit_assessment(seeded_database, "u_li")
    zhang = _purchase(seeded_client, key="shared-key")
    li = _purchase(
        seeded_client, user_id="u_li", account_id="a_li_current", key="shared-key"
    )
    assert zhang["order_id"] != li["order_id"]

    query = {"operation_type": "purchase_product", "idempotent_key": "shared-key"}
    by_zhang = _ok(
        seeded_client.get(f"{BASE}/wealth/orders/by-idempotency-key", params={**query, "user_id": "u_zhang"})
    )
    by_li = _ok(
        seeded_client.get(f"{BASE}/wealth/orders/by-idempotency-key", params={**query, "user_id": "u_li"})
    )
    assert by_zhang["order"]["order_id"] == zhang["order_id"]
    assert by_li["order"]["order_id"] == li["order_id"]
    assert by_zhang["result"]["order_id"] == zhang["order_id"]

    # 未知键：found=false，不编造订单。
    missing = _ok(
        seeded_client.get(
            f"{BASE}/wealth/orders/by-idempotency-key",
            params={**query, "idempotent_key": "never-used"},
        )
    )
    assert missing["found"] is False
    assert missing["order"] is None
    assert missing["result"] is None

    # 只允许理财操作类型，不借接口读其他域的幂等结果。
    _error(
        seeded_client.get(
            f"{BASE}/wealth/orders/by-idempotency-key",
            params={"operation_type": "transfer", "idempotent_key": "shared-key"},
        ),
        422,
        1012,
    )


def test_static_lookup_route_is_not_order_id(seeded_client, wealth_risk_profile):
    purchased = _purchase(seeded_client, key="static-route-1")
    data = _ok(
        seeded_client.get(
            f"{BASE}/wealth/orders/by-idempotency-key",
            params={"operation_type": "purchase_product", "idempotent_key": "static-route-1"},
        )
    )
    assert data["found"] is True
    assert data["order"]["order_id"] == purchased["order_id"]

    # 反向：静态路径必须优先匹配，缺参数是 1012 而不是被当成订单 ID 的 404。
    _error(seeded_client.get(f"{BASE}/wealth/orders/by-idempotency-key"), 422, 1012)


def test_holding_and_order_owner_enforced(seeded_client, seeded_database, wealth_risk_profile):
    submit_assessment(seeded_database, "u_li")
    zhang = _purchase(seeded_client, key="owner-zhang")
    li = _purchase(seeded_client, user_id="u_li", account_id="a_li_current", key="owner-li")

    # A 读 B 的持仓与订单都被拒绝，且不泄露详情。
    _error(seeded_client.get(f"{BASE}/holdings/{li['holding_id']}"), 404, 1004)
    _error(seeded_client.get(f"{BASE}/wealth/orders/{li['order_id']}"), 404, 1004)
    assert _ok(seeded_client.get(f"{BASE}/holdings/{zhang['holding_id']}"))["user_id"] == "u_zhang"

    # 赎回别人的持仓同样按不存在拒绝，且没有资金变化。
    before = _snapshot(seeded_database, business_only=True)
    _error(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "user_id": "u_zhang",
                "holding_id": li["holding_id"],
                "amount_minor": 1000,
                "confirm": True,
                "mfa_code": mfa_code(),
            },
        ),
        404,
        1004,
    )
    assert _snapshot(seeded_database, business_only=True) == before


def test_read_income_is_side_effect_free(seeded_client, seeded_database, wealth_risk_profile):
    purchased = _purchase(seeded_client, key="read-only-1")
    before = _snapshot(seeded_database)

    for _ in range(2):
        _ok(seeded_client.get(f"{BASE}/holdings"))
        _ok(seeded_client.get(f"{BASE}/holdings/{purchased['holding_id']}"))
        _ok(seeded_client.get(f"{BASE}/products"))
        _ok(seeded_client.get(f"{BASE}/products/p_yuebao"))
        _ok(seeded_client.get(f"{BASE}/wealth/orders/{purchased['order_id']}"))
        _ok(seeded_client.get(f"{BASE}/wealth/risk-assessments/latest"))

    assert _snapshot(seeded_database) == before


def test_holding_exposes_redeemable_and_realized_income(
    seeded_client, seeded_database, wealth_risk_profile
):
    flexible = _purchase(seeded_client, key="facts-flexible")
    detail = _ok(seeded_client.get(f"{BASE}/holdings/{flexible['holding_id']}"))
    assert detail["redeemable_minor"] == detail["current_value_minor"] == 100000
    assert detail["realized_income_minor"] == 0

    # 未到期的到期赎回产品不可赎回。
    fixed = _purchase(
        seeded_client,
        product_id="p_fixed_term",
        amount_minor=1000000,
        key="facts-fixed",
    )
    fixed_detail = _ok(seeded_client.get(f"{BASE}/holdings/{fixed['holding_id']}"))
    assert fixed_detail["redeemable_minor"] == 0
    assert fixed_detail["maturity_date"] is not None

    # 部分赎回后：可赎回额与已实现收益来自真实数据。
    redeemed = _ok(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "holding_id": flexible["holding_id"],
                "amount_minor": 40000,
                "confirm": True,
                "mfa_code": mfa_code(),
            },
        )
    )
    after = _ok(seeded_client.get(f"{BASE}/holdings/{flexible['holding_id']}"))
    assert after["redeemable_minor"] == 60000
    assert after["realized_income_minor"] == redeemed["interest_minor"] == 0

    with seeded_database.sessions() as session:
        holding = session.get_one(Holding, flexible["holding_id"])
        assert after["principal_minor"] == holding.principal_minor
        assert after["current_value_minor"] == holding.current_value_minor


def test_product_exposes_redeem_rule(seeded_client):
    products = {item["product_id"]: item for item in _ok(seeded_client.get(f"{BASE}/products"))}
    assert products["p_yuebao"]["redeem_rule"] == "flexible"
    assert products["p_money_fund"]["redeem_rule"] == "flexible"
    assert products["p_fixed_term"]["redeem_rule"] == "maturity_only"
    detail = _ok(seeded_client.get(f"{BASE}/products/p_fixed_term"))
    assert detail["redeem_rule"] == "maturity_only"
    assert detail["duration_months"] == 12
