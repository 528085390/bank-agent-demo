"""真实 HTTP 契约测试：理财适配器 ↔ 运行中的银行服务。

只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行，而且那个服务必须连着 **M1 专用测试 schema**
（已执行 `scripts/seed_wealth_demo.py`）。没有环境变量就跳过：默认不碰开发共享服务，也不在
别人的数据上跑测试。

    $env:WEALTH_TEST_BANK_URL = "http://127.0.0.1:8001/api"   # 指向测试 schema 的银行进程
    $env:WEALTH_TEST_BANK_CUSTOMER = "u_wealth"               # 可选，默认 M1 演示客户
    backend/.venv/Scripts/python.exe -m pytest tests/wealth_integration -q

这里只做**只读**契约验证：写链路（申购／赎回／测评保存）属于任务 9／10 的端到端验收，
不在本文件里改动演示数据。断言的是数据库里的真实行，不是替身或固定 JSON。
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.integration.bank_client import BankClient
from app.modules.wealth.contracts.bank import PURCHASE_OPERATION
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import TrustedContext

BANK_URL_ENV = "WEALTH_TEST_BANK_URL"
CUSTOMER_ENV = "WEALTH_TEST_BANK_CUSTOMER"

# bank_backend/app/storage/wealth_seed.py 的 WEALTH_USER。
DEFAULT_CUSTOMER = "u_wealth"

# M1 演示数据里的固定 ID 与数值。
DEMO_CASH_PRODUCT = "p_demo_cash"
DEMO_SAMPLE_HOLDING = "h_wealth_sample"
DEMO_SAMPLE_ORDER = "order_wealth_sample"
DEMO_SAMPLE_TRANSACTION = "txn_wealth_sample_buy"


@pytest.fixture(scope="module")
def live_bank() -> HttpWealthBank:
    url = os.getenv(BANK_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"未设置 {BANK_URL_ENV}：本用例只对连着 M1 专用测试 schema 的银行服务运行")
    client = BankClient(url, connect_timeout=5.0, read_timeout=10.0)
    try:
        yield HttpWealthBank(client)
    finally:
        client.close()


@pytest.fixture(scope="module")
def live_context() -> TrustedContext:
    customer = os.getenv(CUSTOMER_ENV, "").strip() or DEFAULT_CUSTOMER
    return TrustedContext(
        user_id="agent_live_contract", session_id="sess_live_contract", bank_customer_id=customer
    )


def test_live_bank_contract_reads_persisted_product(
    live_bank: HttpWealthBank, live_context: TrustedContext
):
    """真 HTTP 返回数据库产品，且结构映射正确。"""

    products = live_bank.list_products(live_context)
    cash = next(
        (product for product in products if product.product_id == DEMO_CASH_PRODUCT),
        None,
    )

    assert cash is not None, "M1 演示数据里应有 p_demo_cash；请先执行 seed_wealth_demo.py"
    assert cash.name == "演示灵活理财"
    assert cash.product_type == "money_fund"
    assert cash.risk_level == "low"
    assert cash.redeem_rule == "flexible"
    assert cash.duration_months == 0
    assert cash.min_amount_minor == 10000
    assert cash.status == "available"
    assert cash.expected_return == pytest.approx(0.02)

    detail = live_bank.get_product(live_context, DEMO_CASH_PRODUCT)
    assert detail == cash

    # 停售产品仍在数据库里可查，但状态由银行给出，不由适配器过滤。
    closed = next((product for product in products if product.product_id == "p_demo_closed"), None)
    assert closed is not None
    assert closed.status == "sold_out"


def test_live_bank_contract_reads_persisted_holding(
    live_bank: HttpWealthBank, live_context: TrustedContext
):
    """样例持仓来自数据库：本金、价值、持有收益与可赎回额都按银行字段映射。"""

    holding = live_bank.get_holding(live_context, DEMO_SAMPLE_HOLDING)

    assert holding.holding_id == DEMO_SAMPLE_HOLDING
    assert holding.user_id == live_context.bank_customer_id
    assert holding.principal_minor == 100000
    assert holding.current_value_minor == 100500
    assert holding.accrued_interest_minor == holding.current_value_minor - holding.principal_minor
    assert holding.realized_income_minor == 0
    # 灵活产品的有效持仓可赎回额＝当前价值。
    assert holding.redeemable_minor == holding.current_value_minor

    listed = live_bank.list_holdings(live_context)
    assert holding in listed


def test_live_bank_contract_reads_persisted_order(
    live_bank: HttpWealthBank, live_context: TrustedContext
):
    """订单号与资金流水号是两个字段，来自数据库而不是聊天记录。"""

    order = live_bank.get_order(live_context, DEMO_SAMPLE_ORDER)

    assert order.order_id == DEMO_SAMPLE_ORDER
    assert order.order_type == "product_purchase"
    assert order.status == "completed"
    assert order.transaction_id == DEMO_SAMPLE_TRANSACTION
    assert order.order_id != order.transaction_id
    assert order.holding_id == DEMO_SAMPLE_HOLDING
    assert order.amount_minor == 100000
    assert order.user_id == live_context.bank_customer_id


def test_live_idempotency_lookup_reports_absence_without_fabricating(
    live_bank: HttpWealthBank, live_context: TrustedContext
):
    """按一个从未用过的键查不到，只返回 found=false，不编造订单。"""

    lookup = live_bank.find_order_by_key(live_context, PURCHASE_OPERATION, f"unused-{uuid4().hex}")

    assert lookup.found is False
    assert lookup.order is None
    assert lookup.result is None


def test_live_risk_profile_maps_when_the_bank_has_one(
    live_bank: HttpWealthBank, live_context: TrustedContext
):
    """没有测评记录时是空结果；有记录时等级与有效期都来自银行。"""

    profile = live_bank.get_risk_profile(live_context)

    if profile is None:
        return
    assert profile.user_id == live_context.bank_customer_id
    assert profile.risk_level in {"low", "medium", "high"}
    assert profile.rule_version
    assert isinstance(profile.valid, bool)
