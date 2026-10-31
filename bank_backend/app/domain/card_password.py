"""Private transaction-PIN hashing. Never include these values in bank projections."""

import base64
import hashlib
import hmac
import re
import secrets

N, R, P = 32768, 8, 3


def valid_password(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{6}", value))


def hash_password(value: str) -> str:
    if not valid_password(value):
        raise ValueError("交易密码必须为六位数字")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=N, r=R, p=P, maxmem=67108864)
    return "$".join(
        (
            "scrypt",
            str(N),
            str(R),
            str(P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(value: str, encoded: str | None) -> bool:
    if not valid_password(value) or not encoded:
        return False
    scheme, n, r, p, salt, expected = encoded.split("$")
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(
        value.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=67108864
    )
    return hmac.compare_digest(actual, base64.b64decode(expected))


def initial_password_hash() -> str:
    from app.core.config import get_settings

    return hash_password(get_settings().demo_card_password.get_secret_value())
