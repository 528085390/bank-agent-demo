"""任务9：完成 Agent 申购流程，点击确认即真实提交银行并核实。

这一层证明的是**门禁与状态机**，全部走 HTTP 传输替身（离线层）：

1. **少一步就不写钱**：预览只是预览；没有可信的 `confirm`，`POST /products/purchase` 一次都
   不会发出去。旧 `verify` 入口已经停用，它连银行都不会碰。
2. **确认即提交**：确认绑定的是一份在预览时冻结的申购快照（产品、金额、账户、期限、幂等键）。
   改金额即换意图，旧待办不能再确认；重复确认复用同一份回执与同一个幂等键。
3. **能力不兼容不是交易失败**：本仓库的 `HttpWealthBank` 声明不支持点击确认（银行的写通道要求
   验证码），所以 `bank` 夹具上的确认会在**发出任何资金请求之前**返回
   `reason_code=confirmation_transport_unsupported`，待办保留到过期；成功路径用声明支持的
   适配器替身 `support.ClickConfirmationBank` 验证（交接文档 §6 的 D2 集成阻塞）。
4. **金额是整数分**：负数、零、非整数分、含糊金额都不能直接提交，由程序纠正或追问，而不是猜。
5. **银行拒绝与结果未知分开**：银行明确拒绝如实转达、待办保留；超时（结果未知）既不宣布成功
   也不宣布失败，只按原幂等键**读**一次银行（`kind=reconcile`）。业务拒绝不再累计任何"验证码
   错误次数"——验证码交互已经取消，锁定只由连续技术失败与越门禁事件驱动。
6. **成功话术与业务编号由程序生成**：只有拿到银行已提交回执，并且回执与意图一致、订单能在银行
   查到时，才说"模拟申购已完成"。
7. **写前刷新**：提交前重新读银行事实，条件变了就失败，绝不用已确认的金额去凑一次写。

真实余额、真实订单行与真实重启保留属于 `tests/wealth_integration/`（任务9 的端到端验收）。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.application.runtime import MAX_VERIFICATION_ATTEMPTS
from app.modules.wealth.contracts.bank import (
    PURCHASE_OPERATION,
    RUNTIME_OWNED_FIELDS,
    Account,
    Product,
    PurchaseIntent,
    PurchaseSnapshot,
    WealthBankError,
    WriteAuthorization,
)
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.domain.rules import AmountError, check_purchase, normalize_amount_minor
from app.modules.wealth.llm.decisions import (
    ACTIONS,
    GateViolation,
    parse_decision,
)
from app.modules.wealth.llm.toolcalls import tool_table
from app.modules.wealth.workflow.state import PURCHASE_SLOTS
from app.tools.protocol import ErrorCode
from tests.wealth import support


# 决策解析用的工具表：与工作流交给模型的是同一份（七个只读工具 + 八个控制类动作）。
def _table():
    return tool_table()


PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
PURCHASE = "/products/purchase"
ORDER = "/wealth/orders/order_purchase_1"
# 按幂等键核实（任务11）：结果未知时唯一的读出口。
LOOKUP = "/wealth/orders/by-idempotency-key"

CUSTOMER = support.DEMO_CUSTOMER
ACCOUNT = "a_wealth_main"
CODE = "246810"
# 银行配置的演示码是 123456；这个值银行一定会判错，用来验证"输错之后能重输"。
WRONG_CODE = "000000"
SLOTS = {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": 200000}
AMOUNT_MINOR = 200000
BALANCE_MINOR = 2000000
BALANCE_AFTER = BALANCE_MINOR - AMOUNT_MINOR


# ---------------------------------------------------------------------- 夹具


def _stub_reads(double, *, balances=(BALANCE_MINOR,)):
    """预览／刷新／写后要读的银行事实：产品列表、产品详情、账户（按调用顺序）、测评。

    账户余额按调用顺序取值（最后一个会被重复使用），所以一个用例可以表达"预览时 20000 元、
    写完之后 18000 元"。
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
    return double


def _stub_receipt(double, **overrides):
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(**overrides), status=201)
    return double


def _stub_order_readback(double):
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1",
            holding_id="h_purchase_1",
            transaction_id="txn_purchase_1",
            amount_minor=AMOUNT_MINOR,
        ),
    )
    return double


def _preview_replies(slots=None):
    """模型先查产品列表和账户列表（编号必须有据），再发起一次申购预览。"""

    return (
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(dict(slots or SLOTS)),
    )


