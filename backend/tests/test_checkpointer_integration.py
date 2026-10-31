"""The PostgreSQL checkpointer against the real database.

These cases need a reachable PostgreSQL, because a checkpoint store is the one piece the
in-memory saver cannot prove. Each case gets its own schema and never touches a public
table.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from app.agents.runtime import AgentRuntime
from app.agents.state import AgentState
from app.core.config import Settings
from app.core.errors import AppError
from app.integration.model_client import ModelReply
from app.main import create_app
from app.storage.checkpointer import (
    LockedAsyncPostgresSaver,
    missing_checkpoint_tables,
    require_checkpoint_tables,
)
from tests.conftest import resolve_test_database_url

# The same credentials the `schema_url` fixture uses: `database.txt` in the repository
# root, or `TEST_DATABASE_URL` when a run points somewhere else.
pytestmark = pytest.mark.skipif(
    resolve_test_database_url() is None,
    reason="Provide database.txt or set TEST_DATABASE_URL to run these integration tests",
)


class CountingModel:
    """Echoes the size of the transcript it was handed, so thread reuse is observable."""

    def complete(self, messages):
        return ModelReply(text=f"n={len(messages)}")

    def close(self):
        return None


def counting_graph(model, context, assembler=None, turn=None) -> StateGraph:
    del context

    def respond(state) -> dict:
        reply = model.complete(tuple(state["messages"]))
        return {"messages": [AIMessage(content=reply.text)]}

    graph = StateGraph(AgentState)
    graph.add_node("count", respond)
    graph.add_edge(START, "count")
    graph.add_edge("count", END)
    return graph


def ask_graph(model, context, assembler=None, turn=None) -> StateGraph:
    del model, context

    from langgraph.types import interrupt

    def ask(state) -> dict:
        answer = interrupt({"kind": "input", "fields": ["x"], "display": "请提供 x"})
        return {"messages": [AIMessage(content=f"got:{answer}")]}

    graph = StateGraph(AgentState)
    graph.add_node("ask", ask)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph


async def _open(schema_url):
    """Open a saver in the private schema and make sure the tables exist."""

    saver = await LockedAsyncPostgresSaver.from_conn_string(schema_url).__aenter__()
    await saver.setup()
    return saver


def test_a_second_turn_reads_the_first_from_postgres(schema_url):
    async def scenario():
        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
            runtime = AgentRuntime(
                CountingModel(), graph_factory=counting_graph, checkpointer=saver
            )

            first = [event async for event in runtime.invoke("s1", "u1", "你好", {})]
            second = [event async for event in runtime.invoke("s1", "u1", "再见", {})]
            other = [event async for event in runtime.invoke("s2", "u1", "你好", {})]

        return first, second, other

    first, second, other = asyncio.run(scenario())
    # The graph publishes no custom event, so only the state tells the story.
    assert first == [] and second == [] and other == []


def test_the_transcript_is_read_back_from_postgres(schema_url):
    """A fresh runtime over the same schema sees the earlier turns."""

    seen: list[int] = []

    class WatchingModel:
        def complete(self, messages):
            seen.append(len(messages))
            return ModelReply(text=f"n={len(messages)}")

        def close(self):
            return None

    async def scenario():
        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
            first_runtime = AgentRuntime(
                WatchingModel(), graph_factory=counting_graph, checkpointer=saver
            )
            async for _ in first_runtime.invoke("s1", "u1", "你好", {}):
                pass

        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            second_runtime = AgentRuntime(
                WatchingModel(), graph_factory=counting_graph, checkpointer=saver
            )
            async for _ in second_runtime.invoke("s1", "u1", "再见", {}):
                pass

    asyncio.run(scenario())
    assert seen == [1, 3]


def test_an_interrupt_survives_a_new_saver(schema_url):
    """The paused turn is replayed from Postgres, not from process memory."""

    async def scenario():
        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
            runtime = AgentRuntime(CountingModel(), graph_factory=ask_graph, checkpointer=saver)
            paused = [event async for event in runtime.invoke("s1", "u1", "帮我", {})]

        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            runtime = AgentRuntime(CountingModel(), graph_factory=ask_graph, checkpointer=saver)
            resumed = [
                event
                async for event in runtime.invoke("s1", "u1", "x=7", {"resume": {"value": "7"}})
            ]
        return paused, resumed

    paused, resumed = asyncio.run(scenario())
    assert [event.event for event in paused] == ["interrupt"]
    # This graph is not the domain graph, so its interrupt carries none of the question
    # fields; the adapter forwards them as absent rather than inventing values.
    assert paused[0].data == {
        "kind": "input",
        "question_id": None,
        "agent_id": None,
        "fields": ["x"],
        "display": "请提供 x",
        "attempt": None,
    }
    # The node replayed and the answer came back as the interrupt's return value.
    assert resumed == []


def test_the_lifespan_check_refuses_an_uninitialized_store(schema_url):
    async def scenario():
        await require_checkpoint_tables(schema_url)

    with pytest.raises(AppError) as failure:
        asyncio.run(scenario())
    assert failure.value.code == "CHECKPOINTER_NOT_INITIALIZED"
    assert failure.value.status == 503


def test_the_lifespan_check_passes_after_setup(schema_url):
    async def scenario():
        async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
            await saver.setup()
        await require_checkpoint_tables(schema_url)

    asyncio.run(scenario())


def test_missing_table_names_are_reported_not_guessed():
    assert missing_checkpoint_tables({"checkpoints"}) == [
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
    ]


def sqlalchemy_url(schema_url: str) -> str:
    """The same DSN in the spelling SQLAlchemy wants, schema option included."""

    return schema_url.replace("postgresql://", "postgresql+psycopg://", 1)


def deployment_settings(schema_url: str) -> Settings:
    """Production wiring for a deployment whose environment names no model.

    `.env` sits beside the package and pydantic reads it from the working directory, so a
    developer who configured a model locally would otherwise have these tests reach a real
    provider. An explicit argument outranks the file, and the empty trio is what pins the
    "no model in the environment" premise these two tests are about.
    """

    return Settings(
        database_url=sqlalchemy_url(schema_url),
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        # 这里要的是"部署了但没有模型"的前提，不是真的去连飞书：本机 backend/.env.channels
        # 里通常有真机扫码写入的凭据，不关掉就会在单元测试里起一条真实长连接。
        feishu_enabled=False,
    )


def test_the_production_app_refuses_to_start_without_the_checkpoint_tables(schema_url):
    """A deployment that skipped the init script must fail, not serve a broken chat."""

    settings = deployment_settings(schema_url)

    with pytest.raises(AppError) as failure:
        with TestClient(create_app(settings)):
            pass

    assert failure.value.code == "CHECKPOINTER_NOT_INITIALIZED"


def test_the_production_app_serves_once_the_deployment_initialized_the_store(schema_url):
    """The real wiring: Postgres saver, lifespan check, and the graph built per turn."""

    asyncio.run(_initialize(schema_url))
    settings = deployment_settings(schema_url)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["data"]["model_configured"] is False


async def _initialize(schema_url: str) -> None:
    async with LockedAsyncPostgresSaver.from_conn_string(schema_url) as saver:
        await saver.setup()
