"""PIN input stays on the authenticated decision boundary, outside Agent state/audits."""

from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.runtime import AgentRuntime
from app.integration.bank_client import BankResponse
from app.main import create_app
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from tests.support.scripted_model import ScriptedModel
from tests.test_card_module import ACTION_ID, CardBank, invoke, runtime_for
from tests.test_conversation_api import create, messages, second_session, submit
from tests.test_multi_agent_api import frames

OLD, NEW, CODE = "483719", "725903", "572809"


class PasswordBank(CardBank):
    def __init__(self):
        super().__init__()
        action = self._responses["/cards/actions"].data
        self.register(
            "/cards/actions",
            {
                **action,
                "operation": "change_password",
                "label": "修改银行卡交易密码",
                "permission_level": "red",
            },
        )
        result = self._responses[f"/cards/actions/{ACTION_ID}/execute"].data
        self.register(
            f"/cards/actions/{ACTION_ID}/execute",
            {**result, "operation": "change_password", "status": "active"},
        )

    def request(self, method, path, **kwargs):
        if path.endswith("/password/authorize"):
            self.requests.append({"method": method, "path": path, **kwargs})
            body = kwargs["json"]
            self.authorized = (
                body.get("old_password") == OLD
                and body.get("new_password") == NEW
                and body.get("confirm_password") == NEW
                and body.get("verification_code") == CODE
            )
            return BankResponse(
                0,
                "success",
                {
                    "status": "authorized" if self.authorized else "pending",
                    "verification_passed": self.authorized,
                    "verification_failures": 0,
                    "credential_error": "old_password",
                    "remaining_attempts": 2,
                },
            )
        return super().request(method, path, **kwargs)


def paused_password(rt, session):
    bank = PasswordBank()
    registry = build_registry()
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank), audit=rt.audit)
    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"card","instruction":"修改尾号8801的银行卡交易密码"}'),
            ("call", "card_change_password", '{"card_suffix":"8801"}'),
            "交易密码修改成功。",
        ]
    )
    saver = InMemorySaver()

    def rebuild():
        return AgentRuntime(
            model,
            graph_factory=agent_graph_factory(gateway, client=bank),
            checkpointer=saver,
            customer_resolver=rt.identity.bank_customer_id,
        )

    rt.bank_client = bank
    rt.agent = rebuild()
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )
    conversation_id = create(client)["conversation_id"]
    response = submit(client, conversation_id, "修改尾号8801的银行卡交易密码")
    question = next(data for name, data in frames(response) if name == "interrupt")
    return client, conversation_id, question, bank, model, saver, rebuild


def decide(client, conversation_id, question, *, password=None, key=None, decision="confirm"):
    body = {"question_id": question["question_id"], "decision": decision}
    if decision == "confirm":
        body.update(
            verification_code=CODE,
            transaction_password=password
            or {"old_password": OLD, "new_password": NEW, "confirm_password": NEW},
        )
    return client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{ACTION_ID}/decide",
        json=body,
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def assert_no_secrets(value):
    raw = str(value)
    assert all(secret not in raw for secret in (OLD, NEW, CODE, "scrypt$"))


def test_password_decision_recovery_replay_and_private_channels(rt, session, caplog):
    client, cid, question, bank, model, saver, rebuild = paused_password(rt, session)
    assert question["confirmation"]["operation"] == "change_password"
    assert question["confirmation"]["requires_code"] is True
    rt.agent = rebuild()
    restored = client.get(f"/api/conversations/{cid}").json()["data"]
    assert restored["pending_interrupt"]["confirmation"]["pending_id"] == ACTION_ID
    wrong = decide(
        client,
        cid,
        question,
        password={"old_password": "903176", "new_password": NEW, "confirm_password": NEW},
    )
    assert wrong.status_code == 422
    assert len(messages(client, cid)["items"]) == 1
    click_key = uuid4().hex
    response = decide(client, cid, question, key=click_key)
    assert response.status_code == 200
    assert any(name == "final" for name, _ in frames(response))
    assert "交易密码修改成功" in response.text
    before = len(bank.requests)
    replay = decide(client, cid, question, key=click_key)
    assert replay.status_code == 200 and "application/json" in replay.headers["content-type"]
    assert len(bank.requests) == before
    assert_no_secrets(
        [
            response.text,
            wrong.text,
            replay.text,
            restored,
            messages(client, cid),
            model.messages,
            model.tools,
            saver.storage,
            saver.writes,
            client.get("/api/audit/run-events").text,
            client.get("/api/audit/llm-calls").text,
            caplog.text,
        ]
    )
    tool_calls = [data for name, data in frames(response) if name == "tool_call"]
    assert_no_secrets(tool_calls)
    assert any(
        request["path"].endswith("/password/authorize") and request["json"]["old_password"] == OLD
        for request in bank.requests
    )


def test_password_decision_is_owned_and_cancel_needs_no_credentials(rt, session):
    client, cid, question, bank, _, _, _ = paused_password(rt, session)
    stranger = second_session(rt)
    before = len(bank.requests)
    response = client.post(
        f"/api/conversations/{cid}/pending-actions/{ACTION_ID}/decide",
        headers={"Authorization": "Bearer " + stranger["token"], "Idempotency-Key": uuid4().hex},
        json={
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": CODE,
            "transaction_password": {
                "old_password": OLD,
                "new_password": NEW,
                "confirm_password": NEW,
            },
        },
    )
    assert response.status_code == 404 and len(bank.requests) == before
    cancelled = decide(client, cid, question, decision="cancel")
    assert cancelled.status_code == 200
    assert not any(request["path"].endswith("/execute") for request in bank.requests)
    assert_no_secrets(cancelled.text)


def test_password_selection_uses_bank_candidates_without_model_credentials():
    from tests.test_card_module import CARD

    bank = PasswordBank()
    bank.register("/cards", [CARD, {**CARD, "card_id": "other", "card_number": "**** 8802"}])
    runtime, model = runtime_for(bank, [("call", "card_change_password", "{}")])
    events = invoke(runtime)
    question = next(event.data for event in events if event.event == "interrupt")
    assert question["kind"] == "card_selection"
    assert len(question["fields"][0]["options"]) == 2
    assert_no_secrets([events, model.messages])
