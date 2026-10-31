"""卡片申请、限额、商户黑名单与状态流转。

状态机：active ⇄ frozen → blocked。blocked 是终态，不可解冻或再次阻断。
卡号只保存脱敏值，绝不写入完整卡号。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import BusinessError, NotFoundError, ValidationError
from app.core.ids import CARD, ORDER, new_id
from app.domain import audit_service
from app.domain.ownership import owned
from app.storage.database import Database
from app.storage.models import Account, Card, Order

ACTIVE = "active"
FROZEN = "frozen"
BLOCKED = "blocked"

CARD_TYPES = ("debit", "credit", "virtual")

DEFAULT_DAILY_LIMIT_MINOR = 500000
DEFAULT_MONTHLY_LIMIT_MINOR = 5000000
MAX_LIMIT_MINOR = 100000000


def card_view(card: Card) -> dict:
    return {
        "card_id": card.id,
        "user_id": card.user_id,
        "account_id": card.account_id,
        "card_type": card.card_type,
        "card_number": card.card_number,
        "status": card.status,
        "freeze_reason": card.freeze_reason,
        "daily_limit_minor": card.daily_limit_minor,
        "monthly_limit_minor": card.monthly_limit_minor,
        "merchant_blacklist": list(card.merchant_blacklist or []),
        "expiry_date": card.expiry_date.isoformat() if card.expiry_date else None,
        "cvv_masked": card.cvv_masked,
    }


class CardService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    def list_cards(self, user_id: str) -> list[dict]:
        with self.database.sessions() as session:
            from app.domain.account_service import require_user

            require_user(session, user_id)
            cards = session.execute(
                select(Card).where(Card.user_id == user_id).order_by(Card.id)
            ).scalars()
            return [card_view(card) for card in cards]

    def get_card(self, card_id: str, user_id: str) -> dict:
        with self.database.sessions() as session:
            return card_view(self._load(session, card_id, user_id))

    def apply(
        self,
        *,
        user_id: str,
        card_type: str,
        account_id: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        raise ValidationError("直接申请入口已停用，请准备申请并通过独立密码表单确认")

    def update_limit(
        self,
        card_id: str,
        *,
        user_id: str,
        daily_limit_minor: int,
        monthly_limit_minor: int,
    ) -> dict:
        self._validate_limits(daily_limit_minor, monthly_limit_minor)
        return self._mutate(
            card_id,
            user_id=user_id,
            action="update_card_limit",
            reason="修改卡片限额",
            allowed=(ACTIVE, FROZEN),
            apply=lambda card: (
                setattr(card, "daily_limit_minor", daily_limit_minor),
                setattr(card, "monthly_limit_minor", monthly_limit_minor),
            ),
        )

    def set_restrictions(
        self,
        card_id: str,
        *,
        user_id: str,
        daily_limit_minor: int | None = None,
        monthly_limit_minor: int | None = None,
        merchant_blacklist: list[str] | None = None,
    ) -> dict:
        def apply(card: Card) -> None:
            self._validate_limits(
                card.daily_limit_minor if daily_limit_minor is None else daily_limit_minor,
                card.monthly_limit_minor if monthly_limit_minor is None else monthly_limit_minor,
            )
            if daily_limit_minor is not None:
                card.daily_limit_minor = daily_limit_minor
            if monthly_limit_minor is not None:
                card.monthly_limit_minor = monthly_limit_minor
            if merchant_blacklist is not None:
                cleaned = [item.strip() for item in merchant_blacklist if item and item.strip()]
                card.merchant_blacklist = sorted(set(cleaned))

        return self._mutate(
            card_id,
            user_id=user_id,
            action="set_card_restrictions",
            reason="设置卡片限额与商户黑名单",
            allowed=(ACTIVE, FROZEN),
            apply=apply,
        )

    def get_restrictions(self, card_id: str, user_id: str) -> dict:
        with self.database.sessions() as session:
            card = self._load(session, card_id, user_id)
            return {
                "card_id": card.id,
                "status": card.status,
                "daily_limit_minor": card.daily_limit_minor,
                "monthly_limit_minor": card.monthly_limit_minor,
                "merchant_blacklist": list(card.merchant_blacklist or []),
            }

    def freeze(self, card_id: str, *, user_id: str) -> dict:
        return self._mutate(
            card_id,
            user_id=user_id,
            action="freeze_card",
            reason="挂失或冻结卡片",
            allowed=(ACTIVE,),
            apply=lambda card: (
                setattr(card, "status", FROZEN),
                setattr(card, "freeze_reason", "manual"),
            ),
        )

    def unfreeze(self, card_id: str, *, user_id: str) -> dict:
        return self._mutate(
            card_id,
            user_id=user_id,
            action="unfreeze_card",
            reason="解挂或解冻卡片",
            allowed=(FROZEN,),
            apply=lambda card: (
                setattr(card, "status", ACTIVE),
                setattr(card, "freeze_reason", None),
            ),
        )

    def block(self, card_id: str, *, user_id: str) -> dict:
        return self._mutate(
            card_id,
            user_id=user_id,
            action="block_card",
            reason="永久阻断卡片",
            allowed=(ACTIVE, FROZEN),
            apply=lambda card: setattr(card, "status", BLOCKED),
        )

    # ------------------------------------------------------------ 内部

    def _mutate(
        self, card_id: str, *, user_id: str, action: str, reason: str, allowed, apply
    ) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                card = self._load(session, card_id, user_id)
                if action == "unfreeze_card" and card.freeze_reason == "lost":
                    raise BusinessError("挂失卡片需要验证后解挂")
                if card.status not in allowed:
                    raise BusinessError(f"卡片 {card_id} 状态为 {card.status}，不允许该操作")
                old = card_view(card)
                apply(card)
                session.flush()
                payload = card_view(card)
                audit_service.record(
                    session,
                    action=action,
                    resource_type="card",
                    resource_id=card.id,
                    user_id=card.user_id,
                    old_value=old,
                    new_value=payload,
                    reason=reason,
                )
                return payload

    @staticmethod
    def _validate_limits(daily: int, monthly: int) -> None:
        for name, value in (("daily_limit_minor", daily), ("monthly_limit_minor", monthly)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{name} 必须是非负整数分")
            if value > MAX_LIMIT_MINOR:
                raise ValidationError(f"{name} 不能超过 {MAX_LIMIT_MINOR}")
        if monthly and daily > monthly:
            raise ValidationError("daily_limit_minor 不能大于 monthly_limit_minor")

    @staticmethod
    def _load(session: Session, card_id: str, user_id: str) -> Card:
        return owned(session, Card, card_id, user_id, label="卡片")

    @staticmethod
    def _resolve_account(session: Session, user_id: str, account_id: str | None) -> Account:
        if account_id:
            account = session.get(Account, account_id)
            if account is None:
                raise NotFoundError(f"账户不存在: {account_id}")
            if account.user_id != user_id:
                raise NotFoundError("账户不存在")
            if account.status != ACTIVE or account.account_type != "current":
                raise BusinessError("申请卡片需要正常的本人活期账户")
            return account
        account = session.execute(
            select(Account)
            .where(
                Account.user_id == user_id,
                Account.account_type == "current",
                Account.status == ACTIVE,
            )
            .order_by(Account.id)
            .limit(1)
        ).scalar_one_or_none()
        if account is None:
            raise NotFoundError(f"用户 {user_id} 没有可用的活期账户")
        return account


def create_card(
    session: Session, user_id: str, account_id: str, card_type: str, *, password_hash: str
) -> dict:
    """Create the card and application order in the caller's transaction."""
    if not password_hash:
        raise ValidationError("申请银行卡必须设置交易密码")
    card = Card(
        id=new_id(CARD),
        user_id=user_id,
        account_id=account_id,
        transaction_password_hash=password_hash,
        card_number=_masked_number(card_type),
        card_type=card_type,
        expiry_date=_expiry_date(),
        cvv_masked="***",
        daily_limit_minor=DEFAULT_DAILY_LIMIT_MINOR,
        monthly_limit_minor=DEFAULT_MONTHLY_LIMIT_MINOR,
        status=ACTIVE,
        merchant_blacklist=[],
    )
    session.add(card)
    session.flush()
    order = Order(
        id=new_id(ORDER),
        user_id=user_id,
        order_type="card_application",
        from_account_id=account_id,
        amount_minor=0,
        status="completed",
        details={"card_id": card.id, "card_type": card_type},
    )
    session.add(order)
    session.flush()
    return {**card_view(card), "order_id": order.id}


def _masked_number(card_type: str) -> str:
    suffix = uuid.uuid4().int % 10000
    return f"6222 **** **** {suffix:04d}"


def _expiry_date() -> date:
    return date(2030, 12, 31)
