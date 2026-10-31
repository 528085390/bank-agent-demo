"""Modules must depend on the public agents SDK, not supervisor internals."""

from __future__ import annotations

import ast
from pathlib import Path

_MODULES_ROOT = Path(__file__).resolve().parents[1] / "app" / "modules"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _imports_supervisor(path: Path) -> list[str]:
    """Return import lines (module form) that pull in app.agents.supervisor."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.agents.supervisor" or alias.name.startswith(
                    "app.agents.supervisor."
                ):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module == "app.agents.supervisor" or node.module.startswith(
                "app.agents.supervisor."
            ):
                hits.append(node.module)
            # from app.agents import supervisor
            if node.module == "app.agents":
                for alias in node.names:
                    if alias.name == "supervisor":
                        hits.append("app.agents.supervisor")
    return hits


def _private_sdk_names(path: Path) -> list[str]:
    """Return underscore-prefixed names imported from app.agents.sdk."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.agents.sdk":
            hits.extend(alias.name for alias in node.names if alias.name.startswith("_"))
    return hits


def _imports_turn(path: Path) -> list[str]:
    """Return import lines (module form) that pull in app.agents.turn."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.agents.turn" or alias.name.startswith("app.agents.turn."):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module == "app.agents.turn" or node.module.startswith("app.agents.turn."):
                hits.append(node.module)
            # from app.agents import turn
            if node.module == "app.agents":
                for alias in node.names:
                    if alias.name == "turn":
                        hits.append("app.agents.turn")
    return hits


def test_modules_must_not_import_supervisor() -> None:
    """Guard: app.modules/** must not import app.agents.supervisor (allowlist empty)."""
    offenders: list[str] = []
    for path in _python_files(_MODULES_ROOT):
        for module in _imports_supervisor(path):
            rel = path.relative_to(_MODULES_ROOT.parent.parent)
            offenders.append(f"{rel}: imports {module}")
    assert offenders == [], "modules must use app.agents.sdk only:\n" + "\n".join(offenders)


def test_modules_must_not_import_private_sdk_names() -> None:
    """Guard: app.modules/** may only import public (non-underscore) SDK names."""
    offenders: list[str] = []
    for path in _python_files(_MODULES_ROOT):
        for name in _private_sdk_names(path):
            rel = path.relative_to(_MODULES_ROOT.parent.parent)
            offenders.append(f"{rel}: imports {name}")
    assert offenders == [], "modules must import public names from app.agents.sdk:\n" + "\n".join(
        offenders
    )


def test_modules_must_not_import_agents_turn() -> None:
    """Guard: app.modules/** must take turn vocabulary from app.agents.sdk."""
    offenders: list[str] = []
    for path in _python_files(_MODULES_ROOT):
        for module in _imports_turn(path):
            rel = path.relative_to(_MODULES_ROOT.parent.parent)
            offenders.append(f"{rel}: imports {module}")
    assert offenders == [], (
        "modules must import turn vocabulary from app.agents.sdk:\n" + "\n".join(offenders)
    )


def test_modules_root_exists() -> None:
    assert _MODULES_ROOT.is_dir()
