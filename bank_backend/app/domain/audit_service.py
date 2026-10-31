"""写操作审计。

每个 mutation 都写一条审计：请求 ID、资源、旧值、新值、权限字段、结果和原因。
成功与失败都留痕——失败业务虽然回滚资金变更，审计必须保留。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ids import AUDIT, new_id
from app.core.timeutil import iso
from app.storage.models import AuditLog

GREEN = "green"
YELLOW = "yellow"
RED = "red"

RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:20]}"


def audit_view(entry: AuditLog) -> dict:
    return {
        "audit_id": entry.id,
        "request_id": entry.request_id,
        "user_id": entry.user_id,
        "action": entry.action,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "old_value": entry.old_value,
        "new_value": entry.new_value,
        "permission_level": entry.permission_level,
        "confirm_required": entry.confirm_required,
        "mfa_required": entry.mfa_required,
        "result": entry.result,
        "reason": entry.reason,
        "created_at": iso(entry.created_at),
    }


def snapshot(instance, fields: tuple[str, ...]) -> dict:
    """从 ORM 对象抽取指定字段，作为审计的 old_value / new_value。"""
    payload: dict = {}
    for field in fields:
        value = getattr(instance, field, None)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        payload[field] = value
    return payload


def record(
    session: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    user_id: str | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    permission_level: str = GREEN,
    confirm_required: bool = False,
    mfa_required: bool = False,
    result: str = RESULT_SUCCESS,
    reason: str | None = None,
    request_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        id=new_id(AUDIT),
        request_id=request_id or new_request_id(),
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        permission_level=permission_level,
        confirm_required=confirm_required,
        mfa_required=mfa_required,
        result=result,
        reason=reason,
    )
    session.add(entry)
    session.flush()
    return entry


def list_logs(
    session: Session,
    *,
    user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AuditLog], int]:
    filters = []
    if user_id:
        filters.append(AuditLog.user_id == user_id)
    if action:
        filters.append(AuditLog.action == action)
    if resource_type:
        filters.append(AuditLog.resource_type == resource_type)

    total = session.execute(
        select(func.count()).select_from(AuditLog).where(*filters)
    ).scalar_one()
    statement = (
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(session.execute(statement).scalars()), int(total)


def record_in_new_transaction(database, **kwargs) -> None:
    """在独立事务中写审计。

    用于失败路径：资金变更已经回滚，但失败审计必须保留下来。
    """
    with database.sessions() as session:
        with session.begin():
            record(session, **kwargs)
