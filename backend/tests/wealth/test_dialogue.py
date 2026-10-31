"""任务7：多轮追问、指代、过期和取消。

四组断言：

1. **槽位**：会话按 (user_id, session_id) 隔离并保存金额、产品、账户、持仓、用钱时间、流动性；
   只有还没填写的槽位才会被追问，已经知道的不再问，运行时也不会替用户默认选一个账户。
2. **指代**：最近候选按模型实际展示的顺序保存，"第二个"按这份顺序解析，不按数据库全量顺序猜；
   越界序号与凭空编号都会被拒绝。
3. **过期与失效**：跨用户、跨会话、跨意图版本、已过期或进程重启后的待办都不能恢复；改任何槽位
   即换意图，旧待办（含旧确认与旧验证请求）立即失效。
4. **取消**：只终止未提交的工作，不调用模型、不发出任何银行请求，也不声称撤销任何订单。

真实 HTTP 版本见 `tests/wealth_integration/`，只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行。
"""

from __future__ import annotations

import pytest

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.application.runtime import PENDING_TTL
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.llm.decisions import ACTIONS, DecisionError, WealthDecision, parse_decision
from app.modules.wealth.llm.toolcalls import tool_table
from app.modules.wealth.workflow.nodes import CLARIFY_FAILURE_SUMMARY
from app.modules.wealth.workflow.state import MAX_CLARIFICATIONS, SLOTS
from app.tools.protocol import ErrorCode
from tests.wealth import support

PRODUCTS = "/products"
CASH_PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"

# 空会话在提示词里的样子：断言"重启后槽位不复原"用它。
NO_KNOWN_SLOTS = "本次会话已经知道的业务槽位（已经知道，不要再追问）：\n（还没有）"


def _two_products() -> list[dict]:
    """数据库顺序是 [灵活, 稳健]；模型给用户展示的顺序在用例里故意反过来。"""

    return [
        support.product_payload(),
        support.product_payload(
            product_id="p_demo_stable",
            name="演示稳健理财",
            duration_months=3,
            risk_level="medium",
            redeem_rule="maturity_only",
            min_amount_minor=100000,
            expected_return=0.035,
        ),
    ]


def _two_accounts() -> list[dict]:
    return [
        support.account_payload(),
        support.account_payload(
            account_id="a_wealth_backup",
            account_type="savings",
            balance_minor=500000,
            available_minor=500000,
        ),
    ]


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _table():
    """决策解析用的工具表：与工作流交给模型的是同一份（七个只读工具 + 八个控制类动作）。"""

    return tool_table()


def _ask_amount_then_account() -> support.ScriptModel:
    """一轮脚本：先记下金额，再追问付款账户。"""

    return support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
    )


# ---------------------------------------------------------------- 槽位


def test_only_missing_slots_are_asked(bank, double, clock):
    """已有金额不重复问，缺账户才追问账户；重复追问一个已知槽位会被纠正。"""

    session = {}
    pending = None
    double.on("GET", ACCOUNTS, data=_two_accounts())
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.call_read("wealth_accounts"),
        support.set_slots({"account_id": "a_wealth_main"}),
        support.ask_user("请问金额是多少？", ["plan_amount_minor"]),
        support.finish("好的，用主账户买入 2000.00 元。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="我想买两千块理财"),
        session=session,
        pending=pending,
    )

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.pending is not None
    assert asked.pending.prompt == "请问用哪个账户付款？"
    # 待办只等它真正缺的那个槽位。
    assert pending["missing"] == ("account_id",)
    assert session["slots"] == {"plan_amount_minor": 200000}

    answered, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert answered.status is WealthStatus.COMPLETED
    # 第二次决策时提示词里已经有金额：它不该再问，问了就被纠正。
    assert "2000.00 元（200000 分）" in model.prompts[4]
    assert "槽位 plan_amount_minor 已经知道，不要再追问" in model.prompts[5]
    assert model.calls == 6
    assert session["slots"] == {
        "plan_amount_minor": 200000,
        "account_id": "a_wealth_main",
    }


