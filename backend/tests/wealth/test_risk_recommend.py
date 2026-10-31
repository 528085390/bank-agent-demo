"""任务8：接银行测评，完成推荐与对比。

六组断言（计划任务8 的验收）：

1. **测评以银行为准**：推荐前先取银行最近测评；没有或已经过期时，推荐节点不再自己开五题问卷，
   而是回一条 `needs_evidence` 事实（缺 `risk_profile`），由模型据此向用户问完五题；问卷保存失败
   不产生任何本地档案；模型在对话里自称的等级覆盖不了银行返回的等级。
2. **问卷落库**：答案齐全才 POST，请求体只有 `answers`；等级、分数、有效期和 `assessment_id`
   都来自银行返回，模型给的 `risk_level` 会被直接拒绝。
3. **筛选**：按风险、在售、金额上下限、期限和流动性过滤；停售的 9% 产品不因参数高进入候选。
4. **排序与截断**：最多三个候选，按风险、期限、产品编号排序；落选的每一只都带原因。
5. **没有候选**：如实说明，不放宽风险凑数，也没有任何资金写请求。
6. **对比**：两到三个**本轮已经查到的**产品，列风险、期限、起购额、赎回条件、模拟收益参数和适配
   原因；可以客观对比不适配的产品，但不因此说可以购买，也不产生资金写。

模型这一层走的是工具协议 `{"tool": ..., "arguments": {...}}`（词表是 `llm/toolcalls.py` 的工具
表）。推荐与对比**消费本轮已经查到的事实**：缺什么就回一条 `needs_evidence`（推荐节点不自己拉
问卷，对比节点也不自己重查产品），由模型决定下一步查哪个工具或问什么。所以每个用例都要先把证据
（测评、产品列表）脚本出来，再调 `recommend`／`compare`。

全部使用 HTTP 传输替身（离线层）：这里证明的是决策、门禁与规则，真实 HTTP 端到端属于任务9–12。
"""

from __future__ import annotations

import json

import pytest

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.domain.rules import OPTIONS, QUESTION_IDS, QUESTIONS
from app.modules.wealth.infrastructure.tools import READ_TOOLS
from app.modules.wealth.llm.decisions import ACTIONS, DecisionError, WealthDecision, parse_decision
from app.modules.wealth.llm.toolcalls import tool_table
from app.modules.wealth.workflow.nodes import ASSESSMENT_FAILURE_SUMMARY, CLARIFY_FAILURE_SUMMARY
from app.modules.wealth.workflow.state import MAX_CLARIFICATIONS
from app.tools.protocol import ErrorCode
from tests.wealth import support

PRODUCTS = "/products"
LATEST = "/wealth/risk-assessments/latest"
ASSESSMENTS = "/wealth/risk-assessments"
# 对比只消费产品列表里已经查到的记录，所以这里不再为"逐只重查产品"准备端点；
# 唯一的单只详情端点是模型补查那只它自己提过、但本轮还没查到的产品。
GHOST_PRODUCT = "/products/p_demo_ghost"

ANSWERS = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 2}
RECOMMEND_SLOTS = {"plan_amount_minor": 200000, "horizon_months": 1, "liquidity": "flexible"}

# 节点报告"缺证据"时用的状态：它既不是成功也不是失败，模型据此决定去查还是去问。
NEEDS_EVIDENCE = "needs_evidence"


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _recommendation(response):
    return response.facts["recommendation"]


def _not_recommended(response) -> dict[str, dict]:
    return {row["product_id"]: row for row in _recommendation(response)["not_recommended"]}


def _observations(prompt: str) -> list[dict]:
    """模型这一轮看到的工具事实：提示词里"已经取得的工具事实"那一段的 JSON 原文。

    推荐／对比缺证据时节点不改写 `facts`，只把一条 `needs_evidence` 观察结果交回决策节点；这条
    观察结果唯一的出口就是下一轮提示词，所以断言从提示词里读回来，而不是断言一句转述。
    """

    marker = "已经取得的工具事实（唯一可信的数据来源）：\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n\n调用工具时用本轮提供的工具定义", start)
    block = prompt[start:end]
    return json.loads(block) if block.startswith("[") else []


def _needs_evidence(prompt: str) -> dict:
    """提示词里最后一条"缺证据"观察结果；没有就断言失败。"""

    observation = _observations(prompt)[-1]
    assert observation["status"] == NEEDS_EVIDENCE
    return observation["facts"]["needs_evidence"]


# ---------------------------------------------------------------- 测评以银行为准


