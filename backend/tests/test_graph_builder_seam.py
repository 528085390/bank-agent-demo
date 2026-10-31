"""The seam a module-owned graph is built through: `graph_builder` receives its context.

A module that builds its own graph cannot be handed the conversation the way a whole-turn
handler is: it owns its nodes, so the base has to pass the pieces in. This pins the calling
convention -- `assembler`, `turn` and `client` arrive as keyword arguments -- so the
wealth graph can take the bank client from the same seam the card module ignores.

`domain_scope` is the other half of that seam: the conversation scope a module-owned node
opens around its own nodes, rather than around a subgraph it re-invokes.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

import pytest

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_domain_specs
from app.agents.runtime import TurnContext
from app.agents.supervisor import build_domain_graph, domain_scope
from app.context.handoff import Handoff, current_handoff
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel

SESSION = "s_1"
USER = "u_1"
CONTEXT = TrustedContext(user_id=USER, session_id=SESSION, bank_customer_id="u_wealth")
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


class GraphBuilderSpy:
    """The synthetic `graph_builder`: it records the three keywords it was handed.

    The keywords are required, so a base that does not pass them fails loudly instead of
    silently handing the module a `None` it cannot tell from "no conversation".
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Any, Any, Any]] = []

    def __call__(self, model, spec, *, assembler, turn, client):
        self.calls.append((model, spec, assembler, turn, client))
        return "compiled"


def graph_builder_module(spy: GraphBuilderSpy) -> DomainDeclaration:
    return DomainDeclaration(
        agent_id="wealth",
        display="理财 Agent",
        purpose="理财产品与持仓",
        graph_builder=spy,
    )


def test_the_graph_builder_receives_the_assembler_the_turn_and_the_client():
    spy = GraphBuilderSpy()
    gateway = ToolGateway(ToolCatalog.build([ECHO], {"wealth": ["echo"]}), RecordingAdapter({}))
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(graph_builder_module(spy),))
    model = ScriptedModel([])
    assembler = object()
    turn = object()
    client = object()

    graph = build_domain_graph(model, spec, CONTEXT, assembler, turn, client)

    assert graph == "compiled"
    assert len(spy.calls) == 1, "the graph_builder must be called exactly once"
    got_model, got_spec, got_assembler, got_turn, got_client = spy.calls[0]
    assert got_model is model
    assert got_spec is spec
    assert got_assembler is not None
    assert got_assembler is assembler
    assert got_turn is not None
    assert got_turn is turn
    assert got_client is not None
    assert got_client is client


def test_the_graph_builder_receives_a_none_client_when_the_root_has_none():
    """`client` is optional: a root built without a bank client still reaches the seam."""

    spy = GraphBuilderSpy()
    gateway = ToolGateway(ToolCatalog.build([ECHO], {"wealth": ["echo"]}), RecordingAdapter({}))
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(graph_builder_module(spy),))

    graph = build_domain_graph(ScriptedModel([]), spec, CONTEXT, None, None, None)

    assert graph == "compiled"
    assert len(spy.calls) == 1, "the graph_builder must be called exactly once"
    assert spy.calls[0][4] is None


def test_a_domain_with_business_tools_must_declare_a_graph_builder():
    """Tools without a builder is a build-time error, not a silent second path (A5 §1).

    The base used to run its own decide/wait/process loop for a domain that declared tools
    and no builder, so the same request had two orchestrators and only one of them was the
    module's. Declaring a builder is how a domain owns its graph; anything else is a bug.
    """

    gateway = ToolGateway(ToolCatalog.build([ECHO], {"plain": ["echo"]}), RecordingAdapter({}))
    declaration = DomainDeclaration(agent_id="plain", display="普通 Agent", purpose="没有自己的图")
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(declaration,))

    with pytest.raises(ValueError, match="graph_builder"):
        build_domain_graph(ScriptedModel([]), spec, CONTEXT)


def test_a_graph_builder_owns_its_graph_even_with_an_empty_tool_menu():
    """Owning the graph does not depend on having tools yet.

    A module that declares a builder but ships no tools must still get *its* graph. If the
    guard ever narrows back to "builder and tools", a module that adds its builder before its
    tools would silently run the tool-less shell instead -- the second execution path A5
    removes, with nothing in the suite to notice.
    """

    spy = GraphBuilderSpy()
    gateway = ToolGateway(ToolCatalog.build([ECHO], {}), RecordingAdapter({}))
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(graph_builder_module(spy),))

    graph = build_domain_graph(ScriptedModel([]), spec, CONTEXT)

    assert graph == "compiled"
    assert len(spy.calls) == 1, "a declared builder owns the graph even with no tools"


def test_a_tool_less_spec_compiles_to_the_minimal_shell():
    """No tools and no builder: the model-free template, with no node that can pause.

    The old fallback built decide/wait/process and the wait node interrupted; a shell with
    nothing to do has no reason to ask the user anything, so it concludes in one node.
    """

    gateway = ToolGateway(ToolCatalog.build([ECHO], {}), RecordingAdapter({}))
    declaration = DomainDeclaration(agent_id="shell", display="空壳 Agent", purpose="尚未接入")
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(declaration,))

    graph = build_domain_graph(ScriptedModel([]), spec, CONTEXT)

    assert set(graph.get_graph().nodes) == {"__start__", "conclude", "__end__"}


def test_domain_scope_publishes_the_handoff_for_the_block_and_clears_it_after():
    """The scope a module-owned node opens itself: a real context manager, not a re-invoke."""

    spy = GraphBuilderSpy()
    gateway = ToolGateway(ToolCatalog.build([ECHO], {"wealth": ["echo"]}), RecordingAdapter({}))
    (spec,) = build_domain_specs(gateway, CONTEXT, roster=(graph_builder_module(spy),))
    assembler = object()
    turn = TurnContext(
        conversation_id="c_1",
        thread_id="t_1",
        message_id="m_1",
        content="查一下我的持仓",
        sequence=3,
    )

    assert current_handoff() is None
    scope = domain_scope(spec, {}, assembler, turn, "查一下我的持仓")
    # 真 contextmanager（@contextmanager 的生成器函数），不是把子图再 invoke 一次的包装。
    assert isinstance(scope, AbstractContextManager)
    assert hasattr(domain_scope, "__wrapped__")
    with scope as handoff:
        assert isinstance(handoff, Handoff)
        assert current_handoff() is handoff
        assert handoff.agent_id == "wealth"
        assert handoff.current_user_text == "查一下我的持仓"
        assert handoff.current_sequence == 3
        assert handoff.assembler is assembler
    assert current_handoff() is None