def test_an_unknown_or_bad_slot_value_is_corrected(bank, double, clock):
    """槽位名必须来自词表，金额必须是正整数分，编号必须来自查到的记录。"""

    session = {}
    pending = None
    double.on("GET", ACCOUNTS, data=_two_accounts())
    # 一次决策节点只纠正一次，所以每条坏决策后面都跟一条改好的决策。
    model = support.ScriptModel(
        support.call_read("wealth_accounts"),
        support.set_slots({"risk_appetite": "high"}),
        support.set_slots({"plan_amount_minor": 200000}),
        support.set_slots({"plan_amount_minor": "两千"}),
        support.set_slots({"plan_amount_minor": 200000}),
        support.set_slots({"account_id": "a_somebody_else"}),
        support.set_slots({"account_id": "a_wealth_main"}),
        support.finish("好的。"),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    assert "未知的槽位 risk_appetite" in model.prompts[2]
    # 金额规则现在只有一处（`domain.rules.normalize_amount_minor`），理由的说法因此更具体。
    assert "金额必须是整数分" in model.prompts[4]
    assert "account_id 只能来自已经查到的记录" in model.prompts[6]
    assert session["slots"] == {
        "plan_amount_minor": 200000,
        "account_id": "a_wealth_main",
    }


def test_the_order_amount_cannot_be_set_as_a_slot(bank, double, clock):
    """单笔订单金额不能靠 set_slots 进会话：它只能随 prepare_purchase／prepare_redeem 进来。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.set_slots({"amount_minor": 200000}),
        support.set_slots({"plan_amount_minor": 20000000}),
        support.finish("好的。"),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    assert "未知的槽位 amount_minor" in model.prompts[1]
    assert session["slots"] == {"plan_amount_minor": 20000000}


def test_the_rule_lets_the_model_ask_for_the_order_amount(bank, double, clock):
    """含糊金额要 ask_user 问清楚，而问题本身不再受槽位词表限制。

    会话槽位（SLOTS）里没有 amount_minor——它只能随 prepare_purchase／prepare_redeem 进来——
    所以"还没有填写的槽位"那份清单永远不含它。旧规则要求 missing 只能点名清单里的名字，模型于是
    只剩"猜一个金额"这一条路；新规则把问题交给模型自己组织，唯一的限制是"不要追问已经知道的信息"。
    """

    model = support.ScriptModel(support.finish("好的。"))
    agent = _agent(model, bank, clock)

    response, _, _ = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="我想买点理财"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    # 提示词点明：缺槽清单之外的问题（含糊金额就是其中之一）也可以问。
    assert "也可以追问这里没列出的问题" in model.prompts[0]
    assert "问题本身不受槽位词表限制" in model.prompts[0]
    # 追问规则只剩一条：已经知道的信息不要重复问。
    assert "不要追问已经知道的信息" in model.prompts[0]


def test_a_question_outside_the_slot_vocabulary_is_a_valid_ask_user(bank, double, clock):
    """自由提问是一条合法决策：问题不是槽位名、missing 为空，也一样能铸待办并等用户回答。"""

    session = {}
    pending = None
    question = "请问您的投资经验是？A 没有经验；B 有一些；C 很丰富"
    # 两种形状都合法：完全不写 missing，或显式给一个空的 missing（它只用于把候选补进展示顺序）。
    explicit = parse_decision(
        *support.decision_input(support.call("ask_user", {"question": question, "missing": []})),
        table=_table(),
    )
    assert explicit.action == "ask_user"
    assert explicit.question == question
    assert explicit.missing == ()

    model = support.ScriptModel(support.ask_user(question), support.finish("好的。"))
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="我想做一下风险测评"),
        session=session,
        pending=pending,
    )

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.pending is not None
    assert asked.pending.prompt == question
    # 没点名任何槽位也能铸待办：待办等的是用户的回答，不是某个槽位名。
    assert pending["missing"] == ()

    answered, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="B", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert answered.status is WealthStatus.COMPLETED
    assert answered.summary == "好的。"


def test_multiple_accounts_are_not_auto_selected(bank, double, clock):
    """查到两个账户也不许替用户挑一个：账户槽位仍然缺，运行时不会自己填。"""

    session = {}
    pending = None
    double.on("GET", ACCOUNTS, data=_two_accounts())
    model = support.ScriptModel(
        support.call_read("wealth_accounts"),
        support.ask_user("有两个账户，请问用哪个付款？", ["account_id"]),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="我要买两千块理财"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert session["slots"] == {}
    assert pending["missing"] == ("account_id",)
    assert [row["account_id"] for row in response.facts["accounts"]] == [
        "a_wealth_main",
        "a_wealth_backup",
    ]


def test_slots_are_isolated_between_users_and_sessions(bank, double, clock):
    """同一个进程里，另一个用户／另一个会话看不到本人已经填好的槽位。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.finish("好的。"),
    )
    agent = _agent(model, bank, clock)

    _, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )

    other_user = support.bound_context(user_id="user_0002")
    other_state: dict = {}

    _, other_state, _ = agent.handle(
        other_user,
        WealthRequest(kind="message", text="买两千块"),
        session=other_state,
        pending=None,
    )

    # 会话状态现在只存在于调用方手里：另一个用户／另一个会话拿到的都是自己的空状态，
    # 本人已填好的槽位不会跟着 agent 泄漏过去（agent 里已经没有第二张表，见
    # test_session_state.py::test_the_agent_keeps_no_session_tables）。
    assert other_state.get("slots", {}) == {}
    assert session["slots"] == {"plan_amount_minor": 200000}

    # 另一个用户这一轮的提示词里没有任何"已经知道"的槽位。
    assert NO_KNOWN_SLOTS in model.prompts[2]