def _agent(model, bank, clock):
    return build_wealth_agent(model, bank, clock)


def _click_bank(double):
    """一个**声明支持点击确认**的适配器替身，仍然走同一个传输替身。

    `bank` 夹具是真实的 `HttpWealthBank`，它如实回答"不支持点击确认"，所以确认会在发出任何
    资金请求之前被拒绝。成功路径必须换一个声明支持的适配器（它只改写能力声明，请求体仍由真实
    的 `BankClient` 组装，`double` 的预设与断言照常有效）。
    """

    return support.ClickConfirmationBank(double.client())


def _preview(agent, context, *, session=None, pending=None):
    """跑到待确认：模型只提取产品、金额、账户，其余由程序完成。"""

    return agent.handle(
        context,
        WealthRequest(kind="message", text="买 2000 元的演示灵活理财，用主账户"),
        session=session or {},
        pending=pending,
    )


def _confirmed(agent, context, pending_id, code=None, *, session=None, pending=None):
    """点确认提交这一笔。

    `code` 是用户在确认框上输入的验证码：银行的能力声明要它时（本仓库的 `HttpWealthBank`
    就是），这次确认必须带上；声明不要时（`_click_bank`）必须是 `None`。
    """

    return agent.handle(
        context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=code),
        session=session or {},
        pending=pending,
    )


