"""账号、凭据与登录态的错误码。

和对话错误码同一个思路：一个码对应一句固定文案和一个固定状态，业务代码只能通过
`auth_error()` 构造，避免同一件事在三个地方写出三种说法。`INVALID_CREDENTIALS` 尤其重要
——账号不存在、密码错、账号被禁用必须长得一模一样，否则登录接口就成了账号探测器。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.errors import AppError


class AuthErrorCode(StrEnum):
    LOGIN_NAME_INVALID = "LOGIN_NAME_INVALID"
    LOGIN_NAME_TAKEN = "LOGIN_NAME_TAKEN"
    PASSWORD_POLICY = "PASSWORD_POLICY"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    LOGIN_RATE_LIMITED = "LOGIN_RATE_LIMITED"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    # Three separate failures used to share one code and one message ("请求来源校验失败"), which
    # told the user nothing and told the developer even less. They have nothing in common
    # operationally: an origin mismatch means the proxy is not forwarding the browser's host,
    # a missing token means the page has no session yet, and an invalid token means the
    # cookie and the header disagree. Splitting them is what makes the message actionable.
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    CSRF_TOKEN_MISSING = "CSRF_TOKEN_MISSING"
    CSRF_TOKEN_INVALID = "CSRF_TOKEN_INVALID"
    CURRENT_PASSWORD_INVALID = "CURRENT_PASSWORD_INVALID"
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"
    DEMO_DISABLED = "DEMO_DISABLED"
    PASSWORD_UNCHANGED = "PASSWORD_UNCHANGED"


AUTH_ERROR_MESSAGES: dict[str, str] = {
    AuthErrorCode.LOGIN_NAME_INVALID: "登录名需为 3-32 位字母、数字或下划线",
    AuthErrorCode.LOGIN_NAME_TAKEN: "该登录名已被使用",
    AuthErrorCode.PASSWORD_POLICY: "密码需为 12-128 个字符，且不能与登录名相同",
    # One wording for three causes. Naming which one failed would turn this endpoint into a
    # way to enumerate accounts, which is exactly what an attacker wants before spraying.
    AuthErrorCode.INVALID_CREDENTIALS: "账号或密码不正确",
    AuthErrorCode.LOGIN_RATE_LIMITED: "尝试次数过多，请稍后再试",
    AuthErrorCode.SESSION_REQUIRED: "登录已失效，请重新登录",
    AuthErrorCode.SESSION_MISMATCH: "会话身份不一致",
    # `details` carries the two values, so the page can say which host it expected instead of
    # telling the user to "refresh and retry" -- refreshing cannot fix a proxy that rewrites
    # the Host header, and that is what this code actually means.
    AuthErrorCode.ORIGIN_REJECTED: "请求来源与本站不一致，已被拒绝（跨站请求会被拦截）",
    AuthErrorCode.CSRF_TOKEN_MISSING: "请求缺少 CSRF 令牌，请刷新页面后重试",
    AuthErrorCode.CSRF_TOKEN_INVALID: "CSRF 令牌与当前登录状态不匹配，请刷新页面后重试",
    AuthErrorCode.CURRENT_PASSWORD_INVALID: "当前密码不正确",
    AuthErrorCode.FORBIDDEN_ROLE: "当前账号无权执行此操作",
    AuthErrorCode.DEMO_DISABLED: "演示登录未开启",
    AuthErrorCode.PASSWORD_UNCHANGED: "新密码不能与当前密码相同",
}

AUTH_ERROR_STATUS: dict[str, int] = {
    AuthErrorCode.LOGIN_NAME_INVALID: 422,
    AuthErrorCode.LOGIN_NAME_TAKEN: 409,
    AuthErrorCode.PASSWORD_POLICY: 422,
    AuthErrorCode.INVALID_CREDENTIALS: 401,
    AuthErrorCode.LOGIN_RATE_LIMITED: 429,
    AuthErrorCode.SESSION_REQUIRED: 401,
    AuthErrorCode.SESSION_MISMATCH: 401,
    AuthErrorCode.ORIGIN_REJECTED: 403,
    AuthErrorCode.CSRF_TOKEN_MISSING: 403,
    AuthErrorCode.CSRF_TOKEN_INVALID: 403,
    AuthErrorCode.CURRENT_PASSWORD_INVALID: 422,
    AuthErrorCode.FORBIDDEN_ROLE: 403,
    AuthErrorCode.DEMO_DISABLED: 403,
    AuthErrorCode.PASSWORD_UNCHANGED: 422,
}


def auth_error(
    code: AuthErrorCode, *, retryable: bool = False, details: dict | None = None
) -> AppError:
    return AppError(
        code.value,
        AUTH_ERROR_MESSAGES[code],
        AUTH_ERROR_STATUS[code],
        retryable=retryable,
        details=details,
    )
