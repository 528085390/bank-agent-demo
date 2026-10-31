"""理财 Agent 的契约与正式 HTTP 适配器。

离线用例覆盖四件事，都是“替身不能替代的那部分”：

1. 契约本身：请求／响应／意图的字段集合，授权字段根本不存在，模型无法表达。新交互下确认是
   `kind=confirm`（直接提交）、核实是 `kind=reconcile`（只读），旧验证码入口 `kind=verify`
   还在词表里但已停用。
2. 适配器对银行 HTTP 的映射：method、path、query、body、headers 由真实 `BankClient`
   组装，断言的是真正发出去的东西；失败不变成伪成功，写请求不重发。
3. 适配器不持状态：每一次查询都是一次银行调用。
4. 写通道的**能力声明**：`write_authorization()` 的取值决定这次写要不要带验证码、确认框要不要
   向用户要验证码。本仓库的 `HttpWealthBank` 回答 `CLICK_AND_CODE`（银行的写门禁要求验证码），
   所以它的值在这里被钉住，而不是留给运行时去假设。

真实 HTTP 契约测试在 `backend/tests/wealth_integration/test_bank_contract.py`，只有显式指定
`WEALTH_TEST_BANK_URL` 时才运行。
"""

from __future__ import annotations

from dataclasses import fields

import httpx
import pytest

from app.modules.wealth.contracts.bank import (
    CONFIRMATION_TRANSPORT_UNSUPPORTED,
    FLOW_ONLY_METHODS,
    PURCHASE_OPERATION,
    READ_ONLY_METHODS,
    REDEEM_OPERATION,
    RUNTIME_ONLY_METHODS,
    RUNTIME_OWNED_FIELDS,
    Account,
    Holding,
    Product,
    ProductFilters,
    PurchaseIntent,
    RedeemIntent,
    RiskProfile,
    WealthBank,
    WealthBankError,
    WriteAuthorization,
)
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import Pending, WealthResponse, WealthStatus
from app.tools.protocol import ErrorCode
from tests.wealth import support

# ---------------------------------------------------------------------- 契约


def test_wealth_request_kinds_are_the_six_the_contract_names():
    assert {kind.value for kind in WealthRequestKind} == {
        "message",
        "answer",
        "confirm",
        "reconcile",
        "verify",
        "cancel",
    }


def test_a_wealth_request_rejects_authorisation_fields():
    """授权不是一个字段：确认是 kind=confirm、核实是 kind=reconcile，两者都只带 pending_id，
    没有可伪造的标志位。"""

    with pytest.raises(TypeError):
        WealthRequest(kind="message", text="看看理财", user_id="u_li")
    with pytest.raises(TypeError):
        WealthRequest(kind="message", text="看看理财", confirmed=True)
    with pytest.raises(TypeError):
        WealthRequest(kind="message", text="看看理财", mfa_code="123456")
    with pytest.raises(TypeError):
        WealthRequest(kind="message", text="看看理财", idempotency_key="k1")


def test_a_request_accepts_a_kind_by_name():
    request = WealthRequest(kind="reconcile", pending_id="p1")

    assert request.kind is WealthRequestKind.RECONCILE


def test_the_verification_code_never_reaches_the_model_input():
    """验证码是秘密：即使那条已停用的入口还带着它，它也不进模型上下文，也不进日志。

    点击确认取代了"确认 + 输入验证码"，所以 verify 这条入口已经停用（运行时明确拒绝）；但
    `verification_code` 这个字段仍然是一个**秘密**：停用的入口也不允许把用户输入的验证码
    漏进模型上下文。
    """

    request = WealthRequest(kind="verify", pending_id="p1", verification_code="246810")

    assert request.model_input() == {"kind": "verify", "text": ""}
    assert "246810" not in str(request.model_input())


def test_response_statuses_are_the_seven_the_contract_names():
    """词表还是七项，但 `waiting_verification` 已经不再由运行时产生：留着它只是为了能**点名**
    那条已停用的旧入口，而不是说还有一个等验证码的状态。确认即提交，所以只有
    `waiting_confirmation` 这一种"等用户动作"的写状态。
    """

    assert {status.value for status in WealthStatus} == {
        "completed",
        "waiting_input",
        "waiting_confirmation",
        "waiting_verification",
        "cancelled",
        "failed",
        "unknown",
    }


