"""The only outbound surface to bank_backend.

It does not retry. A transient failure is reported as retryable and the retry decision
belongs to `sdk.run_tool`, which retries once per invocation -- a second retry
layer here would multiply into four calls for one user action.

The bank's own `message` never leaves this module: it is a class-level default
(`resource_not_found`), not a reason, and it is English. Every failure is raised with the
shared Chinese sentence for its kind, so no caller can pass the bank's text on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from app.integration.bank_errors import (
    RETRYABLE_KINDS,
    BankCallError,
    BankErrorKind,
    message_for,
)

# bank_backend's own codes (`bank_backend/app/core/errors.py`). The code wins over the HTTP
# status: the bank is explicit about which business failure it hit, while its status is only
# a coarse echo of it -- three different failures all answer 409.
CODE_KINDS: dict[int, BankErrorKind] = {
    1004: BankErrorKind.NOT_FOUND,
    1005: BankErrorKind.INVALID_STATE,
    1006: BankErrorKind.INSUFFICIENT_BALANCE,
    1007: BankErrorKind.CONFIRMATION_REQUIRED,
    1008: BankErrorKind.VERIFICATION_FAILED,
    1009: BankErrorKind.LOCKED,
    1012: BankErrorKind.VALIDATION,
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BankResponse:
    code: int
    message: str
    data: Any


def _raise(kind: BankErrorKind) -> None:
    raise BankCallError(kind, message_for(kind), retryable=kind in RETRYABLE_KINDS)


def _kind_for_status(status: int) -> BankErrorKind:
    """What the status alone says. The fallback when the body carries no code we know."""

    if status == 429:
        return BankErrorKind.RATE_LIMITED
    if status >= 500:
        return BankErrorKind.SERVER
    return BankErrorKind.CLIENT


def _kind_for(status: int, code: int | None) -> BankErrorKind:
    if code is not None and code in CODE_KINDS:
        return CODE_KINDS[code]
    return _kind_for_status(status)


def _read_envelope(response: httpx.Response) -> dict[str, Any] | None:
    """The envelope, or None when the body is not one: a proxy's error page, a truncated
    body, a shape we do not know."""

    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("code"), int):
        return payload
    return None


def _kind_for_unreadable(status: int) -> BankErrorKind:
    """A body that is not an envelope at all.

    A 5xx or a 429 is a transport-level failure whether or not its body happens to be our
    envelope -- an error page from a proxy is not JSON -- and calling that a protocol error
    would make a transient outage non-retryable. Anything else is a body we cannot read.
    """

    if status == 429 or status >= 500:
        return _kind_for_status(status)
    return BankErrorKind.PROTOCOL


class BankClient:
    """Synchronous on purpose: the tool protocol is synchronous, and a tool call is tens
    of milliseconds next to the model call that precedes it.
    """

    def __init__(
        self,
        base_url: str,
        *,
        service_token: str = "",
        connect_timeout: float = 2.0,
        read_timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # The token rides on every request as a client-level default header, so a caller
        # cannot forget it on a new call site. An empty token stays empty on purpose: the
        # bank answers 401, which is the safe failure -- silently calling an unauthenticated
        # customer endpoint is how the previous version let anything reach every balance.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
            headers={"X-Service-Token": service_token} if service_token else None,
            # Local mock banking must remain reachable when Windows has a system proxy.
            trust_env=httpx.URL(base_url).host not in ("127.0.0.1", "localhost", "::1"),
        )

    @classmethod
    def from_settings(cls) -> BankClient:
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.bank_service_token:
            # Loud on purpose: with an empty token every bank call returns 401, and the
            # symptom (every tool fails) looks nothing like the cause (a missing env var).
            logger.warning(
                "BANK_SERVICE_TOKEN 未配置：bank_backend 会拒绝所有调用。"
                "本地请在 backend/.env 里配置，测试由 conftest 注入。"
            )
        return cls(
            settings.bank_backend_base_url,
            service_token=settings.bank_service_token,
            connect_timeout=settings.bank_backend_connect_timeout,
            read_timeout=settings.bank_backend_read_timeout,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        user_id: str,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> BankResponse:
        """`user_id` is required and keyword-only: the bank refuses a call that does not
        name its customer, so a missing binding can no longer fall through to the bank's
        own demo customer.

        It is sent **twice, in both places the bank looks**: GET routes read a `user_id`
        query parameter while the POST routes read `body.user_id`. Sending only the query
        parameter left every write running as somebody else. The body copy overrides
        whatever the caller put there, so a module cannot name a different customer than
        the one it was bound to.
        """

        if not user_id:
            # Fail closed, and before the socket: a call that cannot name its customer must
            # not become a call that reads the bank's demo customer's money. The adapter
            # refuses first (contract 4.1.3); this is the last line of defence.
            _raise(BankErrorKind.VALIDATION)

        query = dict(params or {})
        query["user_id"] = user_id
        body = None if json is None else {**json, "user_id": user_id}
        return self._send(method, path, params=query, json=body, idempotency_key=idempotency_key)

    def service_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> BankResponse:
        """A call that has no customer: provisioning the pool, and nothing else.

        It still carries the service token -- that is what proves it came from us -- but it
        cannot name a `user_id`, because there is no user behind it. Keeping it a separate
        method rather than making `user_id` optional stops a domain module from quietly
        dropping the identity of a customer-facing call.
        """

        return self._send(method, path, params=None, json=json, idempotency_key=idempotency_key)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Mapping[str, Any] | None,
        idempotency_key: str | None,
    ) -> BankResponse:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.TimeoutException:
            _raise(BankErrorKind.TIMEOUT)
        except httpx.HTTPError:
            # No business result came back at all, so it is reported like a connection
            # failure: transient, and the supervisor's single retry may apply.
            _raise(BankErrorKind.CONNECT)

        payload = _read_envelope(response)
        if payload is None:
            _raise(_kind_for_unreadable(response.status_code))

        code = payload["code"]
        if code != 0 or response.status_code >= 400:
            _raise(_kind_for(response.status_code, code))
        return BankResponse(
            code=code, message=str(payload.get("message") or ""), data=payload.get("data")
        )

    def close(self) -> None:
        self._client.close()
