"""AA 收款请求。

按人数平均拆分金额，最后一名参与者承担余数，保证各分摊项之和恰好等于总额。
每名参与者单独支付（双边流水），可取消；超过 expires_at 后自动过期。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import BankError, BusinessError, NotFoundError, ValidationError
from app.core.ids import AA, AA_PARTICIPANT, new_id
from app.core.timeutil import iso
from app.domain import audit_service, clock_service, risk
from app.domain.idempotency_service import execute_once, request_fingerprint
from app.domain.money_service import post_transfer
from app.domain.ownership import owned
from app.storage.database import Database
from app.storage.models import AaParticipant, AaRequest, Account

OPEN = "open"
PARTIAL = "partial"
COMPLETED = "completed"
CANCELLED = "cancelled"
EXPIRED = "expired"

PENDING = "pending"
PAID = "paid"
REFUSED = "refused"

DEFAULT_EXPIRY_DAYS = 7
MAX_AA_MINOR = 100000000


def split_amount(total_amount_minor: int, participant_count: int) -> list[int]:
    """平均拆分，余数由最后一名参与者承担。"""
    if participant_count < 2:
        raise ValidationError("AA 至少需要 2 名参与者")
    if total_amount_minor < participant_count:
        raise ValidationError("总金额不足以让每位参与者至少分摊 1 分")
    base, remainder = divmod(total_amount_minor, participant_count)
    return [base] * (participant_count - 1) + [base + remainder]


def participant_view(participant: AaParticipant) -> dict:
    return {
        "id": participant.id,
        "participant_index": participant.position,
        "name": participant.name,
        "phone": participant.phone,
        "account_id": participant.account_id,
        "amount_minor": participant.amount_minor,
        "status": participant.status,
        "transaction_id": participant.transaction_id,
        "paid_at": iso(participant.paid_at),
    }


def aa_view(request: AaRequest) -> dict:
    return {
        "aa_request_id": request.id,
        "user_id": request.user_id,
        "from_account_id": request.from_account_id,
        "total_amount_minor": request.total_amount_minor,
        "participant_count": request.participant_count,
        "remark": request.remark,
        "status": request.status,
        "created_at": iso(request.created_at),
        "expires_at": iso(request.expires_at),
        "participants": [
            participant_view(item)
            for item in sorted(request.participants, key=lambda item: item.position)
        ],
    }


class AaService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    # ------------------------------------------------------------ 创建

    def create(
        self,
        *,
        user_id: str,
        total_amount_minor: int,
        participants: list[dict],
        from_account_id: str | None = None,
        remark: str | None = None,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "create_aa"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    from app.domain.account_service import require_user

                    require_user(session, user_id)
                    if not participants or len(participants) < 2:
                        raise ValidationError("AA 至少需要 2 名参与者")
                    if not isinstance(total_amount_minor, int) or isinstance(
                        total_amount_minor, bool
                    ):
                        raise ValidationError("total_amount_minor 必须是整数分")
                    if total_amount_minor <= 0 or total_amount_minor > MAX_AA_MINOR:
                        raise ValidationError("total_amount_minor 超出允许范围")

                    amounts = split_amount(total_amount_minor, len(participants))
                    receiving = self._resolve_receiving_account(
                        session, user_id, from_account_id
                    )
                    now = clock_service.ClockService(self.database).current_time()

                    def factory() -> dict:
                        aa_request = AaRequest(
                            id=new_id(AA),
                            user_id=user_id,
                            from_account_id=receiving.id,
                            total_amount_minor=total_amount_minor,
                            participant_count=len(participants),
                            remark=remark,
                            status=OPEN,
                            expires_at=now + timedelta(days=DEFAULT_EXPIRY_DAYS),
                        )
                        session.add(aa_request)
                        session.flush()

                        for position, (spec, amount) in enumerate(
                            zip(participants, amounts, strict=True)
                        ):
                            session.add(
                                AaParticipant(
                                    id=new_id(AA_PARTICIPANT),
                                    aa_request_id=aa_request.id,
                                    position=position,
                                    name=spec["name"],
                                    phone=spec.get("phone"),
                                    account_id=spec.get("account_id"),
                                    amount_minor=amount,
                                    status=PENDING,
                                )
                            )
                        session.flush()
                        session.refresh(aa_request)
                        payload = aa_view(aa_request)
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="aa_request",
                            resource_id=aa_request.id,
                            user_id=user_id,
                            new_value=payload,
                            permission_level=risk.GREEN,
                            reason=remark,
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
                                "total_amount_minor": total_amount_minor,
                                "participants": participants,
                                "from_account_id": from_account_id,
                            }
                        ),
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            audit_service.record_in_new_transaction(
                self.database,
                action=action,
                resource_type="aa_request",
                user_id=user_id,
                new_value={"total_amount_minor": total_amount_minor},
                result=audit_service.RESULT_FAILURE,
                reason=exc.reason,
                request_id=request_id,
            )
            raise

    # ------------------------------------------------------------ 查询

    def get(self, aa_request_id: str, *, user_id: str) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                aa_request = self._visible(session, aa_request_id, user_id)
                self._refresh_expiry(session, aa_request)
                return aa_view(aa_request)

    # ------------------------------------------------------------ 支付

    def pay(
        self,
        aa_request_id: str,
        *,
        participant_index: int,
        confirm: bool,
        user_id: str,
        idempotent_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        action = "pay_aa"
        try:
            with self.database.sessions() as session:
                with session.begin():
                    aa_request = self._visible(session, aa_request_id, user_id)
                    self._refresh_expiry(session, aa_request)
                    if aa_request.status in (CANCELLED, EXPIRED):
                        raise BusinessError(f"AA 请求已 {aa_request.status}，不可支付")
                    if not confirm:
                        raise BusinessError("支付 AA 分摊项需要 confirm=true")

                    participant = self._participant_at(session, aa_request, participant_index)
                    if participant.status == PAID:
                        raise BusinessError(f"分摊项 {participant_index} 已支付")
                    if participant.status == REFUSED:
                        raise BusinessError(f"分摊项 {participant_index} 已拒绝")
                    if not participant.account_id:
                        raise BusinessError(
                            f"参与者 {participant.name} 没有绑定账户，无法支付"
                        )

                    # 付款账户必须是本人的：分摊项记的是「该参与者从哪个账户出钱」，
                    # 不校验归属就等于允许别人用第三方的账户替我付款。
                    payer = owned(
                        session, Account, participant.account_id, user_id, label="账户"
                    )
                    receiving = session.get(Account, aa_request.from_account_id)
                    if receiving is None:
                        raise NotFoundError(f"账户不存在: {aa_request.from_account_id}")

                    now = clock_service.ClockService(self.database).current_time()

                    def factory() -> dict:
                        outgoing, incoming = post_transfer(
                            session,
                            from_account=payer,
                            to_account=receiving,
                            amount_minor=participant.amount_minor,
                            transaction_type="aa",
                            category="aa",
                            remark=f"AA 分摊：{aa_request.remark or aa_request.id}",
                            reference_type="aa_request",
                            reference_id=aa_request.id,
                            created_at=now,
                        )
                        participant.status = PAID
                        participant.transaction_id = outgoing.id
                        participant.paid_at = now
                        self._refresh_status(session, aa_request)

                        payload = {
                            "aa_request_id": aa_request.id,
                            "participant_index": participant.position,
                            "participant_id": participant.id,
                            "transaction_id": outgoing.id,
                            "counterparty_transaction_id": incoming.id,
                            "amount_minor": outgoing.amount_minor,
                            "status": participant.status,
                            "aa_status": aa_request.status,
                        }
                        audit_service.record(
                            session,
                            action=action,
                            resource_type="aa_participant",
                            resource_id=participant.id,
                            user_id=user_id or aa_request.user_id,
                            old_value={"status": PENDING},
                            new_value=payload,
                            reason="AA 分摊项支付",
                            request_id=request_id,
                        )
                        return payload

                    result, replayed = execute_once(
                        session,
                        user_id=user_id or aa_request.user_id,
                        operation_type=f"{action}:{aa_request_id}:{participant_index}",
                        idempotent_key=idempotent_key,
                        result_factory=factory,
                    )
                    return {**result, "replayed": replayed}
        except BankError as exc:
            audit_service.record_in_new_transaction(
                self.database,
                action=action,
                resource_type="aa_participant",
                resource_id=aa_request_id,
                user_id=user_id,
                new_value={"participant_index": participant_index},
                result=audit_service.RESULT_FAILURE,
                reason=exc.reason,
                request_id=request_id,
            )
            raise

    # ------------------------------------------------------------ 取消

    def cancel(self, aa_request_id: str, *, user_id: str, request_id: str | None = None) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                # 只有发起人能取消；参与人只能看和付自己那一份。
                aa_request = owned(
                    session, AaRequest, aa_request_id, user_id, label="AA 请求"
                )
                self._refresh_expiry(session, aa_request)
                if aa_request.status in (CANCELLED, EXPIRED, COMPLETED):
                    raise BusinessError(f"AA 请求已 {aa_request.status}，不可取消")

                old = aa_view(aa_request)
                aa_request.status = CANCELLED
                for participant in aa_request.participants:
                    if participant.status == PENDING:
                        participant.status = REFUSED
                session.flush()
                session.refresh(aa_request)

                payload = aa_view(aa_request)
                audit_service.record(
                    session,
                    action="cancel_aa",
                    resource_type="aa_request",
                    resource_id=aa_request.id,
                    user_id=aa_request.user_id,
                    old_value=old,
                    new_value=payload,
                    reason="用户取消 AA 请求",
                    request_id=request_id,
                )
                return payload

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _visible(session: Session, aa_request_id: str, user_id: str) -> AaRequest:
        """发起人，或任一参与者的账户主人，才看得见这笔 AA。

        AA 是少数「一笔资源有两个合法主人」的地方：发起人要能查看和取消，参与者要能
        查看和付自己那一份。其他人——包括只是想猜 ID 的人——得到的是一句「不存在」。
        """

        aa_request = session.get(AaRequest, aa_request_id)
        if aa_request is None:
            raise NotFoundError(f"AA 请求不存在: {aa_request_id}")
        if aa_request.user_id == user_id:
            return aa_request
        account_ids = [item.account_id for item in aa_request.participants if item.account_id]
        if account_ids:
            mine = session.scalar(
                select(Account.id).where(
                    Account.id.in_(account_ids), Account.user_id == user_id
                )
            )
            if mine is not None:
                return aa_request
        raise NotFoundError(f"AA 请求不存在: {aa_request_id}")

    @staticmethod
    def _participant_at(
        session: Session, aa_request: AaRequest, participant_index: int
    ) -> AaParticipant:
        if participant_index < 0:
            raise ValidationError("participant_index 必须 >= 0")
        participant = session.execute(
            select(AaParticipant).where(
                AaParticipant.aa_request_id == aa_request.id,
                AaParticipant.position == participant_index,
            )
        ).scalar_one_or_none()
        if participant is None:
            raise NotFoundError(
                f"AA 请求 {aa_request.id} 没有第 {participant_index} 个分摊项"
            )
        return participant

    def _refresh_expiry(self, session: Session, aa_request: AaRequest, now=None) -> None:
        if aa_request.status not in (OPEN, PARTIAL):
            return
        now = now or clock_service.ClockService(self.database).current_time()
        if aa_request.expires_at <= now:
            aa_request.status = EXPIRED
            session.flush()

    @staticmethod
    def _refresh_status(session: Session, aa_request: AaRequest) -> None:
        statuses = {item.status for item in aa_request.participants}
        if statuses == {PAID}:
            aa_request.status = COMPLETED
        elif PAID in statuses:
            aa_request.status = PARTIAL
        session.flush()

    @staticmethod
    def _resolve_receiving_account(
        session: Session, user_id: str, from_account_id: str | None
    ) -> Account:
        if from_account_id:
            # 收款账户必须是发起人自己的：否则可以建一笔「钱进别人账户」的 AA，
            # 参与者以为在还发起人，实际付给了第三方。
            return owned(session, Account, from_account_id, user_id, label="账户")
        account = session.execute(
            select(Account)
            .where(Account.user_id == user_id, Account.account_type == "current")
            .order_by(Account.id)
            .limit(1)
        ).scalar_one_or_none()
        if account is None:
            raise NotFoundError(f"用户 {user_id} 没有可用的活期账户")
        return account
