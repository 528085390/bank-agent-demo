"""What the agent's own log lines carry when an internal step fails.

An exception's text can hold business data or credentials, so the boundary logs the
exception's *type* and never its message or its traceback. The witnesses below use a
sentinel string as the exception message and assert it never reaches the rendered log --
`caplog.text`, not `record.getMessage()`, because a traceback's text lives in
`record.exc_info` and is only visible once the record is formatted.

One test below is not about a failure at all: it pins the supervisor's log *channel* name,
which the split of `supervisor.py` into a package deliberately preserved.
"""

import asyncio
import logging

from app.agents.runtime import AgentRuntime
from app.tools.protocol import ToolStatus, TrustedContext
from tests.support.scripted_model import ScriptedModel
from tests.test_bank_adapter import adapter_for, invocation

SENTINEL = "SENTINEL_LEAK_4c17"


def test_an_internal_tool_failure_logs_its_type_not_its_message(caplog):
    def handler(inv, client):
        raise RuntimeError(SENTINEL)

    with caplog.at_level("ERROR", logger="app.tools.bank_adapter"):
        result = adapter_for(handler).invoke(invocation())

    # The client's half of the rule is unchanged: a Chinese refusal, no exception text.
    assert result.status is ToolStatus.FAILED
    assert result.error == "工具内部错误"

    # The log's half: enough to debug with (the type), and nothing more.
    assert caplog.text, "a handler failure must be logged, or it cannot be debugged"
    assert "RuntimeError" in caplog.text
    assert SENTINEL not in caplog.text


# --- the caller's bank customer ---


class StubGraph:
    """A compiled graph that publishes nothing: these tests are about the log.

    It answers `compile()` because the runtime compiles whatever a factory hands it, and
    `astream()` is an async generator rather than a coroutine -- an `async def` without a
    `yield` would be awaited instead of iterated.
    """

    def compile(self, **kwargs):
        return self

    async def astream(self, graph_input, config=None, **kwargs):
        if False:  # makes this an async generator rather than a coroutine
            yield None


def factory_capturing(contexts):
    def factory(model, context, assembler=None, turn=None):
        contexts.append(context)
        return StubGraph()

    return factory


def test_a_customer_resolution_failure_logs_its_type_not_its_message(caplog):
    contexts: list[TrustedContext] = []

    def resolver(user_id: str) -> str | None:
        raise RuntimeError(SENTINEL)

    runtime = AgentRuntime(
        model=ScriptedModel([]),
        graph_factory=factory_capturing(contexts),
        customer_resolver=resolver,
    )

    async def run():
        return [event async for event in runtime.invoke("sess_1", "user_0001", "你好")]

    with caplog.at_level("ERROR", logger="app.agents.runtime"):
        assert asyncio.run(run()) == []

    assert caplog.text, "a resolver failure must be logged, or it cannot be debugged"
    assert "RuntimeError" in caplog.text
    assert SENTINEL not in caplog.text


# --- the supervisor's log channel ---


def test_the_supervisor_keeps_the_log_channel_name_it_had_before_the_split(caplog):
    """The supervisor became a package; its log channel did not.

    `app/agents/supervisor/graph.py` hardcodes the channel it had while the supervisor was a
    single module, because dashboards and alert rules filter on that name -- `__name__` there
    would silently rename every record to `app.agents.supervisor.graph`. Nothing else pins
    this: the streaming tests read `caplog.text`, which does not care which channel a record
    came from. So pin the channel, and pin that a record emitted through it lands there.
    """
    from app.agents.supervisor import graph as supervisor_graph

    assert supervisor_graph.logger is logging.getLogger("app.agents.supervisor")

    with caplog.at_level("WARNING", logger="app.agents.supervisor"):
        supervisor_graph.logger.warning(SENTINEL)

    assert any(
        record.name == "app.agents.supervisor" and record.getMessage() == SENTINEL
        for record in caplog.records
    )
