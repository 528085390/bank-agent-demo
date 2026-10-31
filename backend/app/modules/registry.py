"""Every declared module, plus the roster and the catalog derived from them.

This is the one place that knows all modules exist. It validates the declarations as a
set -- uniqueness across modules, and that every grant points at something declared --
because a single module cannot see its neighbours.

What lives here and what does not is deliberate. Checks that need cross-module vision
(unique names, unique agents, unique tools, grants naming a declared agent) are here.
The tools/handlers key-set check is not: `ModuleSpec.__post_init__` owns it, and a frozen
dataclass is necessarily constructed before this module can see it, so a second
enforcement point could only ever disagree with the first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.agents.declaration import DomainDeclaration
from app.modules.base import ModuleSpec, ToolHandler
from app.tools.catalog import ToolCatalog
from app.tools.protocol import RESERVED_PARAMETER_NAMES, SideEffect, ToolSpec


@dataclass(frozen=True, slots=True)
class Registry:
    """The assembled seam: every module, who owns which domain, and who may call what."""

    modules: tuple[ModuleSpec, ...]
    roster: tuple[DomainDeclaration, ...]
    catalog: ToolCatalog
    handlers: Mapping[str, ToolHandler]


def build_registry(modules: Iterable[ModuleSpec] | None = None) -> Registry:
    """Collect modules into the roster and catalog the rest of the app reads.

    `None` -- the default -- means the production set, `app.modules.ALL_MODULES`; passing
    it explicitly is the same as omitting it. A caller that means "no modules" must pass
    an empty iterable, and gets the declaration error below.

    A bad declaration raises here, at import time, rather than surfacing later as a tool
    that silently cannot be called.
    """

    if modules is None:
        # Bound here rather than as a module-scope default. This is not about avoiding an
        # import of `app.modules`: the `app.modules.base` import above already executes
        # `app/modules/__init__.py`. It defers the `ALL_MODULES` *binding* until the call,
        # which keeps this module importable while that name does not exist yet -- and
        # tolerates being imported during a partially initialized `app.modules`.
        from app.modules import ALL_MODULES

        modules = ALL_MODULES

    module_list = tuple(modules)
    if not module_list:
        raise ValueError("a registry needs at least one module")

    seen_modules: set[str] = set()
    specs: dict[str, ToolSpec] = {}
    handlers: dict[str, ToolHandler] = {}
    grants: dict[str, list[str]] = {}
    roster: list[DomainDeclaration] = []
    agents: set[str] = set()

    for module in module_list:
        if module.name in seen_modules:
            raise ValueError(f"module is declared twice: {module.name}")
        seen_modules.add(module.name)

        if module.declaration is not None:
            agent_id = module.declaration.agent_id
            if agent_id in agents:
                raise ValueError(f"agent is declared twice: {agent_id}")
            agents.add(agent_id)
            roster.append(module.declaration)

        for name, spec in module.tools.items():
            if name in specs:
                raise ValueError(f"tool is declared twice: {name}")
            # `side_effect` defaults to READ, so a typo would not fail -- it would read as
            # "not WRITE" and quietly regain the retry that write safety exists to remove.
            if spec.side_effect not in (SideEffect.READ, SideEffect.WRITE):
                raise ValueError(
                    f"invalid side_effect for tool {name}: {spec.side_effect!r} "
                    f"(expected READ or WRITE)"
                )
            # The base injects these names; a model's output only ever becomes `arguments`.
            # A tool that declares one as an argument would put a model-chosen value next to
            # the injected one, so a handler could read the forged copy -- and a forged
            # idempotency key or confirmation is exactly what write safety exists to stop.
            properties = spec.parameters.get("properties")
            if isinstance(properties, Mapping):
                reserved = sorted(set(properties) & RESERVED_PARAMETER_NAMES)
                if reserved:
                    raise ValueError(
                        f"tool {name} declares reserved parameter(s): {', '.join(reserved)} "
                        f"(the base injects these, so they must never come from the model)"
                    )
            # Same stance the Gateway takes on `parameters`: a malformed declaration is the
            # module author's bug, so it fails here at import time rather than at the moment
            # a tool happens to return something -- where it would look like the tool's fault.
            if spec.output_schema is not None:
                try:
                    Draft202012Validator.check_schema(spec.output_schema)
                except SchemaError as exc:
                    raise ValueError(f"invalid output_schema for tool {name}: {exc}") from exc
            specs[name] = spec
            # ModuleSpec guarantees the two key sets are equal, so this cannot miss.
            handlers[name] = module.handlers[name]

        for agent_id, names in module.effective_grants.items():
            grants.setdefault(agent_id, []).extend(names)

    unknown = sorted(set(grants) - agents)
    if unknown:
        raise ValueError("grants name undeclared agents: " + ", ".join(unknown))

    # Grants are merged across modules, so the same tool can arrive twice for one agent --
    # a module may re-grant a tool another module owns. Deduplicate keeping first-seen
    # order; `AgentTools` rejects a repeated name outright.
    # ToolCatalog.build re-checks every granted name against the declared tools.
    catalog = ToolCatalog.build(
        tuple(specs.values()),
        {agent: tuple(dict.fromkeys(names)) for agent, names in grants.items()},
    )
    return Registry(
        modules=module_list,
        roster=tuple(roster),
        catalog=catalog,
        handlers=handlers,
    )
