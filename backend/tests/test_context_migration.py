"""The summary table: that it arrives, and that it can be taken back off."""

import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from tests.conftest import require_test_database, resolve_test_database_url

SUMMARY_COLUMNS = {
    "summary_id",
    "conversation_id",
    "version",
    "base_summary_id",
    "covered_from_sequence",
    "covered_to_sequence",
    "summary_text",
    "source_hash",
    "model",
    "created_at",
}
PRIOR_TABLES = {
    "conversation",
    "conversation_message",
    "conversation_submission",
    "llm_call_audit",
    "agent_run_event",
}


@pytest.fixture
def summary_schema(monkeypatch):
    """A schema of its own, named so the session fixture can clean up a killed run's leavings."""

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)
    # `test_runtime_` is one of the prefixes `conftest` sweeps for stale schemas.
    schema = "test_runtime_summary_" + uuid.uuid4().hex
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

    def unique_constraints(table: str) -> set[str]:
        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c"
                    " JOIN pg_class t ON t.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE n.nspname = :s AND t.relname = :t AND c.contype = 'u'"
                ),
                {"s": schema, "t": table},
            )
            return {row[0] for row in rows}

    def unique_constraint_columns(table: str, constraint: str) -> tuple[str, ...]:
        """The constraint's columns, in the key order it declares them.

        A name alone does not pin the shape: `uq_context_summary_version` built over
        `(conversation_id, summary_id)` would keep every name assertion above green. The
        subscript join walks `conkey` in place; `attnum = ANY(conkey)` would lose the order.
        """

        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c"
                    " CROSS JOIN generate_subscripts(c.conkey, 1) AS sub(i)"
                    " JOIN pg_attribute a ON a.attrelid = c.conrelid"
                    " AND a.attnum = c.conkey[sub.i]"
                    " JOIN pg_class t ON t.oid = c.conrelid"
                    " JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE n.nspname = :s AND t.relname = :t AND c.conname = :c"
                    " ORDER BY sub.i"
                ),
                {"s": schema, "t": table, "c": constraint},
            )
            return tuple(row[0] for row in rows)

    def index_columns(table: str, index: str) -> tuple[tuple[str, str], ...]:
        """The index's key columns in position order, each with the direction it sorts.

        `pg_index.indoption` carries the sort direction: the low bit is set for `DESC`, which
        is the half of `(conversation_id, version DESC)` a name assertion cannot see.
        """

        with admin.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT a.attname, (i.indoption[sub.j] & 1) = 1 FROM pg_index i"
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
            return tuple((row[0], "DESC" if row[1] else "ASC") for row in rows)

    # A plain namespace rather than a class: a class body that rebinds `tables` to
    # `staticmethod(tables)` cannot read the closure it is shadowing.
    schema_view = SimpleNamespace(
        name=schema,
        tables=tables,
        columns=columns,
        indexes=indexes,
        unique_constraints=unique_constraints,
        unique_constraint_columns=unique_constraint_columns,
        index_columns=index_columns,
    )

    try:
        yield schema_view
    finally:
        monkeypatch.delenv("PGOPTIONS", raising=False)
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_the_summary_revision_is_still_reachable():
    """0009 stops being the head the moment 0010 lands; it must stay reachable.

    Pinning the head here would re-break this file every time a revision is added, and
    that pin belongs to whoever adds one. What this file owns is 0009's own links.
    """

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0009_context_summary").down_revision == "0008_llm_interaction_audit"
    assert [item.revision for item in script.walk_revisions()][-1] == "0001_billing"


def test_upgrading_creates_the_summary_table_with_its_columns(summary_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert "context_summary" in summary_schema.tables()
    assert SUMMARY_COLUMNS <= summary_schema.columns("context_summary")
    assert PRIOR_TABLES <= summary_schema.tables(), "0007 and 0008 are still there"


def test_the_version_is_unique_per_conversation(summary_schema):
    """The last line of defence against two versions claiming the same number."""

    command.upgrade(Config("alembic.ini"), "head")

    assert "uq_context_summary_version" in summary_schema.unique_constraints("context_summary")
    assert summary_schema.unique_constraint_columns(
        "context_summary", "uq_context_summary_version"
    ) == ("conversation_id", "version")


def test_the_latest_version_lookup_is_indexed(summary_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert "ix_context_summary_conversation_version" in summary_schema.indexes("context_summary")
    assert summary_schema.index_columns(
        "context_summary", "ix_context_summary_conversation_version"
    ) == (("conversation_id", "ASC"), ("version", "DESC"))


def test_downgrading_removes_only_the_summary_table(summary_schema):
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(Config("alembic.ini"), "0013_agent_run_event_call_id")

    assert "context_summary" in summary_schema.tables(), "there is something to remove"

    command.downgrade(Config("alembic.ini"), "0008_llm_interaction_audit")

    assert "context_summary" not in summary_schema.tables()
    assert PRIOR_TABLES <= summary_schema.tables()


def test_the_round_trip_can_be_walked_again(summary_schema):
    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    command.downgrade(config, "0008_llm_interaction_audit")
    command.upgrade(config, "0013_agent_run_event_call_id")

    assert SUMMARY_COLUMNS <= summary_schema.columns("context_summary")
