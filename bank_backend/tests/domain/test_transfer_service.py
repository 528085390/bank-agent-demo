"""转账、定时转账与 AA 的领域服务测试。

覆盖计划要求：为每个写接口实现成功、基础失败和幂等重试测试。
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import (
    BusinessError,
    ConfirmationRequiredError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from app.domain.aa_service import AaService, split_amount
from app.domain.clock_service import ClockService
from app.domain.transfer_service import TransferService
from app.storage.models import AaRequest, Account, AuditLog, ScheduledTask, Transaction

MFA = "123456"


@pytest.fixture
def transfers(seeded_database):
    return TransferService(seeded_database)


@pytest.fixture
def aa(seeded_database):
    return AaService(seeded_database)


@pytest.fixture
def clock(seeded_database):
    return ClockService(seeded_database)


def _balance(database, account_id) -> int:
    with database.sessions() as session:
        return session.get_one(Account, account_id).balance_minor


def _transfers_between(database, account_id) -> int:
    with database.sessions() as session:
        return session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.account_id == account_id, Transaction.reference_type == "transfer")
        ).scalar_one()


# ---------------------------------------------------------------- 即时转账


def test_transfer_moves_money_and_writes_bilateral_entries(transfers, seeded_database):
    result = transfers.transfer(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=20000,
        remark="生活费",
    )
    assert result["status"] == "completed"
    assert result["permission_level"] == "green"
    assert result["replayed"] is False
    assert result["transaction_id"].startswith("txn_")
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 20000
    assert _balance(seeded_database, "a_partner_current") == 1200000 + 20000

    with seeded_database.sessions() as session:
        pair = (
            session.execute(
                select(Transaction).where(Transaction.reference_id == result["reference_id"])
            )
            .scalars()
            .all()
        )
    assert {item.direction for item in pair} == {"debit", "credit"}
    assert all(item.amount_minor == 20000 for item in pair)


def test_transfer_rejects_insufficient_balance(transfers, seeded_database):
    # 权限门在余额校验之前：先确认，才能走到余额不足这条路径。
    with pytest.raises(InsufficientBalanceError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_partner_current",
            amount_minor=99999999,
            confirm=True,
            mfa_code=MFA,
        )
    assert _balance(seeded_database, "a_zhang_current") == 1250050


def test_transfer_rejects_unknown_account(transfers):
    with pytest.raises(NotFoundError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_nope",
            amount_minor=100,
        )


def test_transfer_rejects_unknown_user(transfers):
    with pytest.raises(NotFoundError):
        transfers.transfer(
            user_id="u_nobody",
            from_account_id="a_zhang_current",
            to_account_id="a_partner_current",
            amount_minor=100,
        )


def test_transfer_rejects_a_fixed_deposit_account(transfers):
    with pytest.raises(BusinessError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_fixed",
            to_account_id="a_partner_current",
            amount_minor=100,
        )


def test_same_account_transfer_is_rejected(transfers):
    with pytest.raises(ValidationError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_zhang_current",
            amount_minor=100,
        )


def test_large_transfer_requires_confirmation(transfers, seeded_database):
    with pytest.raises(ConfirmationRequiredError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_partner_current",
            amount_minor=100000,
        )
    assert _balance(seeded_database, "a_zhang_current") == 1250050


def test_large_transfer_succeeds_with_confirmation(transfers):
    result = transfers.transfer(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=100000,
        confirm=True,
    )
    assert result["permission_level"] == "yellow"
    assert result["confirm_required"] is True
    assert result["mfa_required"] is False


def test_very_large_transfer_requires_mfa(transfers):
    with pytest.raises(ConfirmationRequiredError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_partner_current",
            amount_minor=500000,
            confirm=True,
        )
    result = transfers.transfer(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=500000,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["permission_level"] == "red"
    assert result["mfa_required"] is True


def test_idempotent_transfer_does_not_debit_twice(transfers, seeded_database):
    payload = {
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 20000,
        "idempotent_key": "transfer-001",
    }
    first = transfers.transfer(**payload)
    second = transfers.transfer(**payload)

    assert first["transaction_id"] == second["transaction_id"]
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 20000
    assert _transfers_between(seeded_database, "a_zhang_current") == 2


def test_transfer_writes_an_audit_row_with_permission_fields(transfers, seeded_database):
    transfers.transfer(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=20000,
        remark="生活费",
    )
    with seeded_database.sessions() as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == "transfer").order_by(AuditLog.created_at.desc())
        ).scalars().first()
    assert entry.result == "success"
    assert entry.permission_level == "green"
    assert entry.request_id.startswith("req_")
    assert entry.old_value == {"from_balance_minor": 1250050}
    assert entry.new_value["amount_minor"] == 20000


def test_failed_transfer_rolls_back_funds_but_keeps_a_failure_audit(transfers, seeded_database):
    with pytest.raises(InsufficientBalanceError):
        transfers.transfer(
            user_id="u_zhang",
            from_account_id="a_zhang_current",
            to_account_id="a_partner_current",
            amount_minor=99999999,
            confirm=True,
            mfa_code=MFA,
        )
    assert _balance(seeded_database, "a_zhang_current") == 1250050
    with seeded_database.sessions() as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.result == "failure")
        ).scalars().one()
    assert entry.action == "transfer"
    assert "余额不足" in entry.reason


# ---------------------------------------------------------------- 定时转账


def test_scheduled_transfer_runs_when_the_clock_reaches_execute_at(
    transfers, clock, seeded_database
):
    execute_at = clock.current_time() + timedelta(days=1)
    task = transfers.create_scheduled(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=20000,
        execute_at=execute_at,
        remark="定时生活费",
    )
    assert task["status"] == "scheduled"
    assert _balance(seeded_database, "a_zhang_current") == 1250050

    clock.set_time(execute_at)
    result = clock.run_due_tasks()
    assert result["completed_count"] == 1

    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 20000
    assert _balance(seeded_database, "a_partner_current") == 1200000 + 20000

    with seeded_database.sessions() as session:
        finished = session.get_one(ScheduledTask, task["task_id"])
    assert finished.status == "completed"
    assert finished.result["amount_minor"] == 20000


def test_scheduled_transfer_listing_and_cancel(transfers, clock):
    execute_at = clock.current_time() + timedelta(days=2)
    task = transfers.create_scheduled(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=100,
        execute_at=execute_at,
    )
    scheduled = transfers.list_scheduled(user_id="u_zhang")
    assert [item["task_id"] for item in scheduled] == [task["task_id"]]

    cancelled = transfers.cancel_scheduled(task["task_id"], user_id="u_zhang")
    assert cancelled["status"] == "cancelled"
    assert transfers.list_scheduled(user_id="u_zhang", status="cancelled")[0]["task_id"] == (
        task["task_id"]
    )
    with pytest.raises(BusinessError):
        transfers.cancel_scheduled(task["task_id"], user_id="u_zhang")


def test_scheduled_transfer_fails_without_retry_when_balance_is_gone(
    transfers, clock, seeded_database
):
    execute_at = clock.current_time() + timedelta(days=1)
    task = transfers.create_scheduled(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=1250050,
        execute_at=execute_at,
        confirm=True,
        mfa_code=MFA,
    )
    # 先把钱转走，使到期任务余额不足。
    transfers.transfer(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=100000,
        confirm=True,
    )
    clock.set_time(execute_at)
    result = clock.run_due_tasks()
    assert result["failed_count"] == 1
    assert result["items"][0]["error_code"] == "insufficient_balance"
    assert clock.run_due_tasks()["processed_count"] == 0

    with seeded_database.sessions() as session:
        assert session.get_one(ScheduledTask, task["task_id"]).status == "failed"


def test_scheduled_transfer_is_idempotent(transfers, clock):
    execute_at = clock.current_time() + timedelta(days=1)
    payload = {
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 100,
        "execute_at": execute_at,
        "idempotent_key": "sched-001",
    }
    first = transfers.create_scheduled(**payload)
    second = transfers.create_scheduled(**payload)
    assert first["task_id"] == second["task_id"]
    assert second["replayed"] is True


# ---------------------------------------------------------------- AA


def test_split_amount_gives_the_remainder_to_the_last_participant():
    assert split_amount(10000, 3) == [3333, 3333, 3334]
    assert sum(split_amount(10000, 3)) == 10000
    assert split_amount(30000, 3) == [10000, 10000, 10000]
    with pytest.raises(ValidationError):
        split_amount(2, 3)


def test_aa_create_splits_and_lists_participants(aa):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=10000,
        participants=[
            {"name": "同事1", "phone": "13800138001", "account_id": "a_li_current"},
            {"name": "朋友1"},
            {"name": "朋友2"},
        ],
        remark="聚餐",
    )
    assert created["status"] == "open"
    assert created["from_account_id"] == "a_zhang_current"
    assert [item["amount_minor"] for item in created["participants"]] == [3333, 3333, 3334]
    assert [item["participant_index"] for item in created["participants"]] == [0, 1, 2]

    fetched = aa.get(created["aa_request_id"], user_id="u_zhang")
    assert fetched["status"] == "open"


def test_aa_pay_moves_the_share_and_marks_partial(aa, seeded_database):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[
            {"name": "同事1", "account_id": "a_li_current"},
            {"name": "朋友1"},
        ],
    )
    paid = aa.pay(
        created["aa_request_id"], participant_index=0, confirm=True, user_id="u_li"
    )
    assert paid["status"] == "paid"
    assert paid["aa_status"] == "partial"
    assert _balance(seeded_database, "a_li_current") == 850000 - 15000
    assert _balance(seeded_database, "a_zhang_current") == 1250050 + 15000


def test_aa_completes_once_every_participant_pays(aa):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[
            {"name": "同事1", "account_id": "a_li_current"},
            {"name": "爱人", "account_id": "a_partner_current"},
        ],
    )
    aa.pay(created["aa_request_id"], participant_index=0, confirm=True, user_id="u_li")
    result = aa.pay(
        created["aa_request_id"], participant_index=1, confirm=True, user_id="u_partner"
    )
    assert result["aa_status"] == "completed"


def test_aa_pay_requires_confirmation_and_rejects_double_payment(aa):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[
            {"name": "同事1", "account_id": "a_li_current"},
            {"name": "爱人", "account_id": "a_partner_current"},
        ],
    )
    with pytest.raises(BusinessError):
        aa.pay(created["aa_request_id"], participant_index=0, confirm=False, user_id="u_li")
    aa.pay(created["aa_request_id"], participant_index=0, confirm=True, user_id="u_li")
    with pytest.raises(BusinessError):
        aa.pay(created["aa_request_id"], participant_index=0, confirm=True, user_id="u_li")


def test_aa_pay_rejects_a_participant_without_an_account(aa):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[{"name": "同事1"}, {"name": "朋友1"}],
    )
    with pytest.raises(BusinessError):
        aa.pay(created["aa_request_id"], participant_index=0, confirm=True, user_id="u_zhang")


def test_aa_cancel_marks_pending_participants_refused(aa):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[
            {"name": "同事1", "account_id": "a_li_current"},
            {"name": "朋友1"},
        ],
    )
    cancelled = aa.cancel(created["aa_request_id"], user_id="u_zhang")
    assert cancelled["status"] == "cancelled"
    assert {item["status"] for item in cancelled["participants"]} == {"refused"}
    with pytest.raises(BusinessError):
        aa.cancel(created["aa_request_id"], user_id="u_zhang")


def test_aa_expires_after_its_deadline(aa, clock):
    created = aa.create(
        user_id="u_zhang",
        total_amount_minor=30000,
        participants=[
            {"name": "同事1", "account_id": "a_li_current"},
            {"name": "朋友1"},
        ],
    )
    clock.set_time(clock.current_time() + timedelta(days=8))
    assert aa.get(created["aa_request_id"], user_id="u_zhang")["status"] == "expired"
    with pytest.raises(BusinessError):
        aa.pay(created["aa_request_id"], participant_index=0, confirm=True, user_id="u_li")


def test_aa_create_is_idempotent(aa, seeded_database):
    payload = {
        "user_id": "u_zhang",
        "total_amount_minor": 30000,
        "participants": [{"name": "同事1"}, {"name": "朋友1"}],
        "idempotent_key": "aa-001",
    }
    first = aa.create(**payload)
    second = aa.create(**payload)
    assert first["aa_request_id"] == second["aa_request_id"]
    assert second["replayed"] is True
    with seeded_database.sessions() as session:
        assert session.execute(select(func.count()).select_from(AaRequest)).scalar_one() == 1
