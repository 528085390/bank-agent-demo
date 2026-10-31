"""M1 验收：财富演示数据 + 银行 HTTP 申购／赎回 + 重启后仍可查询与重放。

这是“不需要任何智能体就能独立验证”的银行侧验收：
演示数据可重复初始化不重复入金；HTTP 申购／赎回完整落库；
关闭并重新构造银行应用与数据库连接（等价于进程重启）后，测评、余额、持仓、
订单与幂等回执都还在，同键重放不重复扣款。

用例只操作本会话创建并核验的测试 schema，不触碰共享库或 bank_db。
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.deps import get_database
from app.core.errors import BusinessError
from app.main import app
from app.storage.database import Database
from app.storage.models import (
    Account,
    AuditLog,
    Holding,
    IdempotencyRecord,
    Order,
    Product,
    SimulationState,
    Transaction,
)
from app.storage.wealth_seed import (
    SAMPLE_HOLDING_ID,
    SAMPLE_ORDER_ID,
    WEALTH_USER,
    apply_wealth_seed,
    demo_status,
)
from tests.conftest import OPS_TOKEN, SERVICE_TOKEN, mfa_code

BASE = "/api"
HIGH_RISK_ANSWERS = {"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2}
BUY_AMOUNT_MINOR = 200000
SELL_AMOUNT_MINOR = 50000


def _ok(response) -> dict:
    assert response.status_code < 400, response.text
    body = response.json()
    assert body["code"] == 0
    return body["data"]


def _seed_wealth(database) -> dict:
    with database.sessions() as session:
        with session.begin():
            state = session.get_one(SimulationState, 1)
            return apply_wealth_seed(session, now=state.current_time)


@contextmanager
def _restarted_bank(database_url):
    """关闭并重新构造银行应用与数据库连接：新 TestClient + 新 Database。"""

    database = Database(database_url)
    app.dependency_overrides[get_database] = lambda: database
    try:
        # 重启后仍是「我们自己的后端」，所以照样要带服务凭证——这正是被测的行为：
        # 凭证与进程生命周期无关，重建客户端不会丢掉访问权。
        with TestClient(
            app,
            raise_server_exceptions=False,
            headers={"X-Service-Token": SERVICE_TOKEN, "X-Ops-Token": OPS_TOKEN},
        ) as test_client:
            yield test_client, database
    finally:
        app.dependency_overrides.clear()
        database.dispose()


def _balance(database, account_id: str) -> int:
    with database.sessions() as session:
        return session.get_one(Account, account_id).balance_minor


def _count(database, model, *filters) -> int:
    with database.sessions() as session:
        return session.scalar(select(func.count()).select_from(model).where(*filters))


def _submit_assessment(client, user_id: str = WEALTH_USER) -> dict:
    return _ok(
        client.post(
            f"{BASE}/wealth/risk-assessments",
            json={"user_id": user_id, "answers": HIGH_RISK_ANSWERS},
        )
    )


def _purchase(client, *, key: str, amount_minor: int = BUY_AMOUNT_MINOR) -> dict:
    return _ok(
        client.post(
            f"{BASE}/products/purchase",
            json={
                "user_id": WEALTH_USER,
                "from_account_id": "a_wealth_main",
                "product_id": "p_demo_cash",
                "amount_minor": amount_minor,
                "confirm": True,
                "mfa_code": mfa_code(),
            },
            headers={"Idempotency-Key": key},
        )
    )


def test_wealth_seed_is_repeatable_without_extra_credit(seeded_database):
    first = _seed_wealth(seeded_database)
    assert first["status"] == "created"
    assert first["accounts"] == {
        "a_wealth_main": 2000000,
        "a_wealth_backup": 500000,
        "a_wealth_other": 100000,
    }
    assert first["sample_holding"]["principal_minor"] == 100000
    assert first["sample_holding"]["current_value_minor"] == 100500

    second = _seed_wealth(seeded_database)
    assert second["status"] == "reused"
    assert second["accounts"] == first["accounts"]
    assert _balance(seeded_database, "a_wealth_main") == 2000000
    assert _count(seeded_database, Holding, Holding.user_id == WEALTH_USER) == 1
    assert _count(seeded_database, Account) == 8, "基础种子 5 个账户 + 演示 3 个"
    # 演示数据与基础种子并存：原有产品与账户未被改动。
    assert _balance(seeded_database, "a_zhang_current") == 1250050


def test_wealth_seed_refuses_partial_state(seeded_database):
    _seed_wealth(seeded_database)

    # 少一个演示产品：状态为 partial，明确拒绝而不是静默补齐或覆盖。
    with seeded_database.sessions() as session:
        with session.begin():
            session.delete(session.get_one(Product, "p_demo_growth"))
        assert demo_status(session) == "partial"
        with pytest.raises(BusinessError):
            apply_wealth_seed(session, now=session.get_one(SimulationState, 1).current_time)


def test_wealth_seed_does_not_overwrite_tampered_state(seeded_database):
    _seed_wealth(seeded_database)

    # 演示实体齐全但样例持仓被改动：校验阶段拒绝，不静默覆盖已有数据。
    with seeded_database.sessions() as session:
        with session.begin():
            session.get_one(Holding, SAMPLE_HOLDING_ID).principal_minor = 1
    with seeded_database.sessions() as session:
        assert demo_status(session) == "seeded"
        with pytest.raises(BusinessError):
            apply_wealth_seed(session, now=session.get_one(SimulationState, 1).current_time)
    assert _count(seeded_database, Holding, Holding.id == SAMPLE_HOLDING_ID) == 1


def test_seed_wealth_demo_script_creates_then_reuses(seeded_database, database_url, monkeypatch):
    """受控初始化入口本身可用：首次写入，重复执行只校验复用。"""

    from app.core.config import get_settings
    from app.domain.seed_service import SeedService
    from scripts.seed_wealth_demo import main

    SeedService(seeded_database).seed()
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        assert main(["--dry-run", "--allow-test-database"]) == 0
        assert main(["--allow-test-database"]) == 0
        assert main(["--allow-test-database"]) == 0
    finally:
        get_settings.cache_clear()

    assert _balance(seeded_database, "a_wealth_main") == 2000000
    assert _count(seeded_database, Holding, Holding.user_id == WEALTH_USER) == 1


def test_seed_wealth_demo_script_refuses_test_database_without_flag(
    seeded_database, database_url, monkeypatch
):
    from app.core.config import get_settings
    from scripts.seed_wealth_demo import main

    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        assert main([]) == 2, "库名以 test 结尾时默认拒绝执行"
    finally:
        get_settings.cache_clear()

def test_bank_http_purchase_and_redeem_persist(seeded_client, seeded_database):
    _seed_wealth(seeded_database)
    assessment = _submit_assessment(seeded_client)

    purchased = _purchase(seeded_client, key="accept-buy-1")
    assert purchased["status"] == "completed"
    assert _balance(seeded_database, "a_wealth_main") == 1800000

    with seeded_database.sessions() as session:
        order = session.get_one(Order, purchased["order_id"])
        entry = session.get_one(Transaction, purchased["transaction_id"])
        record = session.get_one(
            IdempotencyRecord, (WEALTH_USER, "purchase_product", "accept-buy-1")
        )
        holding = session.get_one(Holding, purchased["holding_id"])
    assert order.order_type == "product_purchase"
    assert order.status == "completed"
    assert order.details["assessment_id"] == assessment["assessment_id"]
    assert entry.direction == "debit"
    assert entry.amount_minor == BUY_AMOUNT_MINOR
    assert record.result["order_id"] == purchased["order_id"]
    assert holding.principal_minor == holding.current_value_minor == BUY_AMOUNT_MINOR

    holding_view = _ok(
        seeded_client.get(
            f"{BASE}/holdings/{purchased['holding_id']}", params={"user_id": WEALTH_USER}
        )
    )
    assert holding_view["redeemable_minor"] == BUY_AMOUNT_MINOR
    assert holding_view["realized_income_minor"] == 0

    redeemed = _ok(
        seeded_client.post(
            f"{BASE}/products/redeem",
            json={
                "user_id": WEALTH_USER,
                "holding_id": purchased["holding_id"],
                "amount_minor": SELL_AMOUNT_MINOR,
                "to_account_id": "a_wealth_main",
                "confirm": True,
                "mfa_code": mfa_code(),
            },
            headers={"Idempotency-Key": "accept-sell-1"},
        )
    )
    assert redeemed["holding_status"] == "partial"
    assert _balance(seeded_database, "a_wealth_main") == 1850000

    with seeded_database.sessions() as session:
        remaining = session.get_one(Holding, purchased["holding_id"])
        sell_order = session.get_one(Order, redeemed["order_id"])
        sell_entry = session.get_one(Transaction, redeemed["transaction_id"])
    assert remaining.current_value_minor == BUY_AMOUNT_MINOR - SELL_AMOUNT_MINOR
    assert remaining.status == "partial"
    assert sell_order.to_account_id == "a_wealth_main"
    assert sell_entry.direction == "credit"
    assert sell_entry.amount_minor == SELL_AMOUNT_MINOR

    # 样例持仓不受影响，仍是收益查询的参照（本金 1000 元、价值 1005 元）。
    sample = _ok(
        seeded_client.get(
            f"{BASE}/holdings/{SAMPLE_HOLDING_ID}", params={"user_id": WEALTH_USER}
        )
    )
    assert sample["accrued_interest_minor"] == 500

    # 每笔理财写都留审计。
    assert _count(seeded_database, AuditLog, AuditLog.action == "purchase_product") == 1
    assert _count(seeded_database, AuditLog, AuditLog.action == "redeem_product") == 1


def test_bank_restart_keeps_assessment_and_order(seeded_client, seeded_database, database_url):
    _seed_wealth(seeded_database)
    _submit_assessment(seeded_client)
    purchased = _purchase(seeded_client, key="restart-buy-1")

    seeded_database.dispose()
    with _restarted_bank(database_url) as (restarted, fresh):
        latest = _ok(
            restarted.get(
                f"{BASE}/wealth/risk-assessments/latest", params={"user_id": WEALTH_USER}
            )
        )
        assert latest["risk_level"] == "high"
        assert latest["valid"] is True

        order = _ok(
            restarted.get(
                f"{BASE}/wealth/orders/{purchased['order_id']}",
                params={"user_id": WEALTH_USER},
            )
        )
        assert order["order_id"] == purchased["order_id"]
        assert order["transaction_id"] == purchased["transaction_id"]

        holding = _ok(
            restarted.get(
                f"{BASE}/holdings/{purchased['holding_id']}", params={"user_id": WEALTH_USER}
            )
        )
        assert holding["principal_minor"] == BUY_AMOUNT_MINOR

        assert _balance(fresh, "a_wealth_main") == 1800000


def test_bank_restart_replays_same_key(seeded_client, seeded_database, database_url):
    _seed_wealth(seeded_database)
    _submit_assessment(seeded_client)
    first = _purchase(seeded_client, key="restart-replay-1")

    seeded_database.dispose()
    with _restarted_bank(database_url) as (restarted, fresh):
        replayed = _purchase(restarted, key="restart-replay-1")
        assert replayed["replayed"] is True
        assert replayed["order_id"] == first["order_id"]
        assert replayed["holding_id"] == first["holding_id"]

        looked_up = _ok(
            restarted.get(
                f"{BASE}/wealth/orders/by-idempotency-key",
                params={
                    "user_id": WEALTH_USER,
                    "operation_type": "purchase_product",
                    "idempotent_key": "restart-replay-1",
                },
            )
        )
        assert looked_up["found"] is True
        assert looked_up["order"]["order_id"] == first["order_id"]

        assert _balance(fresh, "a_wealth_main") == 1800000, "重启后同键重放不重复扣款"
        # 演示样例订单不计入本次交易，只统计新仓那一笔。
        assert (
            _count(
                fresh,
                Order,
                Order.order_type == "product_purchase",
                Order.id != SAMPLE_ORDER_ID,
            )
            == 1
        )
        assert _count(fresh, Holding, Holding.id == first["holding_id"]) == 1
