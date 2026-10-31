"""卡片、订阅与生日联动的领域测试。

覆盖计划要求：卡片状态流转、订阅账期唯一保护、生日订单 mock 成功/失败，
以及三类任务共用 run_due_tasks。
"""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import (
    BusinessError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from app.core.timeutil import CST, iso
from app.domain.birthday_service import BirthdayService, execute_at_for
from app.domain.card_action_service import CardActionService
from app.domain.card_service import CardService
from app.domain.clock_service import ClockService
from app.domain.subscription_service import SubscriptionService
from app.storage.models import (
    Account,
    Card,
    Order,
    ScheduledTask,
    SubscriptionCharge,
    Transaction,
)


@pytest.fixture
def cards(seeded_database):
    return CardService(seeded_database)


@pytest.fixture
def subs(seeded_database):
    return SubscriptionService(seeded_database)


@pytest.fixture
def birthdays(seeded_database):
    return BirthdayService(seeded_database)


@pytest.fixture
def clock(seeded_database):
    return ClockService(seeded_database)


def _balance(database, account_id) -> int:
    with database.sessions() as session:
        return session.get_one(Account, account_id).balance_minor


def _task_status(database, task_id) -> str:
    with database.sessions() as session:
        return session.get_one(ScheduledTask, task_id).status


# ---------------------------------------------------------------- 卡片


def test_seeded_cards_are_listed_per_user(cards):
    zhang = cards.list_cards("u_zhang")
    assert {item["card_id"] for item in zhang} == {"card_zhang_debit", "card_zhang_virtual"}
    assert cards.list_cards("u_partner")[0]["card_id"] == "card_partner_debit"


def test_card_numbers_are_masked(cards):
    for item in cards.list_cards("u_zhang"):
        assert "****" in item["card_number"]
        assert item["cvv_masked"] == "***"


def apply_with_pin(database, key="card-001"):
    service = CardActionService(database)
    action = service.prepare("u_zhang", "apply", {"card_type": "virtual"}, key)
    service.authorize(
        "u_zhang",
        action["action_id"],
        "confirm",
        password_credentials={"new_password": "725903", "confirm_password": "725903"},
    )
    return service.execute("u_zhang", action["action_id"])


def test_apply_card_creates_card_and_order(cards, seeded_database):
    created = apply_with_pin(seeded_database)
    assert created["status"] == "active"
    assert created["daily_limit_minor"] == 500000
    assert created["merchant_blacklist"] == []
    assert created["order_id"].startswith("order_")

    with seeded_database.sessions() as session:
        order = session.get_one(Order, created["order_id"])
    assert order.order_type == "card_application"
    assert order.details["card_id"] == created["card_id"]


def test_apply_card_is_idempotent(cards, seeded_database):
    payload = {
        "user_id": "u_zhang",
        "card_type": "virtual",
        "idempotent_key": "card-001",
    }
    first = apply_with_pin(seeded_database, payload["idempotent_key"])
    second = apply_with_pin(seeded_database, payload["idempotent_key"])
    assert first["card_id"] == second["card_id"]
    assert second["replayed"] is True
    with seeded_database.sessions() as session:
        assert session.execute(select(func.count()).select_from(Card)).scalar_one() == 4


def test_apply_card_rejects_an_unknown_type(cards):
    with pytest.raises(ValidationError):
        cards.apply(user_id="u_zhang", card_type="platinum")


def test_freeze_then_unfreeze_a_card(cards):
    assert cards.freeze("card_zhang_debit", user_id="u_zhang")["status"] == "frozen"
    assert cards.unfreeze("card_zhang_debit", user_id="u_zhang")["status"] == "active"
    with pytest.raises(BusinessError):
        cards.unfreeze("card_zhang_debit", user_id="u_zhang")


def test_block_is_terminal(cards):
    assert cards.block("card_zhang_debit", user_id="u_zhang")["status"] == "blocked"
    with pytest.raises(BusinessError):
        cards.unfreeze("card_zhang_debit", user_id="u_zhang")
    with pytest.raises(BusinessError):
        cards.freeze("card_zhang_debit", user_id="u_zhang")
    with pytest.raises(BusinessError):
        cards.block("card_zhang_debit", user_id="u_zhang")


def test_update_card_limit(cards):
    updated = cards.update_limit(
        "card_zhang_debit", daily_limit_minor=100000, monthly_limit_minor=1000000
    , user_id="u_zhang")
    assert updated["daily_limit_minor"] == 100000
    assert updated["monthly_limit_minor"] == 1000000


def test_card_limits_are_validated(cards):
    with pytest.raises(ValidationError):
        cards.update_limit(
            "card_zhang_debit", daily_limit_minor=-1, monthly_limit_minor=1000
        , user_id="u_zhang")
    with pytest.raises(ValidationError):
        cards.update_limit(
            "card_zhang_debit", daily_limit_minor=2000, monthly_limit_minor=1000
        , user_id="u_zhang")


def test_merchant_blacklist_is_deduplicated(cards):
    result = cards.set_restrictions(
        "card_zhang_debit",
        merchant_blacklist=[" 赌场 ", "赌场", "高风险商户", ""],
    user_id="u_zhang")
    assert result["merchant_blacklist"] == ["赌场", "高风险商户"]
    assert cards.get_restrictions("card_zhang_debit", user_id="u_zhang")["merchant_blacklist"] == [
        "赌场",
        "高风险商户",
    ]


def test_card_operations_on_a_missing_card(cards):
    with pytest.raises(NotFoundError):
        cards.get_card("card_missing", user_id="u_zhang")
    with pytest.raises(NotFoundError):
        cards.freeze("card_missing", user_id="u_zhang")


# ---------------------------------------------------------------- 订阅


def test_seeded_subscriptions_are_listed(subs):
    listed = subs.list_subscriptions(user_id="u_zhang")
    assert {item["subscription_id"] for item in listed} == {"sub_netflix", "sub_spotify"}
    assert subs.list_subscriptions(user_id="u_zhang", status="cancelled") == []


def test_create_subscription_schedules_a_charge_task(subs, seeded_database):
    created = subs.create(
        user_id="u_zhang",
        name="iCloud",
        merchant_name="Apple",
        amount_minor=1200,
        frequency="monthly",
        from_account_id="a_zhang_current",
        start_date=date(2026, 10, 5),
    )
    assert created["status"] == "active"
    assert created["next_due_date"] == "2026-10-05"

    with seeded_database.sessions() as session:
        tasks = session.execute(
            select(ScheduledTask).where(
                ScheduledTask.task_type == "subscription_charge"
            )
        ).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].payload["subscription_id"] == created["subscription_id"]


def test_pause_cancels_the_pending_task_and_resume_reschedules(
    subs, seeded_database
):
    created = subs.create(
        user_id="u_zhang",
        name="iCloud",
        merchant_name="Apple",
        amount_minor=1200,
        frequency="monthly",
        from_account_id="a_zhang_current",
        start_date=date(2026, 10, 5),
    )
    paused = subs.pause(created["subscription_id"], user_id="u_zhang")
    assert paused["status"] == "paused"
    assert paused["auto_renew"] is False

    with seeded_database.sessions() as session:
        statuses = session.execute(
            select(ScheduledTask.status).where(
                ScheduledTask.task_type == "subscription_charge"
            )
        ).scalars().all()
    assert statuses == ["cancelled"]

    resumed = subs.resume(created["subscription_id"], user_id="u_zhang")
    assert resumed["status"] == "active"
    with seeded_database.sessions() as session:
        tasks = session.execute(
            select(ScheduledTask).where(
                ScheduledTask.task_type == "subscription_charge",
                ScheduledTask.status == "scheduled",
            )
        ).scalars().all()
    assert len(tasks) == 1


def test_cancelled_subscription_cannot_be_resumed(subs):
    created = subs.create(
        user_id="u_zhang",
        name="iCloud",
        merchant_name="Apple",
        amount_minor=1200,
        frequency="monthly",
        from_account_id="a_zhang_current",
        start_date=date(2026, 10, 5),
    )
    subs.cancel(created["subscription_id"], user_id="u_zhang")
    with pytest.raises(BusinessError):
        subs.resume(created["subscription_id"], user_id="u_zhang")


def test_charge_now_debits_and_advances_the_due_date(subs, seeded_database):
    result = subs.charge_now("sub_netflix", user_id="u_zhang")
    assert result["status"] == "completed"
    assert result["due_date"] == "2026-10-10"
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 5900

    subscription = subs.get("sub_netflix", user_id="u_zhang")
    assert subscription["next_due_date"] == "2026-11-10"
    assert subscription["last_charge_id"] == result["charge_id"]

    with seeded_database.sessions() as session:
        entry = session.get_one(Transaction, result["transaction_id"])
    assert entry.transaction_type == "subscription"
    assert entry.direction == "debit"


def test_a_period_can_only_be_charged_once(subs, seeded_database):
    subs.charge_now("sub_netflix", user_id="u_zhang")
    # 下一期已推进到 2026-11-10，再次 charge_now 会扣下一期而不是重复当期。
    second = subs.charge_now("sub_netflix", user_id="u_zhang")
    assert second["due_date"] == "2026-11-10"
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 5900 * 2

    with seeded_database.sessions() as session:
        dates = session.execute(
            select(SubscriptionCharge.due_date).where(
                SubscriptionCharge.subscription_id == "sub_netflix"
            )
        ).scalars().all()
    assert len(dates) == len(set(dates))


def test_charge_now_fails_when_the_balance_is_gone(subs, seeded_database):
    # 先把钱几乎转空，再代扣。
    with seeded_database.sessions() as session:
        with session.begin():
            session.get_one(Account, "a_zhang_current").balance_minor = 100
            session.get_one(Account, "a_zhang_current").available_minor = 100

    with pytest.raises(InsufficientBalanceError):
        subs.charge_now("sub_netflix", user_id="u_zhang")

    assert _balance(seeded_database, "a_zhang_current") == 100
    with seeded_database.sessions() as session:
        # 种子里已有 2026-09-10 的已完成账期，这里只取本次失败的那条。
        charge = session.execute(
            select(SubscriptionCharge).where(
                SubscriptionCharge.subscription_id == "sub_netflix",
                SubscriptionCharge.status == "failed",
            )
        ).scalars().one()
    assert charge.due_date.isoformat() == "2026-10-10"
    assert charge.transaction_id is None
    # 失败账期同样推进，避免同一账期反复重试。
    assert subs.get("sub_netflix", user_id="u_zhang")["next_due_date"] == "2026-11-10"


def test_run_due_charges_the_subscription_when_the_clock_reaches_it(
    subs, clock, seeded_database
):
    # 先暂停 Spotify（2026-10-01 到期），把断言限定在 Netflix 上。
    subs.pause("sub_spotify", user_id="u_zhang")
    clock.set_time(clock.current_time().replace(month=10, day=10, hour=10))
    result = subs.run_due()
    assert result["processed_count"] == 1
    assert result["completed_count"] == 1
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 5900

    detail = subs.get("sub_netflix", user_id="u_zhang")
    assert detail["next_due_date"] == "2026-11-10"


def test_run_due_only_processes_subscription_tasks(subs, clock, seeded_database):
    with seeded_database.sessions() as session:
        with session.begin():
            from app.storage.tasks import create_task

            create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=clock.current_time() - timedelta(hours=1),
                payload={},
            )
    assert subs.run_due()["processed_count"] == 0


def test_paused_subscription_is_not_charged_by_run_due(subs, clock, seeded_database):
    subs.pause("sub_netflix", user_id="u_zhang")
    subs.pause("sub_spotify", user_id="u_zhang")
    clock.set_time(clock.current_time().replace(month=10, day=10, hour=10))
    assert subs.run_due()["processed_count"] == 0
    assert _balance(seeded_database, "a_zhang_current") == 1250050


# ---------------------------------------------------------------- 生日联动


def test_mock_products_are_fixed(birthdays):
    products = birthdays.list_mock_products()
    assert products == [
        {"product_code": "flowers", "name": "模拟鲜花", "price_minor": 19900, "currency": "CNY"},
        {"product_code": "cake", "name": "模拟蛋糕", "price_minor": 12900, "currency": "CNY"},
    ]


def test_birthday_contacts_expose_the_recipient_account(birthdays):
    contacts = birthdays.list_birthday_contacts("u_zhang")
    assert [item["contact_id"] for item in contacts] == ["c_partner"]
    assert contacts[0]["recipient_account_id"] == "a_partner_current"


def test_birthday_order_schedules_a_task_two_days_before(birthdays, seeded_database):
    created = birthdays.create_order(
        user_id="u_zhang",
        contact_id="c_partner",
        product_code="flowers",
        from_account_id="a_zhang_current",
        birthday_date=date(2026, 9, 25),
    )
    assert created["status"] == "pending"
    assert created["amount_minor"] == 19900
    assert created["task_id"].startswith("task_")

    with seeded_database.sessions() as session:
        task = session.get_one(ScheduledTask, created["task_id"])
    assert task.task_type == "birthday_order"
    assert task.execute_at == execute_at_for(date(2026, 9, 25))
    assert iso(task.execute_at).startswith("2026-09-23T08:00:00")
    # 到期前不扣款。
    assert _balance(seeded_database, "a_zhang_current") == 1250050


def test_birthday_order_executes_on_clock_run(birthdays, clock, seeded_database):
    created = birthdays.create_order(
        user_id="u_zhang",
        contact_id="c_partner",
        product_code="cake",
        from_account_id="a_zhang_current",
        birthday_date=date(2026, 9, 25),
    )
    clock.set_time(execute_at_for(date(2026, 9, 25)))
    run = clock.run_due_tasks()
    assert run["completed_count"] == 1

    order = birthdays.get_order(created["order_id"], user_id="u_zhang")
    assert order["status"] == "completed"
    assert order["transaction_id"].startswith("txn_")
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 12900

    with seeded_database.sessions() as session:
        entry = session.get_one(Transaction, order["transaction_id"])
    assert entry.transaction_type == "birthday"
    assert entry.direction == "debit"


