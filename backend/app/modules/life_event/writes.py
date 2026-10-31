"""The life event domain's writes, through the fund-write facade.

`submit_order` places the order the confirmation card froze: it carries the frozen amount
(so the facade re-checks the day's cap) and, on a red card, the code the user typed.

Cancelling a frozen birthday order moves no money, so it is sent without an amount: the
facade still emits fund_write_attempt/success (the audit trail of "the user tried"), and
records daily_total_skipped because there is nothing to count against the day's limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.integration.bank_client import BankResponse
from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError, message_for
from app.integration.write_facade import WriteFacade, WritePolicyError
from app.modules.base import ERROR_MESSAGES, ToolFailure
from app.tools.protocol import ErrorCode, TrustedContext


def _policy_failure(exc: WritePolicyError) -> ToolFailure:
    return ToolFailure(exc.code, ERROR_MESSAGES[exc.code])


def _bank_failure(failure: BankCallError) -> ToolFailure:
    return ToolFailure(
        KIND_TO_ERROR_CODE[failure.kind],
        message_for(failure.kind),
        retryable=failure.retryable,
    )


def cancel_order(
    client,
    context: TrustedContext,
    order_id: str,
    *,
    idempotency_key: str,
    writes: WriteFacade | None = None,
) -> BankResponse:
    """Cancel one birthday order at the bank.

    The facade owns the audit hook, the customer binding and the idempotency key; a policy
    refusal or a bank failure comes back as a ToolFailure the adapter can report.
    """

    facade = writes or WriteFacade(client)
    try:
        return facade.send(
            "POST",
            f"/birthdays/orders/{order_id}/cancel",
            context=context,
            json={},
            idempotency_key=idempotency_key,
            operation="cancel_birthday_order",
        )
    except WritePolicyError as exc:
        raise _policy_failure(exc) from exc
    except BankCallError as failure:
        raise _bank_failure(failure) from failure


def _order_amount(intent: Mapping[str, Any]) -> int:
    """The amount the confirmation card froze, or a refusal -- never a silent zero.

    A zero would reach the facade as "this write moves no money": no day-cap re-check and no
    day-total row, so a missing amount would quietly become a way around the limit. The card
    always carries a positive integer, so anything else means the snapshot is not the one the
    user saw.
    """

    amount = intent.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ToolFailure(ErrorCode.VALIDATION, "生日订单金额无效，请重新发起办理。")
    return amount


def submit_order(
    client,
    context: TrustedContext,
    intent: Mapping[str, Any],
    *,
    idempotency_key: str,
    verification_code: str | None = None,
    writes: WriteFacade | None = None,
) -> BankResponse:
    """Place the birthday order the confirmation card froze.

    The intent is the snapshot `prepare_order` stored, so the amount and the account are
    the ones the user saw on the card: nothing here re-prices the gift or re-picks the
    paying account. On a red card the code rides in the body as `mfa_code` for this one
    request, and is handed to the facade only so it can tell that the day's cap has already
    been answered.

    An intent without a positive integer amount is refused before the socket, never
    defaulted to 0: the facade reads a zero as "this write moves no money" and would skip
    the day's cap and the day's total altogether.
    """

    amount = _order_amount(intent)
    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")

    body: dict[str, Any] = {
        "contact_id": intent.get("contact_id"),
        "product_code": intent.get("product_code"),
        "from_account_id": intent.get("from_account_id"),
        "birthday_date": intent.get("birthday_date"),
        "confirm": True,
    }
    if code is not None:
        body["mfa_code"] = code

    facade = writes or WriteFacade(client)
    try:
        return facade.send(
            "POST",
            "/birthdays/orders",
            context=context,
            json=body,
            idempotency_key=idempotency_key,
            operation="birthday_order",
            amount_minor=amount,
            verification_code=code,
        )
    except WritePolicyError as exc:
        raise _policy_failure(exc) from exc
    except BankCallError as failure:
        raise _bank_failure(failure) from failure


__all__ = ["cancel_order", "submit_order"]
