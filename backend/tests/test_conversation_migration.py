"""The migration bridge: what it binds, what it refuses to bind, and that it can be re-run.

The bridge exists to move existing `chat:{session_id}` threads into conversations without
copying checkpoints. Two properties make it safe to run against real data: a thread whose
owner cannot be proven is left for a human instead of guessed at, and running it twice does
not duplicate anything. Both are pinned here against the real database.
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.conversation.service import ConversationService
from app.storage.models import (
    AppUser,
    Conversation,
    LoginSession,
)
from scripts.migrate_conversations import (
    MigrationReport,
    ThreadReport,
    bind_thread,
    import_history,
    importable,
    owner_of,
)

OTHER_USER = "user_0002"


@pytest.fixture
def service(rt):
    """The ledger reader, for asserting what an import actually left behind."""

    return ConversationService(rt.database)


def legacy_thread(session_id: str) -> str:
    return f"chat:{session_id}"


def owner(database, session_id: str):
    return owner_of(database, session_id)


def test_a_known_session_resolves_to_its_user(rt, session):
    assert owner(rt.database, session["session_id"]) == session["user_id"]


def test_a_cleaned_up_session_has_no_owner(rt, session):
    """The session row is the only evidence of ownership, and a missing one is not a guess."""

    assert owner(rt.database, "sess_that_never_existed") is None


def test_a_thread_with_a_known_owner_is_bound_to_its_original_thread_id(rt, session):
    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)

    assert report.status == "bound"
    assert report.user_id == session["user_id"]
    assert report.conversation_id
    with rt.database.sessions() as db:
        stored = db.scalar(
            select(Conversation).where(Conversation.conversation_id == report.conversation_id)
        )
    # The whole point: the conversation points at the checkpoint that already exists, so no
    # graph state is copied and the existing history stays reachable.
    assert stored.thread_id == legacy_thread(session["session_id"])


def test_a_thread_with_no_provable_owner_is_left_for_a_human(rt, session):
    report = bind_thread(rt.database, legacy_thread("sess_gone"), dry_run=False)

    assert report.status == "unknown_owner"
    assert report.conversation_id is None
    with rt.database.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.thread_id == legacy_thread("sess_gone"))
            )
            == 0
        )


def test_a_dry_run_reports_without_writing(rt, session):
    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=True)

    assert report.status == "would_bind"
    with rt.database.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.thread_id == legacy_thread(session["session_id"]))
            )
            == 0
        )


def test_a_thread_that_is_not_a_legacy_session_thread_is_skipped(rt, session):
    report = bind_thread(rt.database, "conv_something_new", dry_run=False)

    assert report.status == "not_legacy"


def test_binding_twice_creates_one_conversation(rt, session):
    """Re-running is the normal case after a crash, so it must be idempotent."""

    thread = legacy_thread(session["session_id"])

    first = bind_thread(rt.database, thread, dry_run=False)
    second = bind_thread(rt.database, thread, dry_run=False)

    assert second.status == "already_bound"
    assert second.conversation_id == first.conversation_id
    with rt.database.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.thread_id == thread)
            )
            == 1
        )


def test_a_bound_thread_and_a_new_conversation_share_one_number_sequence(rt, session, service):
    """The bridge mints its number through the service's rule, so the two cannot collide."""

    bound = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)

    assert bound.status == "bound"
    with rt.database.sessions() as db:
        stored = db.scalar(
            select(Conversation).where(Conversation.conversation_id == bound.conversation_id)
        )
    assert stored.serial_no == 1, "the first conversation of this owner is number one"

    created = service.create(session["user_id"])
    assert created.serial_no == 2, "a conversation made by the service takes the next number"


def test_only_plainly_readable_messages_are_importable():
    """A tool call or a structured block cannot be reconstructed as a visible message."""

    class Block:
        type = "ai"
        content = [{"type": "text", "text": "结构化内容"}]

    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
        {"role": "assistant", "content": None},
        Block(),
        {"role": "tool", "content": "结果"},
    ]

    imported, dropped = importable(messages)

    assert imported == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    assert dropped == 3


def test_history_is_imported_in_order_with_no_run_and_marked_incomplete(rt, session, service):
    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)
    messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "tool", "content": "不可还原"},
    ]

    report = import_history(rt.database, report, messages, dry_run=False)

    assert report.imported_messages == 2
    assert report.skipped_messages == 1
    assert report.ledger_complete is False
    ledger = service.messages(session["user_id"], report.conversation_id)["items"]
    assert [item["content"] for item in ledger] == ["旧问题", "旧回答"]
    assert [item["sequence"] for item in ledger] == [1, 2]
    assert {item["run_id"] for item in ledger} == {None}
    assert all(item["metadata"]["migrated"] is True for item in ledger)
    assert all(item["metadata"]["ledger_complete"] is False for item in ledger)


def test_a_complete_import_is_marked_complete(rt, session, service):
    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)

    report = import_history(
        rt.database,
        report,
        [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "回答"}],
        dry_run=False,
    )

    assert report.ledger_complete is True
    ledger = service.messages(session["user_id"], report.conversation_id)["items"]
    assert all(item["metadata"]["ledger_complete"] is True for item in ledger)


def test_importing_twice_does_not_duplicate_history(rt, session, service):
    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)
    messages = [{"role": "user", "content": "旧问题"}]
    import_history(rt.database, report, messages, dry_run=False)

    again = import_history(rt.database, report, messages, dry_run=False)

    assert again.status == "already_imported"
    assert again.imported_messages == 0
    assert len(service.messages(session["user_id"], report.conversation_id)["items"]) == 1


