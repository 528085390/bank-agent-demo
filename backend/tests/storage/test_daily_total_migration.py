from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from tests.conftest import require_test_database, resolve_test_database_url

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "storage"
    / "migrations"
    / "versions"
    / "0016_daily_operation_total.py"
)


def _load_migration_module():
    assert MIGRATION_PATH.exists(), f"Migration file not found at {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0016", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_file_exists():
    assert MIGRATION_PATH.exists()


def test_migration_revision_metadata():
    mod = _load_migration_module()
    assert mod.revision == "0016_daily_operation_total"
    assert mod.down_revision == "0015_bill_doc_tenant_unique"
    assert getattr(mod, "branch_labels", None) is None
    assert getattr(mod, "depends_on", None) is None


def test_migration_callables():
    mod = _load_migration_module()
    assert callable(getattr(mod, "upgrade", None))
    assert callable(getattr(mod, "downgrade", None))


@pytest.fixture
def migration_test_schema(monkeypatch):
    """创建隔离的 PostgreSQL 专属 Schema，真实执行 DDL 往返验证。"""
    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL to run PostgreSQL tests")
    require_test_database(url)

    schema_name = "test_runtime_daily_total_" + uuid.uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema_name}")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    separator = "&" if "?" in url else "?"
    schema_url = f"{url}{separator}options=-csearch_path%3D{schema_name}"
    engine = create_engine(schema_url)

    yield schema_name, engine

    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    admin.dispose()
    engine.dispose()


def test_0016_migration_real_ddl_round_trip(migration_test_schema):
    """真正执行 Alembic upgrade 与 downgrade，确保 DDL 产生真实物理表和约束，回滚无残留。"""
    schema_name, engine = migration_test_schema
    config = Config("alembic.ini")

    # 1. 升级到 0015
    command.upgrade(config, "0015_bill_doc_tenant_unique")

    # 2. 升级到 0016
    command.upgrade(config, "0016_daily_operation_total")

    # 3. 验证表和字段真实存在
    with engine.begin() as conn:
        columns = conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'daily_operation_total' "
                "ORDER BY ordinal_position"
            ),
            {"schema": schema_name},
        ).fetchall()
        col_names = [r[0] for r in columns]
        assert "user_id" in col_names
        assert "business_date" in col_names
        assert "operation_kind" in col_names
        assert "total_minor" in col_names
        assert "created_at" in col_names
        assert "updated_at" in col_names

        # 验证唯一约束
        constraints = conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'daily_operation_total'::regclass AND contype = 'u'"
            )
        ).fetchall()
        c_names = [r[0] for r in constraints]
        assert "uq_daily_op_user_date_kind" in c_names

        # 真实写入一条日累计记录
        conn.execute(
            text(
                "INSERT INTO daily_operation_total (user_id, business_date, operation_kind, total_minor) "
                "VALUES ('user-1', '2026-10-22', 'transfer', 60000)"
            )
        )
        row = conn.execute(
            text("SELECT total_minor FROM daily_operation_total WHERE user_id = 'user-1'")
        ).fetchone()
        assert row[0] == 60000

    # 4. 执行 downgrade 回退到 0015
    command.downgrade(config, "0015_bill_doc_tenant_unique")

    # 5. 验证表彻底消失
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = 'daily_operation_total'"
            ),
            {"schema": schema_name},
        ).fetchall()
        assert len(tables) == 0

    # 6. 再次升级到 0016 确保往返幂等
    command.upgrade(config, "0016_daily_operation_total")
    with engine.begin() as conn:
        tables_again = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = 'daily_operation_total'"
            ),
            {"schema": schema_name},
        ).fetchall()
        assert len(tables_again) == 1
