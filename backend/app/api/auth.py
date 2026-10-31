"""`/api/auth/*`：注册、登录、退出、改密。

这一层只做三件事：解析请求、调用 `Identity`、把结果装进现有信封。密码策略、限速、会话
签发都在 `app.identity` 里，因为那些规则要在 HTTP 之外也能被脚本和测试用到。

`POST /login` 是唯一把 token 放进响应体以外的地方——它进 HttpOnly Cookie。CSRF token 走
响应体，因为它必须能被页面上的 JS 读到再放回请求头，HttpOnly 会让这件事做不到。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import Field

from app.api.routes import envelope, identity, runtime
from app.api.schemas import RequestModel
from app.api.security import (
    clear_session_cookie,
    presented_token,
    require_trusted_origin,
    set_session_cookie,
)
from app.audit.context import AuditContext
from app.core.errors import AppError
from app.identity.errors import AuthErrorCode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")


class RegisterRequest(RequestModel):
    login_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class LoginRequest(RequestModel):
    login_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(RequestModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


def client_ip(request: Request) -> str | None:
    """The caller's address for rate limiting.

    `X-Forwarded-For` is only honoured because the deployment puts exactly one nginx in
    front of this process and that nginx sets the header itself. A process exposed directly
    would have to ignore it: any client can send this header, and a per-IP limit keyed on a
    value the client chooses is not a limit at all.
    """

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _audit(request: Request, action: str, user_id: str, *, result: str, reason: str | None = None):
    """Write one authentication event into the existing business trail.

    Deliberately no password, no token and no CSRF value in the payload: the audit trail is
    read by more people than the credential store is.
    """

    rt = runtime(request)
    try:
        rt.audit.record_business_event(
            action=action,
            resource_type="app_user",
            resource_id=user_id,
            result=result,
            reason=reason,
            context=AuditContext(
                user_id=user_id, request_id=getattr(request.state, "request_id", None)
            ),
        )
    except Exception:  # noqa: BLE001 - a failed audit must not decide whether login works
        # The alternative is worse in both directions: letting the audit failure fail the
        # login locks everyone out when the trail is down, and swallowing it silently is what
        # this line refuses to do.
        logger.error("auth_audit_failed action=%s user_id=%s", action, user_id)


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, request: Request):
    require_trusted_origin(request)
    rt = runtime(request)
    from starlette.concurrency import run_in_threadpool

    created = await run_in_threadpool(
        rt.identity.register,
        login_name=body.login_name,
        password=body.password,
    )
    _audit(request, "auth.register", created["user_id"], result="ok")
    # No session is issued here on purpose: registration proves nothing about the password
    # being remembered, and auto-login would hide a typo'd password behind a working session.
    return envelope(request, {"user_id": created["user_id"], "login_name": created["login_name"]})


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    require_trusted_origin(request)
    rt = runtime(request)
    from starlette.concurrency import run_in_threadpool

    issued = await run_in_threadpool(
        rt.identity.login,
        login_name=body.login_name,
        password=body.password,
        client_ip=client_ip(request),
    )
    set_session_cookie(response, issued["token"], rt.settings, csrf_token=issued["csrf_token"])
    _audit(request, "auth.login", issued["user_id"], result="ok")
    return envelope(
        request,
        {
            "csrf_token": issued["csrf_token"],
            "user": {
                "user_id": issued["user_id"],
                "login_name": issued["login_name"],
                "role": issued["role"],
            },
        },
    )


@router.get("/me")
async def me(request: Request):
    who = await identity(request)
    rt = runtime(request)
    from starlette.concurrency import run_in_threadpool

    return envelope(request, await run_in_threadpool(rt.identity.me, who))


@router.post("/logout")
async def logout(request: Request, response: Response):
    require_trusted_origin(request)
    rt = runtime(request)
    from starlette.concurrency import run_in_threadpool

    # Revoke first, clear the cookie second. A request that dies between the two leaves a
    # cookie the server already refuses, which is the safe order; the reverse would leave a
    # live session with no cookie and a user who thinks they logged out.
    try:
        who = await identity(request)
    except AppError as exc:
        if exc.code != AuthErrorCode.SESSION_REQUIRED.value:
            raise
        clear_session_cookie(response)
        return envelope(request, {"logged_out": True})
    # The token that authenticated this request, whichever transport carried it -- reading the
    # cookie again would silently do nothing for a Bearer caller.
    token, _ = presented_token(request)
    await run_in_threadpool(rt.identity.logout, token)
    clear_session_cookie(response)
    _audit(request, "auth.logout", who["user_id"], result="ok")
    return envelope(request, {"logged_out": True})


@router.post("/logout-all")
async def logout_all(request: Request, response: Response):
    require_trusted_origin(request)
    rt = runtime(request)
    who = await identity(request)
    from starlette.concurrency import run_in_threadpool

    revoked = await run_in_threadpool(rt.identity.logout_all, who["user_id"])
    clear_session_cookie(response)
    _audit(request, "auth.logout_all", who["user_id"], result="ok")
    return envelope(request, {"logged_out": True, "revoked_sessions": revoked})


@router.post("/password/change")
async def change_password(body: ChangePasswordRequest, request: Request, response: Response):
    require_trusted_origin(request)
    rt = runtime(request)
    who = await identity(request)
    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(
        rt.identity.change_password, who["user_id"], body.current_password, body.new_password
    )
    # Every session was just revoked, this one included, so the cookie has to go with them.
    clear_session_cookie(response)
    _audit(request, "auth.password_change", who["user_id"], result="ok")
    return envelope(request, {"changed": True, "relogin_required": True})
