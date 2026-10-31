# 银行 AI 智能体：L0 地基契约与共享面冻结规格说明书 (Spec)

> **文档标识**：`docs/superpowers/specs/2026-10-21-l0-foundation-and-shared-contracts-spec.md`  
> **制定日期**：2026-10-21  
> **状态**：✅ 已实施并验收（dev `c5c402a`，2026-10-22），见 §4 实施记录  
> **依赖基准**：dev 分支 `7159165`、`docs/GAP-ANALYSIS.md`  
> **目标范围**：L0 阶段的 3 项核心内容（0.1 策略引擎接口、0.2 统一时钟源、0.3 三个共享面冻结），不包含各业务模块的具体改造与前端 UI。

---

## 1. 为什么要有 L0（背景与目的）

根据 `docs/GAP-ANALYSIS.md`，后续 L1 阶段的三条主链（A: `life_event` 智能体、B: 策略引擎与日限额实现、C: 演示时钟与控制面）具备完全并行开发的可能性。

但如果直接开工 L1，三条链会因为以下共享缝隙产生冲突或返工：
1. 各模块对“绿/黄/红判定函数叫什么、入参是什么、返回值是什么”缺乏契约，各自假设会导致接口不齐；
2. 各模块（生日订单、定时转账、账单跑批）对“今天到底几月几号”来源各异（有的 `datetime.now()`，有的调银行端），导致拨钟无法全局生效；
3. SSE 事件字典和数据库迁移版本若未提前锁定，多条分支同时增加事件或迁移文件会导致合并冲突。

**L0 的单一职责：建立强类型契约、冻结共享面，工期 1 天。L0 验收通过后，L1 的 A/B/C 三条线方可安全并行。**

---

## 2. 详细规格定义

### 2.1 任务 0.1：统一策略引擎接口契约 (Policy Engine Seam)

新建独立目录与包：`backend/app/policy/`。本阶段只定契约与最小纯函数决策逻辑，不触碰数据库，不修改各业务模块。

#### 2.1.1 类型定义 (`backend/app/policy/types.py`)

```python
"""策略引擎核心类型定义。模型外的纯规则门禁，不接收自然语言。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RiskLevel(StrEnum):
    """三级权限与风控分级。严格对应赛题要求。"""
    GREEN = "green"    # 绿标：低风险，直接执行（读操作、普通查询）
    YELLOW = "yellow"  # 黄标：中风险，需用户显式确认（HITL 确认卡）
    RED = "red"        # 红标：高风险，需强身份核验（HITL 确认卡 + 验证码/MFA）


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """策略判定结果。"""
    level: RiskLevel
    confirm_required: bool
    mfa_required: bool
    reason: str  # 结构化原因说明，直接用于审计与前端回显
```

#### 2.1.2 判定接口与基准映射 (`backend/app/policy/evaluator.py`)

提供全局无副作用的分类判定函数。

