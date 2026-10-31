"""0017：日累计塌缩成一行一个 (用户, 日期)。

0016 把一天的钱按 `operation_kind` 拆成多行，但读侧本来就跨类型求和，类型从不改变任何
判定，只是把同一笔钱摊在几行里。0017 先把同一 (用户, 日期) 的重复行合并（金额相加），再
删掉这一列，最后把唯一约束换成 `uq_daily_op_user_date`。

本文件照 0016 迁移测试的样式，在真实 PostgreSQL 的隔离 schema 里走一遍
upgrade → 断言 → downgrade → 断言。
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from tests.conftest import require_test_database, resolve_test_database_url

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "storage"
    / "migrations"
    / "versions"
    / "0017_daily_total_single_row.py"
)

DAY = "2026-10-22"


def _load_migration_module():
    assert MIGRATION_PATH.exists(), f"Migration file not found at {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0017", str(MIGRATION_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _columns(conn, schema_name: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = 'daily_operation_total'"
        ),
        {"schema": schema_name},
    ).fetchall()
    return {row[0] for row in rows}


def _unique_constraints(conn) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'daily_operation_total'::regclass AND contype = 'u'"
        )
    ).fetchall()
    return {row[0] for row in rows}


def test_migration_file_exists():
    assert MIGRATION_PATH.exists()


def test_migration_revision_metadata():
    mod = _load_migration_module()
    assert mod.revision == "0017_daily_total_single_row"
    assert mod.down_revision == "0016_daily_operation_total"
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

    schema_name = "test_runtime_daily_total_single_" + uuid.uuid4().hex
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


def test_0017_merges_the_kinds_into_one_row_per_day(migration_test_schema):
    """升级合并重复行并删列；降级把列与旧约束原样还回来。"""
    schema_name, engine = migration_test_schema
    config = Config("alembic.ini")

    # 1. 先到 0016：一天的钱按类型分行，正是要塌缩掉的样子。
    command.upgrade(config, "0016_daily_operation_total")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO daily_operation_total "
                "(user_id, business_date, operation_kind, total_minor) VALUES "
                "('user-1', :day, 'transfer', 60000), "
                "('user-1', :day, 'birthday_order', 15000), "
                "('user-2', :day, 'transfer', 7000)"
            ),
            {"day": DAY},
        )

    # 2. 升级到 0017：列没了，重复行合成一行，金额相加。
    command.upgrade(config, "0017_daily_total_single_row")

    with engine.begin() as conn:
        assert "operation_kind" not in _columns(conn, schema_name)
        rows = conn.execute(
            text("SELECT user_id, total_minor FROM daily_operation_total ORDER BY user_id")
        ).fetchall()
        assert rows == [("user-1", 75000), ("user-2", 7000)]

        # 3. 唯一约束换成了 (user_id, business_date)。
        assert "uq_daily_op_user_date" in _unique_constraints(conn)
        assert "uq_daily_op_user_date_kind" not in _unique_constraints(conn)

        # 4. 同一个 (用户, 日期) 再插一行直接撞约束。
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO daily_operation_total (user_id, business_date, total_minor) "
                        "VALUES ('user-1', :day, 1)"
                    ),
                    {"day": DAY},
                )

    # 5. 降级回 0016：列与旧约束都回来，合并后的金额不能因为回滚就从日累计里消失
    #    （回填成 transfer —— 它在 FUND_WRITE_OPERATIONS 里，读侧仍算得到这笔钱）。
    command.downgrade(config, "0016_daily_operation_total")
    with engine.begin() as conn:
        assert "operation_kind" in _columns(conn, schema_name)
        assert "uq_daily_op_user_date_kind" in _unique_constraints(conn)
        assert "uq_daily_op_user_date" not in _unique_constraints(conn)
        row = conn.execute(
            text(
                "SELECT operation_kind, total_minor FROM daily_operation_total "
                "WHERE user_id = 'user-1'"
            )
        ).fetchone()
        assert row == ("transfer", 75000)
