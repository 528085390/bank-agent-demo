"""The card workflow executes only the immutable, owned, authorized intent."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.domain.card_action_service import CardActionService, utcnow
from app.storage.models import AuditLog, Card, CardAction, Order

BASE = "/api/cards/actions"
CARD_ID = "card_zhang_debit"


def prepare(client, operation="freeze", parameters=None, key="intent-1"):
    response = client.post(
        BASE,
        headers={"Idempotency-Key": key},
        json={
            "user_id": "u_zhang",
            "operation": operation,
            "parameters": parameters if parameters is not None else {"card_id": CARD_ID},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def authorize(client, action, decision="confirm", code=None, user="u_zhang"):
    return client.post(
        f"{BASE}/{action['action_id']}/authorize",
        json={
            "user_id": user,
            "decision": decision,
            "verification_code": code,
        },
    )


def execute(client, action, user="u_zhang"):
    return client.post(f"{BASE}/{action['action_id']}/execute", json={"user_id": user})


def test_preparing_and_forging_confirmation_cannot_freeze_a_card(seeded_client):
    action = prepare(seeded_client)
    assert action["status"] == "pending"
    assert execute(seeded_client, action).json()["code"] == 1007
    assert seeded_client.get(f"/api/cards/{CARD_ID}").json()["data"]["status"] == "active"
    response = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": "forged"},
        json={
            "user_id": "u_zhang",
            "operation": "freeze",
            "parameters": {"card_id": CARD_ID, "confirmed": True},
        },
    )
    assert response.json()["code"] == 1012


def test_yellow_confirmation_executes_and_replays_once(seeded_client, seeded_database):
    action = prepare(seeded_client)
    assert authorize(seeded_client, action).json()["data"]["status"] == "authorized"
    first, second = execute(seeded_client, action), execute(seeded_client, action)
    assert first.json()["data"]["status"] == "frozen"
    assert second.json()["data"]["replayed"] is True
    with seeded_database.sessions() as session:
        events = (
            session.execute(select(AuditLog).where(AuditLog.action == "card.executed"))
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].request_id == action["action_id"]


def test_cancelled_intent_never_executes(seeded_client):
    action = prepare(seeded_client)
    assert authorize(seeded_client, action, "cancel").json()["data"]["status"] == "cancelled"
    assert execute(seeded_client, action).json()["code"] == 1007
    assert authorize(seeded_client, action).json()["code"] == 1005


def test_loss_requires_exact_demo_verification_and_cannot_be_normally_unfrozen(
    seeded_client, seeded_database
):
    action = prepare(seeded_client, "report_loss")
    assert action["permission_level"] == "red"
    # 缺码是「还没确认」，不是一次失败的验证尝试：不计数，仍走 1007。
    missing = authorize(seeded_client, action, code=None)
    assert missing.status_code == 409, missing.text
    assert missing.json()["code"] == 1007
    assert missing.json()["data"] is None
    assert execute(seeded_client, action).json()["code"] == 1007
    # 错码才计入失败次数；卡路径把它作为分类后的成功信封返回（顶层 code=0，
    # data.verification_passed=false），对话侧据此给出「还可尝试 N 次」。
    wrong = authorize(seeded_client, action, code="654321")
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["code"] == 0
    assert wrong.json()["data"]["verification_passed"] is False
    assert wrong.json()["data"]["verification_failures"] == 1
    assert execute(seeded_client, action).json()["code"] == 1007
    assert (
        authorize(seeded_client, action, code="123456").json()["data"]["verification_passed"]
        is True
    )
    result = execute(seeded_client, action).json()["data"]
    assert result["status"] == "frozen" and result["freeze_reason"] == "lost"
    assert seeded_client.post(f"/api/cards/{CARD_ID}/unfreeze").json()["code"] == 1005
    released = prepare(seeded_client, "release_loss", key="release")
    assert authorize(seeded_client, released, code="123456").status_code == 200
    assert execute(seeded_client, released).json()["data"]["status"] == "active"
    with seeded_database.sessions() as session:
        audit = (
            session.execute(select(AuditLog).where(AuditLog.action.like("card.%"))).scalars().all()
        )
        assert "123456" not in str([(row.old_value, row.new_value, row.reason) for row in audit])
        assert sum(row.action == "card.verification_failed" for row in audit) == 1


def test_three_bad_codes_close_the_intent(seeded_client):
    action = prepare(seeded_client, "report_loss")
    for index in range(3):
        data = authorize(seeded_client, action, code="000000").json()["data"]
        assert data["verification_failures"] == index + 1
    assert data["status"] == "cancelled"
    assert authorize(seeded_client, action, code="123456").json()["code"] == 1005


def test_every_step_checks_customer_ownership(seeded_client):
    response = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": "other-card"},
        json={
            "user_id": "u_zhang",
            "operation": "freeze",
            "parameters": {"card_id": "card_partner_debit"},
        },
    )
    assert response.json()["code"] == 1004
    action = prepare(seeded_client)
    assert authorize(seeded_client, action, user="u_partner").json()["code"] == 1004
    assert execute(seeded_client, action, user="u_partner").json()["code"] == 1004
    assert (
        seeded_client.get("/api/cards/card_partner_debit", params={"user_id": "u_zhang"}).json()[
            "code"
        ]
        == 1004
    )


def test_apply_checks_account_ownership_and_replays_same_order(seeded_client):
    response = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": "wrong-account"},
        json={
            "user_id": "u_zhang",
            "operation": "apply",
            "parameters": {"card_type": "virtual", "account_id": "a_partner_current"},
        },
    )
    assert response.json()["code"] == 1004
    action = prepare(seeded_client, "apply", {"card_type": "virtual"})
    seeded_client.post(
        f"{BASE}/{action['action_id']}/password/authorize",
        json={
            "user_id": "u_zhang",
            "decision": "confirm",
            "new_password": "725903",
            "confirm_password": "725903",
        },
    )
    first, second = (
        execute(seeded_client, action).json()["data"],
        execute(seeded_client, action).json()["data"],
    )
    assert first["card_id"] == second["card_id"] and first["order_id"] == second["order_id"]


def test_partial_limit_preserves_other_value_and_increases_require_verification(seeded_client):
    lower = prepare(
        seeded_client, "update_limit", {"card_id": CARD_ID, "daily_limit_minor": 100000}
    )
    assert lower["parameters"]["monthly_limit_minor"] == 5000000
    assert lower["permission_level"] == "yellow"
    authorize(seeded_client, lower)
    assert execute(seeded_client, lower).json()["data"]["daily_limit_minor"] == 100000
    higher = prepare(
        seeded_client,
        "update_limit",
        {"card_id": CARD_ID, "daily_limit_minor": 200000},
        key="higher",
    )
    assert higher["permission_level"] == "red"
    # 红色操作不带码 = 「还没确认」（1007），不是验证失败，也不计数。
    assert authorize(seeded_client, higher).json()["code"] == 1007


@pytest.mark.parametrize(
    "values",
    [
        {"daily_limit_minor": -1},
        {"monthly_limit_minor": 100},
        {"monthly_limit_minor": 0},
        {"daily_limit_minor": True},
        {"daily_limit_minor": 1.5},
    ],
)
def test_invalid_limits_are_refused(seeded_client, values):
    response = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": "invalid"},
        json={
            "user_id": "u_zhang",
            "operation": "update_limit",
            "parameters": {"card_id": CARD_ID, **values},
        },
    )
    assert response.json()["code"] == 1012


def test_blacklist_add_preserves_existing_remove_requires_verification(seeded_client):
    first = prepare(
        seeded_client,
        "update_restrictions",
        {"card_id": CARD_ID, "mode": "add", "merchants": ["赌场"]},
    )
    authorize(seeded_client, first)
    execute(seeded_client, first)
    second = prepare(
        seeded_client,
        "update_restrictions",
        {"card_id": CARD_ID, "mode": "add", "merchants": ["赌场", "高风险商户"]},
        key="add-more",
    )
    assert second["parameters"]["merchant_blacklist"] == ["赌场", "高风险商户"]
    authorize(seeded_client, second)
    execute(seeded_client, second)
    remove = prepare(
        seeded_client,
        "update_restrictions",
        {"card_id": CARD_ID, "mode": "remove", "merchants": ["赌场"]},
        key="remove",
    )
    assert remove["permission_level"] == "red"
    authorize(seeded_client, remove, code="123456")
    assert execute(seeded_client, remove).json()["data"]["merchant_blacklist"] == ["高风险商户"]


def test_stale_and_expired_actions_cannot_execute(seeded_client, seeded_database):
    action = prepare(seeded_client)
    authorize(seeded_client, action)
    seeded_client.post(
        f"/api/cards/{CARD_ID}/limit",
        json={"daily_limit_minor": 100000, "monthly_limit_minor": 5000000},
    )
    assert execute(seeded_client, action).json()["code"] == 1005
    expired = prepare(seeded_client, key="expired")
    with seeded_database.sessions() as session, session.begin():
        session.get(CardAction, expired["action_id"]).expires_at = utcnow() - timedelta(seconds=1)
    assert authorize(seeded_client, expired).json()["code"] == 1005


def test_idempotency_rejects_changed_parameters_and_serializes_concurrent_application(
    seeded_database,
):
    service = CardActionService(seeded_database)

    def create(_):
        return service.prepare("u_zhang", "apply", {"card_type": "virtual"}, "same-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        actions = list(pool.map(create, range(2)))
    assert actions[0]["action_id"] == actions[1]["action_id"]
    action_id = actions[0]["action_id"]
    service.authorize(
        "u_zhang",
        action_id,
        "confirm",
        password_credentials={"new_password": "725903", "confirm_password": "725903"},
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.execute("u_zhang", action_id), range(2)))
    assert results[0]["card_id"] == results[1]["card_id"]
    with seeded_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Card)) == 4
        assert (
            session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.order_type == "card_application")
            )
            == 1
        )
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        service.prepare("u_zhang", "apply", {"card_type": "debit"}, "same-key")


def test_legacy_partial_restriction_merges_current_limits(seeded_client):
    response = seeded_client.post(
        f"/api/cards/{CARD_ID}/restrictions", json={"monthly_limit_minor": 100}
    )
    assert response.json()["code"] == 1012
