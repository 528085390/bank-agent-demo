"""生活事件自有图：只读循环、黄/红确认卡、点击执行与取消。

走真实图（生产 ``MODULE`` 的 ``graph_builder``），不用 Mock 图；模型是 ``ScriptedModel``，
银行是 ``FakeBankClient`` 的生日版。依赖注入方式与 ``bind_life_event`` 一致：``database`` 用
``functools.partial`` 绑到 ``life_prepare_order``，``writes`` 用 ``functools.partial`` 绑到
``declaration.graph_builder``。

时钟固定在 ``life_tools.SystemClock`` 与 ``write_facade.SystemClock`` 两处：读侧按 2031 年
算生日，写侧按真实日期记当日累计，不钉住的话两边看的不是同一天。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime, TurnContext, thread_id_for_session
from app.agents.supervisor import build_domain_graph
from app.core.clock import CST
from app.integration import write_facade as write_facade_module
from app.integration.write_facade import WriteFacade
from app.modules.life_event import MODULE
from app.modules.life_event import tools as life_tools
from app.modules.life_event.workflow import PENDING_EXPIRY_MINUTES, expired
from app.modules.registry import build_registry
from app.storage.daily_total import DailyTotalStore
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel

APP_USER = "user_0001"
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
NOW = datetime(2031, 3, 4, 9, 0, tzinfo=CST)
GOAL = "帮爱人挑一份生日礼物"

CONTEXT = TrustedContext(APP_USER, "sess_life", CUSTOMER)

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
    {"product_code": "flowers", "name": "模拟鲜花", "price_minor": 19900, "currency": "CNY"},
    {"product_code": "cake", "name": "模拟蛋糕", "price_minor": 12900, "currency": "CNY"},
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
    "amount_minor": 19900,
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


def runtime_for(
    bank_client,
    replies,
    *,
    turn=None,
    checkpointer=None,
    writes=None,
    database=None,
):
    """Build the production graph, with the same injections `bind_life_event` makes."""

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
    return AgentRuntime(graph=graph), model


def invoke(runtime, context=None):
    async def collect():
        return [event async for event in runtime.invoke("sess_life", APP_USER, GOAL, context or {})]

    return asyncio.run(collect())


def resume(events, value):
    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    return {"resume": {"question_id": question["question_id"], "value": dict(value)}}


def interrupt(events):
    return next(event.data for event in reversed(events) if event.event == "interrupt")


def subtask(events):
    return next(event.data for event in events if event.event == "subtask_end")


def order_writes(bank_client):
    return [
        request
        for request in bank_client.requests
        if request["method"] == "POST" and request["path"] == "/birthdays/orders"
    ]


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


# ------------------------------------------------------------------ 只读循环


def test_the_model_lists_birthdays_then_products_and_answers_in_one_turn(frozen_clock):
    bank_client = bank()
    runtime, model = runtime_for(
        bank_client,
        [
            call("life_birthdays"),
            call("life_gift_plan", {"budget_yuan": "200"}),
            "爱人 9 月 25 日生日，还有 205 天。预算内推荐模拟鲜花（199.00 元）。",
        ],
    )

    events = invoke(runtime)

    assert len(model.prompts) == 3
    assert [request["path"] for request in bank_client.requests] == [
        "/birthdays",
        "/mock-products",
    ]
    assert [event for event in events if event.event == "interrupt"] == []
    done = subtask(events)
    assert "爱人" in done["summary"]
    assert done["turn_status"] == "completed"

def test_an_invented_gift_price_in_the_model_summary_is_blocked(frozen_clock):
    bank_client = bank()
    runtime, _ = runtime_for(
        bank_client,
        [
            call("life_birthdays"),
            call("life_gift_plan", {"budget_yuan": "200"}),
            "预算内推荐模拟鲜花，99,999.00 元。",
        ],
    )

    events = invoke(runtime)

    done = subtask(events)
    assert done["turn_status"] == "failed"
    assert "99,999" not in done["summary"]
    assert "无法与工具返回核对" in done["summary"]
    assert done["grounding_blocked"]


# ------------------------------------------------------------------ 挂卡


def test_a_yellow_order_hangs_a_card_that_does_not_ask_for_a_code(frozen_clock):
    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])

    events = invoke(runtime)
    question = interrupt(events)

    assert question["kind"] == "confirmation"
    assert question["confirmation"]["operation"] == "birthday_order"
    assert question["confirmation"]["requires_code"] is False
    assert question["confirmation"]["pending_id"].startswith("tp_")
    assert question["confirmation"]["amount_minor"] == 19900
    assert "模拟鲜花" in question["confirmation"]["product_name"]
    assert question["confirmation"]["account_display"] == "活期账户（尾号 rent）"
    assert subtask(events)["turn_status"] == "waiting_confirmation"
    assert order_writes(bank_client) == []


def test_the_frozen_snapshot_is_narrowed_to_the_five_execute_keys(frozen_clock):
    """facts['intent'] 有 8 个键，life_execute_order 只声明了 5 个。"""

    declared = MODULE.tools["life_execute_order"].parameters
    assert declared["additionalProperties"] is False
    assert set(declared["properties"]) == {
        "contact_id",
        "product_code",
        "from_account_id",
        "birthday_date",
        "amount_minor",
    }

    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])

    events = invoke(runtime)
    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert subtask(done)["turn_status"] == "completed"
    # 收窄之后才交得出去：请求体里只有四个冻结字段加 confirm，姓名/执行时间这些
    # 只在服务端用的键没有漏出去。
    body = order_writes(bank_client)[0]["json"]
    assert set(body) == {
        "contact_id",
        "product_code",
        "from_account_id",
        "birthday_date",
        "confirm",
    }


# ------------------------------------------------------------------ 点击消费


def test_confirming_the_card_sends_the_order_and_the_bank_schedules_it(frozen_clock):
    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    posts = order_writes(bank_client)
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["confirm"] is True
    assert not body.get("mfa_code")
    assert body["contact_id"] == "c_partner"
    assert body["product_code"] == "flowers"
    assert body["from_account_id"] == "a_zhang_current"
    assert body["birthday_date"] == "2031-09-25"
    assert posts[0]["idempotency_key"]
    assert subtask(done)["turn_status"] == "completed"
    assert "o_birthday_1" in subtask(done)["summary"]
    assert "自动下单" in subtask(done)["summary"]


def test_cancelling_the_card_sends_nothing_and_ends_normally(frozen_clock):
    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "cancel"}))

    assert order_writes(bank_client) == []
    assert "已取消" in subtask(done)["summary"]
    assert subtask(done)["turn_status"] == "completed"


def test_an_unknown_decision_is_refused_without_writing(frozen_clock):
    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])
    events = invoke(runtime)

    refused = invoke(runtime, resume(events, {"decision": "改成蛋糕"}))

    assert order_writes(bank_client) == []
    assert subtask(refused)["turn_status"] == "unsupported"


# ------------------------------------------------------------------ 红标与验证码


def test_a_red_day_total_hangs_a_card_that_asks_for_a_code(rt, frozen_clock):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 99000)
    checkpoint = InMemorySaver()
    bank_client = bank()
    runtime, _ = runtime_for(
        bank_client, [prepare_call()], checkpointer=checkpoint, database=rt.database
    )

    events = invoke(runtime)
    question = interrupt(events)

    assert question["confirmation"]["requires_code"] is True
    assert "验证码" in question["display"]
    assert order_writes(bank_client) == []

    # 用户带着码再点一次：图带着同一个检查点重建，验证码随新的 TurnContext 进来。
    rebuilt, _ = runtime_for(
        bank_client, [], turn=red_turn(), checkpointer=checkpoint, database=rt.database
    )
    done = invoke(rebuilt, resume(events, {"decision": "confirm"}))

    posts = order_writes(bank_client)
    assert len(posts) == 1
    assert posts[0]["json"]["mfa_code"] == "123456"
    assert subtask(done)["turn_status"] == "completed"


def test_a_red_card_without_a_code_is_refused_before_the_socket(rt, frozen_clock):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, 99000)
    checkpoint = InMemorySaver()
    bank_client = bank()
    runtime, _ = runtime_for(
        bank_client, [prepare_call()], checkpointer=checkpoint, database=rt.database
    )
    events = invoke(runtime)

    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert order_writes(bank_client) == []
    assert subtask(done)["turn_status"] == "failed"
    assert "验证码" in subtask(done)["summary"]


def test_a_yellow_card_refuses_a_code_it_never_asked_for(frozen_clock):
    checkpoint = InMemorySaver()
    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()], checkpointer=checkpoint)
    events = invoke(runtime)

    rebuilt, _ = runtime_for(bank_client, [], turn=red_turn(), checkpointer=checkpoint)
    done = invoke(rebuilt, resume(events, {"decision": "confirm"}))

    assert order_writes(bank_client) == []
    assert subtask(done)["turn_status"] == "failed"


# ------------------------------------------------------------------ 过期


def _past(minutes: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _rewrite_checkpoint(graph, transform) -> None:
    """改写停在 interrupt 上的线程通道值，而不丢掉它那个开放的中断。

    照 tests/test_bill_graph.py:197-211：update_state 会连带清掉开放的中断，所以直接改
    检查点里的通道 blob。这里复现的是「同一张卡搁置了很久才被点」。
    """

    thread_id = thread_id_for_session("sess_life")
    saver = graph.checkpointer
    latest = max(saver.storage[thread_id][""].keys())
    checkpoint, _metadata, _parent = saver.storage[thread_id][""][latest]
    versions = saver.serde.loads_typed(checkpoint)["channel_versions"]
    values = asyncio.run(graph.aget_state({"configurable": {"thread_id": thread_id}})).values
    for channel, value in transform(dict(values)).items():
        saver.blobs[(thread_id, "", channel, versions[channel])] = saver.serde.dumps_typed(value)


def cancel_writes(bank_client):
    return [
        request
        for request in bank_client.requests
        if request["method"] == "POST" and request["path"].endswith("/cancel")
    ]


def test_the_order_card_carries_a_deadline(frozen_clock):
    """卡上带截止时间：前端能显示，服务端也能据此拒掉旧卡。"""

    bank_client = bank()
    runtime, _ = runtime_for(bank_client, [prepare_call()])
    before = datetime.now(timezone.utc)

    events = invoke(runtime)

    after = datetime.now(timezone.utc)
    expires_at = interrupt(events)["confirmation"]["expires_at"]
    assert isinstance(expires_at, str) and expires_at
    deadline = datetime.fromisoformat(expires_at)
    assert deadline.tzinfo is not None
    assert before + timedelta(minutes=PENDING_EXPIRY_MINUTES) <= deadline
    assert deadline <= after + timedelta(minutes=PENDING_EXPIRY_MINUTES)
    assert expired({"expires_at": expires_at}) is False


def test_an_expired_order_card_is_refused(rt, frozen_clock):
    """隔了很久才点确认：拒，银行零写、当日累计零新增。"""

    checkpoint = InMemorySaver()
    bank_client = bank()
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [prepare_call()],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    events = invoke(runtime)
    assert interrupt(events)["confirmation"]["expires_at"]

    _rewrite_checkpoint(
        runtime._graph,
        lambda values: {"life_pending": {**values["life_pending"], "expires_at": _past()}},
    )
    done = invoke(runtime, resume(events, {"decision": "confirm"}))

    assert order_writes(bank_client) == []
    assert subtask(done)["turn_status"] == "failed"
    assert "过期" in subtask(done)["summary"]
    with rt.database.sessions() as session:
        assert DailyTotalStore.get_accumulated_minor(session, APP_USER, BUSINESS_DAY) == 0


def test_an_expired_cancel_card_is_refused(rt, frozen_clock):
    """撤销卡同样会过期：拒掉，零请求。"""

    checkpoint = InMemorySaver()
    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1/cancel", {**ORDER, "status": "cancelled"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [prepare_call()],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    placed = invoke(runtime)
    placed = invoke(runtime, resume(placed, {"decision": "confirm"}))
    assert subtask(placed)["turn_status"] == "completed"

    # 第二回合：用户改主意，要撤销刚下的那一单 —— 图带着同一个检查点重建。
    cancelling, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    hung = invoke(cancelling)
    question = interrupt(hung)
    assert question["confirmation"]["operation"] == "cancel_birthday_order"

    _rewrite_checkpoint(
        cancelling._graph,
        lambda values: {"life_action": {**values["life_action"], "expires_at": _past()}},
    )
    refused = invoke(cancelling, resume(hung, {"decision": "confirm"}))

    assert cancel_writes(bank_client) == []
    assert subtask(refused)["turn_status"] == "failed"
    assert "过期" in subtask(refused)["summary"]


def test_an_expired_cancel_card_can_be_retried(rt, frozen_clock):
    """撤销卡过期被拒之后，同一单还能再说一次「撤销」：重新挂卡，而不是「没有这一单」。

    挂撤销卡时 `life_action` 被 `cancel_pending` 顶掉了。过期分支如果只是 conclude，
    这张过期的 `cancel_pending` 会留在检查点里，重试时 `hang_cancel` 认不出「已下单」，
    只好回一句「没有找到编号为 X 的生日订单」——用户明明刚下过这一单。
    """

    checkpoint = InMemorySaver()
    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1/cancel", {**ORDER, "status": "cancelled"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [prepare_call()],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    placed = invoke(runtime)
    placed = invoke(runtime, resume(placed, {"decision": "confirm"}))
    assert subtask(placed)["turn_status"] == "completed"

    # 第二回合：挂撤销卡，然后把它改成一张搁置了很久的旧卡。
    cancelling, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    hung = invoke(cancelling)
    assert interrupt(hung)["confirmation"]["operation"] == "cancel_birthday_order"
    _rewrite_checkpoint(
        cancelling._graph,
        lambda values: {"life_action": {**values["life_action"], "expires_at": _past()}},
    )
    refused = invoke(cancelling, resume(hung, {"decision": "confirm"}))
    assert subtask(refused)["turn_status"] == "failed"
    assert "过期" in subtask(refused)["summary"]

    # 第三回合：用户再说一次「撤销」，同一单应当重新挂卡，而不是被当成没有这一单。
    retrying, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    retried = invoke(retrying)

    question = interrupt(retried)
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["operation"] == "cancel_birthday_order"
    assert question["confirmation"]["account_display"] == "o_birthday_1"
    assert subtask(retried)["turn_status"] == "waiting_confirmation"
    # 重试只是重新挂卡，银行侧一次撤销都没有发生。
    assert cancel_writes(bank_client) == []


def test_a_follow_up_cancel_without_an_order_id_uses_the_just_placed_order(rt, frozen_clock):
    """用户说「把刚才那单取消掉」：不传订单号也能用本对话最近提交的订单挂撤销卡。"""

    checkpoint = InMemorySaver()
    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1/cancel", {**ORDER, "status": "cancelled"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [prepare_call()],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    placed = invoke(runtime)
    placed = invoke(runtime, resume(placed, {"decision": "confirm"}))
    assert subtask(placed)["turn_status"] == "completed"

    cancelling, model = runtime_for(
        bank_client,
        [call("life_cancel_order")],
        checkpointer=checkpoint,
        writes=writes,
        database=rt.database,
    )
    hung = invoke(cancelling)

    prompt = model.messages[0][-1]["content"]
    assert "本对话最近提交的生日订单号" in prompt
    assert "o_birthday_1" in prompt
    question = interrupt(hung)
    assert question["confirmation"]["operation"] == "cancel_birthday_order"
    assert question["confirmation"]["account_display"] == "o_birthday_1"


def test_a_cancel_without_a_recent_order_still_asks_for_an_order_id(rt, frozen_clock):
    """本对话没有下单记录时不猜单号：直接要求用户提供。"""

    bank_client = bank()
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order")],
        writes=writes,
        database=rt.database,
    )

    events = invoke(runtime)

    assert [event for event in events if event.event == "interrupt"] == []
    assert subtask(events)["turn_status"] == "failed"
    assert "请提供" in subtask(events)["summary"]
    assert bank_client.requests == []


def test_a_cancel_in_a_fresh_conversation_asks_the_bank(rt, frozen_clock):
    """换会话之后凭单号撤销：本对话没有这一单，先向银行核验它还在、还是本人的。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1", ORDER)
    bank_client.register("/birthdays/orders/o_birthday_1/cancel", {**ORDER, "status": "cancelled"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        writes=writes,
        database=rt.database,
    )

    hung = invoke(runtime)

    question = interrupt(hung)
    assert question["confirmation"]["operation"] == "cancel_birthday_order"
    assert question["confirmation"]["amount_minor"] == 19900
    assert question["confirmation"]["account_display"] == "o_birthday_1"
    lookups = [
        request
        for request in bank_client.requests
        if request["method"] == "GET" and request["path"] == "/birthdays/orders/o_birthday_1"
    ]
    assert len(lookups) == 1
    assert lookups[0]["user_id"] == CUSTOMER

    done = invoke(runtime, resume(hung, {"decision": "confirm"}))

    assert subtask(done)["turn_status"] == "completed"
    assert [request["path"] for request in cancel_writes(bank_client)] == [
        "/birthdays/orders/o_birthday_1/cancel"
    ]


def test_a_cancel_of_a_finished_order_is_refused(rt, frozen_clock):
    """银行说这单已经执行完了：不挂卡、不撤销，直接告诉用户。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1", {**ORDER, "status": "completed"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        writes=writes,
        database=rt.database,
    )

    events = invoke(runtime)

    assert [event for event in events if event.event == "interrupt"] == []
    done = subtask(events)
    assert done["turn_status"] == "failed"
    assert "已完成" in done["summary"]
    assert cancel_writes(bank_client) == []


def test_a_cancel_of_someone_elses_order_is_refused(rt, frozen_clock):
    """单号是真的，但不是本人的单子：当成不存在，别替人撤销。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1", {**ORDER, "user_id": "u_other"})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        writes=writes,
        database=rt.database,
    )

    events = invoke(runtime)

    assert [event for event in events if event.event == "interrupt"] == []
    done = subtask(events)
    assert done["turn_status"] == "failed"
    assert "没有找到" in done["summary"]
    # 真的问了银行，才谈得上「不是本人的」。
    assert any(
        request["path"] == "/birthdays/orders/o_birthday_1" for request in bank_client.requests
    )
    assert cancel_writes(bank_client) == []


