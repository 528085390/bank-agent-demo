"""Every `from app... import <name>` in the repository has to resolve.

A refactor that moves a symbol leaves the old import path behind in whatever file
nobody happens to run, and the break stays invisible until that file is finally
collected. That is exactly how the dev merge left two test files importing
`MAX_TOOL_ROUNDS` and `DomainOutcome` from `app.agents.supervisor` long after A2
moved them to `app.agents.sdk`: neither file was on any targeted run list, so every
"all green" report skipped them.

`tests/test_module_import_boundary.py` watches the *direction* imports are allowed
to take, but only inside `app/modules/**`. This file watches that the names exist,
everywhere, including the tests themselves.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCANNED = (BACKEND / "app", BACKEND / "tests")
SKIPPED_PARTS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


def _import_targets() -> list[tuple[str, str, str]]:
    """Every absolute `from app... import name` in the tree, as (file, module, name)."""

    targets: list[tuple[str, str, str]] = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if SKIPPED_PARTS.intersection(path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            source = path.relative_to(BACKEND).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level:
                    continue
                module = node.module or ""
                if module != "app" and not module.startswith("app."):
                    continue
                for alias in node.names:
                    targets.append((source, module, alias.name))
    return targets


def _resolves(module: str, name: str) -> bool:
    """True when `from module import name` would succeed."""

    imported = importlib.import_module(module)
    if hasattr(imported, name):
        return True
    # `from app.modules.wealth.workflow import graph` names a submodule, and a
    # submodule only becomes an attribute of its package once something imports it.
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return False
    return True


def test_the_scan_reaches_the_tests_and_the_supervisor() -> None:
    """Guard the guard: a scanner that matched nothing would pass forever."""

    targets = _import_targets()

    assert len(targets) > 100
    assert any(source.startswith("tests/") for source, _, _ in targets)
    assert any(module == "app.agents.supervisor" for _, module, _ in targets)


def test_every_imported_name_resolves() -> None:
    missing = [
        f"{source}: from {module} import {name}"
        for source, module, name in _import_targets()
        if not _resolves(module, name)
    ]

    assert missing == []
