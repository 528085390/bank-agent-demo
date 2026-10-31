"""An in-memory adapter for tests: it records calls and answers with what it was told.

It is not a mock bank. It knows no business capability, returns no account, bill or
transfer data of its own, and only ever answers from the fixed replies a test
registered. Its whole job is to make the Gateway's decisions observable.
"""

from __future__ import annotations

from typing import Any

from app.tools.protocol import ToolInvocation, ToolResult, ToolStatus


class RecordingAdapter:
    """Records every invocation in order and replies with the registered payload."""

    def __init__(self, replies: dict[str, dict[str, Any]] | None = None) -> None:
        self._replies = dict(replies or {})
        self._invocations: list[ToolInvocation] = []

    @property
    def invocations(self) -> list[ToolInvocation]:
        return list(self._invocations)

    @property
    def tool_names(self) -> list[str]:
        return [invocation.call.tool_name for invocation in self._invocations]

    def register(self, tool_name: str, data: dict[str, Any] | None = None) -> None:
        self._replies[tool_name] = dict(data or {})

    def fail(self, tool_name: str, error: str, *, retryable: bool = False) -> None:
        """Register a failure. Transient failures have to be marked as such explicitly."""

        self._replies[tool_name] = {"__error__": error, "__retryable__": retryable}

    def clear(self) -> None:
        self._invocations.clear()

    def invoke(self, invocation: ToolInvocation) -> ToolResult:
        self._invocations.append(invocation)
        reply = self._replies.get(invocation.call.tool_name, {})
        error = reply.get("__error__")
        if error is not None:
            return ToolResult(
                tool_name=invocation.call.tool_name,
                status=ToolStatus.FAILED,
                call_id=invocation.call.call_id,
                error=error,
                retryable=bool(reply.get("__retryable__")),
            )
        return ToolResult(
            tool_name=invocation.call.tool_name,
            status=ToolStatus.SUCCEEDED,
            call_id=invocation.call.call_id,
            data=dict(reply),
        )
