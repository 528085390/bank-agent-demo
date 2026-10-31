"""How an upstream failure is classified, and how far the graph retries it.

Two layers, both without a database:

- `classify_provider_error` turns a provider exception into a stable code plus a
  retryability verdict. It keys on `status_code` and on the exception's own name, so it
  works for any OpenAI-shaped provider without importing one -- which matters because the
  Base URL is user-configurable.
- `call_with_one_retry` is the single place that decides whether to try again. A failure
  the boundary explicitly marked transient is retried exactly once inside the invocation
  that hit it; a non-retryable one is never retried, because repeating an authentication,
  argument or contract error only repeats it.

The verdicts are asserted as data rather than by calling a provider, so the table below is
the specification: if a code or a retryability flag changes, this test says so.
"""

import pytest

from app.agents.upstream import UpstreamUnavailable, call_with_one_retry
from app.core.errors import AppError
from app.integration.model_client import ModelClientError, classify_provider_error


class APITimeoutError(Exception):
    """Shaped like the provider's timeout: no status code, a telling class name."""


class APIConnectionError(Exception):
    """Shaped like the provider's connection failure."""


class RateLimitError(Exception):
    def __init__(self):
        super().__init__("slow down")
        self.status_code = 429


class InternalServerError(Exception):
    def __init__(self):
        super().__init__("upstream boom")
        self.status_code = 503


class AuthenticationError(Exception):
    def __init__(self):
        super().__init__("bad key")
        self.status_code = 401


class PermissionDeniedError(Exception):
    def __init__(self):
        super().__init__("no access")
        self.status_code = 403


class BadRequestError(Exception):
    def __init__(self):
        super().__init__("bad params")
        self.status_code = 400


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        # Transient: the same call can plausibly succeed a moment later.
        (APITimeoutError("timed out"), "LLM_TIMEOUT", True),
        (APIConnectionError("connection refused"), "LLM_CONNECTION", True),
        (RateLimitError(), "LLM_RATE_LIMITED", True),
        (InternalServerError(), "LLM_UPSTREAM_ERROR", True),
        # Not transient: the same call fails the same way, so retrying only repeats it.
        (AuthenticationError(), "LLM_AUTH", False),
        (PermissionDeniedError(), "LLM_PERMISSION", False),
        (BadRequestError(), "LLM_BAD_REQUEST", False),
        (ValueError("not even a provider error"), "LLM_UNKNOWN", False),
    ],
    ids=[
        "timeout",
        "connection",
        "429",
        "5xx",
        "auth",
        "permission",
        "bad-request",
        "unknown",
    ],
)
def test_provider_failures_are_classified_by_status_then_shape(error, code, retryable):
    classified = classify_provider_error(error)

    assert isinstance(classified, ModelClientError)
    assert classified.code == code
    assert classified.retryable is retryable


def test_the_classification_keeps_the_code_stable_and_the_detail_specific():
    """A code is a contract callers branch on; the provider's own wording is not."""

    coded = classify_provider_error(AuthenticationError())
    assert coded.code == "LLM_AUTH"
    assert "bad key" not in coded.code
    # When the provider gave a status, that is the most specific fact available.
    assert coded.detail == "HTTP 401"

    # A transport failure never reached a response, so its class name is all there is.
    transport = classify_provider_error(APIConnectionError("refused"))
    assert transport.code == "LLM_CONNECTION"
    assert transport.detail == "APIConnectionError"


def test_a_transient_failure_is_retried_once_inside_the_invocation():
    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelClientError("LLM_TIMEOUT", retryable=True)
        return "answered"

    assert call_with_one_retry(operation) == "answered"
    # Exactly one extra attempt: not a retry loop.
    assert len(attempts) == 2


def test_a_transient_failure_that_survives_its_retry_is_upstream_unavailable():
    """The caller gets one verdict to act on, not the raw provider exception."""

    attempts = []

    def operation():
        attempts.append(1)
        raise ModelClientError("LLM_TIMEOUT", retryable=True)

    with pytest.raises(UpstreamUnavailable) as failure:
        call_with_one_retry(operation)

    assert len(attempts) == 2
    assert failure.value.code == "LLM_UPSTREAM_ERROR"
    assert failure.value.status == 502
    # It is retryable for the *client*: sending the message again may well work.
    assert failure.value.retryable is True
    assert failure.value.details["upstream_code"] == "LLM_TIMEOUT"