def test_a_waiting_response_must_carry_a_pending():
    """没有 pending_id 的 waiting 响应，客户端无法继续，属于契约错误而不是业务失败。"""

    with pytest.raises(ValueError, match="pending"):
        WealthResponse(status="waiting_confirmation", summary="请确认")

    response = WealthResponse(
        status="waiting_confirmation",
        summary="请确认",
        pending=Pending(pending_id="p1", prompt="确认申购 2000 元？"),
    )
    assert response.pending is not None
    assert response.pending.pending_id == "p1"


def test_a_response_rejects_authorisation_fields():
    with pytest.raises(TypeError):
        WealthResponse(status="completed", summary="ok", confirmed=True)


def test_the_protocol_partitions_the_model_callable_and_runtime_only_methods():
    """七项只读可由模型选择；保存测评走流程；申购／赎回／按幂等键核实只能由运行时调用。

    `write_authorization` 不属于这三类数据方法：它是**能力声明**（这家银行的写通道要什么才算
    授权），既不读也不写银行，更不能被模型选择，所以单独钉住。
    """

    assert READ_ONLY_METHODS == {
        "list_products",
        "get_product",
        "list_accounts",
        "list_holdings",
        "get_holding",
        "get_risk_profile",
        "get_order",
    }
    assert RUNTIME_ONLY_METHODS == {"purchase", "redeem", "find_order_by_key"}
    assert FLOW_ONLY_METHODS == {"submit_risk_assessment"}

    capability = {"write_authorization"}
    # 能力声明与三类数据方法互不重叠：它一旦混进只读集合，就等于让模型能"选择"银行的能力。
    assert capability.isdisjoint(READ_ONLY_METHODS | RUNTIME_ONLY_METHODS | FLOW_ONLY_METHODS)

    declared = {
        name
        for name, value in vars(WealthBank).items()
        if callable(value) and not name.startswith("_")
    }
    assert READ_ONLY_METHODS | RUNTIME_ONLY_METHODS | FLOW_ONLY_METHODS | capability == declared


def test_the_http_adapter_implements_the_protocol(bank):
    assert isinstance(bank, WealthBank)


def test_the_shipped_adapter_answers_click_and_code(bank):
    """本仓库的银行写通道要求验证码，所以适配器如实回答 `CLICK_AND_CODE`——这是它的**取值**。

    运行时据此让确认框向用户要验证码，并在提交时带上用户输入的那个值。它**不是** `CLICK`
    （那等于声称"银行已经支持无验证码的点击确认"，需要一个真的改过的银行后端，不是改一个枚举
    值），也不是 `UNSUPPORTED`（那条会走集成阻塞分支，而这笔交易其实是能办成的）。
    """

    assert bank.write_authorization(PURCHASE_OPERATION, 200000) is WriteAuthorization.CLICK_AND_CODE
    assert bank.write_authorization(REDEEM_OPERATION, 200000) is WriteAuthorization.CLICK_AND_CODE
    assert CONFIRMATION_TRANSPORT_UNSUPPORTED == "confirmation_transport_unsupported"


# ---------------------------------------------------------------------- 身份


@pytest.mark.parametrize("call", ["list_products", "get_order", "purchase", "find_order_by_key"])
def test_missing_customer_binding_sends_no_request(bank, double, call):
    """未绑定银行客户时不发 HTTP：省略 user_id 会读到银行默认客户的账户。"""

    context = support.unbound_context()

    with pytest.raises(WealthBankError) as caught:
        if call == "list_products":
            bank.list_products(context)
        elif call == "get_order":
            bank.get_order(context, "order_wealth_sample")
        elif call == "purchase":
            bank.purchase(context, _purchase_intent(), "246810", "idem-1")
        else:
            bank.find_order_by_key(context, PURCHASE_OPERATION, "idem-1")

    assert caught.value.code == ErrorCode.NO_CUSTOMER_BINDING
    assert caught.value.retryable is False
    assert double.calls == []


def test_http_payload_uses_trusted_customer(bank, double):
    """客户身份只来自 context 绑定：读走 query，写走 body，两处都是绑定的那个客户。"""

    double.on("GET", "/holdings", data=[support.holding_payload()])
    double.on("POST", "/products/purchase", data=support.purchase_receipt_payload(), status=201)

    bank.list_holdings(support.bound_context(support.DEMO_CUSTOMER))
    bank.purchase(
        support.bound_context(support.DEMO_CUSTOMER), _purchase_intent(), "246810", "idem-1"
    )

    read, write = double.calls
    assert read.query["user_id"] == support.DEMO_CUSTOMER
    assert write.body is not None
    assert write.body["user_id"] == support.DEMO_CUSTOMER