def _reconciled(agent, context, pending_id, *, session=None, pending=None):
    """按原幂等键核实一笔结果未知的写：只读银行，不构成任何写授权。"""

    return agent.handle(
        context,
        WealthRequest(kind="reconcile", pending_id=pending_id),
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


def _write_record(pending):
    write = (pending or {}).get("write")
    return SimpleNamespace(**write) if write else None


def _paths(double) -> list[str]:
    return [call.path for call in double.calls]


def _posts(double):
    return [call for call in double.calls if call.method == "POST"]


# ---------------------------------------------------------------- 少一步就不写钱


def test_no_write_before_confirm(bank, double, clock):
    """预览只是预览：没有可信确认，`POST /products/purchase` 次数为 0。

    `bank` 夹具是真实的 `HttpWealthBank`：它声明 `CLICK_AND_CODE`，所以确认框会要求验证码，
    而这次点击**没带**验证码——运行时的正常路径在**发出任何资金请求之前**就拒绝它，并且能确定
    地说"没有提交"。这不是交易失败，也不是已经成交；待办保留到 TTL。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    preview, session, pending = _preview(agent, context, session=session, pending=pending)

    assert preview.status is WealthStatus.WAITING_CONFIRMATION
    assert preview.pending is not None
    assert _posts(double) == []
    # 确认卡片是程序按冻结快照生成的投影：金额与账户来自预览那一刻的事实。
    assert preview.confirmation is not None
    assert preview.confirmation.operation == PURCHASE_OPERATION
    assert preview.confirmation.amount_minor == AMOUNT_MINOR
    assert preview.confirmation.account_display == ACCOUNT
    assert preview.confirmation.expires_at == preview.pending.expires_at
    # 银行要验证码，所以卡片上必须有验证码输入：卡片和门禁是同一个答案。
    assert preview.confirmation.requires_code is True

    pending_id = preview.pending.pending_id
    key = _write_record(pending).idempotency_key
    expires_at = pending["expires_at"]

    confirmed, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert confirmed.status is WealthStatus.FAILED
    assert confirmed.error_code == ErrorCode.VALIDATION
    assert "需要验证码" in confirmed.summary
    assert confirmed.business_ids == {}
    assert _posts(double) == []
    # 待办与幂等键都留着：用户可以取消，也可以等接入方打通能力之后用同一个待办再确认。
    assert _write_record(pending) is not None
    assert _write_record(pending).receipt is None
    assert pending["expires_at"] == expires_at
    assert _write_record(pending).idempotency_key == key
    # 两次查询（产品、账户）加一次申购预检（产品、账户、测评），确认没有触发任何刷新或写请求。
    assert _paths(double) == [PRODUCTS, ACCOUNTS, PRODUCT, ACCOUNTS, LATEST]


def test_the_shipped_bank_submits_once_the_user_types_the_code(bank, double, clock):
    """真实适配器 + 用户输入的验证码 = 真的提交一次，且只提交一次。

    这是"点击确认接通到银行"的离线证明：预览要求验证码，用户在确认框上输入它，运行时把它
    原样送进写请求的 `mfa_code`，银行回执被比对后成为成交。没有替身参与能力声明。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()

    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id
    assert preview.confirmation.requires_code is True

    confirmed, session, pending = _confirmed(
        agent, context, pending_id, code=CODE, session=session, pending=pending
    )

    assert confirmed.status is WealthStatus.COMPLETED
    writes = _posts(double)
    assert len(writes) == 1
    assert writes[0].path == PURCHASE
    # 提交的是**用户输入的那个值**，不是配置里的演示码，也不是空。
    assert writes[0].body["mfa_code"] == CODE
    assert writes[0].body["confirm"] is True
    assert confirmed.business_ids["order_id"] == "order_purchase_1"


def test_a_retired_verify_never_reaches_the_bank(double, clock):
    """旧验证码入口已经停用：带不带验证码都一样，明确拒绝，银行一次都没被碰过。

    拒绝不消耗待办：用户改用点击确认还能把这笔提交出去，而且只写一次。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id
    before = len(double.calls)

    response, session, pending = agent.handle(
        context,
        WealthRequest(kind="verify", pending_id=pending_id, verification_code="  "),
        session=session,
        pending=pending,
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert "停用" in response.summary
    assert _posts(double) == []
    # 停用的入口连一次银行调用都没有：它不再假装自己是流程里的一步。
    assert len(double.calls) == before
    assert _write_record(pending).receipt is None

    retried, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert retried.status is WealthStatus.COMPLETED
    assert len(_posts(double)) == 1


def test_a_verify_before_confirm_does_not_reach_the_bank(bank, double, clock):
    """先 verify 后 confirm：旧入口直接停用，跳过确认这件事在结构上不可达。

    银行一次写都没收到，待办也没有被消耗——真正能提交这笔钱的只有可信确认。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _retired_verify(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []
    assert _write_record(pending) is not None


def test_the_verification_code_never_enters_the_model_context(bank, double, clock, caplog):
    """验证码是秘密：它进银行的写请求，但不进提示词、不进事实、不进响应、不进日志。

    这条用例走的是**真实路径**（银行要验证码，用户输入后提交），所以它同时证明两件事：
    提交确实带上了那个值，而且它没有从别的任何地方漏出去。
    """

    session = {}
    pending = None
    caplog.set_level(logging.INFO, logger="app.modules.wealth")
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, code=CODE, session=session, pending=pending
    )

    assert response.status is WealthStatus.COMPLETED
    # 秘密确实被送到了银行（写请求里带着它），而不是被悄悄丢掉。
    writes = _posts(double)
    assert len(writes) == 1
    assert writes[0].body["mfa_code"] == CODE
    # 但它不回显，也不进事实。
    assert CODE not in repr(response)
    assert CODE not in json.dumps(dict(response.facts), ensure_ascii=False)
    assert all(CODE not in prompt for prompt in model.prompts)
    # 日志只有脱敏动作摘要：验证码、请求文本与模型输出都不进去。
    records = [record for record in caplog.records if record.name.startswith("app.modules.wealth")]
    assert records, "至少应有一条 wealth audit 记录"
    for record in records:
        assert CODE not in record.getMessage()
        assert CODE not in str(record.args)


# ---------------------------------------------------------------- 快照与预览


def test_the_preview_states_product_amount_account_risk_and_rules(bank, double, clock):
    """预览由程序模板生成：产品、金额、账户、风险、规则和模拟交易提示都在里面。"""

    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR))
    model = support.ScriptModel(*_preview_replies())

    preview, _, _ = _preview(
        _agent(model, bank, clock), support.bound_context(), session={}, pending=None
    )

    text = preview.summary
    assert preview.pending.prompt == text
    assert "演示灵活理财" in text
    assert "2,000.00 元" in text
    assert ACCOUNT in text
    assert "低" in text
    assert "可随时赎回" in text
    assert "模拟" in text

    snapshot = preview.facts["purchase_preview"]
    assert snapshot["product_id"] == "p_demo_cash"
    assert snapshot["from_account_id"] == ACCOUNT
    assert snapshot["amount_minor"] == AMOUNT_MINOR
    assert snapshot["term_months"] == 0
    assert snapshot["balance_minor"] == BALANCE_MINOR
    assert snapshot["preview"] == text
    # 预览不产生业务记录：编号只出现在事实里，业务号是空的。
    assert preview.business_ids == {}


