"""scheduled_tasks 的持久化助手。

定时转账、生日订单、订阅扣款共用同一张任务表，因此也共用这里的创建、
到期扫描和终态流转。终态保护在这里实现：任务只能 scheduled → running →
{completed, failed, cancelled, expired}，且终态不可再变更。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.core.ids import TASK, new_id
from app.storage.models import ScheduledTask

SCHEDULED = "scheduled"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, EXPIRED})

TASK_SCHEDULED_TRANSFER = "scheduled_transfer"
TASK_BIRTHDAY_ORDER = "birthday_order"
TASK_SUBSCRIPTION_CHARGE = "subscription_charge"


def create_task(
    session: Session,
    *,
    user_id: str,
    task_type: str,
    execute_at: datetime,
    payload: dict,
    task_id: str | None = None,
) -> ScheduledTask:
    task = ScheduledTask(
        id=task_id or new_id(TASK),
        user_id=user_id,
        task_type=task_type,
        execute_at=execute_at,
        payload=payload,
        status=SCHEDULED,
    )
    session.add(task)
    session.flush()
    return task


def list_due_tasks(
    session: Session,
    now: datetime,
    *,
    task_types: tuple[str, ...] | None = None,
) -> list[ScheduledTask]:
    """execute_at 不晚于模拟时间、且仍处于 scheduled 的任务。"""
    statement = (
        select(ScheduledTask)
        .where(ScheduledTask.status == SCHEDULED, ScheduledTask.execute_at <= now)
        .order_by(ScheduledTask.execute_at, ScheduledTask.id)
    )
    if task_types:
        statement = statement.where(ScheduledTask.task_type.in_(task_types))
    return list(session.execute(statement).scalars())


def claim_task(session: Session, task: ScheduledTask) -> None:
    """把任务从 scheduled 置为 running；已是终态或已被抢占则抛错。"""
    if task.status in TERMINAL_STATUSES:
        raise BusinessError(f"任务 {task.id} 已处于终态 {task.status}")
    if task.status != SCHEDULED:
        raise BusinessError(f"任务 {task.id} 状态为 {task.status}，不可执行")
    task.status = RUNNING
    session.flush()


def finish_task(
    session: Session,
    task: ScheduledTask,
    *,
    status: str,
    result: dict | None = None,
    error_code: str | None = None,
) -> ScheduledTask:
    """进入终态。终态只能写一次。"""
    if status not in TERMINAL_STATUSES:
        raise BusinessError(f"{status} 不是任务的终态")
    if task.status in TERMINAL_STATUSES:
        raise BusinessError(f"任务 {task.id} 已处于终态 {task.status}，不可重复结束")
    task.status = status
    task.result = result
    task.error_code = error_code
    session.flush()
    return task


def cancel_task(session: Session, task: ScheduledTask) -> ScheduledTask:
    if task.status in TERMINAL_STATUSES:
        raise BusinessError(f"任务 {task.id} 已处于终态 {task.status}，不可取消")
    task.status = CANCELLED
    session.flush()
    return task


def list_tasks(
    session: Session,
    *,
    user_id: str | None = None,
    status: str | None = None,
    task_types: tuple[str, ...] | None = None,
) -> list[ScheduledTask]:
    statement = select(ScheduledTask).order_by(ScheduledTask.created_at.desc(), ScheduledTask.id)
    if user_id:
        statement = statement.where(ScheduledTask.user_id == user_id)
    if status:
        statement = statement.where(ScheduledTask.status == status)
    if task_types:
        statement = statement.where(ScheduledTask.task_type.in_(task_types))
    return list(session.execute(statement).scalars())
