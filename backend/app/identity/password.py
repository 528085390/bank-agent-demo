"""密码：归一化、策略校验、argon2id 哈希与校验。

参数用 argon2-cffi 的默认值（argon2id / t=3 / m=64MiB / p=4），也就是 RFC 9106 的第二推荐
方案。这里不自己调参：调低会被一次 GPU 爆破，调高会让登录在 2 核机器上排队，而「服务器实测」
这件事在没有压测环境时只是换一个数字而已。需要时用环境变量级别的改动，不改代码。

`needs_rehash` 让将来换参数时能就地升级：用户下次成功登录时把旧哈希重算一遍，不需要
强制所有人改密码。
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.identity.errors import AuthErrorCode, auth_error

# 3-32 位字母数字下划线。登录名是给人记的，不是给邮箱用的；限制字符集让归一化规则能说清：
# strip 之后整体 casefold，再按这个集合校验。
LOGIN_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{3,32}")

# 12 位是下限而不是 15：首版没有自助找回，密码越长越可能变成永久失联的账号。上限 128 是为了
# 挡住拿超长输入做 DoS 的请求，不是安全策略。
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

_hasher = PasswordHasher()

# 一个真实的 argon2id 哈希，只用来给「用户不存在」这条路径消耗掉和「密码错」一样的 CPU。
# 用固定的假串（比如手写的 base64）不行：argon2 会先校验格式，格式错就立刻返回，
# 时间差反而更明显。
_DUMMY_HASH = _hasher.hash("timing-equaliser-never-matches-anything")


def normalise_login_name(raw: str) -> str:
    """登录名的唯一规范形式：去空白、统一小写。

    校验放在归一化之后，所以 "Alice" 和 " alice " 是同一个账号，重名也由这一个函数决定，
    不依赖调用方各自 strip。
    """

    name = (raw or "").strip().casefold()
    if not LOGIN_NAME_PATTERN.fullmatch(name):
        raise auth_error(AuthErrorCode.LOGIN_NAME_INVALID)
    return name


def validate_password(raw: str, *, login_name: str = "") -> str:
    """策略校验，返回原样的密码。

    不裁剪、不截断：用户粘贴进来的空格是他密码的一部分，悄悄去掉会让「我明明输对了」变成
    一个永远查不出来的问题。
    """

    if not isinstance(raw, str) or not PASSWORD_MIN_LENGTH <= len(raw) <= PASSWORD_MAX_LENGTH:
        raise auth_error(AuthErrorCode.PASSWORD_POLICY)
    if login_name and raw.strip().casefold() == login_name:
        raise auth_error(AuthErrorCode.PASSWORD_POLICY)
    return raw


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(password_hash: str | None, raw: str) -> bool:
    """校验密码；哈希为空、损坏或格式不认一律当作不匹配，不向上抛。"""

    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def spend_verification_time(raw: str) -> None:
    """Burn the same work a real verification would, for an account that does not exist.

    Called on the miss path of login. Without it, a wrong password costs ~80ms and an
    unknown account costs ~0ms, and the response time alone answers "does this account
    exist?" -- the thing `INVALID_CREDENTIALS` exists to hide.
    """

    verify_password(_DUMMY_HASH, raw)
