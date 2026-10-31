"""The import contract of the two package trees that now depend on each other.

`app.agents.__init__` re-exports its names lazily. An eager `from app.agents.domain import
...` there would make *any* `app.agents.*` import execute `domain.py`, which calls
`build_registry()` at import time; the module packages import `app.agents.declaration`, so
the eager form closes a cycle and `import app.modules` dies with a partially initialized
`app.modules.base`. See the seam contract §3.4.

The cycle check has to run in a subprocess. This test process already loaded `app.agents`
through `conftest`, so an in-process import observes a fully built package and cannot see
the cycle at all -- which is exactly why the whole suite stayed green while
`import app.modules` was broken.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import app.agents

# The entry points a module team actually uses: the package, and one module package
# directly. Both are the first import in a fresh interpreter, with nothing to warm the
# other tree first.
FIRST_IMPORTS = (
    "import app.modules",
    "from app.modules.transfer import MODULE",
)


# A fresh interpreter on a cold import is the kind of thing that can stall rather than
# fail, so the subprocess is bounded. `subprocess.run` raises `TimeoutExpired` instead of
# returning, and that is caught below so a stall reads as this test's own failure rather
# than as an unrelated traceback.
IMPORT_TIMEOUT_SECONDS = 60


@pytest.mark.parametrize("statement", FIRST_IMPORTS)
def test_a_fresh_interpreter_may_import_a_module_package_first(statement):
    try:
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"`{statement}` did not finish within {IMPORT_TIMEOUT_SECONDS}s in a fresh "
            f"interpreter. A cycle raises; a stall usually means an import is waiting on "
            f"itself."
        )

    assert completed.returncode == 0, (
        f"`{statement}` failed in a fresh interpreter. `app/agents/__init__.py` must not "
        f"eagerly import `app.agents.domain`: the module packages import "
        f"`app.agents.declaration`, so that closes a cycle through `build_registry()` "
        f"(seam contract §3.4).\n"
        f"stdout: {completed.stdout}\n"
        f"stderr: {completed.stderr}"
    )


def test_the_agent_package_still_reexports_its_whole_public_surface():
    """The lazy re-export must not cost the surface it replaced.

    The names are listed here rather than read from `__all__`: `__all__` is derived from the
    same table the hook resolves, so dropping an entry would shrink both and this test would
    stay green while a public name silently disappeared. This list is the independent oracle.
    """

    expected = {
        "DomainDeclaration",
        "DomainSpec",
        "DOMAIN_ROSTER",
        "build_agent_graph",
        "build_domain_specs",
        "AgentEvent",
        "AgentRuntime",
        "thread_id_for_session",
        "AgentState",
        "append_messages",
        "append_results",
        "new_agent_state",
        "build_supervisor",
        "TurnResult",
        "TurnStatus",
    }

    assert set(app.agents.__all__) == expected
    for name in expected:
        assert getattr(app.agents, name) is not None

    with pytest.raises(AttributeError):
        app.agents.definitely_not_a_public_name


def test_dir_reports_at_least_the_public_names_and_the_module_metadata():
    """`dir()` is the completion surface, and narrowing it is a silent regression.

    An earlier version of `__dir__` returned `__all__` alone, which dropped the dunders a
    package always exposes -- invisible to every other test here, because nothing asserts
    on `dir()`. This is that assertion.
    """

    reported = set(dir(app.agents))

    assert set(app.agents.__all__) <= reported
    # The module's own metadata, which returning `__all__` alone had removed.
    assert {"__all__", "__name__", "__doc__", "__file__"} <= reported