def test_a_cancel_of_an_unknown_order_is_refused(rt, frozen_clock):
    """银行回 404（code 1004）：没有这一单。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders/o_missing", {}, code=1004)
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_missing"})],
        writes=writes,
        database=rt.database,
    )

    events = invoke(runtime)

    assert [event for event in events if event.event == "interrupt"] == []
    done = subtask(events)
    assert done["turn_status"] == "failed"
    assert "没有找到" in done["summary"]
    assert any(request["path"] == "/birthdays/orders/o_missing" for request in bank_client.requests)
    assert cancel_writes(bank_client) == []


def test_a_cancel_without_a_usable_amount_fails_closed(rt, frozen_clock):
    """金额读不出来就核验失败：宁可让用户重试，也不拿 0 元挂一张撤销卡。"""

    bank_client = bank()
    bank_client.register("/birthdays/orders/o_birthday_1", {**ORDER, "amount_minor": 0})
    writes = WriteFacade(bank_client, database=rt.database)
    runtime, _ = runtime_for(
        bank_client,
        [call("life_cancel_order", {"order_id": "o_birthday_1"})],
        writes=writes,
        database=rt.database,
    )

    events = invoke(runtime)

    assert [event for event in events if event.event == "interrupt"] == []
    assert subtask(events)["turn_status"] == "failed"
    assert "无法核验" in subtask(events)["summary"]
    assert cancel_writes(bank_client) == []


@pytest.mark.parametrize(
    "action",
    [None, {}, {"expires_at": None}, {"expires_at": ""}, {"expires_at": "明天下午"}],
)
def test_expired_treats_an_unreadable_deadline_as_expired(action):
    """读不到截止时间就算过期（fail closed），不是「当它永远有效」。"""

    assert expired(action) is True


def test_expired_compares_a_real_deadline():
    now = datetime.now(timezone.utc)

    assert expired({"expires_at": (now - timedelta(seconds=1)).isoformat()}) is True
    assert expired({"expires_at": (now + timedelta(minutes=5)).isoformat()}) is False
