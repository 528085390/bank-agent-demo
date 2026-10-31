"""Where an audit record's correlation ids come from during one interaction.

A model call and the tool calls it causes happen deep inside a graph, far from the HTTP
request that knows which conversation and which run they belong to. Threading those ids
through every node signature would mean the graph carries audit plumbing it has no use for,
so they travel in a `ContextVar` instead: the turn sets it once, every boundary that writes
an audit record reads it, and the turn restores the previous value when it ends.

Two properties matter and are both tested:

- the context is restored, not merely overwritten, so a failure cannot leave one turn's
  conversation attached to the next request served by the same worker;
- a boundary that runs with no context at all -- a configuration probe, a startup probe --
  records `None` rather than borrowing whatever the last conversation set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

# The sources a record can name. They are spelled out because the value is part of what the
# audit trail claims: "who asked for this call" is the first question anyone reading it has.
SOURCE_CONVERSATION = "conversation"
SOURCE_SESSION_CHAT = "session_chat"
SOURCE_CONFIGURATION_TEST = "configuration_test"
SOURCE_CONFIGURATION_PROBE = "configuration_probe"
SOURCE_STARTUP_PROBE = "startup_probe"
SOURCE_CONTEXT_SUMMARY = "context_summary"

# The agents a call can be attributed to. `source` answers "what kind of call was this";
# `agent_id` answers "who initiated it" -- which is the question the audit trail has to
# answer before anyone can tell a domain's model calls apart from the Supervisor's.
AGENT_ID_SUPERVISOR = "supervisor"


@dataclass(frozen=True, slots=True)
class AuditContext:
    """The optional correlation of one interaction.

    Every field is optional on purpose: a model call from the settings panel belongs to no
    conversation, and a tool call that happens before a run id exists belongs to no run. A
    missing id is recorded as missing rather than invented.

    `request_id` is the HTTP layer's request id, the same value the response carries in
    `X-Request-Id`. It is not part of what a *model call* is correlated with -- a call made
    by a background job has no request behind it -- so only a business event reads it.
    """

    conversation_id: str | None = None
    run_id: str | None = None
    user_id: str | None = None
    source: str | None = None
    agent_id: str | None = None
    request_id: str | None = None

    def merged(self, **overrides: Any) -> AuditContext:
        """This context with some fields replaced, ignoring explicit `None` overrides.

        An override of `None` means "this caller does not know", not "clear the field", so a
        boundary that has a run id but no conversation id cannot silently drop the
        conversation the turn already established.
        """

        named = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **named) if named else self


_CURRENT: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


@contextmanager
def audit_context(context: AuditContext | None) -> Iterator[AuditContext | None]:
    """Bind `context` for the duration of the block, then put back what was there.

    Restoring the previous value is what makes this safe under reuse: a worker that serves
    the next request on the same thread, or the next turn of the same conversation, gets the
    previous value back rather than this turn's ids.

    It restores by setting the previous value rather than by resetting a token, and that is
    deliberate. A turn is an async generator, and an async generator that is abandoned -- the
    client disconnected -- is finalized by the event loop from a *different* context, where
    `Token.reset` raises "was created in a different Context". That exception would be raised
    inside a `finally`, which is the one place nothing may raise. Contexts here nest
    LIFO inside a single task, so putting the previous value back is equivalent.
    """

    previous = _CURRENT.get()
    _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.set(previous)


def current_context() -> AuditContext | None:
    """What this call belongs to, or `None` when it belongs to nothing."""

    return _CURRENT.get()


@contextmanager
def summary_context() -> Iterator[AuditContext]:
    """Scope a summarization call: same turn, but `source` names it as a summary.

    Only the source is replaced. A summary is a call the turn made, so its records must stay
    attributable to the conversation and run that caused it; dropping them would put an
    unattributed model call into the audit trail exactly when someone is trying to find out
    why a turn was compressed.

    It also has to work with no context at all -- a summary may be requested outside a turn --
    which is why the current value falls back to an empty context rather than assuming one.
    `merged` ignores `None`, so the fallback is what keeps this call total.
    """

    carries = (current_context() or AuditContext()).merged(source=SOURCE_CONTEXT_SUMMARY)
    with audit_context(carries):
        yield
