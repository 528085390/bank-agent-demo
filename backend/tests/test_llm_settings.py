"""The settings panel's model configuration, and the retired control plane's absence.

The panel still collects Chat Completions connection settings, and the one thing that
changed is what installing them does: it swaps the agent runtime's model instead of
building a structured plan client. Everything that used to sit behind that setting -- the
plan schema, the capability menu, the execution kernel -- is gone, and this file asserts
it stays gone.
"""

import importlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit.context import AuditContext, audit_context
from app.core.config import Settings
from app.integration.llm_config import LlmConfigManager, configure_from_settings
from app.integration.model_client import ChatCompletionsModelClient, UnconfiguredModelClient
from app.integration.model_config import ChatCompletionsConfig, normalize_base_url
from app.main import create_app
from app.storage.models import LlmCallAudit
from tests.conftest import ReplyHandler, payload

RETIRED_MODULES = [
    "app.integration.control",
    "app.integration.conversation",
    "app.integration.chat_completions_planner",
    "app.kernel.service",
    "app.kernel.executor",
    "app.capabilities.registry",
    "app.capabilities.mock_tools",
    "app.orchestration.model_planner",
    "app.orchestration.planner",
    "app.storage.tasks",
    "app.api.ports",
]


def client_for(rt, session):
    rt.llm = LlmConfigManager(rt)
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def underlying(client):
    """The adapter a client really is, looking through the audit decorator.

    Installing a model wraps the adapter so every call through it is recorded, so the
    installed object is the wrapper. What the panel guarantees is the *adapter* underneath,
    which is what these assertions are about.
    """

    return getattr(client, "inner", client)


def test_settings_install_the_model_and_never_return_the_key(rt, session, provider):
    client = client_for(rt, session)

    saved = client.put("/api/settings/llm", json=payload(provider.server_port))

    assert saved.status_code == 200
    assert saved.json()["data"]["mode"] == "model"
    assert saved.json()["data"]["configured"] is True
    assert isinstance(underlying(rt.agent.model), ChatCompletionsModelClient)
    assert rt.agent.model_configured is True
    # The secret is masked on the way out and never appears in the response body.
    assert "sk-local-protocol" not in saved.text
    assert saved.json()["data"]["api_key_masked"].startswith("sk-")


def test_settings_use_the_real_chat_completions_protocol(rt, session, provider):
    client = client_for(rt, session)

    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200

    assert ReplyHandler.captured["path"] == "/v1/chat/completions"
    assert ReplyHandler.captured["authorization"] == "Bearer sk-local-protocol"
    assert ReplyHandler.captured["body"]["model"] == "mock-model"
    # The probe is a real turn: the provider was asked for an answer, not just pinged.
    assert ReplyHandler.captured["body"]["messages"]


def test_json_object_mode_asks_the_provider_for_a_json_object(rt, session, provider):
    client = client_for(rt, session)

    saved = client.put(
        "/api/settings/llm", json=payload(provider.server_port, response_format="json_object")
    )

    assert saved.status_code == 200
    assert ReplyHandler.captured["body"]["response_format"] == {"type": "json_object"}


def test_json_schema_mode_publishes_no_schema_because_there_is_none(rt, session, provider):
    client = client_for(rt, session)

    assert (
        client.put(
            "/api/settings/llm", json=payload(provider.server_port, response_format="json_schema")
        ).status_code
        == 200
    )

    # The agents parse JSON out of the reply themselves, so no schema is imposed.
    assert "response_format" not in ReplyHandler.captured["body"]


def test_a_failed_connection_never_replaces_the_active_configuration(rt, session):
    client = client_for(rt, session)

    failed = client.put("/api/settings/llm", json=payload(1))

    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "LLM_CONNECTION_FAILED"
    assert isinstance(rt.agent.model, UnconfiguredModelClient)
    assert client.get("/api/settings/llm").json()["data"]["mode"] == "unconfigured"


def test_the_probe_rejects_a_model_that_answers_with_nothing(rt, session, provider):
    ReplyHandler.content = None
    client = client_for(rt, session)

    failed = client.put("/api/settings/llm", json=payload(provider.server_port))

    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "LLM_RESPONSE_INVALID"
    assert isinstance(rt.agent.model, UnconfiguredModelClient)