def test_the_purchase_snapshot_carries_no_authorization_fields():
    """冻结的快照只带业务槽位：身份、确认、验证码和幂等键都不在里面。"""

    snapshot = PurchaseSnapshot(
        product_id="p_demo_cash",
        product_name="演示灵活理财",
        from_account_id=ACCOUNT,
        amount_minor=AMOUNT_MINOR,
        term_months=0,
        risk_level="low",
        redeem_rule="flexible",
        balance_minor=BALANCE_MINOR,
        preview="请确认",
    )

    fields = set(snapshot.as_state())

    assert fields.isdisjoint(RUNTIME_OWNED_FIELDS)
    assert snapshot.intent().product_id == "p_demo_cash"
    assert snapshot.intent().amount_minor == AMOUNT_MINOR


def test_a_malformed_snapshot_is_rejected_rather_than_defaulted():
    """快照缺字段就是协议错误：不补默认值，也不带着半个意图去写钱。"""

    for payload in (
        {},
        {"product_id": "p_demo_cash"},
        {
            "product_id": "p_demo_cash",
            "product_name": "演示灵活理财",
            "from_account_id": ACCOUNT,
            "amount_minor": "200000",
            "term_months": 0,
            "risk_level": "low",
            "redeem_rule": "flexible",
            "balance_minor": BALANCE_MINOR,
            "preview": "请确认",
        },
    ):
        with pytest.raises(ValueError):
            PurchaseSnapshot.from_state(payload)


# ---------------------------------------------------------------- 金额规则


@pytest.mark.parametrize(
    "value", [0, -1, -200000, 2000.5, 200000.5, "2000", "两千块", True, None, []]
)
def test_amounts_that_are_not_whole_minor_are_rejected(value):
    """负数、零、非整数分（超过两位小数的元）、含糊金额都不能直接提交。"""

    with pytest.raises(AmountError):
        normalize_amount_minor(value)


@pytest.mark.parametrize("value", [1, 200000, 2000.0, 999999999])
def test_whole_minor_amounts_are_accepted(value):
    assert normalize_amount_minor(value) == int(value)


