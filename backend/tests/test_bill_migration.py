"""The bill migration chain: one head, 0014 sitting on it, and what 0015 does to rows already there.

The chain assertions are pure script reads. The last test is the one that needs a database: a
schema built from scratch runs 0015's backfill UPDATE over an empty `statement_document`, so a
backfill that wrote NULL or the wrong owner would stay invisible. It therefore upgrades a private
schema to 0014 first, puts rows in it, and only then moves to 0015.
"""

import importlib
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from tests.conftest import require_test_database, resolve_test_database_url


def test_the_chain_has_exactly_one_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    # 只断言"单 head"，不钉死是哪一个：Task 6 还会再加一条 0015，钉死就会像
    # test_business_audit_migration.py 那样每加一个 revision 就红一次。
    assert len(script.get_heads()) == 1


def test_the_bill_revision_is_on_the_chain():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    on_chain = {rev.revision for rev in script.iterate_revisions(script.get_current_head(), "base")}
    assert "0014_bill_compatibility" in on_chain


def test_the_bill_revision_descends_from_the_agent_run_event_revision():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0014_bill_compatibility").down_revision == "0013_agent_run_event_call_id"


def test_every_revision_id_fits_the_version_column():
    """Every id must fit `alembic_version.version_num`, which is `String(32)`.

    The bound is the column's own 32, not the project's tighter self-imposed 28:
    `0007_conversation_persistence` is 29 characters and predates this work, so a 28
    bound could never hold on this chain. The ids this task adds are well inside either
    bound (`0014_bill_compatibility` is 23, `0015_bill_doc_tenant_unique` is 27).
    """
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert all(len(rev.revision) <= 32 for rev in script.walk_revisions())


# --- 0015：文件哈希的唯一性从「全局一份」改成「每个归属人一份」 -----------------------

BILL_DOC_0014_REVISION = "0014_bill_compatibility"
BILL_DOC_0015_REVISION = "0015_bill_doc_tenant_unique"
BILL_DOC_0014_CONSTRAINT = "statement_document_source_file_hash_key"
BILL_DOC_0015_CONSTRAINT = "uq_statement_document_user_file"


