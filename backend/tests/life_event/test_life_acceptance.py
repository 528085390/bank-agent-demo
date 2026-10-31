"""现场验收 AC-1 / AC-2 / AC-2-RED：全部走生产 MODULE 图，一层都不绕过。

- **AC-1 识别联系人并给出礼物方案**：模型先 life_birthdays 再 life_gift_plan，工具从银行
  读回「爱人」的生日（9 月 25 日）与两款候选礼物；最终答复不是 NO_TOOLS_SUMMARY。
- **AC-2 黄标卡 -> 确认 -> 银行落单**：卡片 requires_code=False、金额是正整数分；确认后银行
  收到带 confirm 的下单请求、回一个 task_id，订单停在 pending 等任务执行，本地当日累计按订单
  金额增加。
- **AC-2-RED 红标卡 -> 验证码 -> 落单**：当日累计先真写一笔，再下生日单就越过日限额；卡片
  risk_level=red / requires_code=True，带有效验证码确认后落单成功。

依赖注入与 bind_life_event 一致：database 用 functools.partial 绑到 life_prepare_order，
writes 用 functools.partial 绑到 declaration.graph_builder。时钟钉在 life_tools.SystemClock
与 write_facade.SystemClock 两处：读侧按 2031 年算生日，写侧按同一天记当日累计，不钉住的话
两边看的不是同一天。

日限额阈值只有 app/policy/types.py 一个来源，这里从 DAILY_RED_THRESHOLD_MINOR 推出来，不写死
数字。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from datetime import date, datetime
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph, build_domain_specs
from app.agents.runtime import AgentRuntime, TurnContext, thread_id_for_session
from app.agents.subgraph_templates import NO_TOOLS_SUMMARY
from app.agents.supervisor import build_domain_graph
from app.agents.turn import TurnResult, TurnStatus
from app.core.clock import CST
from app.integration import write_facade as write_facade_module
from app.integration.write_facade import WriteFacade
from app.modules.life_event import MODULE
from app.modules.life_event import tools as life_tools
from app.modules.registry import build_registry
from app.policy import DAILY_RED_THRESHOLD_MINOR
from app.storage.daily_total import DailyTotalStore
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient
from tests.support.handler_graph import handler_module
from tests.support.scripted_model import ScriptedModel, StreamingModel

APP_USER = "user_0001"
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
NOW = datetime(2031, 3, 4, 9, 0, tzinfo=CST)
GOAL = "帮爱人挑一份生日礼物"

CONTEXT = TrustedContext(APP_USER, "sess_life", CUSTOMER)

# 银行 MOCK_PRODUCTS 的同形拷贝：鲜花 19900 分、蛋糕 12900 分。
FLOWERS_MINOR = 19900
CAKE_MINOR = 12900

# 红标卡的存量：只比日限额少一点，再叠一笔生日单就翻红。
RED_PRESEED_MINOR = DAILY_RED_THRESHOLD_MINOR - 10000

PARTNER = {
    "contact_id": "c_partner",
    "user_id": CUSTOMER,
    "name": "爱人",
    "phone": "13800138002",
    "remark": "家人",
    "recipient_user_id": "u_partner",
    "recipient_account_id": "a_partner_current",
    "birthday": "1995-09-25",
}

COLLEAGUE = {
    "contact_id": "c_colleague",
    "user_id": CUSTOMER,
    "name": "同事1",
    "phone": "13800138003",
    "remark": "同事",
    "recipient_user_id": None,
    "recipient_account_id": None,
    "birthday": None,
}

PRODUCTS = [
    {
        "product_code": "flowers",
        "name": "模拟鲜花",
        "price_minor": FLOWERS_MINOR,
        "currency": "CNY",
    },
    {"product_code": "cake", "name": "模拟蛋糕", "price_minor": CAKE_MINOR, "currency": "CNY"},
]

CURRENT_ACCOUNT = {
    "account_id": "a_zhang_current",
    "user_id": CUSTOMER,
    "account_type": "current",
    "balance_minor": 2465950,
    "available_minor": 2465950,
    "currency": "CNY",
    "interest_rate": "0.0025",
    "status": "active",
}

FIXED_ACCOUNT = {
    "account_id": "a_zhang_fixed",
    "user_id": CUSTOMER,
    "account_type": "fixed",
    "balance_minor": 5000000,
    "available_minor": 5000000,
    "currency": "CNY",
    "interest_rate": "0.02",
    "status": "active",
}

ORDER = {
    "order_id": "o_birthday_1",
    "order_type": "birthday",
    "user_id": CUSTOMER,
    "amount_minor": FLOWERS_MINOR,
    "status": "pending",
    "task_id": "t_birthday_1",
    "transaction_id": None,
    "details": {
        "contact_id": "c_partner",
        "contact_name": "爱人",
        "product_code": "flowers",
        "product_name": "模拟鲜花",
        "birthday_date": "2031-09-25",
        "execute_at": "2031-09-23T08:00:00+08:00",
        "task_id": "t_birthday_1",
    },
}


class _FixedClock:
    @staticmethod
    def today(client=None) -> date:
        return BUSINESS_DAY

    @staticmethod
    def now(client=None) -> datetime:
        return NOW


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(life_tools, "SystemClock", _FixedClock)
    monkeypatch.setattr(write_facade_module, "SystemClock", _FixedClock)
    return _FixedClock


def bank() -> FakeBankClient:
    client = FakeBankClient()
    client.register("/birthdays", [PARTNER, COLLEAGUE])
    client.register("/mock-products", PRODUCTS)
    client.register("/accounts", [CURRENT_ACCOUNT, FIXED_ACCOUNT])
    client.register("/birthdays/orders", ORDER)
    return client


@dataclasses.dataclass(frozen=True)
class Life:
    """一次验收要用的全部把手：运行时、模型、编译后的图、装配好的模块。"""

    runtime: AgentRuntime
    model: ScriptedModel
    graph: Any
    module: Any


def life_for(
    bank_client,
    replies,
    *,
    turn=None,
    checkpointer=None,
    writes=None,
    database=None,
) -> Life:
    """按 bind_life_event 的注入方式装配生产图。"""

    module = MODULE
    if database is not None:
        module = dataclasses.replace(
            module,
            handlers={
                **module.handlers,
                "life_prepare_order": functools.partial(
                    module.handlers["life_prepare_order"], database=database
                ),
            },
        )
    registry = build_registry([module])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank_client))
    model = ScriptedModel(list(replies))
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]
    if writes is not None:
        spec = dataclasses.replace(
            spec,
            declaration=dataclasses.replace(
                spec.declaration,
                graph_builder=functools.partial(spec.declaration.graph_builder, writes=writes),
            ),
        )
    graph = build_domain_graph(model, spec, client=bank_client, turn=turn).copy(
        update={"checkpointer": checkpointer or InMemorySaver()}
    )
    return Life(runtime=AgentRuntime(graph=graph), model=model, graph=graph, module=module)


def invoke(life: Life, context=None):
    async def collect():
        stream = life.runtime.invoke("sess_life", APP_USER, GOAL, context or {})
        return [event async for event in stream]

    return asyncio.run(collect())


def resume(events, value):
    return {"resume": {"question_id": interrupt(events)["question_id"], "value": dict(value)}}


def interrupt(events):
    return next(event.data for event in reversed(events) if event.event == "interrupt")


def subtask(events):
    return next(event.data for event in events if event.event == "subtask_end")


def tool_results(events):
    return [event.data for event in events if event.event == "tool_result"]


def order_writes(bank_client):
    return [
        request
        for request in bank_client.requests
        if request["method"] == "POST" and request["path"] == "/birthdays/orders"
    ]


def order_facts(life: Life) -> dict:
    """确认那一轮落进图状态的机读结果（domain_results 是 append 通道）。"""

    state = asyncio.run(
        life.graph.aget_state({"configurable": {"thread_id": thread_id_for_session("sess_life")}})
    )
    return state.values["domain_results"][-1]["facts"]


def daily_total(database) -> int:
    with database.sessions() as session:
        return DailyTotalStore.get_accumulated_minor(session, APP_USER, BUSINESS_DAY)


def invocation(tool, arguments=None):
    return ToolInvocation(
        "life_event",
        ToolCall(
            tool_name=tool,
            arguments=arguments or {},
            call_id="call_life_accept_1",
            idempotency_key="idem-life-accept-1",
        ),
        CONTEXT,
    )


def call(name, arguments=None):
    return ("call", name, json.dumps(arguments or {}, ensure_ascii=False))


def prepare_call(**extra):
    return call(
        "life_prepare_order",
        {"contact_id": "c_partner", "product_code": "flowers", **extra},
    )


def red_turn(sequence=2):
    return TurnContext(
        conversation_id="c_1",
        thread_id="t_1",
        message_id=f"m_{sequence}",
        content="确认下单",
        sequence=sequence,
        verification_code="123456",
    )


# ------------------------------------------------------------------ AC-1


def test_ac1_the_production_graph_recognises_the_partner_and_offers_both_gifts(frozen_clock):
    bank_client = bank()
    life = life_for(
        bank_client,
        [
            call("life_birthdays"),
            call("life_gift_plan", {"budget_yuan": "200"}),
            "爱人 9 月 25 日生日：预算内可以送模拟鲜花（199.00 元），也可以送模拟蛋糕（129.00 元）。",
        ],
    )

    events = invoke(life)

    results = tool_results(events)
    assert [frame["tool_name"] for frame in results] == ["life_birthdays", "life_gift_plan"]
    assert [frame["status"] for frame in results] == ["SUCCEEDED", "SUCCEEDED"]

    birthdays = results[0]["data"]["summary"]
    assert "爱人" in birthdays
    assert "9月25日" in birthdays
    assert "09-25" in birthdays

    gifts = results[1]["data"]["summary"]
    assert "模拟鲜花" in gifts
    assert "模拟蛋糕" in gifts

    # 两次读都真的问过银行，顺序就是模型调用的顺序。
    assert [(request["method"], request["path"]) for request in bank_client.requests] == [
        ("GET", "/birthdays"),
        ("GET", "/mock-products"),
    ]

    # 第二次调模型时第一次的工具结果已经在它的上下文里 —— 「模型知道生日」的证据。
    handed_over = json.dumps(life.model.messages[1], ensure_ascii=False)
    assert "life_birthdays" in handed_over
    assert "9月25日" in handed_over

    done = subtask(events)
    assert done["turn_status"] == "completed"
    assert done["summary"] != NO_TOOLS_SUMMARY
    assert "爱人" in done["summary"]
    assert "模拟鲜花" in done["summary"]
    assert "模拟蛋糕" in done["summary"]


def test_ac1_a_model_that_batches_two_reads_still_gets_both_results(frozen_clock):
    """The provider may send both read calls in one assistant turn.

    The life prompt explicitly allows several read tools in one round; the graph must run
    them all instead of rejecting the turn as an unusable decision.
    """

    bank_client = bank()
    life = life_for(
        bank_client,
        [
            [call("life_birthdays"), call("life_gift_plan", {"budget_yuan": "200"})],
            "爱人 9 月 25 日生日：预算内可以送模拟鲜花（199.00 元）。",
        ],
    )

    events = invoke(life)

    results = tool_results(events)
    assert [frame["tool_name"] for frame in results] == ["life_birthdays", "life_gift_plan"]
    assert [frame["status"] for frame in results] == ["SUCCEEDED", "SUCCEEDED"]
    assert [(request["method"], request["path"]) for request in bank_client.requests] == [
        ("GET", "/birthdays"),
        ("GET", "/mock-products"),
    ]
    assert subtask(events)["turn_status"] == "completed"


# ------------------------------------------------------------------ AC-2


def test_ac2_a_yellow_card_confirms_into_a_pending_order_and_the_day_total_grows(rt, frozen_clock):
    bank_client = bank()
    life = life_for(
        bank_client,
        [prepare_call()],
        checkpointer=InMemorySaver(),
        writes=WriteFacade(bank_client, database=rt.database),
        database=rt.database,
    )

    before = daily_total(rt.database)
    events = invoke(life)
    question = interrupt(events)
    card = question["confirmation"]

    assert card["operation"] == "birthday_order"
    assert card["requires_code"] is False
    assert isinstance(card["amount_minor"], int)
    assert card["amount_minor"] > 0
    assert card["amount_minor"] == FLOWERS_MINOR
    assert "模拟鲜花" in card["product_name"]
    assert subtask(events)["turn_status"] == "waiting_confirmation"
    # 挂卡不动钱：既没有下单请求，当日累计也没变。
    assert order_writes(bank_client) == []
    assert daily_total(rt.database) == before

    done = invoke(life, resume(events, {"decision": "confirm"}))

    posts = order_writes(bank_client)
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["confirm"] is True
    assert "mfa_code" not in body
    assert posts[0]["idempotency_key"]

    # 银行的响应原样落进图状态：task_id 在，订单还是 pending（等任务到点执行）。
    order = order_facts(life)["order"]
    assert order["status"] == "pending"
    assert order["task_id"]
    assert order["amount_minor"] == FLOWERS_MINOR
    assert order["details"]["contact_name"] == "爱人"

    assert subtask(done)["turn_status"] == "completed"
    assert order["order_id"] in subtask(done)["summary"]
    assert daily_total(rt.database) == before + FLOWERS_MINOR


def test_ac2_a_contact_name_in_the_tool_call_still_freezes_the_same_card(rt, frozen_clock):
    """The model may pass the name from life_birthdays instead of the internal id."""

    bank_client = bank()
    life = life_for(
        bank_client,
        [call("life_prepare_order", {"contact_id": "爱人", "product_code": "flowers"})],
        checkpointer=InMemorySaver(),
        writes=WriteFacade(bank_client, database=rt.database),
        database=rt.database,
    )

    events = invoke(life)
    question = interrupt(events)
    card = question["confirmation"]

    assert card["operation"] == "birthday_order"
    assert card["amount_minor"] == FLOWERS_MINOR
    assert "模拟鲜花" in card["product_name"]
    assert order_writes(bank_client) == []


# ------------------------------------------------------------------ AC-2-RED


def test_ac2_red_a_day_total_over_the_cap_demands_a_code_before_the_order_lands(rt, frozen_clock):
    # 先真写一笔当日累计：存量 + 一笔生日单 > 日限额，红标是算出来的，不是标出来的。
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, RED_PRESEED_MINOR)
    assert RED_PRESEED_MINOR + FLOWERS_MINOR > DAILY_RED_THRESHOLD_MINOR

    checkpoint = InMemorySaver()
    bank_client = bank()
    writes = WriteFacade(bank_client, database=rt.database)
    life = life_for(
        bank_client,
        [prepare_call()],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )

    events = invoke(life)
    question = interrupt(events)
    card = question["confirmation"]

    assert card["requires_code"] is True
    assert "需要验证码" in question["display"]
    assert "超过日限额" in question["display"]
    assert order_writes(bank_client) == []
    assert daily_total(rt.database) == RED_PRESEED_MINOR

    # 挂卡这一轮 TurnResult 不带 facts，图状态里没有机读的 risk_level（卡片上只有
    # requires_code 与摘要文字）。判决本身用同一个生产处理器、同一份数据库再取一次。
    data = life.module.handlers["life_prepare_order"](
        invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}),
        bank_client,
    )
    assert data.facts["risk_level"] == "red"
    assert data.facts["requires_code"] is True
    assert data.facts["intent"]["amount_minor"] == FLOWERS_MINOR
    assert "超过日限额" in data.facts["reason"]

    # 用户带着有效验证码再点一次：图带同一个检查点重建，验证码随新的 TurnContext 进来。
    rebuilt = life_for(
        bank_client,
        [],
        turn=red_turn(),
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    done = invoke(rebuilt, resume(events, {"decision": "confirm"}))

    posts = order_writes(bank_client)
    assert len(posts) == 1
    assert posts[0]["json"]["mfa_code"] == "123456"
    assert posts[0]["json"]["confirm"] is True

    order = order_facts(rebuilt)["order"]
    assert order["status"] == "pending"
    assert subtask(done)["turn_status"] == "completed"
    assert daily_total(rt.database) == RED_PRESEED_MINOR + FLOWERS_MINOR

# ------------------------------------------------------------------ 两跳：账单 -> 生日

BILL = DomainDeclaration(
    agent_id="bill",
    display="账单查询",
    purpose="查上月账单并算出可用于其他安排的预算",
)
BILL_SUMMARY = "上月账单已核对：可支配 150.00 元。"
TWO_HOP_MESSAGE = "先查上月账单，算出可支配预算，再按这个预算给爱人准备生日礼物"

CAKE_ORDER = {
    **ORDER,
    "order_id": "o_birthday_cake",
    "amount_minor": CAKE_MINOR,
    "task_id": "t_birthday_cake",
    "details": {
        **ORDER["details"],
        "product_code": "cake",
        "product_name": "模拟蛋糕",
        "task_id": "t_birthday_cake",
    },
}


class ScriptedBill:
    """账单域只返回一个已核对的结论；本测试关心它怎样传给下一个域。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, context, model, message):
        self.calls.append(message)
        return TurnResult(status=TurnStatus.COMPLETED, summary=BILL_SUMMARY)


