"""任务11 的真实 HTTP 端到端：回执丢失后的核实（Agent ↔ 运行中的银行服务）。

只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行，而且那个服务必须连着 **M1 专用测试 schema**
（已执行 `scripts/seed_wealth_demo.py`）。没有环境变量就跳过：默认不碰开发共享服务，也不在
别人的数据上跑测试。

    $env:WEALTH_TEST_BANK_URL = "http://127.0.0.1:8001/api"   # 指向测试 schema 的银行进程
    $env:WEALTH_TEST_BANK_CUSTOMER = "u_wealth"               # 可选，默认 M1 演示客户
    $env:WEALTH_TEST_BANK_CODE = "123456"                     # 可选，默认银行配置的演示码
    backend/.venv/Scripts/python.exe -m pytest tests/wealth_integration/test_recovery.py -q

这里注入的只有**客户端这一侧的收包动作**（`_LostReceipt` 传输包装）：请求照旧真实发出、银行
照旧真实提交，只是客户端"没读到响应"。所以它模拟的正是"写已提交但回执丢了"，而不是一个假银行：
余额、订单行、幂等记录全部来自真实的 PostgreSQL。

三条断言：

- 回执丢失但银行已经有这笔订单 → Agent 按**原**幂等键找回，余额只减一次，`reconciliation.found`
  为真，写后核实（订单与余额）也从银行重新读到；
- 银行确实没收到（请求根本没发出去）→ 仍然是 unknown，余额一点没变，**没有重新下单**；
- 核实引用是银行数据库里的东西，不是 Agent 内存里的：换一条新连接按同一个键查，仍然查得到。

**演示数据是有限的**：`a_wealth_main` 只有 20000 元，每跑一次完整申购就少 2000 元，而
`seed_wealth_demo.py` 不会重复入金。需要回到初始状态时，重建专用 schema
（drop → `alembic upgrade head` → `POST /api/seed/reset` → `scripts/seed_wealth_demo.py`）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import create_engine, text

from app.integration.bank_client import BankClient
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.contracts.bank import PURCHASE_OPERATION, Holding, RiskProfile
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

# bank_backend/app/core/config.py 的 mock_mfa_code 默认值。
DEFAULT_CODE = "123456"
AMOUNT_MINOR = 200000
# 五题总分 6：medium。低风险的演示灵活理财在允许范围内。
ANSWERS = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 2}
SLOTS = {
    "product_id": DEMO_CASH_PRODUCT,
    "account_id": DEMO_MAIN_ACCOUNT,
    "amount_minor": AMOUNT_MINOR,
}
PURCHASE_PATH = "/products/purchase"


# ---------------------------------------------------------------------- 故障注入


class _LostReceipt(httpx.BaseTransport):
    """把真实传输包一层，只改"客户端读响应"这一步。

    - `sent=True`：请求真的发到银行（银行真的提交），然后客户端抛 `ReadTimeout`——回执丢失；
    - `sent=False`：请求根本没发出去，客户端就抛 `ReadTimeout`——银行什么都不知道。

    两种形态都必须走"按原幂等键核实"这一条路，而它们的正确答案不一样：前者查得到、后者查不到。
    这个开关只存在于测试装置里，正式代码没有任何故障注入入口。
    """

    def __init__(self, *, path: str, sent: bool) -> None:
        self._inner = httpx.HTTPTransport()
        self._path = path
        self._sent = sent
        self.injected = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith(self._path):
            self.injected += 1
            if self._sent:
                # 真的发出去，并且把响应读完：银行这一笔已经提交、余额已经变了。
                self._inner.handle_request(request).read()
            raise httpx.ReadTimeout(
                "probe: the receipt was lost on the client side", request=request
            )
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


# ---------------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def bank_url() -> str:
    url = os.getenv(BANK_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"未设置 {BANK_URL_ENV}：本用例只对连着 M1 专用测试 schema 的银行服务运行")
    return url


@pytest.fixture(scope="module")
def live_context() -> TrustedContext:
    customer = os.getenv(CUSTOMER_ENV, "").strip() or DEFAULT_CUSTOMER
    return TrustedContext(
        user_id="agent_live_recovery", session_id="sess_live_recovery", bank_customer_id=customer
    )


@pytest.fixture(scope="module")
def demo_code() -> str:
    return os.getenv(CODE_ENV, "").strip() or DEFAULT_CODE


@pytest.fixture(scope="module")
def plain_bank(bank_url: str) -> HttpWealthBank:
    """没有任何故障注入的适配器：用来准备测评、读余额、按新连接核实。"""

    client = BankClient(bank_url, connect_timeout=5.0, read_timeout=15.0)
    try:
        yield HttpWealthBank(client)
    finally:
        client.close()


@pytest.fixture(scope="module")
def assessment(plain_bank: HttpWealthBank, live_context: TrustedContext) -> RiskProfile:
    profile = plain_bank.submit_risk_assessment(live_context, ANSWERS)

    assert profile.valid is True, "银行返回的测评必须有效，否则申购会被银行拒绝"
    assert profile.risk_level == "medium"
    return profile


def _agent(model, bank_url: str, *, sent: bool):
    """一个脚本模型 + 带故障注入的真实银行适配器。"""

    transport = _LostReceipt(path=PURCHASE_PATH, sent=sent)
    client = BankClient(bank_url, connect_timeout=5.0, read_timeout=15.0, transport=transport)
    return build_wealth_agent(model, HttpWealthBank(client), SystemClock()), transport, client


def _purchase_model():
    return support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(SLOTS),
    )


def _balance(bank: HttpWealthBank, context: TrustedContext) -> int:
    accounts = {item.account_id: item for item in bank.list_accounts(context)}
    return accounts[DEMO_MAIN_ACCOUNT].balance_minor


def _holdings(bank: HttpWealthBank, context: TrustedContext) -> dict[str, Holding]:
    return {item.holding_id: item for item in bank.list_holdings(context)}


@dataclass(frozen=True, slots=True)
class LostReceiptRun:
    """一次"回执丢失 → 按原键核实"留下的真实证据。"""

    preview: WealthResponse
    verified: WealthResponse
    key: str
    balance_before: int
    balance_after: int
    holdings_before: dict[str, Holding]
    holdings_after: dict[str, Holding]


@pytest.fixture(scope="module")
def lost_receipt(
    bank_url: str, plain_bank: HttpWealthBank, live_context: TrustedContext, assessment, demo_code
) -> LostReceiptRun:
    """跑一次真实申购，但把回执丢掉：银行已经提交，Agent 只能按原幂等键找回。"""

    agent, transport, client = _agent(_purchase_model(), bank_url, sent=True)
    try:
        balance_before = _balance(plain_bank, live_context)
        holdings_before = _holdings(plain_bank, live_context)
        preview = SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="message", text="买 2000 元的演示灵活理财，用主账户"),
        )
        assert preview.status is WealthStatus.WAITING_CONFIRMATION, preview.summary
        pending_id = preview.pending.pending_id
        key = SESSION.write(agent, live_context).idempotency_key
        verified = SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="confirm", pending_id=pending_id, verification_code=demo_code),
        )
        assert transport.injected == 1, "这一次写请求必须真的发出去，而且只发一次"
        assert verified.status is WealthStatus.COMPLETED, verified.summary
        return LostReceiptRun(
            preview=preview,
            verified=verified,
            key=key,
            balance_before=balance_before,
            balance_after=_balance(plain_bank, live_context),
            holdings_before=holdings_before,
            holdings_after=_holdings(plain_bank, live_context),
        )
    finally:
        client.close()


# ---------------------------------------------------------------------- 回执丢失 → 按原键找回


def test_a_lost_receipt_is_recovered_from_the_stored_idempotency_record(
    lost_receipt: LostReceiptRun, plain_bank: HttpWealthBank, live_context: TrustedContext
):
    """回执丢了但银行已经有这笔订单：按原幂等键找回，余额只减一次。"""

    verified = lost_receipt.verified

    assert verified.status is WealthStatus.COMPLETED
    assert verified.facts["reconciliation"]["found"] is True
    assert verified.facts["reconciliation"]["idempotent_key"] == lost_receipt.key
    assert verified.facts["reconciliation"]["operation_type"] == PURCHASE_OPERATION
    assert "没有重复提交" in verified.summary
    # 写后核实是从银行重新读回来的：订单与余额都是真实事实。
    assert verified.facts["purchase_verified"] is True
    assert lost_receipt.balance_after == lost_receipt.balance_before - AMOUNT_MINOR

    order_id = verified.business_ids["order_id"]
    order = plain_bank.get_order(live_context, order_id)
    assert order.order_id == order_id
    assert order.order_type == "product_purchase"
    assert order.status == "completed"
    assert order.amount_minor == AMOUNT_MINOR
    assert order.user_id == live_context.bank_customer_id

    holding = plain_bank.get_holding(live_context, verified.business_ids["holding_id"])
    assert holding.principal_minor == AMOUNT_MINOR
    assert holding.holding_id not in lost_receipt.holdings_before
    assert holding.holding_id in lost_receipt.holdings_after


def test_the_reconciliation_reference_is_durable_in_the_bank(
    lost_receipt: LostReceiptRun, bank_url: str, live_context: TrustedContext
):
    """核实引用活在银行的数据库里，不是 Agent 的内存里：换一条新连接按同一个键照样查得到。

    （银行**进程**重启后的重放由 `bank_backend/tests/acceptance/test_wealth_persistence.py`
    验证；这一层不假装自己重启过那个进程，只证明这个键上的结论来自银行而不是本进程的缓存。）
    """

    reference = lost_receipt.verified.facts["reconciliation"]
    fresh_client = BankClient(bank_url, connect_timeout=5.0, read_timeout=15.0)
    try:
        lookup = HttpWealthBank(fresh_client).find_order_by_key(
            live_context, reference["operation_type"], reference["idempotent_key"]
        )
    finally:
        fresh_client.close()

    assert lookup.found is True
    assert lookup.order is not None
    assert lookup.order.order_id == lost_receipt.verified.business_ids["order_id"]
    assert lookup.result is not None
    assert lookup.result["status"] == "completed"


def test_a_write_the_bank_never_received_stays_unknown(
    bank_url: str, plain_bank: HttpWealthBank, live_context: TrustedContext, assessment, demo_code
):
    """请求根本没发出去：按原键核实查不到 → 仍然是 unknown，余额一点没变，也没有重新下单。"""

    agent, transport, client = _agent(_purchase_model(), bank_url, sent=False)
    try:
        before = _balance(plain_bank, live_context)
        holdings_before = _holdings(plain_bank, live_context)
        preview = SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="message", text="买 2000 元的演示灵活理财，用主账户"),
        )
        pending_id = preview.pending.pending_id

        response = SESSION.handle(
            agent,
            live_context,
            WealthRequest(kind="confirm", pending_id=pending_id, verification_code=demo_code),
        )

        assert transport.injected == 1
        assert response.status is WealthStatus.UNKNOWN
        assert response.error_code == ErrorCode.TRANSACTION_UNKNOWN
        assert response.facts["reconciliation"]["found"] is False
        assert response.business_ids == {}
        # 钱一点没动，而且核实之后没有补发第二次写请求。
        assert _balance(plain_bank, live_context) == before
        assert _holdings(plain_bank, live_context) == holdings_before
        assert transport.injected == 1
    finally:
        client.close()


# ---------------------------------------------------------------------- 审计


def test_the_agent_audit_and_the_bank_records_carry_no_verification_code(
    lost_receipt: LostReceiptRun,
    plain_bank: HttpWealthBank,
    live_context: TrustedContext,
    demo_code: str,
    caplog,
):
    """Agent 日志、银行审计与幂等结果都不含验证码。"""

    caplog.set_level(logging.INFO, logger="app.modules.wealth")
    reference = lost_receipt.verified.facts["reconciliation"]

    # Agent 这一侧：日志里只有脱敏摘要，响应与事实里也没有验证码。
    assert demo_code not in repr(lost_receipt.verified)
    assert demo_code not in json.dumps(dict(lost_receipt.verified.facts), ensure_ascii=False)
    for record in caplog.records:
        if not record.name.startswith("app.modules.wealth"):
            continue
        assert demo_code not in record.getMessage()

    # 银行这一侧：HTTP 能看到的原回执里没有验证码。
    lookup = plain_bank.find_order_by_key(
        live_context, reference["operation_type"], reference["idempotent_key"]
    )
    assert lookup.result is not None
    assert demo_code not in json.dumps(dict(lookup.result), ensure_ascii=False)

    dsn = os.getenv(DATABASE_URL_ENV, "").strip()
    schema = os.getenv(SCHEMA_ENV, "").strip()
    if not dsn or not schema:
        pytest.skip(f"未设置 {SCHEMA_ENV}／{DATABASE_URL_ENV}：跳过直连数据库的审计核验")

    engine = create_engine(dsn, connect_args={"options": f"-c search_path={schema}"})
    try:
        with engine.connect() as connection:
            audit_rows = connection.execute(
                text(
                    "select action, old_value, new_value, reason from audit_logs"
                    " where resource_id = :id"
                ),
                {"id": lookup.result["order_id"]},
            ).all()
            idempotency_row = connection.execute(
                text(
                    "select request_hash, result from idempotency_records"
                    " where user_id = :user_id and operation_type = :operation_type"
                    " and idempotent_key = :key"
                ),
                {
                    "user_id": DEFAULT_CUSTOMER,
                    "operation_type": reference["operation_type"],
                    "key": reference["idempotent_key"],
                },
            ).one()
    finally:
        engine.dispose()

    assert audit_rows, "这一笔申购必须有银行审计"
    for row in audit_rows:
        rendered = " ".join(
            json.dumps(value, ensure_ascii=False) if value is not None else "" for value in row
        )
        assert demo_code not in rendered
    assert demo_code not in json.dumps(dict(idempotency_row.result), ensure_ascii=False)
    # 指纹是哈希，不是明文：验证码也不该出现在里面。
    assert idempotency_row.request_hash
    assert demo_code not in idempotency_row.request_hash
