"""Apply the card-only additive migration without overwriting another module's history."""

from importlib import import_module
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def migrate_cards(engine):
    config = Config(str(ROOT / "alembic.ini"))
    revisions = {item.revision for item in ScriptDirectory.from_config(config).walk_revisions()}
    with engine.begin() as connection:
        schema = inspect(connection)
        tables = set(schema.get_table_names())
        if "cards" not in tables:
            raise RuntimeError("银行基础表不存在，请先执行 alembic upgrade head。")
        existing = (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        )
        if any(revision not in revisions for revision in existing):
            column_exists = "freeze_reason" in {
                item["name"] for item in schema.get_columns("cards")
            }
            table_exists = "card_actions" in tables
            if column_exists != table_exists:
                raise RuntimeError("发现不完整的卡片迁移，请先检查现有结构；未继续修改。")
            if not table_exists:
                migration = import_module("app.storage.migrations.versions.0002_card_actions")
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
            password_fields = {
                "transaction_password_hash",
                "password_version",
                "password_failures",
                "password_code_failures",
                "password_locked_until",
            }
            present = password_fields & {
                item["name"] for item in inspect(connection).get_columns("cards")
            }
            if present and present != password_fields:
                raise RuntimeError("发现不完整的交易密码迁移；未改写外部迁移历史。")
            if not present:
                migration = import_module(
                    "app.storage.migrations.versions.0004_card_transaction_password"
                )
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
            return "卡片结构已就绪；保留外部模块迁移编号，未改写迁移历史。"
    command.upgrade(config, "head")
    return "卡片与理财结构已迁移到 0004_card_transaction_password。"


def main():
    engine = create_engine(get_settings().database_url)
    try:
        print(migrate_cards(engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