@dataclasses.dataclass(frozen=True)
class TwoHop:
    runtime: AgentRuntime
    model: StreamingModel
    bill: ScriptedBill
    life_module: Any


def two_hop_for(rt, bank_client, model: StreamingModel) -> TwoHop:
    """生产 Supervisor + 脚本账单域 + 生产生活事件域。"""

    bill = ScriptedBill()
    bill_module = handler_module(
        "bill", BILL, bill, request=lambda state: str(state.get("goal") or "")
    )
    writes = WriteFacade(bank_client, database=rt.database)
    life_module = dataclasses.replace(
        MODULE,
        handlers={
            **MODULE.handlers,
            "life_prepare_order": functools.partial(
                MODULE.handlers["life_prepare_order"], database=rt.database
            ),
        },
        declaration=dataclasses.replace(
            MODULE.declaration,
            graph_builder=functools.partial(MODULE.declaration.graph_builder, writes=writes),
        ),
    )
    registry = build_registry([bill_module, life_module])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank_client))

    def factory(model, context, assembler=None, turn=None):
        return build_agent_graph(
            model,
            gateway,
            context,
            assembler=assembler,
            turn=turn,
            client=bank_client,
            roster=registry.roster,
        )

    runtime = AgentRuntime(
        model,
        graph_factory=factory,
        customer_resolver=lambda _user_id: CUSTOMER,
    )
    return TwoHop(runtime=runtime, model=model, bill=bill, life_module=life_module)


