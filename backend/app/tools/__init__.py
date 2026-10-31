"""The Generic tool gateway: identity binding, existence, argument schema, injection.

Nothing here names a bank, an account or a field. The catalog is declarative, the
adapter is the only outbound surface, and the agent identity is fixed by the handle
the Gateway itself built.
"""

from app.tools.catalog import AgentTools, ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.handle import AgentToolHandle
from app.tools.protocol import (
    ToolAdapter,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)

__all__ = [
    "AgentTools",
    "AgentToolHandle",
    "ToolAdapter",
    "ToolCall",
    "ToolCatalog",
    "ToolGateway",
    "ToolInvocation",
    "ToolResult",
    "ToolSpec",
    "ToolStatus",
    "TrustedContext",
]
