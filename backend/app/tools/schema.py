"""Turn a declared ToolSpec into the function definition a model is given.

A declaration and a schema are two views of one thing, so this module is the only place that
knows how a `parameters` mapping becomes a tool definition. It exists for two reasons:
`ToolSpec.parameters` defaults to `{}`, which is not a JSON Schema; and strict mode has no
notion of an optional property, so an optional one has to become a required nullable one.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.tools.protocol import ToolSpec

ASK_USER_TOOL_NAME = "ask_user"
ASK_USER_FIELDS = "fields"
ASK_USER_DISPLAY = "display"

# A tool name is offered to the model verbatim, and the provider refuses anything that is
# not an identifier -- a dot in particular, with code 11152, as a 503 raised once a turn is
# already in flight. A declaration that cannot be sent is the module author's bug, so it
# fails here, where the message can name the tool.
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")

_NULLABLE = "null"
_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def normalize_parameters(
    parameters: Mapping[str, Any], *, strict: bool = True
) -> tuple[dict[str, Any], bool]:
    """Return `(schema, can_be_strict)`.

    `can_be_strict` is False when a declared property cannot be expressed in strict mode:
    a property with no `type` cannot be made nullable, so the tool is sent unstrict rather
    than as a schema the provider would reject. Only the top level is normalized -- a nested
    object is passed through as declared.
    """

    if not parameters:
        return dict(_EMPTY_OBJECT_SCHEMA), True

    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return dict(_EMPTY_OBJECT_SCHEMA), False

    required = [name for name in (parameters.get("required") or []) if isinstance(name, str)]
    normalized: dict[str, Any] = {}
    can_be_strict = True
    for name, declared in properties.items():
        if not isinstance(declared, Mapping) or "type" not in declared:
            can_be_strict = False
            normalized[name] = dict(declared) if isinstance(declared, Mapping) else {}
            continue
        field = dict(declared)
        if name not in required and not strict:
            normalized[name] = field
            continue
        if name not in required:
            declared_type = field["type"]
            if isinstance(declared_type, str):
                field["type"] = [declared_type, _NULLABLE]
            elif isinstance(declared_type, list) and _NULLABLE not in declared_type:
                field["type"] = [*declared_type, _NULLABLE]
            else:
                can_be_strict = False
            required.append(name)
        normalized[name] = field

    return (
        {
            "type": "object",
            "properties": normalized,
            "required": required,
            "additionalProperties": False,
        },
        can_be_strict,
    )


def function_name(name: str) -> str:
    """The name a tool is offered under. Refused unless it is a lowercase identifier."""

    if not _TOOL_NAME.fullmatch(name):
        raise ValueError(f"a tool name must be a lowercase identifier (no dot): {name!r}")
    return name


def function_schema(spec: ToolSpec, *, strict: bool = True) -> dict[str, Any]:
    """The tool definition for the chat-completions `tools` parameter."""

    parameters, expressible = normalize_parameters(spec.parameters, strict=strict)
    return {
        "type": "function",
        "function": {
            "name": function_name(spec.name),
            "description": spec.description,
            "parameters": parameters,
            "strict": bool(strict and expressible),
        },
    }


def ask_user_spec() -> ToolSpec:
    """The one tool the base injects: asking the user for the fields a call needs.

    It is a framework tool, not a business one. It never appears in the business menu the
    prompt prints, and it is answered by the interrupt machinery rather than by an adapter.
    """

    return ToolSpec(
        name=ASK_USER_TOOL_NAME,
        description="向用户补充信息。fields 是要问的字段名列表，display 是显示给用户的问题。",
        parameters={
            "type": "object",
            "properties": {
                ASK_USER_FIELDS: {"type": "array", "items": {"type": "string"}},
                ASK_USER_DISPLAY: {"type": "string"},
            },
            "required": [ASK_USER_FIELDS, ASK_USER_DISPLAY],
        },
    )
