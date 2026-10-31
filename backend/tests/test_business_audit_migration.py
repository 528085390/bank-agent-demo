"""The business audit table: that it arrives, and that it can be taken back off."""

import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.storage.models import BusinessAuditEvent
from tests.conftest import require_test_database, resolve_test_database_url

BUSINESS_AUDIT_COLUMNS = {
    "id",
    "event_id",
    "user_id",
    "conversation_id",
    "run_id",
    "request_id",
    "action",
    "resource_type",
    "resource_id",
    "result",
    "reason",
    "created_at",
}
PRIOR_TABLES = {
    "conversation",
    "conversation_message",
    "conversation_submission",
    "llm_call_audit",
    "agent_run_event",
    "context_summary",
}
INDEXES = {
    "ix_business_audit_event_user_created",
    "ix_business_audit_event_conversation",
    "ix_business_audit_event_action",
}


@pytest.fixture
def business_audit_schema(monkeypatch):
    """A schema of its own, named so the session fixture can clean up a killed run's leavings."""

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)
    # `test_runtime_` is one of the prefixes `conftest` sweeps for stale schemas.
    schema = "test_runtime_business_audit_" + uuid.uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    def tables() -> set[str]:
        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
                ),
                {"schema": schema},
            )
            return {row[0] for row in rows}

    def columns(table: str) -> set[str]:
        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = :schema AND table_name = :table"
                ),
                {"schema": schema, "table": table},
            )
            return {row[0] for row in rows}

    def indexes(table: str) -> set[str]:
        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT indexname FROM pg_indexes"
                    " WHERE schemaname = :schema AND tablename = :table"
                ),
                {"schema": schema, "table": table},
            )
            return {row[0] for row in rows}

    def index_columns(table: str, index: str) -> tuple[str, ...]:
        """The index's key columns in position order.

        A name alone does not pin the shape: an index named for `(user_id, created_at)` that
        was actually built over `(created_at, user_id)` would keep every name assertion green.
        """

        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT a.attname FROM pg_index i"
                    " CROSS JOIN generate_subscripts(i.indkey, 1) AS sub(j)"
                    " JOIN pg_class c ON c.oid = i.indexrelid"
                    " JOIN pg_class t ON t.oid = i.indrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " JOIN pg_attribute a ON a.attrelid = i.indrelid"
                    " AND a.attnum = i.indkey[sub.j]"
                    " WHERE n.nspname = :s AND t.relname = :t AND c.relname = :i"
                    " ORDER BY sub.j"
                ),
                {"s": schema, "t": table, "i": index},
            )
            return tuple(row[0] for row in rows)

    def connect():
        """A connection in this test's schema, for statements the fixture cannot express.

        The schema travels in the DSN's `options` rather than in `PGOPTIONS`, because this
        engine is opened after `monkeypatch` has already set the environment for alembic
        and must not depend on when the driver happens to read it. The `+psycopg` dialect
        prefix is kept: stripping it would make SQLAlchemy reach for psycopg2, which is not
        installed.
        """

        separator = "&" if "?" in url else "?"
        return create_engine(f"{url}{separator}options=-csearch_path%3D{schema}").connect()

    # A plain namespace rather than a class: a class body that rebinds `tables` to
    # `staticmethod(tables)` cannot read the closure it is shadowing.
    schema_view = SimpleNamespace(
        name=schema,
        tables=tables,
        columns=columns,
        indexes=indexes,
        index_columns=index_columns,
        connect=connect,
    )

    try:
        yield schema_view
    finally:
        monkeypatch.delenv("PGOPTIONS", raising=False)
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_the_business_audit_revision_is_still_reachable():
    """0013 stops being the head the moment 0014 lands; it must stay reachable.

    Pinning the head here would re-break this file every time a revision is added, and
    that pin belongs to whoever adds one. What this file owns is 0010's and 0011's own
    links; the widened tool-call audit id (0013) just has to still be on the chain.
    """

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    on_chain = {
        rev.revision for rev in script.iterate_revisions(script.get_current_head(), "base")
    }
    assert "0013_agent_run_event_call_id" in on_chain
    assert (
        script.get_revision("0011_conversation_serial_no").down_revision
        == "0010_business_audit_event"
    )
    assert script.get_revision("0010_business_audit_event").down_revision == "0009_context_summary"


def test_upgrading_creates_the_business_audit_table_with_its_columns(business_audit_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert "business_audit_event" in business_audit_schema.tables()
    assert BUSINESS_AUDIT_COLUMNS <= business_audit_schema.columns("business_audit_event")
    assert PRIOR_TABLES <= business_audit_schema.tables(), "0007 through 0009 are still there"


def test_the_model_declares_the_same_table_as_the_migration():
    """The migration and the model are two descriptions of one table; they must agree."""

    table = BusinessAuditEvent.__table__

    assert table.name == "business_audit_event"
    assert set(table.columns.keys()) == BUSINESS_AUDIT_COLUMNS
    assert "updated_at" not in table.columns, "immutability: there is no `updated_at` to write"
    assert {index.name for index in table.indexes} == INDEXES


def test_the_result_is_constrained_to_the_three_outcomes(business_audit_schema):
    """A fourth outcome is not a row the database will hold."""

    command.upgrade(Config("alembic.ini"), "head")

    with business_audit_schema.connect() as connection:
        with pytest.raises(IntegrityError, match="ck_business_audit_event_result"):
            connection.execute(
                text(
                    "INSERT INTO business_audit_event"
                    " (id, event_id, action, resource_type, result, created_at)"
                    " VALUES (gen_random_uuid(), 'baud_probe', 'rename', 'conversation',"
                    " 'SUCCEEDED', now())"
                )
            )


def test_the_lookup_paths_are_indexed(business_audit_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert INDEXES <= business_audit_schema.indexes("business_audit_event")
    assert business_audit_schema.index_columns(
        "business_audit_event", "ix_business_audit_event_user_created"
    ) == ("user_id", "created_at")
    assert business_audit_schema.index_columns(
        "business_audit_event", "ix_business_audit_event_conversation"
    ) == ("conversation_id", "created_at")
    assert business_audit_schema.index_columns(
        "business_audit_event", "ix_business_audit_event_action"
    ) == ("action",)


def test_downgrading_removes_only_the_business_audit_table(business_audit_schema):
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(Config("alembic.ini"), "0013_agent_run_event_call_id")

    assert "business_audit_event" in business_audit_schema.tables(), "there is something to remove"

    command.downgrade(Config("alembic.ini"), "0009_context_summary")

    assert "business_audit_event" not in business_audit_schema.tables()
    assert PRIOR_TABLES <= business_audit_schema.tables()


def test_the_round_trip_can_be_walked_again(business_audit_schema):
    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    command.downgrade(config, "0009_context_summary")
    command.upgrade(config, "0013_agent_run_event_call_id")

    assert BUSINESS_AUDIT_COLUMNS <= business_audit_schema.columns("business_audit_event")
