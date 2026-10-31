"""L0 整体契约集成回归验证.

覆盖以下跨模块基础契约的集成与协同：
1. app.core.clock: SystemClock 与 CST 东八区时区（网络隔离 MockTransport）。
2. app.policy: PolicyDecision, RiskLevel, classify_operation, requires_code。
3. app.api.sse: encode_event, EVENT_NAMES 包含 plan, task_update, clock。
4. app.storage.migrations: 0016_daily_operation_total 迁移定义。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.sse import EVENT_NAMES, encode_event
from app.core.clock import CST, SystemClock
from app.policy import PolicyDecision, RiskLevel, classify_operation, requires_code


@dataclass
class DummyStreamEvent:
    """Mock implementation of StreamEvent Protocol."""

    event: str
    data: dict[str, Any]


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "storage"
    / "migrations"
    / "versions"
    / "0016_daily_operation_total.py"
)


def _load_migration_module():
    assert MIGRATION_PATH.exists(), f"Migration file not found at {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0016", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_l0_migration_contract():
    """验证 Alembic 0016 迁移文件契约元数据与调用接口。"""
    mod = _load_migration_module()
    assert mod.revision == "0016_daily_operation_total"
    assert mod.down_revision == "0015_bill_doc_tenant_unique"
    assert callable(getattr(mod, "upgrade", None))
    assert callable(getattr(mod, "downgrade", None))


def test_l0_contracts_integration_scenario():
    """综合测试 L0 时钟（隔离网络）、风控策略、SSE 帧格式的端到端集成。"""
    # 1. 验证统一时钟（使用 MockTransport 彻底切断单元测试对外界 8001 网络的依赖）
    SystemClock.invalidate()
    mock_iso = "2026-10-22T08:00:00+08:00"
    mock_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"code": 200, "data": {"current_time": mock_iso}})
        ),
        base_url="http://mockbank",
    )

    today = SystemClock.today(client=mock_client)
    now = SystemClock.now(client=mock_client)

    assert isinstance(today, date)
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=8)
    assert today == date(2026, 10, 22)
    assert SystemClock.source() == "bank"
    assert SystemClock.is_fallback() is False

    # 2 & 3. 验证风控策略判断 (600元 + 500元累计 > 1000元阈值 -> RED)
    amount_minor = 60_000  # 600.00 CNY
    accumulated_minor = 50_000  # 500.00 CNY
    decision: PolicyDecision = classify_operation(
        "transfer_execute",  # 测试真实工具名
        amount_minor=amount_minor,
        daily_accumulated_minor=accumulated_minor,
    )

    assert decision.level == RiskLevel.RED
    assert decision.confirm_required is True
    assert decision.mfa_required is True
    assert requires_code(decision.level) is True
    assert requires_code("red") is True
    assert "超过日限额" in decision.reason

    # 边界对比：未超限转账 (400元 + 500元累计 = 900元 <= 1000元 -> YELLOW)
    safe_decision = classify_operation(
        "transfer_execute",
        amount_minor=40_000,
        daily_accumulated_minor=accumulated_minor,
    )
    assert safe_decision.level == RiskLevel.YELLOW
    assert safe_decision.confirm_required is True
    assert safe_decision.mfa_required is False
    assert requires_code(safe_decision.level) is False

    # 4. 验证 SSE 'clock' 事件编码
    assert "clock" in EVENT_NAMES
    clock_event = DummyStreamEvent(
        event="clock",
        data={"current_time": now.isoformat()},
    )
    encoded_clock = encode_event(clock_event, request_id="req-integ-001")
    assert "event: clock\n" in encoded_clock
    assert now.isoformat() in encoded_clock
    assert '"request_id": "req-integ-001"' in encoded_clock

    # 5. 验证 SSE 'plan' 事件编码
    assert "plan" in EVENT_NAMES
    plan_payload = {
        "tasks": [
            {
                "task_id": "task-fund-transfer",
                "title": "向张三转账 600.00 元",
                "operation": "transfer",
                "amount_minor": amount_minor,
                "risk_level": decision.level.value,
                "requires_code": requires_code(decision.level),
            }
        ],
        "created_at": now.isoformat(),
    }
    plan_event = DummyStreamEvent(event="plan", data=plan_payload)
    encoded_plan = encode_event(plan_event, request_id="req-integ-001")
    assert "event: plan\n" in encoded_plan
    assert '"risk_level": "red"' in encoded_plan
    assert '"requires_code": true' in encoded_plan

    # 6. 验证 SSE 'task_update' 事件编码
    assert "task_update" in EVENT_NAMES
    task_update_payload = {
        "task_id": "task-fund-transfer",
        "status": "awaiting_mfa_code",
        "node": "human_interrupt",
        "timestamp": now.isoformat(),
    }
    task_update_event = DummyStreamEvent(event="task_update", data=task_update_payload)
    encoded_task_update = encode_event(task_update_event, request_id="req-integ-001")
    assert "event: task_update\n" in encoded_task_update
    assert '"status": "awaiting_mfa_code"' in encoded_task_update
    assert '"node": "human_interrupt"' in encoded_task_update
