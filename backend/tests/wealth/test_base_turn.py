"""任务 S-6（任务 4 改写）：理财模块的整轮处理走领域图，不走进程内的处理器字典。

这一层证明的是**适配器**，不是理财流程本身（那由 tests/wealth 的其余用例覆盖）：

1. 一次只读问答跑完领域图后带回一份结论：状态、摘要和事实都在。
2. 同一个线程连着两轮，会话槽位仍然连续；而且第二轮走的是"回答待办"通道：上一轮的问题和这一轮
   的答案都被拼进提示词，模型看得见"这句话是在回答什么"。
3. 追问时摆到用户眼前的候选（这一轮查到的编号）下一轮直接报编号也认——用户回答待办时不会再
   查一遍，只认"本轮查到的"就会把裸账户号判成编造，把人拖进反复追问。
4. 记忆里的待办已经失效（过期、换了意图）时，这一句话按新消息处理，不能被"没有可以回答的
   待办"吞掉。
5. 写路径（申购／赎回）已经接入：预览回来是 `WAITING_CONFIRMATION` 加一张结构化确认卡片
   （金额、产品、账户、待办编号），待办保留，一次资金写都没有发生。（旧验证码入口已经停用，
   不再是一条"没接进来"的写路径。）
6. set_model 真的换掉了下一轮使用的模型：新模型被调用，旧模型不再增长。

跨轮的东西现在住在检查点上（`support.GraphTurn` 是旧处理器的图替身）：同一个 saver 就是同一个
会话，换一个 saver 就是一次"重启"。
"""

from __future__ import annotations

from datetime import timedelta

from app.agents.turn import TurnStatus
from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from tests.wealth import support

PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
SESSION = "sess_wealth_base"
CONVERSATION = "conv_wealth_base"
HORIZON_QUESTION = "请问您计划什么时候用这笔钱？"


def test_a_read_question_comes_back_as_a_completed_turn(double, context):
    """只读问答：一次工具调用加一次回答，回来后是 completed，摘要与事实都在。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(support.call_read("wealth_products"), support.finish("已查到。"))
    turn = support.GraphTurn(double, context=context)

    result = turn.lap(model, "我有哪些理财产品？", session=SESSION)

    assert result.status is TurnStatus.COMPLETED
    assert result.summary == "已查到。"
    assert result.facts
    assert "products" in result.facts


def test_two_turns_on_the_same_handler_keep_the_slots(double, context):
    """同一个线程处理两轮：第二轮是"回答待办"而不是"新话题"，答案被拼进提示词。"""

    turn = support.GraphTurn(double, context=context)
    first = support.ScriptModel(support.ask_user(HORIZON_QUESTION, ["horizon_months"]))

    asked = turn.lap(first, "推荐点理财", session=SESSION)

    assert asked.status is TurnStatus.WAITING_INPUT
    assert asked.summary == HORIZON_QUESTION

    second = support.ScriptModel(
        support.set_slots({"horizon_months": 3}),
        support.finish("那建议先看可灵活赎回的产品。"),
    )
    answered = turn.answer(
        second, asked.question or {}, {"horizon_months": 3}, message="三个月内要用", session=SESSION
    )

    assert answered.status is TurnStatus.COMPLETED
    assert answered.summary == "那建议先看可灵活赎回的产品。"
    assert answered.slots == {"horizon_months": 3}
    # 上一轮的问题和这一轮的答案都被拼进了提示词（`pending_context` 的 JSON）。只丢一句
    # "你上一次向用户提了一个问题"（新消息通道的续接提示）时，模型得自己猜"这句话是不是答案"：
    # 整句能猜对，裸编号猜不对。答案以槽位名的形式进入提示词，这正是它被当成回答的证据。
    assert HORIZON_QUESTION in second.prompts[-1]
    assert '"horizon_months": 3' in second.prompts[-1]


def test_an_answer_naming_a_candidate_just_shown_is_accepted(double, context):
    """追问摆出来的候选就是下一轮的地面真值：用户直接报编号，不必让他再查一遍。"""

    double.on("GET", ACCOUNTS, data=[support.account_payload()])
    turn = support.GraphTurn(double, context=context)
    first = support.ScriptModel(
        support.call_read("wealth_accounts"),
        support.ask_user("请说明用哪个账户付款：主账户还是备用账户？", ["account_id"]),
    )

    asked = turn.lap(first, "买 2000 元的演示灵活理财", session=SESSION)

    assert asked.status is TurnStatus.WAITING_INPUT

    # 第二轮一个银行调用都没有：账户编号只可能来自"刚追问过的那一轮查到的记录"。
    second = support.ScriptModel(
        support.set_slots({"account_id": "a_wealth_main"}),
        support.finish("好的，用主账户付款。"),
    )
    answered = turn.answer(
        second,
        asked.question or {},
        {"account_id": "a_wealth_main"},
        message="a_wealth_main",
        session=SESSION,
    )

    assert answered.status is TurnStatus.COMPLETED
    assert answered.summary == "好的，用主账户付款。"
    assert len(double.calls) == 1
    # 编号是被直接接受的：没有走"上一次输出不能使用"的纠正回合。纠正一次也能凑出同样的结论，
    # 所以只看最终状态抓不住这个缺陷。
    assert "上一次输出不能使用" not in second.prompts[-1]


def test_a_pending_that_expired_does_not_swallow_the_next_message(double, context, clock):
    """待办过期后用户又发了一句：按新消息处理，不能被"没有可以回答的待办"吞掉。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    turn = support.GraphTurn(double, context=context, clock=clock)
    first = support.ScriptModel(support.ask_user(HORIZON_QUESTION, ["horizon_months"]))

    asked = turn.lap(first, "推荐点理财", session=SESSION, conversation_id=CONVERSATION)

    assert asked.status is TurnStatus.WAITING_INPUT

    clock.advance(timedelta(minutes=6))
    second = support.ScriptModel(support.call_read("wealth_products"), support.finish("已查到。"))

    answered = turn.lap(second, "我有哪些理财产品？", session=SESSION, conversation_id=CONVERSATION)

    assert answered.status is TurnStatus.COMPLETED
    assert answered.summary == "已查到。"
    # 记住的待办已经过期：这句话退回去按新消息处理，而不是变成"没有可以回答的待办"。
    assert "用户回答：" not in second.prompts[0]


