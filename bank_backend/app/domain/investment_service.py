"""理财产品、持仓、收益模拟与赎回。

收益按 `expected_return * days / 365` 计算，days 为持仓天数（默认取模拟时钟的当前时间）。
申购与赎回都在单个事务内完成订单、持仓、资金流水、审计和幂等结果。

理财写的门禁比银行其他域更严：申购／赎回固定需要 `confirm=true` 且验证码等于配置的
`mock_mfa_code`，不按金额分级，也不允许小额免验证。申购还要有本人有效测评且产品风险
不高于测评等级；赎回按产品的 `redeem_rule` 与到期日判断可赎回金额。

赎回语义：amount_minor 是从持仓当前价值中取回的金额。取满即整笔赎回（status=redeemed），
否则为部分赎回（status=partial），本金按 floor(本金 × 赎回额 ÷ 当前价值) 释放，
利息随本金一同入账。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    BankError,
    BusinessError,
    ConfirmationRequiredError,
    NotFoundError,
    ValidationError,
    VerificationFailedError,
)
from app.core.ids import HOLDING, ORDER, new_id
from app.core.timeutil import add_months, iso
from app.domain import audit_service, clock_service, risk, security_service, wealth_risk_service
from app.domain.idempotency_service import find as find_idempotent
from app.domain.idempotency_service import remember, request_fingerprint
from app.domain.money_service import CREDIT, DEBIT, post_entry, validate_amount
from app.domain.ownership import owned
from app.storage.database import Database
from app.storage.models import Account, Holding, Order, Product, User

DAYS_PER_YEAR = 365


def product_view(product: Product) -> dict:
    return {
        "product_id": product.id,
        "name": product.name,
        "product_type": product.product_type,
        "min_amount_minor": product.min_amount_minor,
        "max_amount_minor": product.max_amount_minor,
        "expected_return": float(product.expected_return),
        "risk_level": product.risk_level,
        "duration_months": product.duration_months,
        "redeem_rule": product.redeem_rule,
        "status": product.status,
    }


def holding_view(
    holding: Holding,
    *,
    product_name: str | None = None,
    redeemable_minor: int = 0,
    realized_income_minor: int = 0,
) -> dict:
    return {
        "holding_id": holding.id,
        "user_id": holding.user_id,
        "product_id": holding.product_id,
        "product_name": product_name,
        "source_account_id": holding.source_account_id,
        "principal_minor": holding.principal_minor,
        "current_value_minor": holding.current_value_minor,
        "accrued_interest_minor": holding.current_value_minor - holding.principal_minor,
        "realized_income_minor": realized_income_minor,
        "redeemable_minor": redeemable_minor,
        "expected_return": float(holding.expected_return),
        "status": holding.status,
        "purchased_at": iso(holding.purchased_at),
        "maturity_date": holding.maturity_date.isoformat() if holding.maturity_date else None,
    }


def order_view(order: Order) -> dict:
    return {
        "order_id": order.id,
        "order_type": order.order_type,
        "user_id": order.user_id,
        "product_id": order.product_id,
        "holding_id": order.holding_id,
        "amount_minor": order.amount_minor,
        "status": order.status,
        "transaction_id": order.transaction_id,
        "details": order.details or {},
        "created_at": iso(order.created_at),
    }


def accrued_interest_minor(principal_minor: int, expected_return: Decimal, days: int) -> int:
    """固定年化收益：principal * return * days / 365，四舍五入到分。"""
    if days <= 0 or principal_minor <= 0:
        return 0
    amount = (
        Decimal(principal_minor) * Decimal(expected_return) * Decimal(days) / Decimal(DAYS_PER_YEAR)
    )
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


class InvestmentService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    # ------------------------------------------------------------ 产品

    def list_products(
        self, *, risk_level: str | None = None, product_type: str | None = None
    ) -> list[dict]:
        with self.database.sessions() as session:
            statement = select(Product).order_by(Product.id)
            if risk_level:
                statement = statement.where(Product.risk_level == risk_level)
            if product_type:
                statement = statement.where(Product.product_type == product_type)
            return [product_view(item) for item in session.execute(statement).scalars()]

    def get_product(self, product_id: str) -> dict:
        with self.database.sessions() as session:
            return product_view(self._load_product(session, product_id))

    def recommend(
        self, *, risk_level: str | None = None, amount_minor: int | None = None
    ) -> list[dict]:
        with self.database.sessions() as session:
            statement = select(Product).where(Product.status == "available")
            if risk_level:
                statement = statement.where(Product.risk_level == risk_level)
            if amount_minor is not None:
                statement = statement.where(
                    Product.min_amount_minor <= amount_minor,
                    Product.max_amount_minor >= amount_minor,
                )
            statement = statement.order_by(Product.expected_return.desc(), Product.id)
            return [product_view(item) for item in session.execute(statement).scalars()]

    def compare(self, product_ids: list[str]) -> list[dict]:
        if not product_ids or len(product_ids) < 2:
            raise ValidationError("对比至少需要 2 个产品")
        with self.database.sessions() as session:
            products = {item.id: item for item in session.execute(
                select(Product).where(Product.id.in_(product_ids))
            ).scalars()}
            missing = [item for item in product_ids if item not in products]
            if missing:
                raise NotFoundError(f"产品不存在: {', '.join(missing)}")
            # 保持调用方给定的顺序，便于前端并排展示。
            return [product_view(products[item]) for item in product_ids]

    # ------------------------------------------------------------ 申购

    def purchase(
        self,
        *,
        user_id: str,
        from_account_id: str,
        product_id: str,
        amount_minor: int,
        term_months: int | None = None,
        confirm: bool = False,
        mfa_code: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "purchase_product"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    # 先锁用户行串行化同一用户的理财写，再查幂等记录：
                    # 正常重复在资金校验之前就返回原回执，不因首笔改变余额而失败。
                    self._lock_user(session, user_id)
                    amount_minor = validate_amount(amount_minor)
                    fingerprint = request_fingerprint(
                        {
                            "operation": action,
                            "from_account_id": from_account_id,
                            "product_id": product_id,
                            "amount_minor": amount_minor,
                            "term_months": term_months,
                        }
                    )
                    replayed = self._replay(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        request_hash=fingerprint,
                    )
                    if replayed is not None:
                        return replayed

                    security_service.require_not_locked(
                        session, self.database, user_id, security_service.TOOL_FUND_PURCHASE
                    )

                    product = self._load_product(session, product_id)
                    if product.status != "available":
                        raise BusinessError(f"产品 {product_id} 当前不可申购")
                    if amount_minor < product.min_amount_minor:
                        raise ValidationError(
                            f"低于起购金额 {product.min_amount_minor} 分"
                        )
                    if amount_minor > product.max_amount_minor:
                        raise ValidationError(
                            f"超过单笔上限 {product.max_amount_minor} 分"
                        )
                    # 首版不允许自选缩短期限：缺省取产品定义，显式传值必须一致。
                    if term_months is not None and term_months != product.duration_months:
                        raise ValidationError(
                            f"term_months 必须等于产品期限 {product.duration_months}"
                        )
                    months = product.duration_months

                    account = self._load_account_locked(session, from_account_id)
                    if account is None or account.user_id != user_id:
                        raise NotFoundError(f"账户不存在: {from_account_id}")
                    if account.account_type == "fixed":
                        raise BusinessError(f"定期账户 {from_account_id} 不支持申购扣款")
                    if account.status != "active":
                        raise BusinessError(f"账户 {from_account_id} 状态为 {account.status}")

                    now = clock_service.ClockService(self.database).current_time()
                    assessment = wealth_risk_service.valid_row(session, user_id, now)
                    if assessment is None:
                        raise BusinessError("没有有效的风险测评，请先完成风险测评")
                    self._require_risk_match(product, assessment)

                    decision = self._require_verification(
                        confirm=confirm, mfa_code=mfa_code, action="申购"
                    )

                    purchased_on = now.date()
                    maturity = add_months(purchased_on, months) if months else None

                    def factory() -> dict:
                        entry = post_entry(
                            session,
                            account=account,
                            direction=DEBIT,
                            amount_minor=amount_minor,
                            transaction_type="purchase",
                            category="investment",
                            remark=f"申购 {product.name}",
                            reference_type="order",
                            created_at=now,
                        )
                        holding = Holding(
                            id=new_id(HOLDING),
                            user_id=user_id,
                            product_id=product.id,
                            source_account_id=account.id,
                            principal_minor=amount_minor,
                            current_value_minor=amount_minor,
                            expected_return=product.expected_return,
                            purchased_at=now,
                            maturity_date=maturity,
                            status="holding",
                        )
                        session.add(holding)
                        session.flush()

                        order = Order(
                            id=new_id(ORDER),
                            user_id=user_id,
                            order_type="product_purchase",
                            product_id=product.id,
                            holding_id=holding.id,
                            from_account_id=account.id,
                            amount_minor=amount_minor,
                            status="completed",
                            details={
                                "product_name": product.name,
                                "expected_return": float(product.expected_return),
                                "term_months": months,
                                "redeem_rule": product.redeem_rule,
                                "assessment_id": assessment.id,
                            },
                            transaction_id=entry.id,
                        )
                        session.add(order)
                        session.flush()

                        payload = {
                            "order_id": order.id,
                            "holding_id": holding.id,
                            "transaction_id": entry.id,
                            "product_id": product.id,
                            "amount_minor": amount_minor,
                            "status": order.status,
                            "principal_minor": holding.principal_minor,
                            "maturity_date": maturity.isoformat() if maturity else None,
                            "redeem_rule": product.redeem_rule,
                            "risk_level": product.risk_level,
                            "assessment_id": assessment.id,
                            **decision.as_dict(),
                        }
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="order",
                            resource_id=order.id,
                            user_id=user_id,
                            old_value={"from_balance_minor": account.balance_minor + amount_minor},
                            new_value=payload,
                            permission_level=decision.permission_level,
                            confirm_required=decision.confirm_required,
                            mfa_required=decision.mfa_required,
                            reason=f"申购 {product.name}",
                            request_id=request_id,
                        )
                        return payload

                    result = factory()
                    self._remember(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        result=result,
                        request_hash=fingerprint,
                    )
                    return {**result, "replayed": False}
        except BankError as exc:
            self._audit_failure(
                action=action,
                resource_type="order",
                user_id=user_id,
                reason=exc.reason,
                new_value={"product_id": product_id, "amount_minor": amount_minor},
                request_id=request_id,
                tool_name=security_service.TOOL_FUND_PURCHASE,
                security_relevant=self._security_relevant(exc),
            )
            raise

    # ------------------------------------------------------------ 持仓

    def list_holdings(self, *, user_id: str, status: str | None = None) -> list[dict]:
        with self.database.sessions() as session:
            from app.domain.account_service import require_user

            require_user(session, user_id)
            statement = (
                select(Holding).where(Holding.user_id == user_id).order_by(Holding.purchased_at)
            )
            if status:
                statement = statement.where(Holding.status == status)
            now = clock_service.ClockService(self.database).current_time()
            return [
                self._holding_view(session, item, now)
                for item in session.execute(statement).scalars()
            ]

    def get_holding(self, holding_id: str, *, user_id: str) -> dict:
        """本人持仓详情；不属于本人时按“不存在”拒绝，不泄露他人持仓。"""

        with self.database.sessions() as session:
            from app.domain.account_service import require_user

            require_user(session, user_id)
            # 归属由 _load_holding 判定：不属于本人一律「不存在」，不泄露他人持仓。
            holding = self._load_holding(session, holding_id, user_id)
            now = clock_service.ClockService(self.database).current_time()
            return self._holding_view(session, holding, now)

    def simulate_interest(
        self, holding_id: str, *, user_id: str, as_of: date | None = None
    ) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                holding = self._load_holding(session, holding_id, user_id)
                if holding.status == "redeemed":
                    raise BusinessError(f"持仓 {holding_id} 已赎回，无法模拟收益")

                now = clock_service.ClockService(self.database).current_time()
                target = as_of or now.date()
                if isinstance(target, datetime):
                    target = target.date()
                days = (target - holding.purchased_at.date()).days
                accrued = accrued_interest_minor(
                    holding.principal_minor, holding.expected_return, days
                )
                old_value = holding.current_value_minor
                holding.current_value_minor = holding.principal_minor + accrued
                session.flush()

                payload = {
                    **self._holding_view(session, holding, now),
                    "days": days,
                    "as_of": target.isoformat(),
                }
                audit_service.record(
                    session,
                    action="simulate_interest",
                    resource_type="holding",
                    resource_id=holding.id,
                    user_id=holding.user_id,
                    old_value={"current_value_minor": old_value},
                    new_value=payload,
                    reason=f"按 {days} 天模拟收益",
                )
                return payload

    # ------------------------------------------------------------ 赎回

    def redeem(
        self,
        *,
        user_id: str,
        holding_id: str,
        amount_minor: int,
        to_account_id: str | None = None,
        confirm: bool = False,
        mfa_code: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "redeem_product"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    self._lock_user(session, user_id)
                    amount_minor = validate_amount(amount_minor)
                    fingerprint = request_fingerprint(
                        {
                            "operation": action,
                            "holding_id": holding_id,
                            "amount_minor": amount_minor,
                            "to_account_id": to_account_id,
                        }
                    )
                    replayed = self._replay(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        request_hash=fingerprint,
                    )
                    if replayed is not None:
                        return replayed

                    security_service.require_not_locked(
                        session, self.database, user_id, security_service.TOOL_FUND_REDEEM
                    )

                    holding = self._load_holding_locked(session, holding_id)
                    if holding is None or holding.user_id != user_id:
                        raise NotFoundError(f"持仓不存在: {holding_id}")
                    if holding.status == "redeemed":
                        raise BusinessError(f"持仓 {holding_id} 已全部赎回")

                    now = clock_service.ClockService(self.database).current_time()
                    product = session.get(Product, holding.product_id)
                    if product is not None and product.redeem_rule == "maturity_only":
                        matured = (
                            holding.maturity_date is not None
                            and now.date() >= holding.maturity_date
                        )
                        if not matured:
                            maturity = (
                                holding.maturity_date.isoformat()
                                if holding.maturity_date
                                else "未设置"
                            )
                            raise BusinessError(
                                f"持仓 {holding_id} 为到期赎回产品，"
                                f"到期日 {maturity} 之前不可赎回"
                            )
                    redeemable = self._redeemable_minor(holding, product, now.date())
                    if amount_minor > redeemable:
                        raise BusinessError(
                            f"赎回金额 {amount_minor} 超过可赎回金额 {redeemable}"
                        )

                    target_id = to_account_id or holding.source_account_id
                    account = self._load_account_locked(session, target_id)
                    if account is None or account.user_id != user_id:
                        raise NotFoundError(f"账户不存在: {target_id}")
                    if account.status != "active":
                        raise BusinessError(f"账户 {target_id} 状态为 {account.status}")

                    decision = self._require_verification(
                        confirm=confirm, mfa_code=mfa_code, action="赎回"
                    )

                    # 收益由 simulate_interest 显式结算并持久化到 current_value_minor。
                    # 赎回只按持仓当前价值支付，不在这里重算——否则会把模拟出来的收益抹掉。
                    current_value = holding.current_value_minor

                    def factory() -> dict:
                        full = amount_minor == current_value
                        if full:
                            principal_redeemed = holding.principal_minor
                            holding.principal_minor = 0
                            holding.current_value_minor = 0
                            holding.status = "redeemed"
                        else:
                            # 部分赎回按整数 floor(P×R/V) 释放本金，不用浮点推导资金。
                            principal_redeemed = (
                                holding.principal_minor * amount_minor // current_value
                            )
                            holding.principal_minor -= principal_redeemed
                            holding.current_value_minor -= amount_minor
                            holding.status = "partial"
                        session.flush()

                        entry = post_entry(
                            session,
                            account=account,
                            direction=CREDIT,
                            amount_minor=amount_minor,
                            transaction_type="redeem",
                            category="investment",
                            remark=f"赎回持仓 {holding.id}",
                            reference_type="order",
                            created_at=now,
                        )
                        order = Order(
                            id=new_id(ORDER),
                            user_id=user_id,
                            order_type="product_redeem",
                            product_id=holding.product_id,
                            holding_id=holding.id,
                            to_account_id=account.id,
                            amount_minor=amount_minor,
                            status="completed",
                            details={
                                "full_redemption": full,
                                "principal_redeemed_minor": principal_redeemed,
                                "interest_redeemed_minor": amount_minor - principal_redeemed,
                                "redeem_rule": product.redeem_rule if product else None,
                            },
                            transaction_id=entry.id,
                        )
                        session.add(order)
                        session.flush()

                        payload = {
                            "order_id": order.id,
                            "holding_id": holding.id,
                            "transaction_id": entry.id,
                            "amount_minor": amount_minor,
                            "principal_redeemed_minor": principal_redeemed,
                            "interest_minor": amount_minor - principal_redeemed,
                            "holding_status": holding.status,
                            "remaining_principal_minor": holding.principal_minor,
                            "remaining_value_minor": holding.current_value_minor,
                            "status": order.status,
                            **decision.as_dict(),
                        }
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="order",
                            resource_id=order.id,
                            user_id=user_id,
                            old_value={"holding_value_minor": current_value},
                            new_value=payload,
                            permission_level=decision.permission_level,
                            confirm_required=decision.confirm_required,
                            mfa_required=decision.mfa_required,
                            reason=f"赎回持仓 {holding.id}",
                            request_id=request_id,
                        )
                        return payload

                    result = factory()
                    self._remember(
                        session,
                        user_id=user_id,
                        operation_type=action,
                        idempotent_key=idempotent_key,
                        result=result,
                        request_hash=fingerprint,
                    )
                    return {**result, "replayed": False}
        except BankError as exc:
            self._audit_failure(
                action=action,
                resource_type="order",
                user_id=user_id,
                reason=exc.reason,
                new_value={"holding_id": holding_id, "amount_minor": amount_minor},
                request_id=request_id,
                tool_name=security_service.TOOL_FUND_REDEEM,
                security_relevant=self._security_relevant(exc),
            )
            raise

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _lock_user(session: Session, user_id: str) -> None:
        """锁当前用户行，串行化同一用户的理财写入口。"""

        session.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _replay(
        session: Session,
        *,
        user_id: str,
        operation_type: str,
        idempotent_key: str | None,
        request_hash: str,
    ) -> dict | None:
        """同键同参数返回原回执；同键不同参数拒绝。

        只在理财写入口比较指纹，不改变其他域现有幂等行为。
        """

        if not idempotent_key:
            return None
        existing = find_idempotent(
            session,
            user_id=user_id,
            operation_type=operation_type,
            idempotent_key=idempotent_key,
        )
        if existing is None:
            return None
        if existing.request_hash and existing.request_hash != request_hash:
            raise BusinessError("同一幂等键已用于不同的业务参数，请更换幂等键")
        return {**dict(existing.result), "replayed": True}

    @staticmethod
    def _remember(
        session: Session,
        *,
        user_id: str,
        operation_type: str,
        idempotent_key: str | None,
        result: dict,
        request_hash: str,
    ) -> None:
        """幂等回执与资金变更在同一外层事务提交。"""

        if not idempotent_key:
            return
        remember(
            session,
            user_id=user_id,
            operation_type=operation_type,
            idempotent_key=idempotent_key,
            result=result,
            request_hash=request_hash,
        )

    def _require_verification(
        self, *, confirm: bool, mfa_code: str | None, action: str
    ) -> risk.Decision:
        """理财写固定需要确认与等于配置演示码的验证码，不按金额分级。"""

        if not confirm:
            raise ConfirmationRequiredError(f"{action} 需要 confirm=true 才会执行")
        if not mfa_code or mfa_code != self.settings.mock_mfa_code:
            raise VerificationFailedError(f"{action}的模拟验证码不正确")
        return risk.Decision(risk.RED, confirm_required=True, mfa_required=True)

    @staticmethod
    def _require_risk_match(product: Product, assessment) -> None:
        product_level = wealth_risk_service.RISK_ORDER.get(product.risk_level)
        profile_level = wealth_risk_service.RISK_ORDER.get(assessment.risk_level)
        if product_level is None:
            raise BusinessError(f"产品 {product.id} 的风险等级无法识别")
        if profile_level is None or product_level > profile_level:
            raise BusinessError(
                f"产品风险等级 {product.risk_level} 高于本人测评等级 {assessment.risk_level}"
            )

    @staticmethod
    def _security_relevant(exc: BankError) -> bool:
        """待确认是正常流程不计数；验证码明确错误要计入失败计数。"""

        if isinstance(exc, VerificationFailedError):
            return True
        return not isinstance(exc, ConfirmationRequiredError)

    @staticmethod
    def _redeemable_minor(holding: Holding, product: Product | None, today: date) -> int:
        """可赎回金额：灵活产品取当前价值；到期产品到期后取当前价值，未到期为 0。"""

        if holding.status == "redeemed" or holding.current_value_minor <= 0:
            return 0
        if product is not None and product.redeem_rule == "maturity_only":
            if holding.maturity_date is None or today < holding.maturity_date:
                return 0
        return holding.current_value_minor

    @staticmethod
    def _realized_income_minor(session: Session, holding_id: str, user_id: str) -> int:
        """已实现收益：汇总本人该持仓已完成赎回订单里的 interest_redeemed_minor。"""

        details_rows = session.execute(
            select(Order.details).where(
                Order.user_id == user_id,
                Order.holding_id == holding_id,
                Order.order_type == "product_redeem",
                Order.status == "completed",
            )
        ).scalars()
        total = 0
        for details in details_rows:
            value = (details or {}).get("interest_redeemed_minor")
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total

    def _holding_view(self, session: Session, holding: Holding, now: datetime) -> dict:
        product = session.get(Product, holding.product_id)
        return holding_view(
            holding,
            product_name=product.name if product is not None else None,
            redeemable_minor=self._redeemable_minor(holding, product, now.date()),
            realized_income_minor=self._realized_income_minor(
                session, holding.id, holding.user_id
            ),
        )

    @staticmethod
    def _load_product(session: Session, product_id: str) -> Product:
        product = session.get(Product, product_id)
        if product is None:
            raise NotFoundError(f"产品不存在: {product_id}")
        return product

    @staticmethod
    def _load_holding(session: Session, holding_id: str, user_id: str) -> Holding:
        return owned(session, Holding, holding_id, user_id, label="持仓")

    @staticmethod
    def _load_account_locked(session: Session, account_id: str) -> Account | None:
        """锁定账户行后再复检归属、状态与余额，避免校验与写入之间被其他事务改写。

        只加在理财写入口，转账等其他域保持原有实现。
        """

        return session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _load_holding_locked(session: Session, holding_id: str) -> Holding | None:
        """锁定持仓行后再复检归属、状态与可赎回额。"""

        return session.execute(
            select(Holding).where(Holding.id == holding_id).with_for_update()
        ).scalar_one_or_none()

    def _audit_failure(
        self,
        *,
        action: str,
        resource_type: str,
        user_id: str,
        reason: str | None,
        new_value: dict,
        request_id: str | None,
        tool_name: str,
        security_relevant: bool = True,
    ) -> None:
        """资金已回滚，但失败审计与前缀失败计数必须独立落库保留。"""
        security_service.record_failure(
            self.database,
            user_id=user_id,
            tool_name=tool_name,
            action=action,
            resource_type=resource_type,
            new_value=new_value,
            reason=reason,
            request_id=request_id,
            security_relevant=security_relevant,
        )
