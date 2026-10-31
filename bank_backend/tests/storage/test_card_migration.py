"""An additive card migration must preserve the separately deployed wealth history."""

from importlib import import_module

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from scripts.migrate_cards import migrate_cards
from tests.conftest import ALEMBIC_INI


def test_card_migration_preserves_an_external_revision_and_existing_cards(engine):
    command.downgrade(Config(str(ALEMBIC_INI)), "0001_bank_mock_domain")
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='external_wealth_legacy'"))
        connection.execute(text("CREATE TABLE wealth_marker (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO wealth_marker VALUES (1)"))
        connection.execute(
            text(
                "INSERT INTO users (id,name,phone,risk_level,status) VALUES ('u_keep','保留用户','13900000001','low','active')"
            )
        )
    migrate_cards(engine)
    migrate_cards(engine)
    assert "card_actions" in inspect(engine).get_table_names()
    assert "freeze_reason" in {column["name"] for column in inspect(engine).get_columns("cards")}
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "external_wealth_legacy"
        )
        assert connection.execute(text("SELECT count(*) FROM wealth_marker")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT name FROM users WHERE id='u_keep'")).scalar_one()
            == "保留用户"
        )


@pytest.mark.parametrize("revision", ["0002_card_actions", "0002_wealth_support"])
def test_merge_upgrades_either_deployed_branch(engine, revision):
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0001_bank_mock_domain")
    command.upgrade(config, revision)
    migrate_cards(engine)
    migrate_cards(engine)
    assert {"card_actions", "wealth_risk_assessments"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0004_card_transaction_password"
        )


def test_merge_adopts_card_objects_already_added_to_wealth_deployment(engine):
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "0001_bank_mock_domain")
    command.upgrade(config, "0002_wealth_support")
    migration = import_module("app.storage.migrations.versions.0002_card_actions")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    migrate_cards(engine)
    assert {"card_actions", "wealth_risk_assessments"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0004_card_transaction_password"
        )
