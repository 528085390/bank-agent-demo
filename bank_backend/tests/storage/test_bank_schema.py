"""模拟银行迁移的 schema 契约测试。

覆盖计划要求：空库迁移、外键、幂等唯一键、订阅账期唯一键和 available_minor 字段。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ALEMBIC_INI

EXPECTED_TABLES = {
    "users",
    "accounts",
    "cards",
    "card_actions",
    "contacts",
    "transactions",
    "products",
    "holdings",
    "orders",
    "scheduled_tasks",
    "aa_requests",
    "aa_participants",
    "subscriptions",
    "subscription_charges",
    "audit_logs",
    "idempotency_records",
    "simulation_state",
    "security_state",
    "wealth_risk_assessments",
}


def _insert_user(conn, user_id="u_test", phone="13900000000"):
    conn.execute(
        text(
            "INSERT INTO users (id, name, phone, risk_level, status) "
            "VALUES (:id, '测试用户', :phone, 'low', 'active')"
        ),
        {"id": user_id, "phone": phone},
    )


def _insert_account(conn, account_id="a_test", user_id="u_test", balance=100000):
    conn.execute(
        text(
            "INSERT INTO accounts "
            "(id, user_id, account_type, balance_minor, available_minor, interest_rate, status) "
            "VALUES (:id, :uid, 'current', :balance, :balance, 0, 'active')"
        ),
        {"id": account_id, "uid": user_id, "balance": balance},
    )


def _insert_subscription(conn, subscription_id="s_test", user_id="u_test", account_id="a_test"):
    conn.execute(
        text(
            "INSERT INTO subscriptions "
            "(id, user_id, from_account_id, name, merchant_name, amount_minor, frequency, "
            " start_date, next_due_date, status, auto_renew) "
            "VALUES (:id, :uid, :aid, '测试订阅', '模拟商户', 5900, 'monthly', "
            "        DATE '2026-09-01', DATE '2026-10-01', 'active', true)"
        ),
        {"id": subscription_id, "uid": user_id, "aid": account_id},
    )


def test_empty_database_migration_creates_all_nineteen_tables(engine):
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables
    assert len(EXPECTED_TABLES) == 19


def test_downgrade_removes_every_domain_table(engine, private_schema):
    command.downgrade(Config(str(ALEMBIC_INI)), "base")
    assert EXPECTED_TABLES.isdisjoint(set(inspect(engine).get_table_names()))


def test_foreign_key_rejects_account_for_unknown_user(connection):
    with pytest.raises(IntegrityError):
        _insert_account(connection, user_id="u_missing")


def test_foreign_key_is_restrict_not_cascade(connection):
    _insert_user(connection)
    _insert_account(connection)
    with pytest.raises(IntegrityError):
        connection.execute(text("DELETE FROM users WHERE id = 'u_test'"))


def test_idempotency_primary_key_rejects_duplicate_key(connection):
    statement = text(
        "INSERT INTO idempotency_records (user_id, operation_type, idempotent_key, result) "
        "VALUES ('u_test', 'transfer', 'key-0001', '{}'::jsonb)"
    )
    connection.execute(statement)
    with pytest.raises(IntegrityError):
        connection.execute(statement)


def test_idempotency_same_key_different_operation_type_is_allowed(connection):
    connection.execute(
        text(
            "INSERT INTO idempotency_records (user_id, operation_type, idempotent_key, result) "
            "VALUES ('u_test', 'transfer', 'key-0001', '{}'::jsonb)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO idempotency_records (user_id, operation_type, idempotent_key, result) "
            "VALUES ('u_test', 'purchase', 'key-0001', '{}'::jsonb)"
        )
    )
    count = connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one()
    assert count == 2


def test_subscription_charge_due_date_is_unique_per_subscription(connection):
    _insert_user(connection)
    _insert_account(connection)
    _insert_subscription(connection)
    statement = text(
        "INSERT INTO subscription_charges "
        "(id, subscription_id, due_date, amount_minor, status) "
        "VALUES (:id, 's_test', DATE '2026-10-01', 5900, 'completed')"
    )
    connection.execute(statement, {"id": "c_1"})
    with pytest.raises(IntegrityError):
        connection.execute(statement, {"id": "c_2"})


def test_subscription_charge_same_due_date_different_subscription_is_allowed(connection):
    _insert_user(connection)
    _insert_account(connection)
    _insert_subscription(connection, subscription_id="s_1")
    _insert_subscription(connection, subscription_id="s_2")
    for charge_id, subscription_id in (("c_1", "s_1"), ("c_2", "s_2")):
        connection.execute(
            text(
                "INSERT INTO subscription_charges "
                "(id, subscription_id, due_date, amount_minor, status) "
                "VALUES (:id, :sid, DATE '2026-10-01', 5900, 'completed')"
            ),
            {"id": charge_id, "sid": subscription_id},
        )
    count = connection.execute(text("SELECT count(*) FROM subscription_charges")).scalar_one()
    assert count == 2


def test_available_minor_is_bigint_and_tracks_balance(engine, connection):
    _insert_user(connection)
    _insert_account(connection, balance=1250050)
    row = connection.execute(
        text("SELECT balance_minor, available_minor FROM accounts WHERE id = 'a_test'")
    ).one()
    assert row.balance_minor == 1250050
    assert row.available_minor == 1250050

    columns = {c["name"]: c for c in inspect(engine).get_columns("accounts")}
    assert columns["available_minor"]["type"].__class__.__name__ == "BIGINT"
    assert columns["balance_minor"]["type"].__class__.__name__ == "BIGINT"


def test_available_minor_check_rejects_negative_value(connection):
    _insert_user(connection)
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_id, account_type, balance_minor, available_minor, interest_rate, status) "
                "VALUES ('a_neg', 'u_test', 'current', 0, -1, 0, 'active')"
            )
        )


def test_balance_check_rejects_negative_value(connection):
    _insert_user(connection)
    with pytest.raises(IntegrityError):
        _insert_account(connection, account_id="a_neg", balance=-1)


def test_account_status_check_rejects_unknown_value(connection):
    _insert_user(connection)
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, user_id, account_type, balance_minor, available_minor, interest_rate, status) "
                "VALUES ('a_bad', 'u_test', 'current', 0, 0, 0, 'frozen')"
            )
        )


def test_transaction_amount_check_rejects_non_positive_value(connection):
    _insert_user(connection)
    _insert_account(connection)
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO transactions "
                "(id, user_id, account_id, direction, amount_minor, transaction_type, status) "
                "VALUES ('t_zero', 'u_test', 'a_test', 'debit', 0, 'consumption', 'completed')"
            )
        )


def test_simulation_state_is_limited_to_a_single_row(connection):
    # current_time 是 PostgreSQL 关键字，裸 SQL 必须加引号（ORM 会自动引用）。
    connection.execute(
        text(
            'INSERT INTO simulation_state (id, "current_time", run_id) '
            "VALUES (1, TIMESTAMPTZ '2026-09-20 10:00:00+08', gen_random_uuid())"
        )
    )
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                'INSERT INTO simulation_state (id, "current_time", run_id) '
                "VALUES (2, TIMESTAMPTZ '2026-09-20 10:00:00+08', gen_random_uuid())"
            )
        )


def test_aa_participant_position_is_unique_within_a_request(connection):
    _insert_user(connection)
    _insert_account(connection)
    connection.execute(
        text(
            "INSERT INTO aa_requests "
            "(id, user_id, from_account_id, total_amount_minor, participant_count, status, expires_at) "
            "VALUES ('aa_1', 'u_test', 'a_test', 30000, 2, 'open', "
            "        TIMESTAMPTZ '2026-10-01 00:00:00+08')"
        )
    )
    statement = text(
        "INSERT INTO aa_participants "
        "(id, aa_request_id, position, name, amount_minor, status) "
        "VALUES (:id, 'aa_1', 0, '同事', 15000, 'pending')"
    )
    connection.execute(statement, {"id": "p_1"})
    with pytest.raises(IntegrityError):
        connection.execute(statement, {"id": "p_2"})


def test_declared_indexes_exist_on_transactions(engine):
    index_names = {i["name"] for i in inspect(engine).get_indexes("transactions")}
    assert {
        "ix_transactions_user_created_type",
        "ix_transactions_account_created",
        "ix_transactions_reference",
    } <= index_names


def test_scheduled_task_index_on_status_and_execute_at(engine):
    index_names = {i["name"] for i in inspect(engine).get_indexes("scheduled_tasks")}
    assert "ix_scheduled_tasks_status_execute_at" in index_names