def test_an_unanswered_question_is_put_in_front_of_the_model(bank, double, clock):
    """旧待办还没被回答时，新一轮的提示词会把它摆出来，并要模型先问是否放弃，而不是静默替换。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.finish("好的。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    _, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="买两千块"), session=session, pending=pending
    )
    _, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="对了，你们有几天免息期？"),
        session=session,
        pending=pending,
    )

    assert "你上一次向用户提了一个问题，用户还没有回答" in model.prompts[2]
    assert "请问用哪个账户付款？" in model.prompts[2]
    assert "是否放弃原来那件事" in model.prompts[2]
    # 同会话仍然只有一个活动待办。
    assert session["pending_id"] is not None


# ---------------------------------------------------------------- 指代


def test_reference_uses_display_order(bank, double, clock):
    """ "第二个"按最近展示顺序解析：展示顺序与数据库顺序相反时结果不同。"""

    session = {}
    pending = None
    double.on("GET", PRODUCTS, data=_two_products())
    double.on("GET", CASH_PRODUCT, data=support.product_payload())
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish(
            "1. 演示稳健理财；2. 演示灵活理财。",
            ["p_demo_stable", "p_demo_cash"],
            shown={"products": ["p_demo_stable", "p_demo_cash"]},
        ),
        support.set_slots({"product_id": {"ordinal": 2}}),
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("演示灵活理财：低风险，起购 100.00 元。", ["p_demo_cash"]),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    first, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="有哪些灵活理财？"),
        session=session,
        pending=pending,
    )

    assert first.status is WealthStatus.COMPLETED
    # 展示顺序按模型声明的顺序保存，而不是数据库返回的顺序。
    assert session["shown"] == {"products": ("p_demo_stable", "p_demo_cash")}

    second, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="第二个怎么样？"),
        session=session,
        pending=pending,
    )

    assert second.status is WealthStatus.COMPLETED
    # 提示词按展示顺序编号；"第二个"解析成展示里的第二项（数据库里的第二项是稳健）。
    assert "- products 第1个：p_demo_stable" in model.prompts[2]
    assert "- products 第2个：p_demo_cash" in model.prompts[2]
    assert session["slots"]["product_id"] == "p_demo_cash"
    assert [call.path for call in double.calls] == [PRODUCTS, CASH_PRODUCT]


def test_an_out_of_range_ordinal_is_corrected(bank, double, clock):
    """只展示过两项时说"第5个"：拒绝并纠正，不猜一个产品。"""

    session = {}
    pending = None
    double.on("GET", PRODUCTS, data=_two_products())
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish(
            "1. 演示灵活理财；2. 演示稳健理财。",
            shown={"products": ["p_demo_cash", "p_demo_stable"]},
        ),
        support.set_slots({"product_id": {"ordinal": 5}}),
        support.finish("我只有两只产品，请问您指哪一只？"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    _, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="有哪些产品？"),
        session=session,
        pending=pending,
    )
    second, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="第五个呢？"), session=session, pending=pending
    )

    assert second.status is WealthStatus.COMPLETED
    assert "最近没有展示过第 5 个候选" in model.prompts[3]
    assert "product_id" not in session["slots"]


# ---------------------------------------------------------------- 过期与失效


@pytest.mark.parametrize(
    "other",
    [
        support.bound_context(user_id="user_0002", session_id="sess_wealth_1"),
        support.bound_context(user_id="user_0001", session_id="sess_other"),
    ],
)
def test_pending_owner_session_expiry(bank, double, clock, other):
    """跨用户／跨会话的待办不能恢复：既不回答，也看不到本人的槽位。"""

    session = {}
    pending = None
    model = _ask_amount_then_account()
    agent = _agent(model, bank, clock)
    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )

    response, session, pending = agent.handle(
        other,
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert response.facts == {}
    assert model.calls == 2
    assert double.calls == []


def test_an_expired_pending_cannot_be_answered(bank, double, clock):
    session = {}
    pending = None
    model = _ask_amount_then_account()
    agent = _agent(model, bank, clock)
    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )

    clock.advance(PENDING_TTL)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert model.calls == 2


def test_a_restarted_agent_cannot_resume_a_pending(bank, double, clock):
    """Agent 重启可以丢临时会话，但必须重新确认；银行侧事实不因此改变。"""

    session = {}
    pending = None
    model = _ask_amount_then_account()
    agent = _agent(model, bank, clock)
    asked, _, _ = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session={},
        pending=None,
    )

    restarted_model = support.ScriptModel(support.finish("好的。"))
    restarted = _agent(restarted_model, bank, clock)

    stale, session, pending = restarted.handle(
        support.bound_context(),
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert stale.status is WealthStatus.FAILED
    assert stale.error_code == ErrorCode.INVALID_STATE
    assert restarted_model.calls == 0

    # 重启后槽位不复原：新一轮从"没有已经知道的槽位"开始，需要重新确认。
    _, session, pending = restarted.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="买两千块"),
        session=session,
        pending=pending,
    )
    assert NO_KNOWN_SLOTS in restarted_model.prompts[0]
    assert session["slots"] == {}


def test_change_invalidates_previous_intent(bank, double, clock):
    """改金额即换意图：旧确认、旧验证码请求都不能作用在新金额上。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
        support.set_slots({"plan_amount_minor": 1000000}),
        support.ask_user("改成一万元。请问用哪个账户付款？", ["account_id"]),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    first, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="帮我买两千块的理财"),
        session=session,
        pending=pending,
    )
    old_pending_id = first.pending.pending_id

    second, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="算了，改成一万块"),
        session=session,
        pending=pending,
    )

    assert second.status is WealthStatus.WAITING_INPUT
    assert second.pending.pending_id != old_pending_id
    assert session["slots"] == {"plan_amount_minor": 1000000}
    assert session["intent_version"] == 2

    confirmed, session, pending = agent.handle(
        context,
        WealthRequest(kind="confirm", pending_id=old_pending_id),
        session=session,
        pending=pending,
    )
    verified, session, pending = agent.handle(
        context,
        WealthRequest(kind="verify", pending_id=old_pending_id, verification_code="246810"),
        session=session,
        pending=pending,
    )
    answered, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="用主账户", pending_id=old_pending_id),
        session=session,
        pending=pending,
    )

    for response in (confirmed, verified, answered):
        assert response.status is WealthStatus.FAILED
        assert response.error_code == ErrorCode.INVALID_STATE
    assert double.calls == []
    assert "246810" not in repr(verified)

    # 新待办本身是有效的，但任务7 还没有可确认或可验证的资金操作。
    current, session, pending = agent.handle(
        context,
        WealthRequest(kind="confirm", pending_id=second.pending.pending_id),
        session=session,
        pending=pending,
    )
    assert current.status is WealthStatus.FAILED
    assert current.error_code == ErrorCode.INVALID_STATE
    assert double.calls == []