def test_testing_a_connection_does_not_install_it(rt, session, provider):
    client = client_for(rt, session)

    assert (
        client.post("/api/settings/llm/test", json=payload(provider.server_port)).status_code == 200
    )

    assert isinstance(rt.agent.model, UnconfiguredModelClient)
    assert client.get("/api/settings/llm").json()["data"]["configured"] is False


def test_clearing_the_model_restores_the_loud_failure(rt, session, provider):
    client = client_for(rt, session)
    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200

    cleared = client.delete("/api/settings/llm")

    assert cleared.json()["data"]["mode"] == "unconfigured"
    assert isinstance(rt.agent.model, UnconfiguredModelClient)
    # A turn without a model fails loudly instead of degrading into keyword routing.
    streamed = client.post(
        "/api/chat/messages",
        json={"session_id": session["session_id"], "message": "查活期余额"},
        headers={"Idempotency-Key": "llm-settings-turn"},
    )
    assert streamed.status_code == 200
    assert "MODEL_NOT_CONFIGURED" in streamed.text
    assert "event: final" not in streamed.text


def test_an_unreachable_provider_does_not_stop_the_process_from_starting(rt):
    """A provider that is down must leave the API serving, not abort startup."""

    rt.llm = LlmConfigManager(rt)
    settings = Settings(
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="sk-env-protocol",
        llm_model="env-model",
    )

    assert configure_from_settings(rt.llm, settings) is False

    assert isinstance(rt.agent.model, UnconfiguredModelClient)


def test_the_environment_can_start_the_process_already_configured(rt, provider):
    """The startup path: a model named in the environment needs no visit to the panel."""

    rt.llm = LlmConfigManager(rt)
    settings = Settings(
        llm_base_url=f"http://127.0.0.1:{provider.server_port}/v1",
        llm_api_key="sk-env-protocol",
        llm_model="env-model",
        llm_response_format="json_schema",
        llm_timeout_seconds=10,
    )

    assert configure_from_settings(rt.llm, settings) is True

    assert isinstance(underlying(rt.agent.model), ChatCompletionsModelClient)
    assert rt.agent.model_configured is True
    # It went through the real protocol, carrying the environment's key and model name.
    assert ReplyHandler.captured["authorization"] == "Bearer sk-env-protocol"
    assert ReplyHandler.captured["body"]["model"] == "env-model"


# --- every install path is audited ---------------------------------------------------------


def audited_calls(rt):
    with rt.database.sessions() as db:
        return list(db.scalars(select(LlmCallAudit).order_by(LlmCallAudit.created_at)))


def test_the_settings_panel_test_is_audited_as_a_configuration_test(rt, session, provider):
    """A probe is a real model call, so it is recorded like one -- and installs nothing."""

    client = client_for(rt, session)

    assert (
        client.post("/api/settings/llm/test", json=payload(provider.server_port)).status_code == 200
    )

    rows = audited_calls(rt)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "configuration_test"
    assert row.user_id == session["user_id"]
    assert (row.conversation_id, row.run_id) == (None, None)
    assert row.result == "SUCCEEDED"
    # The whole request went in, and the whole provider response came back.
    assert row.prompt_json["messages"], "the probe's own messages are stored"
    assert row.response_json["id"] == "chatcmpl_local"
    assert row.response_text == '{"agent":null,"reply":"好的"}'
    assert isinstance(rt.agent.model, UnconfiguredModelClient), "testing still installs nothing"


def test_installing_from_the_panel_is_audited_as_a_configuration_probe(rt, session, provider):
    client = client_for(rt, session)

    assert client.put("/api/settings/llm", json=payload(provider.server_port)).status_code == 200

    rows = audited_calls(rt)
    assert len(rows) == 1
    assert rows[0].source == "configuration_probe"
    assert rows[0].user_id == session["user_id"]
    assert (rows[0].conversation_id, rows[0].run_id) == (None, None)