def test_agent_uses_bank_assessment_not_model_grade(bank, double, clock):
    """模型在对话里自称"激进型"：推荐仍按银行返回的 low 筛，且不产生新的测评记录。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low", score=2))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish(
            "银行给您的等级是低风险，因此只推荐低风险产品：演示灵活理财。", ["p_demo_cash"]
        ),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="我是激进型投资者，直接推荐年化最高的产品给我"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    recommendation = _recommendation(response)
    assert recommendation["risk_level"] == "low"
    assert recommendation["assessment_id"] == "wealth_assessment_1"
    assert [row["product_id"] for row in recommendation["candidates"]] == ["p_demo_cash"]
    assert {row["risk_level"] for row in recommendation["candidates"]} == {"low"}
    # 两次调用都是只读：取银行测评、查产品列表。没有保存测评，也没有资金写。
    assert [call.method for call in double.calls] == ["GET", "GET"]
    assert [call.path for call in double.calls] == [LATEST, PRODUCTS]
    # 提示词里带着五题问卷，模型才有可能把用户的回答映射成标准选项。
    assert "本金损失接受度" in model.prompts[0]


def test_recommend_without_assessment_reports_missing_evidence(bank, double, clock):
    """银行没有测评：推荐节点不再自己开问卷，只回一条 needs_evidence（缺 risk_profile）。

    问题由模型组织——节点报告缺什么，模型据此问用户。这一轮也不查产品、不保存、不发任何资金写。
    """

    session = {}
    pending = None
    double.on("GET", LATEST, data=None)
    question = "推荐前需要先完成风险测评，请告诉我您能接受多大的本金损失。"
    model = support.ScriptModel(
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user(question),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.pending is not None
    # 问卷是模型问的：这一轮的话术就是模型自己组织的那句，节点没有替它写问题。
    assert response.summary == question
    missing = _needs_evidence(model.prompts[3])
    assert missing["resource"] == "risk_profile"
    # 提示词里带着五题问卷，模型才有可能把用户的回答映射成标准选项。
    assert "本金损失接受度" in model.prompts[0]
    assert "短期资金需求" in model.prompts[0]
    # 三个推荐条件都齐了，缺的只有银行测评：这一轮只读了一次测评。
    assert [call.path for call in double.calls] == [LATEST]


def test_an_expired_assessment_is_reported_as_missing_evidence(bank, double, clock):
    """过期测评不算有效：仍然只有"缺测评"这一条证据，而且说的是"过期"，不拿过期等级推荐。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low", valid=False))
    question = "银行里的测评已经失效，请重新回答五个问题。"
    model = support.ScriptModel(
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user(question),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.summary == question
    missing = _needs_evidence(model.prompts[3])
    assert missing["resource"] == "risk_profile"
    # 原因说的是"过期"，不是"没查到"：模型据此决定是重问问卷还是先查一次。
    assert "过期" in missing["reason"]
    assert [call.path for call in double.calls] == [LATEST]


def test_failed_assessment_save_blocks_profile_use(bank, double, clock):
    """POST 失败：以 failed 收尾且不产生任何有效档案；再要推荐时仍然只有"缺测评"这一条证据。"""

    session = {}
    pending = None
    double.on("POST", ASSESSMENTS, code=1012, data=None)
    double.on("GET", LATEST, data=None)
    question = "还没有有效的风险测评，请先回答五个问题。"
    model = support.ScriptModel(
        support.submit_risk(ANSWERS),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user(question),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    failed, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="1、1、1、1、2"),
        session=session,
        pending=pending,
    )

    assert failed.status is WealthStatus.FAILED
    assert failed.error_code == ErrorCode.VALIDATION
    assert failed.summary == ASSESSMENT_FAILURE_SUMMARY
    assert "risk_profile" not in failed.facts
    assert failed.business_ids == {}

    again, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="那直接给我推荐吧"),
        session=session,
        pending=pending,
    )

    assert again.status is WealthStatus.WAITING_INPUT
    assert again.summary == question
    # 银行里没有测评，本地也没有档案：推荐只能拿到"缺测评"，由模型重新问问卷。
    assert _needs_evidence(model.prompts[3])["resource"] == "risk_profile"
    assert [call.method for call in double.calls] == ["POST", "GET"]
    assert [call.path for call in double.calls] == [ASSESSMENTS, LATEST]


# ---------------------------------------------------------------- 问卷落库


