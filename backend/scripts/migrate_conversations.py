"""Inventory and backfill of the checkpoints the pre-conversation runtime created.

Two things happen here, and the order matters.

**Inventory is read-only.** Every `chat:{session_id}` thread that exists in the checkpoint
store is enumerated, and the owner of each one is resolved through the login session that
named it. A thread whose session row is gone has no owner this tool can prove, and it is
reported for a human to place rather than guessed at -- binding a conversation to the wrong
user would hand them somebody else's history.

**Backfill only binds what it can prove.** A thread with a known owner gets one conversation
that keeps the original `thread_id`, so the checkpoint stays reachable with no copying. Its
user/assistant messages are imported from the checkpoint's own transcript, and only those
that are reliably readable: a message whose content is not plain text (a tool call, a
structured block) is not imported, and the conversation is marked as having an incomplete
ledger rather than being presented as complete.

Re-running is safe: a conversation that already exists is not created twice, and a
conversation that already holds imported messages is not imported into again. A run that
dies part way can simply be started again.

    python scripts/migrate_conversations.py --dry-run
    python scripts/migrate_conversations.py --report migration-report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.agents.state import role_and_text  # noqa: E402
from app.conversation.service import next_serial_no  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.eventloop import configure_event_loop  # noqa: E402
from app.core.types import new_id, utcnow  # noqa: E402
from app.storage.checkpointer import checkpointer_url  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.models import (  # noqa: E402
    Conversation,
    ConversationMessage,
    LoginSession,
)

LEGACY_PREFIX = "chat:"
# The imported rows carry where they came from, so a reader can tell a recovered history from
# one that was written by this application.
IMPORT_METADATA = {"migrated": True, "source": "checkpoint"}


@dataclass
class ThreadReport:
    """One thread's outcome, whether it was bound, skipped or left for a human."""

    thread_id: str
    status: str
    conversation_id: str | None = None
    user_id: str | None = None
    imported_messages: int = 0
    skipped_messages: int = 0
    ledger_complete: bool = True
    note: str = ""


@dataclass
class MigrationReport:
    dry_run: bool
    threads_seen: int = 0
    threads: list[ThreadReport] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.threads:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts


async def list_thread_ids(database_url: str) -> list[str]:
    """Every thread the checkpoint store knows about, newest activity first.

    Read-only: this asks the checkpointer's own tables, and touches nothing in the
    application schema.
    """

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(
        checkpointer_url(database_url), autocommit=True, row_factory=dict_row
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT thread_id, max(checkpoint_id) AS newest
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY newest DESC
            """
        )
        rows = await cursor.fetchall()
    return [row["thread_id"] for row in rows]


async def checkpoint_messages(database_url: str, thread_id: str) -> list:
    """The newest checkpoint's transcript for one thread, or an empty list."""

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(checkpointer_url(database_url)) as saver:
        async for item in saver.alist({"configurable": {"thread_id": thread_id}}, limit=1):
            values = item.checkpoint.get("channel_values") or {}
            return list(values.get("messages") or [])
    return []


def owner_of(database, session_id: str) -> str | None:
    """The user a legacy session belonged to, or None when that cannot be established."""

    with database.sessions() as db:
        return db.scalar(select(LoginSession.user_id).where(LoginSession.session_id == session_id))


def importable(messages: list) -> tuple[list[dict[str, str]], int]:
    """The visible messages this import can stand behind, and how many it had to drop.

    A message is imported only when its role and its text are both plainly readable. Anything
    else -- a tool call, a structured content block, a message with no text -- is counted and
    left out, and its presence is what marks the ledger incomplete.
    """

    imported: list[dict[str, str]] = []
    dropped = 0
    for message in messages:
        role, text = role_and_text(message)
        if role in ("user", "assistant") and text is not None:
            imported.append({"role": role, "content": text})
        else:
            dropped += 1
    return imported, dropped


def bind_thread(database, thread_id: str, *, dry_run: bool) -> ThreadReport:
    """Create the conversation for one legacy thread, or report why it was not created."""

    if not thread_id.startswith(LEGACY_PREFIX):
        return ThreadReport(thread_id=thread_id, status="not_legacy", note="不是旧会话线程")

    session_id = thread_id[len(LEGACY_PREFIX) :]
    user_id = owner_of(database, session_id)
    if not user_id:
        # No session row, so no provable owner. Guessing would hand one user another's
        # history, which is worse than leaving the thread in the manual list.
        return ThreadReport(
            thread_id=thread_id, status="unknown_owner", note="登录会话已清理，无法确认归属"
        )

    with database.sessions() as db:
        existing = db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))
    if existing is not None:
        if existing.user_id != user_id:
            return ThreadReport(
                thread_id=thread_id,
                status="owner_conflict",
                conversation_id=existing.conversation_id,
                user_id=user_id,
                note="该线程已绑定到另一个用户",
            )
        return ThreadReport(
            thread_id=thread_id,
            status="already_bound",
            conversation_id=existing.conversation_id,
            user_id=user_id,
        )

    if dry_run:
        return ThreadReport(thread_id=thread_id, status="would_bind", user_id=user_id)

    now = utcnow()
    conversation_id = new_id("conv")
    with database.sessions.begin() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id,
                serial_no=next_serial_no(db, user_id),
                user_id=user_id,
                title=f"迁移会话 {session_id[:8]}",
                pinned=False,
                archived_at=None,
                deleted_at=None,
                created_at=now,
                updated_at=now,
                last_message_at=None,
                next_message_sequence=1,
                # The original thread id, not a derived one: the whole point is to reach the
                # checkpoint that already exists instead of starting a new history.
                thread_id=thread_id,
            )
        )
    return ThreadReport(
        thread_id=thread_id,
        status="bound",
        conversation_id=conversation_id,
        user_id=user_id,
    )


