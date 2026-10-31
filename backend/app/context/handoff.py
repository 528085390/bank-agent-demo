"""The memory handoff: what a module-owned graph may see of the conversation.

A module that answers a request with its own graph is handed the caller's identity, the model
of the moment and the user's own words -- and it owns its nodes, so the base cannot open the
view for it. The node opens it itself, for the duration of one synchronous call:

    with domain_scope(spec, state, assembler, turn, message) as handoff:
        ...

and reads it back inside that same call stack:

    view = handoff.view(prompt_overhead=...)

The view is not a fourth argument: it is the earlier rounds, the summary that covers them, and
the current goal, published as an immutable, budget-priced object.

Two properties are structural rather than promised:

- **The scope is a `ContextVar`, and it is always reset.** A `with` block cannot be skipped by
  an exception, and a `ContextVar` is per-task: two concurrent requests cannot see each other's
  memory, and a request that crashes leaves nothing behind for the next one. No process-global
  dict and no "current conversation" field on a shared object, because both of those leak
  across callers exactly when the server is busy.
- **Memory carries no authority.** The view holds words and business references; the write-back
  below accepts a closed set of semantic fields and nothing else. A confirmation, a
  verification code or an idempotency key has no field to travel in, so "restore the approval
  from the summary" is not a rule the module has to remember -- there is no code path for it.

The view is assembled lazily, by the module, because only the module knows how large its own
prompt is: it renders the part of its prompt that is not history, hands that text in as
`prompt_overhead`, and gets back a view that fits the budget with that overhead already paid
for. Assembling it here instead would price the domain's prompt with the Supervisor's overhead
and quietly overrun the window.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_UPDATE_KEYS",
    "MAX_CONTEXT_RECORDS",
    "DomainMessage",
    "DomainView",
    "Handoff",
    "conversation_scope",
    "current_handoff",
    "merge_context_records",
    "validate_context_update",
]

# What a domain may hand back as memory. A closed set, and every entry is a *semantic clue*:
# what the user was trying to do, what has been answered, what was shown, which records were
# mentioned. Confirmation, verification and idempotency fields are not here, and adding one
# would be the bug this whitelist exists to prevent.
CONTEXT_UPDATE_KEYS = frozenset({"goal", "answered", "shown", "references"})

# Bounds on one write-back. Memory is bounded by construction: an unbounded channel would let a
# single turn pin an arbitrary amount of text into the checkpoint forever.
MAX_UPDATE_TEXT = 400
MAX_UPDATE_ITEMS = 24

# How many write-back records one agent may keep. The newest win; older ones are dropped rather
# than merged, because a merge would let a correction be undone by an older record.
MAX_CONTEXT_RECORDS = 8

# A view never carries more than this many retained messages, whatever the ledger holds. The
# budget already cuts by tokens; this is the second bound, so a pathological run of one-character
# messages cannot produce a transcript of ten thousand lines.
MAX_VIEW_MESSAGES = 40


@dataclass(frozen=True, slots=True)
class DomainMessage:
    """One retained round, in the shape a domain prompt can render."""

    role: str
    content: str
    message_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class DomainView:
    """The conversation as one domain call may see it.

    `summary_text` covers everything older than `recent_messages`; it is history, not
    instruction, and a domain prompt must label it as such. `history_text` is the retained
    rounds already rendered (`role: content` lines, empty when there are none) -- on the ledger
    path it is built from the same rows the Supervisor's own prompt shows, and on the session
    path it is the checkpoint transcript, which is what `no_conversation` degrades to.

    **The message being answered is not in either.** It reaches the module as the `message`
    argument of `domain_scope`, and putting it in the history as well would show the same
    sentence twice in one prompt -- once as the ask and once as something the user said
    earlier. The ledger path drops it by sequence; the session path drops it by the supervisor
    trimming the transcript before handing it over.

    `domain_context` is this agent's own bounded memory from earlier turns, already stripped to
    the whitelist above; `context_sources` says, per field, which ledger message it came from.
    Both are empty when the conversation has never written any.
    """

    conversation_id: str | None
    current_message_id: str | None
    current_sequence: int
    current_user_text: str
    summary_text: str | None
    history_text: str
    recent_messages: tuple[DomainMessage, ...]
    domain_context: Mapping[str, Any]
    context_sources: Mapping[str, Any]
    reason: str
    estimated_tokens: int = 0


class Assembler(Protocol):
    """The slice of `app.context.view.ContextAssembler` this module uses.

    Structural rather than imported: `app.context.view` imports `app.agents.prompts`, and a
    domain package importing this module must not drag that in.
    """

    def assemble(
        self,
        *,
        conversation_id: str | None,
        current_sequence: int,
        prompt_overhead: str,
        transcript_fallback: str = "",
        aggressive: bool = False,
    ) -> Any: ...


@dataclass(slots=True)
class Handoff:
    """One domain call's memory scope: an immutable view plus a bounded write-back collector.

    Built by `app.agents.supervisor` per call and never stored. `view()` is memoized per
    overhead string, so a module that renders its prompt twice assembles once -- a second
    assembly would record a second `CONTEXT_ASSEMBLED` event and could trigger a second
    summary call for the same turn.
    """

    conversation_id: str | None
    current_message_id: str | None
    current_sequence: int
    current_user_text: str
    agent_id: str
    assembler: Assembler | None = None
    transcript_fallback: str = ""
    # The raw records the outer layer keeps for this agent, envelope included. The envelope is
    # what the checkpoint stores (payload plus the ledger message it came from); the view hands
    # the module the flattened payload and keeps the provenance beside it.
    domain_records: Sequence[Mapping[str, Any]] = ()
    _views: dict[str, DomainView] = field(default_factory=dict, init=False, repr=False)
    _updates: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def view(self, *, prompt_overhead: str) -> DomainView:
        """Assemble the conversation view with `prompt_overhead` already paid for.

        Raises whatever the assembler raises: an assembly that cannot fit the window is a real
        failure, and answering it by dropping history would be the silent truncation this whole
        design exists to avoid.
        """

        cached = self._views.get(prompt_overhead)
        if cached is not None:
            return cached

        merged, sources = merge_context_records(self.domain_records)
        assembled = self._assemble(prompt_overhead, merged, sources)
        self._views[prompt_overhead] = assembled
        return assembled

    def _assemble(
        self,
        prompt_overhead: str,
        merged: Mapping[str, Any],
        sources: Mapping[str, Any],
    ) -> DomainView:
        if self.conversation_id is None or self.assembler is None:
            # The session path: there is no ledger to read, so the checkpoint transcript is the
            # whole view and its cost is what the prompt will really carry. Reported as
            # `no_conversation` so a caller can tell the two paths apart (spec §8.2).
            from app.context.estimator import estimate_tokens

            return DomainView(
                conversation_id=None,
                current_message_id=self.current_message_id,
                current_sequence=self.current_sequence,
                current_user_text=self.current_user_text,
                summary_text=None,
                history_text=self.transcript_fallback,
                recent_messages=(),
                domain_context=merged,
                context_sources=sources,
                reason="no_conversation",
                estimated_tokens=estimate_tokens(prompt_overhead)
                + estimate_tokens(self.transcript_fallback),
            )

        view = self.assembler.assemble(
            conversation_id=self.conversation_id,
            current_sequence=self.current_sequence,
            prompt_overhead=prompt_overhead,
            transcript_fallback=self.transcript_fallback,
        )
        # The ledger read reaches up to and including the message being answered; that message
        # is handed to the module separately, so it is dropped here rather than shown twice.
        rows = tuple(
            row for row in (getattr(view, "rows", ()) or ()) if row.sequence < self.current_sequence
        )
        messages = tuple(
            DomainMessage(
                role=row.role,
                content=row.content,
                message_id=getattr(row, "message_id", None),
                sequence=row.sequence,
            )
            for row in rows[-MAX_VIEW_MESSAGES:]
        )
        return DomainView(
            conversation_id=self.conversation_id,
            current_message_id=self.current_message_id,
            current_sequence=self.current_sequence,
            current_user_text=self.current_user_text,
            summary_text=view.summary_text,
            history_text=_render(messages),
            recent_messages=messages,
            domain_context=merged,
            context_sources=sources,
            reason=view.reason,
            estimated_tokens=view.estimated_tokens,
        )

    def publish(self, payload: Mapping[str, Any] | None) -> None:
        """Collect one structured memory update; invalid payloads are dropped, not raised.

        Dropping rather than raising is the honest failure mode: memory is an optimisation, and
        a module that computes a slightly malformed clue must not have its whole answer thrown
        away. The record that *is* accepted is stamped with where it came from.
        """

        if payload is None:
            return
        try:
            accepted = validate_context_update(payload)
        except ValueError as error:
            logger.warning("domain handoff: dropped an unusable memory update (%s)", error)
            return
        if not accepted:
            return
        self._updates.append(
            {
                "payload": accepted,
                "source": {
                    "message_id": self.current_message_id,
                    "sequence": self.current_sequence,
                },
            }
        )

    def updates(self) -> tuple[dict[str, Any], ...]:
        """The accepted write-backs, in the order they were published."""

        return tuple(self._updates)


def merge_context_records(
    records: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten stored memory records into the fields a domain prompt reads, newest wins.

    The checkpoint keeps the envelope -- `{"payload": {...}, "source": {...}}` -- because a
    reference nobody can trace back to a ledger message is not memory, it is a rumour. A domain
    prompt wants the fields themselves, so they are flattened here, and the per-field source is
    returned beside them rather than thrown away.
    """

    fields: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        source = record.get("source")
        for key, value in payload.items():
            if key not in CONTEXT_UPDATE_KEYS:
                continue
            fields[key] = value
            sources[key] = dict(source) if isinstance(source, Mapping) else {}
    return fields, sources


