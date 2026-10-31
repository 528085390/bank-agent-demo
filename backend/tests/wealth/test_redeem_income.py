"""任务10：持仓收益查询与部分／全部赎回（离线门禁层）。

这一层证明的是**收益只读、赎回门禁与状态机**，全部走 HTTP 传输替身（离线层）：

1. **收益查询是只读**：GET 银行持仓就展示本金、当前价值、持有收益和已实现收益；没有计息写、
   没有资金写。
2. **少一步就不写钱**：赎回预览只是预览；没有 `confirm`，`POST /products/redeem` 一次都不会
   发出去。而"点击确认"要带什么，取决于适配器的 `write_authorization()`：本仓库的
   `HttpWealthBank` 如实回答 `CLICK_AND_CODE`（银行的写门禁要求验证码），所以确认框会要求用户
   输入验证码，没带的点击在**发出任何资金请求之前**被拒绝；用户输入后这笔写真的提交给银行。
3. **金额有三种表达**：具体金额、全部（`redeemable_minor`）、一半（向下取整到分）；预览前
   必须解析成一个具体金额，并由程序模板写进预览原文。
4. **"刚才买的"绑定最近成功申购的新仓**：不按数据库顺序猜第一笔；同产品多仓就先澄清。
5. **赎回不要求重新测评**：与申购共用 pending／点击确认／改参失效流程，但不做风险预检。
6. **分摊由银行算**：回执里的释放本金与实现收益原样搬运，Agent 不另算权威数值。
7. **验证码入口已停用**：`kind=verify` 被明确拒绝（`INVALID_STATE`），一次银行调用都不新增；
   结果未知既不是成功也不是失败，只能由 `kind=reconcile` 按**原**幂等键只读核实，绝不重发写请求。

真实余额、真实订单行与真实重启保留属于 `tests/wealth_integration/`（任务10 的端到端验收）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.application.runtime import MAX_VERIFICATION_ATTEMPTS
from app.modules.wealth.contracts.bank import (
    REDEEM_OPERATION,
    RUNTIME_OWNED_FIELDS,
    Account,
    Holding,
    RedeemIntent,
    RedeemSnapshot,
    WealthBankError,
    WriteAuthorization,
)
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.domain.rules import (
    RedeemAmountError,
    check_redeem,
    parse_redeem_amount,
    redeem_preview,
)
from app.modules.wealth.llm.decisions import ACTIONS, GateViolation, parse_decision
from app.modules.wealth.llm.toolcalls import tool_table
from app.modules.wealth.workflow.state import REDEEM_SLOTS
from app.tools.protocol import ErrorCode
from tests.wealth import support

HOLDINGS = "/holdings"
HOLDING_SAMPLE = "/holdings/h_wealth_sample"
NEW_HOLDING = "h_purchase_1"
HOLDING_NEW = "/holdings/h_purchase_1"
ACCOUNTS = "/accounts"
REDEEM = "/products/redeem"
ORDER_REDEEM = "/wealth/orders/order_redeem_1"
PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
PURCHASE = "/products/purchase"
LATEST = "/wealth/risk-assessments/latest"
ORDER_PURCHASE = "/wealth/orders/order_purchase_1"
# 按幂等键核实（任务11）：结果未知时唯一的读出口。
LOOKUP = "/wealth/orders/by-idempotency-key"

CUSTOMER = support.DEMO_CUSTOMER
SAMPLE_HOLDING = "h_wealth_sample"
ACCOUNT = "a_wealth_main"
CODE = "246810"
# 银行配置的演示码是 123456；这个值银行一定会判错，用来验证"输错之后能重输"。
WRONG_CODE = "000000"
BALANCE = 2000000
SAMPLE = support.holding_payload()
SLOTS = {"holding_id": SAMPLE_HOLDING, "amount_minor": 50000, "account_id": ACCOUNT}


# ---------------------------------------------------------------------- 夹具


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _click_bank(double):
    """声明支持点击确认的适配器替身：真实适配器在这条路径上根本不带验证码。

    `HttpWealthBank` 回答 False（D2），所以成功路径必须用它；请求的 method／path／query／body
    仍然由真实的 `BankClient` 组装，`BankDouble` 的预设与断言照常有效。
    """

    return support.ClickConfirmationBank(double.client())


def _stub_reads(
    double,
    *,
    holdings=None,
    holding_seq=None,
    account_seq=None,
    holding_path=HOLDING_SAMPLE,
    product_id="p_demo_cash",
):
    """赎回预览／刷新／写后核实要读的银行事实。

    持仓详情与账户按调用顺序取值（最后一个会被重复使用），所以一个用例可以表达"预览时价值
    1005 元、赎回之后剩余 505 元"。产品详情用来把产品名写进预览。
    """

    double.on("GET", HOLDINGS, data=holdings if holdings is not None else [SAMPLE])
    double.on_sequence("GET", holding_path, *(holding_seq or [SAMPLE]))
    double.on("GET", f"/products/{product_id}", data=support.product_payload(product_id=product_id))
    double.on_sequence(
        "GET",
        ACCOUNTS,
        *[
            [support.account_payload(balance_minor=value, available_minor=value)]
            for value in (account_seq or [BALANCE])
        ],
    )
    return double


def _principal_for(amount, *, principal=100000, value=100500):
    """银行的部分赎回分摊：floor(P×R/V)。测试用它写期望值，不当作生产规则。"""

    return principal * amount // value


def _redeem_receipt(holding_id=SAMPLE_HOLDING, amount=50000, **overrides):
    principal = _principal_for(amount)
    return support.redemption_receipt_payload(
        order_id="order_redeem_1",
        holding_id=holding_id,
        transaction_id="txn_redeem_1",
        amount_minor=amount,
        principal_redeemed_minor=principal,
        interest_minor=amount - principal,
        holding_status="partial" if amount < 100500 else "redeemed",
        remaining_principal_minor=100000 - principal,
        remaining_value_minor=100500 - amount,
        **overrides,
    )


def _stub_receipt(double, **overrides):
    double.on("POST", REDEEM, data=_redeem_receipt(**overrides), status=201)
    return double


def _stub_order_readback(double, holding_id=SAMPLE_HOLDING, amount=50000):
    principal = _principal_for(amount)
    double.on(
        "GET",
        ORDER_REDEEM,
        data=support.order_payload(
            order_id="order_redeem_1",
            order_type="product_redeem",
            holding_id=holding_id,
            from_account_id=None,
            to_account_id=ACCOUNT,
            amount_minor=amount,
            transaction_id="txn_redeem_1",
            details={
                "principal_redeemed_minor": principal,
                "interest_redeemed_minor": amount - principal,
            },
        ),
    )
    return double


def _preview_replies(slots=None):
    """模型先查持仓列表和账户列表（编号必须有据），再发起一次赎回预览。"""

    return (
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(dict(slots or SLOTS)),
    )


def _preview(agent, context, *, session=None, pending=None):
    return agent.handle(
        context,
        WealthRequest(kind="message", text="赎回 500 元的演示灵活理财持仓"),
        session=session or {},
        pending=pending,
    )


def _confirmed(agent, context, pending_id, code=None, *, session=None, pending=None):
    """点确认提交这一笔；`code` 是用户在确认框上输入的验证码（银行要求时必填）。"""

    return agent.handle(
        context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=code),
        session=session or {},
        pending=pending,
    )


def _retired_verify(agent, context, pending_id, code=CODE, *, session=None, pending=None):
    """已停用的旧验证码入口：只用来验证"它被明确拒绝，且不碰银行"。"""

    return agent.handle(
        context,
        WealthRequest(kind="verify", pending_id=pending_id, verification_code=code),
        session=session or {},
        pending=pending,
    )


def _reconciled(agent, context, pending_id, *, session=None, pending=None):
    return agent.handle(
        context,
        WealthRequest(kind="reconcile", pending_id=pending_id),
        session=session or {},
        pending=pending,
    )


def _write_record(pending):
    write = (pending or {}).get("write")
    return SimpleNamespace(**write) if write else None


def _paths(double):
    return [call.path for call in double.calls]


def _posts(double):
    return [call for call in double.calls if call.method == "POST"]


# ---------------------------------------------------------------- 收益查询只读


def test_income_read_is_not_interest_write(bank, double, clock):
    """收益查询：读持仓就展示本金、当前价值、持有收益和已实现收益；没有任何计息写请求。"""

    double.on("GET", HOLDING_SAMPLE, data=support.holding_payload())
    model = support.ScriptModel(
        support.call_read("wealth_holding", {"holding_id": SAMPLE_HOLDING}),
        support.finish(
            "样例持仓本金 1,000.00 元、当前价值 1,005.00 元，"
            "持有收益 5.00 元，已实现收益 0.00 元。",
            cited_ids=[SAMPLE_HOLDING],
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="我的持仓收益是多少？"),
        session={},
        pending=None,
    )

    assert response.status is WealthStatus.COMPLETED
    # 只读一次持仓：没有计息端点，也没有任何写请求。
    assert [(call.method, call.path) for call in double.calls] == [("GET", HOLDING_SAMPLE)]
    holding = response.facts["holdings"][0]
    assert holding["principal_minor"] == 100000
    assert holding["current_value_minor"] == 100500
    assert holding["accrued_interest_minor"] == 500
    assert holding["realized_income_minor"] == 0
    # 工具摘要把持有收益和已实现收益都写出来（不是只有英文键名在事实里）。
    assert "持有收益 5.00 元" in model.prompts[1]
    assert "已实现收益 0.00 元" in model.prompts[1]


# ---------------------------------------------------------------- 赎回金额的三种表达


def test_redeem_amounts_can_be_explicit_all_or_half():
    """具体金额原样使用；"全部"取可赎回额；"一半"按可赎回额向下取整到分。"""

    assert parse_redeem_amount(50000).resolve(100500) == 50000
    assert parse_redeem_amount({"all": True}).resolve(100500) == 100500
    assert parse_redeem_amount({"half": True}).resolve(100501) == 50250
    assert parse_redeem_amount({"half": True}).resolve(100500) == 50250


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        50000.5,
        "500",
        "一半",
        True,
        None,
        {"ordinal": 1},
        {"all": False},
        {"half": 1},
        {"all": True, "half": True},
    ],
)
def test_redeem_amounts_that_are_not_submittable_are_rejected(value):
    """含糊金额、负数、零、非整数分和别的引用形式都不能直接提交。"""

    with pytest.raises(RedeemAmountError):
        parse_redeem_amount(value)


# ---------------------------------------------------------------- 赎回预检


def test_the_redeem_pre_check_blocks_a_fixed_term_holding():
    """定期产品未到期：银行的可赎回额是 0，预检直接拦住，不发写请求。"""

    holding = Holding.from_bank(
        support.holding_payload(redeemable_minor=0, maturity_date="2027-01-01")
    )
    account = Account.from_bank(support.account_payload())

    blocked = check_redeem(holding, account=account, amount_minor=100000)

    assert blocked.allowed is False
    assert any("不可赎回" in reason for reason in blocked.reasons)


def test_the_redeem_pre_check_blocks_more_than_the_redeemable_amount():
    holding = Holding.from_bank(support.holding_payload())
    account = Account.from_bank(support.account_payload())

    blocked = check_redeem(holding, account=account, amount_minor=200000)

    assert blocked.allowed is False
    assert any("超过可赎回金额" in reason for reason in blocked.reasons)


def test_the_redeem_pre_check_allows_a_valid_partial_redeem():
    """赎回不要求重新测评：只要持仓、金额和到账账户合规就放行。"""

    holding = Holding.from_bank(support.holding_payload())
    account = Account.from_bank(support.account_payload())

    allowed = check_redeem(
        holding, account=account, amount_minor=50000, user_id=support.DEMO_CUSTOMER
    )

    assert allowed.allowed is True


def test_the_redeem_pre_check_rejects_an_account_that_is_not_mine():
    holding = Holding.from_bank(support.holding_payload())
    account = Account.from_bank(support.account_payload(user_id="u_wealth_other"))

    blocked = check_redeem(
        holding, account=account, amount_minor=50000, user_id=support.DEMO_CUSTOMER
    )

    assert blocked.allowed is False
    assert any("不属于本人" in reason for reason in blocked.reasons)


def test_the_redeem_preview_shows_the_concrete_amount():
    """预览由程序模板生成：持仓、具体金额、可赎回额和到账账户都在里面。"""

    holding = Holding.from_bank(support.holding_payload())
    account = Account.from_bank(support.account_payload())

    text = redeem_preview(holding, account, amount_minor=50000, redeemable_minor=100500)

    assert "500.00 元" in text
    assert SAMPLE_HOLDING in text
    assert ACCOUNT in text
    assert "模拟" in text


# ---------------------------------------------------------------- 少一步就不写钱


def test_no_write_before_confirm_and_a_click_without_a_code_writes_nothing(bank, double, clock):
    """少一步就不写钱：预览只是预览，少了验证码的确认也不会偷偷变成一次资金写。

    预览阶段一次写请求都没有；而本仓库的 `HttpWealthBank` 如实声明 `CLICK_AND_CODE`（银行的
    写门禁要求验证码），所以确认框会要求验证码，而这次点击没带——运行时在**发出任何资金请求
    之前**就拒绝它，并能确定地说"没有提交"，待办保留到 TTL。它不是交易失败，也不是已经成交。
    """

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    preview, session, pending = _preview(agent, context, session=session, pending=pending)

    assert preview.status is WealthStatus.WAITING_CONFIRMATION
    assert preview.pending is not None
    assert preview.confirmation.requires_code is True
    assert _posts(double) == []

    pending_id = preview.pending.pending_id
    refused, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert refused.status is WealthStatus.FAILED
    assert refused.error_code == ErrorCode.VALIDATION
    assert "需要验证码" in refused.summary
    # 一次资金写都没发出去，也没有因此多读一次银行（能力检查发生在写前刷新之前）。
    assert _posts(double) == []
    # 两次查询（持仓、账户）加一次赎回预检（持仓、产品、账户）：没有一次写请求，也没有测评读取。
    assert _paths(double) == [HOLDINGS, ACCOUNTS, HOLDING_SAMPLE, PRODUCT, ACCOUNTS]
    assert LATEST not in _paths(double)
    # 待办保留：补上验证码再点一次就能提交，而不是"没有可确认的待办"。
    assert _write_record(pending).receipt is None
    again, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )
    assert again.status is WealthStatus.FAILED
    assert again.error_code == ErrorCode.VALIDATION
    assert _posts(double) == []


def test_the_retired_verify_entry_is_refused_without_touching_the_bank(bank, double, clock):
    """验证码入口已停用：`kind=verify` 被明确拒绝，而且一次银行调用都不会新增。"""

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    before = list(double.calls)

    response, session, pending = _retired_verify(
        agent, context, preview.pending.pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert "停用" in response.summary
    # 拒绝的原因是这条入口没有了，不是因为待办不对：待办本身仍然有效、仍然没有回执。
    assert _write_record(pending).receipt is None
    assert double.calls == before
    assert _posts(double) == []


def test_the_verification_code_never_enters_the_model_context(double, clock, caplog):
    """验证码是秘密：它进银行的写请求，但不进提示词、不进响应、不进日志。

    这条用例走 `CLICK` 能力的适配器替身（银行**不要**验证码），所以它还钉住另一半：那种银行
    的写请求体里根本没有 `mfa_code` 这个字段——"要不要带验证码"确实由能力声明驱动。
    """

    session = {}
    pending = None
    caplog.set_level(logging.INFO, logger="app.modules.wealth")
    after = support.holding_payload(
        principal_minor=50249, current_value_minor=50500, status="partial"
    )
    _stub_reads(
        double,
        holding_seq=[SAMPLE, SAMPLE, after],
        account_seq=[BALANCE, BALANCE, BALANCE + 50000],
    )
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    retired, session, pending = _retired_verify(
        agent, context, pending_id, session=session, pending=pending
    )

    assert retired.status is WealthStatus.FAILED
    assert retired.error_code == ErrorCode.INVALID_STATE

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.COMPLETED
    assert CODE not in repr(retired)
    assert CODE not in repr(response)
    assert all(CODE not in prompt for prompt in model.prompts)
    # 点击确认那条路径不发验证码：写请求体里没有这个字段。
    assert "mfa_code" not in _posts(double)[0].body
    # 日志里也没有验证码（模型输出原文、请求文本同样不进日志）。
    records = [record for record in caplog.records if record.name.startswith("app.modules.wealth")]
    assert records, "至少应有一条 wealth 日志"
    for record in records:
        assert CODE not in record.getMessage()
        assert CODE not in str(record.args)


# ---------------------------------------------------------------- 预览与快照


def test_the_redeem_preview_states_holding_amount_and_account(bank, double, clock):
    """预览由程序模板生成：持仓、金额、可赎回额、到账账户和模拟提示都在里面。"""

    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    model = support.ScriptModel(*_preview_replies())

    preview, _, _ = _preview(
        _agent(model, bank, clock), support.bound_context(), session={}, pending=None
    )

    text = preview.summary
    assert preview.pending.prompt == text
    assert "500.00 元" in text
    assert SAMPLE_HOLDING in text
    assert ACCOUNT in text
    assert "部分赎回" in text
    assert "模拟" in text

    snapshot = preview.facts["redeem_preview"]
    assert snapshot["holding_id"] == SAMPLE_HOLDING
    assert snapshot["to_account_id"] == ACCOUNT
    assert snapshot["amount_minor"] == 50000
    assert snapshot["redeemable_minor"] == 100500
    assert snapshot["full"] is False
    assert snapshot["preview"] == text
    # 预览不产生业务记录：编号只出现在事实里，业务号是空的。
    assert preview.business_ids == {}


def test_the_full_redeem_preview_resolves_the_redeemable_amount(bank, double, clock):
    """{"all": true} 在预览前解析成银行的可赎回额，预览写明"全部赎回"。"""

    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    model = support.ScriptModel(*_preview_replies({**SLOTS, "amount_minor": {"all": True}}))

    preview, _, _ = _preview(
        _agent(model, bank, clock), support.bound_context(), session={}, pending=None
    )

    assert preview.facts["redeem_preview"]["amount_minor"] == 100500
    assert preview.facts["redeem_preview"]["full"] is True
    assert "全部赎回" in preview.summary


def test_the_half_redeem_preview_rounds_down_to_the_cent(bank, double, clock):
    """{"half": true} 按可赎回额向下取整到分：100501 分的一半是 50250 分。"""

    odd = support.holding_payload(redeemable_minor=100501, current_value_minor=100501)
    _stub_reads(double, holdings=[odd], holding_seq=[odd, odd], account_seq=[BALANCE, BALANCE])
    model = support.ScriptModel(*_preview_replies({**SLOTS, "amount_minor": {"half": True}}))

    preview, _, _ = _preview(
        _agent(model, bank, clock), support.bound_context(), session={}, pending=None
    )

    assert preview.facts["redeem_preview"]["amount_minor"] == 50250


def test_the_redeem_snapshot_carries_no_authorization_fields():
    """冻结的快照只带业务槽位：身份、确认、验证码和幂等键都不在里面。"""

    snapshot = RedeemSnapshot(
        holding_id=SAMPLE_HOLDING,
        product_id="p_demo_cash",
        product_name="演示灵活理财",
        to_account_id=ACCOUNT,
        amount_minor=50000,
        redeemable_minor=100500,
        redeem_rule="flexible",
        full=False,
        preview="请确认",
    )

    fields = set(snapshot.as_state())

    assert fields.isdisjoint(RUNTIME_OWNED_FIELDS)
    assert snapshot.intent().holding_id == SAMPLE_HOLDING
    assert snapshot.intent().amount_minor == 50000
    assert snapshot.intent().to_account_id == ACCOUNT


def test_a_malformed_redeem_snapshot_is_rejected_rather_than_defaulted():
    """快照缺字段就是协议错误：不补默认值，也不带着半个意图去写钱。"""

    for payload in (
        {},
        {"holding_id": SAMPLE_HOLDING},
        {
            "holding_id": SAMPLE_HOLDING,
            "product_id": "p_demo_cash",
            "product_name": "演示灵活理财",
            "to_account_id": ACCOUNT,
            "amount_minor": "50000",
            "redeemable_minor": 100500,
            "redeem_rule": "flexible",
            "full": False,
            "preview": "请确认",
        },
    ):
        with pytest.raises(ValueError):
            RedeemSnapshot.from_state(payload)


def test_the_model_cannot_supply_authorization_fields():
    """授权不是一个字段：prepare_redeem 多带 user_id／confirm／幂等键会被直接拒绝。"""

    for extra in (
        {"user_id": "u_li"},
        {"confirm": True},
        {"mfa_code": CODE},
        {"idempotent_key": "idem-1"},
    ):
        with pytest.raises(GateViolation):
            parse_decision(
                *support.decision_input(
                    support.call("prepare_redeem", {"slots": {**SLOTS, **extra}})
                ),
                table=tool_table(),
            )

    decision = parse_decision(
        *support.decision_input(support.call("prepare_redeem", {"slots": SLOTS})),
        table=tool_table(),
    )

    assert decision.action == "prepare_redeem"
    assert decision.slots == SLOTS
    assert "prepare_redeem" in ACTIONS
    # 工具表里没有资金写：模型能表达的动作里根本没有"下单"这一项。
    assert "redeem" not in tool_table()


def test_a_redeem_slot_outside_the_three_is_corrected(bank, double, clock):
    """prepare_redeem 只接受持仓、金额、到账账户：多带一个产品编号就被纠正。"""

    model = support.ScriptModel(
        support.prepare_redeem({**SLOTS, "product_id": "p_demo_cash"}),
        support.ask_user("请问要赎回哪一笔持仓？", ["holding_id"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    assert "product_id" in model.prompts[1]
    assert double.calls == []


# ---------------------------------------------------------------- 缺持仓与不可赎回


def test_a_missing_holding_is_clarified_with_the_holdings(bank, double, clock):
    """没说赎回哪一笔：把缺项交回模型，由模型先查持仓再让用户挑，不默认赎回第一笔。"""

    second = support.holding_payload(holding_id="h_other", principal_minor=200000)
    _stub_reads(double, holdings=[SAMPLE, second])
    model = support.ScriptModel(
        support.call_read("wealth_accounts"),
        support.prepare_redeem({"amount_minor": 50000, "account_id": ACCOUNT}),
        support.call_read("wealth_holdings"),
        support.ask_user(
            f"请说明要赎回哪一笔持仓：{SAMPLE_HOLDING} 还是 h_other？", ["holding_id"]
        ),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    # 缺项事实点名了持仓，并提醒模型"先查持仓再问、不要替用户选一笔"。
    assert "持仓编号" in model.prompts[2]
    assert "wealth_holdings" in model.prompts[2]
    assert "不要替用户选一笔" in model.prompts[2]
    # 模型真的去查了持仓列表，并把两笔都摆给了用户。
    assert SAMPLE_HOLDING in response.summary
    assert "h_other" in response.summary
    assert _paths(double) == [ACCOUNTS, HOLDINGS]
    assert _posts(double) == []


def test_a_fixed_term_holding_is_blocked_before_any_write(bank, double, clock):
    """未到期的定期持仓：预检拦住，交回模型如实解释，没有任何写请求。"""

    fixed = support.holding_payload(
        product_id="p_demo_stable",
        redeemable_minor=0,
        current_value_minor=200000,
        maturity_date="2027-01-01",
    )
    _stub_reads(
        double,
        holdings=[fixed],
        holding_seq=[fixed],
        account_seq=[BALANCE],
        product_id="p_demo_stable",
    )
    model = support.ScriptModel(
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {"holding_id": SAMPLE_HOLDING, "amount_minor": 50000, "account_id": ACCOUNT}
        ),
        support.finish("这笔持仓还没到期，现在不能赎回。", cited_ids=[SAMPLE_HOLDING]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.COMPLETED
    assert "不可赎回" in model.prompts[3]
    assert _posts(double) == []


def test_a_recent_holding_without_a_purchase_is_clarified(bank, double, clock):
    """ "刚才买的"没有对应新仓时：纠正一次，让模型改问用户，不发任何写请求。"""

    model = support.ScriptModel(
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {"holding_id": {"recent": True}, "amount_minor": {"all": True}, "account_id": ACCOUNT}
        ),
        support.ask_user("请说明要赎回哪一笔持仓。", ["holding_id"]),
    )
    double.on("GET", ACCOUNTS, data=[support.account_payload()])
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    assert "最近没有成功申购的持仓" in model.prompts[2]
    assert _paths(double) == [ACCOUNTS]


# ---------------------------------------------------------------- 确认绑定快照


def test_old_confirmation_cannot_redeem_a_new_amount(bank, double, clock):
    """改金额即换意图：旧待办既不能确认、也不能核实，更没有写请求。"""

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE], account_seq=[BALANCE])
    model = support.ScriptModel(
        *_preview_replies(),
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem({**SLOTS, "amount_minor": 100000}),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    first, session, pending = _preview(agent, context, session=session, pending=pending)
    old_pending_id = first.pending.pending_id

    second, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="算了，改成 1000 元"),
        session=session,
        pending=pending,
    )

    assert second.status is WealthStatus.WAITING_CONFIRMATION
    assert second.pending.pending_id != old_pending_id
    assert second.facts["redeem_preview"]["amount_minor"] == 100000

    stale = [
        _confirmed(agent, context, old_pending_id, code=CODE, session=session, pending=pending)[0],
        _reconciled(agent, context, old_pending_id, session=session, pending=pending)[0],
    ]

    for response in stale:
        assert response.status is WealthStatus.FAILED
        assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []
    # 旧待办连一次按原键的只读核实都触发不了：它已经不属于当前意图。
    assert LOOKUP not in _paths(double)


def test_the_bank_write_carries_the_runtime_key_and_the_bound_customer(double, clock):
    """写请求的身份、确认标志和幂等键都来自运行时，不是模型；点击确认那条路径不带验证码。"""

    session = {}
    pending = None
    _stub_reads(
        double,
        holding_seq=[
            SAMPLE,
            SAMPLE,
            support.holding_payload(principal_minor=50249, current_value_minor=50500),
        ],
        account_seq=[BALANCE, BALANCE, BALANCE + 50000],
    )
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.COMPLETED
    call = _posts(double)[0]
    assert call.path == REDEEM
    assert call.body == {
        "holding_id": SAMPLE_HOLDING,
        "amount_minor": 50000,
        "to_account_id": ACCOUNT,
        "confirm": True,
        "idempotent_key": call.idempotency_key,
        "user_id": CUSTOMER,
    }
    # 点击确认取代了"确认 + 输入验证码"：请求体里没有验证码这个字段。
    assert "mfa_code" not in call.body
    assert _write_record(pending).idempotency_key == call.idempotency_key


# ---------------------------------------------------------------- 明确拒绝与未知结果


def test_a_wrong_code_re_offers_the_preview_and_moves_no_money(bank, double, clock):
    """验证码被银行判错（1008）：没有提交，待办与幂等键都留着，预览重新交回来让用户重输。

    银行明确没有受理（不是结果未知），所以能确定地说"没有提交"；而用户下一步要做的正是重输
    一个码，所以流程不结束——重新等确认的是**同一笔**，幂等键没换。
    """

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    double.on("POST", REDEEM, code=1008, status=409, data=None)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, code=WRONG_CODE, session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert response.pending.pending_id == pending_id
    assert response.confirmation.requires_code is True
    assert "验证码" in response.summary
    assert len(_posts(double)) == 1
    assert _posts(double)[0].body["mfa_code"] == WRONG_CODE
    assert _write_record(pending).receipt is None
    assert response.business_ids == {}
    # 重来的是同一笔：一个意图只有一个幂等键。
    key = _write_record(pending).idempotency_key
    assert {call.idempotency_key for call in _posts(double)} == {key}


def test_repeated_wrong_codes_do_not_lock_the_users_writes(bank, double, clock):
    """输错验证码不驱动锁定：它不是攻击，也不是技术故障。

    写锁定只由连续技术失败与累计绕过门禁驱动；即使到了旧阈值那么多次，用户也不会被锁定，
    而每一次都真的走到了银行（用的是同一个幂等键，所以银行最多只会执行一笔）。
    """

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE], account_seq=[BALANCE])
    double.on("POST", REDEEM, code=1008, status=409, data=None)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    responses = [
        _confirmed(agent, context, pending_id, code=WRONG_CODE, session=session, pending=pending)[0]
        for _ in range(MAX_VERIFICATION_ATTEMPTS)
    ]

    for response in responses:
        assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert len(_posts(double)) == MAX_VERIFICATION_ATTEMPTS
    assert {call.idempotency_key for call in _posts(double)} == {
        _write_record(pending).idempotency_key
    }
    assert context.user_id not in agent._locked_users
    assert agent._technical_failures.get(context.user_id, 0) == 0


def test_an_unknown_outcome_is_not_a_success(double, clock):
    """写请求超时（结果未知）：既不返回成功，也不宣布失败，只按原键只读核实。

    核实用的是**原**幂等键和原操作类型（换键就等于重新下单）；确认时已经自动核实过一次，
    之后每次 `kind=reconcile` 仍然只读银行、绝不重发写请求，待办与幂等键都留着，并把核实引用
    交给调用方（计划任务11）。
    """

    session = {}
    pending = None
    _stub_reads(double, holding_seq=[SAMPLE, SAMPLE], account_seq=[BALANCE, BALANCE])
    double.on_failure("POST", REDEEM, httpx.ReadTimeout("slow"))
    double.on("GET", LOOKUP, data=support.lookup_miss(REDEEM_OPERATION, "idem-unused"))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.UNKNOWN
    assert response.error_code == ErrorCode.TRANSACTION_UNKNOWN
    assert "未知" in response.summary
    assert response.business_ids == {}
    write = _write_record(pending)
    assert write.receipt is None
    # 待办被标成"只能核实"：之后这个待办上的输入不会再变成一次写。
    assert write.reconcile_required is True
    key = write.idempotency_key
    assert key
    assert len(_posts(double)) == 1
    lookups = [call for call in double.calls if call.path == LOOKUP]
    assert len(lookups) == 1
    assert lookups[0].query["idempotent_key"] == key
    assert lookups[0].query["operation_type"] == REDEEM_OPERATION
    assert response.facts["reconciliation"] == {
        "operation_type": REDEEM_OPERATION,
        "idempotent_key": key,
        "found": False,
        "order_id": None,
    }

    again, session, pending = _reconciled(
        agent, context, pending_id, session=session, pending=pending
    )

    assert again.status is WealthStatus.UNKNOWN
    assert again.error_code == ErrorCode.TRANSACTION_UNKNOWN
    assert "未知" in again.summary
    assert again.business_ids == {}
    assert again.facts["reconciliation"]["idempotent_key"] == key
    # 核实是只读的：没有第二次写请求，幂等键也没有换。
    assert len(_posts(double)) == 1
    lookups = [call for call in double.calls if call.path == LOOKUP]
    assert len(lookups) == 2
    assert lookups[1].query["idempotent_key"] == key
    assert lookups[1].query["operation_type"] == REDEEM_OPERATION
    assert _write_record(pending).idempotency_key == key
    assert _write_record(pending).receipt is None


# ---------------------------------------------------------------- 成功与重复


def test_the_success_summary_comes_from_the_receipt(double, clock):
    """成功话术由程序模板生成：释放本金与实现收益都来自银行回执。"""

    session = {}
    pending = None
    after = support.holding_payload(
        principal_minor=50249, current_value_minor=50500, status="partial"
    )
    _stub_reads(
        double, holding_seq=[SAMPLE, SAMPLE, after], account_seq=[BALANCE, BALANCE, BALANCE + 50000]
    )
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.COMPLETED
    assert "order_redeem_1" in response.summary
    assert "txn_redeem_1" in response.summary
    assert "497.51 元" in response.summary  # 银行分摊的释放本金
    assert "2.49 元" in response.summary  # 银行分摊的实现收益
    assert response.business_ids == {
        "order_id": "order_redeem_1",
        "holding_id": SAMPLE_HOLDING,
        "transaction_id": "txn_redeem_1",
        "product_id": "p_demo_cash",
    }
    receipt = response.facts["redeem_receipt"]
    assert receipt["amount_minor"] == 50000
    assert receipt["principal_redeemed_minor"] == 49751
    assert receipt["interest_minor"] == 249
    # 写后核实：订单、剩余持仓与到账账户都从银行重新读到。
    assert response.facts["redeem_verified"] is True
    assert response.facts["orders"][0]["order_id"] == "order_redeem_1"
    assert response.facts["holdings"][0]["principal_minor"] == 50249
    assert response.facts["accounts"][0]["balance_minor"] == BALANCE + 50000
    assert model.calls == 3  # 模型只负责查事实和提取槽位；成交话术不是它写的


def test_duplicate_confirm_returns_the_same_order(double, clock):
    """重复确认仍是一笔银行订单：复用同一个幂等键和同一份回执。"""

    session = {}
    pending = None
    after = support.holding_payload(
        principal_minor=50249, current_value_minor=50500, status="partial"
    )
    _stub_reads(
        double, holding_seq=[SAMPLE, SAMPLE, after], account_seq=[BALANCE, BALANCE, BALANCE + 50000]
    )
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id
    first, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )
    key = _write_record(pending).idempotency_key
    calls = len(double.calls)

    second, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert first.status is second.status is WealthStatus.COMPLETED
    assert first.business_ids == second.business_ids
    assert len(_posts(double)) == 1
    assert _write_record(pending).idempotency_key == key
    # 第二次确认直接返回已存的回执：连一次银行读都没有再发生，更没有第二次写。
    assert len(double.calls) == calls


# ---------------------------------------------------------------- 适配器边界


def test_a_fund_write_without_a_code_is_never_sent(bank, double):
    """本仓库的适配器声明 `CLICK_AND_CODE`，所以它的写请求必须带验证码；缺了就失败关闭。

    它**不是**在偷偷带一个码去试，也不是把空码发出去赌一次：不发一次注定失败的写请求。
    """

    assert bank.write_authorization(REDEEM_OPERATION, 50000) is WriteAuthorization.CLICK_AND_CODE

    with pytest.raises(WealthBankError) as failure:
        bank.redeem(
            support.bound_context(),
            RedeemIntent(holding_id=SAMPLE_HOLDING, amount_minor=50000, to_account_id=ACCOUNT),
            "",
            "idem-1",
        )

    assert failure.value.code == ErrorCode.VALIDATION
    assert double.calls == []


def test_a_verification_failure_is_flagged_on_the_redeem_error(bank, double):
    """1008 在资金写路径上就是"验证码不对"：适配器把它标出来，运行时不必从错误码里猜。

    运行时不再按它累计尝试数（点击确认不带验证码），但这一位仍然是适配器对这次失败的如实分类。
    """

    double.on("POST", REDEEM, code=1008, status=409, data=None)

    with pytest.raises(WealthBankError) as failure:
        bank.redeem(
            support.bound_context(),
            RedeemIntent(holding_id=SAMPLE_HOLDING, amount_minor=50000, to_account_id=ACCOUNT),
            "000000",
            "idem-1",
        )

    assert failure.value.verification_failed is True
    assert failure.value.retryable is False


def test_the_redeem_slots_are_the_three_business_slots():
    assert REDEEM_SLOTS == ("holding_id", "amount_minor", "account_id")
    assert REDEEM_OPERATION == "redeem_product"


# ---------------------------------------------------------------- "刚才买的"


def test_the_recent_purchase_holding_is_the_target(double, clock):
    """先申购一笔，再说"赎回刚才买的"：目标是那笔新仓，不是样例持仓。"""

    session = {}
    pending = None
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on_sequence(
        "GET",
        ACCOUNTS,
        [support.account_payload(balance_minor=BALANCE, available_minor=BALANCE)],
        [support.account_payload(balance_minor=BALANCE, available_minor=BALANCE)],
        [support.account_payload(balance_minor=BALANCE - 200000, available_minor=BALANCE - 200000)],
        [support.account_payload(balance_minor=BALANCE - 200000, available_minor=BALANCE - 200000)],
        [support.account_payload(balance_minor=BALANCE - 200000, available_minor=BALANCE - 200000)],
    )
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER_PURCHASE,
        data=support.order_payload(
            order_id="order_purchase_1",
            holding_id=NEW_HOLDING,
            transaction_id="txn_purchase_1",
            amount_minor=200000,
        ),
    )
    new_holding = support.holding_payload(
        holding_id=NEW_HOLDING,
        principal_minor=200000,
        current_value_minor=200000,
        redeemable_minor=200000,
    )
    double.on("GET", HOLDING_NEW, data=new_holding)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(
            {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": 200000}
        ),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {"holding_id": {"recent": True}, "amount_minor": {"all": True}, "account_id": ACCOUNT}
        ),
    )
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    purchase, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="买 2000 元演示灵活理财"),
        session=session,
        pending=pending,
    )
    assert purchase.status is WealthStatus.WAITING_CONFIRMATION
    purchase_pending = purchase.pending.pending_id
    bought, session, pending = _confirmed(
        agent, context, purchase_pending, session=session, pending=pending
    )
    assert bought.status is WealthStatus.COMPLETED

    preview, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="赎回刚才买的"),
        session=session,
        pending=pending,
    )

    assert preview.status is WealthStatus.WAITING_CONFIRMATION
    snapshot = preview.facts["redeem_preview"]
    assert snapshot["holding_id"] == NEW_HOLDING
    assert snapshot["amount_minor"] == 200000
    assert snapshot["full"] is True