@pytest.fixture
def bill_document_schema(monkeypatch):
    """A private schema for 0015's data change; the test decides which revision it sits at.

    The name keeps conftest's stale-schema prefix (`test_runtime_`) so a killed run's leavings
    are reclaimed like any other test schema's.
    """

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)
    schema = "test_runtime_bill_document_" + uuid.uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    # The schema travels in the DSN as well as in PGOPTIONS: this engine is opened after
    # monkeypatch has set the environment for alembic, and must not depend on when psycopg
    # happens to read it. The `+psycopg` dialect prefix stays -- stripping it would send
    # SQLAlchemy looking for psycopg2, which is not installed.
    separator = "&" if "?" in url else "?"
    engine = create_engine(f"{url}{separator}options=-csearch_path%3D{schema}")

    def execute(statement: str, parameters: dict | None = None) -> None:
        """Run one statement in a transaction of its own; an error rolls it back."""

        with engine.begin() as connection:
            connection.execute(text(statement), parameters or {})

    def rows(statement: str, parameters: dict | None = None) -> list[tuple]:
        with engine.begin() as connection:
            return [tuple(row) for row in connection.execute(text(statement), parameters or {})]

    def unique_constraints() -> dict[str, str]:
        """Every unique constraint on `statement_document`, name -> its definition.

        The definition, not just the name: a key named for `(user_id, source_file_hash)` that
        was actually built over `(source_file_hash)` alone would keep a name-only assertion green.
        """

        return dict(
            rows(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'statement_document'::regclass AND contype = 'u'"
            )
        )

    def is_nullable(table: str, column: str) -> str | None:
        """`information_schema`'s YES/NO for one column, or None when it does not exist yet."""

        found = rows(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_schema = :schema AND table_name = :table AND column_name = :column",
            {"schema": schema, "table": table, "column": column},
        )
        return found[0][0] if found else None

    # A plain namespace rather than a class, like the sibling migration tests use.
    schema_view = SimpleNamespace(
        name=schema,
        execute=execute,
        rows=rows,
        unique_constraints=unique_constraints,
        is_nullable=is_nullable,
    )
    try:
        yield schema_view
    finally:
        engine.dispose()
        monkeypatch.delenv("PGOPTIONS", raising=False)
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_0015_backfills_the_owner_and_scopes_the_file_hash(bill_document_schema):
    """0015 over a schema that already holds a document: the row gains its owner, and the
    global file-hash unique key becomes a per-owner one.

    Three claims, all against a schema that really went through 0014 first: the pre-existing row
    is backfilled to its owning `bill_task.user_id`; `user_id` is NOT NULL afterwards; and
    `(user_id, source_file_hash)` is what the database now enforces -- a second owner may reuse
    a hash, the same owner may not.
    """

    config = Config("alembic.ini")
    command.upgrade(config, BILL_DOC_0014_REVISION)

    # 0014 的形态：文档还没有归属列，文件哈希是**全局**唯一。
    assert bill_document_schema.unique_constraints() == {
        BILL_DOC_0014_CONSTRAINT: "UNIQUE (source_file_hash)"
    }
    assert bill_document_schema.is_nullable("statement_document", "user_id") is None

    bill_document_schema.execute(
        "INSERT INTO app_user (id, user_id, display_name) VALUES"
        " (gen_random_uuid(), 'u_a', '甲'), (gen_random_uuid(), 'u_b', '乙')"
    )
    bill_document_schema.execute(
        "INSERT INTO bill_task (id, task_no, user_id, bank_code, period_start, period_end,"
        " state, source_mode) VALUES"
        " (gen_random_uuid(), 'task_a', 'u_a', 'CMB', '2026-09-01', '2026-09-30',"
        " 'COMPLETED', 'manual_upload'),"
        " (gen_random_uuid(), 'task_b', 'u_b', 'CMB', '2026-09-01', '2026-09-30',"
        " 'WAITING_UPLOAD', 'manual_upload')"
    )
    # 0014 的文档没有 user_id 可写——这正是回填要补上的那一列。
    bill_document_schema.execute(
        "INSERT INTO statement_document (id, task_id, bank_code, source_file_hash,"
        " original_filename) SELECT gen_random_uuid(), id, 'CMB', 'hash_shared', 'a.csv'"
        " FROM bill_task WHERE task_no = 'task_a'"
    )

    command.upgrade(config, BILL_DOC_0015_REVISION)

    # 1) 回填：那一行拿到了所属 bill_task 的 user_id。
    assert bill_document_schema.rows(
        "SELECT d.user_id FROM statement_document d JOIN bill_task t ON t.id = d.task_id"
        " WHERE t.task_no = 'task_a'"
    ) == [("u_a",)]

    # 2) 回填之后 user_id 是 NOT NULL：不带它插一行会被数据库拒绝。
    assert bill_document_schema.is_nullable("statement_document", "user_id") == "NO"
    with pytest.raises(IntegrityError, match="user_id"):
        bill_document_schema.execute(
            "INSERT INTO statement_document (id, task_id, bank_code, source_file_hash,"
            " original_filename) SELECT gen_random_uuid(), id, 'CMB', 'hash_no_owner',"
            " 'no_owner.csv' FROM bill_task WHERE task_no = 'task_b'"
        )

    # 3) 全局唯一键没了，换成了 (user_id, source_file_hash)。
    assert bill_document_schema.unique_constraints() == {
        BILL_DOC_0015_CONSTRAINT: "UNIQUE (user_id, source_file_hash)"
    }
    # 另一个 owner 用同一个哈希：接受——修复前撞上全局约束、任务落 FAILED/INTERNAL 的就是这条路径。
    bill_document_schema.execute(
        "INSERT INTO statement_document (id, task_id, user_id, bank_code, source_file_hash,"
        " original_filename) SELECT gen_random_uuid(), t.id, t.user_id, 'CMB', 'hash_shared',"
        " 'b.csv' FROM bill_task t WHERE t.task_no = 'task_b'"
    )
    # 同一个 owner 再来一次同一个哈希：拒绝。
    with pytest.raises(IntegrityError, match=BILL_DOC_0015_CONSTRAINT):
        bill_document_schema.execute(
            "INSERT INTO statement_document (id, task_id, user_id, bank_code, source_file_hash,"
            " original_filename) SELECT gen_random_uuid(), t.id, t.user_id, 'CMB', 'hash_shared',"
            " 'b2.csv' FROM bill_task t WHERE t.task_no = 'task_b'"
        )
    # 被拒的两次没留下半行：只有回填的那一行和 task_b 成功的那一行。
    assert bill_document_schema.rows("SELECT count(*) FROM statement_document") == [(2,)]


# --- 两条不可回滚的迁移：`downgrade()` 必须响亮地拒绝，而不是悄悄什么都不做 ---------------

#: 两条 downgrade 的第一条语句就是 raise，所以不需要 alembic 上下文，也不需要数据库。
_IRREVERSIBLE_DOWNGRADES = (
    (
        "app.storage.migrations.versions.0014_bill_compatibility",
        "账单兼容迁移包含不可安全回退的数据类型变更，请从数据库备份恢复",
    ),
    (
        "app.storage.migrations.versions.0015_bill_doc_tenant_unique",
        "账单文档归属迁移包含不可安全回退的数据变更，请从数据库备份恢复",
    ),
)


@pytest.mark.parametrize(("module_name", "message"), _IRREVERSIBLE_DOWNGRADES)
def test_an_irreversible_migration_refuses_to_downgrade(module_name, message):
    """`downgrade()` 抛 RuntimeError，且消息与文件里那句一字不差。

    没有这条钉子，把 raise 换成 pass 的改动不会被任何测试发现 —— 而那种迁移一旦被
    真的回滚，数据已经不可逆地丢掉了。
    """

    module = importlib.import_module(module_name)

    with pytest.raises(RuntimeError) as raised:
        module.downgrade()

    assert str(raised.value) == message