def test_an_intent_cannot_carry_identity_or_authorisation():
    """意图只有业务槽位：伪造的用户、确认标志、验证码和幂等键连表达都表达不出来。"""

    with pytest.raises(TypeError):
        PurchaseIntent(
            product_id="p_demo_cash",
            from_account_id="a_wealth_main",
            amount_minor=200000,
            user_id="u_li",
        )
    with pytest.raises(TypeError):
        PurchaseIntent(
            product_id="p_demo_cash",
            from_account_id="a_wealth_main",
            amount_minor=200000,
            confirm=True,
        )
    with pytest.raises(TypeError):
        RedeemIntent(
            holding_id="h_wealth_sample",
            amount_minor=50000,
            to_account_id="a_wealth_main",
            mfa_code="246810",
        )

    for intent_type in (PurchaseIntent, RedeemIntent):
        assert RUNTIME_OWNED_FIELDS.isdisjoint(field.name for field in fields(intent_type))

    assert {"user_id", "confirm", "mfa_code", "idempotent_key"} <= RUNTIME_OWNED_FIELDS


def test_a_redeem_has_to_name_the_receiving_account():
    """到账账户是用户确认过的事实，不接受服务端缺省账户。"""

    with pytest.raises(TypeError):
        RedeemIntent(holding_id="h_wealth_sample", amount_minor=50000)


# ---------------------------------------------------------------------- 资金写请求


def test_purchase_has_confirmation_code_and_key(bank, double):
    """确认、验证码和幂等键只由运行时传入，模型给的业务参数覆盖不了它们。"""

    double.on("POST", "/products/purchase", data=support.purchase_receipt_payload(), status=201)

    bank.purchase(support.bound_context(), _purchase_intent(), "246810", "idem-1")

    call = double.only_call()
    assert call.method == "POST"
    assert call.path == "/products/purchase"
    assert call.body == {
        "product_id": "p_demo_cash",
        "from_account_id": "a_wealth_main",
        "amount_minor": 200000,
        "confirm": True,
        "mfa_code": "246810",
        "idempotent_key": "idem-1",
        "user_id": support.DEMO_CUSTOMER,
    }
    assert call.idempotency_key == "idem-1"


def test_purchase_omits_an_unspecified_term(bank, double):
    """产品期限由银行按产品定义决定：不传就是不缩短，而不是传一个空值。"""

    double.on("POST", "/products/purchase", data=support.purchase_receipt_payload(), status=201)
    intent = PurchaseIntent(
        product_id="p_demo_stable", from_account_id="a_wealth_main", amount_minor=100000
    )

    bank.purchase(support.bound_context(), intent, "246810", "idem-1")

    assert intent.term_months is None
    assert double.only_call().body is not None
    assert "term_months" not in double.only_call().body


def test_redeem_sends_holding_amount_and_account(bank, double):
    double.on("POST", "/products/redeem", data=support.redemption_receipt_payload())

    receipt = bank.redeem(
        support.bound_context(),
        RedeemIntent(
            holding_id="h_wealth_sample", amount_minor=50000, to_account_id="a_wealth_main"
        ),
        "246810",
        "idem-2",
    )

    call = double.only_call()
    assert call.path == "/products/redeem"
    assert call.body == {
        "holding_id": "h_wealth_sample",
        "amount_minor": 50000,
        "to_account_id": "a_wealth_main",
        "confirm": True,
        "mfa_code": "246810",
        "idempotent_key": "idem-2",
        "user_id": support.DEMO_CUSTOMER,
    }
    assert receipt.order_id == "order_redeem_1"
    assert receipt.transaction_id == "txn_redeem_1"


@pytest.mark.parametrize("key", ["", None])
def test_a_write_without_an_idempotency_key_is_not_sent(bank, double, key):
    with pytest.raises(WealthBankError) as caught:
        bank.purchase(support.bound_context(), _purchase_intent(), "246810", key)

    assert caught.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    assert double.calls == []


@pytest.mark.parametrize("code", ["", "   ", None])
def test_a_write_without_a_verification_code_is_never_sent(bank, double, code):
    """本适配器**要求**验证码：空验证码在这里就失败关闭，不发一次注定被银行拒绝的写请求。

    `purchase`／`redeem` 现在收 `str | None`，但"可以不传"只对声明支持点击确认的适配器成立
    （见下一条）；`HttpWealthBank` 回答 False，所以对它来说缺验证码仍然是拒绝，而不是放行。
    银行侧的验证失败计数是有阈值的，所以这里不发出去。
    """

    with pytest.raises(WealthBankError) as caught:
        bank.purchase(support.bound_context(), _purchase_intent(), code, "idem-1")

    assert caught.value.code == ErrorCode.VALIDATION
    assert double.calls == []


