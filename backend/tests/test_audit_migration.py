"""The audit migration: that it reaches the schema, and that it can be taken back off.

A downgrade nobody ever ran is a downgrade that does not work, and this one is the only path
back from the audit tables. The test therefore does the full round trip against a real
database in a schema of its own -- upgrade, check what arrived, downgrade one revision,
check that the audit tables are gone and that the conversation tables they sit next to are
not, then upgrade again.
"""

import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError

from app.core.config import get_settings
from tests.conftest import require_test_database, resolve_test_database_url

AUDIT_TABLES = {"llm_call_audit", "agent_run_event"}
CONVERSATION_TABLES = {"conversation", "conversation_message", "conversation_submission"}

EXPECTED_COLUMNS = {
    "llm_call_audit": {
        "audit_id",
        "call_id",
        "conversation_id",
        "run_id",
        "user_id",
        "source",
        "agent_id",
        "provider",
        "model",
        "request_format",
        "prompt_json",
        "response_json",
        "response_text",
        "result",
        "error_code",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "created_at",
    },
    "agent_run_event": {
        "event_id",
        "conversation_id",
        "run_id",
        "call_id",
        "event_type",
        "agent_id",
        "tool_name",
        "user_id",
        "source",
        "status",
        "payload_json",
        "error_code",
        "created_at",
    },
}


@pytest.fixture
def audit_schema(monkeypatch):
    """A schema of its own, named so the session fixture can clean up a killed run's leavings."""

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)
    # `test_runtime_` is one of the prefixes `conftest` sweeps for stale schemas.
    schema = "test_runtime_audit_" + uuid.uuid4().hex
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

    def column_width(table: str, column: str) -> int | None:
        """The declared character width, for a column whose width is the point of a migration."""

        with admin.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns"
                    " WHERE table_schema = :schema AND table_name = :table"
                    " AND column_name = :column"
                ),
                {"schema": schema, "table": table, "column": column},
            ).one()
            return row[0]

    def connect():
        """A connection in this test's schema, for statements the fixture cannot express.

        The schema travels in the DSN's `options` rather than in `PGOPTIONS`, because this
        engine is opened after `monkeypatch` has already set the environment for alembic and
        must not depend on when the driver happens to read it. The `+psycopg` dialect prefix
        is kept: stripping it would make SQLAlchemy reach for psycopg2, which is not installed.
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
        column_width=column_width,
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


def test_the_audit_revision_is_the_head_of_the_chain():
    """A revision nobody can reach is a revision that never runs."""

    script = ScriptDirectory.from_config(Config("alembic.ini"))

    # 0008's place in the chain, not the newest revision: a later migration makes 0008 stop
    # being the head without making it unreachable. Pinning the head itself would re-break
    # here every time a revision is added, and that pin belongs to whoever adds one.
    assert "0008_llm_interaction_audit" in {r.revision for r in script.walk_revisions()}, (
        "0008 is reachable from the head"
    )
    assert script.get_revision("0008_llm_interaction_audit").down_revision == (
        "0007_conversation_persistence"
    )


def test_upgrading_creates_the_audit_tables_with_their_columns(audit_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert AUDIT_TABLES <= audit_schema.tables()
    assert CONVERSATION_TABLES <= audit_schema.tables(), "0007 is still there"
    for table, expected in EXPECTED_COLUMNS.items():
        assert expected <= audit_schema.columns(table), table


def test_the_query_indexes_exist(audit_schema):
    """The plan's queries are by conversation, run, call and time; each has an index."""

    command.upgrade(Config("alembic.ini"), "head")

    assert {
        "ix_llm_call_audit_conversation",
        "ix_llm_call_audit_run",
        "ix_llm_call_audit_created",
    } <= audit_schema.indexes("llm_call_audit")
    assert {
        "ix_agent_run_event_conversation",
        "ix_agent_run_event_run",
        "ix_agent_run_event_call",
        "ix_agent_run_event_type",
    } <= audit_schema.indexes("agent_run_event")


def test_downgrading_removes_only_the_audit_tables(audit_schema):
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(Config("alembic.ini"), "0013_agent_run_event_call_id")

    command.downgrade(Config("alembic.ini"), "0007_conversation_persistence")

    assert not (AUDIT_TABLES & audit_schema.tables())
    assert CONVERSATION_TABLES <= audit_schema.tables()


def test_the_round_trip_can_be_walked_again(audit_schema):
    """Up, down, up: a downgrade that leaves a broken revision behind would fail here."""

    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    command.downgrade(config, "0007_conversation_persistence")
    command.upgrade(config, "0013_agent_run_event_call_id")

    assert AUDIT_TABLES <= audit_schema.tables()
    assert EXPECTED_COLUMNS["llm_call_audit"] <= audit_schema.columns("llm_call_audit")


def test_the_tool_call_id_column_holds_the_minted_id(audit_schema):
    """0013 widens `agent_run_event.call_id` to 256; its downgrade narrows it back to 64.

    The Supervisor mints `agent_id:tool_name:uuid4().hex`. At the widths the same table
    declares beside it (agent_id 64, tool_name 128) that id can reach 226 characters, which
    the pre-0013 column of 64 could not hold -- `subscription_execute` alone minted 66.
    """

    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    assert audit_schema.column_width("agent_run_event", "call_id") == 256

    command.downgrade(config, "0012_llm_call_audit_stream")
    assert audit_schema.column_width("agent_run_event", "call_id") == 64

    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    assert audit_schema.column_width("agent_run_event", "call_id") == 256


def test_the_downgrade_refuses_a_row_the_narrow_column_cannot_hold(audit_schema):
    """0013's downgrade narrows to 64; a row that needs 256 must stop it, not be truncated.

    The migration docstring promises the downgrade fails loudly rather than truncating a row
    the narrower column cannot hold. An empty-table round trip cannot tell that apart from a
    silent truncation, so this test puts a real minted id in the column first and pins the
    failure to the value being too long for the narrowed column.
    """

    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")

    call_id = "subscription:subscription_execute:" + uuid.uuid4().hex
    assert len(call_id) > 64, "the row must need the widened column"
    event_id = "evt_" + uuid.uuid4().hex
    with audit_schema.connect() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_run_event (id, event_id, event_type, call_id, created_at)"
                " VALUES (gen_random_uuid(), :event_id, 'TOOL_CALL', :call_id, now())"
            ),
            {"event_id": event_id, "call_id": call_id},
        )
        connection.commit()

    with pytest.raises(DataError) as caught:
        command.downgrade(config, "0012_llm_call_audit_stream")

    # 22001 is string_data_right_truncation: the specific failure, not merely "an error".
    assert getattr(caught.value.orig, "sqlstate", None) == "22001", (
        f"the downgrade must fail because the row is too long, not for another reason: {caught.value}"
    )

    # The failed downgrade rolled back, so the column is still 256. Put the schema back at
    # 0013 and remove the row, leaving the empty-table round trip and teardown as they were.
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    with audit_schema.connect() as connection:
        connection.execute(
            text("DELETE FROM agent_run_event WHERE event_id = :event_id"),
            {"event_id": event_id},
        )
        connection.commit()
