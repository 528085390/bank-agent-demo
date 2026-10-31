"""Conversation persistence: the ledger, its idempotency receipts and the turn lifecycle.

The package is the service layer the HTTP routes and the Agent runtime meet at. It knows
about conversations, messages and submissions; it does not know about HTTP, and it does not
project graph state -- the LangGraph checkpoint stays the only execution state there is.

The re-exports are eager, unlike `app.agents`: nothing here is imported by the module
packages, so there is no cycle for the lazy form to break.
"""

from app.conversation.service import (
    ConversationService,
    ConversationView,
    MessageView,
    Receipt,
    request_hash,
    thread_id_for_conversation,
)

__all__ = [
    "ConversationService",
    "ConversationView",
    "MessageView",
    "Receipt",
    "request_hash",
    "thread_id_for_conversation",
]