```python
"""策略决策评估器。"""

from __future__ import annotations

from app.policy.types import PolicyDecision, RiskLevel

# 赛题硬性限额口径：当日累计金额 > 1000 元（100000 分）强制升红标
DAILY_RED_THRESHOLD_MINOR: int = 100000

# 1. 固有红标敏感操作表（任何金额或非金额均红标）
INTRINSIC_RED_OPERATIONS: frozenset[str] = frozenset(
    {
        "card_loss",         # 银行卡挂失
        "card_freeze",       # 银行卡冻结
        "wealth_purchase",   # 理财申购（大额投资）
    }
)

# 2. 资金变动类操作表（按 单笔 + 当日累计 判定）
FUND_WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "transfer",          # 账户转账
        "birthday_order",    # 生日礼物预订扣款
        "subscription_charge",  # 订阅代扣执行
        "aa_payment",        # AA收款支付
    }
)

# 3. 固有黄标配置类操作（需用户确认但免验证码）
INTRINSIC_YELLOW_OPERATIONS: frozenset[str] = frozenset(
    {
        "card_limit_modify",     # 调整限额
        "card_restriction",      # 交易限制设置
        "card_unfreeze",         # 银行卡解冻
        "card_unloss",           # 银行卡解挂
        "card_apply",            # 申请虚拟卡
        "subscription_cancel",   # 取消订阅
        "subscription_pause",    # 暂停订阅
        "subscription_resume",   # 恢复订阅
        "wealth_redeem",         # 理财赎回（资金返还自己账户，黄标确认即可）
    }
)


def classify_operation(
    operation: str,
    amount_minor: int = 0,
    daily_accumulated_minor: int = 0,
) -> PolicyDecision:
    """根据操作类型、单笔金额与当日已累计金额做出分级决策。
    
    规则严格由代码定义，禁止读取模型输出的自然语言。
    防御性设计：
    1. 操作名统一去除首尾空格并小写；
    2. 金额严格非负，负数自动归零。
    """
    op = (operation or "").strip().lower()
    amt = max(0, int(amount_minor or 0))
    accumulated = max(0, int(daily_accumulated_minor or 0))

    # 1. 固有红标
    if op in INTRINSIC_RED_OPERATIONS:
        return PolicyDecision(
            level=RiskLevel.RED,
            confirm_required=True,
            mfa_required=True,
            reason=f"高敏感操作 [{op}] 强制执行红标验证码核验",
        )

    # 2. 资金类操作（动态累计升降级）
    if op in FUND_WRITE_OPERATIONS:
        projected = accumulated + amt
        if projected > DAILY_RED_THRESHOLD_MINOR:
            return PolicyDecision(
                level=RiskLevel.RED,
                confirm_required=True,
                mfa_required=True,
                reason=(
                    f"当日累计金额达 ¥{projected / 100:.2f} "
                    f"(超过日限额 ¥{DAILY_RED_THRESHOLD_MINOR / 100:.2f})，升级为红标验证码核验"
                ),
            )
        return PolicyDecision(
            level=RiskLevel.YELLOW,
            confirm_required=True,
            mfa_required=False,
            reason="常规资金变动操作，需要用户卡片确认",
        )

    # 3. 固有黄标
    if op in INTRINSIC_YELLOW_OPERATIONS:
        return PolicyDecision(
            level=RiskLevel.YELLOW,
            confirm_required=True,
            mfa_required=False,
            reason=f"配置变更操作 [{op}] 需要用户显式确认",
        )

    # 4. 默认绿标
    return PolicyDecision(
        level=RiskLevel.GREEN,
        confirm_required=False,
        mfa_required=False,
        reason="低风险操作，直接放行",
    )


def requires_code(level: RiskLevel | str) -> bool:
    """便捷工具：当前等级是否要求验证码。"""
    lvl_str = level.value if isinstance(level, RiskLevel) else str(level)
    return lvl_str.lower() == RiskLevel.RED.value
```

#### 2.1.3 模块导出 (`backend/app/policy/__init__.py`)

```python
from app.policy.evaluator import (
    DAILY_RED_THRESHOLD_MINOR,
    classify_operation,
    requires_code,
)
from app.policy.types import PolicyDecision, RiskLevel

__all__ = [
    "DAILY_RED_THRESHOLD_MINOR",
    "PolicyDecision",
    "RiskLevel",
    "classify_operation",
    "requires_code",
]
```

---

### 2.2 任务 0.2：“今天”的唯一来源 (Unified Clock Seam)

在 `backend/app/core/clock.py` 建立系统级统一时钟访问器。

#### 2.2.1 避坑与设计原则 (Self-Review Key Findings)
1. **时区绝对统一 (CST, UTC+8)**：
   银行端 `bank_backend/app/core/timeutil.py` 严格定义统一采用东八区（CST）。`SystemClock` 返回的 `datetime` 必须显式携带 `timezone(timedelta(hours=8))` 时区对象，杜绝 Python 在比较 offset-naive 与 offset-aware 时抛出 `TypeError: can't compare offset-naive and offset-aware datetimes`。
