"""Controlled write entry for money-moving bank calls.

This is not a second BankClient. Transport, envelope parsing, and "never retry" stay in
`BankClient`. The facade owns shared *policy* for fund writes:

- require `TrustedContext.bank_customer_id` before the socket
- require a non-empty idempotency key before the socket
- never retain verification codes (callers pass them only in the request body; audit redacts)
- enforce the day's cap at send time. A confirmation card freezes its decision when it is hung,
  but the day's total can move before the user clicks, so a fund write that would now cross
  `DAILY_RED_THRESHOLD_MINOR` and carries no verification code is refused here as well. The
  threshold is the one in `app/policy/types.py` -- the same constant `classify_operation`
  reads -- so this is that one policy applied at the second moment it can be checked, not a
  second policy engine with its own numbers.
- optional audit hook for attempt / success / failure
- record the day's accumulated amount *after* the bank accepted the write, in a transaction
  of its own, so a bookkeeping failure can never turn a successful transfer into a failure
- re-raise `BankCallError` unchanged so adapters keep their domain mapping

See `docs/contracts/write-path.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from app.core.clock import SystemClock
from app.integration.bank_client import BankClient, BankResponse
from app.integration.bank_errors import BankCallError
from app.policy.types import DAILY_RED_THRESHOLD_MINOR
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, TrustedContext

if TYPE_CHECKING:
    from app.storage.database import Database

# Body keys that must never appear in audit / logs / checkpoint-shaped payloads.
SECRET_BODY_KEYS = frozenset({"mfa_code", "verification_code"})

WriteAuditHook = Callable[[str, Mapping[str, Any]], None]

logger = logging.getLogger(__name__)


class WritePolicyError(Exception):
    """A fund-write that must not leave this process.

    Missing identity, missing idempotency key, or no code for a write that the day's cap now
    puts over the limit.
    """

    def __init__(self, code: str) -> None:
        if code not in {
            ErrorCode.NO_CUSTOMER_BINDING,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            ErrorCode.CONFIRMATION_REQUIRED,
        }:
            raise ValueError(f"not a write-policy code: {code}")
        super().__init__(code)
        self.code = code


def require_customer_id(context: TrustedContext) -> str:
    """Bank identity for this user. Fail closed before any bank call."""

    customer = (context.bank_customer_id or "").strip()
    if not customer:
        raise WritePolicyError(ErrorCode.NO_CUSTOMER_BINDING)
    return customer


def require_idempotency_key(key: str | None) -> str:
    """Fund writes without a key cannot be safely reconciled; do not send them."""

    if not isinstance(key, str) or not key.strip():
        raise WritePolicyError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    return key.strip()


def audit_safe_body(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a request body with verification secrets replaced. Never store the raw code."""

    if not body:
        return {}
    return {name: ("***" if name in SECRET_BODY_KEYS else value) for name, value in body.items()}