def test_an_adapter_declaring_click_confirmation_sends_no_verification_code(double):
    """声明支持点击确认的适配器不带 `mfa_code`：一次可信点击本身就是授权。

    它同时说明"缺验证码被拒"不是 `purchase` 的形状要求，而是**这一侧能力声明**的结果：
    同一个 `purchase`，换一个回答 True 的适配器就不发验证码字段。
    """

    double.on("POST", "/products/purchase", data=support.purchase_receipt_payload(), status=201)

    receipt = support.ClickConfirmationBank(double.client()).purchase(
        support.bound_context(), _purchase_intent(), None, "idem-1"
    )

    call = double.only_call()
    assert call.body is not None
    assert "mfa_code" not in call.body
    assert call.body["confirm"] is True
    assert receipt.order_id == "order_purchase_1"


def test_a_write_is_never_resent_and_its_outcome_stays_unknown(bank, double):
    """超时的写请求只能由运行时按原幂等键核实：这一层不重发，也不说它“可以重试”。"""

    double.on_failure("POST", "/products/purchase", httpx.ReadTimeout("slow"))

    with pytest.raises(WealthBankError) as caught:
        bank.purchase(support.bound_context(), _purchase_intent(), "246810", "idem-1")

    assert len(double.calls) == 1
    assert caught.value.outcome_unknown is True
    assert caught.value.retryable is False
    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE


def test_a_refused_connection_is_not_an_unknown_outcome(bank, double):
    """连接被拒说明请求根本没到银行，不必去核实订单。"""

    double.on_failure("POST", "/products/purchase", httpx.ConnectError("refused"))

    with pytest.raises(WealthBankError) as caught:
        bank.purchase(support.bound_context(), _purchase_intent(), "246810", "idem-1")

    assert caught.value.outcome_unknown is False
    assert caught.value.retryable is False


def test_a_business_rejection_keeps_its_own_code(bank, double):
    double.on("POST", "/products/purchase", code=1006, status=409, data=None)

    with pytest.raises(WealthBankError) as caught:
        bank.purchase(support.bound_context(), _purchase_intent(), "246810", "idem-1")

    assert caught.value.code == ErrorCode.INSUFFICIENT_BALANCE
    assert caught.value.retryable is False
    assert caught.value.outcome_unknown is False


# ---------------------------------------------------------------------- 只读查询


def test_no_memory_success_on_bank_failure(bank, double):
    """银行不可用时返回错误，不返回任何看起来像持仓的东西。"""

    double.on_failure("GET", "/holdings", httpx.ConnectError("refused"))

    holdings: list[Holding] | None = None
    with pytest.raises(WealthBankError) as caught:
        holdings = bank.list_holdings(support.bound_context())

    assert holdings is None
    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert len(double.calls) == 1


def test_a_read_failure_keeps_the_banks_retryability(bank, double):
    """读请求可以重试，但重试由运行时决定：适配器只如实报告这一次的失败。"""

    double.on_failure("GET", "/products", httpx.ReadTimeout("slow"))

    with pytest.raises(WealthBankError) as caught:
        bank.list_products(support.bound_context())

    assert caught.value.retryable is True
    assert caught.value.outcome_unknown is False
    assert len(double.calls) == 1


def test_every_read_goes_to_the_bank(bank, double):
    """适配器不维护账户／持仓字典：第二次查询也是第二次 HTTP 调用。"""

    double.on("GET", "/holdings", data=[support.holding_payload()])

    first = bank.list_holdings(support.bound_context())
    second = bank.list_holdings(support.bound_context())

    assert len(double.calls) == 2
    assert first == second


def test_a_wrong_shaped_list_is_a_protocol_error_not_an_empty_list(bank, double):
    """分页形状被误读成空列表，会让 Agent 说“您没有持仓”。"""

    double.on("GET", "/holdings", data={"items": [support.holding_payload()]})

    with pytest.raises(WealthBankError) as caught:
        bank.list_holdings(support.bound_context())

    assert caught.value.code == ErrorCode.PROTOCOL


