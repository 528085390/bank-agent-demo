"""The immutable binding between one agent, its tools and one caller's identity.

A domain subgraph is built with a handle and nothing else. The model may write an
`agent` field into its request; the handle ignores it, because the agent identity is
fixed at construction time and the model has no way to reach the Gateway without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from app.tools.protocol import ToolResult, ToolSpec, TrustedContext

if TYPE_CHECKING:  # a runtime import would be circular; the annotation stays a string
    from app.tools.gateway import ToolGateway


@dataclass(frozen=True, slots=True)
class AgentToolHandle:
    """An immutable, per-agent binding. Constructed by the Gateway, never by a model."""

    agent_id: str
    tools: tuple[ToolSpec, ...]
    trusted_context: TrustedContext
    gateway: ToolGateway = field(repr=False, compare=False)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(spec.name for spec in self.tools)

    def spec(self, tool_name: str) -> ToolSpec | None:
        for spec in self.tools:
            if spec.name == tool_name:
                return spec
        return None

    def call(self, request: Mapping[str, Any]) -> ToolResult:
        """Invoke one tool from a raw model request.

        Only `tool`, `arguments` and `call_id` are read. Any other key the model sent
        -- `agent` in particular -- is ignored rather than honoured.
        """

        return self.gateway.invoke(self, request)
