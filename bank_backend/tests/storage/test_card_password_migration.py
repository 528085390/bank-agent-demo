"""Incremental initialization retains pre-existing bank rows and never resets PINs."""

from importlib import import_module

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.core.config import get_settings
from app.domain.card_password import verify_password
from tests.conftest import ALEMBIC_INI


def test_upgrade_adopts_compatible_columns_without_rewriting_other_history(engine):
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0003_card_wealth_merge")
    migration = import_module("app.storage.migrations.versions.0004_card_transaction_password")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0003_card_wealth_merge"
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0004_card_transaction_password"
        )


def test_upgrade_initializes_existing_cards_without_seed_or_data_loss(engine):
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0003_card_wealth_merge")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users(id,name,phone,risk_level,status) VALUES ('keep','保留','13900001234','low','active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts(id,user_id,account_type,balance_minor,available_minor,interest_rate,status) VALUES ('keep-account','keep','current',12345,12345,0,'active')"
            )
        )
        for card_id in ("keep-card-1", "keep-card-2"):
            connection.execute(
                text(
                    "INSERT INTO cards(id,user_id,account_id,card_number,card_type,daily_limit_minor,monthly_limit_minor,status,merchant_blacklist) VALUES (:id,'keep','keep-account','**** 9922','debit',100,1000,'active','[]')"
                ),
                {"id": card_id},
            )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        hashes = (
            connection.execute(text("SELECT transaction_password_hash FROM cards ORDER BY id"))
            .scalars()
            .all()
        )
        assert len(hashes) == 2 and hashes[0] != hashes[1]
        assert all(
            verify_password(get_settings().demo_card_password.get_secret_value(), value)
            for value in hashes
        )
        assert (
            connection.execute(
                text("SELECT balance_minor FROM accounts WHERE id='keep-account'")
            ).scalar_one()
            == 12345
        )
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0004_card_transaction_password"
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT transaction_password_hash FROM cards ORDER BY id"))
            .scalars()
            .all()
            == hashes
        )