def test_product_query_carries_the_filters_and_the_customer(bank, double):
    double.on("GET", "/products", data=[support.product_payload()])

    bank.list_products(support.bound_context(), ProductFilters(risk_level="low"))

    call = double.only_call()
    assert call.method == "GET"
    assert call.path == "/products"
    assert call.query == {"risk_level": "low", "user_id": support.DEMO_CUSTOMER}
    assert call.body is None


def test_product_fields_map_from_the_bank_payload(bank, double):
    payload = support.product_payload(
        product_id="p_demo_stable",
        name="演示稳健理财",
        min_amount_minor=100000,
        duration_months=3,
        risk_level="medium",
        redeem_rule="maturity_only",
        expected_return=0.035,
    )
    double.on("GET", "/products/p_demo_stable", data=payload)

    product = bank.get_product(support.bound_context(), "p_demo_stable")

    assert product == Product(
        product_id="p_demo_stable",
        name="演示稳健理财",
        product_type="money_fund",
        min_amount_minor=100000,
        max_amount_minor=100000000,
        expected_return=0.035,
        risk_level="medium",
        duration_months=3,
        redeem_rule="maturity_only",
        status="available",
    )
    assert double.only_call().path == "/products/p_demo_stable"


def test_account_fields_map_from_the_bank_payload(bank, double):
    double.on("GET", "/accounts", data=[support.account_payload()])

    account = bank.list_accounts(support.bound_context())[0]

    assert account == Account(
        account_id="a_wealth_main",
        user_id=support.DEMO_CUSTOMER,
        account_type="current",
        balance_minor=2000000,
        available_minor=2000000,
        currency="CNY",
        status="active",
    )


def test_holding_fields_map_from_the_bank_payload(bank, double):
    double.on("GET", "/holdings/h_wealth_sample", data=support.holding_payload())

    holding = bank.get_holding(support.bound_context(), "h_wealth_sample")

    assert holding == Holding(
        holding_id="h_wealth_sample",
        user_id=support.DEMO_CUSTOMER,
        product_id="p_demo_cash",
        product_name="演示灵活理财",
        source_account_id="a_wealth_main",
        principal_minor=100000,
        current_value_minor=100500,
        accrued_interest_minor=500,
        realized_income_minor=0,
        redeemable_minor=100500,
        expected_return=0.02,
        status="holding",
        purchased_at="2026-09-06T10:00:00+08:00",
        maturity_date=None,
    )


def test_expected_return_is_never_earned_income(bank, double):
    """`expected_return` 是演示年化参数；持有收益和已实现收益来自各自的银行字段。"""

    double.on(
        "GET",
        "/holdings",
        data=[
            support.holding_payload(
                expected_return=0.06, accrued_interest_minor=500, realized_income_minor=1200
            )
        ],
    )

    holding = bank.list_holdings(support.bound_context())[0]

    assert holding.expected_return == 0.06
    assert holding.accrued_interest_minor == 500
    assert holding.realized_income_minor == 1200


def test_order_id_and_transaction_id_stay_distinct(bank, double):
    """订单号是业务订单，流水号是资金流水，不互相顶替。"""

    payload = support.order_payload()
    double.on("GET", "/wealth/orders/order_wealth_sample", data=payload)

    order = bank.get_order(support.bound_context(), "order_wealth_sample")

    assert order.order_id == payload["order_id"]
    assert order.transaction_id == payload["transaction_id"]
    assert order.order_id != order.transaction_id


def test_a_purchase_receipt_keeps_order_and_transaction_apart(bank, double):
    payload = support.purchase_receipt_payload()
    double.on("POST", "/products/purchase", data=payload, status=201)

    receipt = bank.purchase(support.bound_context(), _purchase_intent(), "246810", "idem-1")

    assert receipt.order_id == payload["order_id"]
    assert receipt.transaction_id == payload["transaction_id"]
    assert receipt.status == "completed"


def test_risk_profile_is_absent_when_the_bank_has_none(bank, double):
    double.on("GET", "/wealth/risk-assessments/latest", data=None)

    profile = bank.get_risk_profile(support.bound_context())

    assert profile is None


def test_risk_profile_maps_the_banks_own_level(bank, double):
    payload = support.risk_profile_payload(risk_level="low", score=2)
    double.on("GET", "/wealth/risk-assessments/latest", data=payload)

    profile = bank.get_risk_profile(support.bound_context())

    assert profile is not None
    assert profile == RiskProfile(
        assessment_id="wealth_assessment_1",
        user_id=support.DEMO_CUSTOMER,
        answers={"q1": 0, "q2": 1, "q3": 1, "q4": 0, "q5": 0},
        score=2,
        risk_level="low",
        rule_version="wealth-risk-v1",
        assessed_at="2026-10-06T10:00:00+08:00",
        expires_at="2026-11-05T10:00:00+08:00",
        valid=True,
    )


