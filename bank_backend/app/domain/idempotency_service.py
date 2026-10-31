"""幂等键重放。

键为 (user_id, operation_type, idempotent_key)。同一用户、同一操作类型、
同一键重复请求直接返回首次保存的结果，不产生第二笔交易或第二次扣款。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.storage.models import IdempotencyRecord


def request_fingerprint(payload: dict) -> str:
    """请求指纹，用于识别同一幂等键被用于不同请求体的误用。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find(
    session: Session, *, user_id: str, operation_type: str, idempotent_key: str
) -> IdempotencyRecord | None:
    return session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.operation_type == operation_type,
            IdempotencyRecord.idempotent_key == idempotent_key,
        )
    ).scalar_one_or_none()


def remember(
    session: Session,
    *,
    user_id: str,
    operation_type: str,
    idempotent_key: str,
    result: dict,
    request_hash: str | None = None,
) -> IdempotencyRecord:
    entry = IdempotencyRecord(
        user_id=user_id,
        operation_type=operation_type,
        idempotent_key=idempotent_key,
        request_hash=request_hash,
        result=result,
    )
    session.add(entry)
    session.flush()
    return entry


def execute_once(
    session: Session,
    *,
    user_id: str,
    operation_type: str,
    idempotent_key: str | None,
    result_factory: Callable[[], dict],
    request_hash: str | None = None,
) -> tuple[dict, bool]:
    """执行一次写操作，返回 (结果, 是否为重放)。

    未传幂等键时每次都真实执行。并发插入同一键时主键冲突会回滚到保存点，
    随后读取已存在的记录并重放，因此不会重复扣款。
    """
    if not idempotent_key:
        return result_factory(), False

    existing = find(
        session, user_id=user_id, operation_type=operation_type, idempotent_key=idempotent_key
    )
    if existing is not None:
        return dict(existing.result), True

    result = result_factory()

    try:
        with session.begin_nested():
            remember(
                session,
                user_id=user_id,
                operation_type=operation_type,
                idempotent_key=idempotent_key,
                result=result,
                request_hash=request_hash,
            )
    except IntegrityError:
        existing = find(
            session, user_id=user_id, operation_type=operation_type, idempotent_key=idempotent_key
        )
        if existing is not None:
            return dict(existing.result), True
        raise

    return result, False
