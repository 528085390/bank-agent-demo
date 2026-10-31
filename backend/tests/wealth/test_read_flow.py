"""任务6：理财内部多步只读工作流。

三组断言：

1. **内部循环**：一次 `handle` 里"理解请求 → 结构化决策 → HTTP 只读工具 → 加入事实 → 继续决策
   → 回答"跑完；两次查询都在理财内部完成，不交回 Supervisor；发出去的银行请求全是 GET。
2. **门禁**：模型唯一的输出形状是一次工具调用（`{"tool": ..., "arguments": ...}`），工具表里只有
   七个只读方法和八个控制类动作；写动作、越权参数、凭空引用的编号被拒绝或纠正；预算（8 次模型
   调用、12 次节点推进、1 次纠正）是硬边界，不是提示词建议。
3. **对外契约**：status／summary／facts／business_ids／pending／error_code 与真实发生的事一致；
   银行不可用、未绑定客户号、模型不可用都如实失败，不返回"伪成功"。

真实 HTTP 版本在 `tests/wealth_integration/test_read_flow_live.py`，只有显式指定
`WEALTH_TEST_BANK_URL` 时才运行。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.integration.model_client import ModelClientError
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.modules.wealth import WEALTH
from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.application.runtime import PENDING_TTL
from app.modules.wealth.contracts.bank import (
    FLOW_ONLY_METHODS,
    READ_ONLY_METHODS,
    RUNTIME_ONLY_METHODS,
)
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.modules.wealth.infrastructure.tools import (
    READ_TOOLS,
    check_arguments,
    check_facts,
    public_handlers,
)
from app.modules.wealth.llm.decisions import DecisionError, GateViolation, parse_decision
from app.modules.wealth.llm.toolcalls import tool_table
from app.modules.wealth.workflow.nodes import MODEL_FAILURE_SUMMARY
from app.modules.wealth.workflow.state import MAX_MODEL_CALLS, MAX_STEPS
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ErrorCode, SideEffect, ToolStatus
from tests.support.scripted_model import StreamingModel
from tests.wealth import support

# 银行服务在离线测试里只会被问这两条路径。
PRODUCTS = "/products"
CASH_PRODUCT = "/products/p_demo_cash"

FLEXIBLE_QUESTION = "有哪些可以灵活赎回的产品？"


def _two_products() -> list[dict]:
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


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _bank_double() -> HttpWealthBank:
    """一个连着传输替身的正式适配器：工厂的协议检查要求它是 `WealthBank`。"""

    return HttpWealthBank(support.BankDouble().client())


# ---------------------------------------------------------------- 内部循环


def test_read_flow_calls_two_tools_then_answers_without_any_write(bank, double, clock):
    """计划任务6 的样例场景：两次只读查询都在理财内部完成，回答只引用返回过的产品。"""

    double.on("GET", PRODUCTS, data=_two_products())
    double.on("GET", CASH_PRODUCT, data=support.product_payload())
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("可以灵活赎回的是演示灵活理财，起购 100.00 元。", ["p_demo_cash"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.error_code is None
    assert response.summary == "可以灵活赎回的是演示灵活理财，起购 100.00 元。"
    assert model.calls == 3

    # 两次查询都是真实 HTTP 调用，方法全是 GET：只读工作流没有资金写请求。
    assert [call.method for call in double.calls] == ["GET", "GET"]
    assert [call.path for call in double.calls] == [PRODUCTS, CASH_PRODUCT]
    assert all(call.query["user_id"] == support.DEMO_CUSTOMER for call in double.calls)

    # 事实来自工具返回：两次查询的返回都留在 facts 里（同一份记录按 ID 去重）。
    products = response.facts["products"]
    assert {row["product_id"] for row in products} == {"p_demo_cash", "p_demo_stable"}
    assert response.business_ids == {"product_id": "p_demo_cash"}


def test_parallel_read_calls_in_one_turn_are_both_executed(bank, double, clock):
    """模型在一轮里同时要产品和账户：两个只读调用都执行，事实一起交给下一轮。"""

    double.on("GET", PRODUCTS, data=_two_products())
    double.on("GET", "/accounts", data=[support.account_payload()])
    model = StreamingModel(
        [
            [
                ("call", "wealth_products", json.dumps({"risk_level": None})),
                ("call", "wealth_accounts", "{}"),
            ],
            [
                (
                    "call",
                    "finish",
                    json.dumps({"text": "已查到产品和账户。", "cited_ids": []}),
                )
            ],
        ]
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="查一下产品和账户"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert [call.path for call in double.calls] == [PRODUCTS, "/accounts"]
    assert "products" in response.facts
    assert "accounts" in response.facts
    assert model.streams == 2


def test_the_first_reads_facts_reach_the_next_decision(bank, double, clock):
    """加入事实这一步是真的：第二轮提示词里有第一轮查到的产品。"""

    double.on("GET", PRODUCTS, data=_two_products())
    double.on("GET", CASH_PRODUCT, data=support.product_payload())
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("演示灵活理财可以灵活赎回。", ["p_demo_cash"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert "演示灵活理财" in model.prompts[1]
    assert "演示灵活理财" not in model.prompts[0]


# ---------------------------------------------------------------- 门禁


def test_a_write_action_is_refused_and_never_reaches_the_bank(bank, double, clock):
    """模型想"顺手申购"：工具表里没有这个工具，纠正一次后只能改口，银行一个请求都没收到。"""

    model = support.ScriptModel(
        support.call("purchase", {"product_id": "p_demo_cash"}),
        support.finish("我不能直接申购，需要先确认产品、金额和付款账户。"),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="帮我买一千块理财"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert model.calls == 2
    assert double.calls == []
    assert "上一次输出不能使用" in model.prompts[1]
    assert "工具不在词表里：purchase" in model.prompts[1]


def test_a_second_unusable_decision_fails_the_turn(bank, double, clock):
    """格式错误最多纠正 1 次；第二次仍然不可用就以 failed 收尾，不带着坏决策往下走。"""

    model = support.ScriptModel(support.call("finish", {}), support.call("finish", {}))

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.PROTOCOL
    assert model.calls == 2
    assert double.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "path", "data", "fact_key"),
    [
        ("wealth_products", {}, PRODUCTS, _two_products(), "products"),
        (
            "wealth_product",
            {"product_id": "p_demo_cash"},
            CASH_PRODUCT,
            support.product_payload(),
            "products",
        ),
        ("wealth_accounts", {}, "/accounts", [support.account_payload()], "accounts"),
        ("wealth_holdings", {}, "/holdings", [support.holding_payload()], "holdings"),
        (
            "wealth_holding",
            {"holding_id": "h_wealth_sample"},
            "/holdings/h_wealth_sample",
            support.holding_payload(),
            "holdings",
        ),
        (
            "wealth_risk_profile",
            {},
            "/wealth/risk-assessments/latest",
            support.risk_profile_payload(),
            "risk_profile",
        ),
        (
            "wealth_order",
            {"order_id": "order_wealth_sample"},
            "/wealth/orders/order_wealth_sample",
            support.order_payload(),
            "orders",
        ),
    ],
)
def test_every_read_tool_is_wired_to_its_endpoint_and_facts(
    bank, double, clock, tool, arguments, path, data, fact_key
):
    """七个工具逐一走通：打对端点、带对客户号、事实满足自己的 output_schema。"""

    double.on("GET", path, data=data)
    model = support.ScriptModel(support.call_read(tool, arguments), support.finish("已查到。"))

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="帮我查一下"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    call = double.only_call()
    assert call.method == "GET"
    assert call.path == path
    assert call.query["user_id"] == support.DEMO_CUSTOMER
    assert fact_key in response.facts
    assert check_facts(READ_TOOLS[tool], response.facts) is None


def test_holdings_facts_carry_the_product_name(bank, double, clock):
    double.on("GET", "/holdings", data=[support.holding_payload()])
    model = support.ScriptModel(support.call_read("wealth_holdings", {}), support.finish("已查到。"))

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="帮我查一下持仓"),
        session={},
        pending=None,
    )

    assert response.facts["holdings"][0]["product_name"] == "演示灵活理财"


def test_a_model_supplied_identity_cannot_reach_the_bank(bank, double, clock):
    """模型往 arguments 里塞 user_id／confirm／幂等键：被当作越门禁尝试拒绝，出网请求仍用绑定的客户号。"""

    double.on("GET", CASH_PRODUCT, data=support.product_payload())
    model = support.ScriptModel(
        support.call_read(
            "wealth_product",
            {
                "product_id": "p_demo_cash",
                "user_id": "u_li",
                "confirm": True,
                "idempotent_key": "idem-1",
            },
        ),
        support.call_read("wealth_product", {"product_id": "p_demo_cash"}),
        support.finish("演示灵活理财可以灵活赎回。", ["p_demo_cash"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert double.only_call().query == {"user_id": support.DEMO_CUSTOMER}
    assert "u_li" not in json.dumps([call.query for call in double.calls])
    # 纠正话术只说"哪几个字段没有声明"，不回报取值（`u_li` 只作为模型自己的上一次输出被原样
    # 还给它，不是这一层报出来的）。
    assert (
        "wealth_product 的参数里有它没有声明的字段：confirm、idempotent_key、user_id"
        in (model.prompts[1])
    )


def test_an_answer_citing_an_unqueried_record_is_corrected(bank, double, clock):
    """模型凭空引用一个没查过的产品号：被纠正，改用工具返回过的编号。"""

    double.on("GET", PRODUCTS, data=_two_products())
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish("推荐演示高收益理财，年化 9%。", ["p_demo_ghost"]),
        support.finish("可以灵活赎回的是演示灵活理财。", ["p_demo_cash"]),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.summary == "可以灵活赎回的是演示灵活理财。"
    assert "p_demo_ghost" in model.prompts[2]
    assert "引用了工具没有返回过的编号" in model.prompts[2]
    assert model.calls == 3


def test_the_step_budget_bounds_the_loop(bank, double, clock):
    """模型一直要求查询：节点推进到上限就收尾，模型调用与银行调用都被计数约束住。"""

    double.on("GET", PRODUCTS, data=_two_products())
    model = support.ScriptModel(*[support.call_read("wealth_products")] * 20)

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INTERNAL
    assert "上限" in response.summary
    assert model.calls == 6
    assert model.calls <= MAX_MODEL_CALLS
    assert len(double.calls) == 5
    assert len(double.calls) <= MAX_STEPS
    # 上限内的查询结果仍然如实带回去，而不是被丢掉。
    assert response.facts["products"][0]["product_id"] == "p_demo_cash"


def test_the_model_call_budget_bounds_the_loop(bank, double, clock):
    """每次决策都要纠正一次：模型调用次数先到上限，之后的节点不再调用模型。"""

    double.on("GET", PRODUCTS, data=_two_products())
    replies = [
        reply
        for _ in range(4)
        for reply in (
            support.call("purchase", {"product_id": "p_demo_cash"}),
            support.call_read("wealth_products"),
        )
    ]
    model = support.ScriptModel(*replies)

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert model.calls == MAX_MODEL_CALLS
    assert len(double.calls) == 4


# ---------------------------------------------------------------- 对外契约


def test_a_bank_failure_is_failed_not_answered(bank, double, clock):
    """银行不可用：即使模型给了回答，也返回 failed 与银行的错误码，不把故障说成查询结果。"""

    double.on_failure("GET", PRODUCTS, httpx.ConnectError("refused"))
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish("暂时无法查询产品列表，请稍后再试。"),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert response.facts == {}
    assert response.summary == "暂时无法查询产品列表，请稍后再试。"
    # 失败事实进了下一轮提示词：模型据此改口，而不是凭空给产品。
    assert "银行服务暂时无法连接，请稍后重试。" in model.prompts[1]


def test_an_unbound_customer_fails_before_any_http_request(bank, double, clock):
    """没有绑定银行客户号：不发 HTTP，不读银行默认客户的数据。"""

    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish("无法查询，请先绑定银行客户号。"),
    )

    response, _, _ = _agent(model, bank, clock).handle(
        support.unbound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.NO_CUSTOMER_BINDING
    assert double.calls == []


@pytest.mark.parametrize("kind", ["confirm", "verify"])
def test_confirm_and_verify_have_nothing_to_act_on(bank, double, clock, kind):
    """任务6 没有可确认或可验证的操作：明确拒绝，模型与银行都不被调用，验证码也不外泄。"""

    model = support.ScriptModel()

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind=kind, pending_id="wp_missing", verification_code="246810"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert model.calls == 0
    assert double.calls == []
    assert "246810" not in repr(response)


def test_cancel_stops_without_claiming_anything(bank, double, clock):
    """取消只终止未提交的工作：不声称撤销任何订单，也不调用模型或银行。"""

    model = support.ScriptModel()

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(), WealthRequest(kind="cancel"), session={}, pending=None
    )

    assert response.status is WealthStatus.CANCELLED
    assert model.calls == 0
    assert double.calls == []


@pytest.mark.parametrize(
    ("error", "calls"),
    [
        (ModelClientError("LLM_TIMEOUT", retryable=True), 2),
        (ModelClientError("LLM_AUTH", retryable=False), 1),
    ],
)
def test_a_model_failure_is_reported_as_failed(bank, double, clock, error, calls):
    """模型边界失败：瞬时错误按基座边界重试一次，仍然失败就如实返回失败。"""

    model = support.ScriptModel(*[error] * calls)

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text=FLEXIBLE_QUESTION),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert response.summary == MODEL_FAILURE_SUMMARY
    assert model.calls == calls
    assert double.calls == []


def test_an_empty_message_is_refused(bank, double, clock):
    model = support.ScriptModel()

    response, _, _ = _agent(model, bank, clock).handle(
        support.bound_context(), WealthRequest(kind="message", text="   "), session={}, pending=None
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.VALIDATION
    assert model.calls == 0


# ---------------------------------------------------------------- 待办


def test_an_asked_question_becomes_a_pending_the_same_session_can_answer(bank, double, clock):
    """追问返回 waiting_input 与待办；同一会话按待办回答后继续走工作流。"""

    session = {}
    pending = None
    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
        support.finish("那建议先看可灵活赎回的产品。"),
    )
    agent = _agent(model, bank, clock)

    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.pending is not None
    assert asked.pending.prompt == "请问您计划什么时候用这笔钱？"
    assert asked.pending.expires_at == (clock.now() + PENDING_TTL).isoformat()

    answered, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="answer", text="三个月内要用", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert answered.status is WealthStatus.COMPLETED
    assert answered.summary == "那建议先看可灵活赎回的产品。"
    # 第二次处理看得到被回答的问题与用户的回答。
    assert "请问您计划什么时候用这笔钱？" in model.prompts[1]
    assert "三个月内要用" in model.prompts[1]


@pytest.mark.parametrize(
    "other",
    [
        support.bound_context(user_id="user_0002", session_id="sess_wealth_1"),
        support.bound_context(user_id="user_0001", session_id="sess_other"),
    ],
)
def test_a_pending_cannot_be_answered_by_another_user_or_session(bank, double, clock, other):
    session = {}
    pending = None
    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"])
    )
    agent = _agent(model, bank, clock)
    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    response, session, pending = agent.handle(
        other,
        WealthRequest(kind="answer", text="三个月内要用", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert model.calls == 1


def test_an_expired_pending_cannot_be_answered(bank, double, clock):
    session = {}
    pending = None
    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"])
    )
    agent = _agent(model, bank, clock)
    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session=session,
        pending=pending,
    )

    clock.advance(PENDING_TTL)

    response, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="answer", text="三个月内要用", pending_id=asked.pending.pending_id),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert model.calls == 1


# ---------------------------------------------------------------- 工具与装配


def test_the_read_tools_are_the_seven_bank_read_methods():
    """白名单与第三章的七个只读方法一一对应；没有资金写、没有保存测评。"""

    assert len(READ_TOOLS) == 7
    assert set(READ_TOOLS) == {
        "wealth_products",
        "wealth_product",
        "wealth_accounts",
        "wealth_holdings",
        "wealth_holding",
        "wealth_risk_profile",
        "wealth_order",
    }
    assert {tool.method for tool in READ_TOOLS.values()} == READ_ONLY_METHODS
    assert {tool.method for tool in READ_TOOLS.values()}.isdisjoint(
        RUNTIME_ONLY_METHODS | FLOW_ONLY_METHODS
    )
    # 每个工具都显式声明 READ，并且事实有自己的 schema。
    assert all(tool.spec.side_effect is SideEffect.READ for tool in READ_TOOLS.values())
    assert all(tool.spec.output_schema is not None for tool in READ_TOOLS.values())


def test_the_read_tools_are_public_tools_the_shared_gateway_can_carry(bank, double):
    """同一批工具走公共注册表／网关／适配器：参数校验、身份注入、结果信封都由公共层负责。"""

    module = ModuleSpec(
        name="wealth",
        declaration=WEALTH,
        tools={name: tool.spec for name, tool in READ_TOOLS.items()},
        handlers=dict(public_handlers()),
    )
    registry = build_registry([module])
    handle = ToolGateway(registry.catalog, BankToolAdapter(registry, double.client())).handle(
        "wealth", support.bound_context()
    )
    double.on("GET", PRODUCTS, data=_two_products())

    result = handle.call({"tool": "wealth_products", "arguments": {}, "call_id": "c1"})

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data is not None
    assert result.data["facts"]["products"][0]["product_id"] == "p_demo_cash"
    assert double.only_call().query == {"user_id": support.DEMO_CUSTOMER}


def test_the_argument_schema_rejects_unknown_fields_without_repeating_values():
    tool = READ_TOOLS["wealth_product"]

    assert check_arguments(tool, {"product_id": "p_demo_cash"}) is None

    complaint = check_arguments(tool, {"product_id": "p_demo_cash", "user_id": "u_li"})

    assert complaint == "argument (root): failed additionalProperties"
    assert "u_li" not in complaint


def test_the_model_cannot_filter_holdings_by_an_invented_status():
    """对话162：模型自己编了 `status="null"`，银行按字面值过滤后返回空集，被讲成"持仓没了"。

    持仓查询不给模型任何过滤参数——它无从知道银行线的取值（holding／redeemed），编出来的
    哨兵值（null／all／active）只会变成一个匹配不到任何行的过滤条件。这类值在网关就被拒绝，
    不会变成一次查询。
    """

    tool = READ_TOOLS["wealth_holdings"]

    assert check_arguments(tool, {}) is None

    for invented in ("null", "all", "active", "holding"):
        assert (
            check_arguments(tool, {"status": invented})
            == "argument (root): failed additionalProperties"
        )


def test_the_model_cannot_filter_products_by_a_free_text_type():
    """产品类型同理：银行线值是 money_fund／fixed_term，模型只会填中文，落进同一个静默空集。

    风险等级留在表里，因为它有 enum，模型不可能填出一个银行不认识的取值。
    """

    tool = READ_TOOLS["wealth_products"]

    assert check_arguments(tool, {}) is None
    assert check_arguments(tool, {"risk_level": "low"}) is None
    assert (
        check_arguments(tool, {"product_type": "货币基金"})
        == "argument (root): failed additionalProperties"
    )


def test_no_read_tool_takes_a_free_text_filter():
    """整个只读工具面：字符串参数只能是"必须来自上次返回的资源编号"，或取值域封闭的 enum。

    自由文本参数（既没有 enum、又不要求来自返回）就是对话162 的根因——模型只能猜一个取值，
    银行按字面相等过滤后返回空集，和"用户真的没有数据"不可区分。这条断言把七个只读工具
    一起锁住，以后新增工具也不会重新打开这个口子。
    """

    for name, tool in READ_TOOLS.items():
        parameters = tool.spec.parameters
        required = set(parameters["required"])
        for field, schema in parameters["properties"].items():
            if schema.get("type") != "string":
                continue
            assert "enum" in schema or field in required, (
                f"{name}.{field} 是自由文本筛选参数：模型只能猜一个值，猜错就静默返回空集"
            )


def test_the_facts_schema_is_enforced():
    tool = READ_TOOLS["wealth_products"]

    assert check_facts(tool, {"products": []}) is None

    complaint = check_facts(tool, {"accounts": []})

    assert complaint is not None
    assert complaint.startswith("facts ")


def test_the_factory_only_assembles_the_three_dependencies():
    """工厂只装配依赖：不建共享 Runtime、不连平台数据库、缺依赖直接报错。"""

    clock = support.TestClock()
    script = support.ScriptModel()

    with pytest.raises(TypeError):
        build_wealth_agent(script, object(), clock)
    with pytest.raises(TypeError):
        build_wealth_agent(object(), _bank_double(), clock)
    with pytest.raises(TypeError):
        build_wealth_agent(script, _bank_double(), object())

    agent = build_wealth_agent(script, _bank_double(), clock)

    # 一个模型的引用、一个银行适配器、一个时钟、工具表、图工厂、会话表、待办表，以及按用户记的
    # 三笔账：本进程内被锁定写操作的用户、连续技术失败次数、累计绕过门禁次数（任务9／11）。
    # 没有别的状态，尤其没有检查点或数据库会话。
    assert set(vars(agent)) == {
        "_model",
        "_bank",
        "_clock",
        "_tools",
        "_graph_factory",
        "_locked_users",
        "_technical_failures",
        "_bypass_events",
    }


# ---------------------------------------------------------------- 决策词表


def test_only_the_dialogue_actions_and_the_read_whitelist_are_expressible():
    """工具表就是词表：七个只读方法加八个控制类动作，资金写与授权动作根本不在表里。"""

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
    # 没有下单、没有确认、没有验证码：模型写不出这些工具名，解析层也就没有可放行的东西。
    assert {"purchase", "redeem", "confirm", "verify"}.isdisjoint(table)

    read = support.call_read("wealth_products")
    assert parse_decision(*support.decision_input(read), table=table).action == "call_read"
    slots = support.set_slots({"amount_minor": 200000})
    assert parse_decision(*support.decision_input(slots), table=table).action == "set_slots"
    ask = support.ask_user("请问金额是多少？", ["amount_minor"])
    assert parse_decision(*support.decision_input(ask), table=table).action == "ask_user"
    done = support.finish("可以。")
    assert parse_decision(*support.decision_input(done), table=table).action == "finish"

    # 资金写与授权动作不是"格式错误"，而是一次越门禁的尝试：两种写法都写不出这张表。
    for hostile in (
        support.call("purchase", {"product_id": "p_demo_cash"}),
        support.call("wealth.redeem", {}),
    ):
        with pytest.raises(GateViolation):
            parse_decision(*support.decision_input(hostile), table=table)


@pytest.mark.parametrize(
    "tool_call",
    [
        # 资金写与授权动作：工具表里没有这些名字。
        support.call("purchase", {"product_id": "p_demo_cash"}),
        support.call("confirm", {}),
        support.call("wealth.redeem", {}),
        support.call("wealth.purchase", {"product_id": "p_demo_cash"}),
        # 缺必填字段、错形状。
        support.call("ask_user", {}),
        support.call("finish", {}),
        support.call("wealth_products", {"product_id": "p_demo_cash", "extra": True}),
    ],
)
def test_an_unusable_decision_is_a_decision_error(tool_call):
    with pytest.raises(DecisionError):
        parse_decision(*support.decision_input(tool_call), table=tool_table(READ_TOOLS))
