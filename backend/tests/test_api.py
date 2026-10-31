"""The HTTP surface: envelope, session ownership, and the endpoints that must not exist.

The old control plane is gone, so the task API, the governance endpoints and the
confirmation/audit family are all expected to be absent rather than disabled.
"""

import pytest
from fastapi.testclient import TestClient

from app.integration.model_client import ModelReply, UnconfiguredModelClient
from app.main import create_app


class SilentModel:
    """A model that answers, so a configured runtime can be described without a provider."""

    def complete(self, messages: tuple) -> ModelReply:
        del messages
        return ModelReply(text='{"agent":null,"reply":"好的"}')

    def close(self) -> None:
        return None


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def chat(client, session, message, **client_context):
    body = {"session_id": session["session_id"], "message": message}
    if client_context:
        body["client_context"] = client_context
    return client.post("/api/chat/messages", json=body)


def test_http_envelope_carries_the_request_id(rt, session):
    client = client_for(rt, session)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["X-Request-Id"]
    assert response.json()["error"] is None


def test_health_reports_whether_a_model_is_configured(rt, session):
    client = client_for(rt, session)

    assert client.get("/api/health").json()["data"]["model_configured"] is False

    rt.agent.set_model(SilentModel())
    assert client.get("/api/health").json()["data"]["model_configured"] is True

    rt.agent.set_model(UnconfiguredModelClient())
    assert client.get("/api/health").json()["data"]["model_configured"] is False


def test_a_session_is_required_before_chatting(rt):
    anonymous = TestClient(create_app(rt.settings, runtime=rt))

    response = anonymous.post("/api/chat/messages", json={"session_id": "s", "message": "你好"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_REQUIRED"


def test_a_forged_session_is_rejected(rt, session):
    forged = client_for(rt, session).post(
        "/api/chat/messages", json={"session_id": "sess_forged", "message": "hi"}
    )

    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "SESSION_MISMATCH"


def test_an_empty_message_is_rejected_before_the_graph_runs(rt, session):
    response = client_for(rt, session).post(
        "/api/chat/messages", json={"session_id": session["session_id"], "message": ""}
    )

    assert response.status_code == 422
    assert response.json()["data"] is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/tasks"),
        ("get", "/api/tasks/task_x"),
        ("post", "/api/tasks/task_x/cancel"),
        ("post", "/api/tasks/task_x/send-email"),
    ],
)
def test_the_old_task_api_is_gone(rt, session, method, path):
    client = client_for(rt, session)

    response = client.post(path, json={}) if method == "post" else client.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/tasks/task_x/confirm"),
        ("post", "/api/tasks/task_x/mfa"),
        ("post", "/api/tasks/task_x/rollback"),
        ("post", "/api/tasks/task_x/handover"),
        ("get", "/api/tasks/task_x/audit"),
        ("get", "/api/runs"),
        ("get", "/api/clock"),
        ("post", "/api/clock/advance"),
        ("post", "/api/clock/fire-due"),
    ],
)
def test_governance_and_run_endpoints_do_not_exist(rt, session, method, path):
    client = client_for(rt, session)

    response = client.post(path, json={}) if method == "post" else client.get(path)

    assert response.status_code == 404


def test_the_settings_and_notification_endpoints_survive(rt, session):
    client = client_for(rt, session)

    assert client.get("/api/settings/llm").status_code == 200
    assert client.get("/api/queue/stats").status_code == 200
    assert client.put("/api/settings/email", json={"email": "demo@example.com"}).status_code == 200
