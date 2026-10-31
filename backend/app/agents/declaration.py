"""What a domain Agent declares about itself, and what it becomes once bound.

A declaration is data an agent module owns: its id, its display name, its purpose, and how
the fields of the questions it asks look to a user. The tools are deliberately not here.
They belong to the module that implements them (`ModuleSpec.tools`), so "which tools exist"
has one source and cannot drift from the handlers behind it.

A `DomainSpec` is that declaration bound to one caller's tool handle, which is what a
subgraph node is built from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.state import PendingField
from app.tools.handle import AgentToolHandle
from app.tools.schema import ask_user_spec, function_schema


@dataclass(frozen=True, slots=True)
class FieldMeta:
    """How one field of a question looks to a user.

    It lives on the declaration, next to `display` and `purpose`: all three are the domain
    describing itself, and keeping them together means one source rather than two.
    """

    label: str
    type: str = "text"
    required: bool = True
    options: tuple[str, ...] = ()


# What an undeclared field looks like. A module may declare its fields at any time; until
# it does, a question about them still renders and is still answerable.
FALLBACK_FIELD = FieldMeta(label="", type="text", required=True)

# What a question with no fields at all becomes. `ask_user` may name none, and a question
# with an empty field list is one a client cannot answer: it renders no input and its answer
# envelope is the empty object, so the user is stuck behind an enabled submit button. One
# free-text field is the smallest thing that makes it answerable, and it is already a field
# type every client knows.
FREE_TEXT_FIELD: PendingField = {
    "name": "answer",
    "label": "补充说明",
    "type": "text",
    "required": True,
    "options": [],
}


@dataclass(frozen=True, slots=True)
class DomainDeclaration:
    """A domain Agent's own declaration. No handle, no caller identity, no tools."""

    agent_id: str
    display: str
    purpose: str
    # How this domain's question fields look to a user, by the name a model asks with.
    # Empty is the normal starting point: an undeclared field still renders (see
    # `resolve_fields`), so declaring them is incremental rather than a prerequisite.
    fields: Mapping[str, FieldMeta] = field(default_factory=dict)
    graph_builder: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.agent_id or not self.agent_id.strip():
            raise ValueError("domain agent_id must be a non-empty string")
        if not self.display or not self.display.strip():
            raise ValueError(f"{self.agent_id} must declare a display name")


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """A declaration bound to one caller's handle, ready to become a subgraph node."""

    declaration: DomainDeclaration
    handle: AgentToolHandle

    @property
    def agent_id(self) -> str:
        return self.declaration.agent_id

    @property
    def display(self) -> str:
        return self.declaration.display

    @property
    def purpose(self) -> str:
        return self.declaration.purpose

    def tool_menu(self) -> list[tuple[str, str]]:
        """The tools this agent may call, as its prompt shows them.

        The menu comes from the handle, not from the declaration, so what the prompt
        advertises is exactly what the Gateway will accept.
        """

        return [(spec.name, spec.description) for spec in self.handle.tools]

    def tool_schemas(self, *, strict: bool = True) -> list[dict[str, Any]]:
        """The tool definitions the model is given: the business tools plus the framework tool.

        Same source as `tool_menu()` on purpose. If the two were derived separately, the menu
        the prompt prints and the tools the model may call could drift apart -- and the drift
        would show up as a model calling a tool the Gateway then refuses.

        The framework tool is added here and not to the menu: it is not a business capability.
        It is only added when the menu is non-empty, because an empty menu means the capability
        is not connected, not that the user should be asked for more detail.
        """

        if not self.handle.tools:
            return []
        schemas = [function_schema(spec, strict=strict) for spec in self.handle.tools]
        schemas.append(function_schema(ask_user_spec(), strict=strict))
        return schemas


def resolve_fields(declaration: DomainDeclaration, names: Iterable[str]) -> list[PendingField]:
    """Turn the field names a model asked for into what a client renders.

    The model still names bare fields -- it is the module that says what they look like.
    An undeclared field falls back to its own name rather than failing, so modules can
    adopt this incrementally and a missing declaration never blocks a question.
    """

    resolved: list[PendingField] = []
    for name in names:
        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned:
            continue
        meta = declaration.fields.get(cleaned, FALLBACK_FIELD)
        resolved.append(
            {
                "name": cleaned,
                "label": meta.label or cleaned,
                "type": meta.type,
                "required": meta.required,
                "options": list(meta.options),
            }
        )
    return resolved
