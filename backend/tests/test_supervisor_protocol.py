"""The Supervisor's one decision path.

The interesting cases are all on the edges: a turn that hands off, a turn that answers, and a
queue that must not cost a second call.
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import DOMAIN_ROSTER, build_agent_graph
from tests.support.domain_harness import generic_roster
from app.agents.runtime import AgentRuntime
from app.agents.state import new_agent_state
from app.core.errors import AppError
from app.integration.model_client import ModelClientError
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import StreamingModel

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1")
SESSION, USER = "s_1", "u_1"

HANDOFF_WEALTH = '{"agent_id":"wealth","instruction":"查一下持仓"}'
HANDOFF_WEALTH_AGAIN = '{"agent_id":"wealth","instruction":"看看收益"}'


def compiled(model, *, roster=None):
    gateway = ToolGateway(ToolCatalog.build([], {}), RecordingAdapter())
    graph = build_agent_graph(model, gateway, CONTEXT, roster=roster or generic_roster())
    return graph.compile(checkpointer=InMemorySaver())


def collect(graph, message="帮我看看持仓"):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke(SESSION, USER, message, {})]

    return asyncio.run(run())


def final_text(events) -> str | None:
    for event in events:
        if event.event == "final":
            return event.data["text"]
    return None


def test_a_tool_calling_turn_streams_with_the_handoff_tool():
    # Two streamed turns under the one-switch protocol: the Supervisor dispatches and then
    # answers the user directly. Wealth runs with an empty menu here, so the tool-less
    # template concludes on its own and spends no model call -- the domain turn is free.
    model = StreamingModel(
        [[("call", "handoff", HANDOFF_WEALTH)], ["持仓查好了。"]],
    )

    events = collect(compiled(model))

    assert model.tools[0][0]["function"]["name"] == "handoff"
    assert final_text(events) == "持仓查好了。"
    assert len(model.prompts) == 2, "supervisor dispatch, supervisor answer"
    assert len(model.completed) == 0


def test_a_direct_answer_needs_no_handoff():
    model = StreamingModel([["你好，有什么可以帮你的？"]])

    events = collect(compiled(model), message="你好")

    assert final_text(events) == "你好，有什么可以帮你的？"
    assert model.tools[0][0]["function"]["name"] == "handoff"


def test_a_second_handoff_is_dispatched_without_asking_the_model_again():
    model = StreamingModel(
        [
            [
                ("call", "handoff", HANDOFF_WEALTH),
                ("call", "handoff", HANDOFF_WEALTH_AGAIN),
            ],
            ["两件事都办好了。"],
        ],
    )

    events = collect(compiled(model))

    assert final_text(events) == "两件事都办好了。"
    # The queue must not cost a third *supervisor* turn: the middle dispatch reuses what
    # the first decision already said. Both domains run the tool-less template, so neither
    # spends a model call and the raw count is two supervisor turns.
    supervisor_turns = [
        prompt for prompt in model.prompts if prompt.startswith("你是银行 AI 助手的总调度")
    ]
    assert len(supervisor_turns) == 2
    assert len(model.prompts) == 2
    assert len(model.completed) == 0


def test_a_new_request_starts_with_no_queue():
    assert new_agent_state("帮我")["pending_handoffs"] == []


def test_a_failure_to_start_is_retried_once_and_only_once():
    model = StreamingModel(
        [["你好"]],
        replies=["你好"],
        errors=[ModelClientError("LLM_UPSTREAM_ERROR", retryable=True)] * 2,
    )

    with pytest.raises(AppError):
        collect(compiled(model))

    assert len(model.prompts) == 2
