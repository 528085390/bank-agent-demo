"""The Gateway: identity binding, tool existence, argument schema, context injection.

It performs no business logic and knows no business names. Three checks, in order: the
request must name a tool this handle carries, the arguments must satisfy that tool's
declared schema, and only then does the call reach the adapter -- carrying the handle's
trusted identity, never anything the model supplied.

It is also the last place a tool call is a fact rather than a projection. The SSE frames the
graph publishes are a whitelist view for a browser; the audit rows written here hold the
model's own arguments and the adapter's complete result, `facts` included. Both happen, and
neither is derived from the other.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, replace
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from app.audit.service import AuditService, RunEventType
from app.tools.catalog import ToolCatalog
from app.tools.handle import AgentToolHandle
from app.tools.normalize import normalize_arguments
from app.tools.protocol import (
    ErrorCode,
    ToolAdapter,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TrustedContext,
)

logger = logging.getLogger(__name__)


def _complaint(error: ValidationError) -> str:
    """Where an argument broke its schema: path, rule and the key names, never a value.

    jsonschema's own `message` embeds a `repr` of the offending value, so it is never used.
    Two validators carry no path at the root -- `required` and `additionalProperties` -- and
    both are about *names*, so the names are read out of the schema rather than out of the
    message. Every other validator's `path` already names the offending key.

    `bank_adapter._facts_violation` refuses the same leak for a tool's `facts`, and it is not
    reused here. It describes one fixed field of an output envelope as `facts.<path>:
    <validator>`; this one describes the model's own input as `argument <path>: ...` and has
    to spell out the names behind the two root-level validators, which `_facts_violation`
    never has to. Its output is pinned read-only by `tests/test_output_schema.py`, so folding
    the two into one shape would change a checked string to serve a second caller.
    """

    location = ".".join(str(item) for item in error.path) or "(root)"
    if error.validator == "required":
        names = ", ".join(str(name) for name in error.validator_value)
        return f"argument {location}: missing {names}"
    if error.validator == "additionalProperties":
        if not isinstance(error.instance, Mapping):
            return f"argument {location}: failed additionalProperties"
        declared = set(error.schema.get("properties", {}))
        names = sorted(str(name) for name in set(error.instance) - declared)
        return f"argument {location}: unexpected {', '.join(names)}"
    return f"argument {location}: failed {error.validator}"


def _denied(tool_name: str | None, call_id: str | None, reason: str) -> ToolResult:
    """Refuse a call before any adapter sees it.

    `reason` is internal English -- a catalog refusal or a jsonschema complaint -- and the
    seam says a result's `error` is the one sentence the user reads (contract 4.6.2), so the
    reason goes to the log instead of the frame. It is a developer/ops diagnostic, so it is
    logged rather than dropped: the client gets the shared sentence, the server keeps the
    specific one.

    The reason *is* kept in the audit row, because there it is exactly what an operator
    needs and there is no browser in front of it. The reason itself is de-valued, though: a
    schema complaint comes from `_complaint`, which reports the path, the rule and the key
    names and never the argument, so no model-supplied value reaches this log line. The value
    is not lost -- it is in the audit row's `arguments`.
    """

    # Imported here rather than at module scope: `app.modules.base` imports
    # `app.agents.declaration`, which imports `app.tools.handle`, which executes this
    # package's `__init__` -- so a module-scope import of the message table closes a cycle.
    from app.modules.base import ERROR_MESSAGES

    logger.warning("tool call denied call_id=%s: %s", call_id, reason)
    return ToolResult(
        tool_name=tool_name or "",
        status=ToolStatus.DENIED,
        call_id=call_id,
        error=ERROR_MESSAGES[ErrorCode.TOOL_DENIED],
        # A refusal is a distinct outcome for the client, and it never reaches an adapter,
        # so the code is attached here rather than in the adapter's mapping.
        error_code=ErrorCode.TOOL_DENIED,
    )


def _trusted_context(context: TrustedContext) -> dict[str, Any]:
    """The identity the server injected, as the audit record spells it."""

    return {
        "user_id": context.user_id,
        "session_id": context.session_id,
        "bank_customer_id": context.bank_customer_id,
    }


def _result_payload(result: ToolResult) -> dict[str, Any]:
    """The complete result, with nothing projected away.

    `asdict` rather than the SSE envelope: the frame the browser sees deliberately drops
    `facts`, and this is the one place that must not. The `ToolStatus` member is left in the
    dict and converted by the audit serializer, which is what keeps this function free of
    its own copy of the status spelling.
    """

    return asdict(result)


class ToolGateway:
    """Turns a raw model request into a checked adapter call."""

    def __init__(
        self, catalog: ToolCatalog, adapter: ToolAdapter, audit: AuditService | None = None
    ) -> None:
        self._catalog = catalog
        self._adapter = adapter
        # Optional for a test that builds a gateway on its own, injected in production. The
        # writes read the current audit context, so a tool event lands in the turn that
        # caused it without the graph having to pass anything down.
        self._audit = audit
        self._validators: dict[str, Draft202012Validator] = {}

    def handle(self, agent_id: str, context: TrustedContext) -> AgentToolHandle:
        """Build the one handle an agent gets.

        An agent the catalog does not grant anything to still receives a handle; it
        simply carries no tools, so every call through it is denied by the same path
        as any other out-of-catalog tool.
        """

        granted = self._catalog.for_agent(agent_id)
        tools = granted.specs if granted is not None else ()
        return AgentToolHandle(
            agent_id=agent_id,
            tools=tools,
            trusted_context=context,
            gateway=self,
        )

    def invoke(self, handle: AgentToolHandle, request: Mapping[str, Any]) -> ToolResult:
        if not isinstance(request, Mapping):
            return self._refuse(None, None, "tool request must be an object", request)

        call_id = request.get("call_id")
        call_id = call_id if isinstance(call_id, str) else None

        idempotency_key = request.get("idempotency_key")
        idempotency_key = idempotency_key if isinstance(idempotency_key, str) else None

        tool_name = request.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return self._refuse(
                None, call_id, "tool request must name a tool", request, agent_id=handle.agent_id
            )

        # The handle's own catalog decides. A request's `agent` field is never read.
        spec = handle.spec(tool_name)
        if spec is None:
            return self._refuse(
                tool_name,
                call_id,
                f"{handle.agent_id} may not call {tool_name}",
                request,
                agent_id=handle.agent_id,
            )

        raw_arguments = request.get("arguments")
        if raw_arguments is not None and not isinstance(raw_arguments, Mapping):
            return self._refuse(
                tool_name,
                call_id,
                "arguments must be an object",
                request,
                agent_id=handle.agent_id,
            )
        arguments = normalize_arguments(raw_arguments)

        invalid = self._validate(spec, arguments)
        if invalid is not None:
            return self._refuse(tool_name, call_id, invalid, request, agent_id=handle.agent_id)

        call = ToolCall(
            tool_name=tool_name,
            arguments=arguments,
            call_id=call_id,
            idempotency_key=idempotency_key,
        )
        invocation = ToolInvocation(
            agent_id=handle.agent_id, call=call, context=handle.trusted_context
        )
        # Written before the adapter is handed anything, so a call that crashes the process
        # still leaves the fact that it was attempted, with the arguments it was attempted
        # with.
        self._record_call(handle, invocation)
        # The server minted this id, and the client pairs `tool_call` with `tool_result` by
        # it. Nothing in the tool contract makes an adapter echo it back, so a result that
        # reports nothing -- or someone else's id -- is stamped with the one that was
        # actually sent rather than trusted. The record is stamped with the same value, so
        # the audit row and the frame name one call.
        result = replace(self._adapter.invoke(invocation), call_id=call_id)
        self._record_result(handle, invocation, result)
        return result

    def _refuse(
        self,
        tool_name: str | None,
        call_id: str | None,
        reason: str,
        request: Mapping[str, Any] | None = None,
        *,
        agent_id: str | None = None,
    ) -> ToolResult:
        """Refuse a call, and record that a call was refused and why.

        A refusal is a real outcome and an interesting one: it says the model asked for
        something the catalog or the schema does not allow. It reaches no adapter, so this is
        the only place it can be written down.
        """

        result = _denied(tool_name, call_id, reason)
        arguments = request.get("arguments") if isinstance(request, Mapping) else None
        self._record_event(
            event_type=RunEventType.TOOL_RESULT,
            status=ToolStatus.DENIED.value,
            tool_name=tool_name,
            call_id=call_id,
            agent_id=agent_id,
            payload={
                "agent_id": agent_id,
                "tool": tool_name,
                "call_id": call_id,
                "arguments": dict(arguments) if isinstance(arguments, Mapping) else None,
                "reason": reason,
                "result": _result_payload(result),
            },
            error_code=ErrorCode.TOOL_DENIED.value,
        )
        return result

    def _record_call(self, handle: AgentToolHandle, invocation: ToolInvocation) -> None:
        call = invocation.call
        self._record_event(
            event_type=RunEventType.TOOL_CALL,
            status=None,
            tool_name=call.tool_name,
            call_id=call.call_id,
            agent_id=handle.agent_id,
            payload={
                "agent_id": handle.agent_id,
                "tool": call.tool_name,
                "call_id": call.call_id,
                "arguments": dict(call.arguments),
                # The server's own injections, kept because they are part of what the call
                # actually was: which identity it carried, and which intent it repeated.
                "idempotency_key": call.idempotency_key,
                "trusted_context": _trusted_context(invocation.context),
            },
        )

    def _record_result(
        self, handle: AgentToolHandle, invocation: ToolInvocation, result: ToolResult
    ) -> None:
        call = invocation.call
        self._record_event(
            event_type=RunEventType.TOOL_RESULT,
            status=result.status.value,
            tool_name=result.tool_name or call.tool_name,
            call_id=result.call_id or call.call_id,
            agent_id=handle.agent_id,
            payload={
                "agent_id": handle.agent_id,
                "tool": result.tool_name or call.tool_name,
                "call_id": result.call_id or call.call_id,
                "arguments": dict(call.arguments),
                "idempotency_key": call.idempotency_key,
                "trusted_context": _trusted_context(invocation.context),
                "result": _result_payload(result),
            },
            error_code=result.error_code,
        )

    def _record_event(
        self,
        *,
        event_type: RunEventType,
        status: str | None,
        tool_name: str | None,
        call_id: str | None,
        agent_id: str | None,
        payload: dict[str, Any],
        error_code: str | None = None,
    ) -> None:
        """Append one tool event, if this gateway was given somewhere to append it.

        A write that fails propagates. The audit spec is explicit that a run must not quietly
        degrade into "only the log knows", so a gateway that cannot record a tool call fails
        the call rather than performing it untraceably.
        """

        if self._audit is None:
            return
        self._audit.record_agent_event(
            event_type=event_type,
            payload=payload,
            status=status,
            agent_id=agent_id,
            tool_name=tool_name,
            call_id=call_id,
            error_code=error_code,
        )

    def _validate(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> str | None:
        """Return the first schema complaint, or None when the arguments are acceptable."""

        if not spec.parameters:
            return None
        validator = self._validators.get(spec.name)
        if validator is None:
            try:
                validator = Draft202012Validator(spec.parameters)
            except SchemaError as exc:
                # A malformed declaration is the caller's bug, not the model's.
                raise ValueError(f"tool {spec.name} declares an invalid schema: {exc}") from exc
            self._validators[spec.name] = validator
        errors = sorted(validator.iter_errors(dict(arguments)), key=lambda error: list(error.path))
        if not errors:
            return None
        first = errors[0]
        return _complaint(first)