def test_an_ambiguous_amount_is_corrected_and_never_submitted(bank, double, clock):
    """模型把"两千块"当成金额：纠正一次，改问用户，绝不猜一个数字去提交。"""

    _stub_reads(double)
    model = support.ScriptModel(
        support.prepare_purchase({**SLOTS, "amount_minor": "两千块"}),
        support.ask_user("请问具体金额是多少？", ["amount_minor"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    # 含糊金额被参数校验拒掉，纠正提示里点名了那个字段；程序没有猜一个数字。
    assert "上一次输出不能使用" in model.prompts[1]
    assert "slots.amount_minor" in model.prompts[1]
    assert double.calls == []


def test_a_purchase_needs_all_three_slots(bank, double, clock):
    """缺账户就把缺项交回模型，由模型追问：程序不替用户挑一个账户去付款。"""

    _stub_reads(double)
    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.prepare_purchase({"product_id": "p_demo_cash", "amount_minor": AMOUNT_MINOR}),
        support.ask_user("请问用哪个账户付款？", ["account_id"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.summary == "请问用哪个账户付款？"
    # 缺项是节点报给模型的（needs_evidence），不是节点自己组织的问题。
    assert "申购预览还缺这些业务槽位" in model.prompts[2]
    assert "账户编号" in model.prompts[2]
    # 只查过产品列表：账户一个都没读，更没有任何写请求。
    assert _paths(double) == [PRODUCTS]


def test_the_plan_amount_is_not_the_order_amount(bank, double, clock):
    """推荐阶段的"计划投入金额"不是这一笔订单的金额。

    会话里已经有 20 万的计划金额时，模型只给出产品和账户：程序必须让模型追问"本次操作金额"，
    而不是把计划金额当成订单金额去预检（修复前就是这样，用户会看到关于 200,000.00 元的解释）。
    """

    session = {}
    pending = None
    _stub_reads(double)
    model = support.ScriptModel(
        support.set_slots({"plan_amount_minor": 20000000}),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase({"product_id": "p_demo_cash", "account_id": ACCOUNT}),
        support.ask_user("请问这一笔要买多少钱？", ["amount_minor"]),
    )
    agent = _agent(model, bank, clock)

    response, session, pending = _preview(
        agent, support.bound_context(), session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_INPUT
    assert response.pending is not None
    assert pending["missing"] == ("amount_minor",)
    # 缺的是"本次操作金额"，而且缺项事实里点名了它；计划金额没有被当成订单金额。
    assert "本次操作金额" in model.prompts[4]
    assert "200,000.00 元" not in model.prompts[4]
    # 记忆里也不留订单金额：否则下一笔没说金额时会"继承"上一笔。
    assert "amount_minor" not in (response.context_update or {}).get("answered", {})
    # 只读了产品与账户：没有拿计划金额去预检，更没有任何写请求。
    assert _paths(double) == [PRODUCTS, ACCOUNTS]


def test_a_purchase_slot_outside_the_three_is_corrected(bank, double, clock):
    """prepare_purchase 只接受产品、金额、账户：多带一个持仓编号就被纠正。"""

    model = support.ScriptModel(
        support.prepare_purchase({**SLOTS, "holding_id": "h_wealth_sample"}),
        support.ask_user("请问买哪只产品？", ["product_id"]),
    )
    agent = _agent(model, bank, clock)

    response, _, _ = _preview(agent, support.bound_context(), session={}, pending=None)

    assert response.status is WealthStatus.WAITING_INPUT
    assert "holding_id" in model.prompts[1]
    assert double.calls == []


def test_the_pre_check_blocks_a_product_above_the_assessment():
    """预检是硬约束：风险高于测评等级、起购不足都不放行。"""

    product = support.product_payload(risk_level="high", min_amount_minor=500000)
    account = support.account_payload(balance_minor=100000)

    blocked = check_purchase(
        Product.from_bank(product),
        account=Account.from_bank(account),
        risk_level="low",
        amount_minor=AMOUNT_MINOR,
        liquidity=None,
    )

    assert blocked.allowed is False
    assert any("高于您的测评等级" in reason for reason in blocked.reasons)
    assert any("起购" in reason for reason in blocked.reasons)


def test_the_model_cannot_supply_authorization_fields():
    """授权不是一个字段：在 arguments 里多带 user_id／confirm／幂等键会被直接拒绝。"""

    for extra in (
        {"user_id": "u_li"},
        {"confirm": True},
        {"mfa_code": CODE},
        {"idempotent_key": "idem-1"},
    ):
        with pytest.raises(GateViolation):
            parse_decision(
                *support.decision_input(
                    support.call("prepare_purchase", {"slots": {**SLOTS, **extra}})
                ),
                table=_table(),
            )

    # 授权字段当成工具的参数同样被拒：工具没有声明它。
    with pytest.raises(GateViolation):
        parse_decision(
            *support.decision_input(
                support.call("prepare_purchase", {"slots": SLOTS, "confirm": True})
            ),
            table=_table(),
        )

    decision = parse_decision(
        *support.decision_input(support.call("prepare_purchase", {"slots": SLOTS})),
        table=_table(),
    )

    assert decision.action == "prepare_purchase"
    assert decision.slots == SLOTS
    assert "prepare_purchase" in ACTIONS
    # 工具表里没有资金写：模型能表达的动作里根本没有"下单"这一项。
    assert "purchase" not in _table()


def test_the_bank_write_carries_the_runtime_key_and_the_bound_customer(double, clock):
    """写请求的身份、确认标志和幂等键都来自运行时，不是模型；点击确认不带验证码。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
    _stub_receipt(double)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    confirmed, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert confirmed.status is WealthStatus.COMPLETED
    call = _posts(double)[0]
    assert call.path == PURCHASE
    assert call.body == {
        "product_id": "p_demo_cash",
        "from_account_id": ACCOUNT,
        "amount_minor": AMOUNT_MINOR,
        "term_months": 0,
        "confirm": True,
        "idempotent_key": call.idempotency_key,
        "user_id": CUSTOMER,
    }
    # 点击确认就是授权本身：这次写请求里没有验证码字段。
    assert "mfa_code" not in call.body
    # 幂等键由运行时生成，且这一次意图只有一个。
    assert call.idempotency_key
    assert _write_record(pending).idempotency_key == call.idempotency_key


# ---------------------------------------------------------------- 确认绑定快照


def test_old_confirmation_cannot_buy_new_amount(bank, double, clock):
    """改金额即换意图：旧待办不能确认，也没有任何写请求。

    旧确认被拒的是**归属**（`reason_code` 为空），不是能力不兼容：两者都是 `INVALID_STATE`，
    但原因不同，调用方要能分开渲染。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR,))
    model = support.ScriptModel(
        *_preview_replies(),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase({**SLOTS, "amount_minor": 1000000}),
    )
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    first, session, pending = _preview(agent, context, session=session, pending=pending)
    old_pending_id = first.pending.pending_id

    second, session, pending = agent.handle(
        context,
        WealthRequest(kind="message", text="算了，改成一万块"),
        session=session,
        pending=pending,
    )

    assert second.status is WealthStatus.WAITING_CONFIRMATION
    assert second.pending.pending_id != old_pending_id
    assert second.facts["purchase_preview"]["amount_minor"] == 1000000

    stale = [
        _confirmed(agent, context, old_pending_id, code=CODE, session=session, pending=pending)[0],
        _retired_verify(agent, context, old_pending_id, session=session, pending=pending)[0],
    ]

    for response in stale:
        assert response.status is WealthStatus.FAILED
        assert response.error_code == ErrorCode.INVALID_STATE
        assert response.reason_code is None
        assert response.business_ids == {}
    assert _posts(double) == []
    assert CODE not in repr(stale[1])
    # 旧待办与它的幂等键一起作废：它已经不是"用户正在确认的那一笔"。
    assert session["pending_id"] != old_pending_id


def test_a_repeated_confirmation_does_not_extend_the_pending(double, clock):
    """重复点击确认不延长时限、不换幂等键，也不写第二次：回执已经在手就直接返回它。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
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

    assert first.status is WealthStatus.COMPLETED
    key = _write_record(pending).idempotency_key
    expires_at = pending["expires_at"]

    again, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert again.status is WealthStatus.COMPLETED
    assert again.business_ids == first.business_ids
    assert _write_record(pending).idempotency_key == key
    assert pending["expires_at"] == expires_at
    assert len(_posts(double)) == 1


# ---------------------------------------------------------------- 银行拒绝与未知结果


def test_a_wrong_code_re_offers_the_preview_and_moves_no_money(bank, double, clock):
    """验证码被银行判错（1008）：没有提交，待办与幂等键都留着，预览重新交回来让用户重输。

    银行明确没有受理（所以不是结果未知），而用户下一步要做的正是重输一个码——所以流程不结束，
    重新等确认的是**同一笔**：幂等键没换，重输正确之后银行最多也只会执行一次。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR))
    double.on("POST", PURCHASE, code=1008, status=409, data=None)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id
    key = _write_record(pending).idempotency_key

    response, session, pending = _confirmed(
        agent, context, pending_id, code=WRONG_CODE, session=session, pending=pending
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert response.pending.pending_id == pending_id
    assert response.confirmation.requires_code is True
    assert "验证码" in response.summary
    assert response.business_ids == {}
    assert "order_id" not in json.dumps(dict(response.facts), ensure_ascii=False)
    # 银行确实收到了这次写请求，送出去的正是用户输入的那个值——不是空，也不是配置里的演示码。
    assert len(_posts(double)) == 1
    assert _posts(double)[0].body["mfa_code"] == WRONG_CODE
    # 没有任何成交事实，待办也没有被消耗。
    assert _write_record(pending).receipt is None
    assert _write_record(pending) is not None
    # 重来的是同一笔：幂等键没换。
    assert _write_record(pending).idempotency_key == key


def test_repeated_wrong_codes_do_not_lock_the_writes(bank, double, clock):
    """业务拒绝不驱动锁定：连续输错验证码之后，用户在本进程的写操作仍然没有被锁定。

    锁定的两个条件（连续技术失败、累计越门禁）在 `test_safety.py` 里各自验证；"输错验证码"
    只是银行的一次业务结论，不算攻击，也不算技术失败。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR,) * (MAX_VERIFICATION_ATTEMPTS + 2))
    double.on("POST", PURCHASE, code=1008, status=409, data=None)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, bank, clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id
    key = _write_record(pending).idempotency_key

    responses = [
        _confirmed(agent, context, pending_id, code=WRONG_CODE, session=session, pending=pending)[0]
        for _ in range(MAX_VERIFICATION_ATTEMPTS)
    ]

    assert [response.status for response in responses] == [
        WealthStatus.WAITING_CONFIRMATION
    ] * MAX_VERIFICATION_ATTEMPTS
    assert len(_posts(double)) == MAX_VERIFICATION_ATTEMPTS
    assert context.user_id not in agent._locked_users
    assert agent._technical_failures[context.user_id] == 0
    assert agent._bypass_events == {}
    # 每一次重输都还是同一笔：一个意图只有一个幂等键。
    assert {call.idempotency_key for call in _posts(double)} == {key}
    assert _write_record(pending).idempotency_key == key


def test_an_unknown_outcome_is_not_a_success(double, clock):
    """写请求超时（结果未知）：不返回成功，也不宣布失败，只按原键核实。

    核实用的是**原**幂等键（换一个键就等于重新下单），待办与幂等键都留着，并把核实引用交给
    调用方（计划任务11）。`kind=reconcile` 只读银行，不会重发写请求。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR,))
    double.on_failure("POST", PURCHASE, httpx.ReadTimeout("slow"))
    double.on("GET", LOOKUP, data=support.lookup_miss(PURCHASE_OPERATION, "idem-unused"))
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
    assert _write_record(pending).receipt is None
    key = _write_record(pending).idempotency_key
    assert key
    # 只有一次写请求；核实是一次读，用的是同一个键，没有第二次写。
    assert len(_posts(double)) == 1
    lookups = [call for call in double.calls if call.path == LOOKUP]
    assert len(lookups) == 1
    assert lookups[0].query["idempotent_key"] == key
    assert lookups[0].query["operation_type"] == PURCHASE_OPERATION
    # 核实引用交给受控调用方：它只用于读银行，不是写授权。
    reference = {
        "operation_type": PURCHASE_OPERATION,
        "idempotent_key": key,
        "found": False,
        "order_id": None,
    }
    assert response.facts["reconciliation"] == reference

    # 再用 `kind=reconcile` 显式核实一次：仍然只读、仍然是未知，写请求没有第二次。
    reconciled, session, pending = _reconciled(
        agent, context, pending_id, session=session, pending=pending
    )

    assert reconciled.status is WealthStatus.UNKNOWN
    assert reconciled.error_code == ErrorCode.TRANSACTION_UNKNOWN
    assert reconciled.facts["reconciliation"] == reference
    assert len(_posts(double)) == 1
    lookups = [call for call in double.calls if call.path == LOOKUP]
    assert len(lookups) == 2
    assert {call.query["idempotent_key"] for call in lookups} == {key}


def test_repeating_a_confirm_after_an_unknown_reconciles_instead_of_crashing(double, clock):
    """C09：未知之后再次 confirm 只核实，绝不重下单，也不许退化成内部错误。

    待办已经不在了（过期、重启，或调用方只递回了会话），但会话上还留着那笔未核实的写，
    而且这次点击带的正是它的编号：唯一正确的回答是按原键核实（UNKNOWN）。
    如果这里把响应当成 `(response, pending)` 拆包失败，用户拿到的就会是"本次处理未能完成"
    ——在一笔可能已经成交的资金写面前，这是最不该丢信息的地方。
    """

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR,))
    double.on_failure("POST", PURCHASE, httpx.ReadTimeout("slow"))
    double.on("GET", LOOKUP, data=support.lookup_miss(PURCHASE_OPERATION, "idem-unused"))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    unknown, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )
    assert unknown.status is WealthStatus.UNKNOWN

    # 待办这一份丢了，但点击带的还是那笔未核实写的编号：按原键核实，不是"没有待确认的操作"。
    again, session, pending = _confirmed(agent, context, pending_id, session=session, pending=None)

    assert again.status is WealthStatus.UNKNOWN
    assert again.error_code == ErrorCode.TRANSACTION_UNKNOWN
    assert again.error_code is not ErrorCode.INTERNAL
    assert "核实" in again.summary
    # 只发过一次写请求：核实是只读的。
    assert len(_posts(double)) == 1


