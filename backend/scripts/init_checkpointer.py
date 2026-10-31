"""Initialize the LangGraph checkpoint tables.

This is the only place `setup()` is called. The application never migrates its own
storage: it connects at startup and refuses to serve if these tables are missing.

    cd backend && uv run python scripts/init_checkpointer.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.eventloop import configure_event_loop  # noqa: E402
from app.storage.checkpointer import (  # noqa: E402
    CHECKPOINT_TABLES,
    LockedAsyncPostgresSaver,
    checkpointer_url,
    missing_checkpoint_tables,
)

# psycopg's async connections refuse Windows' default Proactor loop, and this script opens
# one before any other code runs, so the policy has to be installed first -- exactly as
# uvicorn and pytest do it. Without this the command fails on Windows.
configure_event_loop()


async def _table_names() -> set[str]:
    import psycopg
    from psycopg.rows import dict_row

    dsn = checkpointer_url(get_settings().database_url)
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, row_factory=dict_row
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema()
            """
        )
        return {row["table_name"] for row in await cursor.fetchall()}


async def initialize() -> list[str]:
    """Create the checkpoint tables, then report which ones are still missing."""

    dsn = checkpointer_url(get_settings().database_url)
    async with LockedAsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    return missing_checkpoint_tables(await _table_names())


def main() -> int:
    missing = asyncio.run(initialize())
    if missing:
        print("checkpoint 初始化失败，缺失表：" + ", ".join(missing))
        return 1
    print("checkpoint 表已就绪：" + ", ".join(sorted(CHECKPOINT_TABLES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
