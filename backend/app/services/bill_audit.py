"""审计日志的写入与脱敏，供任务服务、入库服务与后台处理协调器共用。

写入的是 001 的 ``bill_audit_event``：它的字段语义是「步骤 + 结果」。我方原有的事件名
放在 ``step`` 里（001 的 ``step`` 是 text，无 CHECK，任意字符串合法），``result`` 按语义
取 ``SUCCESS`` / ``FAILURE`` / ``SKIPPED``。
"""

from __future__ import annotations

from app.storage.bill_models import BillTaskAudit

#: 审计详情里必须脱敏的键。密码绝不落库、绝不进日志。
SENSITIVE_KEYS = frozenset({"password", "account_no", "verification_code"})


def sanitize(details: dict) -> dict:
    return {key: ("***" if key in SENSITIVE_KEYS else value) for key, value in (details or {}).items()}


def write_audit(
    db,
    task_id,
    user_id: str,
    step: str,
    detail: dict,
    *,
    result: str = "SUCCESS",
    request_id: str | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """写一条审计事件。``task_id`` 是 ``bill_task.id``（uuid），不是业务号。"""

    db.add(
        BillTaskAudit(
            task_id=task_id,
            user_id=user_id,
            step=step,
            result=result,
            request_id=request_id,
            error_code=error_code,
            detail=sanitize(detail),
            duration_ms=duration_ms,
        )
    )
