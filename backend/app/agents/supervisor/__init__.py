"""The Supervisor graph and its six domain subgraphs.

The Supervisor is the only cross-domain orchestrator. It reads the transcript and the
results collected so far, names one domain agent, and reads that agent's structured
result; a domain agent may call the tools its own bound handle carries. Domains never
call each other. Neither answers for the other: the Supervisor's own assistant turn is
the user-facing reply, published as `final`.

Every event is published through the custom stream, so what the client sees is exactly
what a node chose to publish, and the adapter never reconstructs events from state.

The implementation lives in two modules: `graph` assembles the Supervisor and its domain
subgraphs and routes a turn, and `handoff` runs the single streamed decision turn that
names the next domain. Both are re-exported here, so `import app.agents.supervisor`
still hands back the same surface it did when this was one module.
"""

from __future__ import annotations

from app.agents import sdk as _agents_sdk
from app.agents.subgraph_templates import NO_TOOLS_SUMMARY
from app.agents.supervisor.graph import (
    DONE,
    MAX_DOMAIN_TURNS,
    OVERFLOW_UPSTREAM_CODE,
    TERMINAL_ABORTED_STREAMING,
    TERMINAL_API_ERROR,
    TERMINAL_COMPLETED,
    TERMINAL_MAX_TURNS,
    _already_concluded,
    _closing_reply,
    _collect_business_ids,
    _final_from,
    build_domain_graph,
    build_supervisor,
    logger,
    reset_goal_node,
    supervisor_node,
    supervisor_route,
)
from app.agents.supervisor.handoff import (
    DELTA_FLUSH_CHARS,
    DELTA_FLUSH_SECONDS,
    NO_REPLY_FALLBACK,
    SupervisorOutcome,
    _DeltaBatch,
    _outcome_from,
    _stream_decision,
    supervisor_user_turn,
)

# Compat re-exports for tests that still import these from supervisor.
MAX_ASK_ATTEMPTS = _agents_sdk.MAX_ASK_ATTEMPTS
_TURN_EVENT_STATUS = _agents_sdk._TURN_EVENT_STATUS
_confirmation_pause = _agents_sdk._confirmation_pause
_exhausted = _agents_sdk._exhausted
_history_turns = _agents_sdk._history_turns
_ledger_answer = _agents_sdk._ledger_answer
_merged_domain_context = _agents_sdk._merged_domain_context
_new_question_id = _agents_sdk._new_question_id
domain_scope = _agents_sdk.domain_scope

__all__ = [
    "DELTA_FLUSH_CHARS",
    "DELTA_FLUSH_SECONDS",
    "DONE",
    "MAX_ASK_ATTEMPTS",
    "MAX_DOMAIN_TURNS",
    "NO_REPLY_FALLBACK",
    "NO_TOOLS_SUMMARY",
    "OVERFLOW_UPSTREAM_CODE",
    "TERMINAL_ABORTED_STREAMING",
    "TERMINAL_API_ERROR",
    "TERMINAL_COMPLETED",
    "TERMINAL_MAX_TURNS",
    "SupervisorOutcome",
    "_DeltaBatch",
    "_TURN_EVENT_STATUS",
    "_already_concluded",
    "_closing_reply",
    "_collect_business_ids",
    "_confirmation_pause",
    "_exhausted",
    "_final_from",
    "_history_turns",
    "_ledger_answer",
    "_merged_domain_context",
    "_new_question_id",
    "_outcome_from",
    "_stream_decision",
    "build_domain_graph",
    "build_supervisor",
    "domain_scope",
    "logger",
    "reset_goal_node",
    "supervisor_node",
    "supervisor_route",
    "supervisor_user_turn",
]
