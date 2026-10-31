"""飞书长连接（lark-oapi ws）的生命周期：起、停、别把事件循环卡住。

为什么要有这一层（而不是在 lifespan 里直接调 `Client.start()`）：

* `lark_oapi.ws.Client.start()` 是**同步阻塞**的，内部 `run_until_complete` 一个
  `while True: await asyncio.sleep(3600)` 的 `_select()`；直接在事件循环里调它，整个 FastAPI
  连一个请求都处理不了。所以它一律跑在 `asyncio.to_thread` 出来的工作线程里。
* lark 1.7 的 ws 客户端**在 import 时**就抓了一个**模块级全局事件循环**
  （`lark_oapi.ws.client.loop`），之后所有 `create_task` 都挂在那个循环上。如果 lark 是在
  FastAPI 的循环跑起来之后在主线程上第一次 import 的，那个全局循环就是**正在运行**的
  主循环，从工作线程里 `run_until_complete` 会直接抛
  `RuntimeError: This event loop is already running`。所以：**客户端在工作线程里构建**，构建
  时顺手把那个模块级循环换成一个本次专用的新循环，并把新循环留给自己 `stop()` 用。
* 1.7 的 `ws.Client` **没有公开的 stop()**（只有 `_disconnect()` 与 `_select()` 的无限等待），
  所以停止是尽力而为的三步：先断开，再让那个专属循环停掉，让 `start()` 自然返回；将来
  版本若提供了 `stop()` 就直接用它。

依赖与凭据都不具备时，这里**只记 error 日志并返回 False**，应用照常启动——飞书渠道暂不
生效，比整个服务起不来要好得多。同理，`lark_oapi` 只在本模块的函数内部 import，模块顶层
保持干净，单元测试不需要装 SDK 也不需要联网。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: 等工作线程把客户端建起来的上限。超过就不等了，但线程继续建，连上后状态自己转好。
#: 窗口给到 45 秒是因为这台机器上**首次** import lark-oapi 就要十几秒（冷启动、没有 pyc）。
READY_TIMEOUT_SEC = 45.0

#: 等长连接线程退出的上限。
STOP_TIMEOUT_SEC = 8.0

#: 卡片回调等引擎算完的上限。飞书自己约 3 秒就超时，留 0.5 秒余量给回包。
CARD_REPLY_TIMEOUT_SEC = 2.5

#: 卡片回调拿不到引擎结果时的兜底应答（飞书要求返回一个 toast / card）。
_TOAST_IGNORED = {"toast": {"type": "info", "content": "这条操作已处理过或已失效"}}
_TOAST_FAILED = {"toast": {"type": "error", "content": "处理失败，请稍后在对话里重试"}}

#: 构建 ws 客户端时换上的那个专属事件循环（`stop()` 要用它把 `_select()` 叫醒）。
_ws_loop: asyncio.AbstractEventLoop | None = None


def _as_dict(value: Any) -> Any:
    """lark 的事件模型 -> 普通 dict（缺省值/下划线字段/None 一律跳过）。

    dispatcher 只认普通 JSON 形状；把 SDK 的模型类直接塞进去，等于让解析逻辑依赖 SDK 的
    内部结构。lark 自己发回包时也是这么转的（`Encoder` 里同样走 `vars()`）。
    """

    if isinstance(value, Mapping):
        return {key: _as_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_dict(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            key: _as_dict(item)
            for key, item in attrs.items()
            if not key.startswith("_") and item is not None
        }
    return value


def _app_credentials(settings: Any) -> tuple[str, str]:
    app_id = str(getattr(settings, "feishu_app_id", "") or "").strip()
    app_secret = str(getattr(settings, "feishu_app_secret", "") or "").strip()
    return app_id, app_secret


def _serving_loop(dispatcher: Any) -> asyncio.AbstractEventLoop | None:
    """dispatcher 上挂着的、服务自己的事件循环（`FeishuRunner.start()` 回填）。"""

    return getattr(dispatcher, "loop", None)


def _log_future(future: Any) -> None:
    """后台事件的结果只看日志：飞书那边已经 ACK 了，这里再抛也没人接。"""

    try:
        future.result()
    except Exception:
        logger.exception("feishu_ws_message_dispatch_failed 事件处理失败")


def _on_message(dispatcher: Any, data: Any) -> None:
    """SDK 线程上收到一条消息事件：转成 dict 后丢回服务循环（不等待结果）。"""

    loop = _serving_loop(dispatcher)
    if loop is None or loop.is_closed():
        logger.warning("feishu_ws_no_loop 服务事件循环还没挂上，这条消息事件被丢弃")
        return
    event = _as_dict(data)
    try:
        future = asyncio.run_coroutine_threadsafe(dispatcher.on_message_event(event), loop)
    except RuntimeError:
        logger.exception("feishu_ws_dispatch_failed 投递消息事件失败")
        return
    future.add_done_callback(_log_future)


def _on_card_action(dispatcher: Any, data: Any) -> Any:
    """SDK 线程上收到一次卡片回调：等引擎算出应答再返回（飞书要同步的回包）。"""

    loop = _serving_loop(dispatcher)
    if loop is None or loop.is_closed():
        logger.warning("feishu_ws_no_loop 服务事件循环还没挂上，这次卡片回调无法处理")
        return dict(_TOAST_FAILED)
    event = _as_dict(data)
    try:
        future = asyncio.run_coroutine_threadsafe(dispatcher.on_card_action_event(event), loop)
        response = future.result(timeout=CARD_REPLY_TIMEOUT_SEC)
    except Exception:
        logger.exception("feishu_ws_card_failed 卡片回调没能在时限内拿到应答")
        return dict(_TOAST_FAILED)
    if isinstance(response, Mapping):
        # 约定：dispatcher 回的就是 card.action.trigger 的回执本体（SDK 只认 toast 与 card 两个
        # 顶层键，包成什么形状是它的事），这里原样交回飞书，不再二次包装。
        return dict(response)
    return dict(_TOAST_IGNORED)


def _build_ws_client(dispatcher: Any, settings: Any) -> Any:
    """构建并返回 lark 的 ws 客户端——**整个仓库里唯一 import lark ws SDK 的地方**。

    必须在工作线程里被调用，因为 import 本身就会抓 `lark_oapi.ws.client.loop`。
    """

    global _ws_loop

    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as WsClient
    from lark_oapi.ws import client as ws_client_module

    # import 时抓的那个全局循环可能是主线程正在运行的循环：换成本次专用的一份。
    loop = asyncio.new_event_loop()
    ws_client_module.loop = loop
    asyncio.set_event_loop(loop)
    _ws_loop = loop

    app_id, app_secret = _app_credentials(settings)
    handler = (
        EventDispatcherHandler.builder(
            str(getattr(settings, "feishu_encrypt_key", "") or ""),
            str(getattr(settings, "feishu_verification_token", "") or ""),
        )
        .register_p2_im_message_receive_v1(lambda data: _on_message(dispatcher, data))
        .register_p2_card_action_trigger(lambda data: _on_card_action(dispatcher, data))
        .build()
    )
    return WsClient(app_id, app_secret, event_handler=handler)


class FeishuRunner:
    """把飞书长连接跑在后台线程里，并保证关得干净。

    用法只有三步：`await runner.start()`（返回是否真的连上了）、`runner.describe()` 给探针
    看、`await runner.stop()`（幂等）。`start()` 永远不抛：缺凭据、缺依赖、连不上，都是
    一条 error 日志 + 返回 False，调用方（lifespan）什么都不用额外处理。

    `client_factory(dispatcher, settings)` 是给测试用的替换点：给了它就不碰 lark，直接拿
    调用方造的对象，于是"启动不阻塞事件循环""停两次不报错"这些行为可以脱离 SDK 验证。
    """

    def __init__(self, dispatcher: Any, settings: Any, *, client_factory: Any = None) -> None:
        self._dispatcher = dispatcher
        self._settings = settings
        self._client_factory = client_factory
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._build_error: BaseException | None = None

    # --- 状态 -------------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """长连接线程还活着（或者正在建）。探针与日志都读这个。"""

        return self._task is not None and not self._task.done()

    @property
    def build_error(self) -> BaseException | None:
        """启动失败的原因（成功启动时为 None）。"""

        return self._build_error

    def describe(self) -> str:
        """一行状态，给启动日志和健康探针用（不含密钥）。"""

        app_id, _ = _app_credentials(self._settings)
        return (
            f"feishu_runner app_id={app_id or '<empty>'} "
            f"running={self.is_running} connected={self._client is not None} "
            f"error={type(self._build_error).__name__ if self._build_error else 'none'}"
        )

    # --- 生命周期 ---------------------------------------------------------------------

    async def start(self) -> bool:
        """起长连接；返回是否连上。任何失败都只记日志，绝不抛。"""

        if self.is_running:
            return True
        app_id, app_secret = _app_credentials(self._settings)
        if not app_id or not app_secret:
            logger.error(
                "feishu_runner_not_started 缺少 feishu_app_id / feishu_app_secret，"
                "飞书渠道不会启动（应用其余部分不受影响）"
            )
            return False

        self._stopping = False
        self._build_error = None
        self._loop = asyncio.get_running_loop()
        # 让 lark 的回调线程知道该把事件投回哪个循环（dispatcher 侧只读这个属性）。
        with contextlib.suppress(Exception):
            self._dispatcher.loop = self._loop

        ready = threading.Event()
        self._task = asyncio.create_task(self._serve(ready), name="feishu-ws-client")
        await asyncio.to_thread(ready.wait, READY_TIMEOUT_SEC)

        if self._client is None:
            if self._build_error is not None:
                await self._abandon_task()
                return False
            # 客户端还没建起来，但工作线程还在跑：**不收回任务**。首次 import lark 慢的时候，
            # 建起来只比窗口晚一点；收掉就再也回不来了。留着它，连上之后 is_running 自己变真，
            # 网页上的接入状态也就跟着变回「已接入」。
            logger.warning(
                "feishu_runner_still_connecting 飞书长连接在 %.0f 秒内还没建起来，"
                "先让应用启动完成，建好之后状态会自动转好",
                READY_TIMEOUT_SEC,
            )
            return False
        logger.info("feishu_runner_started %s", self.describe())
        return True

    async def stop(self) -> None:
        """停止长连接（幂等）。重复调用不会抛，也不会等第二次。"""

        task = self._task
        client = self._client
        if task is None and client is None:
            return
        self._stopping = True
        # 先摘掉任务：is_running 立刻变 False，不会有人以为它还活着。
        self._task = None
        if client is not None:
            await self._stop_client(client)
            self._client = None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT_SEC)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "feishu_runner_stop_timeout 长连接线程 %.0f 秒内没退出，强制取消",
                    STOP_TIMEOUT_SEC,
                )
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            except asyncio.CancelledError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            # 尽力收尸；daemon 线程即使卡在重连里也不影响进程退出。
            await asyncio.to_thread(thread.join, 1.0)
        logger.info(
            "feishu_runner_stopped app_id=%s", _app_credentials(self._settings)[0] or "<empty>"
        )

    # --- 内部 -------------------------------------------------------------------------

    async def _serve(self, ready: threading.Event) -> None:
        """在工作线程里跑阻塞的 client.start()，事件循环只等它结束。

        线程必须是 daemon：asyncio.to_thread 用的是默认执行器，事件循环关闭时
        shutdown_default_executor() 会 join 里面所有线程；而 lark 的 start() 在断线重连里
        可以永不返回，那样整个进程（以及 TestClient 的 portal）会卡在退出上。
        """

        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def _finish() -> None:
            if not done.done():
                done.set_result(None)

        def _run() -> None:
            try:
                self._blocking_serve(ready)
            finally:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_finish)

        thread = threading.Thread(target=_run, name="feishu-ws-client", daemon=True)
        self._thread = thread
        thread.start()
        try:
            await done
        except asyncio.CancelledError:
            logger.info("feishu_ws_task_cancelled 长连接任务被取消")
            raise
        except Exception:
            logger.exception("feishu_ws_task_failed 长连接任务异常结束")

    def _blocking_serve(self, ready: threading.Event) -> None:
        """工作线程本体：建客户端（唯一 import lark 的地方）然后阻塞在 `start()` 上。"""

        try:
            client = self._create_client()
        except BaseException as exc:  # noqa: BLE001 - 建不起来也只是渠道不可用
            self._build_error = exc
            logger.error(
                "feishu_ws_build_failed 飞书长连接客户端没能建起来：%r。"
                "多半是依赖没装（在 backend 目录执行 uv add lark-oapi）或凭据不对；"
                "应用会继续运行，飞书渠道暂不生效。",
                exc,
            )
            ready.set()
            return

        self._client = client
        self._ws_loop = _ws_loop
        ready.set()
        try:
            client.start()  # 阻塞，直到连接结束（正常或异常）
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须就地收口
            if self._stopping:
                logger.info("feishu_ws_loop_exited 长连接已按请求停止（%r）", exc)
            else:
                logger.error("feishu_ws_crashed 长连接意外结束：%r", exc, exc_info=True)

    def _create_client(self) -> Any:
        factory = self._client_factory
        if factory is not None:
            return factory(self._dispatcher, self._settings)
        # 运行时按模块全局查一次：测试用 monkeypatch 换掉 _build_ws_client 才生效。
        return _build_ws_client(self._dispatcher, self._settings)

    async def _stop_client(self, client: Any) -> None:
        """尽力让客户端断开：有 stop() 就用；没有就断连 + 停掉它自己的循环。"""

        stop = getattr(client, "stop", None)
        if callable(stop):
            try:
                if asyncio.iscoroutinefunction(stop):
                    await stop()
                else:
                    await asyncio.to_thread(stop)
            except Exception:
                logger.warning("feishu_ws_stop_failed 调用长连接 stop() 失败", exc_info=True)
            return

        disconnect = getattr(client, "_disconnect", None)
        loop = self._ws_loop
        if callable(disconnect) and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(disconnect(), loop).result(timeout=3.0)
            except Exception:
                logger.warning("feishu_ws_disconnect_failed 断开长连接失败", exc_info=True)
            with contextlib.suppress(RuntimeError):
                # 叫醒 start() 里那个 run_until_complete(_select())。
                loop.call_soon_threadsafe(loop.stop)
            return

        logger.warning(
            "feishu_ws_no_stop 这个版本的 lark-oapi 既没有 stop() 也无法断开，"
            "长连接会随进程退出被回收"
        )

    async def _abandon_task(self) -> None:
        """启动失败时把已经派出去的线程任务收回来，别留下一个后台任务。"""

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
