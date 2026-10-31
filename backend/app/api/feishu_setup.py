"""Web 端的「IM 接入」端点：出码、查状态、取消扫码、取消接入。

四个端点和绑定码那一端同一个前提：调用者是已经进了演示会话的人（identity），接口只对**这台
后端进程**说话。真正的设备流在 app.channels.feishu.onboarding，长连接在 app.channels.feishu.boot；
这里只把它们的返回值装进统一信封。

为什么 device_code 不出现在任何响应里：它本身就是一枚能换 client_secret 的句柄，前端只需要
setup_id。二维码 SVG 由服务端渲染（前端没有 QR 依赖），短链原样给出，方便用户手输。
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Request

from app.api.channels import channel_bindings_for
from app.api.routes import envelope, identity, runtime
from app.channels.feishu.boot import feishu_bridge_for
from app.channels.feishu.onboarding import FeishuOnboarding, UnknownSetup
from app.core.errors import AppError

router = APIRouter(prefix="/api/channels/feishu", tags=["channels"])

#: 一次进程一份：会话表（谁在等扫码）与长连接句柄都必须唯一，所以这里也只建一份。
_onboarding_lock = threading.Lock()


def feishu_onboarding_for(app: Any, rt: Any) -> FeishuOnboarding:
    """这个应用的那一份扫码编排器，挂在 app.state.feishu_onboarding 上。

    测试会直接往 app.state 里塞一个注入了假 transport 的实例；生产路径在这里补建，并把
    app.state.feishu_bridge 那条长连接句柄交给它——热启动必须落在同一条连接上。
    """

    onboarding = getattr(app.state, "feishu_onboarding", None)
    if onboarding is not None:
        return onboarding
    with _onboarding_lock:
        onboarding = getattr(app.state, "feishu_onboarding", None)
        if onboarding is None:
            bridge = feishu_bridge_for(app, rt, channel_bindings_for(app, rt))
            onboarding = FeishuOnboarding(rt.settings, bridge=bridge)
            app.state.feishu_onboarding = onboarding
        return onboarding


def _onboarding(request: Request) -> FeishuOnboarding:
    return feishu_onboarding_for(request.app, runtime(request))


_SETUP_GONE = "这次扫码已经失效了，请重新点击接入"


@router.get("/status")
async def feishu_status(request: Request):
    """面板打开时先问这一句：配没配、连没连、几个人绑着。"""

    await identity(request)
    return envelope(request, _onboarding(request).status(runtime(request)))


@router.post("/setup")
async def feishu_start_setup(request: Request):
    """点「接入」就是这一步：给出二维码、短链和这次扫码的编号。"""

    await identity(request)
    return envelope(request, await _onboarding(request).begin())


@router.get("/setup/{setup_id}")
async def feishu_poll_setup(request: Request, setup_id: str):
    """前端按 2 秒轮询这一句；服务端自己按飞书给的 interval 控制真正去问的频率。"""

    await identity(request)
    try:
        return envelope(request, await _onboarding(request).poll(setup_id))
    except UnknownSetup as error:
        raise AppError("SETUP_NOT_FOUND", _SETUP_GONE, 404) from error


@router.delete("/setup/{setup_id}")
async def feishu_cancel_setup(request: Request, setup_id: str):
    """用户扫到一半改主意了：关掉码，别再去问飞书（幂等）。"""

    await identity(request)
    try:
        return envelope(request, _onboarding(request).cancel(setup_id))
    except UnknownSetup as error:
        raise AppError("SETUP_NOT_FOUND", _SETUP_GONE, 404) from error


@router.post("/disconnect")
async def feishu_disconnect(request: Request):
    """取消接入：停长连接、把凭据从 .env.channels 里抹掉、清掉飞书绑定。"""

    await identity(request)
    return envelope(request, await _onboarding(request).disconnect(runtime(request)))
