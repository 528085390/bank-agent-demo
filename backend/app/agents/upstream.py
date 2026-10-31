"""What the graph does when the upstream fails.

The model and the tools are the only two things a turn cannot do without, and both can
fail transiently. The rule is deliberately narrow: a failure the boundary explicitly
marked transient is retried exactly once, inside the invocation that hit it. Everything
else fails immediately -- repeating an authentication, argument or contract error only
repeats it, and repeating a tool call can repeat its side effect.

The retry count is a local variable, never state. It does not survive the invocation, so
there is no failure counter in the checkpoint and nothing to reconcile afterwards.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from app.core.errors import AppError
from app.integration.model_client import FeatureUnsupported, ModelClientError

T = TypeVar("T")

UPSTREAM_ERROR_CODE = "LLM_UPSTREAM_ERROR"

# A transient outage: the same request is worth sending again later.
UPSTREAM_UNAVAILABLE_MESSAGE = "模型服务暂时不可用，请稍后重试"

# A rejected request: sending the same one again cannot help, so do not suggest it.
UPSTREAM_REJECTED_MESSAGE = "模型服务调用失败，请检查模型配置"

# Shown when a question is re-presented because the upstream failed, not the user. The
# client can answer with the very same value it already gave.
UPSTREAM_RETRY_NOTICE = "暂时无法处理，请重试。"


class UpstreamUnavailable(AppError):
    """A transient failure that survived its one retry inside this invocation.

    It is an `AppError`, so it leaves the graph as a terminal SSE error without the
    transport layer having to know anything about providers.
    """

    def __init__(self, upstream_code: str) -> None:
        super().__init__(
            UPSTREAM_ERROR_CODE,
            UPSTREAM_UNAVAILABLE_MESSAGE,
            502,
            retryable=True,
            details={"upstream_code": upstream_code},
        )


def upstream_app_error(error: ModelClientError) -> AppError:
    """A non-retryable boundary failure, reported as an upstream error and not retried."""

    return AppError(
        UPSTREAM_ERROR_CODE,
        UPSTREAM_UNAVAILABLE_MESSAGE if error.retryable else UPSTREAM_REJECTED_MESSAGE,
        502,
        retryable=error.retryable,
        details={"upstream_code": error.code},
    )


def tools_unsupported_error(error: FeatureUnsupported) -> AppError:
    """A provider that cannot call tools at all.

    This is the whole answer to "what if the upstream has no tools": there is exactly one way
    to make a decision, so a provider that cannot do it ends the turn. It is not retryable --
    the same request would be refused the same way -- and it says which feature was missing so
    the user knows what to change (spec §4.5).
    """

    return AppError(
        UPSTREAM_ERROR_CODE,
        f"当前模型接口不支持工具调用（{error.feature}），无法继续办理。请更换模型或接口。",
        502,
        retryable=False,
        details={"upstream_code": "LLM_TOOLS_UNSUPPORTED", "feature": error.feature},
    )


def call_with_one_retry(operation: Callable[[], T]) -> T:
    """Run `operation`, retrying one explicitly transient failure in this same invocation.

    Only `ModelClientError` is this helper's business. Anything else -- including the
    `AppError` the unconfigured model raises -- passes straight through, because it is not
    a transient boundary failure and retrying it would be guessing.
    """

    try:
        return operation()
    except ModelClientError as error:
        if not error.retryable:
            raise upstream_app_error(error) from error
        try:
            return operation()
        except ModelClientError as second:
            # The retry can fail differently, and the *second* verdict is the one the
            # client acts on. Calling an auth rejection "temporarily unavailable" because
            # the first attempt timed out would tell them to resend a request that was
            # just refused on its merits.
            if not second.retryable:
                raise upstream_app_error(second) from second
            raise UpstreamUnavailable(second.code) from second
