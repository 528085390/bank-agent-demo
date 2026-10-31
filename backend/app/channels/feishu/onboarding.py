"""扫码接入的会话编排：生成二维码 → 轮询授权 → 落凭据热启动 → 取消/撤销。

设备流的三次请求在 setup.py，长连接在 boot.py，这一层只做三件事：
1. **记住谁在等**：device_code 只活在进程内存的会话表里，对外只给一枚 setup_id；
2. **别把飞书问急了**：每个会话自己带下一次可问的时间（interval，slow_down 会加长）；
3. **授权落地那一刻把事情一次做完**：写 .env.channels、刷新 settings、热启动长连接。

不落盘 device_code、不打日志（返回值里有 client_secret）、会话表有上限（旧会话被挤掉只是
需要重扫码，不会漏内存）。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from starlette.concurrency import run_in_threadpool

from app.channels.feishu import setup

#: 同时挂着的扫码会话上限。演示场景一个人扫，8 个足够；被挤掉的旧会话只是要重扫。
MAX_SESSIONS = 8

PENDING = "pending"
AUTHORIZED = "authorized"
DENIED = "denied"
EXPIRED = "expired"
CANCELLED = "cancelled"


class UnknownSetup(LookupError):
    """这枚 setup_id 不在这台进程里（重启过、被挤掉、或者压根没发过）。"""


@dataclass
class SetupSession:
    setup_id: str
    grant: setup.Grant
    state: str = PENDING
    message: str = ""
    interval: float = float(setup.MIN_POLL_INTERVAL_SECONDS)
    expires_at: float = 0.0
    next_poll_at: float = 0.0
    applied: bool = field(default=False)


class FeishuOnboarding:
    """一次进程一份。transport / env_path / bridge / clock 都可注入，便于测试。"""

    def __init__(
        self,
        settings: Any,
        *,
        transport: setup.PostForm | None = None,
        env_path: str | Path | None = None,
        bridge: Any = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._env_path = Path(env_path) if env_path is not None else setup.DEFAULT_ENV_PATH
        self._bridge = bridge
        self._clock = clock or time.time
        self._sessions: OrderedDict[str, SetupSession] = OrderedDict()
        self._lock = threading.Lock()

    # --- 会话表 -------------------------------------------------------------------------

    def _remember(self, session: SetupSession) -> None:
        with self._lock:
            self._sessions[session.setup_id] = session
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)

    def _require(self, setup_id: str) -> SetupSession:
        with self._lock:
            session = self._sessions.get(setup_id)
        if session is None:
            raise UnknownSetup(setup_id)
        return session

    def _forget_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    # --- 扫码 ---------------------------------------------------------------------------

    def _begin_grant(self) -> setup.Grant:
        # 握手只是问服务端支持哪些认证方式；它挂了不影响 begin，别让它挡住用户扫码。
        try:
            setup.init_device_flow(post=self._transport)
        except setup.SetupError:
            pass
        return setup.begin_grant(post=self._transport)

    async def begin(self) -> dict[str, Any]:
        """生成一次扫码：返回二维码与短链，device_code 留在服务端。"""

        grant = await run_in_threadpool(self._begin_grant)
        now = self._clock()
        session = SetupSession(
            setup_id="setup_" + secrets.token_urlsafe(12),
            grant=grant,
            interval=grant.interval,
            expires_at=now + grant.expires_in,
        )
        self._remember(session)
        return {
            "setup_id": session.setup_id,
            "user_code": grant.user_code,
            "link": grant.link,
            "qr_svg": setup.render_qr_svg(grant.link),
            "expires_in": grant.expires_in,
            "interval": grant.interval,
        }

    async def poll(self, setup_id: str) -> dict[str, Any]:
        """问一次授权进展。已经是终态的会话不再打扰飞书。"""

        session = self._require(setup_id)
        if session.state != PENDING:
            return self._public(session)
        now = self._clock()
        if now >= session.expires_at:
            session.state = EXPIRED
            session.message = "二维码已过期，请重新点击接入"
            return self._public(session)
        if now < session.next_poll_at:
            return self._public(session)
        try:
            outcome = await run_in_threadpool(self._ask, session)
        except setup.SetupError as error:
            if error.code == "DENIED":
                session.state = DENIED
                session.message = "你在飞书里取消了授权"
            elif error.code == "EXPIRED":
                session.state = EXPIRED
                session.message = "二维码已过期，请重新点击接入"
            else:
                # 网络抖动 / 上游报错都还能再试：保持 pending，等下一个间隔。
                session.message = str(error)
                session.next_poll_at = now + session.interval
            return self._public(session)

        session.interval = max(float(setup.MIN_POLL_INTERVAL_SECONDS), outcome.interval)
        session.next_poll_at = self._clock() + session.interval
        if not outcome.authorized:
            session.message = ""
            return self._public(session)
        await self._authorize(session, outcome)
        return self._public(session)

    def _ask(self, session: SetupSession) -> setup.PollOutcome:
        return setup.poll_grant(
            session.grant.device_code, post=self._transport, interval=session.interval
        )

    async def _authorize(self, session: SetupSession, outcome: setup.PollOutcome) -> None:
        """授权到手：凭据落盘、settings 刷新、长连接热启动。三件事依次做完再报 authorized。"""

        if session.applied:
            return
        updates = setup.env_updates_for(
            outcome.client_id, outcome.client_secret, outcome.owner_open_id
        )
        await run_in_threadpool(setup.write_env_file, updates, self._env_path)
        # 文件与内存一起改：长连接和渲染器读的是 settings 对象，只写文件要重启才生效。
        setup.apply_settings(self._settings, updates)
        session.applied = True
        session.state = AUTHORIZED
        session.message = "接入成功，回到飞书给机器人发一句话就能开始"
        # 设备码用完即弃：留着它等于留一枚能换凭据的句柄。
        session.grant = replace(session.grant, device_code="")
        if self._bridge is not None:
            try:
                await self._bridge.start(updates)
            except Exception:  # 长连接起不来也要让用户知道状态，别把 authorized 变成 500
                session.message = "凭据已保存，但长连接没连上，请稍后重启后端再试"

    def cancel(self, setup_id: str) -> dict[str, Any]:
        """取消扫码（幂等）。已经拿到凭据的会话不会被这一步撤销——那要走 disconnect。"""

        session = self._require(setup_id)
        if session.state == PENDING:
            session.state = CANCELLED
            session.message = "已取消扫码"
            session.grant = replace(session.grant, device_code="")
        return self._public(session)

    def _public(self, session: SetupSession) -> dict[str, Any]:
        retry_after = max(0.0, session.next_poll_at - self._clock())
        return {
            "setup_id": session.setup_id,
            "state": session.state,
            "message": session.message,
            "retry_after": round(retry_after, 1),
        }

    # --- 状态 / 撤销 --------------------------------------------------------------------

    def status(self, rt: Any) -> dict[str, Any]:
        app_id = getattr(self._settings, "feishu_app_id", "") or ""
        secret = getattr(self._settings, "feishu_app_secret", "") or ""
        return {
            "enabled": bool(getattr(self._settings, "feishu_enabled", False)),
            "configured": bool(app_id and secret),
            # 只回打码后的 app_id：前端要的是「配的是哪个应用」，不是凭据本身。
            "app_id": setup.mask_secret(app_id),
            "running": bool(self._bridge is not None and self._bridge.running),
            "bound_users": self._count_bindings(rt),
        }

    def _count_bindings(self, rt: Any) -> int:
        with rt.database.sessions() as db:
            return int(
                db.execute(
                    select(func.count())
                    .select_from(_binding_model())
                    .where(_binding_model().channel == "feishu")
                ).scalar()
                or 0
            )

    async def disconnect(self, rt: Any) -> dict[str, Any]:
        """取消接入：停长连接、抹凭据、清绑定。什么都没接的时候只是个空操作。"""

        bound = self._count_bindings(rt)
        app_id = getattr(self._settings, "feishu_app_id", "") or ""
        secret = getattr(self._settings, "feishu_app_secret", "") or ""
        connected = bool(
            getattr(self._settings, "feishu_enabled", False)
            or app_id
            or secret
            or bound
            or (self._bridge is not None and self._bridge.running)
        )
        if not connected:
            return {"disconnected": False, "bound_users": bound}

        if self._bridge is not None:
            await self._bridge.stop()
        await run_in_threadpool(setup.remove_env_keys, setup.FEISHU_ENV_KEYS, self._env_path)
        self._reset_settings()
        await run_in_threadpool(self._clear_bindings, rt)
        self._forget_all()
        return {"disconnected": True, "bound_users": 0}

    def _reset_settings(self) -> None:
        settings = self._settings
        settings.feishu_enabled = False
        settings.feishu_app_id = ""
        settings.feishu_app_secret = ""
        settings.feishu_allow_from = ""
        settings.feishu_encrypt_key = ""
        settings.feishu_verification_token = ""

    def _clear_bindings(self, rt: Any) -> None:
        model = _binding_model()
        with rt.database.sessions() as db:
            db.execute(delete(model).where(model.channel == "feishu"))
            db.commit()


def _binding_model():
    # 放在函数里：这一层只在真正查询时才需要存储层的模型，import 顺序更省心。
    from app.storage.models import ChannelBinding

    return ChannelBinding


__all__ = [
    "AUTHORIZED",
    "CANCELLED",
    "DENIED",
    "EXPIRED",
    "MAX_SESSIONS",
    "PENDING",
    "FeishuOnboarding",
    "SetupSession",
    "UnknownSetup",
]
