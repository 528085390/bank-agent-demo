"""任务9 的真实 HTTP 端到端：Agent 的申购闭环 ↔ 运行中的银行服务。

只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行，而且那个服务必须连着 **M1 专用测试 schema**
（已执行 `scripts/seed_wealth_demo.py`）。没有环境变量就跳过：默认不碰开发共享服务，也不在
别人的数据上跑测试。

    $env:WEALTH_TEST_BANK_URL = "http://127.0.0.1:8001/api"   # 指向测试 schema 的银行进程
    $env:WEALTH_TEST_BANK_CUSTOMER = "u_wealth"               # 可选，默认 M1 演示客户
    $env:WEALTH_TEST_BANK_CODE = "123456"                     # 可选，默认银行配置的演示码
    backend/.venv/Scripts/python.exe -m pytest tests/wealth_integration -q

这里断言的是**真实余额、真实持仓和真实订单行**，不是替身：

- 预览与确认阶段银行没有任何资金变动；
- 验证码错误时银行收到请求但余额与持仓不变；
- 验证码正确时余额正好减少申购金额、多出一笔本金等于该金额的新持仓，回执里的订单号能在银行
  重新查到（`purchase_verified`）；
- 重复的验证码事件仍是一笔订单（同一幂等键），余额不会再减一次；
- 改金额之后旧确认不能作用在新金额上。

设置 `WEALTH_TEST_BANK_SCHEMA` 与 `WEALTH_TEST_BANK_DATABASE_URL` 时，还会直接查一次数据库，
确认那笔订单、那笔持仓和那次余额变动真的落了库（Agent 的正式路径只有 HTTP，直连 SQL 只用于
测试核验）。

**演示数据是有限的**：`a_wealth_main` 只有 20000 元，每跑一次完整申购就少 2000 元，而
`seed_wealth_demo.py` 不会重复入金。需要回到初始状态时，重建专用 schema
（drop → `alembic upgrade head` → `POST /api/seed/reset` → `scripts/seed_wealth_demo.py`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text

from app.integration.bank_client import BankClient
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.contracts.bank import Holding, RiskProfile, WealthOrder
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthResponse, WealthStatus
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import ErrorCode, TrustedContext
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
DEMO_MAIN_ACCOUNT = "a_wealth_main"
DEMO_SAMPLE_HOLDING = "h_wealth_sample"

# bank_backend/app/core/config.py 的 mock_mfa_code 默认值。
DEFAULT_CODE = "123456"
WRONG_CODE = "000000"

AMOUNT_MINOR = 200000
# 五题总分 6：medium。低风险的演示灵活理财在允许范围内。
ANSWERS = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 2}
SLOTS = {
    "product_id": DEMO_CASH_PRODUCT,
    "account_id": DEMO_MAIN_ACCOUNT,
    "amount_minor": AMOUNT_MINOR,
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
        user_id="agent_live_purchase", session_id="sess_live_purchase", bank_customer_id=customer
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


def _agent(live_bank: HttpWealthBank) -> object:
    """一个脚本模型 + 真实银行适配器：模型只提取槽位，其余全由程序与银行完成。"""

    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(SLOTS),
    )
    return build_wealth_agent(model, live_bank, SystemClock())


def _balance(live_bank: HttpWealthBank, context: TrustedContext) -> int:
    accounts = {item.account_id: item for item in live_bank.list_accounts(context)}
    return accounts[DEMO_MAIN_ACCOUNT].balance_minor


def _holdings(live_bank: HttpWealthBank, context: TrustedContext) -> dict[str, Holding]:
    return {item.holding_id: item for item in live_bank.list_holdings(context)}


def _preview(agent, context, live_bank) -> tuple[WealthResponse, int]:
    before = _balance(live_bank, context)
    response = SESSION.handle(
        agent, context, WealthRequest(kind="message", text="买 2000 元的演示灵活理财，用主账户付款")
    )
    return response, before


@dataclass(frozen=True, slots=True)
class PurchaseRun:
    """一次完整申购留下的真实证据。"""

    preview: WealthResponse
    submitted: WealthResponse
    balance_before: int
    balance_after: int
    order: WealthOrder
    holding: Holding
    # 同一个 Agent 实例与同一个待办，用来验证"重复的确认事件"。
    agent: object
    pending_id: str


@pytest.fixture(scope="module")
def purchase(live_bank: HttpWealthBank, live_context: TrustedContext, assessment, demo_code):
    """跑一次完整的真实申购：预览 → 在确认框上输入验证码并确认 → 银行成交。"""

    agent = _agent(live_bank)
    preview, balance_before = _preview(agent, live_context, live_bank)
    assert preview.status is WealthStatus.WAITING_CONFIRMATION, preview.summary
    pending_id = preview.pending.pending_id
    # 银行要验证码，所以卡片上必须有验证码输入——这是"确认框按银行的要求变形"的取值。
    assert preview.confirmation.requires_code is True

    submitted = SESSION.handle(
        agent,
        live_context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=demo_code),
    )
    assert submitted.status is WealthStatus.COMPLETED, submitted.summary

    order_id = submitted.business_ids["order_id"]
    holding_id = submitted.business_ids["holding_id"]
    return PurchaseRun(
        preview=preview,
        submitted=submitted,
        balance_before=balance_before,
        balance_after=_balance(live_bank, live_context),
        order=live_bank.get_order(live_context, order_id),
        holding=live_bank.get_holding(live_context, holding_id),
        agent=agent,
        pending_id=pending_id,
    )


# ---------------------------------------------------------------------- 少一步就不写钱


def test_no_write_before_confirm_and_a_click_without_a_code_writes_nothing(
    live_bank, live_context, assessment, demo_code
):
    """只走到确认、而且没带验证码：银行里余额和持仓一点没变，而且能确定地说"没有提交"。"""

    agent = _agent(live_bank)
    preview, before = _preview(agent, live_context, live_bank)
    holdings_before = _holdings(live_bank, live_context)

    confirmed = SESSION.handle(
        agent, live_context, WealthRequest(kind="confirm", pending_id=preview.pending.pending_id)
    )

    assert preview.status is WealthStatus.WAITING_CONFIRMATION
    assert preview.confirmation.requires_code is True
    assert confirmed.status is WealthStatus.FAILED
    assert confirmed.error_code == ErrorCode.VALIDATION
    assert _balance(live_bank, live_context) == before
    assert _holdings(live_bank, live_context) == holdings_before
    # 待办还在：补上验证码就能继续；这里主动取消，不留悬空待办。
    assert (
        SESSION.handle(agent, live_context, WealthRequest(kind="cancel")).status
        is WealthStatus.CANCELLED
    )


def test_wrong_code_does_not_move_money(live_bank, live_context, assessment, demo_code):
    """验证码错误：银行收到了写请求，但余额与持仓没有变化，而且预览重新交回来让用户重输。"""

    agent = _agent(live_bank)
    preview, before = _preview(agent, live_context, live_bank)
    holdings_before = _holdings(live_bank, live_context)
    pending_id = preview.pending.pending_id

    response = SESSION.handle(
        agent,
        live_context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=WRONG_CODE),
    )

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert response.pending.pending_id == pending_id
    assert response.confirmation.requires_code is True
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
    assert _balance(live_bank, live_context) == before - AMOUNT_MINOR


def test_old_confirmation_cannot_buy_new_amount(live_bank, live_context, assessment, demo_code):
    """改金额之后旧确认不能触发新交易：没有新订单，余额不变。"""

    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(SLOTS),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase({**SLOTS, "amount_minor": 100000}),
    )
    agent = build_wealth_agent(model, live_bank, SystemClock())
    before = _balance(live_bank, live_context)
    first = SESSION.handle(agent, live_context, WealthRequest(kind="message", text="买 2000 元"))
    old_pending_id = first.pending.pending_id

    second = SESSION.handle(
        agent, live_context, WealthRequest(kind="message", text="算了，改成 1000 元")
    )

    assert second.status is WealthStatus.WAITING_CONFIRMATION
    assert second.facts["purchase_preview"]["amount_minor"] == 100000
    assert second.pending.pending_id != old_pending_id

    stale = [
        SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="confirm", pending_id=old_pending_id, verification_code=demo_code),
        ),
        SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="verify", pending_id=old_pending_id, verification_code=demo_code),
        ),
    ]

    for response in stale:
        assert response.status is WealthStatus.FAILED
        assert response.error_code == ErrorCode.INVALID_STATE
    assert _balance(live_bank, live_context) == before


# ---------------------------------------------------------------------- 真实成交


def test_agent_purchase_persists_to_real_bank(purchase: PurchaseRun, live_bank, live_context):
    """输入验证码并确认之后：余额正好减少申购金额，多出一笔本金相同的新持仓，订单能在银行查到。"""

    assert purchase.submitted.status is WealthStatus.COMPLETED
    assert purchase.balance_after == purchase.balance_before - AMOUNT_MINOR

    assert purchase.order.order_id == purchase.submitted.business_ids["order_id"]
    assert purchase.order.order_type == "product_purchase"
    assert purchase.order.status == "completed"
    assert purchase.order.product_id == DEMO_CASH_PRODUCT
    assert purchase.order.amount_minor == AMOUNT_MINOR
    assert purchase.order.holding_id == purchase.submitted.business_ids["holding_id"]
    assert purchase.order.transaction_id == purchase.submitted.business_ids["transaction_id"]
    assert purchase.order.order_id != purchase.order.transaction_id

    assert purchase.holding.principal_minor == AMOUNT_MINOR
    assert purchase.holding.current_value_minor == AMOUNT_MINOR
    assert purchase.holding.status == "holding"
    assert purchase.holding.user_id == live_context.bank_customer_id

    # 写后复核是 Agent 自己在银行重新查的，不是替身。
    assert purchase.submitted.facts["purchase_verified"] is True
    assert purchase.submitted.facts["orders"][0]["order_id"] == purchase.order.order_id
    assert any(
        account["account_id"] == DEMO_MAIN_ACCOUNT
        and account["balance_minor"] == purchase.balance_after
        for account in purchase.submitted.facts["accounts"]
    )
    # 样例持仓（本金 1000 元、价值 1005 元）没有被这笔申购改动。
    sample = live_bank.get_holding(live_context, DEMO_SAMPLE_HOLDING)
    assert sample.principal_minor == 100000
    assert sample.current_value_minor == 100500


def test_duplicate_confirm_returns_the_same_order(
    purchase: PurchaseRun, live_bank, live_context, demo_code
):
    """重复的确认事件仍是一笔银行订单：同一个订单号，余额不再减一次。"""

    again = SESSION.handle(
        purchase.agent,
        live_context,
        WealthRequest(kind="confirm", pending_id=purchase.pending_id, verification_code=demo_code),
    )

    assert again.status is WealthStatus.COMPLETED
    assert again.business_ids["order_id"] == purchase.order.order_id
    assert again.business_ids["holding_id"] == purchase.order.holding_id
    assert _balance(live_bank, live_context) == purchase.balance_after


def test_the_agent_purchase_row_is_in_the_database(purchase: PurchaseRun):
    """直接查库核对：订单、持仓和余额变动真的落了库（直连 SQL 只用于测试核验）。"""

    dsn = os.getenv(DATABASE_URL_ENV, "").strip()
    schema = os.getenv(SCHEMA_ENV, "").strip()
    if not dsn or not schema:
        pytest.skip(f"未设置 {SCHEMA_ENV}／{DATABASE_URL_ENV}：跳过直连数据库的核验")

    engine = create_engine(dsn, connect_args={"options": f"-c search_path={schema}"})
    try:
        with engine.connect() as connection:
            order = connection.execute(
                text(
                    "select user_id, product_id, holding_id, amount_minor, status, transaction_id"
                    " from orders where id = :id"
                ),
                {"id": purchase.order.order_id},
            ).one()
            holding = connection.execute(
                text("select user_id, product_id, principal_minor from holdings where id = :id"),
                {"id": purchase.order.holding_id},
            ).one()
            balance = connection.execute(
                text("select balance_minor from accounts where id = :id"),
                {"id": DEMO_MAIN_ACCOUNT},
            ).scalar_one()
    finally:
        engine.dispose()

    assert order.user_id == DEFAULT_CUSTOMER
    assert order.product_id == DEMO_CASH_PRODUCT
    assert order.holding_id == purchase.order.holding_id
    assert order.amount_minor == AMOUNT_MINOR
    assert order.status == "completed"
    assert order.transaction_id == purchase.order.transaction_id
    assert holding.product_id == DEMO_CASH_PRODUCT
    assert holding.principal_minor == AMOUNT_MINOR
    assert balance == purchase.balance_after
