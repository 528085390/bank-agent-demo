"""任务3：模型负责下一步，程序负责可执行性（验收矩阵 T01–T06）。

这一层证明的是"**执行器尊重模型的决策，而且不偷偷多做一步**"，以及"同一个输入在工具返回不同
条件时会产生不同的后续选择"。两者都不能靠读提示词证明：

- 脚本模型测试（这里）证明**执行器**：模型说调哪个工具就只调那个，说结束就结束，缺证据时把
  "缺什么"交回去而不是自己拉起一条固定查询链。
- 真实模型测试（`tests/wealth_integration/`）证明**提示词与工具表**足以让模型按需选择。
  两者不能互相替代：脚本模型不会因为提示词写得差而选错，真实模型不会因为执行器有分支而多查一次。

每个用例都断言**实际发出的银行调用**（`double.calls` 的 method + path），而不只是话术或状态：
"没有固定查询链"这件事，只有请求序列能证明。
"""

from __future__ import annotations

import json

import pytest

from app.agents.decisions import DecisionError
from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.llm.decisions import WealthDecision, parse_decision
from app.modules.wealth.llm.toolcalls import function_schemas, tool_table
from app.modules.wealth.workflow.state import (
    ASKABLE_SLOTS,
    MAX_MODEL_CALLS,
    MAX_STEPS,
)
from app.tools.schema import normalize_parameters
from tests.wealth import support

PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
HOLDINGS = "/holdings"
LATEST = "/wealth/risk-assessments/latest"

ACCOUNT = "a_wealth_main"
AMOUNT_MINOR = 200000
PLAN_AMOUNT_MINOR = 20000000  # 20 万


def _stub(double, *, products=True, accounts=True, profile=True, holdings=True):
    """只预设这一次要用的端点；没预设的调用会让 `BankDouble` 直接报错。

    这正是"没有无关预取"的断言方式：多查一个端点就是一次 `AssertionError`，而不是一条静默的
    多余请求。
    """

    if products:
        double.on("GET", PRODUCTS, data=support.wealth_demo_products())
        double.on("GET", PRODUCT, data=support.product_payload())
    if accounts:
        double.on("GET", ACCOUNTS, data=[support.account_payload()])
    if profile:
        double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    if holdings:
        double.on("GET", HOLDINGS, data=[support.holding_payload()])
    return double


def _paths(double) -> list[str]:
    return [call.path for call in double.calls]


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _ask(agent, context, text="随便问问", *, session=None, pending=None):
    return agent.handle(
        context,
        WealthRequest(kind="message", text=text),
        session=session or {},
        pending=pending,
    )


# ---------------------------------------------------------------------- T01 解释术语


def test_t01_an_explanation_finishes_with_zero_bank_calls(bank, double, clock):
    """T01：解释理财术语可以零工具调用，没有固定查询链。"""

    # 一个端点都不预设：任何一次银行调用都会变成 AssertionError。
    model = support.ScriptModel(
        support.finish(
            "期限指产品约定的投资时长。",
        )
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "期限是什么意思？", session={}, pending=None
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.summary == "期限指产品约定的投资时长。"
    assert double.calls == []
    # 一次决策就结束：模型没有被逼着先查点什么。
    assert model.calls == 1
    assert response.facts == {}


def test_t01_a_concept_question_is_not_turned_into_a_product_search(bank, double, clock):
    """T01：概念问题不会因为"提到产品"就自动去查产品列表。"""

    model = support.ScriptModel(support.finish("起购金额是买入的最低金额。"))
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "理财产品的起购金额是什么意思？", session={}, pending=None
    )

    assert response.status is WealthStatus.COMPLETED
    assert double.calls == []


# ---------------------------------------------------------------------- T02 只问产品期限


