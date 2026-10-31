"""理财订单核实：本人订单详情与按幂等键查询原回执。

只读，不写任何业务数据。Agent 用它核对“刚才那笔到底成交没有”，因此这里返回的
是数据库里的真实订单、资金流水和幂等回执，而不是聊天历史或内存字典。

归属规则：只返回本人（user_id）的 `product_purchase`／`product_redeem` 订单；
不存在、不属于本人或不属于理财订单类型时统一按“资源不存在”拒绝，避免用猜 ID
的方式绕过列表接口读别人的订单。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.timeutil import iso
from app.domain.account_service import require_user
from app.domain.idempotency_service import find as find_idempotent
from app.storage.database import Database
from app.storage.models import Order, Product

WEALTH_ORDER_TYPES = ("product_purchase", "product_redeem")
# 只允许按理财操作的幂等记录查询，不借这个接口读其他域的幂等结果。
WEALTH_OPERATION_TYPES = ("purchase_product", "redeem_product")


def wealth_order_view(session: Session, order: Order) -> dict:
    """理财订单到 API schema 的映射；订单号与流水号严格分开。"""

    product = session.get(Product, order.product_id) if order.product_id else None
    return {
        "order_id": order.id,
        "order_type": order.order_type,
        "user_id": order.user_id,
        "product_id": order.product_id,
        "product_name": product.name if product is not None else None,
        "holding_id": order.holding_id,
        "from_account_id": order.from_account_id,
        "to_account_id": order.to_account_id,
        "amount_minor": order.amount_minor,
        "status": order.status,
        "transaction_id": order.transaction_id,
        "details": dict(order.details or {}),
        "created_at": iso(order.created_at),
        "updated_at": iso(order.updated_at),
    }


class WealthOrderService:
    def __init__(self, database: Database):
        self.database = database

    def get_order(self, *, user_id: str, order_id: str) -> dict:
        with self.database.sessions() as session:
            require_user(session, user_id)
            return wealth_order_view(session, self._load_wealth_order(session, user_id, order_id))

    def find_by_idempotency_key(
        self, *, user_id: str, operation_type: str, idempotent_key: str
    ) -> dict:
        """按 (user_id, operation_type, idempotent_key) 查原回执并核实关联订单。"""

        if operation_type not in WEALTH_OPERATION_TYPES:
            raise ValidationError(
                "operation_type 只能是 " + " 或 ".join(WEALTH_OPERATION_TYPES)
            )
        if not idempotent_key:
            raise ValidationError("idempotent_key 不能为空")

        with self.database.sessions() as session:
            require_user(session, user_id)
            record = find_idempotent(
                session,
                user_id=user_id,
                operation_type=operation_type,
                idempotent_key=idempotent_key,
            )
            if record is None:
                # 查不到不等于没提交：调用方应按 unknown 处理，这里不编造订单。
                return {
                    "found": False,
                    "user_id": user_id,
                    "operation_type": operation_type,
                    "idempotent_key": idempotent_key,
                    "result": None,
                    "order": None,
                    "created_at": None,
                }

            result = dict(record.result or {})
            order_id = result.get("order_id")
            order = None
            if isinstance(order_id, str) and order_id:
                order = self._load_wealth_order(session, user_id, order_id, optional=True)
            return {
                "found": True,
                "user_id": user_id,
                "operation_type": operation_type,
                "idempotent_key": idempotent_key,
                "result": result,
                "order": wealth_order_view(session, order) if order is not None else None,
                "created_at": iso(record.created_at),
            }

    @staticmethod
    def _load_wealth_order(
        session: Session, user_id: str, order_id: str, *, optional: bool = False
    ) -> Order | None:
        order = session.get(Order, order_id)
        if (
            order is None
            or order.user_id != user_id
            or order.order_type not in WEALTH_ORDER_TYPES
        ):
            if optional:
                return None
            raise NotFoundError(f"理财订单不存在: {order_id}")
        return order
