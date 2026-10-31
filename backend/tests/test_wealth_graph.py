"""理财自己的领域图：整轮处理、原生工具帧、跨轮记忆（W-5／W-7）。

基座只把 model／spec／assembler／turn／client 交给 `build_wealth_graph`，图自己跑完一轮。
所以这一层断言的是"图确实在跑、而且跑在基座的工具网关上"，而不是理财流程的细节（那由
tests/wealth 覆盖）。

T-12 会话记忆挂在图上：同一个 thread 重建图之后还在，换一个 thread 就是空的。
T-13 基座的 build_domain_graph 走 graph_builder 分支，只读工具留下 tool_call／tool_result 帧。
T-14 这张图可以独立组装：build_wealth_graph + InMemorySaver，不经过基座的 turn 插槽。
T-15 追问变成 kind=input 的 interrupt，回答按字段名提交。
T-16 图 state 能被 langgraph 自己的 serde 原样读写：只有数据进 state（spec §14.8／§16）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.agents.domain import build_agent_graph, build_domain_specs
from app.agents.runtime import AgentRuntime
from app.modules.registry import build_registry
from app.modules.wealth.workflow.graph import build_wealth_graph
from app.modules.wealth.workflow.state import WealthState
from app.runtime import build_tool_gateway
from tests.wealth import support

USER = "user_0001"
SESSION = "sess_wealth_1"
OTHER_SESSION = "sess_wealth_2"
CONTEXT = support.bound_context(session_id=SESSION)
OTHER_CONTEXT = support.bound_context(session_id=OTHER_SESSION)

PRODUCTS = "/products"
HORIZON_QUESTION = "请问您计划什么时候用这笔钱？"


def wealth_spec(double, context=CONTEXT):
    """理财模块的 DomainSpec：声明加上这一位调用者的工具句柄，都走生产注册表。"""

    registry = build_registry()
    gateway = build_tool_gateway(registry, client=double.client())
    specs = build_domain_specs(gateway, context, roster=registry.roster)
    return next(spec for spec in specs if spec.agent_id == "wealth")


def standalone(double, model, *, context=CONTEXT, checkpointer=None):
    """T-14 的组装：只有理财自己的图，基座的 turn 插槽不参与。

    领域图自己就是一个编译好的子图（`build_wealth_graph` 以 `graph.compile()` 收尾），所以这里
    给它套一层最小的宿主图：thread 与检查点属于宿主，图本身不挑宿主是谁。
    """

    inner = build_wealth_graph(model, wealth_spec(double, context), client=double.client())
    host = StateGraph(WealthState)
    host.add_node("wealth", inner)
    host.add_edge(START, "wealth")
    host.add_edge("wealth", END)
    return host.compile(checkpointer=checkpointer or InMemorySaver())


def supervised(double, model, *, context=CONTEXT, checkpointer=None):
    """基座的总图：supervisor 加上每个领域一个节点，理财那一个是它自己的 builder 建的。"""

    registry = build_registry()
    gateway = build_tool_gateway(registry, client=double.client())
    graph = build_agent_graph(
        model, gateway, context, roster=registry.roster, client=double.client()
    )
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def collect(graph, message="帮我", *, session=SESSION):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke(session, USER, message, {})]

    return asyncio.run(run())


def resume(graph, question_id, value, *, session=SESSION, message="三个月内要用"):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [
            event
            async for event in runtime.invoke(
                session, USER, message, {"resume": {"question_id": question_id, "value": value}}
            )
        ]

    return asyncio.run(run())


def held_question(graph, *, session=SESSION):
    """检查点里正等着的那个问题，就是刷新页面时 API 读出来的那一份。"""

    async def run():
        state = await graph.aget_state({"configurable": {"thread_id": f"chat:{session}"}})
        return [getattr(item, "value", None) for item in state.interrupts]

    return asyncio.run(run())


def thread_state(graph, *, session=SESSION):
    async def run():
        return (await graph.aget_state({"configurable": {"thread_id": f"chat:{session}"}})).values

    return asyncio.run(run())


def interrupts(events):
    return [event.data for event in events if event.event == "interrupt"]


def finals(events):
    return [event.data["text"] for event in events if event.event == "final"]


def frames(events, name):
    return [event.data for event in events if event.event == name]


def test_the_supervisor_answers_a_read_question_through_the_wealth_graph():
    """整轮走完：handoff → 理财图 → 只读工具 → 回答；工具调用留下成对的原生事件帧。"""

    double = support.BankDouble()
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.call("handoff", {"agent_id": "wealth", "instruction": "查产品"}),
        support.call_read("wealth_products"),
        support.finish("已查到。"),
        "为您找到 4 只产品。",
    )
    graph = supervised(double, model)

    events = collect(graph, "我有哪些理财产品？")

    # 只读工具不再由理财自己直接打银行：它经基座网关，所以有 tool_call／tool_result。
    assert [item["tool_name"] for item in frames(events, "tool_call")] == ["wealth_products"]
    assert [item["agent_id"] for item in frames(events, "tool_call")] == ["wealth"]
    assert [item["status"] for item in frames(events, "tool_result")] == ["SUCCEEDED"]
    assert [item["summary"] for item in frames(events, "subtask_end")] == ["已查到。"]
    assert [item["turn_status"] for item in frames(events, "subtask_end")] == ["completed"]
    assert finals(events) == ["为您找到 4 只产品。"]
    assert interrupts(events) == []


def test_the_wealth_graph_stands_on_its_own():
    """A15：图能独立组装并跑完一轮，不需要基座的 turn 插槽。"""

    double = support.BankDouble()
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(support.call_read("wealth_products"), support.finish("已查到。"))
    graph = standalone(double, model)

    events = collect(graph, "我有哪些理财产品？")

    assert [item["tool_name"] for item in frames(events, "tool_call")] == ["wealth_products"]
    assert [item["status"] for item in frames(events, "tool_result")] == ["SUCCEEDED"]
    assert [item["turn_status"] for item in frames(events, "subtask_end")] == ["completed"]
    assert interrupts(events) == []
    assert len(double.calls) == 1, "只读工具只打了一次银行"


def test_the_wealth_graph_keeps_the_session_across_a_rebuild_and_not_across_threads():
    """同一个 thread 的记忆留在检查点里；换一个 thread 就是干净的一轮。"""

    double = support.BankDouble()
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    saver = InMemorySaver()
    first = support.ScriptModel(support.ask_user(HORIZON_QUESTION, ["horizon_months"]))
    graph = standalone(double, first, checkpointer=saver)

    asked = collect(graph, "推荐点理财")

    card = interrupts(asked)[0]
    assert card["kind"] == "input"

    # 重建一张图（进程重启的等价物）：记忆在检查点里，不在图对象上。
    second = support.ScriptModel(
        support.set_slots({"horizon_months": 3}),
        support.finish("那建议先看可灵活赎回的产品。"),
    )
    rebuilt = standalone(double, second, checkpointer=saver)

    answered = resume(rebuilt, card["question_id"], {"horizon_months": 3})

    assert [item["turn_status"] for item in frames(answered, "subtask_end")] == ["completed"]
    # 上一轮的提问和这一轮按字段名提交的答案都进了提示词：这句话是在回答什么，模型看得见。
    assert f"问题：{HORIZON_QUESTION}" in second.prompts[-1]
    assert '"horizon_months": 3' in second.prompts[-1]
    assert thread_state(rebuilt)["wealth_slots"] == {"horizon_months": 3}

    # 换一个 thread：同一份检查点里必须是另一段会话，上一轮的槽位不能漏进来。
    third = support.ScriptModel(support.call_read("wealth_products"), support.finish("已查到。"))
    other = standalone(double, third, context=OTHER_CONTEXT, checkpointer=saver)

    collect(other, "我有哪些理财产品？", session=OTHER_SESSION)

    # 槽位名本身在系统提示里（可问的槽位清单），所以这里看的是上一轮记住的那个**值**。
    assert '"horizon_months": 3' not in third.prompts[0]
    assert not thread_state(other, session=OTHER_SESSION).get("wealth_slots")


def test_a_follow_up_question_becomes_an_input_interrupt():
    """追问不再靠进程内的表：它是一次 kind=input 的 interrupt，字段按声明渲染。"""

    double = support.BankDouble()
    model = support.ScriptModel(
        support.ask_user(HORIZON_QUESTION, ["horizon_months"]),
        support.set_slots({"horizon_months": 3}),
        support.finish("那建议先看可灵活赎回的产品。"),
    )
    graph = standalone(double, model)

    events = collect(graph, "推荐点理财")

    cards = interrupts(events)
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "input"
    assert card["agent_id"] == "wealth"
    assert card["display"] == HORIZON_QUESTION
    assert card["fields"] == [
        {
            "name": "horizon_months",
            "label": "用钱时间（月）",
            "type": "text",
            "required": True,
            "options": [],
        }
    ]
    # 检查点里也留着同一个问题：刷新页面后输入卡还在。
    assert [item["question_id"] for item in held_question(graph)] == [card["question_id"]]

    answered = resume(graph, card["question_id"], {"horizon_months": 3})

    assert [item["turn_status"] for item in frames(answered, "subtask_end")] == ["completed"]
    assert interrupts(answered) == []
    assert thread_state(graph)["wealth_slots"] == {"horizon_months": 3}
    assert '"horizon_months": 3' in model.prompts[-1]


# ------------------------------------------------------------------ T-16 序列化往返

PREVIEW_PRODUCT = "/products/p_demo_cash"
PREVIEW_ACCOUNTS = "/accounts"
PREVIEW_RISK = "/wealth/risk-assessments/latest"
PREVIEW_SLOTS = {
    "product_id": "p_demo_cash",
    "account_id": "a_wealth_main",
    "amount_minor": 200000,
}
PREVIEW_MESSAGE = "买 2000 元的演示灵活理财，用主账户"


def _normalized(value: Any) -> Any:
    """tuple → list：serde 读回来的 tuple 是 list，比较前先归一化（spec §14.8 点名了
    `_Pending.missing` 与 `_Session.shown`）。"""

    if isinstance(value, Mapping):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def _round_trip(state: Mapping[str, Any], *, strict: bool) -> Any:
    """把图 state 喂给 langgraph 自己的序列化器再读回来。

    `allowed_msgpack_modules=None` 就是 `LANGGRAPH_STRICT_MSGPACK=true` 在构造器里的等价写法
    （jsonplus.py:107-114）：只允许内置的安全类型，连"注册过的自定义类型"都不放行。
    """

    serde = JsonPlusSerializer(allowed_msgpack_modules=None) if strict else JsonPlusSerializer()
    return serde.loads_typed(serde.dumps_typed(dict(state)))


def _serde_lap(double, shape: str):
    """三种停留形状各跑一轮：它们是 state 里字段最全的三个时刻。"""

    if shape == "read":
        double.on("GET", PRODUCTS, data=support.wealth_demo_products())
        model = support.ScriptModel(
            support.call_read("wealth_products"), support.finish("已查到。")
        )
        message = "我有哪些理财产品？"
    elif shape == "input":
        model = support.ScriptModel(support.ask_user(HORIZON_QUESTION, ["horizon_months"]))
        message = "推荐点理财"
    else:
        double.on("GET", PRODUCTS, data=support.wealth_demo_products())
        double.on("GET", PREVIEW_PRODUCT, data=support.product_payload())
        double.on_sequence(
            "GET",
            PREVIEW_ACCOUNTS,
            [support.account_payload(balance_minor=2000000, available_minor=2000000)],
        )
        double.on("GET", PREVIEW_RISK, data=support.risk_profile_payload(risk_level="low"))
        model = support.ScriptModel(
            support.call_read("wealth_products"),
            support.call_read("wealth_accounts"),
            support.prepare_purchase(dict(PREVIEW_SLOTS)),
        )
        message = PREVIEW_MESSAGE

    return support.GraphTurn(double, context=CONTEXT).lap(model, message, session=SESSION)


@pytest.mark.parametrize("strict", [False, True], ids=["permissive", "strict"])
@pytest.mark.parametrize(
    "shape", ["read", "input", "preview"], ids=["read-lap", "input-lap", "preview-lap"]
)
def test_the_graph_state_survives_a_serde_round_trip(shape, strict):
    """T-16（spec §14.8／§16）：图 state 必须能被 langgraph 的 serde 原样读写。

    它守卫的是"只有数据进 state"：`Clock`／`WealthBank`／`ModelClient`，或任何冻结的 slots
    数据类，一旦混进 state，`dumps_typed` 立刻抛 TypeError（实测：Type is not msgpack
    serializable: TestClock）——检查点存不下的东西不该住在这里。两种模式都跑：默认的宽容模式，
    以及只允许内置安全类型的严格模式。
    """

    lap = _serde_lap(support.BankDouble(), shape)

    state = dict(lap.state)
    assert state, "这一轮应当留下了图状态"
    assert _normalized(_round_trip(state, strict=strict)) == _normalized(state)
