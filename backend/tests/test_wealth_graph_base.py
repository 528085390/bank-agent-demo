"""基座层：理财写流程的端到端验收（V1-1 .. V1-8）。

这一层不过 HTTP、不碰数据库：理财图挂在基座自己的整轮处理器（build_agent_graph）上，银行是
替身，模型是脚本。断言的是基座层的契约——追问以 kind="input" 的 interrupt 出现、回答按字段名
回流、预览只以确认框出现（不当成一条聊天消息）、只有按钮事件才写钱、写恰好一次、验证码进不了
模型上下文。

所有轮次都走 conversation/turn 那条路（TurnContext + runtime.invoke(..., turn=turn)）：它既是
生产路径，也是验证码唯一能到达理财图的通道——process 从烘焙在闭包里的 turn 读它，不从图状态读。
混用 session 路径与 turn 路径会让 scope_id 从 session_id 变成 conversation_id，待办归属复核
（_valid_pending）随即拒绝这次点击。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime, TurnContext, thread_id_for_session
from app.core.errors import AppError
from app.modules.registry import build_registry
from app.modules.wealth.workflow.state import WEALTH_FIELDS
from app.runtime import build_tool_gateway
from tests.wealth import support

USER = "user_0001"
SESSION = "sess_wealth_1"
OTHER_SESSION = "sess_wealth_2"
CONVERSATION = "conv_wealth_1"
THREAD = thread_id_for_session(SESSION)
CONTEXT = support.bound_context(session_id=SESSION)

CODE = "246810"
PRODUCT_ID = "p_demo_cash"
PRODUCT_NAME = "演示灵活理财"
STABLE_NAME = "演示稳健理财"
ACCOUNT = "a_wealth_main"
AMOUNT_MINOR = 100000
ORDER_ID = "order_purchase_1"

PRODUCTS = "/products"
PRODUCT = f"/products/{PRODUCT_ID}"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
PURCHASE = "/products/purchase"
ORDER = f"/wealth/orders/{ORDER_ID}"

FIRST_MESSAGE = "我想申购1000元理财产品"
HORIZON_QUESTION = "请问您计划什么时候用这笔钱？"
ACCOUNT_QUESTION = "请问用哪个账户扣款？"


# ---------------------------------------------------------------------- 银行替身


def _stub_reads(double, *, balances=(2000000, 2000000, 2000000)):
    """预览要用的三份只读事实：候选产品、账户、风险测评。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on_sequence(
        "GET",
        ACCOUNTS,
        *[
            [support.account_payload(balance_minor=value, available_minor=value)]
            for value in balances
        ],
    )
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))


def _stub_write(double):
    """一次成功的资金写：回执加按原单号读回的那一笔。"""

    double.on(
        "POST",
        PURCHASE,
        data=support.purchase_receipt_payload(
            amount_minor=AMOUNT_MINOR, principal_minor=AMOUNT_MINOR
        ),
        status=201,
    )
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id=ORDER_ID,
            holding_id="h_purchase_1",
            transaction_id="txn_purchase_1",
            amount_minor=AMOUNT_MINOR,
        ),
    )


# ---------------------------------------------------------------------- 模型脚本


def _handoff(instruction="买一笔"):
    return support.call("handoff", {"agent_id": "wealth", "instruction": instruction})


def _open_replies():
    """第一轮：交给理财，然后追问用钱时间。"""

    return [_handoff(), support.ask_user(HORIZON_QUESTION, ["horizon_months"])]


def _answer_replies():
    """第二轮：记下期限与流动性，查产品，报候选。"""

    return [
        support.set_slots({"horizon_months": 6, "liquidity": "flexible"}),
        support.call_read("wealth_products"),
        support.finish(
            f"候选产品：{PRODUCT_NAME}（{PRODUCT_ID}）、{STABLE_NAME}（p_demo_stable）。"
        ),
        f"候选产品：{PRODUCT_NAME}、{STABLE_NAME}。",
    ]


def _choose_replies():
    """第三轮：记下产品，追问扣款账户。"""

    return [
        _handoff("选一只"),
        support.set_slots({"product_id": PRODUCT_ID}),
        support.ask_user(ACCOUNT_QUESTION, ["account_id"]),
    ]


