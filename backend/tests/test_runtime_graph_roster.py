"""The production graph must be built from the bound roster, not the import-time one.

production_modules() applies each module binder, and the binder is what hands the
composition root write facade and database to a module own graph builder. The
import-time DOMAIN_ROSTER bypasses that: transfer then constructs a second WriteFacade
with no audit hook and no daily-total store, so the 1,000-yuan cap silently stops
working. This test pins the factory contract that prevents the regression.
"""

from app.modules import production_modules
from app.modules.registry import build_registry
from app.runtime import agent_graph_factory


def test_graph_factory_passes_bound_roster(monkeypatch):
    sentinel_writes = object()
    registry = build_registry(
        production_modules(
            notification_service=object(),
            database=object(),
            writes=sentinel_writes,
        )
    )
    captured = {}

    def fake_build_agent_graph(model, gateway, context, **kwargs):
        captured.update(kwargs)
        return "graph"

    monkeypatch.setattr("app.runtime.build_agent_graph", fake_build_agent_graph)
    factory = agent_graph_factory(object(), roster=registry.roster)
    assert factory(object(), object(), assembler=None, turn=None) == "graph"

    transfer = next(item for item in captured["roster"] if item.agent_id == "transfer")
    bound = transfer.graph_builder
    assert bound.keywords["writes"] is sentinel_writes