def test_a_dry_run_import_writes_nothing(rt, session, service):
    report = ThreadReport(
        thread_id=legacy_thread(session["session_id"]),
        status="bound",
        conversation_id=service.create(session["user_id"]).conversation_id,
        user_id=session["user_id"],
    )

    report = import_history(
        rt.database, report, [{"role": "user", "content": "问题"}], dry_run=True
    )

    assert report.status == "would_import"
    assert report.imported_messages == 1
    assert service.messages(session["user_id"], report.conversation_id)["items"] == []


def test_a_thread_already_bound_to_another_user_is_reported_not_rebound(rt, session):
    """The one outcome worse than a manual list entry is handing over someone else's history."""

    thread = legacy_thread(session["session_id"])
    first = bind_thread(rt.database, thread, dry_run=False)
    # The session row now names a different user than the conversation it created.
    with rt.database.sessions.begin() as db:
        if not db.scalar(select(AppUser).where(AppUser.user_id == OTHER_USER)):
            db.add(AppUser(user_id=OTHER_USER))
            db.flush()
        row = db.scalar(
            select(LoginSession).where(LoginSession.session_id == session["session_id"])
        )
        row.user_id = OTHER_USER

    report = bind_thread(rt.database, thread, dry_run=False)

    assert report.status == "owner_conflict"
    assert report.conversation_id == first.conversation_id
    with rt.database.sessions() as db:
        stored = db.scalar(
            select(Conversation).where(Conversation.conversation_id == first.conversation_id)
        )
    assert stored.user_id == session["user_id"], "the original owner keeps the conversation"


def test_an_interrupted_import_can_be_resumed(rt, session, service):
    """Half-imported history must not stop the next run from finishing the job."""

    report = bind_thread(rt.database, legacy_thread(session["session_id"]), dry_run=False)
    # Simulate a run that died after binding but before importing anything: the next run
    # finds the conversation already bound and simply imports.
    assert report.status == "bound"
    assert service.messages(session["user_id"], report.conversation_id)["items"] == []

    resumed = bind_thread(rt.database, report.thread_id, dry_run=False)
    resumed = import_history(
        rt.database,
        resumed,
        [{"role": "user", "content": "问题"}],
        dry_run=False,
    )

    assert resumed.status == "imported"
    assert len(service.messages(session["user_id"], resumed.conversation_id)["items"]) == 1


def test_the_report_serializes(rt, session):
    """The report is a deliverable: an operator has to be able to read it and file it."""

    import json
    from dataclasses import asdict

    report = MigrationReport(dry_run=True)
    report.threads_seen = 2
    report.threads = [
        ThreadReport(thread_id="chat:a", status="unknown_owner", note="无法确认归属"),
        ThreadReport(thread_id="chat:b", status="would_bind", user_id="user_0001"),
    ]

    payload = json.loads(
        json.dumps(
            {
                "dry_run": report.dry_run,
                "threads_seen": report.threads_seen,
                "summary": report.summary(),
                "threads": [asdict(item) for item in report.threads],
            },
            ensure_ascii=False,
        )
    )

    assert payload["summary"] == {"unknown_owner": 1, "would_bind": 1}
    assert payload["threads"][0]["note"] == "无法确认归属"


def test_listing_threads_reads_the_checkpoint_store(rt, session, schema_url):
    """The inventory asks the checkpointer's own tables, and writes nothing anywhere."""

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from scripts.migrate_conversations import list_thread_ids

    async def seed_one():
        async with AsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
            graph = _tiny_graph(saver)
            await graph.ainvoke(
                {"messages": [{"role": "user", "content": "旧消息"}]},
                {"configurable": {"thread_id": legacy_thread(session["session_id"])}},
            )

    def _tiny_graph(saver):
        from langgraph.graph import END, START, StateGraph

        from app.agents.state import AgentState

        graph = StateGraph(AgentState)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        graph.add_edge("noop", END)
        return graph.compile(checkpointer=saver)

    asyncio.run(seed_one())

    # The DSN travels whole: the schema lives in its `options=-csearch_path=` parameter, and
    # dropping the query string would silently point the inventory at `public`.
    found = asyncio.run(list_thread_ids(schema_url))

    assert legacy_thread(session["session_id"]) in found


@pytest.mark.parametrize("thread_id", ["chat:", "conv_x", ""])
def test_a_thread_that_cannot_name_a_session_is_never_bound(rt, thread_id):
    report = bind_thread(rt.database, thread_id, dry_run=False)

    assert report.status in ("not_legacy", "unknown_owner")
    assert report.conversation_id is None


def test_the_tool_refuses_a_database_the_revision_has_not_reached():
    """Without this the first write fails inside SQLAlchemy, which reads as a tool bug.

    A real run against an unmigrated database reported `relation "conversation" does not
    exist` with a driver traceback; the operator needs to be told which migration to apply.
    The check runs against a genuinely empty schema, because that is the state it guards.
    """

    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine, text

    from scripts.migrate_conversations import require_conversation_schema
    from tests.conftest import resolve_test_database_url

    url = resolve_test_database_url()
    if not url:
        pytest.skip("Provide database.txt or set TEST_DATABASE_URL")
    schema = "test_conv_unmigrated_" + uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"}
    )
    try:
        with pytest.raises(SystemExit) as raised:
            require_conversation_schema(SimpleNamespace(engine=engine))

        assert "alembic upgrade head" in str(raised.value)
        for table in ("conversation", "conversation_message", "conversation_submission"):
            assert table in str(raised.value)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_the_tool_accepts_a_migrated_database(rt):
    from scripts.migrate_conversations import require_conversation_schema

    require_conversation_schema(rt.database)