2. **Windows 本地代理避坑 (`trust_env=False`)**：
   在发起对 `127.0.0.1:8001` 的 HTTP 请求时，必须设置 `trust_env=False`，防止 Windows 开发者开启代理软件（如 Clash、VPN）时将内部请求错误转发导致 502/连接失败。
3. **连接池复用与性能缓存**：
   维护模块级长连接 Client，带 3.0 秒 TTL 缓存；提供 `SystemClock.invalidate()` 供外部演示修改时钟时立即刷新。

#### 2.2.2 契约代码 (`backend/app/core/clock.py`)

```python
"""统一系统时钟契约。全系统业务逻辑获取‘今天’或‘当前时间’的唯一法定出口。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import time

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 统一东八区（CST）
CST = timezone(timedelta(hours=8))

_CLOCK_CACHE_TTL_SECONDS: float = 3.0
_last_fetch_time: float = 0.0
_cached_datetime: datetime | None = None
_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        base = settings.bank_backend_base_url
        _client = httpx.Client(
            base_url=base.rstrip("/"),
            timeout=httpx.Timeout(1.0, connect=0.5),
            # 避坑：针对本地银行后端绕过 Windows 系统代理
            trust_env=httpx.URL(base).host not in ("127.0.0.1", "localhost", "::1"),
        )
    return _client


class SystemClock:
    @classmethod
    def now(cls, client: httpx.Client | None = None) -> datetime:
        """获取当前业务时间（带 CST 时区，优先读取银行模拟时钟）。"""
        global _last_fetch_time, _cached_datetime
        now_mono = time.monotonic()
        if _cached_datetime is not None and (now_mono - _last_fetch_time) < _CLOCK_CACHE_TTL_SECONDS:
            return _cached_datetime

        http = client or _get_http_client()
        try:
            resp = http.get("/clock")
            if resp.status_code == 200:
                payload = resp.json().get("data", {})
                iso_str = payload.get("current_time")
                if iso_str:
                    parsed = datetime.fromisoformat(iso_str)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=CST)
                    _cached_datetime = parsed.astimezone(CST)
                    _last_fetch_time = now_mono
                    return _cached_datetime
        except Exception as exc:
            logger.debug("读取银行端模拟时钟失败，降级为本地系统时间: %s", exc)

        fallback = datetime.now(CST)
        _cached_datetime = fallback
        _last_fetch_time = now_mono
        return fallback

    @classmethod
    def today(cls, client: httpx.Client | None = None) -> date:
        """获取当前业务日期（基于 CST）。"""
        return cls.now(client).date()

    @classmethod
    def invalidate(cls) -> None:
        """清除缓存（用于外部设置时钟后强制重新拉取）。"""
        global _last_fetch_time, _cached_datetime
        _cached_datetime = None
        _last_fetch_time = 0.0
```

---

### 2.3 任务 0.3：冻结三个共享面 (Freeze Shared Seams)

#### 2.3.1 共享面 1：SSE 事件字典扩充

修改 `backend/app/api/sse.py`：
保持原有 8 个事件不变，正式加入为 L1/L2 预留的 3 个规范事件名称：

```python
# backend/app/api/sse.py line 31
EVENT_NAMES = frozenset(
    {
        # --- 现有基础事件 ---
        "agent_start",
        "tool_call",
        "tool_result",
        "interrupt",
        "subtask_end",
        "reply_delta",
        "narration",
        "final",
        # --- L0 冻结预留事件 ---
        "plan",         # 任务图结构（全量节点、所有者、依赖）
        "task_update",   # 任务节点实时状态变更 (pending/running/done/failed)
        "clock",        # 模拟时钟变动通知
    }
)
```

#### 2.3.2 共享面 2：Supervisor 提示词与派发逻辑权限冻结

在 L1 完成之前，对 `backend/app/agents/supervisor/` 和 `backend/app/agents/prompts.py` 做出如下**权限与改动规则冻结**：

1. **禁止在 Supervisor 中引入业务判定**：
   - 绿/黄/红判定必须发生在各领域图（或 ToolGateway 之前），Supervisor 绝不干涉。
