"""User-chosen initial PINs: all issuing paths, ownership and atomic replay."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessError
from app.domain.card_action_service import CardActionService, utcnow
from app.domain.card_password import verify_password
from app.storage.models import AuditLog, Card, CardAction, Order
from tests.api.test_card_password import CODE, NEW, OLD, assert_private
from tests.api.test_card_password import credentials as credentials

BASE = "/api/cards/actions"


def prepare(client):
    response = client.post(
        BASE,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "user_id": "u_zhang",
            "operation": "apply",
            "parameters": {"card_type": "virtual"},
        },
    )
    assert response.status_code == 200
    return response.json()["data"]


def authorize(client, action, **values):
    return client.post(
        f"{BASE}/{action['action_id']}/password/authorize",
        json={
            "user_id": "u_zhang",
            "decision": "confirm",
            "new_password": NEW,
            "confirm_password": NEW,
            **values,
        },
    )


def execute(client, action, user="u_zhang"):
    return client.post(f"{BASE}/{action['action_id']}/execute", json={"user_id": user})


def counts(database):
    with database.sessions() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Card, Order))


def test_initial_pin_required_and_atomic_once_with_private_state(seeded_client, seeded_database):
    before = counts(seeded_database)
    with seeded_database.sessions() as db:
        existing = {c.id: c.transaction_password_hash for c in db.scalars(select(Card))}
    action = prepare(seeded_client)
    assert action["permission_level"] == "yellow"
    assert execute(seeded_client, action).status_code == 409
    assert (
        seeded_client.post(
            f"{BASE}/{action['action_id']}/authorize",
            json={
                "user_id": "u_zhang",
                "decision": "confirm",
            },
        ).status_code
        == 422
    )
    assert counts(seeded_database) == before
    proof = authorize(seeded_client, action)
    assert proof.json()["data"]["verification_passed"] is True
    assert counts(seeded_database) == before
    assert authorize(seeded_client, action).status_code == 200
    response = execute(seeded_client, action)
    result = response.json()["data"]
    assert result["card_type"] == "virtual"
    assert execute(seeded_client, action).json()["data"]["replayed"] is True
    # A completed authorization cannot replace the issued card's PIN.
    authorize(seeded_client, action, new_password=OLD, confirm_password=OLD)
    assert counts(seeded_database) == (before[0] + 1, before[1] + 1)
    with seeded_database.sessions() as db:
        card = db.get(Card, result["card_id"])
        encoded = card.transaction_password_hash
        assert verify_password(NEW, encoded) and not verify_password(OLD, encoded)
        assert all(
            db.get(Card, cid).transaction_password_hash == hashed
            for cid, hashed in existing.items()
        )
        pending = db.get(CardAction, action["action_id"])
        assert pending.pending_password_hash is None
        audit = [(a.old_value, a.new_value, a.reason) for a in db.scalars(select(AuditLog))]
        public = [pending.before, pending.parameters, pending.result, pending.request_hash, audit]
    assert_private(
        [action, proof.json(), response.json(), public, seeded_client.get("/api/cards").json()],
        [encoded],
    )


@pytest.mark.parametrize(
    "values",
    [
        {"new_password": ""},
        {"new_password": "12345"},
        {"new_password": "12345a"},
        {"new_password": "１２３４５６"},
        {"confirm_password": OLD},
        {"old_password": OLD},
        {"verification_code": "000000"},
    ],
)
def test_invalid_initial_pin_cannot_issue(seeded_client, seeded_database, values):
    before = counts(seeded_database)
    action = prepare(seeded_client)
    response = authorize(seeded_client, action, **values)
    assert (
        response.status_code == 422 or response.json()["data"].get("verification_passed") is False
    )
    assert execute(seeded_client, action).status_code == 409
    assert counts(seeded_database) == before
    assert authorize(seeded_client, action).json()["data"]["verification_passed"] is True
    assert execute(seeded_client, action).status_code == 200


def test_missing_fields_and_legacy_entry_never_issue(seeded_client, seeded_database):
    before = counts(seeded_database)
    action = prepare(seeded_client)
    for values in ({}, {"new_password": NEW}, {"confirm_password": NEW}):
        assert (
            seeded_client.post(
                f"{BASE}/{action['action_id']}/password/authorize",
                json={
                    "user_id": "u_zhang",
                    "decision": "confirm",
                    **values,
                },
            ).status_code
            == 422
        )
    assert seeded_client.post("/api/cards/apply", json={"card_type": "virtual"}).status_code == 422
    assert counts(seeded_database) == before


def test_foreign_owner_cancel_expiry_and_failed_retry_clear_staging(seeded_client, seeded_database):
    before = counts(seeded_database)
    action = prepare(seeded_client)
    assert authorize(seeded_client, action, user_id="u_partner").status_code == 404
    assert execute(seeded_client, action, "u_partner").status_code == 404
    assert authorize(seeded_client, action).status_code == 200
    assert (
        authorize(seeded_client, action, verification_code="000000").json()["data"][
            "verification_passed"
        ]
        is False
    )
    assert execute(seeded_client, action).status_code == 409
    assert authorize(seeded_client, action).status_code == 200
    assert (
        seeded_client.post(
            f"{BASE}/{action['action_id']}/authorize",
            json={"user_id": "u_zhang", "decision": "cancel"},
        ).status_code
        == 200
    )
    assert execute(seeded_client, action).status_code == 409
    expired = prepare(seeded_client)
    authorize(seeded_client, expired)
    with seeded_database.sessions.begin() as db:
        db.get(CardAction, expired["action_id"]).expires_at = utcnow() - timedelta(seconds=1)
    assert execute(seeded_client, expired).status_code == 409
    with seeded_database.sessions() as db:
        assert all(
            db.get(CardAction, a["action_id"]).pending_password_hash is None
            for a in (action, expired)
        )
    assert counts(seeded_database) == before


def test_concurrent_authorization_and_execution_issue_once(seeded_client, seeded_database):
    action = prepare(seeded_client)
    before = counts(seeded_database)
    service = CardActionService(seeded_database)

    def issue(_):
        service.authorize(
            "u_zhang",
            action["action_id"],
            "confirm",
            password_credentials={"new_password": NEW, "confirm_password": NEW},
        )
        return service.execute("u_zhang", action["action_id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(issue, range(4)))
    assert len({row["card_id"] for row in results}) == 1
    assert len({row["order_id"] for row in results}) == 1
    assert counts(seeded_database) == (before[0] + 1, before[1] + 1)


def test_failure_after_card_and_order_flush_rolls_back_everything(
    seeded_client, seeded_database, monkeypatch
):
    import app.domain.card_action_service as actions

    action = prepare(seeded_client)
    authorize(seeded_client, action)
    before = counts(seeded_database)
    create = actions.create_card

    def fail_after_flush(*args, **kwargs):
        create(*args, **kwargs)
        raise BusinessError("模拟事务失败")

    monkeypatch.setattr(actions, "create_card", fail_after_flush)
    assert execute(seeded_client, action).status_code == 409
    assert counts(seeded_database) == before
    with seeded_database.sessions() as db:
        assert db.get(CardAction, action["action_id"]).pending_password_hash is None


def test_prepare_rejects_password_before_generic_fingerprint(seeded_client, monkeypatch):
    import app.domain.card_action_service as actions

    def forbidden(_):
        pytest.fail("Password entered generic fingerprint")

    monkeypatch.setattr(actions, "request_fingerprint", forbidden)
    response = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "user_id": "u_zhang",
            "operation": "apply",
            "parameters": {"card_type": "virtual", "new_password": NEW},
        },
    )
    assert response.status_code == 422
    assert_private(response.json())


def test_red_application_checks_code_and_preserves_secret_isolation(seeded_client, seeded_database):
    action = prepare(seeded_client)
    with seeded_database.sessions.begin() as db:
        db.get(CardAction, action["action_id"]).permission_level = "red"
    assert authorize(seeded_client, action).status_code == 409
    for _ in range(3):
        response = authorize(seeded_client, action, verification_code="000000")
    assert response.json()["data"]["status"] == "cancelled"
    assert authorize(seeded_client, action, verification_code=CODE).status_code == 409
    another = prepare(seeded_client)
    with seeded_database.sessions.begin() as db:
        db.get(CardAction, another["action_id"]).permission_level = "red"
    assert (
        authorize(seeded_client, another, verification_code=CODE).json()["data"][
            "verification_passed"
        ]
        is True
    )
    assert execute(seeded_client, another).status_code == 200