def _preview_replies():
    """第四轮：读产品与账户，冻结预览，生成确认框。"""

    return [
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": PRODUCT_ID, "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}
        ),
    ]


def _click_replies():
    """点击确认那一轮：理财不再调模型，只有基座收尾的那句话。"""

    return ["已按您的确认提交。"]


# ---------------------------------------------------------------------- 驱动器


class Flow:
    """整条写流程：每一轮都是一次真实的基座 invoke，状态留在同一个检查点上。"""

    def __init__(self, double, model):
        self.double = double
        self.model = model
        self.registry = build_registry()
        self.gateway = build_tool_gateway(self.registry, client=double.client())
        self.saver = InMemorySaver()
        self.runtime = AgentRuntime(
            model,
            graph_factory=self._build,
            checkpointer=self.saver,
            customer_resolver=lambda user_id: support.DEMO_CUSTOMER,
        )
        self.sequence = 0
        self.turns: list[tuple[str, tuple]] = []

    def _build(self, model, context, assembler=None, turn=None):
        return build_agent_graph(
            model,
            self.gateway,
            context,
            assembler=assembler,
            turn=turn,
            roster=self.registry.roster,
            client=self.double.client(),
        )

    def lap(self, message, *, resume=None, verification_code=None):
        """一轮 invoke：turn 带着会话编号、消息与（点击时的）验证码。"""

        self.sequence += 1
        turn = TurnContext(
            conversation_id=CONVERSATION,
            thread_id=THREAD,
            message_id=f"msg_{self.sequence}",
            content=message,
            sequence=self.sequence,
            resume=resume,
            verification_code=verification_code,
        )

        async def run():
            return [
                event
                async for event in self.runtime.invoke(SESSION, USER, message, None, turn=turn)
            ]

        events = tuple(asyncio.run(run()))
        self.turns.append((message, events))
        return events

    def answer(self, question, value, *, message):
        """回答追问：resume 的信封按字段名提交。"""

        return self.lap(
            message,
            resume={"question_id": question["question_id"], "value": dict(value)},
        )

    def click(self, question, *, message="确认本次操作", code=None):
        """按钮事件：确认框上的 decision，加上用户当场输入的验证码。"""

        return self.lap(
            message,
            resume={"question_id": question["question_id"], "value": {"decision": "confirm"}},
            verification_code=code,
        )

    def state(self, *, session=SESSION):
        """检查点里的图状态（暂停时那份在子图的命名空间里）。"""

        graph = self._build(self.model, CONTEXT).compile(checkpointer=self.saver)
        config = {"configurable": {"thread_id": thread_id_for_session(session)}}

        async def run():
            return await support.domain_state(graph, await graph.aget_state(config))

        return asyncio.run(run())

    def held(self, *, session=SESSION):
        """检查点里正挂着的问题，就是刷新页面时 API 读出来的那一份。"""

        graph = self._build(self.model, CONTEXT).compile(checkpointer=self.saver)
        config = {"configurable": {"thread_id": thread_id_for_session(session)}}

        async def run():
            snapshot = await graph.aget_state(config)
            return [getattr(item, "value", None) for item in snapshot.interrupts]

        return asyncio.run(run())


# ---------------------------------------------------------------------- 事件读取


def frames(events, name):
    return [event.data for event in events if event.event == name]


def interrupts(events):
    return frames(events, "interrupt")


def finals(events):
    return [data["text"] for data in frames(events, "final")]


def final_business_ids(events):
    """这一轮每个 final 帧上拍平的业务编号。"""

    return [
        business_id
        for data in frames(events, "final")
        for business_id in data.get("business_ids") or []
    ]


def subtask_end(events):
    ends = frames(events, "subtask_end")
    assert ends, "这一轮没有 subtask_end"
    return ends[-1]


def turn_status(events):
    return subtask_end(events)["turn_status"]


def writes(double):
    return [call for call in double.calls if call.method == "POST" and call.path == PURCHASE]


def _flow(*, with_write=False, extra_replies=()):
    double = support.BankDouble()
    _stub_reads(double)
    if with_write:
        _stub_write(double)
    model = support.ScriptModel(
        *_open_replies(),
        *_answer_replies(),
        *_choose_replies(),
        *_preview_replies(),
        *extra_replies,
    )
    return double, model, Flow(double, model)