def test_submit_risk_assessment_posts_the_answers_and_takes_the_banks_level(bank, double):
    """问卷保存是非资金写：没有确认、没有验证码，也没有幂等键；等级由银行给。"""

    payload = support.risk_profile_payload(risk_level="low", score=2)
    double.on("POST", "/wealth/risk-assessments", data=payload, status=201)
    answers = {"q1": 0, "q2": 1, "q3": 1, "q4": 0, "q5": 0}

    profile = bank.submit_risk_assessment(support.bound_context(), answers)

    call = double.only_call()
    assert call.path == "/wealth/risk-assessments"
    assert call.body == {"answers": answers, "user_id": support.DEMO_CUSTOMER}
    assert call.idempotency_key is None
    assert profile.risk_level == "low"


def test_find_order_by_key_sends_the_operation_and_the_key(bank, double):
    double.on("GET", "/wealth/orders/by-idempotency-key", data=support.idempotency_payload())

    lookup = bank.find_order_by_key(support.bound_context(), PURCHASE_OPERATION, "idem-1")

    call = double.only_call()
    assert call.path == "/wealth/orders/by-idempotency-key"
    assert call.query == {
        "operation_type": PURCHASE_OPERATION,
        "idempotent_key": "idem-1",
        "user_id": support.DEMO_CUSTOMER,
    }
    assert lookup.found is True
    assert lookup.operation_type == PURCHASE_OPERATION
    assert lookup.idempotent_key == "idem-1"
    assert lookup.result is not None
    assert lookup.result["order_id"] == "order_purchase_1"
    assert lookup.order is not None
    assert lookup.order.order_id == "order_purchase_1"
    assert lookup.order.transaction_id == "txn_purchase_1"
    assert lookup.created_at == "2026-10-06T10:00:00+08:00"


def test_a_missing_idempotency_record_is_not_a_fabricated_order(bank, double):
    """按幂等键查不到不等于没提交：适配器只如实返回 found=false，不编造订单。"""

    double.on(
        "GET",
        "/wealth/orders/by-idempotency-key",
        data=support.idempotency_payload(
            found=False, result=None, order=None, idempotent_key="idem-unknown", created_at=None
        ),
    )

    lookup = bank.find_order_by_key(support.bound_context(), REDEEM_OPERATION, "idem-unknown")

    assert lookup.found is False
    assert lookup.order is None
    assert lookup.result is None


def test_no_failure_path_shows_the_banks_own_message(bank, double):
    """银行的 message 是类名默认值，既不是原因也是英文，用户看到的是这一侧的中文句子。"""

    double.on("GET", "/products", code=1004, status=404, data=None, message="resource_not_found")

    with pytest.raises(WealthBankError) as caught:
        bank.list_products(support.bound_context())

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert caught.value.message == "没有找到对应的银行记录。"
    assert "resource_not_found" not in caught.value.message


def test_a_resource_id_cannot_rewrite_the_request_path(bank, double):
    """ID 是数据：带斜杠的 ID 只作为一个路径段发出，不能改道到别的银行接口。

    不编码时 httpx 会把 `/holdings/h/../accounts` 规范化成 `/holdings/accounts`，一个订单号
    就能让理财适配器去读别的域接口。替身对未预设的调用直接报错，所以这里同时证明了请求
    确实落在编码后的路径上。
    """

    double.on("GET", "/products/p%2F..%2Faccounts", data=support.product_payload())
    double.on("GET", "/holdings/h%2F..%2Faccounts", data=support.holding_payload())
    double.on("GET", "/wealth/orders/o%2F..%2Fclock", data=support.order_payload())

    bank.get_product(support.bound_context(), "p/../accounts")
    bank.get_holding(support.bound_context(), "h/../accounts")
    bank.get_order(support.bound_context(), "o/../clock")

    assert [call.path for call in double.calls] == [
        "/products/p%2F..%2Faccounts",
        "/holdings/h%2F..%2Faccounts",
        "/wealth/orders/o%2F..%2Fclock",
    ]


# ---------------------------------------------------------------------- 小工具


def _purchase_intent() -> PurchaseIntent:
    return PurchaseIntent(
        product_id="p_demo_cash", from_account_id="a_wealth_main", amount_minor=200000
    )
