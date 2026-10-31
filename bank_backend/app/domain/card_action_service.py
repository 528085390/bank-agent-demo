"""Card intents: prepare, explicitly authorize, and atomically execute once.

The model-facing prepare/execute endpoints cannot confirm or verify an intent. Only
the separate user interaction endpoint accepts the demo verification code.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.errors import (
    BankError,
    BusinessError,
    ConfirmationRequiredError,
    NotFoundError,
    ValidationError,
)
from app.domain import audit_service
from app.domain.account_service import require_user
from app.domain.card_password import hash_password, valid_password, verify_password
from app.domain.card_service import ACTIVE, CARD_TYPES, FROZEN, CardService, card_view, create_card
from app.domain.idempotency_service import request_fingerprint
from app.storage.models import Card, CardAction

OPERATIONS = (
    "apply",
    "update_limit",
    "update_restrictions",
    "freeze",
    "unfreeze",
    "report_loss",
    "release_loss",
    "change_password",
)
LABELS = {
    "apply": "申请银行卡",
    "update_limit": "调整交易限额",
    "update_restrictions": "修改商户黑名单",
    "freeze": "冻结银行卡",
    "unfreeze": "解冻银行卡",
    "report_loss": "挂失银行卡",
    "release_loss": "解除挂失",
    "change_password": "修改银行卡交易密码",
}


def utcnow():
    # Authorization expiry uses real time; moving the demonstration clock cannot extend it.
    return datetime.now(timezone.utc)


def action_view(action: CardAction) -> dict:
    return {
        "action_id": action.id,
        "operation": action.operation,
        "label": LABELS[action.operation],
        "status": action.status,
        "permission_level": action.permission_level,
        "expires_at": action.expires_at.isoformat(),
        "before": action.before,
        "parameters": action.parameters,
        "result": action.result,
        "verification_failures": action.verification_failures,
    }


class CardActionService:
    def __init__(self, database):
        self.database = database

    def prepare(self, user_id: str, operation: str, parameters: dict, key: str) -> dict:
        if not key or len(key) > 128:
            raise ValidationError("需要有效的幂等键")
        if operation not in OPERATIONS:
            raise ValidationError("不支持的卡片操作")
        if operation == "apply" and set(parameters) - {"card_type", "account_id"}:
            raise ValidationError("申请参数只能包含卡类型和账户，密码必须通过独立接口提交")
        fingerprint = request_fingerprint({"operation": operation, "parameters": parameters})
        with self.database.sessions() as session, session.begin():
            require_user(session, user_id)
            # Serialize identical intents before looking up/creating their unique record.
            lock = int.from_bytes(
                hashlib.sha256(f"{user_id}:{key}".encode()).digest()[:8], "big", signed=True
            )
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            existing = session.execute(
                select(CardAction).where(
                    CardAction.user_id == user_id, CardAction.idempotency_key == key
                )
            ).scalar_one_or_none()
            if existing:
                if existing.request_hash != fingerprint:
                    raise ValidationError("幂等键不能用于不同操作")
                return action_view(existing)
            before, normalized, level = self._normalize(session, user_id, operation, parameters)
            action = CardAction(
                id=f"CA-{uuid.uuid4().hex}",
                user_id=user_id,
                operation=operation,
                parameters=normalized,
                before=before,
                permission_level=level,
                status="pending",
                verification_failures=0,
                idempotency_key=key,
                request_hash=fingerprint,
                expires_at=utcnow() + timedelta(minutes=10),
                expected_password_version=(
                    session.get(Card, normalized["card_id"]).password_version
                    if operation == "change_password"
                    else None
                ),
            )
            session.add(action)
            session.flush()
            self._audit(session, action, "prepared")
            return action_view(action)

    def authorize(
        self,
        user_id: str,
        action_id: str,
        decision: str,
        code: str | None = None,
        *,
        password_credentials: dict | None = None,
    ) -> dict:
        with self._action_transaction(user_id, action_id, "authorization_failed") as session:
            action = self._load(session, user_id, action_id)
            password_action = action.operation in ("apply", "change_password")
            if password_credentials is not None and not password_action:
                raise ValidationError("当前操作不接受交易密码")
            if decision not in ("confirm", "cancel"):
                raise ValidationError("请选择确认或取消")
            if decision == "cancel":
                if action.status == "completed":
                    raise BusinessError("已执行的操作不能撤销")
                if action.status == "cancelled":
                    return action_view(action)
                action.status = "cancelled"
                action.pending_password_hash = None
                self._audit(session, action, "cancelled")
                return action_view(action)
            if action.status == "authorized" and password_action:
                if action.expires_at <= utcnow():
                    raise BusinessError("授权已过期，请重新办理")
                if action.operation == "change_password":
                    return self._authorize_password(session, action, code, password_credentials)
            if action.status == "completed" or (
                action.status == "authorized" and not password_action
            ):
                return action_view(action)
            if action.status != "authorized":
                self._require_pending(action)
            if action.operation == "change_password":
                return self._authorize_password(session, action, code, password_credentials)
            if action.operation == "apply":
                if not password_credentials or set(password_credentials) != {
                    "new_password",
                    "confirm_password",
                }:
                    raise ValidationError("请通过独立表单设置并确认交易密码")
                new = password_credentials["new_password"]
                if not isinstance(new, str) or not valid_password(new):
                    raise ValidationError("交易密码必须为六位 ASCII 数字")
                if new != password_credentials["confirm_password"]:
                    raise ValidationError("两次交易密码不一致")
                if action.pending_password_hash and not verify_password(
                    new, action.pending_password_hash
                ):
                    raise ValidationError("已授权的申请不能更换交易密码")
            if action.permission_level == "red" or (
                action.operation == "apply" and code is not None
            ):
                if not code:
                    # 没带码不是一次失败尝试：它连验证都没发生。
                    raise ConfirmationRequiredError("这笔操作需要验证码")
                if not hmac.compare_digest(code.encode(), get_settings().mock_mfa_code.encode()):
                    action.verification_failures += 1
                    if action.verification_failures >= 3:
                        action.status = "cancelled"
                    if action.operation == "apply":
                        action.pending_password_hash = None
                        if action.status != "cancelled":
                            action.status = "pending"
                    self._audit(session, action, "verification_failed", failed=True)
                    # Return a classified state so the failed-attempt counter commits.
                    return {**action_view(action), "verification_passed": False}
            if action.operation == "apply" and not action.pending_password_hash:
                action.pending_password_hash = hash_password(new)
            action.status = "authorized"
            self._audit(session, action, "authorized")
            return {**action_view(action), "verification_passed": True}

    def execute(self, user_id: str, action_id: str) -> dict:
        with self._action_transaction(user_id, action_id, "execution_failed") as session:
            action = self._load(session, user_id, action_id)
            if action.status == "completed":
                return {
                    **action.result,
                    "action_id": action.id,
                    "operation": action.operation,
                    "replayed": True,
                }
            if action.status != "authorized":
                raise ConfirmationRequiredError("操作尚未获得用户授权")
            if action.expires_at <= utcnow():
                raise BusinessError("授权已过期，请重新办理")
            params = action.parameters
            if action.operation == "apply":
                if not action.pending_password_hash:
                    raise ConfirmationRequiredError("申请尚未设置交易密码，请重新确认")
                account = CardService._resolve_account(session, user_id, params["account_id"])
                result = create_card(
                    session,
                    user_id,
                    account.id,
                    params["card_type"],
                    password_hash=action.pending_password_hash,
                )
                action.pending_password_hash = None
            else:
                card = session.execute(
                    select(Card)
                    .where(Card.id == params["card_id"], Card.user_id == user_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if card is None:
                    raise NotFoundError("卡片不存在")
                if card_view(card) != action.before:
                    raise BusinessError("卡片已变化，请重新确认")
                if action.operation == "change_password":
                    if card.password_locked_until and card.password_locked_until > utcnow():
                        raise BusinessError("交易密码验证暂时锁定")
                    if (
                        card.password_version != action.expected_password_version
                        or not action.pending_password_hash
                    ):
                        raise BusinessError("交易密码已变化，请重新办理")
                    card.transaction_password_hash = action.pending_password_hash
                    card.password_version += 1
                    card.password_failures = card.password_code_failures = 0
                    card.password_locked_until = None
                    action.pending_password_hash = None
                elif action.operation == "update_limit":
                    card.daily_limit_minor = params["daily_limit_minor"]
                    card.monthly_limit_minor = params["monthly_limit_minor"]
                elif action.operation == "update_restrictions":
                    card.merchant_blacklist = params["merchant_blacklist"]
                elif action.operation in ("freeze", "report_loss"):
                    card.status = FROZEN
                    card.freeze_reason = "lost" if action.operation == "report_loss" else "manual"
                else:
                    card.status = ACTIVE
                    card.freeze_reason = None
                session.flush()
                result = card_view(card)
            action.status = "completed"
            action.result = result
            self._audit(session, action, "executed", result=result)
            return {
                **result,
                "action_id": action.id,
                "operation": action.operation,
                "replayed": False,
            }

    def _authorize_password(self, session, action, code, credentials):
        card = session.execute(
            select(Card)
            .where(Card.id == action.parameters["card_id"], Card.user_id == action.user_id)
            .with_for_update()
        ).scalar_one()
        if card.password_version != action.expected_password_version:
            raise BusinessError("交易密码已变化，请重新办理")
        if card.password_locked_until and card.password_locked_until > utcnow():
            return {
                **action_view(action),
                "verification_passed": False,
                "credential_error": "locked",
                "remaining_attempts": 0,
            }
        if card.password_locked_until:
            card.password_locked_until = None
            card.password_failures = card.password_code_failures = 0
        if not credentials or set(credentials) != {
            "old_password",
            "new_password",
            "confirm_password",
        }:
            raise ValidationError("请通过独立密码表单提交完整信息")
        old, new, repeated = (
            credentials[name] for name in ("old_password", "new_password", "confirm_password")
        )
        if not all(
            isinstance(value, str) and valid_password(value) for value in (old, new, repeated)
        ):
            raise ValidationError("交易密码必须为六位数字")
        if new != repeated:
            raise ValidationError("两次新密码不一致")
        if old == new:
            raise ValidationError("新交易密码应与旧密码不同")
        if not code:
            raise ConfirmationRequiredError("这笔操作需要验证码")
        if not verify_password(old, card.transaction_password_hash):
            card.password_failures += 1
            error, count = "old_password", card.password_failures
        elif not hmac.compare_digest(code.encode(), get_settings().mock_mfa_code.encode()):
            card.password_code_failures += 1
            action.verification_failures += 1
            error, count = "verification_code", card.password_code_failures
        else:
            card.password_failures = card.password_code_failures = 0
            if action.pending_password_hash and not verify_password(
                new, action.pending_password_hash
            ):
                raise ValidationError("已授权的操作不能更换新交易密码")
            if not action.pending_password_hash:
                action.pending_password_hash = hash_password(new)
            action.status = "authorized"
            self._audit(session, action, "authorized")
            return {**action_view(action), "verification_passed": True}
        # A failed retry revokes any previously staged authorization for this action.
        action.status = "pending"
        action.pending_password_hash = None
        if count >= 3:
            card.password_locked_until = utcnow() + timedelta(minutes=15)
            action.status = "cancelled"
            action.pending_password_hash = None
        self._audit(session, action, "verification_failed", failed=True)
        return {
            **action_view(action),
            "verification_passed": False,
            "credential_error": error,
            "remaining_attempts": max(0, 3 - count),
        }

    @contextmanager
    def _action_transaction(self, user_id, action_id, event):
        try:
            with self.database.sessions() as session, session.begin():
                yield session
        except BankError as error:
            # The failed mutation rolls back; its audit survives in a separate transaction.
            with self.database.sessions() as session, session.begin():
                action = session.execute(
                    select(CardAction)
                    .where(CardAction.id == action_id, CardAction.user_id == user_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if action:
                    if action.operation in ("apply", "change_password") and action.status not in (
                        "completed",
                        "cancelled",
                    ):
                        action.pending_password_hash = None
                        action.status = (
                            "cancelled"
                            if action.expires_at <= utcnow() or isinstance(error, BusinessError)
                            else "pending"
                        )
                    self._audit(session, action, event, failed=True, reason=error.reason)
            raise

    @staticmethod
    def _load(session, user_id, action_id):
        action = session.execute(
            select(CardAction)
            .where(CardAction.id == action_id, CardAction.user_id == user_id)
            .with_for_update()
        ).scalar_one_or_none()
        if action is None:
            raise NotFoundError("操作不存在")
        return action

    @staticmethod
    def _require_pending(action):
        if action.status != "pending" or action.expires_at <= utcnow():
            raise BusinessError("操作已取消或过期，请重新办理")

    @staticmethod
    def _normalize(session, user_id, operation, params):
        allowed = {
            "apply": {"card_type", "account_id"},
            "update_limit": {"card_id", "daily_limit_minor", "monthly_limit_minor"},
            "update_restrictions": {"card_id", "mode", "merchants"},
        }.get(operation, {"card_id"})
        if set(params) - allowed:
            raise ValidationError("操作包含未声明字段")
        if operation == "apply":
            card_type = params.get("card_type")
            if card_type not in CARD_TYPES:
                raise ValidationError("请选择借记卡、信用卡或虚拟卡")
            if "account_id" in params and not isinstance(params["account_id"], str):
                raise ValidationError("账户标识格式不正确")
            account = CardService._resolve_account(session, user_id, params.get("account_id"))
            return {}, {"card_type": card_type, "account_id": account.id}, "yellow"
        if not isinstance(params.get("card_id"), str) or not params["card_id"]:
            raise ValidationError("需要有效的卡片标识")
        card = session.execute(
            select(Card)
            .where(Card.id == params.get("card_id"), Card.user_id == user_id)
            .with_for_update()
        ).scalar_one_or_none()
        if card is None:
            raise NotFoundError("卡片不存在")
        before = card_view(card)
        normalized = {"card_id": card.id}
        level = "yellow"
        if operation == "update_limit":
            if not ({"daily_limit_minor", "monthly_limit_minor"} & set(params)):
                raise ValidationError("请提供至少一个交易限额")
            daily = params.get("daily_limit_minor", card.daily_limit_minor)
            monthly = params.get("monthly_limit_minor", card.monthly_limit_minor)
            CardService._validate_limits(daily, monthly)
            if daily > monthly:
                raise ValidationError("日限额不能超过月限额；零表示禁止交易")
            normalized.update(daily_limit_minor=daily, monthly_limit_minor=monthly)
            level = (
                "red"
                if daily > card.daily_limit_minor or monthly > card.monthly_limit_minor
                else "yellow"
            )
        elif operation == "update_restrictions":
            mode, merchants = params.get("mode"), params.get("merchants")
            if (
                mode not in ("add", "remove", "replace")
                or not isinstance(merchants, list)
                or len(merchants) > 100
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 128
                    for item in merchants
                )
            ):
                raise ValidationError("商户名单或修改方式不正确")
            old, requested = (
                set(card.merchant_blacklist or []),
                {item.strip() for item in merchants},
            )
            updated = (
                old | requested
                if mode == "add"
                else old - requested
                if mode == "remove"
                else requested
            )
            normalized["merchant_blacklist"] = sorted(updated)
            level = "red" if old - updated else "yellow"
        elif operation == "change_password":
            if card.status not in (ACTIVE, FROZEN) or card.freeze_reason == "lost":
                raise BusinessError("该卡当前不能修改交易密码")
            level = "red"
        elif operation == "freeze":
            if card.status != ACTIVE:
                raise BusinessError("只能冻结正常卡片")
        elif operation == "report_loss":
            if card.status not in (ACTIVE, FROZEN) or card.freeze_reason == "lost":
                raise BusinessError("该卡不能再次挂失")
            level = "red"
        elif operation in ("unfreeze", "release_loss"):
            if card.status != FROZEN or (card.freeze_reason == "lost") != (
                operation == "release_loss"
            ):
                raise BusinessError("挂失卡需要解挂，普通冻结卡需要解冻")
            level = "red"
        if operation in ("update_limit", "update_restrictions") and card.status not in (
            ACTIVE,
            FROZEN,
        ):
            raise BusinessError("永久阻断卡不能修改")
        return before, normalized, level

    @staticmethod
    def _audit(session, action, event, *, failed=False, result=None, reason=None):
        audit_service.record(
            session,
            action=f"card.{event}",
            resource_type="card_action",
            resource_id=action.id,
            request_id=action.id,
            user_id=action.user_id,
            old_value=action.before or None,
            new_value=result,
            permission_level=action.permission_level,
            confirm_required=True,
            mfa_required=action.permission_level == "red",
            result="failure" if failed else "success",
            reason=reason or LABELS[action.operation],
        )