def invoke_two_hop(two_hop: TwoHop, context=None):
    async def collect():
        stream = two_hop.runtime.invoke(
            "sess_two_hop", APP_USER, TWO_HOP_MESSAGE, context or {}
        )
        return [event async for event in stream]

    return asyncio.run(collect())


def test_bill_result_becomes_the_birthday_budget_across_two_domains(rt, frozen_clock):
    """账单结论作为下一域的输入，而不是两段互不相干的回答。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders", CAKE_ORDER)
    model = StreamingModel(
        [
            [
                (
                    "call",
                    "handoff",
                    '{"agent_id":"bill","instruction":"查上月账单，算出可支配预算"}',
                ),
                (
                    "call",
                    "handoff",
                    '{"agent_id":"life_event","instruction":"按上一步结论，用可支配预算给爱人准备生日礼物：{{previous_summary}}"}',
                ),
            ],
            [call("life_birthdays")],
            [call("life_gift_plan", {"budget_yuan": "150"})],
            [call("life_prepare_order", {"contact_id": "c_partner", "product_code": "cake"})],
            ["账单显示可支配 150.00 元，已按这个预算为爱人准备生日礼物。"],
        ]
    )
    two_hop = two_hop_for(rt, bank_client, model)

    events = invoke_two_hop(two_hop)

    assert two_hop.bill.calls == ["查上月账单，算出可支配预算"]

    # 第二个域拿到的是第一个域的实际摘要，不是模板占位符。
    life_prompt = json.dumps(model.messages[1], ensure_ascii=False)
    assert BILL_SUMMARY in life_prompt
    assert "{{previous_summary}}" not in life_prompt

    results = tool_results(events)
    assert [frame["tool_name"] for frame in results] == [
        "life_birthdays",
        "life_gift_plan",
        "life_prepare_order",
    ]
    assert "模拟蛋糕" in results[1]["data"]["summary"]
    assert "模拟鲜花" not in results[1]["data"]["summary"]

    question = interrupt(events)
    card = question["confirmation"]
    assert card["operation"] == "birthday_order"
    assert card["amount_minor"] == CAKE_MINOR
    assert "模拟蛋糕" in card["product_name"]
    assert card["requires_code"] is False
    assert order_writes(bank_client) == []
    assert daily_total(rt.database) == 0

    done = invoke_two_hop(two_hop, resume(events, {"decision": "confirm"}))

    posts = order_writes(bank_client)
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["confirm"] is True
    assert body["product_code"] == "cake"
    assert body["contact_id"] == "c_partner"
    assert "mfa_code" not in body

    finished = [event.data for event in done if event.event == "subtask_end"]
    assert finished[-1]["turn_status"] == "completed"
    assert "o_birthday_cake" in finished[-1]["summary"]
    assert "模拟蛋糕" in finished[-1]["summary"]
    assert daily_total(rt.database) == CAKE_MINOR