def test_the_questionnaire_is_saved_through_the_bank(bank, double, clock):
    """答案齐全才 POST；请求体只有 answers，等级与编号以银行返回为准。"""

    double.on(
        "POST",
        ASSESSMENTS,
        data=support.risk_profile_payload(risk_level="medium", score=6, answers=ANSWERS),
        status=201,
    )
    model = support.ScriptModel(
        support.submit_risk(ANSWERS),
        support.finish("银行已保存您的测评：中风险，30 天内有效。", ["wealth_assessment_1"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="1、1、1、1、2"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    call = double.only_call()
    assert call.method == "POST"
    assert call.path == ASSESSMENTS
    assert call.body == {"answers": ANSWERS, "user_id": support.DEMO_CUSTOMER}
    assert call.idempotency_key is None
    assert response.facts["risk_profile"]["risk_level"] == "medium"
    assert response.facts["risk_profile"]["answers"] == ANSWERS
    assert response.business_ids == {"assessment_id": "wealth_assessment_1"}


def test_a_saved_assessment_is_read_back_from_the_bank(bank, double, clock):
    """保存之后再推荐：等级来自银行重新读到的记录，而不是本地记住的那次回执。"""

    session = {}
    pending = None
    double.on(
        "POST",
        ASSESSMENTS,
        data=support.risk_profile_payload(risk_level="low", score=2),
        status=201,
    )
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low", score=2))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.submit_risk(ANSWERS),
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish("银行测评等级为低风险，推荐演示灵活理财。", ["p_demo_cash"]),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="1、1、1、1、2，给我推荐一只"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    assert _recommendation(response)["risk_level"] == "low"
    assert [call.method for call in double.calls] == ["POST", "GET", "GET"]
    assert [call.path for call in double.calls] == [ASSESSMENTS, LATEST, PRODUCTS]


def test_an_incomplete_questionnaire_is_corrected_and_not_saved(bank, double, clock):
    """只答了四题：不 POST，先纠正；补齐五题才保存。"""

    double.on("POST", ASSESSMENTS, data=support.risk_profile_payload(), status=201)
    model = support.ScriptModel(
        support.submit_risk({"q1": 1, "q2": 1, "q3": 1, "q4": 1}),
        support.submit_risk({"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 0}),
        support.finish("已保存。", ["wealth_assessment_1"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="1、1、1、1"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert "问卷答案缺少 q5" in model.prompts[1]
    assert len(double.calls) == 1
    assert double.calls[0].method == "POST"


# ---------------------------------------------------------------- 筛选


def test_liquidity_and_risk_filter(bank, double, clock):
    """两千元、30 天可能用、随时取、low 测评：只留 low 的灵活产品，其余都带原因落选。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low", score=2))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish("按您的条件推荐演示灵活理财。", ["p_demo_cash"]),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="两千块，30 天内可能要用，要能随时取"),
        session=session,
        pending=pending,
    )

    recommendation = _recommendation(response)
    assert [row["product_id"] for row in recommendation["candidates"]] == ["p_demo_cash"]
    assert recommendation["amount_minor"] == 200000
    assert recommendation["horizon_months"] == 1
    assert recommendation["liquidity"] == "flexible"

    not_recommended = _not_recommended(response)
    assert set(not_recommended) == {"p_demo_stable", "p_demo_growth", "p_demo_closed"}
    stable = not_recommended["p_demo_stable"]
    assert stable["suitable"] is False
    assert any("高于您的测评等级" in reason for reason in stable["reasons"])
    assert any("超过您的用钱时间" in reason for reason in stable["reasons"])
    assert any("不符合您随时可以赎回的要求" in reason for reason in stable["reasons"])


def test_sold_out_high_rate_is_excluded(bank, double, clock):
    """停售产品不因 9% 的模拟年化参数进入候选。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.set_slots(dict(RECOMMEND_SLOTS)),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish("演示停售产品已经停售，参数再高也不能推荐。", ["p_demo_cash"]),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    recommendation = _recommendation(response)
    assert "p_demo_closed" not in [row["product_id"] for row in recommendation["candidates"]]
    closed = _not_recommended(response)["p_demo_closed"]
    assert closed["expected_return"] == 0.09
    assert closed["suitable"] is False
    assert any("已停售" in reason for reason in closed["reasons"])


def test_candidates_are_at_most_three_and_ranked(bank, double, clock):
    """候选最多三个，按风险、期限、产品编号排序；排后面的仍然带原因出现在落选里。"""

    session = {}
    pending = None
    products = [
        support.product_payload(product_id="p_c", name="丙产品", duration_months=2),
        support.product_payload(product_id="p_a", name="甲产品", duration_months=1),
        support.product_payload(product_id="p_b", name="乙产品", duration_months=1),
        support.product_payload(product_id="p_d", name="丁产品", duration_months=0),
    ]
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    double.on("GET", PRODUCTS, data=products)
    model = support.ScriptModel(
        support.set_slots(
            {"plan_amount_minor": 200000, "horizon_months": 3, "liquidity": "flexible"}
        ),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish("按风险、期限和编号排序，前三个是丁、甲、乙。", ["p_d", "p_a", "p_b"]),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    recommendation = _recommendation(response)
    assert [row["product_id"] for row in recommendation["candidates"]] == ["p_d", "p_a", "p_b"]
    beyond = [row for row in recommendation["not_recommended"] if row["suitable"] is True]
    assert [row["product_id"] for row in beyond] == ["p_c"]
    assert any("候选最多三个" in reason for reason in beyond[0]["reasons"])


def test_recommend_needs_amount_horizon_and_liquidity(bank, double, clock):
    """条件不齐就不查产品：推荐节点回一条 needs_evidence（缺 slots），由模型追问缺的那几个。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    question = "为了给您推荐，还需要您补充：用钱时间（月）、流动性要求。"
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 200000}),
        support.call_read("wealth_risk_profile"),
        support.recommend(),
        support.ask_user(question, ["horizon_months", "liquidity"]),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="帮我推荐，我有两千块"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.pending is not None
    assert response.summary == question
    assert pending["missing"] == ("horizon_months", "liquidity")
    # 节点报的缺项事实：缺的是三个推荐条件里的两个，而且点了名。
    missing = _needs_evidence(model.prompts[3])
    assert missing["resource"] == "slots"
    assert missing["missing_slots"] == ["horizon_months", "liquidity"]
    assert "用钱时间（月）" in missing["reason"]
    assert "流动性要求" in missing["reason"]
    # 条件不齐就不查产品：这一轮只读了一次测评。
    assert [call.path for call in double.calls] == [LATEST]


def test_a_stuck_clarification_loop_is_stopped(bank, double, clock):
    """一直问同一件事、又没有任何新事实时，第 6 次追问不再发生，直接以 failed 收尾。

    这一轮一次银行调用都没有（没有新证据），槽位也没变，所以"连续追问"的计数一直在涨。
    """

    session = {}
    pending = None
    question = "推荐前需要先完成风险测评，您能接受多大的本金损失？"
    model = support.ScriptModel(*([support.ask_user(question)] * (MAX_CLARIFICATIONS + 1)))
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    for _ in range(MAX_CLARIFICATIONS):
        asked, session, pending = agent.handle(
            context,
            WealthRequest(kind="message", text="推荐点理财"),
            session=session,
            pending=pending,
        )
        assert asked.status is WealthStatus.WAITING_INPUT
        assert asked.summary == question

    stopped, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="推荐点理财"), session=session, pending=pending
    )

    assert stopped.status is WealthStatus.FAILED
    assert stopped.summary == CLARIFY_FAILURE_SUMMARY
    # 一次银行调用都没有：没有新证据，所以这一轮不算"有进展"。
    assert double.calls == []


def test_answering_the_questionnaire_keeps_it_alive(bank, double, clock):
    """用户每轮都在回答：澄清计数随"这一轮有进展"归零，五题问卷不会在第 5 题被截断。

    这就是"同一个问题不无限追问"这条边界的另一半：计数盯的是**卡住的**循环，不是问题的个数。
    """

    session = {}
    pending = None
    question = "请回答问卷第一题：您能接受多大的本金损失？"
    model = support.ScriptModel(*([support.ask_user(question)] * (MAX_CLARIFICATIONS + 2)))
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    asked, session, pending = agent.handle(
        context, WealthRequest(kind="message", text="推荐点理财"), session=session, pending=pending
    )
    assert asked.status is WealthStatus.WAITING_INPUT

    for _ in range(MAX_CLARIFICATIONS + 1):
        answered, session, pending = agent.handle(
            context,
            WealthRequest(kind="answer", pending_id=asked.pending.pending_id, text="1"),
            session=session,
            pending=pending,
        )
        # 每一次回答都是一次进展，所以追问预算一直重新计数，没有被截断。
        assert answered.status is WealthStatus.WAITING_INPUT
        assert answered.summary == question
        asked = answered

    assert double.calls == []


def test_no_match_is_explained_without_purchase(bank, double, clock):
    """没有候选：如实说明，不放宽风险凑数，银行资金写次数为 0。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 500, "horizon_months": 0, "liquidity": "flexible"}),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.recommend(),
        support.finish(
            "没有符合您条件的产品：5.00 元低于所有在售产品的起购额，也没有放宽风险等级。"
        ),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="5 块钱能买点什么？"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    assert _recommendation(response)["candidates"] == []
    assert "没有符合" in response.summary
    assert all(call.method == "GET" for call in double.calls)


# ---------------------------------------------------------------- 对比


def test_comparison_cannot_unlock_purchase(bank, double, clock):
    """对比可以客观列出不适配的产品，但结论仍是"不适合"，也不产生任何资金写。"""

    session = {}
    pending = None
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.set_slots(
            {"plan_amount_minor": 200000, "horizon_months": 12, "liquidity": "maturity_only"}
        ),
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.compare("p_demo_cash", "p_demo_growth"),
        support.finish(
            "演示灵活理财适合您；演示进取理财风险高于您的测评等级，不适合您，不能购买。",
            ["p_demo_cash", "p_demo_growth"],
        ),
    )

    response, session, pending = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="对比一下灵活和进取两只产品"),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.COMPLETED
    rows = response.facts["comparison"]["rows"]
    assert [row["product_id"] for row in rows] == ["p_demo_cash", "p_demo_growth"]
    assert [row["suitable"] for row in rows] == [True, False]

    growth = rows[1]
    assert growth["risk_level"] == "high"
    assert growth["duration_months"] == 12
    assert growth["min_amount_minor"] == 500000
    assert growth["redeem_rule"] == "maturity_only"
    assert growth["expected_return"] == 0.06
    assert any("高于您的测评等级" in reason for reason in growth["reasons"])
    # 对比只用本轮已经查到的事实：产品列表读一次就够，节点不逐只重查。
    assert all(call.method == "GET" for call in double.calls)
    assert [call.path for call in double.calls] == [LATEST, PRODUCTS]


def test_comparison_works_without_an_assessment_and_never_asks_for_one(bank, double, clock):
    """纯对比不强制测评：没有测评也能对比，但每行都标注"无法判断是否适合"，也不开问卷。"""

    double.on("GET", LATEST, data=None)
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    model = support.ScriptModel(
        support.call_read("wealth_risk_profile"),
        support.call_read("wealth_products"),
        support.compare("p_demo_cash", "p_demo_stable"),
        support.finish("两只产品的客观参数如上；是否适合您需要先完成风险测评。", ["p_demo_cash"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="对比一下这两只产品"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.pending is None
    rows = response.facts["comparison"]["rows"]
    assert [row["suitable"] for row in rows] == [False, False]
    assert all(any("没有有效的银行风险测评" in reason for reason in row["reasons"]) for row in rows)
    assert all(call.method == "GET" for call in double.calls)
    assert [call.path for call in double.calls] == [LATEST, PRODUCTS]


def test_compare_only_accepts_products_that_were_already_fetched(bank, double, clock):
    """对比的编号必须来自本轮已经查到的产品：没查到的会被点名，先查它再对比就成了。"""

    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on(
        "GET",
        GHOST_PRODUCT,
        data=support.product_payload(product_id="p_demo_ghost", name="演示幽灵理财"),
    )
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.compare("p_demo_cash", "p_demo_ghost"),
        support.call_read("wealth_product", {"product_id": "p_demo_ghost"}),
        support.compare("p_demo_cash", "p_demo_ghost"),
        support.finish(
            "演示灵活理财与演示幽灵理财的客观对比见上。", ["p_demo_cash", "p_demo_ghost"]
        ),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="对比第一个和这个幽灵产品"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    # 第一次对比：不报决策错误，而是回一条"缺证据"并点名那只没查到的产品。
    missing = _needs_evidence(model.prompts[2])
    assert missing["resource"] == "products"
    assert "p_demo_ghost" in missing["reason"]
    # 补查那只产品之后，第二次对比成功。
    assert [row["product_id"] for row in response.facts["comparison"]["rows"]] == [
        "p_demo_cash",
        "p_demo_ghost",
    ]
    assert [call.path for call in double.calls] == [PRODUCTS, GHOST_PRODUCT]


# ---------------------------------------------------------------- 决策词表


def test_the_dialogue_actions_are_the_nine_the_workflow_understands():
    """动作词表只有这九项：没有 purchase／redeem／confirm，也没有任何授权字段。

    任务9 在这张词表上加了第八项 `prepare_purchase`，任务10 又加了第九项 `prepare_redeem`
    （都只发起预览，资金写仍由运行时的受控节点负责），所以这里从八项改成九项；
    "没有任何一个动作可以直接写钱"这条性质不变。模型看到的工具表与它一一对应：七个只读工具加
    八个控制类工具，同样没有资金写。
    """

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
    assert {"purchase", "redeem", "confirm", "verify"}.isdisjoint(ACTIONS)
    assert QUESTION_IDS == ("q1", "q2", "q3", "q4", "q5")
    assert OPTIONS == (0, 1, 2)
    assert len(QUESTIONS) == 5

    # 工具表才是模型真正看到的那份词表：七个只读工具加八个控制类工具，同样没有资金写工具。
    table = tool_table(READ_TOOLS)
    assert set(table) == set(READ_TOOLS) | {
        "set_slots",
        "ask_user",
        "submit_risk",
        "recommend",
        "compare",
        "prepare_purchase",
        "prepare_redeem",
        "finish",
    }
    assert {"purchase", "redeem", "confirm", "verify"}.isdisjoint(table)


@pytest.mark.parametrize(
    "tool_call",
    [
        # 缺必填参数：submit_risk 没有 answers，compare 没有 product_ids，ask_user 没有问题。
        support.call("submit_risk"),
        support.call("submit_risk", {"answers": {}}),
        support.call("submit_risk", {"answers": []}),
        support.call("ask_user", {}),
        support.call("ask_user", {"missing": ["plan_amount_minor"]}),
        support.call("compare"),
        # 多带一个没有声明的字段：模型自己写的 risk_level 不算数（这一条按越门禁计）。
        support.call(
            "submit_risk",
            {"answers": {"q1": 0, "q2": 0, "q3": 0, "q4": 0, "q5": 0}, "risk_level": "high"},
        ),
        support.call("recommend", {"risk_level": "high"}),
        # 选项必须是 0／1／2 整数：布尔、字符串 "0" 和 3 都不行。
        support.call("submit_risk", {"answers": {"q1": True, "q2": 0, "q3": 0, "q4": 0, "q5": 0}}),
        support.call("submit_risk", {"answers": {"q1": 3, "q2": 0, "q3": 0, "q4": 0, "q5": 0}}),
        support.call("submit_risk", {"answers": {"q1": "0", "q2": 0, "q3": 0, "q4": 0, "q5": 0}}),
        # compare 的编号必须是两到三个不重复的字符串列表。
        support.call("compare", {"product_ids": "p_demo_cash"}),
        support.compare("p_demo_cash"),
        support.compare("p_demo_cash", "p_demo_cash"),
        support.compare("p_demo_cash", "p_demo_stable", "p_demo_growth", "p_x"),
    ],
)
def test_an_unusable_risk_or_compare_decision_is_a_decision_error(tool_call):
    """风险与对比的坏决策仍然是决策错误：缺必填参数、类型不对、编号个数不对、多带字段。

    工具表里那几种"越门禁"（不存在的工具、没声明的参数）抛的是 `GateViolation`，它是
    `DecisionError` 的子类，由 `test_safety.py` 单独分辨，这里只要求"不能用"。
    """

    with pytest.raises(DecisionError):
        parse_decision(*support.decision_input(tool_call), table=tool_table())


def test_a_question_outside_the_slot_vocabulary_is_a_valid_decision():
    """追问的问题不再受槽位词表限制：问卷的某一题也可以直接问。

    旧协议要求 `question` 对应槽位词表里的名字，所以"问问卷第一题"是一次决策错误；新协议里问题
    自由，`missing` 只回答"这次在补哪几个槽位"——不在补槽位就写空数组。**声明本身是必填的**
    （省略它，"已知信息不重复问"就无从机械检查），但空声明完全合法。
    """

    question = "请回答问卷第一题：您能接受多大的本金损失？"
    decision = parse_decision(
        *support.decision_input(support.ask_user(question, [])), table=tool_table()
    )

    assert decision.action == "ask_user"
    assert decision.question == question
    assert decision.missing == ()


def test_the_risk_decisions_round_trip_through_state():
    """三条新决策存进状态再读回来：形状不变，且没有授权字段。"""

    for tool_call in (
        support.submit_risk(ANSWERS),
        support.recommend(),
        support.compare("p_demo_cash", "p_demo_stable"),
    ):
        decision = parse_decision(*support.decision_input(tool_call), table=tool_table())
        restored = WealthDecision.from_dict(decision.as_dict())

        assert restored == decision
