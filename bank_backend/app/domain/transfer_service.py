"""转账、定时转账与统一任务执行。

即时转账在单个事务内完成双边流水、审计和幂等结果；余额不足等基础失败会
回滚资金变更，但失败审计单独落库保留。

定时转账创建 scheduled_tasks 记录，由 /clock/run-tasks 在模拟时间到期时执行。
执行时检查余额，失败不重试。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    BankError,
    BusinessError,
    ConfirmationRequiredError,
    NotFoundError,
    ValidationError,
)
from app.domain import audit_service, clock_service, risk, security_service
from app.domain.idempotency_service import execute_once, request_fingerprint
from app.domain.money_service import post_transfer
from app.domain.ownership import owned
from app.storage import tasks as task_store
from app.storage.database import Database
from app.storage.models import Account, ScheduledTask

MAX_TRANSFER_MINOR = 100000000

TRANSFER_FIELDS = ("from_account_id", "to_account_id", "amount_minor", "remark")


def _load_account(session: Session, account_id: str) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"账户不存在: {account_id}")
    return account


def _writable_account(session: Session, account_id: str, user_id: str) -> Account:
    """转出账户：必须存在、属于本人、并且允许转出。

    归属检查放在最前面而不是调用方：调用方有三处（即时转账、定时转账、到期执行），
    漏一处的后果是「用别人的钱转账」，所以把规则钉在唯一必经的这道门上。
    """

    account = owned(session, Account, account_id, user_id, label="账户")
    if account.account_type == "fixed":
        raise BusinessError(f"定期账户 {account_id} 首版不支持资金转出")
    if account.status != "active":
        raise BusinessError(f"账户 {account_id} 状态为 {account.status}，不可转出")
    return account


def _validate_amount(amount_minor: int) -> int:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValidationError("amount_minor 必须是整数分")
    if amount_minor <= 0:
        raise ValidationError("amount_minor 必须是正整数分")
    if amount_minor > MAX_TRANSFER_MINOR:
        raise ValidationError(f"amount_minor 不能超过 {MAX_TRANSFER_MINOR}")
    return amount_minor


def _transfer_payload(
    outgoing, incoming, *, from_account_id: str, to_account_id: str, decision: risk.Decision
) -> dict:
    return {
        "transaction_id": outgoing.id,
        "counterparty_transaction_id": incoming.id,
        "reference_id": outgoing.reference_id,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount_minor": outgoing.amount_minor,
        "status": "completed",
        **decision.as_dict(),
    }


class TransferService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    # ------------------------------------------------------------ 即时转账

    def transfer(
        self,
        *,
        user_id: str,
        from_account_id: str,
        to_account_id: str,
        amount_minor: int,
        remark: str | None = None,
        confirm: bool = False,
        mfa_code: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "transfer"
        decision = risk.classify(amount_minor) if isinstance(amount_minor, int) else None
        try:
            with self.database.sessions() as session:
                with session.begin():
                    result, replayed = self._transfer(
                        session,
                        user_id=user_id,
                        from_account_id=from_account_id,
                        to_account_id=to_account_id,
                        amount_minor=amount_minor,
                        remark=remark,
                        confirm=confirm,
                        mfa_code=mfa_code,
                        idempotent_key=idempotent_key,
                        request_id=request_id,
                    )
                    if replayed:
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="transfer",
                            resource_id=result["transaction_id"],
                            user_id=user_id,
                            permission_level=result["permission_level"],
                            confirm_required=result["confirm_required"],
                            mfa_required=result["mfa_required"],
                            new_value=result,
                            reason="幂等重放，返回首次结果",
                            request_id=request_id,
                        )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            self._audit_failure(
                action=action,
                resource_type="transfer",
                user_id=user_id,
                reason=exc.reason,
                decision=decision,
                new_value={
                    "from_account_id": from_account_id,
                    "to_account_id": to_account_id,
                    "amount_minor": amount_minor,
                },
                request_id=request_id,
                security_relevant=not isinstance(exc, ConfirmationRequiredError),
            )
            raise

    def _transfer(
        self,
        session: Session,
        *,
        user_id: str,
        from_account_id: str,
        to_account_id: str,
        amount_minor: int,
        remark: str | None,
        confirm: bool,
        mfa_code: str | None,
        idempotent_key: str | None,
        request_id: str | None,
    ) -> tuple[dict, bool]:
        from app.domain.account_service import require_user

        require_user(session, user_id)
        security_service.require_not_locked(
            session, self.database, user_id, security_service.TOOL_FUND_TRANSFER
        )
        _validate_amount(amount_minor)
        if from_account_id == to_account_id:
            raise ValidationError("转出与转入账户不能相同")

        source = _writable_account(session, from_account_id, user_id)
        target = _load_account(session, to_account_id)

        decision = risk.classify(amount_minor)
        risk.require_confirmation(
            decision, confirm=confirm, mfa_code=mfa_code, action="转账"
        )

        def factory() -> dict:
            outgoing, incoming = post_transfer(
                session,
                from_account=source,
                to_account=target,
                amount_minor=amount_minor,
                category="transfer",
                remark=remark,
                reference_type="transfer",
            )
            payload = _transfer_payload(
                outgoing,
                incoming,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                decision=decision,
            )
            audit_service.record(
                session,
                action="transfer",
                resource_type="transfer",
                resource_id=outgoing.id,
                user_id=user_id,
                old_value={"from_balance_minor": source.balance_minor + amount_minor},
                new_value={**payload, "from_balance_minor": source.balance_minor},
                permission_level=decision.permission_level,
                confirm_required=decision.confirm_required,
                mfa_required=decision.mfa_required,
                reason=remark,
                request_id=request_id,
            )
            return payload

        return execute_once(
            session,
            user_id=user_id,
            operation_type="transfer",
            idempotent_key=idempotent_key,
            result_factory=factory,
            request_hash=request_fingerprint(
                {
                    "from_account_id": from_account_id,
                    "to_account_id": to_account_id,
                    "amount_minor": amount_minor,
                }
            ),
        )

    # ------------------------------------------------------------ 定时转账

    def create_scheduled(
        self,
        *,
        user_id: str,
        from_account_id: str,
        to_account_id: str,
        amount_minor: int,
        execute_at: datetime,
        remark: str | None = None,
        confirm: bool = False,
        mfa_code: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "schedule_transfer"
        decision = risk.classify(amount_minor) if isinstance(amount_minor, int) else None
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    _validate_amount(amount_minor)
                    if from_account_id == to_account_id:
                        raise ValidationError("转出与转入账户不能相同")
                    _writable_account(session, from_account_id, user_id)
                    _load_account(session, to_account_id)

                    decision = risk.classify(amount_minor)
                    risk.require_confirmation(
                        decision, confirm=confirm, mfa_code=mfa_code, action="定时转账"
                    )

                    payload = {
                        "from_account_id": from_account_id,
                        "to_account_id": to_account_id,
                        "amount_minor": amount_minor,
                        "remark": remark,
                        "permission_level": decision.permission_level,
                        "confirm_required": decision.confirm_required,
                        "mfa_required": decision.mfa_required,
                    }

                    def factory() -> dict:
                        task = task_store.create_task(
                            session,
                            user_id=user_id,
                            task_type=task_store.TASK_SCHEDULED_TRANSFER,
                            execute_at=execute_at,
                            payload=payload,
                        )
                        view = clock_service.task_view(task)
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="scheduled_task",
                            resource_id=task.id,
                            user_id=user_id,
                            old_value=None,
                            new_value=view,
                            permission_level=decision.permission_level,
                            confirm_required=decision.confirm_required,
                            mfa_required=decision.mfa_required,
                            reason=remark,
                            request_id=request_id,
                        )
                        return view

                    result, replayed = execute_once(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        result_factory=factory,
                        request_hash=request_fingerprint(payload),
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            self._audit_failure(
                action=action,
                resource_type="scheduled_task",
                user_id=user_id,
                reason=exc.reason,
                decision=decision,
                new_value={
                    "from_account_id": from_account_id,
                    "to_account_id": to_account_id,
                    "amount_minor": amount_minor,
                    "execute_at": execute_at.isoformat() if execute_at else None,
                },
                request_id=request_id,
            )
            raise

    def list_scheduled(self, *, user_id: str, status: str | None = None) -> list[dict]:
        with self.database.sessions() as session:
            from app.domain.account_service import require_user

            require_user(session, user_id)
            tasks = task_store.list_tasks(
                session,
                user_id=user_id,
                status=status,
                task_types=(task_store.TASK_SCHEDULED_TRANSFER,),
            )
            return [clock_service.task_view(task) for task in tasks]

    def get_scheduled(self, task_id: str, *, user_id: str) -> dict:
        with self.database.sessions() as session:
            return clock_service.task_view(self._load_task(session, task_id, user_id))

    def cancel_scheduled(
        self, task_id: str, *, user_id: str, request_id: str | None = None
    ) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                task = self._load_task(session, task_id, user_id)
                old = clock_service.task_view(task)
                task_store.cancel_task(session, task)
                view = clock_service.task_view(task)
                audit_service.record(
                    session,
                    action="cancel_scheduled_transfer",
                    resource_type="scheduled_task",
                    resource_id=task.id,
                    user_id=user_id,
                    old_value=old,
                    new_value=view,
                    reason="用户取消定时转账",
                    request_id=request_id,
                )
                return view

    @staticmethod
    def _load_task(session: Session, task_id: str, user_id: str) -> ScheduledTask:
        return owned(session, ScheduledTask, task_id, user_id, label="任务")

    # ------------------------------------------------------------ 失败审计

    def _audit_failure(
        self,
        *,
        action: str,
        resource_type: str,
        user_id: str,
        reason: str | None,
        decision: risk.Decision | None,
        new_value: dict | None,
        request_id: str | None,
        security_relevant: bool = True,
    ) -> None:
        """资金已回滚，但失败审计必须独立落库保留，并累计安全失败次数。"""
        security_service.record_failure(
            self.database,
            user_id=user_id,
            tool_name=security_service.TOOL_FUND_TRANSFER,
            action=action,
            resource_type=resource_type,
            new_value=new_value,
            permission_level=decision.permission_level if decision else risk.GREEN,
            confirm_required=decision.confirm_required if decision else False,
            mfa_required=decision.mfa_required if decision else False,
            reason=reason,
            request_id=request_id,
            security_relevant=security_relevant,
        )


# ---------------------------------------------------------------- 任务处理器


def handle_scheduled_transfer(session: Session, task: ScheduledTask, context) -> dict:
    """到期的定时转账：执行时检查余额，余额不足即失败且不重试。"""
    payload = task.payload or {}
    # 到期执行走的是当初下单的那个人：`task.user_id` 才是一笔定时的主人。
    source = _writable_account(session, payload["from_account_id"], task.user_id)
    target = _load_account(session, payload["to_account_id"])

    outgoing, incoming = post_transfer(
        session,
        from_account=source,
        to_account=target,
        amount_minor=payload["amount_minor"],
        category="transfer",
        remark=payload.get("remark"),
        reference_type="scheduled_transfer",
        reference_id=task.id,
        created_at=context.now,
    )
    result = {
        "transaction_id": outgoing.id,
        "counterparty_transaction_id": incoming.id,
        "reference_id": outgoing.reference_id,
        "amount_minor": outgoing.amount_minor,
    }
    audit_service.record(
        session,
        action="run_scheduled_transfer",
        resource_type="scheduled_task",
        resource_id=task.id,
        user_id=task.user_id,
        new_value=result,
        reason="定时转账到期执行",
    )
    return result


def register() -> None:
    clock_service.register_handler(
        task_store.TASK_SCHEDULED_TRANSFER, handle_scheduled_transfer
    )


register()
