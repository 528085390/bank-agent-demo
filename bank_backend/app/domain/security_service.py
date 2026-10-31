"""模拟安全状态：连续失败计数与资金工具锁定。

只做演示用的固定规则，不接入真实风控模型，也不做真实认证或权限阻断。

失败要写在**独立的新事务**里：调用方的资金事务已经回滚，失败审计和失败计数必须
保留下来。达到阈值后锁定该工具一段时间，锁定期内该工具的资金操作会被拒绝，
`/security/reset-lock` 可以清除。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import LockedError
from app.core.timeutil import CST, iso
from app.domain import audit_service
from app.domain.clock_service import ClockService
from app.storage.database import Database
from app.storage.models import SecurityState, SimulationState, User

# 连续失败达到该次数即锁定。
LOCK_THRESHOLD = 5
LOCK_MINUTES = 15

TOOL_FUND_TRANSFER = "fund_transfer"
TOOL_FUND_PURCHASE = "fund_purchase"
TOOL_FUND_REDEEM = "fund_redeem"
TOOL_SUBSCRIPTION_CHARGE = "subscription_charge"
TOOL_BIRTHDAY_ORDER = "birthday_order"


def now(database: Database) -> datetime:
    """锁定以模拟时钟为准，和系统其余部分保持一致；无时钟时退回真实时间。"""
    with database.sessions() as session:
        if session.get(SimulationState, 1) is not None:
            return ClockService(database).current_time()
    return datetime.now(tz=CST)


def security_view(state: SecurityState) -> dict:
    return {
        "user_id": state.user_id,
        "tool_name": state.tool_name,
        "failure_count": state.failure_count,
        "locked_until": iso(state.locked_until),
        "last_reason": state.last_reason,
    }


def _get_or_create(session: Session, user_id: str, tool_name: str) -> SecurityState | None:
    """取回或新建安全状态行。

    security_state.user_id 对 users 有外键，因此用户不存在时无法建行——此时返回 None，
    调用方跳过计数；这次失败本身已经由 audit_logs（无外键）记录下来了。
    """
    state = session.execute(
        select(SecurityState).where(
            SecurityState.user_id == user_id, SecurityState.tool_name == tool_name
        )
    ).scalar_one_or_none()
    if state is not None:
        return state
    if session.get(User, user_id) is None:
        return None
    state = SecurityState(
        user_id=user_id, tool_name=tool_name, failure_count=0, last_reason="ok"
    )
    session.add(state)
    session.flush()
    return state


def is_locked(state: SecurityState, moment: datetime) -> bool:
    return state.locked_until is not None and state.locked_until > moment


def require_not_locked(session: Session, database: Database, user_id: str, tool_name: str) -> None:
    """锁定期内拒绝该工具的资金操作。"""
    state = session.execute(
        select(SecurityState).where(
            SecurityState.user_id == user_id, SecurityState.tool_name == tool_name
        )
    ).scalar_one_or_none()
    if state is None:
        return
    moment = now(database)
    if is_locked(state, moment):
        # 锁有自己的线路码 1009（而不是通用 1005）。require_not_locked 的全部三个调用点
        # 因此一起变：transfer_service.py:163、investment_service.py:199 与 :451。
        # 银行侧 message 是稳定的英文 token "locked"，中文文案只在对话侧。
        raise LockedError(
            f"资金工具 {tool_name} 已锁定至 {state.locked_until.isoformat()}，"
            "请先调用 /security/reset-lock"
        )


def bump_failure(
    session: Session,
    database: Database,
    *,
    user_id: str,
    tool_name: str,
    reason: str | None,
) -> SecurityState | None:
    """在调用方已有的事务里累计一次失败，达到阈值即锁定。"""
    state = _get_or_create(session, user_id, tool_name)
    if state is None:
        return None
    state.failure_count += 1
    state.last_reason = reason
    if state.failure_count >= LOCK_THRESHOLD:
        state.locked_until = now(database) + timedelta(minutes=LOCK_MINUTES)
    session.flush()
    return state


def record_failure(
    database: Database,
    *,
    user_id: str,
    tool_name: str,
    action: str,
    resource_type: str,
    reason: str | None,
    resource_id: str | None = None,
    new_value: dict | None = None,
    request_id: str | None = None,
    permission_level: str | None = None,
    confirm_required: bool = False,
    mfa_required: bool = False,
    security_relevant: bool = True,
) -> None:
    """在新事务里写失败审计，并按需累计失败次数、触发锁定。

    `security_relevant=False` 时只留审计不计数——待确认（pending_confirmation）
    属于正常流程，不应被当成安全失败。
    """
    with database.sessions() as session:
        with session.begin():
            audit_service.record(
                session,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                user_id=user_id,
                new_value=new_value,
                permission_level=permission_level or audit_service.GREEN,
                confirm_required=confirm_required,
                mfa_required=mfa_required,
                result=audit_service.RESULT_FAILURE,
                reason=reason,
                request_id=request_id,
            )
            if not security_relevant:
                return
            bump_failure(
                session,
                database,
                user_id=user_id,
                tool_name=tool_name,
                reason=reason,
            )


class SecurityService:
    def __init__(self, database: Database):
        self.database = database

    def status(self, user_id: str) -> list[dict]:
        with self.database.sessions() as session:
            states = session.execute(
                select(SecurityState)
                .where(SecurityState.user_id == user_id)
                .order_by(SecurityState.tool_name)
            ).scalars()
            return [security_view(state) for state in states]

    def reset_lock(self, user_id: str, tool_name: str | None = None) -> list[dict]:
        with self.database.sessions() as session:
            with session.begin():
                statement = select(SecurityState).where(SecurityState.user_id == user_id)
                if tool_name:
                    statement = statement.where(SecurityState.tool_name == tool_name)
                states = list(session.execute(statement.order_by(SecurityState.tool_name)).scalars())

                for state in states:
                    old = security_view(state)
                    state.failure_count = 0
                    state.locked_until = None
                    state.last_reason = "reset"
                    session.flush()
                    audit_service.record(
                        session,
                        action="reset_security_lock",
                        resource_type="security_state",
                        resource_id=state.tool_name,
                        user_id=user_id,
                        old_value=old,
                        new_value=security_view(state),
                        reason="清除模拟安全锁定",
                    )
                return [security_view(state) for state in states]
