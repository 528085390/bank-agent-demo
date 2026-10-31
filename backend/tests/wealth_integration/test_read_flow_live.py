"""任务6 的真实 HTTP 版本：独立理财 Agent ↔ 运行中的银行服务。

只有显式指定 `WEALTH_TEST_BANK_URL` 时才运行，而且那个服务必须连着 **M1 专用测试 schema**
（已执行 `scripts/seed_wealth_demo.py`）。没有环境变量就跳过：默认不碰开发共享服务，也不在
别人的数据上跑测试。

    $env:WEALTH_TEST_BANK_URL = "http://127.0.0.1:8001/api"   # 指向测试 schema 的银行进程
    $env:WEALTH_TEST_BANK_CUSTOMER = "u_wealth"               # 可选，默认 M1 演示客户
    backend/.venv/Scripts/python.exe -m pytest tests/wealth_integration -q

这里只做**只读**验证：脚本模型固定决策，Agent 经 `HttpWealthBank` → `BankClient` 读真实数据库
产品，再回答。写链路（申购／赎回／测评保存）属于任务 9／10 的端到端验收，本文件不改动演示数据。
"""

from __future__ import annotations

import os

import pytest

from app.integration.bank_client import BankClient
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import ErrorCode, TrustedContext
from tests.wealth import support

# 跨轮状态由用例自己拿着：运行时不再替调用方记会话与待办（任务 3）。
SESSION = support.SessionCarrier()

BANK_URL_ENV = "WEALTH_TEST_BANK_URL"
CUSTOMER_ENV = "WEALTH_TEST_BANK_CUSTOMER"

# bank_backend/app/storage/wealth_seed.py 的 WEALTH_USER 与 M1 演示数据。
DEFAULT_CUSTOMER = "u_wealth"
DEMO_CASH_PRODUCT = "p_demo_cash"
DEMO_CASH_NAME = "演示灵活理财"
DEMO_CLOSED_PRODUCT = "p_demo_closed"


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
        user_id="agent_live_read", session_id="sess_live_read", bank_customer_id=customer
    )


def test_live_read_flow_answers_from_the_database(live_bank: HttpWealthBank, live_context):
    """一次 handle 完成两次真实 HTTP 查询并回答：事实来自数据库，不是替身或固定 JSON。"""

    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.call_read("wealth_product", {"product_id": DEMO_CASH_PRODUCT}),
        support.finish(f"可以灵活赎回的是{DEMO_CASH_NAME}，起购 100.00 元。", [DEMO_CASH_PRODUCT]),
    )
    agent = build_wealth_agent(model, live_bank, SystemClock())

    response = SESSION.handle(
        agent, live_context, WealthRequest(kind="message", text="有哪些可以灵活赎回的产品？")
    )

    assert response.status is WealthStatus.COMPLETED
    assert response.error_code is None
    assert response.summary == f"可以灵活赎回的是{DEMO_CASH_NAME}，起购 100.00 元。"
    assert model.calls == 3

    products = {row["product_id"]: row for row in response.facts["products"]}
    cash = products[DEMO_CASH_PRODUCT]
    assert cash["name"] == DEMO_CASH_NAME
    assert cash["redeem_rule"] == "flexible"
    assert cash["risk_level"] == "low"
    assert cash["min_amount_minor"] == 10000
    assert cash["status"] == "available"
    # 停售产品也在数据库里、也被读回来了，但状态由银行给出。
    assert products[DEMO_CLOSED_PRODUCT]["status"] == "sold_out"
    assert response.business_ids == {"product_id": DEMO_CASH_PRODUCT}


def test_live_read_flow_reads_the_database_again_on_every_query(live_bank, live_context):
    """适配器不缓存：第二次 handle 仍然是一次真实 HTTP 查询。"""

    def run_once() -> dict:
        model = support.ScriptModel(
            support.call_read("wealth_products"),
            support.finish(f"{DEMO_CASH_NAME}可以灵活赎回。", [DEMO_CASH_PRODUCT]),
        )
        response = SESSION.handle(
            build_wealth_agent(model, live_bank, SystemClock()),
            live_context,
            WealthRequest(kind="message", text="有哪些可以灵活赎回的产品？"),
        )
        assert response.status is WealthStatus.COMPLETED
        return {row["product_id"]: row for row in response.facts["products"]}

    assert run_once() == run_once()


def test_live_read_flow_refuses_an_unbound_customer(live_bank, live_context):
    """没有绑定银行客户号时不读银行默认客户的数据，直接以 NO_CUSTOMER_BINDING 失败。"""

    model = support.ScriptModel(
        support.call_read("wealth_products"),
        support.finish("无法查询。"),
    )
    agent = build_wealth_agent(model, live_bank, SystemClock())
    unbound = TrustedContext(user_id="agent_live_read", session_id="sess_live_read")

    response = SESSION.handle(agent, unbound, WealthRequest(kind="message", text="有哪些产品？"))

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.NO_CUSTOMER_BINDING
    assert response.facts == {}