def _paused_at_confirmation(*, with_write=False, extra_replies=()):
    """跑完四轮，停在确认框上：返回 (double, model, flow, 确认框)。"""

    double, model, flow = _flow(with_write=with_write, extra_replies=extra_replies)
    first = flow.lap(FIRST_MESSAGE)
    flow.answer(interrupts(first)[0], {"horizon_months": 6}, message="6个月内不用，不需要随时赎回")
    third = flow.lap("选第2只，货币基金")
    events = flow.answer(
        interrupts(third)[0], {"account_id": ACCOUNT}, message=f"用第二个账户 {ACCOUNT}"
    )
    return double, model, flow, interrupts(events)[0]


# ---------------------------------------------------------------------- V1-1


def test_the_first_turn_asks_for_the_missing_slot():
    """V1-1：第一轮把缺的槽位变成 kind=input 的追问，而不是一句聊天回复。"""

    double, _, flow = _flow()

    events = flow.lap(FIRST_MESSAGE)

    assert turn_status(events) == "waiting_input"
    held = interrupts(events)
    assert len(held) == 1
    question = held[0]
    assert question["kind"] == "input"
    names = {field["name"] for field in question["fields"]}
    assert names
    assert names <= set(WEALTH_FIELDS)
    # 用户看到的那句话在追问上：这一轮没有 final，也没有任何聊天回复。
    assert question["display"] == HORIZON_QUESTION
    assert subtask_end(events)["summary"] == HORIZON_QUESTION
    assert finals(events) == []
    # 问一句不需要碰银行。
    assert double.calls == []


# ---------------------------------------------------------------------- V1-2


def test_the_answer_round_trips_and_the_candidates_arrive():
    """V1-2：回答按字段名回流，第二轮给出候选产品。"""

    _, _, flow = _flow()
    first = flow.lap(FIRST_MESSAGE)
    question = interrupts(first)[0]

    second = flow.answer(question, {"horizon_months": 6}, message="6个月内不用，不需要随时赎回")

    assert turn_status(second) == "completed"
    text = finals(second)[-1]
    assert PRODUCT_NAME in text
    assert STABLE_NAME in text
    state = flow.state()
    # domain_results 一轮一条：第一条是上一轮那次追问，最后一条才是这一轮。
    assert state["domain_results"][0]["turn_status"] == "waiting_input"
    assert state["domain_results"][-1]["turn_status"] == "completed"


# ---------------------------------------------------------------------- V1-3


def test_the_preview_becomes_a_confirmation_pause_not_a_message():
    """V1-3：预览只以确认框出现，不当成一条聊天消息。"""

    double, _, flow, _ = _paused_at_confirmation()
    events = flow.turns[-1][1]

    assert turn_status(events) == "waiting_confirmation"
    held = interrupts(events)
    assert len(held) == 1
    assert held[0]["kind"] == "confirmation"
    payload = held[0]["confirmation"]
    assert payload["pending_id"].startswith("wp_")
    assert payload["amount_minor"] == AMOUNT_MINOR
    # 预览不是聊天消息：这一轮没有任何一种聊天文本帧（reply_delta / narration / final），
    # 金额只走确认通道——它出现在 interrupt 的 display 里（下一行），而不是被当成一句回复发出去。
    # subtask_end.summary 就是这份预览原文（设计如此），不属于聊天文本。
    assert finals(events) == []
    assert [
        event.event for event in events if event.event in {"reply_delta", "narration", "final"}
    ] == []
    assert str(AMOUNT_MINOR) in held[0]["display"]
    # 检查点里挂着的是同一个确认框。
    checkpointed = flow.held()
    assert checkpointed, "确认框应该挂在检查点上"
    assert checkpointed[-1]["kind"] == "confirmation"
    assert checkpointed[-1]["confirmation"]["pending_id"] == payload["pending_id"]
    assert writes(double) == []


# ---------------------------------------------------------------------- V1-4


def test_a_chat_message_cannot_confirm_a_preview():
    """V1-4：预览挂着的时候，聊天里说确认不能当授权。"""

    double, _, flow, question = _paused_at_confirmation()
    before = len(double.calls)

    with pytest.raises(AppError) as caught:
        asyncio.run(flow.runtime.admission(USER, SESSION, THREAD, None))

    assert caught.value.code == "INVALID_INTERRUPT_ANSWER"
    assert caught.value.status == 409
    assert caught.value.details["question_id"] == question["question_id"]
    assert len(double.calls) == before
    assert writes(double) == []