def test_the_startup_probe_is_audited_as_its_own_source(rt, provider):
    """A process that came up configured can say what it was configured with."""

    rt.llm = LlmConfigManager(rt)
    settings = Settings(
        llm_base_url=f"http://127.0.0.1:{provider.server_port}/v1",
        llm_api_key="sk-env-protocol",
        llm_model="env-model",
        llm_timeout_seconds=10,
    )

    assert configure_from_settings(rt.llm, settings) is True

    rows = audited_calls(rt)
    assert len(rows) == 1
    assert rows[0].source == "startup_probe"
    assert rows[0].model == "env-model"


def test_a_probe_does_not_inherit_the_previous_turns_conversation(rt, session, provider):
    """The panel's probe runs outside any turn, and must record that.

    This is the failure the explicit context exists to prevent: a `ContextVar` left behind by
    an earlier turn would make a configuration probe look like it belonged to somebody's
    conversation.
    """

    client = client_for(rt, session)
    with audit_context(
        AuditContext(
            conversation_id="conv_someone_else", run_id="run_someone_else", user_id="user_0001"
        )
    ):
        pass

    assert (
        client.post("/api/settings/llm/test", json=payload(provider.server_port)).status_code == 200
    )

    row = audited_calls(rt)[0]
    assert (row.conversation_id, row.run_id, row.source) == (
        None,
        None,
        "configuration_test",
    )


def test_the_api_key_is_never_in_an_audit_record(rt, session, provider):
    client = client_for(rt, session)

    client.put("/api/settings/llm", json=payload(provider.server_port))

    row = audited_calls(rt)[0]
    stored = json.dumps(
        {"prompt": row.prompt_json, "response": row.response_json, "text": row.response_text}
    )
    assert "sk-local-protocol" not in stored
    assert "api_key" not in stored
    # What was stored is what actually changed the answer.
    assert row.prompt_json["parameters"]["model"] == "mock-model"
    assert row.model == "mock-model"


def test_an_environment_that_names_no_model_changes_nothing(rt):
    """The default: no `.env`, no model, and the same loud failure as before."""

    rt.llm = LlmConfigManager(rt)
    settings = Settings(llm_base_url="", llm_api_key="", llm_model="")

    assert configure_from_settings(rt.llm, settings) is False

    assert isinstance(rt.agent.model, UnconfiguredModelClient)
    assert rt.agent.model_configured is False


def test_the_settings_expose_only_two_modes(rt, session):
    client = client_for(rt, session)

    snapshot = client.get("/api/settings/llm").json()["data"]

    assert set(snapshot) == {
        "configured",
        "mode",
        "base_url",
        "model",
        "response_format",
        "timeout_seconds",
        "api_key_masked",
    }
    assert snapshot["mode"] == "unconfigured"


def test_the_runtime_has_no_task_or_control_surface(rt):
    """The retired control plane must not come back through the composition root.

    `conversations` was on this list because it named the old *in-memory conversation buffer*.
    It is deliberately not on it any more: the conversation ledger is a persisted service the
    persistence spec requires, which is a different thing that happens to share the name. What
    the guard actually protects is pinned below, so re-introducing a buffer still fails.
    """

    for retired in ["control", "tasks"]:
        assert not hasattr(rt, retired)
    # A buffer would be a list or a dict held on the runtime; the store is a service over the
    # database, and its state survives the process.
    assert not isinstance(rt.conversations, (list, dict))
    assert hasattr(rt.conversations, "create") and hasattr(rt.conversations, "messages")


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_the_retired_control_plane_modules_are_gone(module):
    with pytest.raises(ImportError):
        importlib.import_module(module)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/v1"),
        ("https://api.example.com/", "https://api.example.com/v1"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("http://127.0.0.1:8000/openai/v1", "http://127.0.0.1:8000/openai/v1"),
    ],
)
def test_base_url_normalization(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize("value", ["", "example.com/v1", "ftp://api.example.com", "https://"])
def test_base_url_must_be_an_absolute_http_url(value):
    with pytest.raises(ValueError):
        ChatCompletionsConfig(base_url=value, api_key="sk-x", model="m")
