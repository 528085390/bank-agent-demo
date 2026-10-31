"""Transaction PIN changes and private credential storage, through the bank API."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.domain.card_action_service import CardActionService, utcnow
from app.domain.card_password import verify_password
from app.storage.models import AuditLog, Card, CardAction

OLD, NEW, CODE = "483719", "725903", "572809"
CARD = "card_zhang_debit"
BASE = "/api/cards/actions"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("DEMO_CARD_PASSWORD", OLD)
    monkeypatch.setenv("MOCK_MFA_CODE", CODE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def prepare(client, *, user="u_zhang", card=CARD):
    response = client.post(
        BASE,
        headers={"Idempotency-Key": uuid4().hex},
        json={"user_id": user, "operation": "change_password", "parameters": {"card_id": card}},
    )
    assert response.status_code == 200
    return response.json()["data"]


def authorize(client, action, **overrides):
    return client.post(
        f"{BASE}/{action['action_id']}/password/authorize",
        json={
            "user_id": "u_zhang",
            "decision": "confirm",
            "verification_code": CODE,
            "old_password": OLD,
            "new_password": NEW,
            "confirm_password": NEW,
            **overrides,
        },
    )


def execute(client, action, user="u_zhang"):
    return client.post(f"{BASE}/{action['action_id']}/execute", json={"user_id": user})


def stored(database):
    with database.sessions() as session:
        card = session.get(Card, CARD)
        return card.transaction_password_hash, card.password_version


def assert_private(payload, hashes=()):
    raw = json.dumps(payload, ensure_ascii=False)
    assert all(secret not in raw for secret in (OLD, NEW, CODE, *hashes))
    assert "scrypt$" not in raw


def test_change_password_requires_proof_and_executes_once_without_leaks(
    seeded_client, seeded_database
):
    old_hash, version = stored(seeded_database)
    action = prepare(seeded_client)
    assert action["permission_level"] == "red"
    assert execute(seeded_client, action).json()["code"] == 1007
    generic = seeded_client.post(
        f"{BASE}/{action['action_id']}/authorize",
        json={"user_id": "u_zhang", "decision": "confirm", "verification_code": CODE},
    )
    assert generic.status_code == 422
    authorized = authorize(seeded_client, action)
    assert authorized.json()["data"]["verification_passed"] is True
    assert stored(seeded_database)[0] == old_hash
    result = execute(seeded_client, action)
    assert result.status_code == 200
    assert execute(seeded_client, action).json()["data"]["replayed"] is True
    new_hash, changed_version = stored(seeded_database)
    assert changed_version == version + 1
    assert verify_password(NEW, new_hash) and not verify_password(OLD, new_hash)
    with seeded_database.sessions() as session:
        record = session.get(CardAction, action["action_id"])
        assert record.pending_password_hash is None
        logs = [row.new_value for row in session.scalars(select(AuditLog))]
        snapshots = [record.before, record.parameters, record.result]
    assert_private(
        [
            action,
            authorized.json(),
            result.json(),
            logs,
            snapshots,
            seeded_client.get("/api/cards").json(),
            seeded_client.get(f"/api/cards/{CARD}").json(),
            seeded_client.get("/api/audit").json(),
        ],
        [old_hash, new_hash],
    )


@pytest.mark.parametrize(
    "values",
    [
        {"old_password": "903176"},
        {"verification_code": "000000"},
        {"new_password": "12345", "confirm_password": "12345"},
        {"new_password": "12345a", "confirm_password": "12345a"},
        {"new_password": "１２３４５６", "confirm_password": "１２３４５６"},
        {"confirm_password": "903176"},
        {"new_password": OLD, "confirm_password": OLD},
    ],
)
def test_invalid_proofs_never_change_password(seeded_client, seeded_database, values):
    action = prepare(seeded_client)
    before = stored(seeded_database)
    response = authorize(seeded_client, action, **values)
    assert response.status_code == 422 or response.json()["data"]["verification_passed"] is False
    assert execute(seeded_client, action).status_code == 409
    assert stored(seeded_database) == before
    assert_private(response.json())


@pytest.mark.parametrize("field", ["old_password", "verification_code"])
def test_failures_cannot_be_reset_by_preparing_another_action(
    seeded_client, seeded_database, field
):
    before = stored(seeded_database)
    for index in range(3):
        result = authorize(seeded_client, prepare(seeded_client), **{field: "903176"}).json()[
            "data"
        ]
        assert result["remaining_attempts"] == 2 - index
    assert result["status"] == "cancelled"
    locked = authorize(seeded_client, prepare(seeded_client)).json()["data"]
    assert locked["credential_error"] == "locked"
    assert stored(seeded_database) == before


def test_cancel_and_expiry_discard_staged_hash(seeded_client, seeded_database):
    before = stored(seeded_database)
    for expired in (False, True):
        action = prepare(seeded_client)
        assert authorize(seeded_client, action).status_code == 200
        if expired:
            with seeded_database.sessions() as session, session.begin():
                session.get(CardAction, action["action_id"]).expires_at = utcnow() - timedelta(
                    seconds=1
                )
        else:
            seeded_client.post(
                f"{BASE}/{action['action_id']}/authorize",
                json={"user_id": "u_zhang", "decision": "cancel"},
            )
        assert execute(seeded_client, action).status_code == 409
        with seeded_database.sessions() as session:
            assert session.get(CardAction, action["action_id"]).pending_password_hash is None
            assert session.get(CardAction, action["action_id"]).status == "cancelled"
        assert authorize(seeded_client, action).status_code == 409
    assert stored(seeded_database) == before


def test_cross_user_and_forged_parameters_are_refused(seeded_client):
    action = prepare(seeded_client)
    other = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": uuid4().hex},
        json={"user_id": "u_zhang", "operation": "freeze", "parameters": {"card_id": CARD}},
    ).json()["data"]
    assert authorize(seeded_client, other).status_code == 422
    assert authorize(seeded_client, action, user_id="u_li").status_code == 404
    assert execute(seeded_client, action, user="u_li").status_code == 404
    assert (
        seeded_client.post(
            BASE,
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "user_id": "u_li",
                "operation": "change_password",
                "parameters": {"card_id": CARD},
            },
        ).status_code
        == 404
    )
    assert (
        seeded_client.post(
            BASE,
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "user_id": "u_zhang",
                "operation": "change_password",
                "parameters": {"card_id": CARD, "new_password": NEW, "confirmed": True},
            },
        ).status_code
        == 422
    )


def test_parallel_actions_and_replayed_execution_change_the_password_once(seeded_database):
    service = CardActionService(seeded_database)
    actions = [
        service.prepare("u_zhang", "change_password", {"card_id": CARD}, uuid4().hex)
        for _ in range(2)
    ]
    for action in actions:
        service.authorize(
            "u_zhang",
            action["action_id"],
            "confirm",
            CODE,
            password_credentials={
                "old_password": OLD,
                "new_password": NEW,
                "confirm_password": NEW,
            },
        )

    def run(action):
        try:
            return service.execute("u_zhang", action["action_id"])
        except BusinessError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, actions))
    assert sum(result is not None for result in results) == 1
    assert stored(seeded_database)[1] == 1
    winner = next(result for result in results if result is not None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replays = list(
            pool.map(lambda _: service.execute("u_zhang", winner["action_id"]), range(2))
        )
    assert all(result["replayed"] for result in replays)
    assert stored(seeded_database)[1] == 1


def test_parallel_failed_authorizations_cannot_lose_failure_counts(seeded_database):
    service = CardActionService(seeded_database)
    actions = [
        service.prepare("u_zhang", "change_password", {"card_id": CARD}, uuid4().hex)
        for _ in range(3)
    ]

    def wrong(action):
        return service.authorize(
            "u_zhang",
            action["action_id"],
            "confirm",
            CODE,
            password_credentials={
                "old_password": "903176",
                "new_password": NEW,
                "confirm_password": NEW,
            },
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(wrong, actions))
    assert sorted(result["remaining_attempts"] for result in results) == [0, 1, 2]
    with seeded_database.sessions() as session:
        card = session.get(Card, CARD)
        assert card.password_failures == 3
        assert card.password_locked_until > utcnow()
        assert card.password_version == 0


def test_failed_retry_revokes_staged_authorization(seeded_client, seeded_database):
    action = prepare(seeded_client)
    assert authorize(seeded_client, action).json()["data"]["status"] == "authorized"
    assert (
        authorize(seeded_client, action, old_password="903176").json()["data"][
            "verification_passed"
        ]
        is False
    )
    assert execute(seeded_client, action).status_code == 409
    assert authorize(seeded_client, action).json()["data"]["status"] == "authorized"
    assert (
        authorize(
            seeded_client, action, new_password="903176", confirm_password="903176"
        ).status_code
        == 422
    )
    assert execute(seeded_client, action).status_code == 409
    assert stored(seeded_database)[1] == 0


def test_existing_and_new_cards_initialize_with_distinct_salted_hashes(
    seeded_client, seeded_database
):
    application = seeded_client.post(
        BASE,
        headers={"Idempotency-Key": uuid4().hex},
        json={"user_id": "u_zhang", "operation": "apply", "parameters": {"card_type": "virtual"}},
    ).json()["data"]
    seeded_client.post(
        f"{BASE}/{application['action_id']}/password/authorize",
        json={
            "user_id": "u_zhang",
            "decision": "confirm",
            "new_password": NEW,
            "confirm_password": NEW,
        },
    )
    assert execute(seeded_client, application).status_code == 200
    with seeded_database.sessions() as session:
        hashes = list(session.scalars(select(Card.transaction_password_hash)))
    assert len(set(hashes)) == len(hashes)
    assert sum(verify_password(OLD, value) for value in hashes) == len(hashes) - 1
    assert sum(verify_password(NEW, value) for value in hashes) == 1