2. **`SUPERVISOR_PROMPT` 模板冻结**：
   - 保持现有提示词模板结构；在 L1 阶段，各领域 Agent 通过其已注册的 `DomainDeclaration.purpose` 提供描述，严禁在 `SUPERVISOR_PROMPT` 硬编码特定模块的补丁规则。
3. **多域 Handoff 契约**：
   - `HandoffRequest` 仅允许传递声明好的上下文白名单字段，禁止传递自然语言的指令劫持文本。

#### 2.3.3 共享面 3：数据库迁移序号与 DDL 结构冻结

当前最新版本为 `0015_bill_doc_tenant_unique.py`。
正式锁定下一个迁移版本为 **`0016_daily_operation_total`**。

- **文件名**：`backend/app/storage/migrations/versions/0016_daily_operation_total.py`
- **Revision 标识**：`"0016_daily_operation_total"`
- **Down Revision**：`"0015_bill_doc_tenant_unique"`
- **表 DDL 规格定义**：

```python
"""Add daily_operation_total table for tracking user daily accumulated amount.

Revision ID: 0016_daily_operation_total
Revises: 0015_bill_doc_tenant_unique
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_daily_operation_total"
down_revision = "0015_bill_doc_tenant_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_operation_total",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "business_date", "operation_kind", name="uq_daily_op_user_date_kind"),
    )
    op.create_index("ix_daily_op_user_date", "daily_operation_total", ["user_id", "business_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_op_user_date", table_name="daily_operation_total")
    op.drop_table("daily_operation_total")
```

#### 2.3.4 架构裁决 ADR-L0-N6：日累计限额的权威计算与状态归属
**裁决结果**：日累计限额由 **Agent 侧 `daily_operation_total` 独立权威维护**，银行侧不设跨业务日累计状态，只做单笔底线校验。
- **背景与原因**：
  1. 跨业务多渠道统一控额：转账（transfer）、生日送礼订单（life_event）、订阅扣款（subscription）属于不同领域的资金写操作。银行沙箱核心系统只提供各子系统的独立执行端点，缺乏跨域日累计聚合计费表；
  2. 智能体风控主权：Agent 侧作为业务门户，必须在下发指令前结合 `daily_operation_total` 与 `PolicyEngine` 进行统一的红/黄/绿标风控拦截；
- **落地实现规约 (L1-B2)**：
  1. **查询点**：在写操作发起确认（prepare 阶段）时，读取当前 `user_id` 在 `SystemClock.today()` 下的累计额；
  2. **累加时机**：在 `WriteFacade` 收到银行端 200/201 成功响应后原子累加；若银行端报错则不累加，保证记账幂等与最终一致；
  3. **兜底与防线**：若 Agent 端查库异常，单笔超限判定仍强制生效（单笔 > 1000 元直接红标），且银行端单笔风控做最后一道硬拦截。

---

## 3. L0 验收标准 (Acceptance Criteria)

- [x] **AC-L0-1 (策略纯函数与防御)**：
  - `classify_operation("transfer", 60000, 0)` -> YELLOW, confirm=True, mfa=False
  - `classify_operation("transfer", 60000, 50000)` -> RED, confirm=True, mfa=True (累计 1100 > 1000)
  - `classify_operation("card_loss")` -> RED (无条件)
  - `classify_operation("query_balance")` -> GREEN
  - `classify_operation("transfer", -100, -100)` -> 负数安全归零，正常输出 YELLOW
  - `requires_code(RiskLevel.RED)` 与 `requires_code("red")` 均返回 True
- [x] **AC-L0-2 (时钟东八区与容错)**：
  - `SystemClock.now()` 必定附带 CST 时区；
  - 银行可用时读取银行端时间，3 秒内连续读取命中内存缓存；
  - `SystemClock.invalidate()` 清除缓存后即刻重新拉取；
  - 模拟银行断网时，不抛出任何异常，平滑降级返回本地 CST 时间。
- [x] **AC-L0-3 (SSE 字典扩充验证)**：
  - `encode_event` 支持 `plan`、`task_update`、`clock`，输出合规 SSE 格式；未注册事件依然抛出 `ValueError`。
