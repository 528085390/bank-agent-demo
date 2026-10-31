"""账户、余额、流水与联系人查询。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.timeutil import day_end_exclusive, day_start, iso
from app.domain.ownership import owned
from app.storage.database import Database
from app.storage.models import Account, Contact, Transaction, User

MAX_PAGE_SIZE = 100


def require_user(session: Session, user_id: str) -> User:
    """首版不做登录/授权，只做基础 ID 存在检查。"""
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"用户不存在: {user_id}")
    return user


def account_view(account: Account) -> dict:
    return {
        "account_id": account.id,
        "user_id": account.user_id,
        "account_type": account.account_type,
        "balance_minor": account.balance_minor,
        "available_minor": account.available_minor,
        "currency": account.currency,
        "interest_rate": float(account.interest_rate),
        "status": account.status,
    }


def balance_view(account: Account) -> dict:
    return {
        "account_id": account.id,
        "balance_minor": account.balance_minor,
        "available_minor": account.available_minor,
        "currency": account.currency,
    }


def transaction_view(entry: Transaction) -> dict:
    return {
        "transaction_id": entry.id,
        "account_id": entry.account_id,
        "counterparty_account_id": entry.counterparty_account_id,
        "amount_minor": entry.amount_minor,
        "direction": entry.direction,
        "transaction_type": entry.transaction_type,
        "category": entry.category,
        "status": entry.status,
        "remark": entry.remark,
        "reference_id": entry.reference_id,
        "created_at": iso(entry.created_at),
    }


def contact_view(contact: Contact) -> dict:
    return {
        "contact_id": contact.id,
        "user_id": contact.user_id,
        "name": contact.name,
        "phone": contact.phone,
        "remark": contact.remark,
        "recipient_user_id": contact.recipient_user_id,
        "recipient_account_id": contact.recipient_account_id,
        "birthday": contact.birthday.isoformat() if contact.birthday else None,
    }


class AccountService:
    def __init__(self, database: Database):
        self.database = database

    def list_accounts(self, user_id: str) -> list[dict]:
        with self.database.sessions() as session:
            require_user(session, user_id)
            accounts = session.execute(
                select(Account).where(Account.user_id == user_id).order_by(Account.id)
            ).scalars()
            return [account_view(account) for account in accounts]

    def get_account(self, account_id: str, user_id: str) -> dict:
        with self.database.sessions() as session:
            require_user(session, user_id)
            return account_view(self.load_account(session, account_id, user_id))

    def get_balance(self, account_id: str, user_id: str) -> dict:
        with self.database.sessions() as session:
            require_user(session, user_id)
            return balance_view(self.load_account(session, account_id, user_id))

    def list_balances(self, user_id: str) -> list[dict]:
        with self.database.sessions() as session:
            require_user(session, user_id)
            accounts = session.execute(
                select(Account).where(Account.user_id == user_id).order_by(Account.id)
            ).scalars()
            return [balance_view(account) for account in accounts]

    def list_transactions(
        self,
        *,
        user_id: str,
        account_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        transaction_type: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        page, page_size = _pagination(page, page_size)
        with self.database.sessions() as session:
            require_user(session, user_id)
            # 按账户过滤时先确认这个账户是本人的：只加 `Transaction.account_id == X`
            # 会把别人的流水一起筛出来，因为流水的 user_id 过滤和 account_id 过滤原本是
            # 两个独立条件。
            if account_id:
                self.load_account(session, account_id, user_id)

            filters = [Transaction.user_id == user_id]
            if start_date:
                filters.append(Transaction.created_at >= day_start(start_date))
            if end_date:
                filters.append(Transaction.created_at < day_end_exclusive(end_date))
            if transaction_type:
                filters.append(Transaction.transaction_type == transaction_type)
            if category:
                filters.append(Transaction.category == category)

            total = session.execute(
                select(func.count()).select_from(Transaction).where(*filters)
            ).scalar_one()
            rows = session.execute(
                select(Transaction)
                .where(*filters)
                .order_by(Transaction.created_at.desc(), Transaction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars()
            return _page_view([transaction_view(row) for row in rows], page, page_size, int(total))

    def search_contacts(self, user_id: str, query: str | None = None) -> list[dict]:
        text = (query or "").strip()
        with self.database.sessions() as session:
            require_user(session, user_id)
            conditions = [Contact.user_id == user_id]
            if text:
                pattern = f"%{text}%"
                conditions.append(
                    or_(
                        Contact.name.ilike(pattern),
                        Contact.phone.ilike(pattern),
                        Contact.remark.ilike(pattern),
                    )
                )
            contacts = session.execute(
                select(Contact).where(*conditions).order_by(Contact.id)
            ).scalars()
            return [contact_view(contact) for contact in contacts]

    def list_birthdays(self, user_id: str) -> list[dict]:
        with self.database.sessions() as session:
            require_user(session, user_id)
            contacts = session.execute(
                select(Contact)
                .where(Contact.user_id == user_id, Contact.birthday.is_not(None))
                .order_by(Contact.birthday)
            ).scalars()
            return [contact_view(contact) for contact in contacts]

    @staticmethod
    def load_account(session: Session, account_id: str, user_id: str) -> Account:
        """取本人的账户；别人的账户与不存在的账户回答同一句 404。"""

        return owned(session, Account, account_id, user_id, label="账户")


def _pagination(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise ValidationError("page 必须 >= 1")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间")
    return page, page_size


def _page_view(items: list[dict], page: int, page_size: int, total: int) -> dict:
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }
