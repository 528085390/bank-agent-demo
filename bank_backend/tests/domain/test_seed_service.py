"""固定种子与重置的契约测试。

覆盖计划要求：显式 ID、固定余额、总额对账和重复 reset。
"""

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessError
from app.domain.money_service import reconcile
from app.domain.seed_service import SeedService
from app.storage.models import (
    Account,
    Card,
    Contact,
    Product,
    ScheduledTask,
    SimulationState,
    Subscription,
    SubscriptionCharge,
    Transaction,
    User,
)
from app.storage.seed import (
    ACCOUNTS,
    EXPECTED_CLOSING_BALANCES,
    SEED_CHARGE_ID,
    SEED_TRANSACTION_IDS,
)


@pytest.fixture
def service(database):
    return SeedService(database)


@pytest.fixture
def seeded(service):
    return service.seed()


def _count(database, model) -> int:
    with database.sessions() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_inserts_the_documented_users_and_accounts(service, seeded, database):
    with database.sessions() as session:
        user_ids = set(session.execute(select(User.id)).scalars())
        account_ids = set(session.execute(select(Account.id)).scalars())
    assert user_ids == {"u_zhang", "u_li", "u_partner", "u_merchant"}
    assert account_ids == set(EXPECTED_CLOSING_BALANCES)


def test_seed_lands_on_the_documented_fixed_balances(service, seeded, database):
    with database.sessions() as session:
        balances = dict(session.execute(select(Account.id, Account.balance_minor)).all())
    assert balances == EXPECTED_CLOSING_BALANCES


def test_seed_available_minor_equals_balance_for_every_account(service, seeded, database):
    with database.sessions() as session:
        rows = session.execute(select(Account.id, Account.balance_minor, Account.available_minor))
        for account_id, balance, available in rows:
            assert available == balance, account_id


def test_every_seeded_account_reconciles_against_its_flows(service, seeded, database):
    with database.sessions() as session:
        for spec in ACCOUNTS:
            account = session.get_one(Account, spec["id"])
            assert reconcile(session, account, spec["opening_minor"]) == (
                EXPECTED_CLOSING_BALANCES[spec["id"]]
            )


def test_seeded_history_uses_the_documented_fixed_transaction_ids(service, seeded, database):
    expected = {
        SEED_TRANSACTION_IDS["shopping"],
        *SEED_TRANSACTION_IDS["dining"],
        SEED_TRANSACTION_IDS["transfer_out"],
        SEED_TRANSACTION_IDS["transfer_in"],
        SEED_TRANSACTION_IDS["subscription"],
        SEED_TRANSACTION_IDS["interest"],
    }
    with database.sessions() as session:
        actual = set(session.execute(select(Transaction.id)).scalars())
    assert expected <= actual


def test_seeded_transfer_is_bilateral_and_shares_a_reference_id(service, seeded, database):
    with database.sessions() as session:
        out = session.get_one(Transaction, SEED_TRANSACTION_IDS["transfer_out"])
        incoming = session.get_one(Transaction, SEED_TRANSACTION_IDS["transfer_in"])
    assert out.direction == "debit"
    assert incoming.direction == "credit"
    assert out.reference_id == incoming.reference_id == "ref_seed_transfer"
    assert out.counterparty_account_id == incoming.account_id
    assert incoming.counterparty_account_id == out.account_id


def test_seeded_dining_transactions_span_ten_minutes(service, seeded, database):
    with database.sessions() as session:
        rows = session.execute(
            select(Transaction.created_at, Transaction.amount_minor)
            .where(Transaction.id.in_(SEED_TRANSACTION_IDS["dining"]))
            .order_by(Transaction.created_at)
        ).all()
    assert len(rows) == 5
    assert {amount for _, amount in rows} == {2000}
    assert (rows[-1][0] - rows[0][0]).total_seconds() == 8 * 60


def test_seeded_netflix_charge_is_linked_both_ways(service, seeded, database):
    with database.sessions() as session:
        charge = session.get_one(SubscriptionCharge, SEED_CHARGE_ID)
        netflix = session.get_one(Subscription, "sub_netflix")
    assert charge.status == "completed"
    assert charge.transaction_id == SEED_TRANSACTION_IDS["subscription"]
    assert netflix.last_charge_id == SEED_CHARGE_ID
    assert netflix.next_due_date.isoformat() == "2026-10-10"


def test_seed_creates_cards_contacts_and_products(service, seeded, database):
    assert _count(database, Card) == 3
    assert _count(database, Contact) == 3
    assert _count(database, Product) == 3
    with database.sessions() as session:
        partner = session.get_one(Contact, "c_partner")
    assert partner.birthday.isoformat() == "1995-09-25"
    assert partner.recipient_account_id == "a_partner_current"


def test_seed_sets_the_simulation_clock_and_run_id(service, seeded, database):
    from app.core.timeutil import iso

    with database.sessions() as session:
        state = session.get_one(SimulationState, 1)
    assert str(state.run_id) == seeded["run_id"]
    # 统一按东八区渲染：数据库返回的偏移随服务器而变。
    assert iso(state.current_time).startswith("2026-09-20T10:00:00")


def test_seed_does_not_create_scheduled_tasks(service, seeded, database):
    assert _count(database, ScheduledTask) == 0


def test_reset_generates_a_new_run_id(service, database):
    first = service.seed()
    second = service.reset()
    assert first["run_id"] != second["run_id"]
    assert service.current_run_id() is not None
    assert str(service.current_run_id()) == second["run_id"]


def test_reset_is_repeatable_and_does_not_duplicate_rows(service, database):
    service.seed()
    baseline = {model: _count(database, model) for model in (User, Account, Transaction, Card)}
    for _ in range(3):
        service.reset()
    after = {model: _count(database, model) for model in (User, Account, Transaction, Card)}
    assert after == baseline
    with database.sessions() as session:
        balances = dict(session.execute(select(Account.id, Account.balance_minor)).all())
    assert balances == EXPECTED_CLOSING_BALANCES


def test_reset_rejects_a_mismatched_expected_run_id(service, database):
    seeded = service.seed()
    with pytest.raises(BusinessError):
        service.reset(expected_run_id="00000000-0000-0000-0000-000000000000")
    # 被拒绝的 reset 不改变当前 run_id。
    assert str(service.current_run_id()) == seeded["run_id"]


def test_reset_accepts_a_matching_expected_run_id(service, database):
    seeded = service.seed()
    result = service.reset(expected_run_id=seeded["run_id"])
    assert result["run_id"] != seeded["run_id"]
    with database.sessions() as session:
        balances = dict(session.execute(select(Account.id, Account.balance_minor)).all())
    assert balances == EXPECTED_CLOSING_BALANCES


def test_reset_clears_previously_created_scheduled_tasks(service, database):
    from datetime import timedelta

    from app.storage.tasks import create_task

    service.seed()
    with database.sessions() as session:
        with session.begin():
            create_task(
                session,
                user_id="u_zhang",
                task_type="scheduled_transfer",
                execute_at=service.current_time() + timedelta(days=1),
                payload={},
            )
    assert _count(database, ScheduledTask) == 1
    service.reset()
    assert _count(database, ScheduledTask) == 0
