"""生日联动：固定 mock 商品、生日订单与到期任务。

创建订单时登记一条 birthday_order 任务，execute_at 为生日前两天的 08:00。
拨钟后由 /clock/run-tasks 执行：扣款、写 birthday 流水、把订单置为 completed。

mock 结果由 /clock/run-tasks 的 birthday_outcome 控制。fail 时任务与订单都置为
failed，且不产生任何资金流水；因为处理器写在保存点内，订单的失败状态由
clock_service 的失败钩子在保存点回滚之后补记。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import BankError, BusinessError, NotFoundError, ValidationError
from app.core.ids import ORDER, new_id
from app.core.timeutil import day_start
from app.domain import audit_service, clock_service, risk, security_service
from app.domain.idempotency_service import execute_once, request_fingerprint
from app.domain.money_service import DEBIT, post_entry
from app.domain.ownership import owned
from app.storage import tasks as task_store
from app.storage.database import Database
from app.storage.models import Account, Contact, Order

MOCK_PRODUCTS = (
    {
        "product_code": "flowers",
        "name": "模拟鲜花",
        "price_minor": 19900,
        "currency": "CNY",
    },
    {
        "product_code": "cake",
        "name": "模拟蛋糕",
        "price_minor": 12900,
        "currency": "CNY",
    },
)

MOCK_PRODUCT_BY_CODE = {item["product_code"]: item for item in MOCK_PRODUCTS}

# 生日前两天创建并执行订单。
LEAD_DAYS = 2
ORDER_HOUR = 8

PENDING = "pending"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


def order_view(order: Order) -> dict:
    details = order.details or {}
    return {
        "order_id": order.id,
        "order_type": order.order_type,
        "user_id": order.user_id,
        "amount_minor": order.amount_minor,
        "status": order.status,
        "task_id": details.get("task_id"),
        "transaction_id": order.transaction_id,
        "details": details,
    }


def execute_at_for(birthday_date: date) -> datetime:
    return day_start(birthday_date - timedelta(days=LEAD_DAYS)) + timedelta(hours=ORDER_HOUR)


class BirthdayService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    @staticmethod
    def list_mock_products() -> list[dict]:
        return [dict(item) for item in MOCK_PRODUCTS]

    def list_birthday_contacts(self, user_id: str) -> list[dict]:
        from app.domain.account_service import AccountService

        return AccountService(self.database).list_birthdays(user_id)

    def create_order(
        self,
        *,
        user_id: str,
        contact_id: str,
        product_code: str,
        from_account_id: str,
        birthday_date: date,
        confirm: bool = True,
        mfa_code: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "create_birthday_order"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    product = MOCK_PRODUCT_BY_CODE.get(product_code)
                    if product is None:
                        raise ValidationError(
                            f"product_code 必须是 {tuple(MOCK_PRODUCT_BY_CODE)} 之一"
                        )

                    contact = session.get(Contact, contact_id)
                    if contact is None or contact.user_id != user_id:
                        raise NotFoundError(f"联系人不存在: {contact_id}")

                    # 扣款账户必须是本人的：只看「账户存在」等于允许用别人的钱下单。
                    account = owned(session, Account, from_account_id, user_id, label="账户")
                    if account.account_type == "fixed":
                        raise BusinessError(f"定期账户 {from_account_id} 不支持扣款")
                    if account.status != "active":
                        raise BusinessError(f"账户 {from_account_id} 状态为 {account.status}")

                    decision = risk.classify(product["price_minor"])
                    risk.require_confirmation(
                        decision, confirm=confirm, mfa_code=mfa_code, action="生日订单"
                    )

                    execute_at = execute_at_for(birthday_date)

                    def factory() -> dict:
                        order = Order(
                            id=new_id(ORDER),
                            user_id=user_id,
                            order_type="birthday",
                            from_account_id=account.id,
                            amount_minor=product["price_minor"],
                            status=PENDING,
                            details={
                                "contact_id": contact.id,
                                "contact_name": contact.name,
                                "product_code": product_code,
                                "product_name": product["name"],
                                "birthday_date": birthday_date.isoformat(),
                                "execute_at": execute_at.isoformat(),
                            },
                        )
                        session.add(order)
                        session.flush()

                        task = task_store.create_task(
                            session,
                            user_id=user_id,
                            task_type=task_store.TASK_BIRTHDAY_ORDER,
                            execute_at=execute_at,
                            payload={
                                "order_id": order.id,
                                "contact_id": contact.id,
                                "product_code": product_code,
                                "amount_minor": product["price_minor"],
                                "from_account_id": account.id,
                            },
                        )
                        order.details = {**order.details, "task_id": task.id}
                        session.flush()

                        payload = order_view(order)
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="order",
                            resource_id=order.id,
                            user_id=user_id,
                            new_value=payload,
                            permission_level=decision.permission_level,
                            confirm_required=decision.confirm_required,
                            mfa_required=decision.mfa_required,
                            reason=f"为 {contact.name} 创建生日订单",
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
                                "contact_id": contact_id,
                                "product_code": product_code,
                                "birthday_date": birthday_date.isoformat(),
                                "from_account_id": from_account_id,
                            }
                        ),
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            security_service.record_failure(
                self.database,
                user_id=user_id,
                tool_name=security_service.TOOL_BIRTHDAY_ORDER,
                action=action,
                resource_type="order",
                new_value={"contact_id": contact_id, "product_code": product_code},
                reason=exc.reason,
                request_id=request_id,
            )
            raise

    def get_order(self, order_id: str, *, user_id: str) -> dict:
        with self.database.sessions() as session:
            order = self._load(session, order_id, user_id)
            if order.order_type != "birthday":
                raise NotFoundError(f"生日订单不存在: {order_id}")
            return order_view(order)

    def cancel_order(self, order_id: str, *, user_id: str) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                order = self._load(session, order_id, user_id)
                if order.order_type != "birthday":
                    raise NotFoundError(f"生日订单不存在: {order_id}")
                if order.status != PENDING:
                    raise BusinessError(f"订单 {order_id} 状态为 {order.status}，不可取消")

                old = order_view(order)
                order.status = CANCELLED
                session.flush()

                task_id = (order.details or {}).get("task_id")
                if task_id:
                    task = session.get(task_store.ScheduledTask, task_id)
                    if task is not None and task.status not in task_store.TERMINAL_STATUSES:
                        task_store.cancel_task(session, task)

                payload = order_view(order)
                audit_service.record(
                    session,
                    action="cancel_birthday_order",
                    resource_type="order",
                    resource_id=order.id,
                    user_id=order.user_id,
                    old_value=old,
                    new_value=payload,
                    reason="用户取消生日订单",
                )
                return payload

    @staticmethod
    def _load(session: Session, order_id: str, user_id: str) -> Order:
        return owned(session, Order, order_id, user_id, label="订单")


# ---------------------------------------------------------------- 任务处理器


def handle_birthday_order(session: Session, task, context) -> dict:
    payload = task.payload or {}
    order = session.get(Order, payload["order_id"])
    if order is None:
        raise NotFoundError(f"订单不存在: {payload['order_id']}")
    if order.status != PENDING:
        raise BusinessError(f"订单 {order.id} 状态为 {order.status}，不可执行")

    if context.birthday_outcome == "fail":
        raise BusinessError("mock 生日订单失败（birthday_outcome=fail）")

    account = session.get(Account, payload["from_account_id"])
    if account is None:
        raise NotFoundError(f"账户不存在: {payload['from_account_id']}")

    details = order.details or {}
    entry = post_entry(
        session,
        account=account,
        direction=DEBIT,
        amount_minor=order.amount_minor,
        transaction_type="birthday",
        category="gift",
        remark=f"生日礼物 {details.get('product_name', payload.get('product_code'))}",
        reference_type="order",
        reference_id=order.id,
        created_at=context.now,
    )
    order.status = COMPLETED
    order.transaction_id = entry.id
    session.flush()

    audit_service.record(
        session,
        action="run_birthday_order",
        resource_type="order",
        resource_id=order.id,
        user_id=order.user_id,
        new_value=order_view(order),
        reason="生日订单到期执行",
    )
    return order_view(order)


def _on_birthday_failure(session: Session, task, reason: str | None) -> None:
    """保存点回滚后补记订单失败状态。"""
    order_id = (task.payload or {}).get("order_id")
    if not order_id:
        return
    order = session.get(Order, order_id)
    if order is None or order.status != PENDING:
        return
    order.status = FAILED
    order.details = {**(order.details or {}), "failure_reason": reason}
    session.flush()


def register() -> None:
    clock_service.register_handler(task_store.TASK_BIRTHDAY_ORDER, handle_birthday_order)
    clock_service.register_failure_hook(task_store.TASK_BIRTHDAY_ORDER, _on_birthday_failure)


register()
