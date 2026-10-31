"""订阅与代扣。

每个账期只生成一次扣款：subscription_charges 上的 UNIQUE(subscription_id, due_date)
是数据库层的最终保证，服务层在扣款前也会显式检查。

扣款与定时转账、生日订单共用 scheduled_tasks 和 /clock/run-tasks：订阅在创建、恢复
或完成一次扣款后，为下一个 due date 登记一条 subscription_charge 任务。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import BankError, BusinessError, NotFoundError, ValidationError
from app.core.ids import CHARGE, SUBSCRIPTION, new_id
from app.core.timeutil import advance_due_date, day_start
from app.domain import audit_service, clock_service, security_service
from app.domain.idempotency_service import execute_once, request_fingerprint
from app.domain.money_service import DEBIT, post_entry
from app.domain.ownership import owned
from app.storage import tasks as task_store
from app.storage.database import Database
from app.storage.models import Account, Subscription, SubscriptionCharge

ACTIVE = "active"
PAUSED = "paused"
CANCELLED = "cancelled"

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")

COMPLETED = "completed"
FAILED = "failed"

MAX_SUBSCRIPTION_MINOR = 100000000
# 扣款发生当天的 08:00（东八区）。
CHARGE_HOUR = 8


def subscription_view(subscription: Subscription) -> dict:
    return {
        "subscription_id": subscription.id,
        "user_id": subscription.user_id,
        "from_account_id": subscription.from_account_id,
        "name": subscription.name,
        "merchant_name": subscription.merchant_name,
        "amount_minor": subscription.amount_minor,
        "frequency": subscription.frequency,
        "start_date": subscription.start_date.isoformat(),
        "next_due_date": subscription.next_due_date.isoformat(),
        "status": subscription.status,
        "auto_renew": subscription.auto_renew,
        "last_charge_id": subscription.last_charge_id,
    }


def charge_view(charge: SubscriptionCharge) -> dict:
    return {
        "charge_id": charge.id,
        "subscription_id": charge.subscription_id,
        "due_date": charge.due_date.isoformat(),
        "amount_minor": charge.amount_minor,
        "transaction_id": charge.transaction_id,
        "status": charge.status,
    }


def charge_time(due_date: date) -> datetime:
    return day_start(due_date) + timedelta(hours=CHARGE_HOUR)


class SubscriptionService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    # ------------------------------------------------------------ 查询

    def list_subscriptions(self, *, user_id: str, status: str | None = None) -> list[dict]:
        with self.database.sessions() as session:
            from app.domain.account_service import require_user

            require_user(session, user_id)
            statement = (
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.id)
            )
            if status:
                statement = statement.where(Subscription.status == status)
            return [subscription_view(item) for item in session.execute(statement).scalars()]

    def get(self, subscription_id: str, *, user_id: str) -> dict:
        with self.database.sessions() as session:
            return subscription_view(self._load(session, subscription_id, user_id))

    # ------------------------------------------------------------ 创建

    def create(
        self,
        *,
        user_id: str,
        name: str,
        merchant_name: str,
        amount_minor: int,
        frequency: str,
        from_account_id: str,
        start_date: date,
        auto_renew: bool = True,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "create_subscription"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    if frequency not in FREQUENCIES:
                        raise ValidationError(f"frequency 必须是 {FREQUENCIES} 之一")
                    if (
                        isinstance(amount_minor, bool)
                        or not isinstance(amount_minor, int)
                        or amount_minor <= 0
                        or amount_minor > MAX_SUBSCRIPTION_MINOR
                    ):
                        raise ValidationError("amount_minor 超出允许范围")

                    # 代扣账户必须是本人的，理由同转账：只校验存在就等于允许拿别人的卡代扣。
                    account = owned(session, Account, from_account_id, user_id, label="账户")
                    if account.account_type == "fixed":
                        raise BusinessError(f"定期账户 {from_account_id} 不支持代扣")

                    def factory() -> dict:
                        subscription = Subscription(
                            id=new_id(SUBSCRIPTION),
                            user_id=user_id,
                            from_account_id=account.id,
                            name=name,
                            merchant_name=merchant_name,
                            amount_minor=amount_minor,
                            frequency=frequency,
                            start_date=start_date,
                            next_due_date=start_date,
                            status=ACTIVE,
                            auto_renew=auto_renew,
                        )
                        session.add(subscription)
                        session.flush()
                        _schedule_next_charge(session, subscription)

                        payload = subscription_view(subscription)
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="subscription",
                            resource_id=subscription.id,
                            user_id=user_id,
                            new_value=payload,
                            reason=f"创建订阅 {name}",
                            request_id=request_id,
                        )
                        return payload

                    result, replayed = execute_once(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        result_factory=factory,
                        request_hash=request_fingerprint(
                            {
                                "name": name,
                                "merchant_name": merchant_name,
                                "amount_minor": amount_minor,
                                "frequency": frequency,
                                "from_account_id": from_account_id,
                                "start_date": start_date.isoformat(),
                            }
                        ),
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            audit_service.record_in_new_transaction(
                self.database,
                action=action,
                resource_type="subscription",
                user_id=user_id,
                new_value={"name": name, "amount_minor": amount_minor},
                result=audit_service.RESULT_FAILURE,
                reason=exc.reason,
                request_id=request_id,
            )
            raise

    # ------------------------------------------------------------ 状态流转

    def pause(self, subscription_id: str, *, user_id: str) -> dict:
        return self._transition(
            subscription_id,
            user_id=user_id,
            action="pause_subscription",
            reason="暂停订阅",
            allowed=(ACTIVE,),
            new_status=PAUSED,
            cancel_pending_task=True,
        )

    def resume(self, subscription_id: str, *, user_id: str) -> dict:
        return self._transition(
            subscription_id,
            user_id=user_id,
            action="resume_subscription",
            reason="恢复订阅",
            allowed=(PAUSED,),
            new_status=ACTIVE,
            schedule_next=True,
        )

    def cancel(self, subscription_id: str, *, user_id: str) -> dict:
        return self._transition(
            subscription_id,
            user_id=user_id,
            action="cancel_subscription",
            reason="取消订阅",
            allowed=(ACTIVE, PAUSED),
            new_status=CANCELLED,
            cancel_pending_task=True,
        )

    def _transition(
        self,
        subscription_id: str,
        *,
        user_id: str,
        action: str,
        reason: str,
        allowed: tuple[str, ...],
        new_status: str,
        cancel_pending_task: bool = False,
        schedule_next: bool = False,
    ) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                subscription = self._load(session, subscription_id, user_id)
                if subscription.status not in allowed:
                    raise BusinessError(
                        f"订阅 {subscription_id} 状态为 {subscription.status}，不允许该操作"
                    )
                old = subscription_view(subscription)
                subscription.status = new_status
                subscription.auto_renew = new_status == ACTIVE
                session.flush()

                if cancel_pending_task:
                    _cancel_pending_charges(session, subscription.id)
                if schedule_next:
                    _schedule_next_charge(session, subscription)

                payload = subscription_view(subscription)
                audit_service.record(
                    session,
                    action=action,
                    resource_type="subscription",
                    resource_id=subscription.id,
                    user_id=subscription.user_id,
                    old_value=old,
                    new_value=payload,
                    reason=reason,
                )
                return payload

    # ------------------------------------------------------------ 扣款

    def charge_now(
        self,
        subscription_id: str,
        *,
        user_id: str,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """立即对当前 next_due_date 执行一次代扣，不等模拟时钟。"""
        action = "charge_subscription"
        due_date: date | None = None
        try:
            with self.database.sessions() as session:
                with session.begin():
                    subscription = self._load(session, subscription_id, user_id)
                    if subscription.status != ACTIVE:
                        raise BusinessError(
                            f"订阅 {subscription_id} 状态为 {subscription.status}，不可扣款"
                        )
                    now = clock_service.ClockService(self.database).current_time()
                    due_date = subscription.next_due_date

                    def factory() -> dict:
                        charge = _charge(
                            session, subscription, due_date, now, action=action, request_id=request_id
                        )
                        # 扣完当期后为下一期登记任务，保持与 run-due 路径一致。
                        _schedule_next_charge(session, subscription)
                        return charge_view(charge)

                    result, replayed = execute_once(
                        session,
                        user_id=subscription.user_id,
                        # 幂等键只按操作类型归属：扣款成功会推进 due date，
                        # 若把账期编进 operation_type，同一键重试会落到下一个账期上去。
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        result_factory=factory,
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            # 事务已回滚，用新事务补记失败账期、失败审计与安全失败计数。
            with self.database.sessions() as session:
                with session.begin():
                    subscription = session.get(Subscription, subscription_id)
                    if subscription is not None:
                        if due_date is not None:
                            record_failed_charge(session, subscription, due_date, exc.reason)
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="subscription",
                            resource_id=subscription_id,
                            user_id=subscription.user_id,
                            new_value={"subscription_id": subscription_id},
                            result=audit_service.RESULT_FAILURE,
                            reason=exc.reason,
                            request_id=request_id,
                        )
                        security_service.bump_failure(
                            session,
                            self.database,
                            user_id=subscription.user_id,
                            tool_name=security_service.TOOL_SUBSCRIPTION_CHARGE,
                            reason=exc.reason,
                        )
            raise

    def run_due(self) -> dict:
        """只处理订阅任务，与 /clock/run-tasks 共用同一套执行逻辑。"""
        clock = clock_service.ClockService(self.database)
        now = clock.current_time()
        with self.database.sessions() as session:
            with session.begin():
                _schedule_missing_charges(session, now)
        return clock.run_due_tasks(task_types=(task_store.TASK_SUBSCRIPTION_CHARGE,))

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _load(session: Session, subscription_id: str, user_id: str) -> Subscription:
        return owned(session, Subscription, subscription_id, user_id, label="订阅")


def _find_charge(session: Session, subscription_id: str, due_date: date) -> SubscriptionCharge | None:
    return session.execute(
        select(SubscriptionCharge).where(
            SubscriptionCharge.subscription_id == subscription_id,
            SubscriptionCharge.due_date == due_date,
        )
    ).scalar_one_or_none()


def _charge(
    session: Session,
    subscription: Subscription,
    due_date: date,
    now: datetime,
    *,
    action: str,
    request_id: str | None,
) -> SubscriptionCharge:
    """执行一次代扣。每个账期只允许成功扣款一次。"""
    existing = _find_charge(session, subscription.id, due_date)
    if existing is not None:
        raise BusinessError(
            f"订阅 {subscription.id} 的 {due_date.isoformat()} 账期已存在扣款记录"
            f"（{existing.status}），每个账期只生成一次扣款"
        )

    account = session.get(Account, subscription.from_account_id)
    if account is None:
        raise NotFoundError(f"账户不存在: {subscription.from_account_id}")

    # 余额不足会抛 InsufficientBalanceError，由调用方回滚并在新事务里补记失败账期。
    entry = post_entry(
        session,
        account=account,
        direction=DEBIT,
        amount_minor=subscription.amount_minor,
        transaction_type="subscription",
        category="subscription",
        remark=f"{subscription.name} 订阅扣款",
        reference_type="subscription",
        reference_id=subscription.id,
        created_at=now,
    )

    charge = SubscriptionCharge(
        id=new_id(CHARGE),
        subscription_id=subscription.id,
        due_date=due_date,
        amount_minor=subscription.amount_minor,
        transaction_id=entry.id,
        status=COMPLETED,
    )
    session.add(charge)
    session.flush()
    subscription.last_charge_id = charge.id
    subscription.next_due_date = advance_due_date(due_date, subscription.frequency)
    session.flush()

    audit_service.record(
        session,
        action=action,
        resource_type="subscription_charge",
        resource_id=charge.id,
        user_id=subscription.user_id,
        old_value={"next_due_date": due_date.isoformat()},
        new_value=charge_view(charge),
        reason=f"{subscription.name} 代扣",
        request_id=request_id,
    )
    return charge


def record_failed_charge(
    session: Session, subscription: Subscription, due_date: date, reason: str | None
) -> SubscriptionCharge | None:
    """补记一条失败账期并推进 next_due_date。

    必须在失败事务回滚之后的新事务里调用：任务的保存点会连带回滚处理器写下的账期行。
    账期唯一键仍然成立，因此同一账期不会既失败又成功。
    """
    if _find_charge(session, subscription.id, due_date) is not None:
        return None
    charge = SubscriptionCharge(
        id=new_id(CHARGE),
        subscription_id=subscription.id,
        due_date=due_date,
        amount_minor=subscription.amount_minor,
        status=FAILED,
    )
    session.add(charge)
    subscription.next_due_date = advance_due_date(due_date, subscription.frequency)
    session.flush()
    return charge


def _schedule_next_charge(session: Session, subscription: Subscription) -> None:
    """为下一个 due date 登记扣款任务；已有同账期任务则不重复登记。"""
    if subscription.status != ACTIVE or not subscription.auto_renew:
        return
    due = subscription.next_due_date
    existing = session.execute(
        select(task_store.ScheduledTask).where(
            task_store.ScheduledTask.task_type == task_store.TASK_SUBSCRIPTION_CHARGE,
            task_store.ScheduledTask.status == task_store.SCHEDULED,
            task_store.ScheduledTask.payload["subscription_id"].astext == subscription.id,
            task_store.ScheduledTask.payload["due_date"].astext == due.isoformat(),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    task_store.create_task(
        session,
        user_id=subscription.user_id,
        task_type=task_store.TASK_SUBSCRIPTION_CHARGE,
        execute_at=charge_time(due),
        payload={
            "subscription_id": subscription.id,
            "due_date": due.isoformat(),
            "amount_minor": subscription.amount_minor,
        },
    )


def _cancel_pending_charges(session: Session, subscription_id: str) -> None:
    pending = session.execute(
        select(task_store.ScheduledTask).where(
            task_store.ScheduledTask.task_type == task_store.TASK_SUBSCRIPTION_CHARGE,
            task_store.ScheduledTask.status == task_store.SCHEDULED,
            task_store.ScheduledTask.payload["subscription_id"].astext == subscription_id,
        )
    ).scalars()
    for task in pending:
        task_store.cancel_task(session, task)


def _schedule_missing_charges(session: Session, now: datetime) -> None:
    """把已经到期但没有待办任务的活跃订阅补登记一条，供 run-due 追赶。"""
    subscriptions = session.execute(
        select(Subscription).where(
            Subscription.status == ACTIVE,
            Subscription.auto_renew.is_(True),
            Subscription.next_due_date <= now.date(),
        )
    ).scalars()
    for subscription in subscriptions:
        _schedule_next_charge(session, subscription)


# ---------------------------------------------------------------- 任务处理器


def handle_subscription_charge(session: Session, task, context) -> dict:
    payload = task.payload or {}
    subscription = session.get(Subscription, payload["subscription_id"])
    if subscription is None:
        raise NotFoundError(f"订阅不存在: {payload['subscription_id']}")
    if subscription.status != ACTIVE:
        raise BusinessError(f"订阅 {subscription.id} 状态为 {subscription.status}，跳过代扣")

    due_date = date.fromisoformat(payload["due_date"])
    charge = _charge(
        session,
        subscription,
        due_date,
        context.now,
        action="run_subscription_charge",
        request_id=None,
    )
    # 扣完当期后立刻为下一期登记任务，演示可以连续拨钟。
    _schedule_next_charge(session, subscription)
    return charge_view(charge)


def _on_charge_failure(session: Session, task, reason: str | None) -> None:
    """任务保存点回滚之后补记失败账期，避免同一账期反复重试。"""
    payload = task.payload or {}
    subscription = session.get(Subscription, payload.get("subscription_id"))
    if subscription is None:
        return
    record_failed_charge(session, subscription, date.fromisoformat(payload["due_date"]), reason)


def register() -> None:
    clock_service.register_handler(
        task_store.TASK_SUBSCRIPTION_CHARGE, handle_subscription_charge
    )
    clock_service.register_failure_hook(
        task_store.TASK_SUBSCRIPTION_CHARGE, _on_charge_failure
    )


register()
