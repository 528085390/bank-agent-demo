"""Proactive reminders, read by the workspace shell rather than asked for in chat.

The subscription module can already answer "什么时候扣款" once a user asks. This surface is
the other half: the page loads it on its own, so a charge that is close enough gets a visible
reminder before it happens. It is deliberately read-only and separate from the model tools --
a reminder is a projection of the bank's data, not an action, and it must never require a
model call to appear.

Two rules keep it honest:

- "today" comes from the bank clock through `SystemClock`, never from this host. The demo
  moves the bank's date, and a local date would make the same subscription look due or not
  due depending on which process asked.
- only active, auto-renewing subscriptions inside the horizon are returned. A paused plan and
  a manual renewal will not charge, so listing them as "即将续费" would be a false alarm.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from app.api.routes import envelope, identity, runtime
from app.core.clock import SystemClock
from app.core.errors import AppError
from app.integration.bank_errors import BankCallError

router = APIRouter(prefix="/api/reminders", tags=["reminders"])

DEFAULT_HORIZON_DAYS = 14
MAX_HORIZON_DAYS = 60


def _unavailable() -> AppError:
    return AppError("BANK_UNAVAILABLE", "银行服务暂不可用，无法读取续费提醒", 502, retryable=True)


def _due_date(value: object) -> date | None:
    """The bank's date, or None when the row cannot be used as a reminder."""

    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _reminder(row: dict, *, today: date, horizon_days: int) -> dict | None:
    """One row in the shape the shelf renders, or None when it is not due."""

    if row.get("status") != "active" or row.get("auto_renew") is not True:
        return None
    due = _due_date(row.get("next_due_date"))
    if due is None:
        return None
    days_until_due = (due - today).days
    if days_until_due < 0 or days_until_due > horizon_days:
        return None
    amount_minor = row.get("amount_minor")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        return None
    name = row.get("name") or row.get("merchant_name") or "未命名订阅"
    return {
        "subscription_id": str(row.get("subscription_id") or ""),
        "name": str(name),
        "merchant_name": str(row.get("merchant_name") or ""),
        "amount_minor": amount_minor,
        "next_due_date": due.isoformat(),
        "days_until_due": days_until_due,
    }


@router.get("/subscriptions")
async def subscription_reminders(
    request: Request,
    horizon_days: int = Query(DEFAULT_HORIZON_DAYS, ge=1, le=MAX_HORIZON_DAYS),
):
    """Subscriptions that will charge soon, for the signed-in customer."""

    who = await identity(request)
    rt = runtime(request)
    customer_id = await run_in_threadpool(rt.identity.bank_customer_id, who["user_id"])
    if not customer_id:
        raise AppError("CUSTOMER_BINDING_REQUIRED", "当前用户未绑定银行客户号", 422)

    try:
        response = await run_in_threadpool(
            rt.bank_client.request, "GET", "/subscriptions", user_id=customer_id
        )
    except BankCallError as exc:
        raise _unavailable() from exc
    if not isinstance(response.data, list):
        raise _unavailable()

    today = await run_in_threadpool(SystemClock.today)
    items = [
        item
        for row in response.data
        if isinstance(row, dict)
        if (item := _reminder(row, today=today, horizon_days=horizon_days)) is not None
    ]
    items.sort(key=lambda item: (item["next_due_date"], item["subscription_id"]))
    return envelope(
        request,
        {
            "items": items,
            "count": len(items),
            "total_minor": sum(item["amount_minor"] for item in items),
            "horizon_days": horizon_days,
        },
    )
