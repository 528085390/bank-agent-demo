"""The Agent's audit domain.

Two append-only tables and the code that writes them: every model interaction, and every
fact about a run that is worth tracing. Nothing here is a conversation and nothing here is
execution state -- the message ledger holds what the user saw, the LangGraph checkpoint
holds what a run may do next, and these records hold what actually happened between the
process and a provider or a tool.
"""

from app.audit.context import AuditContext, audit_context, current_context
from app.audit.service import (
    AuditReader,
    AuditService,
    RunEventType,
    jsonable,
)

__all__ = [
    "AuditContext",
    "AuditReader",
    "AuditService",
    "RunEventType",
    "audit_context",
    "current_context",
    "jsonable",
]
