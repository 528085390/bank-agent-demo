"""The presentation control plane: simulated clock and security controls.

The demo moves the bank's clock, and the agent has to see the moved clock -- "today" for
every policy decision, due-date calculation and birthday reminder is read from bank_backend
(`GET /clock`). These routes are the only way an operator inspects or moves simulation state,
and they are deliberately **not** model tools: a model that could set the clock or reset security
locks could forge "today" or unlock locked tools on behalf of a user (`docs/GAP-ANALYSIS.md`).

Three rules hold for all of them, and each has its own reason:

- The switch is read from the *running application's* settings on every request, never from
  the global `get_settings()`: an operator or a test flips `runtime.settings.demo_mode`, and
  a module-level settings object would not see that.
- "Now" is always the bank's clock. `advance` reads `GET /clock` before adding its step,
  because this machine's clock is not the simulated one -- that is the whole point of a
  simulated clock.
- A bank failure is this API's `502 BANK_UNAVAILABLE`. `BankCallError` is not the global
  handler's business: letting it escape would turn a retryable upstream failure into
  `500 INTERNAL_ERROR`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.routes import admin, envelope, identity, runtime
from app.core.clock import CST, SystemClock
from app.core.errors import AppError
from app.integration.bank_errors import BankCallError

router = APIRouter(prefix="/api/demo", tags=["demo"])

# Who the control plane acts as. `BankClient.request` takes `user_id` as a required keyword
# argument and the bank rejects an empty one as VALIDATION, so the operator is named once.
OPERATOR = "demo_operator"


class ClockAdvanceRequest(BaseModel):
    """How far to move the simulated clock forward.

    The ranges are pydantic's because they are shape; "at least one hour" is a business rule
    and is refused in the route instead, so the two failures stay distinguishable.
    """

    days: int = Field(0, ge=0, le=365)
    hours: int = Field(0, ge=0, le=24)


class TaskRunRequest(BaseModel):
    """Which outcome the birthday reminder scheduled for today should have."""

    birthday_outcome: str = Field("success", pattern="^(success|fail)$")


def _demo_runtime(request: Request):
    """The runtime, provided this process runs the demo control plane at all."""

    rt = runtime(request)
    if not rt.settings.demo_mode:
        raise AppError("DEMO_DISABLED", "演示控制面仅在演示模式下可用", 403)
    return rt


def _unavailable(message: str = "银行服务暂不可用") -> AppError:
    return AppError("BANK_UNAVAILABLE", message, 502, retryable=True)


def _bank_call(
    rt,
    method: str,
    path: str,
    *,
    user_id: str = OPERATOR,
    params: dict | None = None,
    json: dict | None = None,
    unavailable_message: str = "银行服务暂不可用",
) -> dict:
    """One bank call, with both of its failure modes translated into this API's vocabulary.

    A `BankCallError` is a bank that answered with a failure; a non-object `data` is a bank
    that answered a different question. Neither is this server's 500, and neither may leave
    as a 200 carrying something the panel cannot render.
    """

    post_json = (
        {}
        if (method.upper() == "POST" and path == "/security/reset-lock" and json is None)
        else json
    )
    try:
        resp = rt.bank_client.request(
            method, path, user_id=user_id, params=params, json=post_json
        )
    except BankCallError as exc:
        raise _unavailable(unavailable_message) from exc
    if not isinstance(resp.data, dict):
        raise _unavailable(unavailable_message)
    return resp.data


def _bank_clock(view: dict) -> datetime:
    """The bank's `current_time` as an instant, or a 502 if the view does not carry one."""

    value = view.get("current_time")
    if not isinstance(value, str):
        raise _unavailable()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _unavailable() from None
    # The bank always renders a timezone (`bank_backend/app/core/timeutil.py:13-23`); a naive
    # value is read as CST, the same assumption `SystemClock.now` makes, rather than silently
    # invented as UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


@router.get("/clock")
async def get_clock(request: Request):
    """The bank's clock, passed through unchanged.

    No local fallback: a clock that fell back to this host's time would show a demo date no
    bank call agrees with, which is exactly the confusion this endpoint exists to prevent.
    """

    await admin(request)
    return envelope(request, _bank_call(_demo_runtime(request), "GET", "/clock"))


@router.post("/clock/advance")
async def advance_clock(body: ClockAdvanceRequest, request: Request):
    """Move the simulated clock forward by a day/hour step, as the bank tells the time."""

    await admin(request)
    rt = _demo_runtime(request)
    if body.days + body.hours == 0:
        raise AppError("INVALID_PARAMETER", "advance 至少需要向前拨 1 小时", 422)

    current = _bank_clock(_bank_call(rt, "GET", "/clock"))
    target = current + timedelta(days=body.days, hours=body.hours)
    view = _bank_call(rt, "POST", "/clock", json={"current_time": target.isoformat()})
    # Every reader of "today" caches it for a few seconds; the demo cannot wait that out.
    SystemClock.invalidate()
    return envelope(request, view)


@router.post("/clock/run-tasks")
async def run_due_tasks(request: Request, body: TaskRunRequest | None = None):
    """Run the tasks that are due at the bank's current simulated time."""

    await admin(request)
    rt = _demo_runtime(request)
    # Only a body that actually named the outcome is forwarded: the bank applies its own
    # default when the field is absent, so an empty body and "success" are the same call.
    payload = None if body is None else body.model_dump(exclude_unset=True)
    view = _bank_call(rt, "POST", "/clock/run-tasks", json=payload or None)
    SystemClock.invalidate()
    return envelope(request, view)


@router.get("/security")
async def get_security_status(request: Request):
    """The authenticated customer's security and lock status."""
    rt = _demo_runtime(request)
    who = await identity(request)
    customer_id = await run_in_threadpool(rt.identity.bank_customer_id, who["user_id"])
    if not customer_id:
        raise AppError("CUSTOMER_BINDING_REQUIRED", "当前用户未绑定银行客户号", 422)

    view = await run_in_threadpool(
        _bank_call, rt, "GET", "/security/status", user_id=customer_id
    )
    return envelope(request, view)


@router.post("/security/reset-lock")
async def reset_security_lock(request: Request):
    """Clear security lock and failure counter for the authenticated customer."""
    rt = _demo_runtime(request)
    who = await identity(request)
    customer_id = await run_in_threadpool(rt.identity.bank_customer_id, who["user_id"])
    if not customer_id:
        raise AppError("CUSTOMER_BINDING_REQUIRED", "当前用户未绑定银行客户号", 422)

    view = await run_in_threadpool(
        _bank_call, rt, "POST", "/security/reset-lock", user_id=customer_id, json={}
    )
    return envelope(request, view)
