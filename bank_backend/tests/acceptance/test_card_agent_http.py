"""Production card graph/client over HTTP against the real isolated bank application."""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from tests.conftest import SERVICE_TOKEN

AGENT_SCENARIO = r"""
import asyncio
import json
import os
import sys

from app.agents.runtime import AgentRuntime
from app.integration.bank_client import BankClient
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from tests.support.scripted_model import ScriptedModel

calls = [
    ("cards", {}),
    ("apply", {"card_type": "virtual"}),
    ("update_limit", {"card_suffix": "8801", "daily_limit_yuan": "1000.01"}),
    ("update_restrictions", {"card_suffix": "8801", "mode": "add", "merchants": ["测试商户"]}),
    ("freeze", {"card_suffix": "8801"}),
    ("unfreeze", {"card_suffix": "8801"}),
    ("report_loss", {"card_suffix": "8801"}),
    ("release_loss", {"card_suffix": "8801"}),
]
bank = BankClient(sys.argv[1], service_token=os.environ["BANK_SERVICE_TOKEN"], read_timeout=30)
registry = build_registry()
gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
model = ScriptedModel([])
agent = AgentRuntime(model, graph_factory=agent_graph_factory(gateway, client=bank),
                     customer_resolver=lambda _: "u_zhang")


def script(*steps):
    # Re-arm the double for one phase, so a phase never has to guess its call count.
    # (No docstring here: triple quotes would close the enclosing AGENT_SCENARIO string.)
    model._replies.clear()
    model._replies.extend(steps)


def answer(operation, args):
    # Native tool calling: the Supervisor hands off with the handoff tool, the card domain
    # calls its own tool, then each side answers in plain text.
    return [
        ("call", "handoff", json.dumps({"agent_id": "card", "instruction": "办理 " + operation})),
        ("call", "card_" + operation, json.dumps(args)),
        "办理结果已返回",
        "办理结果已返回",
        "办理结果已返回",
    ]


async def collect(context=None):
    return [e async for e in agent.invoke("sess_http", "user_0001", "办理卡片业务", context or {})]

async def scenario():
    for operation, args in calls:
        script(*answer(operation, args))
        events = await collect()
        assert "card_zhang_debit" not in str(events)
        if operation != "cards":
            question = next(e.data for e in events if e.event == "interrupt")
            action_id = question["confirmation"]["pending_id"]
            card = bank.request("GET", "/cards/card_zhang_debit", user_id="u_zhang").data
            if operation == "report_loss":
                assert card["status"] == "active"
            # 申请卡走独立密码表单：新卡初始交易密码只能在这个接口提交，
            # 通用 /authorize 只接受验证码。其余卡片操作仍走通用授权接口。
            if operation == "apply":
                bank.request(
                    "POST",
                    f"/cards/actions/{action_id}/password/authorize",
                    user_id="u_zhang",
                    json={
                        "decision": "confirm",
                        "verification_code": "123456",
                        "new_password": "572809",
                        "confirm_password": "572809",
                    },
                )
            else:
                bank.request("POST", f"/cards/actions/{action_id}/authorize", user_id="u_zhang",
                             json={"decision": "confirm", "verification_code": "123456"})
            script("办理结果已返回", "办理结果已返回", "办理结果已返回")
            events = await collect({"resume": {"question_id": question["question_id"],
                                               "value": {"decision": "confirm"}}})
            if operation == "report_loss":
                card = bank.request("GET", "/cards/card_zhang_debit", user_id="u_zhang").data
                assert card["status"] == "frozen" and card["freeze_reason"] == "lost"
        assert any(e.event == "subtask_end" and e.data["status"] == "SUCCEEDED" for e in events), str(events)
        assert any(e.event == "final" for e in events)
    card = bank.request("GET", "/cards/card_zhang_debit", user_id="u_zhang").data
    assert card["status"] == "active" and card["freeze_reason"] is None
    assert card["daily_limit_minor"] == 100001 and card["monthly_limit_minor"] == 5000000
    assert card["merchant_blacklist"] == ["测试商户"]
    print("8 production card scenarios passed over real HTTP")

try:
    asyncio.run(scenario())
finally:
    bank.close()
"""


def test_production_agent_executes_card_scenarios_over_real_http(seeded_client):
    backend = Path(__file__).resolve().parents[3] / "backend"
    python = backend / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        pytest.skip("Install the Agent backend environment to run cross-service acceptance")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(seeded_client.app, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            completed = subprocess.run(
                [
                    str(python),
                    "-c",
                    AGENT_SCENARIO,
                    f"http://127.0.0.1:{listener.getsockname()[1]}/api",
                ],
                cwd=backend,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={
                    **os.environ,
                    # 银行现在拒绝没有服务凭证的调用；子进程是「app 后端」的替身，
                    # 所以把同一把钥匙传下去（与 conftest 里 bank 侧校验的值一致）。
                    "BANK_SERVICE_TOKEN": SERVICE_TOKEN,
                    "PYTHONIOENCODING": "utf-8",
                    "NO_PROXY": "",
                    "no_proxy": "",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                },
                timeout=120,
            )
            assert completed.returncode == 0, completed.stderr
            assert "8 production card scenarios passed" in completed.stdout
        finally:
            server.should_exit = True
            thread.join(timeout=10)