def test_the_refresh_before_the_write_blocks_a_changed_condition(double, clock):
    """写前刷新发现余额不够：用新事实失败，不用已确认的金额去凑一次写。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, 100))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert response.reason_code is None
    assert "余额" in response.summary
    assert _posts(double) == []
    # 待办已经作废：条件变了就要重新预览、重新确认。
    assert pending is None
    assert (
        _confirmed(agent, context, pending_id, session=session, pending=pending)[0].status
        is WealthStatus.FAILED
    )


# ---------------------------------------------------------------- 成功与重复


def test_the_success_summary_comes_from_the_receipt(double, clock):
    """成功话术由程序模板生成：关键金额与业务编号都来自银行回执。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
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
    assert "order_purchase_1" in response.summary
    assert "h_purchase_1" in response.summary
    assert "txn_purchase_1" in response.summary
    assert "2,000.00 元" in response.summary
    assert response.business_ids == {
        "order_id": "order_purchase_1",
        "holding_id": "h_purchase_1",
        "transaction_id": "txn_purchase_1",
        "product_id": "p_demo_cash",
    }
    receipt = response.facts["purchase_receipt"]
    assert receipt["amount_minor"] == AMOUNT_MINOR
    assert receipt["principal_minor"] == AMOUNT_MINOR
    assert receipt["status"] == "completed"
    # 写后核实：订单从银行重新查到，账户余额也重新读到。
    assert response.facts["purchase_verified"] is True
    assert response.facts["orders"][0]["order_id"] == "order_purchase_1"
    assert response.facts["accounts"][0]["balance_minor"] == BALANCE_AFTER
    assert model.calls == 3  # 模型只负责查事实和提取槽位；成交话术不是它写的


