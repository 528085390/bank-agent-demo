"""The graph a domain with no business tools runs.

A domain that declares no business tools has nothing to decide and nothing to wait for:
the request cannot be served, so the activation concludes at once. There is no model call
-- a model could only be asked to pick from an empty tool menu -- and no `interrupt`, so
the domain never pauses.

This is the one production path left for a tool-less shell. A domain that carries business
tools must declare a `graph_builder` of its own (roadmap A5 §1); `build_domain_graph`
refuses to fall back to a generic loop.

The plan suggested the name `minimal_tool_loop_graph`; the graph runs no tool loop, so the
honest name is `minimal_shell_graph`.
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.sdk import conclude_domain
from app.agents.state import AgentState
from app.tools.protocol import ToolStatus

# What a tool-less domain tells the user, and what the Supervisor's ledger records. It is
# defined here because this module owns the only ending a tool-less domain has; the
# Supervisor imports it so its `_already_concluded` check keeps reading the same sentence.
NO_TOOLS_SUMMARY = "该能力当前尚未接入，暂时无法处理此请求。"


def minimal_shell_graph(
    model: Any,
    spec: Any,
    *,
    assembler: Any = None,
    turn: Any = None,
    client: Any = None,
) -> Any:
    """Compile the tool-less shell as a `graph_builder`-compatible callable.

    The signature matches `DomainDeclaration.graph_builder`, so the same seam can carry
    it. The model and the assembled context are accepted and ignored: the shell never calls
    the model.
    """

    def conclude(state: AgentState) -> dict[str, Any]:
        writer = get_stream_writer()
        writer({"event": "agent_start", "agent_id": spec.agent_id})
        return conclude_domain(
            spec, state, NO_TOOLS_SUMMARY, ToolStatus.SUCCEEDED, writer, business_ids={}
        )

    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("conclude", conclude)
    graph.add_edge(START, "conclude")
    graph.add_edge("conclude", END)
    return graph.compile()