def test_t02_asking_one_product_term_reads_only_that_product(bank, double, clock):
    """T02：查询某产品期限 → 模型选产品工具，不无故查账户／测评。"""

    _stub(double, products=True, accounts=False, profile=False, holdings=False)
    model = support.ScriptModel(
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("期限 0 个月。", cited_ids=["p_demo_cash"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "演示灵活理财的期限是多久？", session={}, pending=None
    )

    assert response.status is WealthStatus.COMPLETED
    assert _paths(double) == [PRODUCT]
    assert response.business_ids.get("product_id") == "p_demo_cash"


def test_t02_the_model_may_list_products_first_and_nothing_else(bank, double, clock):
    """T02：模型也可以先列产品再挑一只——但不会顺手把账户和测评一起读掉。"""

    _stub(double, products=True, accounts=False, profile=False, holdings=False)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("期限 0 个月。", cited_ids=["p_demo_cash"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "演示灵活理财期限多久？", session={}, pending=None
    )

    assert response.status is WealthStatus.COMPLETED
    assert _paths(double) == [PRODUCTS, PRODUCT]


# ---------------------------------------------------------------------- T03 测评有效／无效


def test_t03_the_same_request_takes_different_next_steps_by_observation(bank, double, clock):
    """T03：同一个推荐请求，测评有效与无效产生不同的下一步。

    有效：推荐直接算出来。无效：节点交回 `needs_evidence`，模型改成问问卷。两条路径的银行调用
    与最终状态都不同——这就是"根据观察产生不同下一步"，而不是"总走同一串工具"。
    """

    # 有效测评：模型查测评 + 产品，然后推荐。
    session = {}
    pending = None
    _stub(double, products=True, accounts=False, holdings=False)
    good = support.ScriptModel(
        support.set_slots(
            {"plan_amount_minor": PLAN_AMOUNT_MINOR, "horizon_months": 3, "liquidity": "flexible"}
        ),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish("为您筛出候选。", shown={"products": ["p_demo_cash"]}),
    )
    agent = _agent(good, bank, clock)
    valid, session, pending = _ask(
        agent, support.bound_context(), "推荐一下", session=session, pending=pending
    )

    assert valid.status is WealthStatus.COMPLETED
    assert _paths(double) == [LATEST, PRODUCTS]

    # 无效测评：模型查测评，拿到 needs_evidence，改成问问卷。
    double2 = support.BankDouble()
    double2.on("GET", LATEST, data=support.risk_profile_payload(valid=False))
    poor = support.ScriptModel(
        support.set_slots(
            {"plan_amount_minor": PLAN_AMOUNT_MINOR, "horizon_months": 3, "liquidity": "flexible"}
        ),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user("请先回答问卷：您的投资经验是？"),
    )
    agent2 = _agent(poor, support.ClickConfirmationBank(double2.client()), clock)
    blocked, _, _ = _ask(agent2, support.bound_context(), "推荐一下", session={}, pending=None)

    assert blocked.status is WealthStatus.WAITING_INPUT
    assert blocked.summary == "请先回答问卷：您的投资经验是？"
    # 无效测评就不查产品：没有把"推荐"硬走成"先拉一遍产品再说"。
    assert _paths(double2) == [LATEST]


def test_t03_no_valid_assessment_means_no_personalised_recommendation(bank, double, clock):
    """T03：没有有效测评时不输出适配推荐——硬条件不放宽。"""

    session = {}
    pending = None
    _stub(double, products=False, accounts=False, holdings=False)
    double.on("GET", LATEST, data=None)
    model = support.ScriptModel(
        support.set_slots(
            {"plan_amount_minor": PLAN_AMOUNT_MINOR, "horizon_months": 3, "liquidity": "flexible"}
        ),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user("银行还没有您的测评，请先回答问卷。"),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = _ask(
        agent, support.bound_context(), "推荐一下", session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert "recommendation" not in response.facts
    assert _paths(double) == [LATEST]


# ---------------------------------------------------------------------- T04 对比


def test_t04_a_comparison_never_purchases_and_never_forces_a_questionnaire(bank, double, clock):
    """T04：对比两三个产品 → 工具由模型选择，程序计算；不自动下单，不强制问卷。"""

    _stub(double, accounts=False, holdings=False)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_risk_profile"),
        support.compare("p_demo_cash", "p_demo_stable"),
        support.finish(
            "两只产品的客观差异如上。", shown={"products": ["p_demo_cash", "p_demo_stable"]}
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent,
        support.bound_context(),
        "对比一下演示灵活理财和演示稳健理财",
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    # 对比用本轮已经查到的产品，不逐只重新拉取；没有写请求，也没有强制问卷。
    assert _paths(double) == [PRODUCTS, LATEST]
    assert response.facts["comparison"]["assessment_valid"] is True
    assert len(response.facts["comparison"]["rows"]) == 2


def test_t04_a_comparison_works_without_an_assessment(bank, double, clock):
    """T04：没有测评也能客观对比，但不因此说可以购买。"""

    _stub(double, accounts=False, holdings=False, profile=False)
    double.on("GET", LATEST, data=None)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_risk_profile"),
        support.compare("p_demo_cash", "p_demo_stable"),
        support.finish("客观参数如上；没有测评，无法判断是否适合您。"),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "对比这两只", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert response.facts["comparison"]["assessment_valid"] is False
    # 没有测评也不去开问卷：对比不需要它。
    assert response.status is not WealthStatus.WAITING_INPUT


def test_t04_comparing_an_unfetched_product_asks_for_that_evidence(bank, double, clock):
    """T04：对比里出现本轮没查到的编号 → 交回缺证据，模型去查它，而不是被当成决策错误。"""

    _stub(double, accounts=False, holdings=False)
    double.on(
        "GET",
        "/products/p_demo_stable",
        data=support.product_payload(
            product_id="p_demo_stable",
            name="演示稳健理财",
            risk_level="medium",
            duration_months=3,
            redeem_rule="maturity_only",
            min_amount_minor=100000,
        ),
    )
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.compare("p_demo_cash", "p_demo_stable"),
        support.call_read("wealth_product", {"product_id": "p_demo_stable"}),
        support.compare("p_demo_cash", "p_demo_stable"),
        support.finish("对比完成。"),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "对比这两只", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert _paths(double) == [PRODUCTS, "/products/p_demo_stable"]
    # 缺证据这条事实是"对比"报的，不是"推荐"报的。
    assert "wealth_compare" in model.prompts[2]
    assert "p_demo_stable" in model.prompts[2]


# ---------------------------------------------------------------------- T05 非法工具／参数


def test_t05_an_unknown_tool_is_refused_and_does_not_widen_permissions(bank, double, clock):
    """T05：工具名非法 → 拒绝执行，有限纠正，不扩大权限。"""

    model = support.ScriptModel(
        support.call("wealth.purchase", {"product_id": "p_demo_cash"}),
        support.finish("我不能直接下单。"),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "帮我买一只", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert double.calls == []
    # 纠正提示告诉模型它点名的工具不在词表里；权限没有被"顺手放宽"。
    assert "工具不在词表里：wealth.purchase" in model.prompts[1]


def test_t05_bad_arguments_are_refused_without_touching_the_bank(bank, double, clock):
    """T05：参数不合法 → 拒绝执行，纠正一次，不发银行调用。"""

    model = support.ScriptModel(
        support.call_read("wealth_product", {}),  # 缺必填的 product_id
        support.finish("请告诉我要查哪一只产品。"),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "查一下产品", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert double.calls == []
    # 缺必填参数时 jsonschema 报在根上（`required` 是对象级约束）：提示里带规则名，不带取值。
    assert "argument (root): failed required" in model.prompts[1]


def test_t05_the_correction_budget_is_bounded(bank, double, clock):
    """T05：纠正不是无限的：连续不可用的输出会以失败收尾，而不是一直重问。"""

    from app.modules.wealth.workflow.nodes import DECISION_FAILURE_SUMMARY

    model = support.ScriptModel(
        support.call("wealth.purchase", {}),
        support.call("wealth.purchase", {}),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "帮我买一只", session={}, pending=None)

    assert response.status is WealthStatus.FAILED
    assert response.summary == DECISION_FAILURE_SUMMARY
    assert double.calls == []
    assert model.calls == 2


def test_t05_the_budgets_are_unchanged_by_the_tool_protocol():
    """T05：有限循环的上限没有被这次改造取消。"""

    assert MAX_MODEL_CALLS == 8
    assert MAX_STEPS == 12
    table = tool_table()
    # 表里没有任何资金写：模型能表达的动作里根本没有"下单"这一项。
    assert {"purchase", "redeem", "confirm", "verify"}.isdisjoint(table)
    # 七个只读工具都在，而且都能被模型选中。
    assert {
        "wealth_products",
        "wealth_product",
        "wealth_accounts",
        "wealth_holdings",
        "wealth_holding",
        "wealth_risk_profile",
        "wealth_order",
    } <= set(table)


# ---------------------------------------------------------------------- T06 预算与订单金额


def test_t06_the_plan_amount_never_becomes_the_order_amount(bank, double, clock):
    """T06：推荐预算 20 万、实际买 2000 元 → 第一笔只用明确的 2000 元。"""

    session = {}
    pending = None
    _stub(double)
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": PLAN_AMOUNT_MINOR}),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}
        ),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = _ask(
        agent, support.bound_context(), "买 2000 元的演示灵活理财", session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    preview = response.facts["purchase_preview"]
    assert preview["amount_minor"] == AMOUNT_MINOR
    # 计划金额留在会话里（它是推荐筛选条件），但没有变成订单金额。
    assert preview["amount_minor"] != PLAN_AMOUNT_MINOR


def test_t06_a_second_purchase_without_an_amount_is_asked_about(bank, double, clock):
    """T06：第二笔没说金额 → 追问；不继承预算，也不继承上一笔的金额。

    这里断言的是**程序有没有认为它已经知道金额**：上一笔的订单金额绝不能留在会话槽位里，
    否则程序会替用户把上一笔的 2000 元当成这一笔的金额。
    """

    session = {}
    pending = None
    _stub(double)
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": PLAN_AMOUNT_MINOR}),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}
        ),
        support.ask_user("请问这一笔要买多少钱？", ["amount_minor"]),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    first, session, pending = _ask(
        agent, context, "买 2000 元的演示灵活理财", session=session, pending=pending
    )
    assert first.facts["purchase_preview"]["amount_minor"] == AMOUNT_MINOR

    # 会话里只有用户说过的长期条件：单笔订单金额不在其中。
    assert "amount_minor" not in session["slots"]
    assert session["slots"]["plan_amount_minor"] == PLAN_AMOUNT_MINOR

    second, session, pending = _ask(
        agent, context, "再买一笔同样的产品", session=session, pending=pending
    )

    assert second.status is WealthStatus.WAITING_INPUT
    assert second.summary == "请问这一笔要买多少钱？"
    assert pending["missing"] == ("amount_minor",)
    # 追问能成立本身就说明程序没有把上一笔的金额当成已知（否则会拒绝这次追问）。
    assert "amount_minor" not in session["slots"]
    assert _paths(double).count("/products/purchase") == 0


def test_t06_the_order_amount_is_not_written_into_outer_memory(bank, double, clock):
    """T06：单笔订单金额不进外层记忆——否则下一笔会被"继承"。"""

    _stub(double)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "买 2000 元的演示灵活理财", session={}, pending=None
    )

    answered = (response.context_update or {}).get("answered", {})
    assert "amount_minor" not in answered
    # 会话槽位本身照常交回（它们是用户说过的长期条件）。
    assert answered.get("product_id") == "p_demo_cash"
    assert answered.get("account_id") == ACCOUNT


# ---------------------------------------------------------------------- 追问声明必填


def test_an_ask_user_without_a_missing_declaration_is_refused(bank, double, clock):
    """`missing` 是**必填**声明：省略它，"已知信息不重复问"就无从机械检查。

    这正是把它从可选改成必填的原因——可选等于可以省略，一省略，程序既拿不到候选展示所需的
    槽位名，也没法判断这次提问是不是在重复问已经知道的东西。
    """

    model = support.ScriptModel(
        support.call("ask_user", {"question": "请问您计划什么时候用这笔钱？"}),
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "帮我推荐", session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    # 缺必填字段，jsonschema 报在根上；纠正提示让模型补上声明，而不是放弃这次追问。
    assert "argument (root): failed required" in model.prompts[1]
    assert double.calls == []


def test_an_ask_user_declaring_a_known_slot_is_refused(bank, double, clock):
    """点名一个已经知道的槽位 → 拒绝。这就是"已知信息不重复问"的机械那一半。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.set_slots({"horizon_months": 3}),
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
        support.ask_user("请问您希望随时可以赎回吗？", ["liquidity"]),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = _ask(
        agent, support.bound_context(), "三个月后要用", session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.summary == "请问您希望随时可以赎回吗？"
    assert "槽位 horizon_months 已经知道，不要再追问" in model.prompts[2]


def test_an_ask_user_declaring_a_name_outside_the_vocabulary_is_refused(bank, double, clock):
    """`missing` 只能列词表里的槽位名：程序不认识的名字既驱动不了候选，也检查不了，等于没声明。"""

    model = support.ScriptModel(
        support.ask_user("请问您的投资经验如何？", ["q1"]),
        support.ask_user("请问您的投资经验如何？", []),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "帮我推荐", session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    assert "未知的槽位 q1" in model.prompts[1]


def test_a_free_question_with_an_empty_declaration_stays_valid(bank, double, clock):
    """不在补槽位（例如问问卷的某一题）就写空数组：**问题本身仍然自由**，没有回到词表限制。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.ask_user("您的投资经验属于哪一档？", []),
        support.finish("好的。"),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = _ask(agent, context, "帮我推荐", session=session, pending=pending)

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.summary == "您的投资经验属于哪一档？"
    assert asked.pending is not None
    # 空声明不会把候选塞进展示顺序，也不会被误判成"在补某个槽位"。
    assert pending["missing"] == ()


def test_the_askable_vocabulary_is_what_a_declaration_may_name():
    """声明的合法取值就是 `ASKABLE_SLOTS`：会话槽位，加上"本次操作金额"。"""

    assert "horizon_months" in ASKABLE_SLOTS
    assert "liquidity" in ASKABLE_SLOTS
    assert "amount_minor" in ASKABLE_SLOTS
    # 问卷题目不是槽位，所以它只能靠空声明表达。
    assert "q1" not in ASKABLE_SLOTS


# ---------------------------------------------------------------------- 交易预检是操作不变量


def test_a_purchase_preview_reads_what_the_model_never_asked_for(bank, double, clock):
    """交易预检是**操作不变量**：模型没查的，程序也要查（计划 §3.2 第 6 条）。

    模型这一次只查了产品列表和账户列表（用来给编号提供依据），**没有**查风险测评、也没有查单只
    产品详情。程序仍然把产品详情、账户和测评都读了一遍——否则"不查"就能绕过风险与余额校验，
    而那正是预检存在的理由。
    """

    _stub(double, holdings=False)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(
        agent, support.bound_context(), "买 2000 元的演示灵活理财", session={}, pending=None
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert _paths(double) == [PRODUCTS, ACCOUNTS, PRODUCT, ACCOUNTS, LATEST]
    # 关键的一条：风险测评是**程序**读的。模型只说了三次话（列产品、列账户、申请预览），
    # 它从来没有、也没有机会点名 wealth_risk_profile。
    assert model.calls == 3


def test_a_redeem_preview_reads_its_pre_check_and_never_the_risk_profile(bank, double, clock):
    """赎回预检同样固定，而且**不读风险测评**：赎回是取回自己的持仓，不要求重做问卷。

    这里两条不变量一起断言：模型没点名的持仓详情与产品详情被程序读了；而风险测评**一次都没读**
    ——"不查"也是一条不变量，不是"查了不用"。
    """

    _stub(double, profile=True, holdings=True)
    double.on("GET", "/holdings/h_wealth_sample", data=support.holding_payload())
    model = support.ScriptModel(
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {"holding_id": "h_wealth_sample", "amount_minor": 50000, "account_id": ACCOUNT}
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "赎回 500 元", session={}, pending=None)

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert _paths(double) == [HOLDINGS, ACCOUNTS, "/holdings/h_wealth_sample", PRODUCT, ACCOUNTS]
    assert LATEST not in _paths(double)


@pytest.mark.parametrize(
    "tool",
    [
        "wealth_products",
        "wealth_accounts",
        "wealth_holdings",
        "wealth_risk_profile",
    ],
)
def test_an_unrelated_tool_is_only_called_when_the_model_asks_for_it(bank, double, clock, tool):
    """每个只读工具都只在模型点名时才发请求：没有"顺手多查一个"。"""

    _stub(double)
    model = support.ScriptModel(
        support.call_read(tool),
        support.finish("查到了。"),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "看看", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert len(double.calls) == 1


# ---------------------------------------------------------------------- T-1..T-9 原生工具调用


def test_t01_a_decision_goes_through_stream_with_tools(bank, double, clock):
    """T-1：决策走 `stream` 并带上原生工具定义；`complete` 一次都没被调用。"""

    model = support.ScriptModel(support.finish("好的。"))
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "随便问问", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert model.streams == 1
    # 只有 stream 被调用过：calls 与 streams 同时为 1，complete 没有被调用。
    assert model.calls == 1
    names = {schema["function"]["name"] for schema in model.tools[0]}
    assert {"set_slots", "ask_user", "finish"} <= names
    assert {
        "wealth_products",
        "wealth_product",
        "wealth_accounts",
        "wealth_holdings",
        "wealth_holding",
        "wealth_risk_profile",
        "wealth_order",
    } <= names


def test_t02_the_wire_schema_is_strict_expressible():
    """T-2：送出去的 schema 是 strict 可表达的：闭合、属性全必填、无不可表达关键词。"""

    table = tool_table()
    schemas = function_schemas(table)

    assert [schema["function"]["name"] for schema in schemas] == list(table)
    for schema in schemas:
        function = schema["function"]
        parameters = function["parameters"]
        assert schema["type"] == "function"
        assert parameters["additionalProperties"] is False
        _, expressible = normalize_parameters(table[function["name"]].parameters)
        # `strict` 必须等于 `strict and expressible`：不可表达的工具不许声称 strict。
        assert function["strict"] is expressible
        if not expressible:
            continue
        assert set(parameters["properties"]) <= set(parameters["required"])
        rendered = json.dumps(parameters, ensure_ascii=False)
        for keyword in ("minLength", "minItems", "maxItems", "minProperties", "pattern"):
            assert keyword not in rendered


def test_t03_the_dropped_constraints_land_in_the_description():
    """T-3：被 strict 拿掉的约束改写成 description 里的话，模型仍然看得见。"""

    schemas = {schema["function"]["name"]: schema for schema in function_schemas(tool_table())}

    description = schemas["compare"]["function"]["description"]

    assert "至少 2" in description


def test_t04_the_parameter_check_is_not_relaxed():
    """T-4：schema 换成了原生定义，校验没有放松：只给一个 product_id 仍然报错。"""

    with pytest.raises(DecisionError):
        parse_decision(*support.decision_input(support.compare("p_demo_cash")), table=tool_table())


def test_t05_two_tool_calls_in_one_turn_are_refused():
    """T-5：一次只允许一个工具调用；同一轮给两个直接拒绝。"""

    with pytest.raises(DecisionError) as failure:
        parse_decision(
            "",
            [support.recommend(), support.compare("p_demo_cash", "p_demo_stable")],
            table=tool_table(),
        )

    assert "一次只能调用一个工具" in str(failure.value)


def test_t05b_multiple_read_calls_are_accepted_in_one_turn():
    """T-5b：同一轮多个调用全是只读工具时接受，并按顺序保留下来。"""

    decision = parse_decision(
        "",
        [
            support.call_read("wealth_products", {"risk_level": None}),
            support.call_read("wealth_accounts", {}),
        ],
        table=tool_table(),
    )

    assert decision.action == "call_read"
    assert decision.tool == "wealth_products"
    assert [call["tool"] for call in decision.read_calls] == [
        "wealth_products",
        "wealth_accounts",
    ]
    assert decision.read_calls[0]["arguments"] == {}
    assert WealthDecision.from_dict(decision.as_dict()) == decision


def test_t05c_multiple_read_calls_still_reject_a_control_action():
    """T-5c：并行只读里混入一个控制动作，仍按一次一个拒绝。"""

    with pytest.raises(DecisionError) as failure:
        parse_decision(
            "",
            [
                support.call_read("wealth_products"),
                support.recommend(),
            ],
            table=tool_table(),
        )

    assert "一次只能调用一个工具" in str(failure.value)


def test_t07_prose_only_is_equivalent_to_one_finish(bank, double, clock):
    """T-7：只回散文时等价于一次 `finish`，`answer` 是 strip 之后的结果。"""

    decision = parse_decision(*support.text_reply("  可以。  "), table=tool_table())
    assert decision.action == "finish"
    assert decision.answer == "可以。"

    model = support.ScriptModel("  期限指产品约定的投资时长。  ")
    agent = _agent(model, bank, clock)

    response, _, _ = _ask(agent, support.bound_context(), "什么是期限？", session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert response.summary == "期限指产品约定的投资时长。"
    assert double.calls == []


def test_t09_the_prompt_has_no_tool_table_text(bank, double, clock):
    """T-9：工具表不再进提示词——它是原生工具定义，不是一段文本。"""

    model = support.ScriptModel(support.finish("好的。"))
    agent = _agent(model, bank, clock)

    _ask(agent, support.bound_context(), "随便问问", session={}, pending=None)

    prompt = model.prompts[0]
    assert "### set_slots" not in prompt
    assert '{"tool"' not in prompt


def test_t16_the_wire_schema_and_the_validator_agree_on_a_null_argument():
    """T-16：strict 下模型会显式回 `cited_ids: null`；校验器不能把它当成格式错。"""

    decision = parse_decision(
        *support.decision_input(support.call("finish", {"text": "可以。", "cited_ids": None})),
        table=tool_table(),
    )

    assert decision.action == "finish"
    assert decision.cited_ids == ()