- [x] **AC-L0-4 (数据库迁移健全性)**：
  - 迁移脚本 `0016_daily_operation_total.py` 链条与 0015 闭合，可通过 alembic upgrade 和 downgrade 验证。

---

## 4. 实施记录（2026-10-22 完成）

### 4.1 提交链（dev 分支，按序）

| 提交 | 内容 |
|---|---|
| `2539b3a` | docs：本 spec 与 L1 实施计划 |
| `ca2d8d2` | 0.1 `backend/app/policy/`（types + evaluator） |
| `e433894` | 0.2 `backend/app/core/clock.py`（银行时钟 + 3s TTL 缓存） |
| `36759c4` | 0.3 SSE 事件表扩充（`plan` / `task_update` / `clock`） |
| `b5bcc22` | 契约测试 |
| `1c9bcd5` | 迁移 `0016_daily_operation_total` |
| `d92f21b` | L0 契约集成测试 |
| `88d4de7` | 第一轮审查修复（13 条：fail-open / 时钟未接入 / 降级静默 / 跨端白名单 / 迁移空转 / 文档不一致） |
| `c5c402a` | 第二轮复审修复（N1–N6：畸形响应兜底 / `transfer_prepare` 误判 / `card_cancel` 漏登记 / `ProcessClock` 改名 / 报表期接入时钟 / ADR-L0-N6） |

### 4.2 生产代码落点

| 契约 | 文件 |
|---|---|
| 策略引擎 | `backend/app/policy/types.py`、`backend/app/policy/evaluator.py`（`TOOL_TO_OPERATION` 映射 + Fail-Closed 兜底） |
| 统一时钟 | `backend/app/core/clock.py`（`now/today/invalidate/source/is_fallback`）；注入 `backend/app/modules/dates.py`、`backend/app/analysis/summarizer.py`、`backend/app/analysis/cmb_parser.py` |
| SSE 共享面 | `backend/app/api/sse.py`（`EVENT_NAMES`）、`frontend/src/api.ts`（`AGENT_EVENT_NAMES`）、`frontend/src/types.ts`（`AgentEvent`） |
| 迁移序列 | `backend/app/storage/migrations/versions/0016_daily_operation_total.py` |

### 4.3 验收结果

- **AC-L0-1 ~ AC-L0-4 全部通过**，对应测试：`backend/tests/policy/test_policy_engine.py`、`backend/tests/core/test_system_clock.py`、`backend/tests/api/test_sse_events.py`、`backend/tests/storage/test_daily_total_migration.py`（真实 DDL 往返，非空转）、`backend/tests/test_l0_contracts_integration.py`。
- **全量回归**：`./pytest-local.ps1 -Parallel 8 -q` → **1877 passed, 50 skipped, 0 failed**（本机 PostgreSQL，2m40s）。
- 两轮审查共提出 3 个 Critical + 4 个 Important + 6 个 Minor，全部闭环；修复过程中未出现"改测试迁就实现"的情况。

### 4.4 已知遗留（不阻塞 L1）

1. ADR-L0-N6 里"读取日累计异常时按 0 计入"是 fail-open，与整体 fail-closed 基调相反；建议在 L1-B2 实施时改为"读失败 → 当日累计按 RED 处理 + 写审计事件 `daily_total_unavailable`"。
2. 时钟测试重写后丢掉了 `httpx.ReadTimeout` 用例（现只覆盖 `ConnectError`），建议补第 8 个畸形响应 case。
3. `backend/app/analysis/summarizer.py` 的 `from datetime import date` 已成死 import。
4. `classify_operation` 在**不传** `side_effect` 时对未知操作一律判 RED；L1-B3 接线必须传 `spec.side_effect`，不能只传工具名——这条要进 B3 的验收清单。

### 4.5 L1 解锁

L0 的出口标准（"三份契约写进代码/文档，后续并行不返工"）已达成，L1 的 A（life_event）、B（策略与限额）、C（演示控制面）三条线互不依赖，可同时开工。
