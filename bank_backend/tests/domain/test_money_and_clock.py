"""资金、幂等、审计和时钟的契约测试。

覆盖计划要求：重复幂等、余额不足、时钟回拨和任务重复执行。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessError, InsufficientBalanceError, ValidationError
from app.core.timeutil import iso
from app.domain import audit_service, clock_service
from app.domain.clock_service import ClockService
from app.domain.idempotency_service import execute_once, find, request_fingerprint
from app.domain.money_service import CREDIT, DEBIT, post_entry, post_transfer
from app.domain.seed_service import SeedService
from app.storage.models import Account, AuditLog, IdempotencyRecord, Transaction
from app.storage.tasks import (
    COMPLETED,
    FAILED,
    cancel_task,
    create_task,
    finish_task,
)

CST = timezone(timedelta(hours=8))


@pytest.fixture
def seeded(database):
    SeedService(database).seed()
    return database


def _account(session, account_id):
    return session.get_one(Account, account_id)


# ---------------------------------------------------------------- 资金写入


def test_debit_over_balance_is_rejected_and_leaves_balance_untouched(seeded):
    with seeded.sessions() as session:
        with session.begin():
            account = _account(session, "a_li_current")
            before = account.balance_minor
            with pytest.raises(InsufficientBalanceError):
                post_entry(
                    session,
                    account=account,
                    direction=DEBIT,
                    amount_minor=before + 1,
                    transaction_type="consumption",
                )

    with seeded.sessions() as session:
        assert _account(session, "a_li_current").balance_minor == 850000


def test_amount_must_be_a_positive_integer(seeded):
    with seeded.sessions() as session:
        with session.begin():
            account = _account(session, "a_li_current")
            for bad in (0, -1, True, "100"):
                with pytest.raises(ValidationError):
                    post_entry(
                        session,
                        account=account,
                        direction=DEBIT,
                        amount_minor=bad,
                        transaction_type="consumption",
                    )


def test_bilateral_transfer_shares_reference_id_and_updates_both_accounts(seeded):
    with seeded.sessions() as session:
        with session.begin():
            source = _account(session, "a_zhang_current")
            target = _account(session, "a_li_current")
            out, incoming = post_transfer(
                session,
                from_account=source,
                to_account=target,
                amount_minor=20000,
                category="transfer",
            )
            assert out.reference_id == incoming.reference_id
            assert out.direction == DEBIT
            assert incoming.direction == CREDIT
            assert source.balance_minor == 1250050 - 20000
            assert target.balance_minor == 850000 + 20000

    with seeded.sessions() as session:
        source = _account(session, "a_zhang_current")
        target = _account(session, "a_li_current")
        assert source.available_minor == source.balance_minor == 1230050
        assert target.available_minor == target.balance_minor == 870000


def test_available_minor_is_updated_with_balance_on_every_entry(seeded):
    with seeded.sessions() as session:
        with session.begin():
            account = _account(session, "a_li_current")
            post_entry(
                session,
                account=account,
                direction=CREDIT,
                amount_minor=12345,
                transaction_type="interest",
            )
            assert account.balance_minor == account.available_minor == 862345


# ---------------------------------------------------------------- 幂等


def test_repeated_idempotent_key_replays_the_first_result(seeded):
    calls = []

    def factory():
        calls.append(1)
        return {"transaction_id": "txn_fixed", "amount_minor": 20000}

    with seeded.sessions() as session:
        with session.begin():
            first, replayed_first = execute_once(
                session,
                user_id="u_zhang",
                operation_type="transfer",
                idempotent_key="key-0001",
                result_factory=factory,
                request_hash=request_fingerprint({"amount_minor": 20000}),
            )
            second, replayed_second = execute_once(
                session,
                user_id="u_zhang",
                operation_type="transfer",
                idempotent_key="key-0001",
                result_factory=factory,
                request_hash=request_fingerprint({"amount_minor": 20000}),
            )

    assert first == second == {"transaction_id": "txn_fixed", "amount_minor": 20000}
    assert replayed_first is False
    assert replayed_second is True
    assert len(calls) == 1

    with seeded.sessions() as session:
        count = session.execute(
            select(func.count()).select_from(IdempotencyRecord)
        ).scalar_one()
    assert count == 1


def test_different_operation_type_or_user_is_a_separate_key(seeded):
    def factory():
        return {"ok": True}

    with seeded.sessions() as session:
        with session.begin():
            _, a = execute_once(
                session,
                user_id="u_zhang",
                operation_type="transfer",
                idempotent_key="key-0002",
                result_factory=factory,
            )
            _, b = execute_once(
                session,
                user_id="u_zhang",
                operation_type="purchase",
                idempotent_key="key-0002",
                result_factory=factory,
            )
            _, c = execute_once(
                session,
                user_id="u_li",
                operation_type="transfer",
                idempotent_key="key-0002",
                result_factory=factory,
            )
    assert (a, b, c) == (False, False, False)


def test_without_a_key_every_call_executes(seeded):
    calls = []

    def factory():
        calls.append(1)
        return {"ok": True}

    with seeded.sessions() as session:
        with session.begin():
            for _ in range(3):
                _, replayed = execute_once(
                    session,
                    user_id="u_zhang",
                    operation_type="transfer",
                    idempotent_key=None,
                    result_factory=factory,
                )
                assert replayed is False
    assert len(calls) == 3
    with seeded.sessions() as session:
        assert find(
            session, user_id="u_zhang", operation_type="transfer", idempotent_key="x"
        ) is None


# ---------------------------------------------------------------- 审计


def test_audit_records_request_id_and_old_new_values(seeded):
    with seeded.sessions() as session:
        with session.begin():
            account = _account(session, "a_li_current")
            old = audit_service.snapshot(account, ("balance_minor",))
            post_entry(
                session,
                account=account,
                direction=DEBIT,
                amount_minor=500,
                transaction_type="consumption",
            )
            entry = audit_service.record(
                session,
                action="consume",
                resource_type="account",
                resource_id=account.id,
                user_id=account.user_id,
                old_value=old,
                new_value=audit_service.snapshot(account, ("balance_minor",)),
                permission_level=audit_service.YELLOW,
                confirm_required=True,
                reason="mock 消费",
            )
            assert entry.request_id.startswith("req_")

    with seeded.sessions() as session:
        entry = session.execute(select(AuditLog)).scalars().one()
        assert entry.old_value == {"balance_minor": 850000}
        assert entry.new_value == {"balance_minor": 849500}
        assert entry.permission_level == "yellow"
        assert entry.confirm_required is True
        assert entry.result == "success"


# ---------------------------------------------------------------- 时钟


def test_clock_cannot_be_moved_backwards(seeded):
    clock = ClockService(seeded)
    now = clock.current_time()
    with pytest.raises(BusinessError):
        clock.set_time(now - timedelta(seconds=1))
    assert clock.current_time() == now


def test_clock_moves_forward_and_reports_due_count(seeded):
    clock = ClockService(seeded)
    now = clock.current_time()
    with seeded.sessions() as session:
        with session.begin():
            create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=now + timedelta(days=1),
                payload={"amount_minor": 100},
            )
    assert clock.get()["due_task_count"] == 0
    payload = clock.set_time(now + timedelta(days=2))
    assert payload["due_task_count"] == 1
    # 接口按东八区渲染，和数据库返回的偏移无关。
    assert payload["current_time"].startswith(iso(now + timedelta(days=2))[:19])


def test_due_scan_ignores_future_and_already_terminal_tasks(seeded):
    clock = ClockService(seeded)
    now = clock.current_time()
    with seeded.sessions() as session:
        with session.begin():
            create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=now + timedelta(days=5),
                payload={},
            )
            cancelled = create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=now - timedelta(days=1),
                payload={},
            )
            cancel_task(session, cancelled)
    assert clock.get()["due_task_count"] == 0


@contextmanager
def _temporary_handler(task_type: str, handler):
    """临时覆盖任务处理器，退出时恢复原有注册。

    直接 unregister 会连带删掉 transfer_service 在导入时注册的生产处理器，
    导致依赖真实处理器的用例在整套运行时失败。
    """
    previous = clock_service.handler_for(task_type)
    clock_service.register_handler(task_type, handler)
    try:
        yield
    finally:
        if previous is None:
            clock_service.unregister_handler(task_type)
        else:
            clock_service.register_handler(task_type, previous)


@pytest.fixture
def credit_handler(seeded):
    """注册一个把 payload.amount_minor 记到 payload.account_id 的处理器。"""
    executed = []

    def handler(session, task, context):
        executed.append(task.id)
        account = session.get_one(Account, task.payload["account_id"])
        entry = post_entry(
            session,
            account=account,
            direction=CREDIT,
            amount_minor=task.payload["amount_minor"],
            transaction_type="interest",
            remark=f"task {task.id}",
        )
        return {"transaction_id": entry.id}

    with _temporary_handler("scheduled_transfer", handler):
        yield executed


def _make_due_task(seeded, amount_minor=700):
    clock = ClockService(seeded)
    now = clock.current_time()
    with seeded.sessions() as session:
        with session.begin():
            task = create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=now - timedelta(minutes=1),
                payload={"account_id": "a_li_current", "amount_minor": amount_minor},
            )
            return task.id


def test_run_due_tasks_executes_and_marks_completed(seeded, credit_handler):
    task_id = _make_due_task(seeded)
    result = ClockService(seeded).run_due_tasks()
    assert result["processed_count"] == 1
    assert result["completed_count"] == 1
    assert result["failed_count"] == 0
    assert result["items"][0]["task_id"] == task_id
    assert result["items"][0]["status"] == COMPLETED

    with seeded.sessions() as session:
        assert _account(session, "a_li_current").balance_minor == 850700


def test_running_tasks_twice_does_not_execute_twice(seeded, credit_handler):
    _make_due_task(seeded)
    clock = ClockService(seeded)
    first = clock.run_due_tasks()
    second = clock.run_due_tasks()
    assert first["processed_count"] == 1
    assert second["processed_count"] == 0
    assert len(credit_handler) == 1
    with seeded.sessions() as session:
        assert _account(session, "a_li_current").balance_minor == 850700


def test_unregistered_task_type_fails_without_retrying(seeded, monkeypatch):
    # transfer_service 在导入时会注册 scheduled_transfer，这里清空注册表以验证兜底路径。
    monkeypatch.setattr(clock_service, "_HANDLERS", {})
    _make_due_task(seeded)
    clock = ClockService(seeded)
    result = clock.run_due_tasks()
    assert result["failed_count"] == 1
    assert result["items"][0]["status"] == FAILED
    assert result["items"][0]["error_code"] == "handler_not_registered"
    assert clock.run_due_tasks()["processed_count"] == 0


def test_failed_task_rolls_back_funds_but_keeps_status_and_audit(seeded):
    def failing_handler(session, task, context):
        account = session.get_one(Account, task.payload["account_id"])
        post_entry(
            session,
            account=account,
            direction=CREDIT,
            amount_minor=task.payload["amount_minor"],
            transaction_type="interest",
        )
        raise BusinessError("mock 失败")

    with _temporary_handler("scheduled_transfer", failing_handler):
        _make_due_task(seeded, amount_minor=999)
        result = ClockService(seeded).run_due_tasks()

    assert result["failed_count"] == 1
    assert result["items"][0]["status"] == FAILED
    assert result["items"][0]["error_code"] == "invalid_state"

    with seeded.sessions() as session:
        # 资金变更已回滚。
        assert _account(session, "a_li_current").balance_minor == 850000
        # 失败审计保留。
        entry = session.execute(
            select(AuditLog).where(AuditLog.result == "failure")
        ).scalars().one()
        assert entry.resource_type == "scheduled_task"
        assert entry.reason == "mock 失败"


def test_finish_task_refuses_a_second_terminal_transition(seeded):
    from app.storage.models import ScheduledTask

    task_id = _make_due_task(seeded)
    with seeded.sessions() as session:
        with session.begin():
            task = session.get_one(ScheduledTask, task_id)
            finish_task(session, task, status=COMPLETED, result={})
            with pytest.raises(BusinessError):
                finish_task(session, task, status=FAILED, result={})


def test_cancel_refuses_a_terminal_task(seeded):
    from app.storage.models import ScheduledTask

    task_id = _make_due_task(seeded)
    with seeded.sessions() as session:
        with session.begin():
            task = session.get_one(ScheduledTask, task_id)
            finish_task(session, task, status=COMPLETED, result={})
    with seeded.sessions() as session:
        with session.begin():
            task = session.get_one(ScheduledTask, task_id)
            with pytest.raises(BusinessError):
                cancel_task(session, task)


def test_transaction_rows_carry_created_at_from_the_simulation_clock(seeded):
    with seeded.sessions() as session:
        stamps = set(
            session.execute(
                select(Transaction.created_at).where(
                    Transaction.account_id == "a_zhang_current"
                )
            ).scalars()
        )
    assert stamps
    assert all(stamp.tzinfo is not None for stamp in stamps)
    assert max(stamps) < datetime(2026, 9, 20, 10, 0, tzinfo=CST)