# ---------------------------------------------------------------------- V1-5


def test_the_button_event_writes_once():
    """V1-5：按钮事件带上正确验证码，写恰好一次，订单号回到业务编号里。"""

    double, _, flow, question = _paused_at_confirmation(
        with_write=True, extra_replies=_click_replies()
    )

    events = flow.click(question, code=CODE)

    assert turn_status(events) == "completed"
    assert subtask_end(events)["status"] == "SUCCEEDED"
    posts = writes(double)
    assert len(posts) == 1
    assert posts[0].body["mfa_code"] == CODE
    assert posts[0].body["confirm"] is True
    # final 上的 business_ids 是拍平后的编号清单；domain_results 里那份还是按名字索引的。
    assert ORDER_ID in frames(events, "final")[-1]["business_ids"]
    assert flow.state()["domain_results"][-1]["business_ids"]["order_id"] == ORDER_ID


# ---------------------------------------------------------------------- V1-6


def test_a_second_click_does_not_write_again():
    """V1-6：成交之后再点同一个确认框，在银行之前就被拒——不会有第二次写，也没有新订单号。

    第二次点击进不了图：成交后没有挂着的问题，基座的门禁（AgentRuntime.admission）先拒绝。
    所以这条的幂等含义是"被拒的那次点击不可能写"：证据是银行调用数不变、全流程只有一个订单号。
    """

    double, _, flow, question = _paused_at_confirmation(
        with_write=True, extra_replies=_click_replies() + _click_replies()
    )

    first = flow.click(question, code=CODE)
    assert turn_status(first) == "completed"
    assert len(writes(double)) == 1
    assert writes(double)[0].body["mfa_code"] == CODE
    assert flow.held() == []
    calls_after_first = len(double.calls)

    # 第二次点击连图都进不去：没有挂着的问题，基座的门禁先拒绝。
    with pytest.raises(AppError) as caught:
        asyncio.run(
            flow.runtime.admission(
                USER,
                SESSION,
                THREAD,
                {
                    "resume": {
                        "question_id": question["question_id"],
                        "value": {"decision": "confirm"},
                    }
                },
            )
        )

    assert caught.value.code == "INVALID_INTERRUPT_ANSWER"
    # 被拒的这一次没有碰银行，也没有产生新的收尾帧或新的订单号。
    assert len(double.calls) == calls_after_first
    assert len(writes(double)) == 1
    assert [bid for _, events in flow.turns for bid in final_business_ids(events)].count(
        ORDER_ID
    ) == 1


# ---------------------------------------------------------------------- V1-7


def test_the_graph_state_carries_the_slots_across_turns():
    """V1-7：槽位住在图状态里，跨轮不丢；换一个 thread 就看不到。"""

    _, _, flow, _ = _paused_at_confirmation()

    state = flow.state()
    slots = state["wealth_slots"]
    assert slots["horizon_months"] == 6
    assert slots["liquidity"] == "flexible"
    assert slots["product_id"] == PRODUCT_ID
    assert slots["account_id"] == ACCOUNT

    other = flow.state(session=OTHER_SESSION)
    other_slots = other.get("wealth_slots") or {}
    assert "horizon_months" not in other_slots
    assert "product_id" not in other_slots


# ---------------------------------------------------------------------- V1-8


def test_the_verification_code_never_reaches_the_model():
    """V1-8：验证码只在确认那一次被当场用掉，不进模型上下文、不进事件、不进检查点。"""

    _, model, flow, question = _paused_at_confirmation(
        with_write=True, extra_replies=_click_replies()
    )

    events = flow.click(question, code=CODE)

    assert turn_status(events) == "completed"
    assert model.prompts, "这一轮之后模型至少被调用过一次（基座收尾）"
    assert all(CODE not in prompt for prompt in model.prompts)
    assert CODE not in json.dumps([event.data for event in events], ensure_ascii=False, default=str)
    assert CODE not in json.dumps(dict(flow.state()), ensure_ascii=False, default=str)
