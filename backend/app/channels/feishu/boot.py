"""飞书长连接的进程内句柄：启动、热启动、关闭。

这段逻辑原来长在 app.main 的 lifespan 里。搬到这里的唯一原因是扫码向导：用户扫完码，凭据才
刚写进 backend/.env.channels，需要**在同一个正在跑的进程里**把长连接热启起来——lifespan 已经
跑过去了，不会再执行第二遍。所以启停被收进一个对象，lifespan 与 Web 端点共用它。

约束和原来一模一样：
- binding_mgr 必须是调用方那一份（绑定码只存「hmac(进程随机盐, 码)」，两个实例两份盐，
  网页生成的码在飞书 /bind 里就永远核销不了）；
- feishu_enabled 为假时一个对象都不建、一个字节都不多花；
- 任何异常都关在这一层：飞书挂了最多是少一个渠道，绝不能让整个服务起不来。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping

from app.channels.feishu.setup import apply_settings
from app.core.config import warn_if_mfa_base_url_is_local

logger = logging.getLogger(__name__)


class FeishuBridge:
    """一条飞书长连接的生命周期。start 可重复调用：会先把旧的拆掉再起新的。"""

    def __init__(self, rt: Any, settings: Any, binding_mgr: Any) -> None:
        self._rt = rt
        self._settings = settings
        self._binding_mgr = binding_mgr
        self._runner: Any = None

    @property
    def runner(self) -> Any:
        return self._runner

    @property
    def running(self) -> bool:
        runner = self._runner
        return bool(runner is not None and getattr(runner, "is_running", False))

    async def start(self, updates: Mapping[str, str] | None = None) -> bool:
        """按当前 settings 起长连接。返回是否真的连上了（连不上只记 error，不抛）。"""

        if updates:
            apply_settings(self._settings, updates)
        if not getattr(self._settings, "feishu_enabled", False):
            return False
        await self.stop()
        self._runner = await self._build_and_start()
        return self.running

    async def stop(self) -> None:
        """关掉长连接（幂等）。它的失败不能挡住调用方继续做别的事。"""

        runner, self._runner = self._runner, None
        if runner is None:
            return
        try:
            await runner.stop()
        except Exception:
            logger.exception("feishu_bridge_stop_failed 关闭飞书渠道时出错，继续其余流程")

    async def _build_and_start(self):
        try:
            # 手机打开的是卡片上那条链接，配成 127.0.0.1 时它在手机上指向手机自己。
            warn_if_mfa_base_url_is_local(self._settings)
            # 渠道相关的 import 留在这里：没开飞书时，正常启动不会加载这些模块。
            from app.channels.dedup import ChannelDedup
            from app.channels.engine import ChannelEngine
            from app.channels.feishu import FeishuDispatcher, FeishuRunner

            # 进程内唯一的那一份防重台账（水位线 + 消息/点击两个窗口），引擎与 dispatcher 共用：
            # dispatcher 只用水位线，判重由引擎负责——那张台账「查即记账」，两边都查会把每条
            # 首投都当成重复。
            dedup = ChannelDedup(boot_time=time.time())
            engine = ChannelEngine(
                rt=self._rt,
                settings=self._settings,
                binding_mgr=self._binding_mgr,
                dedup=dedup,
            )
            runner = FeishuRunner(FeishuDispatcher(engine, self._settings, dedup), self._settings)
            if await runner.start():
                logger.info("feishu_bridge_ready %s", runner.describe())
            else:
                # 依赖没装 / 凭据没配：登记一条 error，应用照常提供服务。
                logger.error(
                    "feishu_bridge_degraded 飞书渠道未连上，应用继续运行 %s", runner.describe()
                )
            return runner
        except Exception:
            logger.exception("feishu_bridge_failed 初始化飞书渠道失败（飞书渠道不可用）")
            return None


_STATE_LOCK = threading.Lock()


def feishu_bridge_for(app: Any, rt: Any, binding_mgr: Any) -> FeishuBridge:
    """进程内唯一的那条长连接句柄，挂在 app.state.feishu_bridge 上。

    running 要反映**真实**的那一条连接，所以 lifespan 与 Web 端点必须拿到同一个对象：
    否则向导会以为「没连上」再去起第二条，飞书那边就有两个客户端抢同一批事件。
    """

    bridge = getattr(app.state, "feishu_bridge", None)
    if bridge is not None:
        return bridge
    with _STATE_LOCK:
        bridge = getattr(app.state, "feishu_bridge", None)
        if bridge is None:
            bridge = FeishuBridge(rt, rt.settings, binding_mgr)
            app.state.feishu_bridge = bridge
        return bridge
