"""The six domain Agents, and the Supervisor that orchestrates them.

Each agent is a declaration plus its own bound tool handle. The first phase declares no
business tools, so every handle is empty: the agents exist as graphs with their own
prompt and their own catalog view, which is what this phase is meant to prove.
"""

from __future__ import annotations

from typing import Any

from app.agents.declaration import DomainDeclaration, DomainSpec
from app.agents.runtime import TurnContext
from app.agents.supervisor import build_supervisor
from app.integration.model_client import ModelClient
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.protocol import TrustedContext

# The roster is derived from the registered modules rather than written here, so a module
# team never edits this file. `build_registry()` runs at import: a bad declaration fails
# at startup, not on some user's question.
DOMAIN_ROSTER: tuple[DomainDeclaration, ...] = build_registry().roster


def build_domain_specs(
    gateway: ToolGateway,
    context: TrustedContext,
    *,
    roster: tuple[DomainDeclaration, ...] = DOMAIN_ROSTER,
) -> list[DomainSpec]:
    """Bind each declared agent to its own handle, in roster order."""

    return [
        DomainSpec(
            declaration=declaration,
            handle=gateway.handle(declaration.agent_id, context),
        )
        for declaration in roster
    ]


def build_agent_graph(
    model: ModelClient,
    gateway: ToolGateway,
    context: TrustedContext,
    *,
    assembler: Any | None = None,
    turn: TurnContext | None = None,
    client: Any | None = None,
    roster: tuple[DomainDeclaration, ...] | None = None,
):
    """The Supervisor graph with all six domains attached as subgraph nodes.

    `assembler` and `turn` are passed straight through to the Supervisor: the context
    assembler is built by the composition root, and only the turn knows which conversation
    and which ledger sequence the prompt belongs to.

    `context` is the trusted identity: it reaches the tool handles and the domain specs.
    """

    return build_supervisor(
        model,
        build_domain_specs(
            gateway, context, roster=DOMAIN_ROSTER if roster is None else roster
        ),
        assembler=assembler,
        turn=turn,
        context=context,
        client=client,
    )
