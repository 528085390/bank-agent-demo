"""浏览器凭证的生成与存储形式。

数据库里只存摘要：拿到一份库导出的人不应该能直接拿去登录。摘要是无盐 sha256 而不是
argon2，这是刻意的——token 本身是 256 位随机数，不存在字典可以爆破，而每次请求都跑一次
argon2 会让登录态校验变成 CPU 热点。密码那边必须慢，token 这边必须快，两者不能套同一个
函数。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# 32 字节熵。token_urlsafe 出来 43 个字符，落在 cookie 里没有体积问题。
_SESSION_TOKEN_BYTES = 32
# CSRF token 只在本会话内比对，不需要和登录 token 同等熵；24 字节已经远超暴力猜测的成本。
_CSRF_TOKEN_BYTES = 24


def new_session_token() -> str:
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(_CSRF_TOKEN_BYTES)


def digest(value: str) -> str:
    """Token 的存储形式：sha256 十六进制。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def matches(stored_digest: str | None, presented: str | None) -> bool:
    """常量时间比较，避免用响应时间逐字节猜出摘要。"""

    if not stored_digest or not presented:
        return False
    return hmac.compare_digest(stored_digest, digest(presented))