def _render(messages: Sequence[DomainMessage]) -> str:
    """The retained rounds as `role: content` lines, or an empty string when there are none.

    Deliberately a local four-liner rather than `app.agents.prompts.describe_transcript`: this
    module is imported by a domain package, and reaching into `app.agents` from here would make
    the domain's import graph depend on the Supervisor's prompt layer. It is also not quite the
    same function -- that one answers "（暂无）" for an empty transcript, and whether an empty
    history reads as a sentence is the prompt layer's decision, not this one's.
    """

    return "\n".join(f"{message.role}: {message.content}" for message in messages)


def validate_context_update(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Check one memory update against the whitelist; raise `ValueError` when it cannot be kept.

    The rules are mechanical and deliberately narrow. A key outside `CONTEXT_UPDATE_KEYS` is a
    refusal, not a silent drop, so a module that tries to remember an authorization gets a
    visible warning rather than a memory that quietly stops working.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("记忆更新必须是对象")
    unknown = sorted(str(key) for key in payload if key not in CONTEXT_UPDATE_KEYS)
    if unknown:
        raise ValueError("记忆更新含有不允许的字段：" + "、".join(unknown))

    accepted: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "goal":
            text = value.strip() if isinstance(value, str) else ""
            if not text:
                continue
            accepted["goal"] = text[:MAX_UPDATE_TEXT]
            continue
        if key == "answered":
            accepted["answered"] = _bounded_map(value, text_only=True)
            continue
        if key == "shown":
            accepted["shown"] = _bounded_map(value, text_only=False)
            continue
        accepted["references"] = _bounded_map(value, text_only=True)
    return {key: value for key, value in accepted.items() if value}


def _bounded_map(value: Any, *, text_only: bool) -> dict[str, Any]:
    """A bounded string→(string | list of strings) map, truncated and deduplicated by key."""

    if not isinstance(value, Mapping):
        raise ValueError("记忆更新的值必须是对象")
    bounded: dict[str, Any] = {}
    for name, item in list(value.items())[:MAX_UPDATE_ITEMS]:
        if not isinstance(name, str) or not name.strip():
            continue
        if text_only:
            if isinstance(item, str) and item.strip():
                bounded[name.strip()] = item.strip()[:MAX_UPDATE_TEXT]
            continue
        if isinstance(item, str):
            raise ValueError("shown 的值必须是编号列表")
        if not isinstance(item, Sequence) or isinstance(item, (bytes, bytearray)):
            raise ValueError("shown 的值必须是编号列表")
        identifiers = [
            entry.strip()[:MAX_UPDATE_TEXT]
            for entry in item[:MAX_UPDATE_ITEMS]
            if isinstance(entry, str) and entry.strip()
        ]
        if identifiers:
            bounded[name.strip()] = identifiers
    return bounded


# ---------------------------------------------------------------------- 调用作用域

_SCOPE: ContextVar[Handoff | None] = ContextVar("domain_conversation_handoff", default=None)


@contextmanager
def conversation_scope(handoff: Handoff) -> Iterator[Handoff]:
    """Publish `handoff` for the duration of the block, and restore the previous scope after.

    `reset` in a `finally` is the whole cleanup story: the token restores whatever was there
    before, so a nested or re-entrant call cannot be left holding another request's memory.
    """

    token = _SCOPE.set(handoff)
    try:
        yield handoff
    finally:
        _SCOPE.reset(token)


def current_handoff() -> Handoff | None:
    """The scope this call runs in, or `None` for a caller that has no conversation.

    `None` is a normal answer, not an error: the standalone CLI drives the same module without
    a Supervisor, and it must keep working.
    """

    return _SCOPE.get()
