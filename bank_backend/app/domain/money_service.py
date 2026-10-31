"""统一资金写入。

**所有**改变账户余额的路径都必须经过本模块。它在一个事务内同时写
`transactions` 并更新 `accounts.balance_minor` 与 `available_minor`，
因此余额与流水天然一致，seed 的历史流水也由同一函数生成。

本模块只负责“写”，事务边界由调用方（领域服务）持有。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import InsufficientBalanceError, ValidationError
from app.core.ids import REFERENCE, TRANSACTION, new_id
from app.storage.models import Account, Transaction

DEBIT = "debit"
CREDIT = "credit"

# 只有付款方记录的流水类型：消费、订阅代扣、理财收益都不产生对手方账户行。
SINGLE_SIDED_TYPES = frozenset({"consumption", "subscription", "interest", "birthday"})


def validate_amount(amount_minor: int) -> int:
    """金额必须是正整数分。bool 是 int 的子类，显式排除。"""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValidationError("amount_minor 必须是整数分")
    if amount_minor <= 0:
        raise ValidationError("amount_minor 必须是正整数分")
    return amount_minor


def post_entry(
    session: Session,
    *,
    account: Account,
    direction: str,
    amount_minor: int,
    transaction_type: str,
    category: str | None = None,
    remark: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    counterparty_account_id: str | None = None,
    created_at: datetime | None = None,
    transaction_id: str | None = None,
    status: str = "completed",
) -> Transaction:
    """写一条流水并同步账户余额与 available_minor。

    debit 需要余额充足；余额不足抛 InsufficientBalanceError，调用方回滚事务。
    """
    validate_amount(amount_minor)
    if direction not in (DEBIT, CREDIT):
        raise ValidationError(f"未知的 direction: {direction}")
    if account.status != "active":
        raise ValidationError(f"账户 {account.id} 状态为 {account.status}，不可发生资金变动")

    if direction == DEBIT:
        if account.balance_minor < amount_minor:
            raise InsufficientBalanceError(
                f"账户 {account.id} 余额不足：需要 {amount_minor}，可用 {account.balance_minor}"
            )
        account.balance_minor -= amount_minor
    else:
        account.balance_minor += amount_minor

    # available_minor 是冗余读模型字段，必须与 balance_minor 一起更新。
    account.available_minor = account.balance_minor

    entry = Transaction(
        id=transaction_id or new_id(TRANSACTION),
        user_id=account.user_id,
        account_id=account.id,
        counterparty_account_id=counterparty_account_id,
        direction=direction,
        amount_minor=amount_minor,
        transaction_type=transaction_type,
        category=category,
        status=status,
        remark=remark,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    if created_at is not None:
        entry.created_at = created_at
    session.add(entry)
    session.flush()
    return entry


def post_transfer(
    session: Session,
    *,
    from_account: Account,
    to_account: Account,
    amount_minor: int,
    transaction_type: str = "transfer",
    category: str | None = None,
    remark: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    created_at: datetime | None = None,
    outgoing_transaction_id: str | None = None,
    incoming_transaction_id: str | None = None,
) -> tuple[Transaction, Transaction]:
    """双边转账：两条流水共享同一个 reference_id。"""
    if from_account.id == to_account.id:
        raise ValidationError("转出与转入账户不能相同")

    reference = reference_id or new_id(REFERENCE)
    outgoing = post_entry(
        session,
        account=from_account,
        direction=DEBIT,
        amount_minor=amount_minor,
        transaction_type=transaction_type,
        category=category,
        remark=remark,
        reference_type=reference_type,
        reference_id=reference,
        counterparty_account_id=to_account.id,
        created_at=created_at,
        transaction_id=outgoing_transaction_id,
    )
    incoming = post_entry(
        session,
        account=to_account,
        direction=CREDIT,
        amount_minor=amount_minor,
        transaction_type="transfer_in",
        category=category,
        remark=remark,
        reference_type=reference_type,
        reference_id=reference,
        counterparty_account_id=from_account.id,
        created_at=created_at,
        transaction_id=incoming_transaction_id,
    )
    return outgoing, incoming


def reconcile(session: Session, account: Account, opening_balance_minor: int) -> int:
    """对账：期初 + Σ(credit) − Σ(debit) 应等于当前 balance_minor。

    返回当前余额，调用方与期望值比较。
    """
    from sqlalchemy import func, select

    credited = session.execute(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.account_id == account.id,
            Transaction.direction == CREDIT,
            Transaction.status == "completed",
        )
    ).scalar_one()
    debited = session.execute(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.account_id == account.id,
            Transaction.direction == DEBIT,
            Transaction.status == "completed",
        )
    ).scalar_one()
    expected = opening_balance_minor + int(credited) - int(debited)
    if expected != account.balance_minor:
        raise AssertionError(
            f"账户 {account.id} 余额与流水不一致：期望 {expected}，实际 {account.balance_minor}"
        )
    if account.available_minor != account.balance_minor:
        raise AssertionError(
            f"账户 {account.id} 的 available_minor 与 balance_minor 不一致："
            f"{account.available_minor} != {account.balance_minor}"
        )
    return account.balance_minor
