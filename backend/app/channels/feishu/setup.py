"""飞书个人号扫码接入 —— 设备流、二维码、凭据落盘（产品层实现）。

原 _accept/test_feishu_bridge.py 里的 CLI 向导把这三件事揉在一次终端交互里，
Web 端点没法复用，所以抽到这里：transport 可注入（单测不发真实请求）、
错误带稳定 code（端点据此给出中文话术）、落盘路径参数化。

三条纪律：
1. 不打日志。这一层的返回值里有 client_secret，任何 debug 打印都会把它写进文件或终端。
2. 不缓存 device_code 到磁盘。设备码只在调用方进程内存里活到本次扫码结束。
3. 不认领「差不多成功」。字段缺一个就抛 BAD_RESPONSE，宁可让用户重扫，
   也不要写一份缺 APP_ID 的 .env 让后端起一个哑的长连接。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import qrcode
from qrcode.constants import ERROR_CORRECT_M

DEFAULT_DOMAIN = "feishu.cn"
REGISTRATION_PATH = "/oauth/v1/app/registration"
REQUEST_TIMEOUT_SECONDS = 15.0
# 飞书 begin 偶尔回 interval=0；没有下限就会变成忙轮询，把应用打到限流。
MIN_POLL_INTERVAL_SECONDS = 5
SLOW_DOWN_STEP_SECONDS = 3
# 飞书设备流把「还没扫码 / 慢慢来 / 被拒 / 过期」也放在 HTTP 400 的 body 里
# （{"error": "authorization_pending"} 之类）；这几个 error 是协议状态，不是传输故障。
DEVICE_FLOW_STATES = frozenset(
    {"authorization_pending", "slow_down", "access_denied", "expired_token", "no_such_device_code"}
)
QR_MODULES_PER_SIDE_LIMIT = 177  # 版本 40 的上限，超出说明输入不合理

# backend/.env.channels —— 与会提交进 git 的 backend/.env 分开，凭据不进版本库。
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env.channels"

Form = Mapping[str, str]
# post(url, form, timeout) -> 已解析的 JSON dict。签名固定成位置参数，便于用例注入。
PostForm = Callable[[str, "Form", float], Mapping[str, object]]


class SetupError(RuntimeError):
    """带稳定 code 的失败：NETWORK / BAD_RESPONSE / DENIED / EXPIRED / UPSTREAM。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InitInfo:
    auth_methods: tuple[str, ...] = ()
    nonce: str = ""


@dataclass(frozen=True)
class Grant:
    """一次扫码会话的服务端句柄 + 给人看的那一面。"""

    device_code: str
    user_code: str
    link: str
    expires_in: float
    interval: float


@dataclass(frozen=True)
class PollOutcome:
    state: str = "pending"  # pending | authorized
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    owner_open_id: str = ""
    interval: float = float(MIN_POLL_INTERVAL_SECONDS)

    @property
    def authorized(self) -> bool:
        return self.state == "authorized"


# --- 传输层 ------------------------------------------------------------------------------


