"""请求来源校验。

CSRF 只在「浏览器自动带上凭据」时才是问题，所以这里按凭据来源分流：

* **Cookie**：同源请求 + `X-CSRF-Token` 双校验，缺一即拒。
* **`Authorization: Bearer`**：不校验 CSRF。浏览器不会自动附加这个头，第三方页面无法让
  用户的浏览器带上它，所以它本身就不是 CSRF 载体。测试和命令行工具走的都是这条路。

`Origin` 缺失时放行而不是拒绝：现代浏览器对跨站 POST 一定带 `Origin`，缺失意味着这不是
一个浏览器发起的跨站表单，拒绝它只会把 curl 和健康检查挡在门外。只要它出现了，就必须和
`Host` 一致。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from app.identity.errors import AuthErrorCode, auth_error

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# `Path=/api` rather than `/`: the token never has to reach the static bundle, and a narrower
# path is one less place it can leak from. `SameSite=Strict` is what stops a cross-site POST
# from carrying the session at all; the CSRF token is the second layer for the cases where a
# browser still sends it.
SESSION_COOKIE = "bank_session"
SESSION_COOKIE_PATH = "/api"
# The CSRF token travels in a *readable* cookie on purpose. It has to reach the page's JS so the
# page can echo it back in a header, which an HttpOnly cookie cannot do. That is not a weakening:
# a script on another origin cannot read this cookie (same-origin policy), and the value alone is
# useless without the session cookie it is bound to. Keeping it in a cookie rather than in memory
# is what makes it survive a page reload and stay consistent across tabs.
#
# **Its path is `/`, not `/api`, and that is the whole point.** `document.cookie` only exposes
# cookies whose path covers the current document. The SPA is served at `/`, so a `/api`-scoped
# CSRF cookie was invisible to it: the token only ever existed in the memory of the page that
# logged in, and every write after a reload came back `CSRF_TOKEN_MISSING` — including logout.
# Found by `_accept/auth_acceptance.py` + `_e2e/browser/auth-sync.cjs` on 2026-10-28. The
# session cookie keeps `/api`: it is the credential, and it has no reason to ride along with
# static assets.
CSRF_COOKIE = "bank_csrf"
CSRF_COOKIE_PATH = "/"


def set_session_cookie(response, token: str, settings, *, csrf_token: str | None = None) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path=SESSION_COOKIE_PATH,
    )
    if csrf_token:
        # 迁移：`Path=/api` 那一版留下的同名 Cookie 还在老浏览器里，不清掉的话一次请求会带
        # 两份 `bank_csrf`，页面读到的可能是过时的那份（同一登录态下摘要对不上 → 403）。
        # 删的是旧路径，写的是新路径，顺序不能反。
        response.delete_cookie(CSRF_COOKIE, path=SESSION_COOKIE_PATH, samesite="strict")
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            httponly=False,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path=CSRF_COOKIE_PATH,
        )


def clear_session_cookie(response) -> None:
    # Same attributes as the setter, or the browser keeps the old cookie and "logged out"
    # turns into "still logged in" on the next request. The paths differ, so the two deletes
    # have to name their own — deleting `/api` does not remove a cookie scoped to `/`.
    response.delete_cookie(
        SESSION_COOKIE, path=SESSION_COOKIE_PATH, httponly=True, samesite="strict"
    )
    response.delete_cookie(CSRF_COOKIE, path=CSRF_COOKIE_PATH, samesite="strict")
    # 旧路径的那一份也要删，否则退出之后它还留在浏览器里。
    response.delete_cookie(CSRF_COOKIE, path=SESSION_COOKIE_PATH, samesite="strict")


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def cookie_token(request: Request) -> str | None:
    return request.cookies.get("bank_session")


def presented_token(request: Request) -> tuple[str, str]:
    """The token this request presents, and where it came from.

    A request that presents both a cookie and a bearer token must present the same one. That
    is not redundancy for its own sake: two different credentials in one request means either
    a client bug or an attempt to have the cookie authorise one identity while the header
    names another, and there is no safe way to pick between them.
    """

    cookie = cookie_token(request)
    bearer = bearer_token(request)
    if cookie and bearer and cookie != bearer:
        raise auth_error(AuthErrorCode.SESSION_MISMATCH)
    if bearer:
        return bearer, "bearer"
    if cookie:
        return cookie, "cookie"
    raise auth_error(AuthErrorCode.SESSION_REQUIRED)


def require_trusted_origin(request: Request) -> None:
    """Reject a mutating request whose `Origin`/`Referer` names another site.

    The comparison is against the host the *browser* used, which is not always the `Host`
    header this process receives: every proxy in front is free to rewrite it. Vite's dev
    proxy rewrites it to the upstream target (`127.0.0.1:8000`) and nginx's `$host` drops the
    port, so both used to make a perfectly same-origin request look cross-site. `X-Forwarded-
    Host` is therefore honoured as an additional candidate -- and it is safe to honour because
    a browser cannot set it on a cross-site form post, which is the attack this guards.
    """

    if request.method.upper() in SAFE_METHODS:
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    expected = _expected_hosts(request)
    if not expected:
        return
    if _netloc(source) not in expected:
        raise auth_error(
            AuthErrorCode.ORIGIN_REJECTED,
            details={"origin": _netloc(source), "expected": sorted(expected)},
        )


def _expected_hosts(request: Request) -> set[str]:
    """Every host this request could legitimately claim to have come from."""

    candidates = set()
    for header in ("host", "x-forwarded-host"):
        value = request.headers.get(header)
        if not value:
            continue
        # `X-Forwarded-Host` may carry a chain when several proxies are stacked; the first
        # entry is the one the browser saw.
        candidates.add(_netloc(f"//{value.split(',')[0].strip()}"))
    return {candidate for candidate in candidates if candidate}


def _netloc(value: str) -> str:
    parts = urlsplit(value)
    netloc = parts.netloc or parts.path
    return netloc.lower()