def test_birthday_order_mock_failure_marks_order_failed_without_charging(
    birthdays, clock, seeded_database
):
    created = birthdays.create_order(
        user_id="u_zhang",
        contact_id="c_partner",
        product_code="flowers",
        from_account_id="a_zhang_current",
        birthday_date=date(2026, 9, 25),
    )
    clock.set_time(execute_at_for(date(2026, 9, 25)))
    run = clock.run_due_tasks(birthday_outcome="fail")
    assert run["failed_count"] == 1
    assert run["items"][0]["error_code"] == "invalid_state"

    order = birthdays.get_order(created["order_id"], user_id="u_zhang")
    assert order["status"] == "failed"
    assert order["transaction_id"] is None
    assert "birthday_outcome=fail" in order["details"]["failure_reason"]
    # 没有产生任何扣款。
    assert _balance(seeded_database, "a_zhang_current") == 1250050
    with seeded_database.sessions() as session:
        count = session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.transaction_type == "birthday")
        ).scalar_one()
    assert count == 0


def test_cancelled_birthday_order_does_not_execute(birthdays, clock, seeded_database):
    created = birthdays.create_order(
        user_id="u_zhang",
        contact_id="c_partner",
        product_code="cake",
        from_account_id="a_zhang_current",
        birthday_date=date(2026, 9, 25),
    )
    cancelled = birthdays.cancel_order(created["order_id"], user_id="u_zhang")
    assert cancelled["status"] == "cancelled"
    assert _task_status(seeded_database, created["task_id"]) == "cancelled"

    clock.set_time(execute_at_for(date(2026, 9, 25)))
    assert clock.run_due_tasks()["processed_count"] == 0
    assert _balance(seeded_database, "a_zhang_current") == 1250050
    with pytest.raises(BusinessError):
        birthdays.cancel_order(created["order_id"], user_id="u_zhang")


def test_birthday_order_rejects_an_unknown_contact_and_product(birthdays):
    with pytest.raises(NotFoundError):
        birthdays.create_order(
            user_id="u_zhang",
            contact_id="c_missing",
            product_code="flowers",
            from_account_id="a_zhang_current",
            birthday_date=date(2026, 9, 25),
        )
    with pytest.raises(ValidationError):
        birthdays.create_order(
            user_id="u_zhang",
            contact_id="c_partner",
            product_code="balloon",
            from_account_id="a_zhang_current",
            birthday_date=date(2026, 9, 25),
        )


def test_birthday_order_is_idempotent(birthdays, seeded_database):
    payload = {
        "user_id": "u_zhang",
        "contact_id": "c_partner",
        "product_code": "flowers",
        "from_account_id": "a_zhang_current",
        "birthday_date": date(2026, 9, 25),
        "idempotent_key": "birthday-001",
    }
    first = birthdays.create_order(**payload)
    second = birthdays.create_order(**payload)
    assert first["order_id"] == second["order_id"]
    assert second["replayed"] is True
    with seeded_database.sessions() as session:
        assert session.execute(
            select(func.count()).select_from(Order).where(Order.order_type == "birthday")
        ).scalar_one() == 1


# ---------------------------------------------------------------- 统一任务执行


def test_transfer_subscription_and_birthday_share_one_task_runner(
    seeded_database, clock, subs, birthdays
):
    """三类任务在同一张表、同一次 run_due_tasks 中一起被执行。"""
    from app.domain.transfer_service import TransferService

    TransferService(seeded_database).create_scheduled(
        user_id="u_zhang",
        from_account_id="a_zhang_current",
        to_account_id="a_partner_current",
        amount_minor=20000,
        execute_at=datetime(2026, 9, 23, 9, 0, tzinfo=CST),
    )
    birthdays.create_order(
        user_id="u_zhang",
        contact_id="c_partner",
        product_code="flowers",
        from_account_id="a_zhang_current",
        birthday_date=date(2026, 9, 25),
    )
    with seeded_database.sessions() as session:
        kinds = set(
            session.execute(select(ScheduledTask.task_type)).scalars().all()
        )
    assert kinds == {"scheduled_transfer", "birthday_order"}

    clock.set_time(clock.current_time().replace(month=9, day=23, hour=10))
    run = clock.run_due_tasks()
    # 定时转账到期执行；生日订单要等到 09-23 08:00 之后，同一次运行也命中。
    assert run["processed_count"] == 2
    assert run["completed_count"] == 2
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 20000 - 19900