def _decode(raw: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as error:
        raise SetupError("BAD_RESPONSE", "飞书返回的不是 JSON") from error
    if not isinstance(payload, Mapping):
        raise SetupError("BAD_RESPONSE", "飞书返回的结构异常")
    return payload


def post_form(
    url: str, form: Form, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> Mapping[str, object]:
    """把表单 POST 出去并解析 JSON。HTTP 层错误被翻成带 code 的 SetupError。"""

    data = urllib.parse.urlencode({key: str(value) for key, value in form.items()}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _decode(response.read())
    except urllib.error.HTTPError as error:
        payload: Mapping[str, object] = {}
        try:
            payload = _decode(error.read() or b"{}")
        except SetupError:
            payload = {}
        soft = _text(payload, "error")
        if soft in DEVICE_FLOW_STATES:
            # 「还没好」不是异常：把 body 原样交回 poll_grant 分类。
            return payload
        detail = payload.get("error_description") or payload.get("error") or f"HTTP {error.code}"
        raise SetupError("UPSTREAM", f"飞书接口返回错误：{detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SetupError("NETWORK", f"无法连接飞书开放平台：{error}") from error


def _call(
    post: PostForm | None, path: str, form: dict[str, str], domain: str, timeout: float
) -> Mapping[str, object]:
    url = f"https://accounts.{domain}{path}"
    transport = post or post_form
    return transport(url, form, timeout)


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _number(payload: Mapping[str, object], key: str, fallback: float) -> float:
    value = payload.get(key)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return fallback
    return fallback


def _reject(payload: Mapping[str, object]) -> None:
    """init / begin 收到 error 就是不正常 —— 设备流的那些状态错误只属于 poll。"""

    error = _text(payload, "error")
    if error:
        detail = _text(payload, "error_description") or error
        raise SetupError("UPSTREAM", f"飞书接口返回错误：{detail}")


# --- 设备流 ------------------------------------------------------------------------------


def init_device_flow(
    post: PostForm | None = None,
    domain: str = DEFAULT_DOMAIN,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> InitInfo:
    """握手：问服务端支持哪些认证方式。拿不到 nonce 也不影响后续 begin。"""

    payload = _call(post, REGISTRATION_PATH, {"action": "init"}, domain, timeout)
    _reject(payload)
    methods = payload.get("supported_auth_methods")
    return InitInfo(
        auth_methods=tuple(str(item) for item in methods) if isinstance(methods, list) else (),
        nonce=_text(payload, "nonce"),
    )


def begin_grant(
    post: PostForm | None = None,
    domain: str = DEFAULT_DOMAIN,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Grant:
    """申请一次 PersonalAgent 授权：返回给人扫的短链和仅存服务端的 device_code。"""

    payload = _call(
        post,
        REGISTRATION_PATH,
        {
            "action": "begin",
            "archetype": "PersonalAgent",
            "auth_method": "client_secret",
            "request_user_info": "open_id",
        },
        domain,
        timeout,
    )
    _reject(payload)
    device_code = _text(payload, "device_code")
    user_code = _text(payload, "user_code")
    link = _text(payload, "verification_uri_complete") or _text(payload, "verification_uri")
    if not device_code or not user_code or not link:
        raise SetupError("BAD_RESPONSE", "飞书没有返回完整的授权码，请重新点击接入")
    interval = max(
        float(MIN_POLL_INTERVAL_SECONDS), _number(payload, "interval", MIN_POLL_INTERVAL_SECONDS)
    )
    return Grant(
        device_code=device_code,
        user_code=user_code,
        link=link,
        expires_in=max(60.0, _number(payload, "expires_in", 600.0)),
        interval=interval,
    )


def poll_grant(
    device_code: str,
    post: PostForm | None = None,
    interval: float = float(MIN_POLL_INTERVAL_SECONDS),
    domain: str = DEFAULT_DOMAIN,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> PollOutcome:
    """问一次授权结果。pending 的两种「还没好」都只是节奏问题，不是错误。"""

    payload = _call(
        post, REGISTRATION_PATH, {"action": "poll", "device_code": device_code}, domain, timeout
    )
    error = _text(payload, "error")
    if error:
        if error in {"authorization_pending", "no_such_device_code"}:
            return PollOutcome(state="pending", interval=interval)
        if error == "slow_down":
            return PollOutcome(state="pending", interval=interval + SLOW_DOWN_STEP_SECONDS)
        if error == "access_denied":
            raise SetupError("DENIED", "你在飞书里取消了授权")
        if error == "expired_token":
            raise SetupError("EXPIRED", "二维码已过期，请重新生成")
        detail = _text(payload, "error_description") or error
        raise SetupError("UPSTREAM", f"飞书返回未知状态：{detail}")

    client_id = _text(payload, "client_id")
    client_secret = _text(payload, "client_secret")
    user_info = payload.get("user_info")
    owner_open_id = _text(user_info, "open_id") if isinstance(user_info, Mapping) else ""
    if not client_id or not client_secret or not owner_open_id:
        raise SetupError("BAD_RESPONSE", "授权已通过但飞书没给全凭据，请重新扫码")
    return PollOutcome(
        state="authorized",
        client_id=client_id,
        client_secret=client_secret,
        owner_open_id=owner_open_id,
        interval=interval,
    )


# --- 二维码 ------------------------------------------------------------------------------


def render_qr_svg(text: str, *, border: int = 2) -> str:
    """把短链渲染成可内联的 SVG：单个 path、无外链、无脚本。

    不用 qrcode 自带的 SvgImage：它带 XML 声明和 xmlns，内联进 HTML 时多余，
    而且一个模块一个 rect，体积是下面这种合并 path 的好几倍。
    """

    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=border)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()
    side = len(matrix)
    if side > QR_MODULES_PER_SIDE_LIMIT:
        raise SetupError("BAD_RESPONSE", "二维码内容过长，无法渲染")

    segments: list[str] = []
    for row_index, row in enumerate(matrix):
        column = 0
        while column < side:
            if not row[column]:
                column += 1
                continue
            run = 0
            while column + run < side and row[column + run]:
                run += 1
            segments.append(f"M{column} {row_index}h{run}v1h-{run}z")
            column += run

    path = "".join(segments)
    return (
        f'<svg viewBox="0 0 {side} {side}" width="240" height="240" role="img" '
        f'aria-label="飞书接入二维码" shape-rendering="crispEdges">'
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
        f'<path d="{path}" fill="#111827"/>'
        "</svg>"
    )


# --- 凭据落盘 ----------------------------------------------------------------------------


def _split_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value


def write_env_file(updates: Mapping[str, str], path: str | Path = DEFAULT_ENV_PATH) -> Path:
    """覆盖/追加键值，写前留 .bak。只动 .env.channels，绝不碰有意图提交的 .env。"""

    target = Path(path)
    lines: list[str] = []
    replaced: set[str] = set()
    if target.exists():
        text = target.read_text(encoding="utf-8")
        target.with_name(target.name + ".bak").write_text(text, encoding="utf-8")
        for line in text.splitlines():
            parsed = _split_env_line(line)
            if parsed is None or parsed[0] not in updates:
                lines.append(line)
                continue
            key = parsed[0]
            lines.append(f"{key}={updates[key]}")
            replaced.add(key)
    for key, value in updates.items():
        if key not in replaced:
            lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def read_env_file(path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """读回键值（取消接入 / 状态查询要据此判断「配没配」）。"""

    target = Path(path)
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        parsed = _split_env_line(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def remove_env_keys(keys: list[str] | tuple[str, ...], path: str | Path = DEFAULT_ENV_PATH) -> Path:
    """取消接入时把凭据从 .env.channels 里抹掉；文件不存在就当已经干净。"""

    target = Path(path)
    if not target.exists():
        return target
    doomed = set(keys)
    kept = [
        line
        for line in target.read_text(encoding="utf-8").splitlines()
        if (_split_env_line(line) or ("", ""))[0] not in doomed
    ]
    target.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return target


def env_updates_for(app_id: str, app_secret: str, owner_open_id: str) -> dict[str, str]:
    """扫码成功后的 7 个键：开通道、装凭据、把扫码人钉成唯一白名单、免手敲 /bind。"""

    return {
        "FEISHU_ENABLED": "true",
        "FEISHU_APP_ID": app_id,
        "FEISHU_APP_SECRET": app_secret,
        "FEISHU_ALLOW_FROM": owner_open_id,
        "FEISHU_WHITELIST_ENABLED": "true",
        "FEISHU_AUTO_BIND": "true",
        "FEISHU_DEFAULT_BOUND_USER": "user_0001",
    }


#: 取消接入要清掉的键（顺序固定，日志/测试都靠它）。
FEISHU_ENV_KEYS = (
    "FEISHU_ENABLED",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_ALLOW_FROM",
    "FEISHU_WHITELIST_ENABLED",
    "FEISHU_AUTO_BIND",
    "FEISHU_DEFAULT_BOUND_USER",
)


def mask_secret(secret: str) -> str:
    """前端只展示「像不像」：前缀 6 位 + 后 4 位，中间全打码。"""

    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:2] + "*" * max(1, len(secret) - 2)
    return f"{secret[:6]}****{secret[-4:]}"


#: .env.channels 里的键名 → Settings 字段名。两边只差大小写，但显式列出来更不容易写错。
_BOOL_FIELDS = {"feishu_enabled", "feishu_whitelist_enabled", "feishu_auto_bind"}
_TRUE = {"1", "true", "yes", "on"}


def apply_settings(settings, updates: Mapping[str, str]) -> None:
    """把刚写进 .env.channels 的键同时落到内存里的 settings 上。

    长连接与渲染器读的都是 settings 对象，不刷新它就得重启进程才生效——而「扫完就能用」正是
    这个向导的全部意义。
    """

    for key, value in updates.items():
        field = key.lower()
        if not hasattr(settings, field):
            continue
        if field in _BOOL_FIELDS:
            setattr(settings, field, str(value).strip().lower() in _TRUE)
        else:
            setattr(settings, field, str(value))
