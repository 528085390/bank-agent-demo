"""任务10 的真实 HTTP 端到端：Agent 的收益查询与赎回闭环 ↔ 运行中的银行服务。

只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行，而且那个服务必须连着 **M1 专用测试 schema**
（已执行 `scripts/seed_wealth_demo.py`）。没有环境变量就跳过：默认不碰开发共享服务，也不在
别人的数据上跑测试。

    $env:WEALTH_TEST_BANK_URL = "http://127.0.0.1:8001/api"   # 指向测试 schema 的银行进程
    $env:WEALTH_TEST_BANK_CUSTOMER = "u_wealth"               # 可选，默认 M1 演示客户
    $env:WEALTH_TEST_BANK_CODE = "123456"                     # 可选，默认银行配置的演示码
    backend/.venv/Scripts/python.exe -m pytest tests/wealth_integration -q

这里断言的是**真实余额、真实持仓和真实赎回订单行**，不是替身：

- 收益查询只读：读持仓就展示本金、当前价值、持有收益和已实现收益，银行没有任何计息写；
- 预览与确认阶段银行没有任何资金变动；验证码错误时银行收到请求但余额与持仓不变；
- 部分赎回之后余额正好增加赎回金额、持仓本金按银行分摊减少、赎回订单能查到；
- 再全部赎回之后余额回到起点、新仓清零，**样例持仓不受影响**；
- 样例持仓的一半赎回：银行按 floor(P×R/V) 分摊出释放本金与实现收益，且与持久化字段一致；
- 未到期的定期持仓：Agent 预检先拦住，直接调银行接口也会被拒绝，且不产生任何资金变动。

**顺序敏感**：赎回会真的改变演示数据，所以本模块的用例按文件顺序执行——先做只读的收益查询，
再赎回**新仓**（并断言样例仓不受影响），最后才赎回样例仓（那一步之后样例仓本金只剩一半）。

**演示数据是有限的**：`a_wealth_main` 只有 20000 元，`seed_wealth_demo.py` 不会重复入金。
需要回到初始状态时，重建专用 schema（drop → `alembic upgrade head` → `POST /api/seed/reset`
→ `scripts/seed_wealth_demo.py`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text

from app.integration.bank_client import BankClient
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.contracts.bank import Holding, RedeemIntent, RiskProfile, WealthBankError
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthResponse, WealthStatus
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import TrustedContext
from tests.wealth import support

# 跨轮状态由用例自己拿着：运行时不再替调用方记会话与待办（任务 3）。
SESSION = support.SessionCarrier()

BANK_URL_ENV = "WEALTH_TEST_BANK_URL"
CUSTOMER_ENV = "WEALTH_TEST_BANK_CUSTOMER"
CODE_ENV = "WEALTH_TEST_BANK_CODE"
SCHEMA_ENV = "WEALTH_TEST_BANK_SCHEMA"
DATABASE_URL_ENV = "WEALTH_TEST_BANK_DATABASE_URL"

# bank_backend/app/storage/wealth_seed.py 的 WEALTH_USER 与 M1 演示数据。
DEFAULT_CUSTOMER = "u_wealth"
DEMO_CASH_PRODUCT = "p_demo_cash"
DEMO_STABLE_PRODUCT = "p_demo_stable"
DEMO_MAIN_ACCOUNT = "a_wealth_main"
DEMO_SAMPLE_HOLDING = "h_wealth_sample"

# bank_backend/app/core/config.py 的 mock_mfa_code 默认值。
DEFAULT_CODE = "123456"
WRONG_CODE = "000000"

# 样例持仓：本金 1000 元、价值 1005 元。
SAMPLE_PRINCIPAL_MINOR = 100000
SAMPLE_VALUE_MINOR = 100500

PURCHASE_AMOUNT_MINOR = 200000  # 新仓 2000 元
PARTIAL_AMOUNT_MINOR = 50000  # 部分赎回 500 元
REMAINING_AMOUNT_MINOR = 150000  # 新仓赎回 500 元之后剩下的 1500 元
FIXED_TERM_AMOUNT_MINOR = 100000  # 定期产品起购 1000 元
HALF_OF_SAMPLE_MINOR = 50250  # 样例持仓的一半（100500／2，向下取整到分）

# 五题总分 6：medium。低风险的演示灵活理财与中风险的演示稳健理财都在允许范围内。
ANSWERS = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 2}

PURCHASE_SLOTS = {
    "product_id": DEMO_CASH_PRODUCT,
    "account_id": DEMO_MAIN_ACCOUNT,
    "amount_minor": PURCHASE_AMOUNT_MINOR,
}
FIXED_TERM_SLOTS = {
    "product_id": DEMO_STABLE_PRODUCT,
    "account_id": DEMO_MAIN_ACCOUNT,
    "amount_minor": FIXED_TERM_AMOUNT_MINOR,
}


# ---------------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def live_bank() -> HttpWealthBank:
    url = os.getenv(BANK_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"未设置 {BANK_URL_ENV}：本用例只对连着 M1 专用测试 schema 的银行服务运行")
    client = BankClient(url, connect_timeout=5.0, read_timeout=15.0)
    try:
        yield HttpWealthBank(client)
    finally:
        client.close()


@pytest.fixture(scope="module")
def live_context() -> TrustedContext:
    customer = os.getenv(CUSTOMER_ENV, "").strip() or DEFAULT_CUSTOMER
    return TrustedContext(
        user_id="agent_live_redeem", session_id="sess_live_redeem", bank_customer_id=customer
    )


@pytest.fixture(scope="module")
def demo_code() -> str:
    return os.getenv(CODE_ENV, "").strip() or DEFAULT_CODE


@pytest.fixture(scope="module")
def assessment(live_bank: HttpWealthBank, live_context: TrustedContext) -> RiskProfile:
    """用同一套 HTTP 接口准备一份有效测评（不是直接写库，也不是本地伪造等级）。"""

    profile = live_bank.submit_risk_assessment(live_context, ANSWERS)

    assert profile.valid is True, "银行返回的测评必须有效，否则申购会被银行拒绝"
    assert profile.risk_level == "medium"
    return profile


def _agent(live_bank: HttpWealthBank, *replies: str) -> object:
    """一个脚本模型 + 真实银行适配器：模型只提取槽位，其余全由程序与银行完成。"""

    return build_wealth_agent(support.ScriptModel(*replies), live_bank, SystemClock())


def _balance(live_bank: HttpWealthBank, context: TrustedContext) -> int:
    accounts = {item.account_id: item for item in live_bank.list_accounts(context)}
    return accounts[DEMO_MAIN_ACCOUNT].balance_minor


def _holdings(live_bank: HttpWealthBank, context: TrustedContext) -> dict[str, Holding]:
    return {item.holding_id: item for item in live_bank.list_holdings(context)}


def _purchase_replies(slots):
    return (
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(dict(slots)),
    )


def _redeem_replies(holding_id, amount, account_id=DEMO_MAIN_ACCOUNT):
    return (
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {"holding_id": holding_id, "amount_minor": amount, "account_id": account_id}
        ),
    )


def _run_purchase(agent, context, slots, code) -> WealthResponse:
    preview = SESSION.handle(agent, context, WealthRequest(kind="message", text="买一笔演示理财"))
    assert preview.status is WealthStatus.WAITING_CONFIRMATION, preview.summary
    pending_id = preview.pending.pending_id
    return SESSION.handle(
        agent,
        context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=code),
    )


def _run_redeem(agent, context, holding_id, amount, code) -> tuple[WealthResponse, WealthResponse]:
    preview = SESSION.handle(
        agent, context, WealthRequest(kind="message", text=f"赎回持仓 {holding_id} 的 {amount} 分")
    )
    assert preview.status is WealthStatus.WAITING_CONFIRMATION, preview.summary
    pending_id = preview.pending.pending_id
    verified = SESSION.handle(
        agent,
        context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=code),
    )
    return preview, verified


@dataclass(frozen=True, slots=True)
class NewHolding:
    """先买一笔 2000 元的新仓：赎回的收益与持久化都以它为准。"""

    holding_id: str
    order_id: str
    balance_after_purchase: int


@pytest.fixture(scope="module")
def purchased(live_bank, live_context, assessment, demo_code) -> NewHolding:
    """跑一次完整的真实申购，得到一个可赎回的新仓。"""

    agent = _agent(live_bank, *_purchase_replies(PURCHASE_SLOTS))
    verified = _run_purchase(agent, live_context, PURCHASE_SLOTS, demo_code)

    assert verified.status is WealthStatus.COMPLETED, verified.summary
    return NewHolding(
        holding_id=verified.business_ids["holding_id"],
        order_id=verified.business_ids["order_id"],
        balance_after_purchase=_balance(live_bank, live_context),
    )


@dataclass(frozen=True, slots=True)
class PartialRedeem:
    """新仓的一次真实部分赎回留下的证据，后面的用例共用它。"""

    agent: object
    preview: WealthResponse
    verified: WealthResponse
    holding_id: str
    balance_before: int
    balance_after: int
    sample_before: Holding


@pytest.fixture(scope="module")
def partial(live_bank, live_context, purchased, demo_code) -> PartialRedeem:
    """赎回新仓的 500 元（部分赎回），并记下样例仓在这一刻的样子。"""

    before = _balance(live_bank, live_context)
    sample_before = _holdings(live_bank, live_context)[DEMO_SAMPLE_HOLDING]
    agent = _agent(live_bank, *_redeem_replies(purchased.holding_id, PARTIAL_AMOUNT_MINOR))
    preview, verified = _run_redeem(
        agent, live_context, purchased.holding_id, PARTIAL_AMOUNT_MINOR, demo_code
    )

    assert verified.status is WealthStatus.COMPLETED, verified.summary
    return PartialRedeem(
        agent=agent,
        preview=preview,
        verified=verified,
        holding_id=purchased.holding_id,
        balance_before=before,
        balance_after=_balance(live_bank, live_context),
        sample_before=sample_before,
    )


# ---------------------------------------------------------------- 收益查询只读


def test_income_read_is_not_interest_write(live_bank, live_context):
    """原样例持仓：本金 1000 元、价值 1005 元 → 持有收益 5 元；银行没有任何计息写。"""

    before = _holdings(live_bank, live_context)

    agent = _agent(
        live_bank,
        support.call_read("wealth_holding", {"holding_id": DEMO_SAMPLE_HOLDING}),
        support.finish(
            "样例持仓本金 1,000.00 元，持有收益 5.00 元，已实现收益 0.00 元。",
            cited_ids=[DEMO_SAMPLE_HOLDING],
        ),
    )
    response = SESSION.handle(
        agent, live_context, WealthRequest(kind="message", text="我的持仓收益是多少？")
    )

    assert response.status is WealthStatus.COMPLETED, response.summary
    holding = response.facts["holdings"][0]
    assert holding["principal_minor"] == SAMPLE_PRINCIPAL_MINOR
    assert holding["current_value_minor"] == SAMPLE_VALUE_MINOR
    assert holding["accrued_interest_minor"] == 500
    assert holding["realized_income_minor"] == 0
    # 只读：银行那边的持仓与余额一点没变。
    assert _holdings(live_bank, live_context) == before


# ---------------------------------------------------------------- 部分／全部赎回


def test_partial_redeem_persists(live_bank, live_context, partial):
    """新仓 2000 元赎 500 元：余额 +500、持仓本金 1500、赎回订单存在，样例仓不变。"""

    receipt = partial.verified.facts["redeem_receipt"]
    assert partial.preview.facts["redeem_preview"]["amount_minor"] == PARTIAL_AMOUNT_MINOR
    assert receipt["amount_minor"] == PARTIAL_AMOUNT_MINOR
    assert receipt["holding_id"] == partial.holding_id
    assert receipt["status"] == "completed"
    # 新仓本金与价值都是 2000 元，所以这次分摊是"全额本金、零收益"。
    assert receipt["principal_redeemed_minor"] == PARTIAL_AMOUNT_MINOR
    assert receipt["interest_minor"] == 0

    assert partial.balance_after == partial.balance_before + PARTIAL_AMOUNT_MINOR
    holding = _holdings(live_bank, live_context)[partial.holding_id]
    assert holding.principal_minor == PURCHASE_AMOUNT_MINOR - PARTIAL_AMOUNT_MINOR
    assert holding.current_value_minor == PURCHASE_AMOUNT_MINOR - PARTIAL_AMOUNT_MINOR
    assert holding.status == "partial"

    order = live_bank.get_order(live_context, receipt["order_id"])
    assert order.order_type == "product_redeem"
    assert order.holding_id == partial.holding_id
    assert order.amount_minor == PARTIAL_AMOUNT_MINOR
    assert order.status == "completed"
    # 旧样例持仓保持独立：这笔赎回没有碰它。
    assert _holdings(live_bank, live_context)[DEMO_SAMPLE_HOLDING] == partial.sample_before


def test_duplicate_confirm_returns_the_same_order(live_bank, live_context, partial, demo_code):
    """重复的确认事件仍是一笔银行订单：同一个订单号，余额不再变一次。"""

    again = SESSION.handle(
        partial.agent,
        live_context,
        WealthRequest(
            kind="confirm",
            pending_id=partial.preview.pending.pending_id,
            verification_code=demo_code,
        ),
    )

    assert again.status is WealthStatus.COMPLETED
    assert again.business_ids["order_id"] == partial.verified.business_ids["order_id"]
    assert _balance(live_bank, live_context) == partial.balance_after


def test_wrong_code_does_not_move_money(live_bank, live_context, purchased, demo_code):
    """验证码错误：银行收到了写请求，但余额与持仓没有变化，而且预览重新交回来让用户重输。"""

    before = _balance(live_bank, live_context)
    holdings_before = _holdings(live_bank, live_context)

    agent = _agent(live_bank, *_redeem_replies(purchased.holding_id, PARTIAL_AMOUNT_MINOR))
    preview = SESSION.handle(agent, live_context, WealthRequest(kind="message", text="赎回一部分"))
    assert preview.status is WealthStatus.WAITING_CONFIRMATION, preview.summary
    pending_id = preview.pending.pending_id

    response = SESSION.handle(
        agent,
        live_context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=WRONG_CODE),
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert response.pending.pending_id == pending_id
    assert response.business_ids == {}
    assert _balance(live_bank, live_context) == before
    assert _holdings(live_bank, live_context) == holdings_before

    # 重输正确的验证码就能成交：银行按同一个幂等键去重，所以不会有两笔。
    retried = SESSION.handle(
        agent,
        live_context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=demo_code),
    )

    assert retried.status is WealthStatus.COMPLETED, retried.summary
    assert retried.business_ids["order_id"]
    # 到账了：余额比赎回前多。
    assert _balance(live_bank, live_context) > before


def test_full_redeem_persists(live_bank, live_context, purchased, partial, demo_code):
    """再赎剩下的 1500 元：余额回到申购前、新仓清零，样例仓仍然不变。"""

    before = _balance(live_bank, live_context)
    sample_before = _holdings(live_bank, live_context)[DEMO_SAMPLE_HOLDING]

    agent = _agent(live_bank, *_redeem_replies(purchased.holding_id, REMAINING_AMOUNT_MINOR))
    _, verified = _run_redeem(
        agent, live_context, purchased.holding_id, REMAINING_AMOUNT_MINOR, demo_code
    )

    assert verified.status is WealthStatus.COMPLETED, verified.summary
    receipt = verified.facts["redeem_receipt"]
    assert receipt["amount_minor"] == REMAINING_AMOUNT_MINOR
    assert receipt["holding_status"] == "redeemed"
    assert receipt["remaining_principal_minor"] == 0
    assert receipt["remaining_value_minor"] == 0

    assert _balance(live_bank, live_context) == before + REMAINING_AMOUNT_MINOR
    holding = _holdings(live_bank, live_context)[purchased.holding_id]
    assert holding.principal_minor == 0
    assert holding.current_value_minor == 0
    assert holding.status == "redeemed"
    # 全部赎回之后仍然保留订单引用，不是"删掉本地持仓就说成功"。
    assert live_bank.get_order(live_context, receipt["order_id"]).order_id == receipt["order_id"]
    assert _holdings(live_bank, live_context)[DEMO_SAMPLE_HOLDING] == sample_before


# ---------------------------------------------------------------- 定期产品不可提前赎回


def test_fixed_term_redeem_is_blocked_and_refused(live_bank, live_context, assessment, demo_code):
    """未到期的定期持仓：Agent 预检先拦住；直接调银行接口也被拒绝，且不产生资金变动。"""

    agent = _agent(live_bank, *_purchase_replies(FIXED_TERM_SLOTS))
    verified = _run_purchase(agent, live_context, FIXED_TERM_SLOTS, demo_code)
    assert verified.status is WealthStatus.COMPLETED, verified.summary
    fixed_holding = verified.business_ids["holding_id"]

    # 银行的持仓视图：未到期的定期产品可赎回额是 0。
    holding = live_bank.get_holding(live_context, fixed_holding)
    assert holding.redeemable_minor == 0
    assert holding.status == "holding"

    # Agent 侧：预检拦住，不发任何写请求（模型如实解释）。
    blocked_agent = _agent(
        live_bank,
        support.call_read("wealth_holdings"),
        support.call_read("wealth_accounts"),
        support.prepare_redeem(
            {
                "holding_id": fixed_holding,
                "amount_minor": FIXED_TERM_AMOUNT_MINOR,
                "account_id": DEMO_MAIN_ACCOUNT,
            }
        ),
        support.finish("这笔定期持仓还没到期，现在不能赎回。", cited_ids=[fixed_holding]),
    )
    balance_before = _balance(live_bank, live_context)
    blocked = SESSION.handle(
        blocked_agent, live_context, WealthRequest(kind="message", text="赎回这笔定期持仓")
    )
    assert blocked.status is WealthStatus.COMPLETED, blocked.summary
    assert "不可赎回" in blocked_agent._model.prompts[3]
    assert _balance(live_bank, live_context) == balance_before

    # 银行侧：即使绕过预检直接提交，银行也会拒绝，且余额与持仓不变。
    with pytest.raises(WealthBankError):
        live_bank.redeem(
            live_context,
            RedeemIntent(
                holding_id=fixed_holding,
                amount_minor=FIXED_TERM_AMOUNT_MINOR,
                to_account_id=DEMO_MAIN_ACCOUNT,
            ),
            demo_code,
            "idem-fixed-term-redeem",
        )
    assert _balance(live_bank, live_context) == balance_before
    assert (
        live_bank.get_holding(live_context, fixed_holding).principal_minor
        == FIXED_TERM_AMOUNT_MINOR
    )


# ---------------------------------------------------------------- 银行分摊与持久化


def test_bank_returns_principal_income_allocation(live_bank, live_context, demo_code):
    """样例持仓的一半：P100000／V100500／R50250 → 释放本金 50000、实现收益 250。

    **这一步之后样例持仓只剩一半本金**，所以它排在本模块最后（除了直连数据库的核验）。
    """

    agent = _agent(live_bank, *_redeem_replies(DEMO_SAMPLE_HOLDING, HALF_OF_SAMPLE_MINOR))
    _, verified = _run_redeem(
        agent, live_context, DEMO_SAMPLE_HOLDING, HALF_OF_SAMPLE_MINOR, demo_code
    )

    assert verified.status is WealthStatus.COMPLETED, verified.summary
    receipt = verified.facts["redeem_receipt"]
    assert receipt["amount_minor"] == HALF_OF_SAMPLE_MINOR
    assert receipt["principal_redeemed_minor"] == 50000
    assert receipt["interest_minor"] == 250
    assert receipt["remaining_principal_minor"] == 50000
    assert receipt["remaining_value_minor"] == 50250

    holding = live_bank.get_holding(live_context, DEMO_SAMPLE_HOLDING)
    assert holding.principal_minor == 50000
    assert holding.current_value_minor == 50250
    assert holding.status == "partial"


def test_the_redeem_row_is_in_the_database(live_bank, live_context, purchased, partial):
    """直接查库核对：赎回订单、持仓分摊字段和余额变动真的落了库（直连 SQL 只用于测试核验）。"""

    dsn = os.getenv(DATABASE_URL_ENV, "").strip()
    schema = os.getenv(SCHEMA_ENV, "").strip()
    if not dsn or not schema:
        pytest.skip(f"未设置 {SCHEMA_ENV}／{DATABASE_URL_ENV}：跳过直连数据库的核验")

    engine = create_engine(dsn, connect_args={"options": f"-c search_path={schema}"})
    try:
        with engine.connect() as connection:
            orders = connection.execute(
                text(
                    "select id, order_type, holding_id, to_account_id, amount_minor, status, details"
                    " from orders where holding_id = :holding_id and order_type = 'product_redeem'"
                    " order by created_at"
                ),
                {"holding_id": purchased.holding_id},
            ).all()
            holding = connection.execute(
                text(
                    "select principal_minor, current_value_minor, status from holdings where id = :id"
                ),
                {"id": purchased.holding_id},
            ).one()
    finally:
        engine.dispose()

    # 部分赎回 + 全部赎回：两笔订单，金额与分摊字段都在库里。
    assert len(orders) == 2
    part, full = orders
    assert part.amount_minor == PARTIAL_AMOUNT_MINOR
    assert part.status == "completed"
    assert part.to_account_id == DEMO_MAIN_ACCOUNT
    assert part.details["principal_redeemed_minor"] == PARTIAL_AMOUNT_MINOR
    assert part.details["interest_redeemed_minor"] == 0
    assert full.amount_minor == REMAINING_AMOUNT_MINOR
    assert full.details["full_redemption"] is True
    assert holding.principal_minor == 0
    assert holding.current_value_minor == 0
    assert holding.status == "redeemed"
