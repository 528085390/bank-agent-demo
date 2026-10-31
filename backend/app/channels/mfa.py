"""独立 MFA 短链的签名令牌：一条链接绑定一笔待确认操作，同一枚 nonce 只核销一次。

飞书的确认卡片里带不了验证码（按钮回调只有一个按键值），所以「需要验证码」的那类卡片给
的是一个**跳转短链**，用户在手机上打开它、填码、提交。短链本身就是凭据：它必须证明"是哪个人、
在哪个会话里、要确认哪一笔"，又不能让客户端伪造或改写其中任何一项。

**编码。** 令牌是 `载荷.签名`：载荷是 compact JSON 的 urlsafe-base64（无填充），签名是
`HMAC-SHA256(secret_key, 载荷) ` 的 urlsafe-base64。JSON 的键排序固定，所以同一份载荷签出
同一个令牌。字段只有六个：

- `v` 版本号，将来换编码时旧链接会被认成验签失败而不是被误读；
- `uid` 用户 id，`cid` 会话 id，`pid` 待确认操作 id，`qid` 问题 id —— 状态机要求的四元组，
  缺一个就没法把这次点击绑定到"服务器自己记下的那份暂停"；
- `nonce` 一次性的随机串，`exp` 过期的 Unix 秒（默认 5 分钟）。

**过期判定用墙钟。** 短链存在的时间尺度是"用户找手机的几分钟"，单调时钟只在同一次开机内可比，
而进程重启后仍然有效的链接必须按真实时间判断，所以这里读 `time.time()`，和 LoginSession 的
过期同一个坐标系。判据是 `exp <= now`：等于到期时刻就算过期，不留一秒钟的模糊地带。

**验签失败一律返回 None，不抛异常。** 调用方拿 None 去渲染中文错误页；一个畸形链接不该变成
500，也不该让路由层去猜异常类型。签名比较走 `hmac.compare_digest`，避免按前缀逐字节比较泄
露时序信息。

**nonce 的核销状态只在本进程内存里。** 它是分钟量级的临时状态：重启后链接跟着作废，用户重
新点一次卡片就有一条新的。为它建表、写迁移、再操心清理，代价远大于收益（同
`app/channels/binding.py` 的理由）。结构就是一把 `threading.Lock` 护着的 dict，容量有上限，
过期项顺手清掉；**满了就拒绝（fail-closed）**——宁可让这位用户重开一次卡片，也不淘汰一枚
可能还有效的 nonce 去腾地方，那等于把重放防线静默拆掉一段。**本模块不打任何日志**：令牌里
写着别人正在确认哪一笔操作，日志里不该出现它。

**核销是「先占用、成功才留下」。** 提交开始时占用（`spend_mfa_nonce`），这一次没成（验证码错、
卡片已经不在）就把 nonce 放回去（`release_mfa_nonce`），用户拿同一条链接重试仍然有效；成功了
才真的烧掉。否则"填错一次验证码"就等于"这条链接作废"，用户只能回飞书重新点一遍卡片。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

#: 短链的默认有效期（秒）：够用户翻出手机、等一条短信、输完六位数字。
DEFAULT_MFA_TTL_SECONDS = 300

#: 已占用 nonce 表的容量上限。常驻进程里一个只增不减的 dict 就是慢性内存事故（同
#: `app/channels/binding.py` 与 `app/channels/dedup.py` 的理由）。但**这里不做淘汰**：从表里挤掉
#: 一枚 nonce 就是让那枚 nonce 可以再被花一次，而表满时被挤掉的那枚完全可能还没过期——重放
#: 防线会在最需要它的时候静默消失。所以满了就 fail-closed：先清掉确实过期的，仍然满就**拒绝
#: 这次占用**，用户看到的是「这笔链接已经用过/处理过」，而不是一条被悄悄放宽的防线。
MAX_TRACKED_NONCES = 4096

#: 载荷里必须有值的字符串字段。少一个就说明这不是本进程签出来的那一份载荷。
_REQUIRED_FIELDS = ("uid", "cid", "pid", "qid", "nonce")

_lock = threading.Lock()
#: nonce -> 到期时刻（Unix 秒，与令牌的 exp 同源）。
_spent: dict[str, float] = {}


def _b64encode(raw: bytes) -> str:
    """urlsafe-base64，去掉填充：URL 查询串里不需要再多出现一个百分号编码。"""

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """把去填充的 base64 解回来；非法字符由 base64 自己抛 ValueError，调用方负责吞掉。"""

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, body: str) -> bytes:
    """对载荷字符串的 HMAC-SHA256：签的是编码后的那份字面量，验签才能逐字节比对。"""

    return hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()


def create_mfa_token(
    user_id: str,
    conversation_id: str,
    pending_id: str,
    question_id: str,
    secret: str,
    ttl: int = DEFAULT_MFA_TTL_SECONDS,
) -> str:
    """签一条指向"某个人在某个会话里要确认某一笔"的短链令牌。

    四元组一个都不能少：状态机要拿 `qid` 绑定"服务器自己记下的那次暂停"、拿 `pid` 绑定
    "那次暂停是哪一笔操作"，`uid`/`cid` 则决定这次确认能不能落在别人的会话上（不能）。
    `ttl` 为 0 即立刻过期，负数同理——过期的判断在 `verify_mfa_token` 里，这里只负责记下时限。
    """

    now = int(time.time())
    payload = {
        "v": 1,
        "uid": user_id,
        "cid": conversation_id,
        "pid": pending_id,
        "qid": question_id,
        "nonce": secrets.token_urlsafe(16),
        "exp": now + max(0, int(ttl)),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return body + "." + _b64encode(_signature(secret, body))


def verify_mfa_token(token: Any, secret: str) -> dict[str, Any] | None:
    """验签并验期，通过就返回载荷，任何一处不对都返回 None。

    返回 None 的情形都算"这条链接不能用"：不是字符串、不是两段、签名解不出来、签名对
    不上（被改过或不是这把密钥签的）、载荷解不出来、版本号不是 1、字段缺失或已经过期。
    调用方据此渲染中文错误页——所以这里绝不抛异常，尤其是不能把 500 甩给用户。

    **不核销 nonce。** 页面可以被刷新、可以被同一部手机重新打开，看页面本身不该消耗掉链接；
    消耗发生在提交那一刻（`consume_mfa_token` / `spend_mfa_nonce`）。
    """

    if not isinstance(token, str) or not token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, signature = parts
    try:
        provided = _b64decode(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(provided, _signature(secret, body)):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != 1:
        # 版本号是载荷的一部分，就签在签名里：换编码时旧链接要被认成"不是这一版的令牌"，
        # 而不是用新规则去误读旧字段。缺 `v` 同理——那不是本进程签出来的那一份载荷。
        return None
    for name in _REQUIRED_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= int(time.time()):
        return None
    return payload


def spend_mfa_nonce(nonce: str, expires_at: float, *, max_nonces: int | None = None) -> bool:
    """占用这枚 nonce：第一次返回 True，之后一律 False。**查与写在同一把锁里。**

    "先查后写"在这里不是原子操作，而两个线程同时到达就是两次提交：所以整段都在锁内。

    表满时**不淘汰、只拒绝**（见 `MAX_TRACKED_NONCES`）：挤掉一枚可能还没过期的 nonce 等于
    悄悄放开一次重放。拒绝的代价只是这一位用户回到飞书重开一条链接；放宽的代价是防线失效。

    `max_nonces` 只给测试注入更小的容量用；生产路径永远用模块默认值。
    """

    capacity = MAX_TRACKED_NONCES if max_nonces is None else max(0, int(max_nonces))
    now = int(time.time())
    with _lock:
        _purge_expired(now)
        if nonce in _spent:
            return False
        if len(_spent) >= capacity:
            # 表满：这次直接失败。调用方把它当成"这笔链接已经用过/处理过"——比淘汰一枚
            # 仍然有效的 nonce 安全得多。
            return False
        _spent[nonce] = float(expires_at)
        return True


def release_mfa_nonce(nonce: str) -> None:
    """把没用掉的那次占用放回去：这一次提交没成，不等于这条链接作废。

    与 `app/channels/binding.py` 的 `_release` 同一个理由：码是对的、链接是好的，只是这次没
    走通（验证码填错、卡片已经不在了），用户应该能拿它重试；真正的作废点是"成功提交一次"。
    """

    with _lock:
        _spent.pop(nonce, None)


def nonce_already_spent(nonce: str) -> bool:
    """这枚 nonce 是不是已经用掉了。给页面判断用，**不占用它**。"""

    with _lock:
        _purge_expired(int(time.time()))
        return nonce in _spent


def consume_mfa_token(token: Any, secret: str) -> dict[str, Any] | None:
    """验签 + 占用 nonce：一次调用就把这条链接用掉，成功返回载荷，否则 None。

    提交路由用的是更细的两步（先占用、失败再放回），所以它自己调 `spend_mfa_nonce`；这里是
    "一锤子用完"的那个入口，给只需要一次性的调用方（例如将来渠道侧的自动化校验）。
    """

    payload = verify_mfa_token(token, secret)
    if payload is None:
        return None
    if not spend_mfa_nonce(payload["nonce"], payload["exp"]):
        return None
    return payload


def _purge_expired(now: int) -> None:
    """清掉已经过期的 nonce 记账。**必须在锁内调用。**"""

    for nonce in [name for name, expires_at in _spent.items() if expires_at <= now]:
        del _spent[nonce]