def test_clarifications_are_bounded(bank, double, clock):
    """连续澄清最多 5 次：第 6 次追问直接停止，不无限追问。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        *[support.ask_user("请问用哪个账户付款？", ["account_id"])] * (MAX_CLARIFICATIONS + 1)
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    for _ in range(MAX_CLARIFICATIONS):
        response, session, pending = agent.handle(
            context,
            WealthRequest(kind="message", text="买两千块"),
            session=session,
            pending=pending,
        )
        assert response.status is WealthStatus.WAITING_INPUT

    stopped, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="买两千块"), session=session, pending=pending
    )

    assert stopped.status is WealthStatus.FAILED
    assert stopped.error_code == ErrorCode.INTERNAL
    assert stopped.summary == CLARIFY_FAILURE_SUMMARY
    assert model.calls == MAX_CLARIFICATIONS + 1
    assert double.calls == []


# ---------------------------------------------------------------- 取消


def test_cancel_does_not_call_bank_write(bank, double, clock):
    """未提交的取消不调用模型、不发任何银行请求，也不声称撤销任何订单。"""

    session = {}
    pending = None
    model = _ask_amount_then_account()
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    asked, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="买两千块"), session=session, pending=pending
    )

    response, session, pending = agent.handle(
        context, WealthRequest(kind="cancel"), session=session, pending=pending
    )

    assert response.status is WealthStatus.CANCELLED
    assert model.calls == 2
    assert double.calls == []
    assert all(call.method == "GET" for call in double.calls)
    assert "撤销" not in response.summary
    assert "已提交" not in response.summary
    assert "没有提交任何交易" in response.summary

    # 待办与草稿都终止了：旧待办不能再回答，槽位也不再保留。
    assert pending is None
    assert session["slots"] == {}
    stale, session, pending = agent.handle(
        context,
        WealthRequest(kind="answer", text="用主账户", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )
    assert stale.status is WealthStatus.FAILED
    assert stale.error_code == ErrorCode.INVALID_STATE


def test_cancel_without_a_pending_is_still_a_clean_stop(bank, double, clock):
    model = support.ScriptModel()
    agent = _agent(model, bank, clock)

    response, _, _ = agent.handle(
        support.bound_context(), WealthRequest(kind="cancel"), session={}, pending=None
    )

    assert response.status is WealthStatus.CANCELLED
    assert model.calls == 0
    assert double.calls == []


# ---------------------------------------------------------------- 决策词表


def test_the_dialogue_actions_are_the_nine_the_workflow_understands():
    """动作词表只有这九项（任务10 在任务9 的八项上加了 `prepare_redeem` 赎回预览）：
    没有 purchase／redeem／confirm，也没有任何授权字段。"""

    assert ACTIONS == (
        "call_read",
        "set_slots",
        "ask_user",
        "submit_risk",
        "recommend",
        "compare",
        "prepare_purchase",
        "prepare_redeem",
        "finish",
    )
    assert SLOTS == (
        "plan_amount_minor",
        "product_id",
        "account_id",
        "holding_id",
        "horizon_months",
        "liquidity",
    )


@pytest.mark.parametrize(
    "tool_call",
    [
        # 缺必填字段、空对象、错类型。
        support.call("set_slots", {}),
        support.call("set_slots", {"slots": {}}),
        support.call("set_slots", {"slots": []}),
        # 点名了 missing 时，它必须是槽位名字符串。
        support.call("ask_user", {"question": "请问金额是多少？", "missing": [1]}),
        # finish 的回答字段叫 text；shown 是"资源名 → 编号列表"。
        support.call("finish", {"answer": "好"}),
        support.call("finish", {"text": "好", "shown": []}),
        support.call("finish", {"text": "好", "shown": {"products": "p_demo_cash"}}),
    ],
)
def test_an_unusable_dialogue_decision_is_a_decision_error(tool_call):
    with pytest.raises(DecisionError):
        parse_decision(*support.decision_input(tool_call), table=_table())


def test_the_dialogue_decisions_round_trip_through_state():
    """决策存进状态再读回来：形状不变，且没有任何授权字段。"""

    decision = parse_decision(
        *support.decision_input(support.set_slots({"product_id": {"ordinal": 2}})),
        table=_table(),
    )
    restored = WealthDecision.from_dict(decision.as_dict())

    assert restored == decision
    assert restored.slots == {"product_id": {"ordinal": 2}}

    # 新协议下"决策"就是一次工具调用：工具名与参数一起过状态往返。
    call = parse_decision(
        *support.decision_input(support.call_read("wealth_product", {"product_id": "p_demo_cash"})),
        table=_table(),
    )
    assert WealthDecision.from_dict(call.as_dict()) == call
    assert call.action == "call_read"
    assert call.tool == "wealth_product"
    assert call.arguments == {"product_id": "p_demo_cash"}

    # 状态里没有任何授权字段：确认、身份、验证码和幂等键都无处可写。
    state = decision.as_dict()
    assert {"confirm", "confirmed", "user_id", "verification_code", "idempotent_key"}.isdisjoint(
        set(state)
    )
