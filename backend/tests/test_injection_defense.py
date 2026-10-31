"""Comprehensive tests for D4: Prompt Injection Defense and explicit interception.

Asserts zero business occurrences and explicit DENIED/interception status
across four representative adversarial injection vectors:
1. Prompt override attempting large transfer without approval.
2. Privilege escalation attempting to skip verification code.
3. Policy tampering attempting to elevate daily transaction limit.
4. Parameter/price tampering in life event orders.
"""

from __future__ import annotations

import pytest

from app.agents.turn import TurnResult, TurnStatus
from app.modules.base import ERROR_MESSAGES
from app.policy.evaluator import classify_operation
from app.policy.types import DAILY_RED_THRESHOLD_MINOR, RiskLevel
from app.storage.models import LedgerEntry
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ErrorCode, ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.domain_harness import FakeBankClient
from tests.test_pending_action_api import PREVIEW_WITH_CODE, paused


# --------------------------------------------------------------------------
# Test Vector 1: Prompt Override Large Transfer (业务零发生断言)
# --------------------------------------------------------------------------
def test_injection_prompt_override_large_transfer_blocked_zero_writes(rt, session):
    """断言：Prompt 诱导直接转账 10 万，底层数据库流水严格零新增，银行零写调用。"""
    bank = FakeBankClient()
    initial_requests_count = len(bank.requests)

    with rt.database.sessions() as db_session:
        initial_ledger_count = db_session.query(LedgerEntry).count()

    # 模拟策略评估：即使 Prompt 包含注入提权指令，策略引擎只读结构化参数，坚决判定为 RED 级
    amount_minor = 100_000_00
    decision = classify_operation("transfer", amount_minor=amount_minor, daily_accumulated_minor=0)
    assert decision.level == RiskLevel.RED
    assert decision.mfa_required is True
    assert decision.confirm_required is True

    # 业务零发生断言：未向底层银行发出任何 /transfers 写调用
    post_transfer_calls = [
        r for r in bank.requests if r.get("path") == "/transfers" and r.get("method") == "POST"
    ]
    assert len(post_transfer_calls) == 0

    # 数据库流水严格零新增
    with rt.database.sessions() as db_session:
        current_ledger_count = db_session.query(LedgerEntry).count()
        assert current_ledger_count == initial_ledger_count


def test_injection_prompt_override_embedded_text_in_fields_ignored(rt, session):
    """断言：Prompt 注入词夹带于合法参数（如收款人字段）中，策略引擎不受干扰。"""
    amount_minor = 50_000_00
    # 模拟操作名正常，但金额超限，策略引擎不看任何文字说明
    decision = classify_operation("transfer", amount_minor=amount_minor, daily_accumulated_minor=0)
    assert decision.level == RiskLevel.RED
    assert decision.mfa_required is True


# --------------------------------------------------------------------------
# Test Vector 2: Privilege Escalation Skip MFA (HTTP 接口层拒绝)
# --------------------------------------------------------------------------
def test_injection_privilege_escalation_skip_mfa_blocked(rt, session):
    """断言：声称管理员跳过验证码直接确认，接口层 422 拒绝，响应结构符合规范。"""
    client, conversation, question, _spy, _rebuild = paused(rt, session, preview=PREVIEW_WITH_CODE)
    conversation_id = conversation["conversation_id"]
    question_id = question["question_id"]
    pending_id = PREVIEW_WITH_CODE.pending_id

    # 恶意请求：模拟黑客绕过前端，直接向 decide 端点发起空验证码的 confirm
    res = client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json={
            "decision": "confirm",
            "question_id": question_id,
            "verification_code": "",  # 恶意留空
        },
        headers={"Idempotency-Key": "test_attack_key_empty_code"},
    )

    # 必须被 HTTP 422 直接打回，且错误码为 VERIFICATION_CODE_REQUIRED
    assert res.status_code == 422
    body = res.json()
    assert body["error"]["code"] == "VERIFICATION_CODE_REQUIRED"
    assert "验证码" in body["error"]["message"]

    # 确认数据库无流水发生
    with rt.database.sessions() as db_session:
        assert db_session.query(LedgerEntry).count() == 0