class WriteFacade:
    """Thin policy layer in front of `BankClient.request` for fund writes."""

    def __init__(
        self,
        client: BankClient,
        *,
        audit_hook: WriteAuditHook | None = None,
        database: Database | None = None,
    ) -> None:
        self._client = client
        self._audit = audit_hook
        # Without a database there is nowhere to record the day's total, so the facade stays
        # a pure policy object (module tests construct it that way). The composition root
        # always passes one.
        self._database = database

    def send(
        self,
        method: str,
        path: str,
        *,
        context: TrustedContext,
        json: Mapping[str, Any],
        idempotency_key: str,
        operation: str | None = None,
        amount_minor: int | None = None,
        verification_code: str | None = None,
    ) -> BankResponse:
        """Send one fund write. Policy runs before the socket; codes are not retained here.

        `operation` names the write for the audit trail. A positive `amount_minor` is what makes
        this a fund write: money moves, so it is checked against the day's cap and counted
        toward the day's total. A write that moves no money -- a cancelled scheduled transfer,
        a wealth purchase -- passes no amount and records nothing.

        `verification_code` is the code the user typed for this click. It answers exactly one
        question -- has the day's cap already been answered? -- and is then dropped: it is never
        stored on this object nor handed to the audit hook (the body is redacted there).
        """

        # Two identities, two jobs. The bank only knows the customer id, so that is what
        # goes on the wire. Everything the app records about who this was -- the audit trail
        # and the day's accumulated total -- is keyed by the app user id, because that is the
        # identity the API and the timeline filter on (app/audit/routes.py).
        customer_id = require_customer_id(context)
        user_id = context.user_id
        key = require_idempotency_key(idempotency_key)
        operation_name = operation or f"{method} {path}"
        # Before this write becomes visible anywhere -- no attempt row, no bank call -- ask
        # whether the day's total still has room for it. The card answered that question when
        # it was hung; the total may have moved since, so it has to be answered again.
        self._require_daily_headroom(
            user_id=user_id,
            operation=operation_name,
            amount_minor=amount_minor,
            verification_code=verification_code,
        )
        # Body may contain mfa_code for this HTTP call only; we never assign it to fields
        # on this object or pass the raw value into the audit hook.
        audit_payload: dict[str, Any] = {
            "operation": operation_name,
            "method": method,
            "path": path,
            "user_id": user_id,
            "bank_customer_id": customer_id,
            "idempotency_key": key,
            "body": audit_safe_body(json),
        }
        self._emit("fund_write_attempt", audit_payload)
        try:
            response = self._client.request(
                method,
                path,
                user_id=customer_id,
                json=json,
                idempotency_key=key,
            )
        except BankCallError as failure:
            self._emit(
                "fund_write_failure",
                {**audit_payload, "error_kind": str(failure.kind)},
            )
            raise
        self._emit("fund_write_success", {**audit_payload, "code": response.code})
        self._record_daily_total(
            user_id=user_id,
            operation=audit_payload["operation"],
            amount_minor=amount_minor,
        )
        return response

    def _require_daily_headroom(
        self,
        *,
        user_id: str,
        operation: str,
        amount_minor: int | None,
        verification_code: str | None,
    ) -> None:
        """Refuse a fund write that the day's total now puts over the cap.

        The same read `transfer_prepare` makes, against the same threshold: the card decided
        on the total as it stood when the card was hung, this decides on the total as it stands
        when the money would move. A write the user has already answered with a code passes;
        an empty or blank code is not an answer.

        Skipped when this facade cannot judge: no database (unit-style callers) or no positive
        amount (the write moves no money). A failed read fails closed, exactly like
        `transfer_prepare`: one more code is cheap, a missed cap is not.
        """

        if self._database is None or amount_minor is None or int(amount_minor) <= 0:
            return
        if (verification_code or "").strip():
            return
        try:
            with self._database.sessions() as session:
                accumulated = DailyTotalStore.get_accumulated_minor(
                    session, user_id, SystemClock.today()
                )
        except Exception:  # noqa: BLE001 - 读不到当日累计就按「已超限」判，宁可多要一次码
            logger.warning(
                "write_daily_total_read_failed user_id=%s operation=%s", user_id, operation
            )
            raise WritePolicyError(ErrorCode.CONFIRMATION_REQUIRED) from None
        if accumulated + int(amount_minor) > DAILY_RED_THRESHOLD_MINOR:
            raise WritePolicyError(ErrorCode.CONFIRMATION_REQUIRED)

    def _record_daily_total(
        self,
        *,
        user_id: str,
        operation: str,
        amount_minor: int | None,
    ) -> None:
        """Record this write in the day's accumulated total -- only after the bank accepted it.

        The store is keyed by the app user id -- the same identity the audit trail uses -- so
        the risk decision reads back exactly what this path wrote, under the identity the API
        and the timeline already filter on. A write that moves no money passes no positive
        amount and records nothing, but it still emits `daily_total_skipped`: "nothing was
        accumulated" has to be visible rather than silent.

        The transaction is opened here, on purpose: the fund write has already committed on the
        bank's side, so bookkeeping must not share its fate. Only the recording segment sits
        inside the `try`; a failure emits `daily_total_write_failed` and the caller still gets
        the bank response.
        """

        if self._database is None:
            return
        if amount_minor is None or int(amount_minor) <= 0:
            self._emit(
                "daily_total_skipped",
                {
                    "operation": operation,
                    "user_id": user_id,
                    "reason": "no amount to count: this write moves no money",
                },
            )
            return
        try:
            with self._database.sessions.begin() as session:
                DailyTotalStore.record_operation_amount(
                    session,
                    user_id,
                    SystemClock.today(),
                    int(amount_minor),
                )
        except Exception as exc:  # noqa: BLE001 - 记账失败不得影响已成功的资金写
            self._emit(
                "daily_total_write_failed",
                {
                    "operation": operation,
                    "user_id": user_id,
                    "error_kind": type(exc).__name__,
                },
            )

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._audit is None:
            return
        self._audit(event, payload)
