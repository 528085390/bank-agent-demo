"""Agent layer: the domain roster, the graph, and the runtime that drives it.

Names are resolved on first access instead of at package import. An eager
`from app.agents.domain import ...` here would make *any* `app.agents.*` import execute
`domain.py`, which calls `build_registry()` at import; module packages import
`app.agents.declaration`, so the eager form closes a cycle and breaks `import app.modules`.

Submodules are imported the usual way -- `import app.agents.domain`, or `from app.agents
import domain`. What the lazy form no longer pre-binds is *attribute* access on a bare
`import app.agents`, so `app.agents.domain` raises `AttributeError`; import the submodule.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agents.declaration import DomainDeclaration as DomainDeclaration
    from app.agents.declaration import DomainSpec as DomainSpec
    from app.agents.domain import DOMAIN_ROSTER as DOMAIN_ROSTER
    from app.agents.domain import build_agent_graph as build_agent_graph
    from app.agents.domain import build_domain_specs as build_domain_specs
    from app.agents.runtime import AgentEvent as AgentEvent
    from app.agents.runtime import AgentRuntime as AgentRuntime
    from app.agents.runtime import thread_id_for_session as thread_id_for_session
    from app.agents.state import AgentState as AgentState
    from app.agents.state import append_messages as append_messages
    from app.agents.state import append_results as append_results
    from app.agents.state import new_agent_state as new_agent_state
    from app.agents.supervisor import build_supervisor as build_supervisor
    from app.agents.turn import TurnResult as TurnResult
    from app.agents.turn import TurnStatus as TurnStatus

# The one list: each public name and the module that defines it.
_REEXPORTS = {
    "DomainDeclaration": "app.agents.declaration",
    "DomainSpec": "app.agents.declaration",
    "DOMAIN_ROSTER": "app.agents.domain",
    "build_agent_graph": "app.agents.domain",
    "build_domain_specs": "app.agents.domain",
    "AgentEvent": "app.agents.runtime",
    "AgentRuntime": "app.agents.runtime",
    "thread_id_for_session": "app.agents.runtime",
    "AgentState": "app.agents.state",
    "append_messages": "app.agents.state",
    "append_results": "app.agents.state",
    "new_agent_state": "app.agents.state",
    "build_supervisor": "app.agents.supervisor",
    "TurnResult": "app.agents.turn",
    "TurnStatus": "app.agents.turn",
}

__all__ = sorted(_REEXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name = _REEXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    value = getattr(import_module(module_name), name)
    globals()[name] = value  # resolved once; later access skips this hook
    return value


def __dir__() -> list[str]:
    # `__all__` is the public surface, but `dir()` is what a REPL or an editor completes
    # from, so it must not be narrowed to the public names alone: the module's own globals
    # (dunders included) stay visible.
    #
    # Submodule names are the one thing the lazy form does *not* put here, because importing
    # a submodule is what binds it on the parent package. Reach them with `import
    # app.agents.domain` rather than `app.agents.domain`.
    return sorted(set(globals()) | set(__all__))
