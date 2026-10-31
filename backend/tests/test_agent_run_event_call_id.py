"""The tool-call audit id must fit the column the Supervisor writes it to.

The Supervisor mints `f"{agent_id}:{tool_name}:{uuid4().hex}"` for every tool call
(`app/agents/sdk.py:738` for the idempotency key, `:651` for the call id -- the mint moved
out of `supervisor.py` when that module was split) and ToolGateway writes that call id to
`agent_run_event.call_id` before the adapter is handed the call
(gateway.py:209 vs :215). When the column was `String(64)`, `subscription_execute` minted
66 characters and the INSERT raised `StringDataRightTruncation`, so the tool never ran --
and the 1653-test suite stayed green, because the domain harness builds the graph directly
and never exercises this mint or this write.

These tests pin the invariant at its source -- the widths the model itself declares -- so
the next long tool name cannot reintroduce the defect silently.
"""

from __future__ import annotations

from types import SimpleNamespace

from app import modules as app_modules
from app.storage.models import AgentRunEvent

# The Supervisor builds `f"{agent_id}:{tool_name}:{uuid4().hex}"`. A uuid4 hex is always 32
# characters; the widths of the two halves are read off the model, never hardcoded, so the
# test states the bound instead of restating today's numbers.
UUID4_HEX = 32


def declared_width(column: str) -> int:
    """The character width the model declares for a column of `agent_run_event`."""

    return AgentRunEvent.__table__.c[column].type.length


def minted_id(agent_id: str, tool_name: str) -> str:
    """The id the Supervisor would mint, with the uuid's 32 hex characters pinned.

    The uuid is random, not deterministic; its length is the only part that matters here, so
    the placeholder keeps the test reproducible.
    """

    return f"{agent_id}:{tool_name}:{'0' * UUID4_HEX}"


def test_the_call_id_column_holds_the_declared_maxima():
    """`call_id` is at least as wide as the widest id the columns beside it can build.

    This states *why* the width is what it is: it is derived from `agent_id` (String(64))
    + ':' + `tool_name` (String(128)) + ':' + a uuid4 hex, not chosen to fit today's names.
    """

    width = declared_width("call_id")

    assert declared_width("agent_id") + 1 + declared_width("tool_name") + 1 + UUID4_HEX <= width


def audit_the_registry() -> tuple[list[tuple[str, str, int]], list[str]]:
    """Walk the registry and return the over-long mints plus the declaration-less modules.

    A module with no declaration mints no `agent_id:tool_name` id at all -- a shared-tools
    module need not declare an agent -- so it is stepped over rather than asserted against.
    `test_the_call_id_column_holds_the_declared_maxima` still pins the bound any such tool
    would have to fit if it ever did mint.
    """

    width = declared_width("call_id")
    too_long: list[tuple[str, str, int]] = []
    undeclared: list[str] = []
    for module in app_modules.ALL_MODULES:
        if module.declaration is None:
            undeclared.append(module.name)
            continue
        agent_id = module.declaration.agent_id
        for tool_name in module.tools:
            length = len(minted_id(agent_id, tool_name))
            if length > width:
                too_long.append((module.name, tool_name, length))
    return too_long, undeclared


def test_every_registered_tool_mints_an_id_the_column_can_hold():
    """No tool of any module in the production registry may mint an id the column rejects."""

    width = declared_width("call_id")
    too_long, _ = audit_the_registry()

    assert too_long == [], (
        f"agent_run_event.call_id is String({width}); the audit INSERT for these tools would "
        f"raise StringDataRightTruncation before the adapter runs: {too_long}"
    )


def test_a_module_without_a_declaration_is_skipped_not_asserted(monkeypatch):
    """A declaration-less shared-tools module must be stepped over, not asserted against.

    A future shared-tools module may put tools in the registry without declaring an agent of
    its own. It mints no `agent_id:tool_name` id, so there is nothing to check for it; a bare
    `assert module.declaration is not None` would fail on a module that violates nothing.
    """

    stub = SimpleNamespace(name="stub_shared_tools", declaration=None, tools=["stub_tool"])
    monkeypatch.setattr(app_modules, "ALL_MODULES", [*app_modules.ALL_MODULES, stub])

    too_long, undeclared = audit_the_registry()

    assert too_long == []
    assert undeclared == ["stub_shared_tools"], "the stub was reached and stepped over"
