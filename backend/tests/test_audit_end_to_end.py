"""One real turn, traced from the user's message to the tool's private payload.

This is the acceptance test for the whole feature: a turn is submitted over HTTP, the model
is called four times, one of those calls invokes a tool whose result carries a module-private
`facts` block, and the reply is published. Afterwards the audit trail has to be able to
reconstruct all of it -- the exact prompts, the exact provider answers, the exact tool
arguments and the exact tool result -- while the stream the browser received carries none of
the private material.

Every other test in this area pins one piece. This one pins that the pieces are the same
story: one conversation, one run, and a chain of records that a reader can actually walk.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.runtime import AgentRuntime
from app.audit.model_client import AuditedModelClient
from app.conversation.service import ConversationService
from app.main import create_app
from app.agents.domain import build_agent_graph
from app.runtime import agent_graph_factory
from tests.support.domain_harness import generic_roster
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolInvocation, ToolResult, ToolSpec, ToolStatus
from tests.support.scripted_model import ScriptedModel
from tests.test_multi_agent_api import frames

ECHO = ToolSpec(
    name="echo",
    description="Return what it was given",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)

# What the model is asked, in order: delegate to the domain agent, call the tool, report on
# the tool round (spec §4.8.3), and answer the user. Four calls, which is what a tool-using
# turn costs.
REPLIES = [
    ("call", "handoff", '{"agent_id":"transfer","instruction":"处理用户的请求"}'),
    ("call", "echo", '{"value":"v"}'),
    "已经处理好了",
    "已经为您处理好了",
]

FACTS = {"records": [{"record_id": "rec_1", "amount": "100.00"}], "private": "模块私有字段"}


class FactsAdapter:
    """An adapter whose envelope carries the module-private `facts` the frame must not show."""

    def __init__(self) -> None:
        self.invocations: list[ToolInvocation] = []

    def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        return ToolResult(
            tool_name=invocation.call.tool_name,
            status=ToolStatus.SUCCEEDED,
            call_id=invocation.call.call_id,
            data={
                "summary": "回显完成",
                "display": {"records": 1},
                "business_ids": {"record_id": "rec_1"},
                "facts": FACTS,
            },
        )


def wire(rt, replies=REPLIES, adapter=None):
    """Install a tool-using runtime whose model calls are audited, exactly as production is."""

    adapter = adapter or FactsAdapter()
    gateway = ToolGateway(
        ToolCatalog.build([ECHO], {"transfer": ["echo"]}), adapter, audit=rt.audit
    )
    model = AuditedModelClient(ScriptedModel(list(replies)), rt.audit)
    rt.agent = AgentRuntime(
        model,
        graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
            model, gateway, context, assembler=assembler, turn=turn, roster=generic_roster()
        ),
    )
    return adapter


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def conversation_for(rt, session, title=None) -> dict:
    return ConversationService(rt.database).create(session["user_id"], title).as_dict()


def submit(client, conversation_id, message, *, key=None):
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"message": message},
        headers={"Idempotency-Key": key or f"e2e-{uuid4().hex}"},
    )


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_tool_using_turn_is_fully_reconstructable_from_the_audit_trail(rt, session):
    client = client_for(rt, session)
    created = conversation_for(rt, session, "端到端")
    adapter = wire(rt)

    response = submit(client, created["conversation_id"], "帮我处理一下")
    assert response.status_code == 200, response.text
    assert "event: final" in response.text

    # --- the ledger: what the user saw -------------------------------------------------
    ledger = ConversationService(rt.database).messages(
        session["user_id"], created["conversation_id"]
    )["items"]
    assert [item["role"] for item in ledger] == ["user", "assistant"]
    assert ledger[1]["content"] == "已经为您处理好了"
    run_id = ledger[0]["run_id"]
    assert run_id is not None and ledger[1]["run_id"] == run_id

    # --- the audit trail: what actually happened ---------------------------------------
    calls = get(client, "/api/audit/llm-calls", run_id=run_id)["items"]
    events = get(client, "/api/audit/run-events", run_id=run_id)["items"]

    assert len(calls) == 4, "one record per model call, in the order they were made"
    assert [item["result"] for item in calls] == ["SUCCEEDED"] * 4
    assert len({item["call_id"] for item in calls}) == 4, "each call has its own id"
    assert {item["conversation_id"] for item in calls} == {created["conversation_id"]}
    assert {item["source"] for item in calls} == {"conversation"}
    # The prompts are stored verbatim, so a reader can see what the model was actually told.
    assert all(item["request"]["messages"] for item in calls)
    assert any("帮我处理一下" in item["request"]["messages"][0]["content"] for item in calls)
    assert [item["response_text"] for item in calls][-1] == "已经为您处理好了"

    # --- the tool call, arguments and complete result ----------------------------------
    tool_calls = [item for item in events if item["event_type"] == "TOOL_CALL"]
    tool_results = [item for item in events if item["event_type"] == "TOOL_RESULT"]
    assert len(tool_calls) == 1 and len(tool_results) == 1
    assert tool_calls[0]["payload"]["arguments"] == {"value": "v"}
    assert tool_calls[0]["payload"]["tool"] == "echo"
    assert tool_calls[0]["payload"]["trusted_context"]["user_id"] == session["user_id"]
    assert tool_calls[0]["payload"]["idempotency_key"]
    # The complete ToolResult, `facts` included: this is the material the frame projected away.
    assert tool_results[0]["payload"]["result"]["data"]["facts"] == FACTS
    assert tool_results[0]["status"] == "SUCCEEDED"

    # --- the run's own lifecycle --------------------------------------------------------
    kinds = [item["event_type"] for item in events]
    assert kinds[0] == "RUN_STARTED"
    assert kinds[-1] == "RUN_SUCCEEDED"
    assert "AGENT_STARTED" in kinds
    assert {item["run_id"] for item in events} == {run_id}

    # --- and it all points at one conversation and one run ------------------------------
    by_conversation = get(
        client, "/api/audit/llm-calls", conversation_id=created["conversation_id"]
    )["items"]
    assert [item["call_id"] for item in by_conversation] == [item["call_id"] for item in calls]
    assert len(adapter.invocations) == 1, "the tool really was called exactly once"


def test_the_browser_never_receives_the_private_facts_or_the_raw_model_object(rt, session):
    """The stream is a display channel. What it shows is a whitelist, and this pins it."""

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    wire(rt)

    response = submit(client, created["conversation_id"], "帮我处理一下")
    tool_result = next(data for name, data in frames(response) if name == "tool_result")

    # The envelope the browser gets is projected: three fields, and `facts` is not one.
    assert set(tool_result["data"]) == {"summary", "display", "business_ids"}
    assert "facts" not in response.text
    assert "模块私有字段" not in response.text
    assert "rec_1" in response.text, "the published business id is still published"
    # And no raw provider object either: the stream carries decisions, not wire payloads.
    assert "chatcmpl" not in response.text
    assert "只输出一个 JSON 对象" not in response.text


def test_a_failing_tool_is_still_fully_recorded(rt, session):
    """A failure is an outcome worth tracing, and its payload is kept in full."""

    class FailingAdapter:
        def invoke(self, invocation):
            return ToolResult(
                tool_name=invocation.call.tool_name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error="上游不可用",
                retryable=False,
                error_code="UPSTREAM_UNAVAILABLE",
                data={"facts": {"attempts": 1}},
            )

    client = client_for(rt, session)
    created = conversation_for(rt, session)
    wire(rt, adapter=FailingAdapter())

    submit(client, created["conversation_id"], "帮我处理一下")

    events = get(client, "/api/audit/run-events", event_type="TOOL_RESULT")["items"]
    assert len(events) == 1
    assert events[0]["status"] == "FAILED"
    assert events[0]["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert events[0]["payload"]["result"]["error"] == "上游不可用"
    assert events[0]["payload"]["result"]["data"]["facts"] == {"attempts": 1}


def test_a_configuration_probe_lands_in_the_same_trail(rt, session):
    """A model call with no conversation is recorded like any other, under its own source."""

    from app.audit.context import SOURCE_CONFIGURATION_TEST
    from app.integration.model_client import ModelReply

    class ProbeModel:
        def complete(self, messages):
            return ModelReply(text='{"ok":true}', raw={"choices": [{"message": {"content": "x"}}]})

        def close(self):
            return None

    client = client_for(rt, session)
    model = AuditedModelClient(ProbeModel(), rt.audit)
    rt.agent.set_model(model)

    from app.audit.context import AuditContext, audit_context

    with audit_context(AuditContext(user_id=session["user_id"], source=SOURCE_CONFIGURATION_TEST)):
        model.complete(({"role": "user", "content": "probe"},))

    page = get(client, "/api/audit/llm-calls")

    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["source"] == SOURCE_CONFIGURATION_TEST
    assert item["conversation_id"] is None and item["run_id"] is None
    assert item["response"]["choices"] == [{"message": {"content": "x"}}]


def test_the_settings_panel_probe_is_audited_under_its_own_source(rt, session):
    """`POST /api/settings/llm/test` is a model call, and it goes through the same boundary."""

    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class ReplyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            body = json.dumps(
                {
                    "id": "chatcmpl_probe",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "probe-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": '{"ok":true}'},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            del args

    from app.integration.llm_config import LlmConfigManager

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReplyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rt.llm = LlmConfigManager(rt)
        client = client_for(rt, session)

        response = client.post(
            "/api/settings/llm/test",
            json={
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "api_key": "sk-probe-never-audited",
                "model": "probe-model",
                "response_format": "json_schema",
                "timeout_seconds": 10,
            },
        )

        assert response.status_code == 200, response.text
        items = get(client, "/api/audit/llm-calls")["items"]
        assert len(items) == 1, "the probe is a real model call and was recorded"
        item = items[0]
        assert item["source"] == "configuration_test"
        assert (item["conversation_id"], item["run_id"]) == (None, None)
        assert item["response"]["id"] == "chatcmpl_probe", "the whole provider response is kept"
        # The credential is not in the record, and therefore not in the response.
        assert "sk-probe-never-audited" not in response.text
        assert "sk-probe-never-audited" not in client.get("/api/audit/llm-calls").text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