def test_a_successful_write_is_still_reported_when_the_read_back_fails(double, clock):
    """回执到手但写后复核读不回来：仍然如实报告成交，并说明没能在银行重新查到。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR))
    _stub_receipt(double)
    double.on_failure("GET", ORDER, httpx.ConnectError("refused"))
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(bank=_click_bank(double), model=model, clock=clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.facts["purchase_verified"] is False
    assert "未能在银行重新查到" in response.summary
    assert "order_purchase_1" in response.summary


def test_an_inconsistent_receipt_is_not_reported_as_success(double, clock):
    """回执与意图不一致（金额或产品对不上）：宁可失败，也不宣布成交。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR,))
    _stub_receipt(double, amount_minor=AMOUNT_MINOR + 1)
    _stub_order_readback(double)
    model = support.ScriptModel(*_preview_replies())
    agent = _agent(model, _click_bank(double), clock)
    context = support.bound_context()
    preview, session, pending = _preview(agent, context, session=session, pending=pending)
    pending_id = preview.pending.pending_id

    response, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.PROTOCOL
    assert "模拟申购已完成" not in response.summary


def test_duplicate_confirmation_returns_the_same_order(double, clock):
    """重复点击确认仍是一笔银行订单：复用同一个幂等键和同一份回执。"""

    session = {}
    pending = None
    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_AFTER))
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

    second, session, pending = _confirmed(
        agent, context, pending_id, session=session, pending=pending
    )

    assert first.status is second.status is WealthStatus.COMPLETED
    assert first.business_ids == second.business_ids
    assert len(_posts(double)) == 1
    assert _posts(double)[0].idempotency_key == key
    assert _write_record(pending).idempotency_key == key


