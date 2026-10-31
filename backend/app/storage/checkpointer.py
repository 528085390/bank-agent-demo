"""LangGraph checkpoint storage.

`langgraph-checkpoint-postgres` owns its own tables; this module only checks that the
deployment ran the setup command. FastAPI's lifespan connects and verifies -- it never
calls `setup()`, because a running application must not migrate its own storage.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.errors import AppError

# The tables `setup()` creates. Presence is checked before the app accepts traffic.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


class LockedAsyncPostgresSaver(AsyncPostgresSaver):
    """An `AsyncPostgresSaver` usable from more than one task.

    LangGraph serializes checkpoint writes with an `asyncio.Lock` created while the
    saver is being constructed. On Windows that is the Proactor loop, while the
    application runs on a Selector loop, and the first write then raises "is bound to
    a different event loop". The lock is per-saver, so re-creating it on the running
    loop removes the cross-loop binding without touching LangGraph internals.
    """

    def _rebind_loop_lock(self) -> None:
        lock = getattr(self, "lock", None)
        bind = getattr(lock, "_get_loop", None)
        if bind is None:
            return
        try:
            bind()
        except RuntimeError:
            self.lock = asyncio.Lock()


def checkpointer_url(database_url: str) -> str:
    """psycopg wants a libpq DSN; SQLAlchemy's dialect prefix is stripped."""

    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url[len("postgresql+psycopg://") :]
    if database_url.startswith("postgresql+"):
        raise AppError(
            "CHECKPOINTER_DRIVER_UNSUPPORTED",
            "LangGraph checkpointer 只支持 psycopg 驱动",
            500,
        )
    return database_url


@asynccontextmanager
async def postgres_checkpointer(database_url: str):
    """Open the shared async saver. The caller owns the graph lifetime, not the tables.

    It opens the loop-rebinding subclass, not the base saver: `from_conn_string` builds
    an instance of the class it is called on, so asking the base class here would hand
    the application a saver whose lock is still bound to the loop it was built on.
    """

    async with LockedAsyncPostgresSaver.from_conn_string(checkpointer_url(database_url)) as saver:
        saver._rebind_loop_lock()
        yield saver


def missing_checkpoint_tables(existing: set[str]) -> list[str]:
    return sorted(set(CHECKPOINT_TABLES) - set(existing))


async def require_checkpoint_tables(database_url: str) -> None:
    """Refuse to start when the deployment never initialized the checkpointer."""

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(
        checkpointer_url(database_url), autocommit=True, row_factory=dict_row
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema()
            """
        )
        rows = await cursor.fetchall()
    missing = missing_checkpoint_tables({row["table_name"] for row in rows})
    if missing:
        raise AppError(
            "CHECKPOINTER_NOT_INITIALIZED",
            "checkpoint 表尚未初始化，请先运行部署脚本",
            503,
            details={"missing_tables": missing},
        )
