"""Card confirmation goes through pending-actions decide; the old authorize path is gone."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.runtime import AgentRuntime
from app.main import create_app
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from tests.support.scripted_model import ScriptedModel
from tests.test_card_module import ACTION_ID, CardBank

__all__ = ["ACTION_ID", "AuthorizationBank", "paused_card", "decide"]
from tests.test_conversation_api import create, messages, second_session, submit
from tests.test_multi_agent_api import frames


class AuthorizationBank(CardBank):
    def __init__(self):
        super().__init__()
        action = self._responses["/cards/actions"].data
        self.register("/cards/actions", {**action, "permission_level": "red"})

    def request(self, method, path, **kwargs):
        if path.endswith("/authorize") and (kwargs.get("json") or {}).get("decision") == "confirm":
            self.authorized = kwargs["json"].get("verification_code") == "123456"
            self.register(
                path,
                {
                    "status": "authorized" if self.authorized else "pending",
                    "verification_passed": self.authorized,
                    "verification_failures": 0 if self.authorized else 1,
                },
            )
            self.requests.append({"method": method, "path": path, **kwargs})
            return self._responses[path]
        return super().request(method, path, **kwargs)


def paused_card(rt, session):
    bank = AuthorizationBank()
    registry = build_registry()
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank), audit=rt.audit)
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"card","instruction":"冻结银行卡"}'),
            ("call", "card_freeze", '{"card_suffix":"8801"}'),
            "已冻结银行卡",
        ]
    )
    checkpoint = InMemorySaver()

    def agent():
        return AgentRuntime(
            model,
            graph_factory=agent_graph_factory(gateway, client=bank),
            checkpointer=checkpoint,
            customer_resolver=rt.identity.bank_customer_id,
        )

    rt.bank_client = bank
    rt.agent = agent()
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )
    conversation = create(client)
    paused = submit(client, conversation["conversation_id"], "冻结我的卡")
    question = next(data for name, data in frames(paused) if name == "interrupt")
    return client, conversation, question, bank, model, agent


def decide(client, conversation_id, pending_id, body, *, key=None):
    return client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json=body,
        headers={"Idempotency-Key": key or f"click-{uuid4().hex}"},
    )


def test_old_authorize_route_returns_gone(rt, session):
    client, conversation, question, bank, _, _ = paused_card(rt, session)
    before = len(bank.requests)
    path = (
        f"/api/conversations/{conversation['conversation_id']}/card-actions/{ACTION_ID}/authorize"
    )
    response = client.post(
        path,
        json={
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
        },
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "CARD_AUTHORIZE_GONE"
    assert len(bank.requests) == before


def test_decide_authorizes_and_executes_without_leaking_the_code(rt, session):
    client, conversation, question, bank, model, rebuild = paused_card(rt, session)
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["pending_id"] == ACTION_ID
    assert question["confirmation"]["requires_code"] is True

    rt.agent = rebuild()
    detail = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]
    assert detail["pending_interrupt"]["confirmation"]["pending_id"] == ACTION_ID

    wrong = decide(
        client,
        conversation["conversation_id"],
        ACTION_ID,
        {
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "000000",
        },
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "VERIFICATION_FAILED"
    assert not bank.authorized
    assert len(messages(client, conversation["conversation_id"])["items"]) == 1
    still = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]
    assert still["pending_interrupt"]["confirmation"]["pending_id"] == ACTION_ID

    finished = decide(
        client,
        conversation["conversation_id"],
        ACTION_ID,
        {
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
        },
    )
    assert finished.status_code == 200, finished.text
    assert "text/event-stream" in finished.headers["Content-Type"]
    assert any(name == "final" for name, _ in frames(finished))
    assert any(request["path"].endswith("/authorize") for request in bank.requests)
    assert any(request["path"].endswith("/execute") for request in bank.requests)
    assert "123456" not in finished.text
    assert "123456" not in json.dumps(messages(client, conversation["conversation_id"]))
    assert "123456" not in json.dumps(model.prompts)
    assert "123456" not in client.get("/api/audit/run-events").text

    before = len(bank.requests)
    stale = decide(
        client,
        conversation["conversation_id"],
        ACTION_ID,
        {"question_id": question["question_id"], "decision": "confirm"},
    )
    assert stale.status_code == 409
    assert len(bank.requests) == before


def test_another_user_cannot_decide_the_card(rt, session):
    client, conversation, question, bank, _, _ = paused_card(rt, session)
    before = len(bank.requests)
    stranger = second_session(rt)
    response = client.post(
        f"/api/conversations/{conversation['conversation_id']}/pending-actions/{ACTION_ID}/decide",
        headers={
            "Authorization": "Bearer " + stranger["token"],
            "Idempotency-Key": f"click-{uuid4().hex}",
        },
        json={
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
        },
    )
    assert response.status_code == 404
    assert len(bank.requests) == before
