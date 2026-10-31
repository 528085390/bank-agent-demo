"""The production adapter: verified calls reach a module's handler.

It is the only place that knows handlers exist. It never retries, it validates the
envelope a handler returned, and it turns a failure into a result the client can read
without ever leaking an exception's text or the bank's own English message.
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.integration.bank_client import BankClient
from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError
from app.modules.base import ERROR_MESSAGES, ToolFailure, as_envelope
from app.modules.registry import Registry
from app.tools.protocol import (
    ErrorCode,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    ToolStatus,
)

logger = logging.getLogger(__name__)


class BankToolAdapter:
    def __init__(self, registry: Registry, client: BankClient) -> None:
        self._handlers = dict(registry.handlers)
        self._specs = {
            name: spec for module in registry.modules for name, spec in module.tools.items()
        }
        self._client = client
        # Keyed by tool name on this adapter, not in a module-level cache: a global keyed by
        # name alone would hand a second registry's same-named tool the first one's schema.
        self._validators: dict[str, Draft202012Validator] = {}

    def invoke(self, invocation: ToolInvocation) -> ToolResult:
        name = invocation.call.tool_name
        handler = self._handlers.get(name)
        if handler is None:
            # The Gateway should have refused this. It is refused here too, because this
            # adapter cannot assume every caller went through the Gateway.
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error=f"没有名为 {name} 的工具",
                error_code=ErrorCode.NOT_FOUND,
            )

        spec = self._specs.get(name)
        if spec is not None and spec.side_effect == SideEffect.WRITE:
            if not invocation.call.idempotency_key:
                # Fail closed, and before the handler runs: an unkeyed write cannot be
                # retried safely, so it is not attempted at all. The Supervisor always mints
                # a key, so this defends against a caller that did not go through it.
                logger.warning("tool %s is a write and carries no idempotency key", name)
                return ToolResult(
                    tool_name=name,
                    status=ToolStatus.FAILED,
                    call_id=invocation.call.call_id,
                    error=ERROR_MESSAGES[ErrorCode.IDEMPOTENCY_KEY_REQUIRED],
                    error_code=ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                )

        try:
            returned = handler(invocation, self._client)
        except ToolFailure as failure:
            logger.info("tool %s failed: %s", name, failure.code)
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error=failure.message,
                retryable=failure.retryable,
                error_code=failure.code,
            )
        except BankCallError as failure:
            logger.warning("tool %s: bank call failed (%s)", name, failure.kind)
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error=failure.message,
                retryable=failure.retryable,
                error_code=KIND_TO_ERROR_CODE[failure.kind],
            )
        except Exception as error:
            # A programming error or an unexpected upstream shape. The text may carry
            # business data or credentials, so it goes neither to the client nor to the
            # log: the client gets a Chinese refusal, and the log gets only the type.
            logger.error("tool %s raised: %s", name, type(error).__name__)
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error="工具内部错误",
                error_code=ErrorCode.INTERNAL,
            )

        # Deliberately outside the `try`: a handler that returns something other than
        # `ToolData` is a declaration error, not a business failure, so it raises rather
        # than becoming a readable FAILED result. Same stance the Gateway takes on an
        # illegal tool schema. The `facts` check below runs only once this has succeeded,
        # so a malformed envelope is never reported as an `OUTPUT_SCHEMA_VIOLATION`.
        envelope = as_envelope(returned, tool_name=name)

        violation = self._facts_violation(self._specs.get(name), envelope)
        if violation is not None:
            logger.warning("tool %s produced facts that violate its schema: %s", name, violation)
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error=ERROR_MESSAGES[ErrorCode.OUTPUT_SCHEMA_VIOLATION],
                error_code=ErrorCode.OUTPUT_SCHEMA_VIOLATION,
            )

        return ToolResult(
            tool_name=name,
            status=ToolStatus.SUCCEEDED,
            call_id=invocation.call.call_id,
            data=envelope,
        )

    def _facts_violation(self, spec: ToolSpec | None, envelope: dict[str, Any]) -> str | None:
        """Where `facts` broke its schema: the path and the failing keyword, never the value.

        Only `facts` is checked: `summary`, `display` and `business_ids` are pinned by the
        seam, and `facts` is the one field a module designs freely -- so it is the one whose
        shape has to be caught where it is produced rather than where it is later read.

        The description is built from `validator` and `path` rather than from `message`
        because jsonschema's messages embed a `repr` of the offending value -- so logging
        one would put a module's private `facts` into the server log, which is the one
        audience `facts` is allowed to reach. The path names keys, and the keyword names the
        rule; together they say what broke without repeating what it held.
        """

        if spec is None or spec.output_schema is None:
            return None
        validator = self._validators.get(spec.name)
        if validator is None:
            try:
                validator = Draft202012Validator(spec.output_schema)
            except SchemaError as exc:
                # `build_registry` already refuses this, so reaching it means the adapter
                # was handed a hand-built registry. A declaration bug either way.
                raise ValueError(f"invalid output_schema for tool {spec.name}: {exc}") from exc
            self._validators[spec.name] = validator
        errors = sorted(
            validator.iter_errors(envelope.get("facts") or {}),
            key=lambda error: list(error.path),
        )
        if not errors:
            return None
        first = errors[0]
        location = ".".join(str(item) for item in first.path) or "(root)"
        return f"facts.{location}: {first.validator}"