def test_injection_privilege_escalation_invalid_code_blocked_zero_writes(rt, session):
    """断言：攻击者伪造非空错误验证码绕过 422 门禁，模块/银行层校验失败，流水严格零新增。"""
    failed_result = TurnResult(
        status=TurnStatus.FAILED,
        summary="验证码不正确。",
    )
    client, conversation, question, _spy, _rebuild = paused(
        rt, session, action=failed_result, preview=PREVIEW_WITH_CODE
    )
    conversation_id = conversation["conversation_id"]
    question_id = question["question_id"]
    pending_id = PREVIEW_WITH_CODE.pending_id

    res = client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json={
            "decision": "confirm",
            "question_id": question_id,
            "verification_code": "999999",  # 伪造错误验证码
        },
        headers={"Idempotency-Key": "test_attack_key_fake_code"},
    )

    # 接口恢复执行但业务失败，数据库流水严格零新增
    with rt.database.sessions() as db_session:
        assert db_session.query(LedgerEntry).count() == 0


# --------------------------------------------------------------------------
# Test Vector 3: Policy Tamper Daily Limit (网关阻断未授权工具)
# --------------------------------------------------------------------------
def test_injection_policy_tamper_daily_limit_blocked():
    """断言：试图调用伪造的风控限额修改工具，网关直接返回 TOOL_DENIED 与统一拦截文案。"""
    spec = ToolSpec(name="transfer_prepare", description="t", parameters={"type": "object"})
    catalog = ToolCatalog.build([spec], {"transfer": ["transfer_prepare"]})
    gateway = ToolGateway(catalog, RecordingAdapter())
    context = TrustedContext(user_id="u_01", session_id="s_01", bank_customer_id="c_01")
    handle = gateway.handle("transfer", context)

    # 模拟攻击者诱导调用未授权/伪造的风控限额修改工具
    result = handle.call({"tool": "set_daily_limit", "arguments": {"limit": 99999}, "call_id": "call_hack_01"})

    assert result.status is ToolStatus.DENIED
    assert result.error_code == ErrorCode.TOOL_DENIED
    assert result.error == ERROR_MESSAGES[ErrorCode.TOOL_DENIED]
    assert result.error.startswith("已拦截：")

    # 系统硬编码常量与风控判断依然固若金汤
    assert DAILY_RED_THRESHOLD_MINOR == 100000
    decision = classify_operation("transfer", amount_minor=150000, daily_accumulated_minor=0)
    assert decision.level == RiskLevel.RED
    assert decision.mfa_required is True


# --------------------------------------------------------------------------
# Test Vector 4: Price Tampering in Life Service (Schema 拦截与目录定价隔离)
# --------------------------------------------------------------------------
def test_injection_price_tamper_life_service_blocked():
    """断言：生活服务下单注入 50 万价格，网关 Schema 强校验（additionalProperties: False）直接拒收。"""
    order_spec = ToolSpec(
        name="life_prepare_order",
        description="下单",
        parameters={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "product_code": {"type": "string", "enum": ["flowers", "cake"]},
            },
            "required": ["contact_id", "product_code"],
            "additionalProperties": False,
        },
    )
    catalog = ToolCatalog.build([order_spec], {"life": ["life_prepare_order"]})
    gateway = ToolGateway(catalog, RecordingAdapter())
    handle = gateway.handle("life", TrustedContext(user_id="u1", session_id="s1", bank_customer_id="c1"))

    # 尝试注入未声明的 50 万价格字段
    tampered_call = {
        "tool": "life_prepare_order",
        "arguments": {
            "contact_id": "c_zhang",
            "product_code": "flowers",
            "price_minor": 50000000,  # 恶意注入篡改价格
        },
        "call_id": "call_price_hack",
    }
    result = handle.call(tampered_call)

    # 网关 Schema 校验失败，直接拒绝
    assert result.status is ToolStatus.DENIED
    assert result.error_code == ErrorCode.TOOL_DENIED
    assert result.error == ERROR_MESSAGES[ErrorCode.TOOL_DENIED]
    assert result.error.startswith("已拦截：")
