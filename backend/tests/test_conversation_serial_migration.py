"""The per-owner conversation number: that it arrives, and that it can be taken back off.

The number is a property of the row, not of the list: archiving and soft-deleting a
conversation keep its number, and a user's numbers are contiguous from 1. The backfill has
to reproduce that from history, which is why the ordering it walks -- `(created_at,
conversation_id)` within one owner -- is pinned here rather than assumed.
"""

import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, create_engine, text

from app.core.config import get_settings
from app.storage.models import Conversation
from tests.conftest import require_test_database, resolve_test_database_url

PRIOR_TABLES = {
    "conversation",
    "conversation_message",
    "conversation_submission",
    "llm_call_audit",
    "agent_run_event",
    "context_summary",
    "business_audit_event",
}


@pytest.fixture
def serial_schema(monkeypatch):
    """A schema of its own, named so the session fixture can clean up a killed run's leavings."""

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)
    # `test_runtime_` is one of the prefixes `conftest` sweeps for stale schemas.
    schema = "test_runtime_conversation_serial_" + uuid.uuid4().hex
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

        A name alone does not pin the shape: an index named for `(user_id, serial_no)` that
        was actually built over `(serial_no, user_id)` would keep every name assertion green.
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


def _insert_user(connection, user_id: str, display_name: str = "演示用户") -> None:
    """A row for the app_user table exactly as the schema at this revision declares it.

    `id` is written explicitly because the model's `uuid.uuid4` is a client-side default:
    raw SQL never triggers it, and the column is the primary key with no server default.

    `display_name` is conditional on purpose. This test stops at 0010, where the column is
    still NOT NULL; `head` has dropped it (0019). Asking the schema instead of assuming one
    shape is what keeps the helper usable at both ends of the chain -- hardcoding either
    shape breaks the other end, and the failure reads like a backfill bug rather than a
    fixture bug.
    """

    columns = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'app_user'"
            )
        )
    }
    if "display_name" in columns:
        connection.execute(
            text(
                "INSERT INTO app_user (id, user_id, display_name)"
                " VALUES (gen_random_uuid(), :user_id, :display_name)"
            ),
            {"user_id": user_id, "display_name": display_name},
        )
    else:
        connection.execute(
            text(
                "INSERT INTO app_user (id, user_id)"
                " VALUES (gen_random_uuid(), :user_id)"
            ),
            {"user_id": user_id},
        )


def _insert_conversation(
    connection,
    conversation_id: str,
    user_id: str,
    created_at: str,
    *,
    archived_at: str | None = None,
    deleted_at: str | None = None,
) -> None:
    """A conversation row with every NOT NULL column spelled out.

    `pinned` and `next_message_sequence` carry server defaults, but they are written here
    too so the insert states the whole row rather than leaning on a default that a later
    migration could move.
    """

    connection.execute(
        text(
            "INSERT INTO conversation"
            " (id, conversation_id, user_id, title, pinned, archived_at, deleted_at,"
            " created_at, updated_at, last_message_at, next_message_sequence, thread_id)"
            " VALUES (gen_random_uuid(), :conversation_id, :user_id, '新会话', false,"
            " :archived_at, :deleted_at, :created_at, :created_at, NULL, 1, :thread_id)"
        ),
        {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "archived_at": archived_at,
            "deleted_at": deleted_at,
            "created_at": created_at,
            "thread_id": f"conv:{conversation_id}",
        },
    )


def test_the_serial_number_revision_is_still_reachable():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    # 0013 (the widened tool-call audit id) sits above it, and 0014 above that; the chain still runs
    # through here. Pinning the head would re-break this file every time a revision is added, so
    # assert reachability from the head instead.
    on_chain = {
        rev.revision for rev in script.iterate_revisions(script.get_current_head(), "base")
    }
    assert "0013_agent_run_event_call_id" in on_chain
    assert (
        script.get_revision("0012_llm_call_audit_stream").down_revision
        == "0011_conversation_serial_no"
    )
    assert (
        script.get_revision("0011_conversation_serial_no").down_revision
        == "0010_business_audit_event"
    )


def test_upgrading_adds_a_not_null_unique_number(serial_schema):
    command.upgrade(Config("alembic.ini"), "head")

    assert "serial_no" in serial_schema.columns("conversation")
    assert "uq_conversation_serial_no_owner" in serial_schema.indexes("conversation")
    assert serial_schema.index_columns("conversation", "uq_conversation_serial_no_owner") == (
        "user_id",
        "serial_no",
    )
    with serial_schema.connect() as connection:
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_schema = :schema AND table_name = 'conversation'"
                " AND column_name = 'serial_no'"
            ),
            {"schema": serial_schema.name},
        ).scalar_one()
    assert nullable == "NO", "a number every row has cannot be absent"
    assert PRIOR_TABLES <= serial_schema.tables()


def test_the_model_declares_the_same_number_as_the_migration():
    """The migration and the model are two descriptions of one column; they must agree."""

    table = Conversation.__table__

    assert "serial_no" in table.columns
    assert table.c.serial_no.nullable is False
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_conversation_serial_no_owner"] == ("user_id", "serial_no")


def test_existing_rows_are_numbered_per_owner_in_creation_order(serial_schema):
    """History is renumbered per owner, oldest first, and a hidden row still holds a number."""

    config = Config("alembic.ini")
    command.upgrade(config, "0010_business_audit_event")

    with serial_schema.connect() as connection:
        _insert_user(connection, "user_0001", "演示用户")
        _insert_user(connection, "user_0002", "另一个人")
        # Deliberately out of order: the backfill must sort, not trust insertion order.
        _insert_conversation(connection, "conv_a_third", "user_0001", "2026-01-03T00:00:00+00:00")
        _insert_conversation(connection, "conv_b_first", "user_0001", "2026-01-01T00:00:00+00:00")
        _insert_conversation(
            connection,
            "conv_c_second",
            "user_0001",
            "2026-01-02T00:00:00+00:00",
            archived_at="2026-01-05T00:00:00+00:00",
        )
        _insert_conversation(
            connection,
            "conv_d_only",
            "user_0002",
            "2026-01-04T00:00:00+00:00",
            deleted_at="2026-01-06T00:00:00+00:00",
        )
        _insert_conversation(connection, "conv_e_second", "user_0002", "2026-01-05T00:00:00+00:00")
        connection.commit()

    command.upgrade(config, "head")

    def numbered(user_id: str) -> list[tuple[str, int]]:
        with serial_schema.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT conversation_id, serial_no FROM conversation"
                    " WHERE user_id = :user_id ORDER BY created_at, conversation_id"
                ),
                {"user_id": user_id},
            )
            return [(row[0], row[1]) for row in rows]

    assert numbered("user_0001") == [
        ("conv_b_first", 1),
        ("conv_c_second", 2),
        ("conv_a_third", 3),
    ]
    assert numbered("user_0002") == [("conv_d_only", 1), ("conv_e_second", 2)]
    # Archived and soft-deleted rows are numbered like any other: the number is a fact about
    # the row, not about its visibility.
    assert [number for _, number in numbered("user_0001")] == [1, 2, 3]


def test_the_number_can_be_taken_back_off(serial_schema):
    config = Config("alembic.ini")
    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")

    command.downgrade(config, "0010_business_audit_event")

    assert "serial_no" not in serial_schema.columns("conversation")
    assert "uq_conversation_serial_no_owner" not in serial_schema.indexes("conversation")

    # 0014_bill_compatibility refuses to downgrade by design; build up to the last reversible revision.
    command.upgrade(config, "0013_agent_run_event_call_id")
    assert "serial_no" in serial_schema.columns("conversation")
