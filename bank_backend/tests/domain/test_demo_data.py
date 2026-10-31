"""完整演示数据应可查询、可继续操作、资金可对账。"""

from sqlalchemy import func, select

from app.demo_data import build_demo_data
from app.domain.money_service import reconcile
from app.storage.models import (
    DOMAIN_TABLES,
    AaRequest,
    Account,
    Holding,
    Order,
    ScheduledTask,
    Subscription,
)
from app.storage.seed import ACCOUNTS


def test_demo_data_covers_domains_and_reconciles(database, client):
    result = build_demo_data(database)
    with database.sessions() as session:
        for model in DOMAIN_TABLES:
            assert session.scalar(select(func.count()).select_from(model)) > 0, model.__tablename__
        for spec in ACCOUNTS:
            account = session.get_one(Account, spec["id"])
            assert reconcile(session, account, spec["opening_minor"]) == account.balance_minor
            assert account.balance_minor == account.available_minor
        for model, expected in (
            (Holding, {"holding", "partial", "redeemed"}),
            (AaRequest, {"open", "partial", "completed", "cancelled", "expired"}),
            (Order, {"pending", "completed", "failed", "cancelled"}),
            (ScheduledTask, {"scheduled", "completed", "failed", "cancelled"}),
            (Subscription, {"active", "paused", "cancelled"}),
        ):
            assert expected <= set(session.scalars(select(model.status)))
    for path in ("holdings", "cards", "subscriptions", "transfers/scheduled", "audit"):
        response = client.get(f"/api/{path}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]
    response = client.get(f"/api/transfers/aa/{result['examples']['aa_partial']}")
    assert response.status_code == 200, response.text


def test_demo_data_rebuild_does_not_accumulate_records(database):
    first = build_demo_data(database)
    second = build_demo_data(database)
    assert first["counts"] == second["counts"]
    assert first["balances"] == second["balances"]
    assert first["run_id"] != second["run_id"]


def test_demo_seed_endpoint_returns_complete_dataset_summary(client):
    response = client.post("/api/seed/demo")
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["counts"]["holdings"] == 3
    assert payload["counts"]["aa_requests"] == 5
    assert payload["counts"]["scheduled_tasks"] == 4
