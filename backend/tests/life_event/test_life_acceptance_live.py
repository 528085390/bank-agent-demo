"""现场验收 AC-3：真实银行进程上的生日订单，由银行自己的模拟时钟推进到执行日。

只有显式设置 LIFE_TEST_BANK_URL 时才运行；没设置就跳过 —— 默认不碰开发共享服务，也不在别人的
数据上跑测试。这个用例会重置银行演示数据（POST /api/seed/reset），所以那个服务必须是可以随便清
的测试实例：

    $env:LIFE_TEST_BANK_URL = "http://127.0.0.1:8001/api"
    cd backend; uv run pytest tests/life_event/test_life_acceptance_live.py -q

链路：Agent 走生产 MODULE 图下单（黄标卡 -> 确认）-> 银行回 pending + task_id -> 把银行时钟拨到
execute_at 之后 -> POST /api/clock/run-tasks -> 断言订单 completed，且流水里多出一笔
transaction_type == "birthday" 的借记。

Agent 侧时钟钉在银行时钟上（同一个时刻），否则读侧算的生日和写侧记的当日累计不是同一天。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import os
from datetime import datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime, thread_id_for_session
from app.agents.supervisor import build_domain_graph
from app.core.clock import CST
from app.integration import write_facade as write_facade_module
from app.integration.bank_client import BankClient
from app.integration.write_facade import WriteFacade
from app.modules.life_event import MODULE
from app.modules.life_event import tools as life_tools
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from tests.support.scripted_model import ScriptedModel

BANK_URL_ENV = "LIFE_TEST_BANK_URL"
APP_USER = "user_0001"
CUSTOMER = "u_zhang"
GOAL = "帮爱人挑一份生日礼物"

CONTEXT = TrustedContext(APP_USER, "sess_life_live", CUSTOMER)


def _frozen_clock(now: datetime):
    """按银行时钟造一个 SystemClock 替身：tools 与 write_facade 都调 today()/now()。"""

    class _Clock:
        @staticmethod
        def today(client=None):
            return now.date()

        @staticmethod
        def now(client=None):
            return now

    return _Clock


def call(name, arguments=None):
    return ("call", name, json.dumps(arguments or {}, ensure_ascii=False))


def invoke(runtime, context=None):
    async def collect():
        return [
            event async for event in runtime.invoke("sess_life_live", APP_USER, GOAL, context or {})
        ]

    return asyncio.run(collect())


def interrupt(events):
    return next(event.data for event in reversed(events) if event.event == "interrupt")


def resume(events, value):
    return {"resume": {"question_id": interrupt(events)["question_id"], "value": dict(value)}}


def subtask(events):
    return next(event.data for event in events if event.event == "subtask_end")


def order_facts(graph) -> dict:
    state = asyncio.run(
        graph.aget_state({"configurable": {"thread_id": thread_id_for_session("sess_life_live")}})
    )
    return state.values["domain_results"][-1]["facts"]


@pytest.fixture(scope="module")
def live_bank() -> BankClient:
    url = os.getenv(BANK_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"未设置 {BANK_URL_ENV}：本用例只对可重置数据的银行测试实例运行")
    client = BankClient(url, connect_timeout=5.0, read_timeout=10.0)
    try:
        yield client
    finally:
        client.close()


def reset_bank(live_bank: BankClient) -> datetime:
    """重置演示数据，并把银行当前（模拟）时间读回来。"""

    response = live_bank.request("POST", "/seed/reset", user_id=CUSTOMER, json={})
    assert response.data is not None, "seed/reset 没有回数据"
    current = datetime.fromisoformat(response.data["current_time"])
    assert current.tzinfo is not None, "银行时钟必须带时区"
    return current.astimezone(CST)


def flowers_price(live_bank: BankClient) -> int:
    response = live_bank.request("GET", "/mock-products", user_id=CUSTOMER)
    products = response.data or []
    return next(item["price_minor"] for item in products if item["product_code"] == "flowers")


def test_ac3_advancing_the_bank_clock_completes_the_order_and_writes_a_birthday_debit(
    rt, live_bank, monkeypatch
):
    bank_now = reset_bank(live_bank)
    price_minor = flowers_price(live_bank)
    monkeypatch.setattr(life_tools, "SystemClock", _frozen_clock(bank_now))
    monkeypatch.setattr(write_facade_module, "SystemClock", _frozen_clock(bank_now))

    module = MODULE
    module = dataclasses.replace(
        module,
        handlers={
            **module.handlers,
            "life_prepare_order": functools.partial(
                module.handlers["life_prepare_order"], database=rt.database
            ),
        },
    )
    registry = build_registry([module])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, live_bank))
    model = ScriptedModel(
        [call("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"})]
    )
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]
    spec = dataclasses.replace(
        spec,
        declaration=dataclasses.replace(
            spec.declaration,
            graph_builder=functools.partial(
                spec.declaration.graph_builder,
                writes=WriteFacade(live_bank, database=rt.database),
            ),
        ),
    )
    graph = build_domain_graph(model, spec, client=live_bank).copy(
        update={"checkpointer": InMemorySaver()}
    )
    runtime = AgentRuntime(graph=graph)

    # 1) 黄标卡：不需要验证码，金额就是银行给的鲜花价。
    events = invoke(runtime)
    card = interrupt(events)["confirmation"]
    assert card["requires_code"] is False
    assert card["amount_minor"] == price_minor
    assert subtask(events)["turn_status"] == "waiting_confirmation"

    # 2) 确认：银行落单，回 task_id，订单停在 pending 等任务执行。
    done = invoke(runtime, resume(events, {"decision": "confirm"}))
    assert subtask(done)["turn_status"] == "completed"
    order = order_facts(graph)["order"]
    assert order["status"] == "pending"
    assert order["task_id"]
    assert order["amount_minor"] == price_minor

    execute_at = datetime.fromisoformat(order["details"]["execute_at"])
    assert execute_at > bank_now, "下单时订单的执行时间必须还在将来"

    # 3) 把银行时钟拨到执行时间之后，再跑一次到期任务。
    target = execute_at + timedelta(minutes=1)
    advanced = live_bank.request(
        "POST", "/clock", user_id=CUSTOMER, json={"current_time": target.isoformat()}
    )
    assert datetime.fromisoformat(advanced.data["current_time"]) >= target

    ran = live_bank.request("POST", "/clock/run-tasks", user_id=CUSTOMER, json={})
    executed = next(item for item in ran.data["items"] if item["task_id"] == order["task_id"])
    assert executed["status"] == "completed"

    # 4) 订单自己变成 completed，并挂上了流水号。
    stored = live_bank.request("GET", f"/birthdays/orders/{order['order_id']}", user_id=CUSTOMER)
    assert stored.data["status"] == "completed"
    assert stored.data["transaction_id"]

    # 5) 流水里真的多了一笔生日借记，金额与订单一致。
    ledger = live_bank.request(
        "GET",
        "/accounts/transactions",
        user_id=CUSTOMER,
        params={"transaction_type": "birthday", "page_size": 100},
    )
    entries = [
        entry for entry in ledger.data["items"] if entry["reference_id"] == order["order_id"]
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["direction"] == "debit"
    assert entry["amount_minor"] == price_minor
    assert entry["transaction_type"] == "birthday"
    assert entry["category"] == "gift"
    assert entry["created_at"].startswith(target.date().isoformat())
