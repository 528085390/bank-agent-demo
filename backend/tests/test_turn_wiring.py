"""The composition root: the registry is built once, and the bank client reaches the builder.

A second registry means a second wealth agent, and the next message starts over; a dropped
`client=client` hands a module-owned graph a `None` it cannot tell from "no bank client".
Neither failure is loud, so both hops are pinned here.
"""

from __future__ import annotations

import os

import pytest

from app.agents.declaration import DomainDeclaration
from app.core.config import Settings
from app.core.database_file import sqlalchemy_url
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.tools.protocol import ToolSpec, TrustedContext
from tests.support.scripted_model import ScriptedModel

DECLARATION = DomainDeclaration(agent_id="wealth", display="理财 Agent", purpose="理财")
CONTEXT = TrustedContext(user_id="u_1", session_id="s_1", bank_customer_id="u_wealth")
ECHO = ToolSpec(
    name="echo",
    description="Return what it was given",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)


def _unconnected_settings():
    """Settings for a Runtime that opens no connection: construction only builds objects."""

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        try:
            url = sqlalchemy_url("test")
        except RuntimeError:
            pytest.skip("Set TEST_DATABASE_URL or provide database.txt to build a Runtime")
    return Settings(database_url=url)


def test_the_runtime_builds_the_registry_exactly_once(monkeypatch):
    """A second registry means a second wealth agent, and the next message starts over."""

    import app.modules.registry as registry_module
    import app.runtime as runtime_module

    builds: list[int] = []
    real = registry_module.build_registry

    def spy(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(registry_module, "build_registry", spy)
    runtime = runtime_module.Runtime(_unconnected_settings())
    try:
        assert builds == [1]
    finally:
        runtime.database.dispose()


def test_the_bank_client_reaches_the_graph_builder_end_to_end(monkeypatch):
    """Every client hop, from the object the root was built with to the module's builder.

    Dropping `client=client` anywhere on the way silently hands a module-owned graph a
    `None` it cannot tell from "no bank client", and nothing failed. The only thing
    substituted here is the roster -- the factory never passes one and the production
    roster has no builder to spy on -- so every hop this test walks is the production one.
    """

    import app.agents.domain as domain_module
    import app.modules.registry as registry_module
    import app.runtime as runtime_module

    calls: list[tuple[object, object, object, object, object]] = []

    def graph_builder(model, spec, *, assembler, turn, client):
        calls.append((model, spec, assembler, turn, client))

        def node(state):
            return {}

        return node

    module = ModuleSpec(
        name="wealth",
        declaration=DomainDeclaration(
            agent_id="wealth",
            display="理财 Agent",
            purpose="理财产品与持仓",
            graph_builder=graph_builder,
        ),
        tools={"echo": ECHO},
        handlers={"echo": lambda invocation, client: None},
    )
    registry = build_registry([module])
    monkeypatch.setattr(registry_module, "build_registry", lambda *args, **kwargs: registry)

    real_specs = domain_module.build_domain_specs
    monkeypatch.setattr(
        domain_module,
        "build_domain_specs",
        lambda gateway, context, *, roster=None: real_specs(
            gateway, context, roster=registry.roster
        ),
    )

    client = object()
    runtime = runtime_module.Runtime(_unconnected_settings(), client=client)
    try:
        graph = runtime.agent._graph_factory(ScriptedModel([]), CONTEXT, None, None)
        graph.compile()
    finally:
        runtime.database.dispose()

    assert runtime.bank_client is client
    assert len(calls) == 1, "the module's own graph builder is the one that must be called"
    assert calls[0][4] is client, "the bank client must arrive at the graph_builder itself"