def import_history(
    database, report: ThreadReport, messages: list, *, dry_run: bool
) -> ThreadReport:
    """Append the readable messages of one thread to its conversation, in order."""

    if report.conversation_id is None or not messages:
        return report
    with database.sessions() as db:
        already = db.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == report.conversation_id)
        )
    if already:
        # Re-running must not duplicate history, and it must not renumber it either. The
        # counters describe *this* run, so they are cleared rather than left holding the
        # previous run's totals: a report claiming "imported 2" for a run that imported
        # nothing is a report an operator cannot act on.
        report.status = "already_imported"
        report.imported_messages = 0
        report.skipped_messages = 0
        report.note = "该会话已有消息，未重复导入"
        return report

    imported, dropped = importable(messages)
    report.imported_messages = len(imported)
    report.skipped_messages = dropped
    report.ledger_complete = dropped == 0
    if dry_run:
        report.status = "would_import"
        return report

    now = utcnow()
    with database.sessions.begin() as db:
        conversation = db.scalar(
            select(Conversation)
            .where(Conversation.conversation_id == report.conversation_id)
            .with_for_update()
        )
        sequence = int(conversation.next_message_sequence)
        for item in imported:
            db.add(
                ConversationMessage(
                    message_id=new_id("msg"),
                    conversation_id=report.conversation_id,
                    # The run a historical message belonged to cannot be recovered from a
                    # checkpoint, and inventing one would make an audit trail that lies.
                    run_id=None,
                    sequence=sequence,
                    role=item["role"],
                    content=item["content"],
                    message_kind="message",
                    question_id=None,
                    created_at=now,
                    meta={**IMPORT_METADATA, "ledger_complete": dropped == 0},
                )
            )
            sequence += 1
        conversation.next_message_sequence = sequence
        if imported:
            conversation.last_message_at = now
        conversation.updated_at = now
    report.status = "imported"
    if dropped:
        report.note = f"{dropped} 条消息无法可靠还原，账本标记为不完整"
    return report


def require_conversation_schema(database) -> None:
    """Refuse to run against a database this revision has not been applied to.

    Without this the first write fails inside SQLAlchemy with `relation "conversation" does
    not exist`, which reads like a bug in the tool rather than a missing migration. The
    bridge creates conversations, so it needs the tables that hold them.
    """

    from sqlalchemy import inspect

    existing = set(inspect(database.engine).get_table_names())
    missing = sorted({"conversation", "conversation_message", "conversation_submission"} - existing)
    if missing:
        raise SystemExit(
            "数据库尚未升级到会话持久化版本，缺少表："
            + ", ".join(missing)
            + "\n请先在 backend/ 执行：uv run alembic upgrade head"
        )


async def run(database_url: str, *, dry_run: bool, limit: int | None) -> MigrationReport:
    database = Database(database_url)
    report = MigrationReport(dry_run=dry_run)
    try:
        require_conversation_schema(database)
        threads = await list_thread_ids(database_url)
        if limit is not None:
            threads = threads[:limit]
        report.threads_seen = len(threads)
        for thread_id in threads:
            item = bind_thread(database, thread_id, dry_run=dry_run)
            if item.status in ("bound", "would_bind", "already_bound"):
                messages = await checkpoint_messages(database_url, thread_id)
                item = import_history(database, item, messages, dry_run=dry_run)
            report.threads.append(item)
    finally:
        database.dispose()
    return report


def main() -> int:
    # Before any loop exists: psycopg's async connections refuse Windows' default Proactor
    # loop, and `asyncio.run` below would otherwise build one. The application installs this
    # in `create_app`, and pytest in its own `conftest` -- a script is a third entry point and
    # has to do it itself, or the first connection raises rather than the script failing
    # somewhere it can explain.
    configure_event_loop()

    parser = argparse.ArgumentParser(description="盘点并回填旧 chat:{session_id} 线程")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写任何数据")
    parser.add_argument("--limit", type=int, default=None, help="只处理最新的 N 个线程")
    parser.add_argument("--report", type=Path, default=None, help="把完整报告写到这个 JSON 文件")
    parser.add_argument("--database-url", default=None, help="覆盖配置里的数据库地址")
    args = parser.parse_args()

    url = args.database_url or get_settings().database_url
    report = asyncio.run(run(url, dry_run=args.dry_run, limit=args.limit))

    for item in report.threads:
        line = f"{item.status:>16}  {item.thread_id}"
        if item.imported_messages or item.skipped_messages:
            line += f"  导入 {item.imported_messages} / 跳过 {item.skipped_messages}"
        if item.note:
            line += f"  ({item.note})"
        print(line)
    print(
        f"\n共 {report.threads_seen} 个线程："
        + ", ".join(f"{name}={count}" for name, count in sorted(report.summary().items()))
    )
    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "dry_run": report.dry_run,
                    "threads_seen": report.threads_seen,
                    "summary": report.summary(),
                    "threads": [asdict(item) for item in report.threads],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