def test_a_write_preview_comes_back_as_a_confirmation_card_and_keeps_its_pending(double, context):
    """写路径已经接入：预览回来是一张确认卡片，待办**留着**（它是确认的绑定键）。

    这条断言钉住的是"不能只回一句『请确认』，也不能在生成预览后直接撤下待办"：状态、金额、
    产品、账户和待办编号必须一起交给基座，用户点击时才有东西可绑定。一次资金写都没有发生。
    """

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on_sequence(
        "GET", ACCOUNTS, [support.account_payload(balance_minor=2000000, available_minor=2000000)]
    )
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": "a_wealth_main", "amount_minor": 200000}
        ),
    )
    turn = support.GraphTurn(double, context=context)

    result = turn.lap(model, "买 2000 元的演示灵活理财，用主账户", session=SESSION)

    assert result.status is TurnStatus.WAITING_CONFIRMATION
    assert result.confirmation is not None
    assert result.confirmation.operation == "purchase_product"
    assert result.confirmation.amount_minor == 200000
    assert result.confirmation.product_name == "演示灵活理财"
    assert result.confirmation.account_display == "a_wealth_main"
    assert result.confirmation.pending_id
    # 预览原文（程序模板生成）就是卡片上方那段话。它**不**替银行回答"要不要验证码"——
    # 那由卡片自己显示；写死在文案里会在银行要验证码时与卡片上的输入框自相矛盾。
    assert "请确认这笔模拟申购" in result.summary
    assert "还需要输入模拟验证码" not in result.summary
    assert "不需要输入验证码" not in result.summary
    assert "点击确认即提交给银行" in result.summary
    # 要不要验证码由银行的能力声明决定；本仓库的银行要，所以卡片上必须有输入。
    assert result.confirmation.requires_code is True
    # 预览不写钱：一次资金写请求都没有发出去。
    assert [call for call in double.calls if call.method == "POST"] == []


def test_set_model_swaps_the_model_the_next_turn_uses(double, bank, clock, context):
    """set_model 换掉的模型从下一轮开始生效：新模型被调用，旧模型停在原地。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", ACCOUNTS, data=[support.account_payload()])
    first = support.ScriptModel(support.call_read("wealth_products"), support.finish("已查到。"))
    agent = build_wealth_agent(first, bank, clock)

    before, _, _ = agent.handle(
        context, WealthRequest(kind="message", text="我有哪些理财产品？"), session={}, pending=None
    )

    assert before.status is WealthStatus.COMPLETED
    assert first.calls == 2

    second = support.ScriptModel(support.call_read("wealth_accounts"), support.finish("已查到。"))
    agent.set_model(second)

    after, _, _ = agent.handle(
        context, WealthRequest(kind="message", text="我有哪些账户？"), session={}, pending=None
    )

    assert after.status is WealthStatus.COMPLETED
    assert second.calls == 2
    assert first.calls == 2
