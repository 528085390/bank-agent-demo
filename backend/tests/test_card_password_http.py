"""Real HTTP Agent -> bank acceptance, using a scripted model and isolated PostgreSQL.

This proves transport, authorization and persistence; it is not a real-model language test.
"""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select, text

from app.agents.runtime import AgentRuntime
from app.integration.bank_client import BankClient
from app.main import create_app
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory
from app.storage.models import AppUser
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from tests.conftest import resolve_test_database_url
from tests.support.scripted_model import ScriptedModel
from tests.test_multi_agent_api import frames

OLD, NEW, LAST, CODE = "483719", "725903", "903176", "572809"


@pytest.mark.parametrize("apply_first", [False, True])
def test_password_changes_over_agent_and_bank_http(rt, session, caplog, apply_first):
    # The isolated bank seed has this customer; bind it server-side, never in a request.
    # Team-wide production demo bindings are deliberately left untouched.
    with rt.database.sessions.begin() as db:
        db.scalar(
            select(AppUser).where(AppUser.user_id == session["user_id"])
        ).bank_customer_id = "u_zhang"
    bank_root = Path(__file__).resolve().parents[2] / "bank_backend"
    bank_python = bank_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    schema = "test_bank_pin_http_" + uuid4().hex
    database_url = resolve_test_database_url()
    admin = create_engine(database_url, hide_parameters=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    bank_socket = socket.socket()
    bank_socket.bind(("127.0.0.1", 0))
    bank_port = bank_socket.getsockname()[1]
    bank_socket.close()
    bank_process = None
    bank = None
    server = None
    thread = None
    try:
        bank_source = """
from alembic import command
from alembic.config import Config
from app.core.config import get_settings
from app.storage.database import Database
from app.domain.seed_service import SeedService
import sys, uvicorn
command.upgrade(Config('alembic.ini'), 'head')
database = Database(get_settings().database_url)
SeedService(database).seed()
database.dispose()
uvicorn.run('app.main:app',host='127.0.0.1',port=int(sys.argv[1]),log_level='error')
"""
        env = {
            **os.environ,
            "DATABASE_URL": database_url,
            "PGOPTIONS": f"-c search_path={schema}",
            "DEMO_CARD_PASSWORD": OLD,
            "MOCK_MFA_CODE": CODE,
            "DEFAULT_USER_ID": "u_li",
            "SERVICE_TOKEN": rt.settings.bank_service_token,
            "PYTHONIOENCODING": "utf-8",
        }
        bank_process = subprocess.Popen(
            [str(bank_python), "-c", bank_source, str(bank_port)],
            cwd=bank_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        bank_url = f"http://127.0.0.1:{bank_port}/api"
        with httpx.Client(trust_env=False, timeout=1) as probe:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if bank_process.poll() is not None:
                    raise AssertionError("isolated bank failed to start")
                try:
                    if probe.get(bank_url + "/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("isolated bank startup timed out")
        bank = BankClient(bank_url, service_token=rt.settings.bank_service_token)
        registry = build_registry()
        gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank), audit=rt.audit)
        model = ScriptedModel([])
        saver = InMemorySaver()
        rt.bank_client = bank
        rt.agent = AgentRuntime(
            model,
            graph_factory=agent_graph_factory(gateway, client=bank),
            checkpointer=saver,
            customer_resolver=rt.identity.bank_customer_id,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(rt.settings, runtime=rt),
                    log_level="error",
                    loop="app.core.eventloop:selector_loop",
                )
            )
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            base = f"http://127.0.0.1:{listener.getsockname()[1]}/api"
            collected = []
            with httpx.Client(
                base_url=base,
                headers={"Authorization": "Bearer " + session["token"]},
                trust_env=False,
                timeout=30,
            ) as client:
                target_suffix = "8801"
                target_card = "card_zhang_debit"

                def preview(*, issue=False):
                    model._replies.extend(
                        [
                            (
                                "call",
                                "handoff",
                                '{"agent_id":"card","instruction":"申请一张虚拟卡"}'
                                if issue
                                else (
                                    '{"agent_id":"card","instruction":"修改尾号'
                                    + target_suffix
                                    + '的银行卡交易密码"}'
                                ),
                            ),
                            ("call", "card_apply", '{"card_type":"virtual"}')
                            if issue
                            else (
                                "call",
                                "card_change_password",
                                '{"card_suffix":"' + target_suffix + '"}',
                            ),
                        ]
                    )
                    cid = client.post("/conversations", json={"title": "改密 HTTP 验收"}).json()[
                        "data"
                    ]["conversation_id"]
                    response = client.post(
                        f"/conversations/{cid}/messages",
                        json={
                            "message": "申请一张虚拟卡"
                            if issue
                            else "修改尾号" + target_suffix + "的银行卡交易密码"
                        },
                        headers={"Idempotency-Key": uuid4().hex},
                    )
                    collected.append(response.text)
                    question = next(data for name, data in frames(response) if name == "interrupt")
                    return cid, question

                def click(cid, question, old=OLD, new=NEW, *, decision="confirm", key=None):
                    body = {"question_id": question["question_id"], "decision": decision}
                    if decision == "confirm":
                        body.update(
                            verification_code=CODE,
                            transaction_password={
                                **(
                                    {"old_password": old}
                                    if question["confirmation"]["operation"] != "apply"
                                    else {}
                                ),
                                "new_password": new,
                                "confirm_password": new,
                            },
                        )
                    return client.post(
                        f"/conversations/{cid}/pending-actions/{question['confirmation']['pending_id']}/decide",
                        json=body,
                        headers={"Idempotency-Key": key or uuid4().hex},
                    )

                existing_ids = {
                    row["card_id"] for row in bank.request("GET", "/cards", user_id="u_zhang").data
                }
                cid, question = preview(issue=apply_first)
                key = uuid4().hex
                success = click(cid, question, key=key)
                assert success.status_code == 200 and any(
                    name == "final" for name, _ in frames(success)
                )
                expected = "银行卡操作已完成" if apply_first else "交易密码修改成功"
                assert expected in success.text
                replay = click(cid, question, key=key)
                assert (
                    replay.status_code == 200
                    and "application/json" in replay.headers["content-type"]
                )
                collected.extend([success.text, replay.text])
                if apply_first:
                    issued = [
                        row
                        for row in bank.request("GET", "/cards", user_id="u_zhang").data
                        if row["card_id"] not in existing_ids
                    ]
                    assert len(issued) == 1 and issued[0]["card_type"] == "virtual"
                    target_suffix, target_card = issued[0]["card_number"][-4:], issued[0]["card_id"]
                cid2, question2 = preview()
                wrong_old = click(cid2, question2, old=OLD, new=LAST)
                assert wrong_old.status_code == 422
                # The former new PIN now authorizes another change; the former old PIN failed.
                new_works = click(cid2, question2, old=NEW, new=LAST)
                assert new_works.status_code == 200 and any(
                    name == "final" for name, _ in frames(new_works)
                )
                collected.extend([wrong_old.text, new_works.text])
                cid3, question3 = preview()
                cancelled = click(cid3, question3, decision="cancel")
                assert cancelled.status_code == 200
                assert any(name == "final" for name, _ in frames(cancelled))
                collected.append(cancelled.text)
                for conversation_id in (cid, cid2):
                    collected.append(client.get(f"/conversations/{conversation_id}/messages").text)
                collected.extend(
                    [client.get("/audit/run-events").text, client.get("/audit/llm-calls").text]
                )
            collected.extend(
                [
                    bank.request("GET", "/cards", user_id="u_zhang").data,
                    bank.request("GET", "/audit", user_id="u_zhang").data,
                    model.messages,
                    model.tools,
                    saver.storage,
                    saver.writes,
                    caplog.text,
                ]
            )
            raw = str(collected)
            assert all(value not in raw for value in (OLD, NEW, LAST, CODE, "scrypt$"))
            with admin.connect().execution_options(schema_translate_map=None) as connection:
                assert connection.execute(
                    text(
                        f"SELECT password_version FROM \"{schema}\".cards WHERE id='{target_card}'"
                    )
                ).scalar_one() == (1 if apply_first else 2)
            # Stop Uvicorn before the owning socket context closes.
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        if bank:
            bank.close()
        if bank_process:
            bank_process.terminate()
            output, errors = bank_process.communicate(timeout=10)
            assert all(
                value.encode() not in output + errors for value in (OLD, NEW, LAST, CODE, "scrypt$")
            )
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
