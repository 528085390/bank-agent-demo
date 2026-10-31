"""The declared tool menu, and which agent may reach which tool.

There is no capability registry and no business field table here. Every name and every
schema in a catalog was supplied by the caller that built it, so this module stays
domain-free and the first phase can declare no tools at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.tools.protocol import ToolSpec


@dataclass(frozen=True, slots=True)
class AgentTools:
    """One agent's own view of the catalog."""

    agent_id: str
    specs: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.agent_id or not self.agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        names = [spec.name for spec in self.specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError("agent declares a tool twice: " + ", ".join(duplicates))

    def spec(self, name: str) -> ToolSpec | None:
        for spec in self.specs:
            if spec.name == name:
                return spec
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """Every declared tool, plus the per-agent grants that point at them."""

    specs: Mapping[str, ToolSpec] = field(default_factory=dict)
    grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        specs: Iterable[ToolSpec] = (),
        grants: Mapping[str, Sequence[str]] | None = None,
    ) -> ToolCatalog:
        declared: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in declared:
                raise ValueError(f"tool is declared twice: {spec.name}")
            declared[spec.name] = spec

        bound: dict[str, tuple[str, ...]] = {}
        for agent_id, names in (grants or {}).items():
            missing = sorted({name for name in names if name not in declared})
            if missing:
                raise ValueError(f"{agent_id} is granted undeclared tools: " + ", ".join(missing))
            bound[agent_id] = tuple(names)
        return cls(specs=declared, grants=bound)

    def for_agent(self, agent_id: str) -> AgentTools | None:
        names = self.grants.get(agent_id)
        if names is None:
            return None
        return AgentTools(agent_id=agent_id, specs=tuple(self.specs[name] for name in names))

    def get(self, name: str) -> ToolSpec | None:
        return self.specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.specs)