def test_a_non_retryable_failure_is_not_retried_at_all():
    attempts = []

    def operation():
        attempts.append(1)
        raise ModelClientError("LLM_AUTH", retryable=False)

    with pytest.raises(AppError) as failure:
        call_with_one_retry(operation)

    assert len(attempts) == 1, "an auth failure must not be repeated"
    assert failure.value.code == "LLM_UPSTREAM_ERROR"
    # Not retryable for the client either: resending the same message cannot fix a key.
    assert failure.value.retryable is False
    assert failure.value.details["upstream_code"] == "LLM_AUTH"


def test_a_failure_that_is_not_a_boundary_failure_passes_through_untouched():
    """Only `ModelClientError` is this helper's business; everything else is not ours."""

    def operation():
        raise AppError("MODEL_NOT_CONFIGURED", "尚未配置模型", 503)

    with pytest.raises(AppError) as failure:
        call_with_one_retry(operation)

    assert failure.value.code == "MODEL_NOT_CONFIGURED"


# --- the second failure is classified on its own merits, not on the first one's ---


def test_a_transient_failure_followed_by_a_permanent_one_is_reported_as_permanent():
    """The retry can fail differently, and the second verdict is the one that matters.

    Reporting this as retryable would tell the client to resend a request that was just
    rejected on its merits -- the first failure's shape says nothing about the second's.
    """

    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelClientError("LLM_TIMEOUT", retryable=True)
        raise ModelClientError("LLM_AUTH", retryable=False)

    with pytest.raises(AppError) as failure:
        call_with_one_retry(operation)

    assert len(attempts) == 2
    assert failure.value.code == "LLM_UPSTREAM_ERROR"
    assert failure.value.retryable is False
    assert failure.value.details["upstream_code"] == "LLM_AUTH"


def test_a_transient_failure_followed_by_another_transient_one_stays_retryable():
    """Two timeouts in a row is still an outage, so the client may send the message again."""

    attempts = []

    def operation():
        attempts.append(1)
        raise ModelClientError("LLM_TIMEOUT", retryable=True)

    with pytest.raises(AppError) as failure:
        call_with_one_retry(operation)

    assert len(attempts) == 2
    assert failure.value.retryable is True
    assert failure.value.details["upstream_code"] == "LLM_TIMEOUT"


def test_a_transient_failure_followed_by_a_permanent_one_is_not_an_upstream_outage():
    """`UpstreamUnavailable` is specifically the "outage, try later" verdict."""

    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelClientError("LLM_CONNECTION", retryable=True)
        raise ModelClientError("LLM_BAD_REQUEST", retryable=False)

    with pytest.raises(AppError) as failure:
        call_with_one_retry(operation)

    assert not isinstance(failure.value, UpstreamUnavailable)
    assert failure.value.retryable is False


class Response:
    """Providers that wrap the status on a response object rather than on the exception."""

    def __init__(self, status_code):
        self.status_code = status_code


class WrappedProviderError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = Response(status_code)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(429, "LLM_RATE_LIMITED", True), (503, "LLM_UPSTREAM_ERROR", True), (401, "LLM_AUTH", False)],
)
def test_a_status_carried_on_the_response_object_is_still_read(status, code, retryable):
    """Not every provider puts the status on the exception, so both places are checked."""

    classified = classify_provider_error(WrappedProviderError(status))

    assert classified.code == code
    assert classified.retryable is retryable


def test_a_status_on_the_exception_wins_over_one_on_its_response():
    """When both are present the exception's own status is the more specific fact."""

    class Both(Exception):
        def __init__(self):
            super().__init__("both")
            self.status_code = 401
            self.response = Response(503)

    classified = classify_provider_error(Both())

    assert classified.code == "LLM_AUTH"
    assert classified.retryable is False


def test_a_response_without_a_status_is_ignored_rather_than_guessed():
    """A response object that carries no status must not turn into a made-up verdict."""

    class NoStatus(Exception):
        def __init__(self):
            super().__init__("nothing useful")
            self.response = object()

    classified = classify_provider_error(NoStatus())

    assert classified.code == "LLM_UNKNOWN"
    assert classified.retryable is False
