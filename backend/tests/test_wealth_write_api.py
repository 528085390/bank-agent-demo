"""理财写流程的 HTTP 全链路：四投走到确认卡，一次点击成交（V-2）。

V-1 直接调 runtime.invoke；这一层钉的是用户在浏览器里真正遇到的那条路：会话路由、
SSE 帧、GET /api/conversations/{id} 上的确认卡投影、以及点击路由
POST /api/conversations/{id}/pending-actions/{pending_id}/decide。

理财模块是真的（build_registry() 的生产模块集，wealth 声明了 graph_builder），
银行是 tests/wealth/support.py 的 BankDouble：没有真银行参与。

确认卡的 payload 不在 SSE 里——interrupt 帧只有六个键——它只在会话投影上。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.main import create_app
from app.modules.registry import build_registry
from app.runtime import build_tool_gateway
from tests.test_conversation_api import create, messages, submit
from tests.test_multi_agent_api import EVENT_FIELDS, frames
from tests.wealth import support

FIRST_MESSAGE = "我想申购1000元理财产品"
HORIZON_ANSWER = "6个月内不用，不需要随时赎回"
CHOOSE_MESSAGE = "选第2只，货币基金"
ACCOUNT_ANSWER = "用第二个账户 a_wealth_main"
CONFIRM_MESSAGE = "确认本次操作"

HORIZON_QUESTION = "请问您计划什么时候用这笔钱？"
ACCOUNT_QUESTION = "请问用哪个账户扣款？"

PRODUCT_ID = "p_demo_cash"
PRODUCT_NAME = "演示灵活理财"
ACCOUNT = "a_wealth_main"
AMOUNT_MINOR = 100000
NEW_AMOUNT_MINOR = 200000
ORDER_ID = "order_purchase_1"
CODE = "246810"
WRONG_CODE = "000000"

PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
PURCHASE = "/products/purchase"
ORDER = "/wealth/orders/order_purchase_1"

# 点击轮由 Supervisor 收尾，模型只剩这一句要说。
CLICK_REPLY = "已按您的确认提交。"

# 会话一律带着标题建：自动命名会在后台线程里再问一次模型（app/conversation/turns.py:384
# suggest_title(self.runtime.model, ...)），而这里的 runtime.model 就是本用例的脚本模型——
# 它会把脚本吃掉一条，而且时序是竞态的。带标题的会话 auto_title_candidate 直接返回 None
# （app/conversation/service.py:765：row.title != DEFAULT_TITLE），命名这一步根本不会发生。
# 命名与写流程无关，禁掉它不影响本文件要钉的任何一条。
CONVERSATION_TITLE = "理财申购"


# ------------------------------------------------------------------- 银行替身


def _stub_reads(double, *, balances=(2000000, 2000000, 2000000)):
    """申购预览要读的东西：产品、产品详情、账户余额、最新测评。

    /accounts 每个响应都必须是一个数组：这个端点返回列表，包错了网关会报
    PROTOCOL 并多烧一轮模型纠正。
    """

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
    """一次成功申购：银行回执 + 成交后的订单回读。金额必须和预览一致，否则运行时拒收。"""

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


# ------------------------------------------------------------------- 模型脚本


def _handoff():
    return support.call("handoff", {"agent_id": "wealth", "instruction": "办理理财"})


def _open_replies():
    return (_handoff(), support.ask_user(HORIZON_QUESTION, ["horizon_months"]))


def _answer_replies():
    return (
        support.set_slots({"horizon_months": 6, "liquidity": "flexible"}),
        support.call_read("wealth_products"),
        support.finish(
            f"候选产品：{PRODUCT_NAME}（{PRODUCT_ID}）、演示稳健理财（p_demo_stable）。"
        ),
        f"候选产品：{PRODUCT_NAME}、演示稳健理财。",
    )


def _choose_replies():
    return (
        _handoff(),
        support.set_slots({"product_id": PRODUCT_ID}),
        support.ask_user(ACCOUNT_QUESTION, ["account_id"]),
    )


def _preview_replies(amount_minor=AMOUNT_MINOR):
    return (
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": PRODUCT_ID, "account_id": ACCOUNT, "amount_minor": amount_minor}
        ),
    )


def _flow(*, with_write=False, extra_replies=(), balances=(2000000, 2000000, 2000000)):
    """四投（开单 → 答用钱时间 → 选产品 → 答账户）的替身与脚本，外加后面的轮次。"""

    double = support.BankDouble()
    _stub_reads(double, balances=balances)
    if with_write:
        _stub_write(double)
    model = support.ScriptModel(
        *(
            _open_replies()
            + _answer_replies()
            + _choose_replies()
            + _preview_replies()
            + tuple(extra_replies)
        )
    )
    return double, model


# ------------------------------------------------------------------- HTTP 脚手架


def _client(rt, session, double, model):
    """生产模块集 + 真实财富图，挂在 rt 上，再包成 TestClient。"""

    registry = build_registry()
    gateway = build_tool_gateway(registry, client=double.client(), audit=rt.audit)
    checkpoint = InMemorySaver()

    def rebuild():
        return AgentRuntime(
            model,
            graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
                model,
                gateway,
                context,
                assembler=assembler,
                turn=turn,
                roster=registry.roster,
                client=double.client(),
            ),
            checkpointer=checkpoint,
            customer_resolver=rt.identity.bank_customer_id,
        )

    rt.agent = rebuild()
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )
    return client, rebuild


def _decide(client, conversation_id, pending_id, body, *, key=None):
    return client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json=body,
        headers={"Idempotency-Key": key or f"click-{uuid4().hex}"},
    )


def _resume(question, value):
    return {"resume": {"question_id": question["question_id"], "value": dict(value)}}


def _interrupt(response):
    return next(data for name, data in frames(response) if name == "interrupt")


def _card(client, conversation_id):
    detail = client.get(f"/api/conversations/{conversation_id}").json()["data"]
    return detail["pending_interrupt"]


def _writes(double):
    return [call for call in double.calls if call.method == "POST" and call.path == PURCHASE]


def _business_ids(response):
    """这一投 final 帧上拍平的业务编号；停在确认框的一投没有 final，就是空的。"""

    return [
        business_id
        for name, data in frames(response)
        if name == "final"
        for business_id in data.get("business_ids") or []
    ]


def _assert_documented(response):
    """每一帧的键集合都恰好等于 EVENT_FIELDS 里那一行。"""

    parsed = frames(response)
    assert parsed, "这一投没有任何帧"
    for name, data in parsed:
        assert name in EVENT_FIELDS, name
        expected = EVENT_FIELDS[name]
        if name == "interrupt" and data.get("kind") == "confirmation":
            # 确认框是唯一多一个键的帧：tests/test_multi_agent_api.py:47-52 的注释写明了
            # 「interrupt carries one extra key only on a confirmation box」——六个标准键
            # 加 confirmation。输入框仍然是严格的六个键。
            expected = expected | {"confirmation"}
        assert set(data) == expected, (name, sorted(data))


def _walk_to_confirmation(client, conversation_id):
    """四投走到确认卡，返回确认卡那一帧（SSE 里的 interrupt）。"""

    first = submit(client, conversation_id, FIRST_MESSAGE)
    assert first.status_code == 200, first.text
    horizon = _interrupt(first)

    second = submit(
        client, conversation_id, HORIZON_ANSWER, context=_resume(horizon, {"horizon_months": 6})
    )
    assert second.status_code == 200, second.text

    third = submit(client, conversation_id, CHOOSE_MESSAGE)
    assert third.status_code == 200, third.text
    account = _interrupt(third)

    fourth = submit(
        client, conversation_id, ACCOUNT_ANSWER, context=_resume(account, {"account_id": ACCOUNT})
    )
    assert fourth.status_code == 200, fourth.text
    return _interrupt(fourth)


# ---------------------------------------------------------------------- V2-1


def test_the_write_flow_streams_the_documented_frames(rt, session):
    """全链路每一帧都只带它声明的那几个键，确认卡只在投影里出现。"""

    double, model = _flow(with_write=True)
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]

    first = submit(client, conversation_id, FIRST_MESSAGE)
    assert first.status_code == 200, first.text
    _assert_documented(first)
    horizon = _interrupt(first)
    assert horizon["kind"] == "input"
    assert set(horizon) == EVENT_FIELDS["interrupt"]
    assert horizon["display"] == HORIZON_QUESTION

    second = submit(
        client, conversation_id, HORIZON_ANSWER, context=_resume(horizon, {"horizon_months": 6})
    )
    assert second.status_code == 200, second.text
    _assert_documented(second)

    third = submit(client, conversation_id, CHOOSE_MESSAGE)
    assert third.status_code == 200, third.text
    _assert_documented(third)
    account = _interrupt(third)

    fourth = submit(
        client, conversation_id, ACCOUNT_ANSWER, context=_resume(account, {"account_id": ACCOUNT})
    )
    assert fourth.status_code == 200, fourth.text
    _assert_documented(fourth)
    card = _interrupt(fourth)
    assert card["kind"] == "confirmation"
    # 确认框的帧就是那六个键，外加唯一被文档允许的 confirmation。
    assert set(card) == EVENT_FIELDS["interrupt"] | {"confirmation"}
    assert card["confirmation"]["pending_id"].startswith("wp_")

    # 同一笔也出现在会话投影上（GET /api/conversations/{id}）。
    projected = _card(client, conversation_id)
    assert projected["kind"] == "confirmation"
    assert projected["question_id"] == card["question_id"]
    assert projected["confirmation"]["pending_id"].startswith("wp_")
    assert projected["confirmation"]["amount_minor"] == AMOUNT_MINOR
    assert projected["confirmation"]["requires_code"] is True

    clicked = _decide(
        client,
        conversation_id,
        projected["confirmation"]["pending_id"],
        {
            "question_id": projected["question_id"],
            "decision": "confirm",
            "verification_code": CODE,
        },
    )
    assert clicked.status_code == 200, clicked.text
    _assert_documented(clicked)
    # 点击轮：结论、回话、收尾。理财图的确认回答从 wait/process 进入，不再经过 decide，
    # 所以没有 agent_start（通用确认卡那条路会多一个 agent_start——见 test_pending_action_api）。
    # 点击是恢复，所以基座层先从检查点重发计划和正在跑的节点，再续上结论。
    assert [name for name, _ in frames(clicked)] == [
        "plan",
        "task_update",
        "subtask_end",
        "task_update",
        "final",
    ]
    assert [data["status"] for name, data in frames(clicked) if name == "task_update"] == [
        "running",
        "done",
    ]


# ---------------------------------------------------------------------- V2-2


def test_the_click_completes_the_write_and_the_ledger_records_the_answer(rt, session):
    """点击成交：写一次、账本多两条、卡片被消费。"""

    double, model = _flow(with_write=True, extra_replies=(CLICK_REPLY,))
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]
    _walk_to_confirmation(client, conversation_id)
    card = _card(client, conversation_id)
    before = len(messages(client, conversation_id)["items"])

    response = _decide(
        client,
        conversation_id,
        card["confirmation"]["pending_id"],
        {
            "question_id": card["question_id"],
            "decision": "confirm",
            "verification_code": CODE,
        },
    )

    assert response.status_code == 200, response.text
    posts = _writes(double)
    assert len(posts) == 1
    assert posts[0].body["mfa_code"] == CODE
    assert posts[0].body["confirm"] is True

    parsed = frames(response)
    assert (
        next(data for name, data in parsed if name == "subtask_end")["turn_status"] == "completed"
    )
    assert ORDER_ID in next(data for name, data in parsed if name == "final")["business_ids"]

    ledger = messages(client, conversation_id)["items"]
    assert len(ledger) == before + 2
    assert ledger[-2]["message_kind"] == "interrupt_answer"
    assert ledger[-2]["question_id"] == card["question_id"]
    assert ledger[-2]["content"] == CONFIRM_MESSAGE
    assert ledger[-1]["role"] == "assistant"
    # 卡片被消费：没有东西还在等回答。
    assert _card(client, conversation_id) is None


# ---------------------------------------------------------------------- V2-3


def test_a_click_without_the_code_is_refused_before_the_bank(rt, session):
    """卡片要验证码而点击不带：422，在银行之前就被拒，写 0 次、账本不变。"""

    double, model = _flow(with_write=True)
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]
    _walk_to_confirmation(client, conversation_id)
    card = _card(client, conversation_id)
    assert card["confirmation"]["requires_code"] is True
    before = len(messages(client, conversation_id)["items"])

    response = _decide(
        client,
        conversation_id,
        card["confirmation"]["pending_id"],
        {"question_id": card["question_id"], "decision": "confirm"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VERIFICATION_CODE_REQUIRED"
    assert _writes(double) == []
    assert len(messages(client, conversation_id)["items"]) == before
    still = _card(client, conversation_id)
    assert still["confirmation"]["pending_id"] == card["confirmation"]["pending_id"]
    assert still["confirmation"]["requires_code"] is True


# ---------------------------------------------------------------------- V2-4


def test_a_wrong_code_is_reported_and_no_second_write_happens(rt, session):
    """验证码被判错：银行没有受理，如实回到等待确认；改成正确验证码再点，才只成交一笔。"""

    double, model = _flow(extra_replies=(CLICK_REPLY,))
    # 银行明确拒绝（1008 验证码不对 / 409）：业务拒绝，不是结果未知。
    double.on("POST", PURCHASE, code=1008, status=409, data=None)
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]
    _walk_to_confirmation(client, conversation_id)
    card = _card(client, conversation_id)

    refused = _decide(
        client,
        conversation_id,
        card["confirmation"]["pending_id"],
        {
            "question_id": card["question_id"],
            "decision": "confirm",
            "verification_code": WRONG_CODE,
        },
    )

    assert refused.status_code == 200, refused.text
    subtask = next(data for name, data in frames(refused) if name == "subtask_end")
    assert subtask["turn_status"] == "waiting_confirmation"
    # 用户被告知要改的是验证码，而且话术是逐字固定的：银行 1008 的文案「验证码不正确。」
    # 经 runtime 的 _repreview 拼成这一句。缺码那条（CODE_REQUIRED_SUMMARY）不含这句话，
    # 所以它能把两条路分开。
    assert subtask["summary"] == "验证码不正确。请重新输入验证码后再确认。"
    # 没有成交事实：订单号不出现，也没有任何业务编号回来。
    assert ORDER_ID not in refused.text
    assert _business_ids(refused) == []
    posts = _writes(double)
    assert len(posts) == 1
    assert posts[0].body["mfa_code"] == WRONG_CODE
    # 卡片还在，而且是同一笔：幂等键和 pending_id 都没换。
    still = _card(client, conversation_id)
    assert still["kind"] == "confirmation"
    assert still["confirmation"]["pending_id"] == card["confirmation"]["pending_id"]
    assert still["confirmation"]["requires_code"] is True

    # 用户改成正确验证码，对同一笔再点一次：这次银行受理，整条流程仍然只成交一笔。
    # 重问会换一个新的 question_id（确认暂停每次都会新发一个），但待办编号与幂等键不换——
    # 所以第二次点击要用卡片上现在挂着的那个 question_id。
    assert still["question_id"] != card["question_id"]
    _stub_write(double)
    retried = _decide(
        client,
        conversation_id,
        still["confirmation"]["pending_id"],
        {
            "question_id": still["question_id"],
            "decision": "confirm",
            "verification_code": CODE,
        },
    )

    assert retried.status_code == 200, retried.text
    # 银行一共只收到两次申购请求：一次错码、一次对码。
    posts = _writes(double)
    assert len(posts) == 2
    assert [post.body["mfa_code"] for post in posts] == [WRONG_CODE, CODE]
    # 全流程只出现一个订单号，而且只来自成功的那一次；失败那一次没有任何业务编号。
    assert _business_ids(retried).count(ORDER_ID) == 1
    assert _business_ids(refused) == []
    # 成交之后卡片被消费。
    assert _card(client, conversation_id) is None


# ---------------------------------------------------------------------- V2-5


def test_the_card_survives_a_graph_rebuild(rt, session):
    """每一轮生产都会重建图；重建之后卡片上的编号还是用户看到的那一笔。"""

    double, model = _flow()
    client, rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]
    _walk_to_confirmation(client, conversation_id)
    seen = _card(client, conversation_id)

    rt.agent = rebuild()

    again = _card(client, conversation_id)
    assert again["kind"] == "confirmation"
    assert again["question_id"] == seen["question_id"]
    assert again["confirmation"]["pending_id"] == seen["confirmation"]["pending_id"]
    assert again["confirmation"]["amount_minor"] == AMOUNT_MINOR
    assert again["confirmation"]["product_name"] == PRODUCT_NAME
    assert again["confirmation"]["account_display"] == ACCOUNT


# ---------------------------------------------------------------------- V2-6


def test_a_click_on_a_stale_pending_is_refused(rt, session):
    """预览被换成另一笔之后，旧编号的点击不能落到现在这笔上：拒绝，写 0 次。"""

    second_preview = (
        _handoff(),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": PRODUCT_ID, "account_id": ACCOUNT, "amount_minor": NEW_AMOUNT_MINOR}
        ),
    )
    double, model = _flow(extra_replies=second_preview)
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]
    _walk_to_confirmation(client, conversation_id)
    stale = _card(client, conversation_id)

    # 先取消这一笔（取消不写钱），再发起另一笔金额不同的预览。
    cancelled = _decide(
        client,
        conversation_id,
        stale["confirmation"]["pending_id"],
        {"question_id": stale["question_id"], "decision": "cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert _writes(double) == []
    assert _card(client, conversation_id) is None

    second = submit(client, conversation_id, "改成申购2000元")
    assert second.status_code == 200, second.text
    fresh = _card(client, conversation_id)
    assert fresh["kind"] == "confirmation"
    assert fresh["confirmation"]["pending_id"] != stale["confirmation"]["pending_id"]
    assert fresh["confirmation"]["amount_minor"] == NEW_AMOUNT_MINOR

    response = _decide(
        client,
        conversation_id,
        stale["confirmation"]["pending_id"],
        {
            "question_id": stale["question_id"],
            "decision": "confirm",
            "verification_code": CODE,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert _writes(double) == []
    # 现在这笔没被动过。
    still = _card(client, conversation_id)
    assert still["confirmation"]["pending_id"] == fresh["confirmation"]["pending_id"]


# ---------------------------------------------------------------------- V2-7


def test_a_plain_message_cannot_answer_an_open_input_interrupt(rt, session):
    """追问挂着的时候，裸聊天消息走普通消息通道，被 409 拒；问题原样留着。"""

    double, model = _flow()
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]

    first = submit(client, conversation_id, FIRST_MESSAGE)
    assert first.status_code == 200, first.text
    question = _interrupt(first)
    before = len(messages(client, conversation_id)["items"])
    calls_before = model.calls

    response = submit(client, conversation_id, "确认")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert response.json()["error"]["details"]["question_id"] == question["question_id"]
    # 没有模型调用、没有账本行。
    assert model.calls == calls_before
    assert len(messages(client, conversation_id)["items"]) == before
    # 问题没有被消费，也没有被改写。
    still = _card(client, conversation_id)
    assert still["kind"] == "input"
    assert still["question_id"] == question["question_id"]
    assert still["fields"] == question["fields"]
    assert still["attempt"] == 1


# ---------------------------------------------------------------------- V2-8


def test_the_resume_envelope_keys_must_match_the_question(rt, session):
    """信封的键集合必须与问题完全相等：多一个、少一个都重问，不 500、不丢槽位。"""

    double, model = _flow()
    client, _rebuild = _client(rt, session, double, model)
    conversation = create(client, title=CONVERSATION_TITLE)
    conversation_id = conversation["conversation_id"]

    first = submit(client, conversation_id, FIRST_MESSAGE)
    assert first.status_code == 200, first.text
    question = _interrupt(first)
    calls_before = model.calls

    extra = submit(
        client,
        conversation_id,
        HORIZON_ANSWER,
        context=_resume(question, {"horizon_months": 6, "liquidity": "flexible"}),
    )
    assert extra.status_code == 200, extra.text
    reasked = _interrupt(extra)
    assert reasked["kind"] == "input"
    assert reasked["question_id"] == question["question_id"]
    assert reasked["attempt"] == 2
    # 只补缺的那个槽位：已经收集到的信息没有丢。
    assert reasked["fields"] == question["fields"]

    missing = submit(
        client,
        conversation_id,
        HORIZON_ANSWER,
        context=_resume(question, {}),
    )
    assert missing.status_code == 200, missing.text
    reasked_again = _interrupt(missing)
    assert reasked_again["question_id"] == question["question_id"]
    assert reasked_again["attempt"] == 3

    # 重问不走模型：两条坏信封一个模型调用都没花。
    assert model.calls == calls_before

    good = submit(
        client,
        conversation_id,
        HORIZON_ANSWER,
        context=_resume(question, {"horizon_months": 6}),
    )
    assert good.status_code == 200, good.text
    subtask = next(data for name, data in frames(good) if name == "subtask_end")
    assert subtask["turn_status"] == "completed"