# ---------------------------------------------------------------- 适配器边界


def test_a_fund_write_without_a_code_is_never_sent(bank, double):
    """没有验证码的写请求根本不发出去：不发一次注定失败的请求，也不消耗银行的失败计数。

    这是**真实适配器**的门禁（它声明 `CLICK_AND_CODE`，所以 `mfa_code` 必填）。运行时的正常
    路径在发出任何资金请求之前就会因为"这次确认没带验证码"而拒绝，所以这里直接调用适配器，
    单独验证这道闸门。
    """

    # 本仓库的银行能力如实回答"点击 + 验证码"——这正是确认框会要求输入验证码的原因。
    assert bank.write_authorization(PURCHASE_OPERATION, AMOUNT_MINOR) is (
        WriteAuthorization.CLICK_AND_CODE
    )

    with pytest.raises(WealthBankError) as failure:
        bank.purchase(
            support.bound_context(),
            PurchaseIntent(
                product_id="p_demo_cash", from_account_id=ACCOUNT, amount_minor=AMOUNT_MINOR
            ),
            "",
            "idem-1",
        )

    assert failure.value.code == ErrorCode.VALIDATION
    assert double.calls == []


def test_a_verification_failure_is_flagged_on_the_error(bank, double):
    """1008 在资金写路径上就是"验证码不对"：适配器把它标出来，运行时不必猜。

    缺码／还需要确认是 1007，是另一回事；只有银行明说 1008 时这一位才为真，运行时据此重新
    交回预览让用户重输，而不是把它当成一次通用拒绝。
    """

    double.on("POST", PURCHASE, code=1008, status=409, data=None)

    with pytest.raises(WealthBankError) as failure:
        bank.purchase(
            support.bound_context(),
            PurchaseIntent(
                product_id="p_demo_cash", from_account_id=ACCOUNT, amount_minor=AMOUNT_MINOR
            ),
            "000000",
            "idem-1",
        )

    assert failure.value.verification_failed is True
    assert failure.value.retryable is False
    assert failure.value.outcome_unknown is False


def test_the_purchase_slots_are_the_three_business_slots():
    assert PURCHASE_SLOTS == ("product_id", "amount_minor", "account_id")
    assert PURCHASE_OPERATION == "purchase_product"
