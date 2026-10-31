import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.auth import router as auth_router
from app.api.bill import router as bill_router
from app.api.channels import channel_bindings_for
from app.api.channels import router as channels_router
from app.api.demo import router as demo_router
from app.api.feishu_setup import router as feishu_setup_router
from app.api.reminders import router as reminders_router
from app.api.routes import router
from app.audit.routes import router as audit_router
from app.channels.feishu.boot import feishu_bridge_for
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.eventloop import configure_event_loop
from app.core.proxy_env import sanitize_no_proxy
from app.core.types import new_id
from app.integration.llm_config import LlmConfigManager, configure_from_settings
from app.modules.card.routes import router as card_router
from app.runtime import Runtime
from app.storage.checkpointer import postgres_checkpointer, require_checkpoint_tables

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, runtime=None):
    configure_event_loop()  # psycopg's async connections need the selector loop on Windows
    # A NO_PROXY entry httpx cannot parse fails every httpx client in the process at
    # construction time -- the model boundary included, with a misleading message. Clean
    # it before anything builds a transport; see app.core.proxy_env.
    sanitize_no_proxy()
    settings = settings or get_settings()
    # An injected runtime owns its own agent, including an in-memory checkpointer.
    owns_runtime = runtime is None
    rt = runtime or Runtime(settings)
    if rt.llm is None:
        rt.llm = LlmConfigManager(rt)

    @asynccontextmanager
    async def lifespan(app):
        # 进程内唯一的那一份绑定管理器：Web 端点与飞书渠道必须拿到同一个对象（绑定码的摘要
        # 盐是进程局部的）。注入的 runtime 也一样，测试走的也是这条路径。
        binding_mgr = channel_bindings_for(app, rt)
        if not owns_runtime:
            try:
                yield
            finally:
                rt.database.dispose()
            return
        # A deployment initializes the checkpoint tables before the app starts; the app
        # only verifies them, because a running service must not migrate its own storage.
        await require_checkpoint_tables(settings.database_url)
        async with postgres_checkpointer(settings.database_url) as saver:
            rt.agent.use_checkpointer(saver)
            # A deployment may name the model in its environment, which is what lets the
            # process come up already configured. This runs only for the production wiring:
            # an injected runtime (a test) owns its own model and must not be reconfigured.
            configure_from_settings(rt.llm, settings)
            # The Feishu IM bridge is optional: disabled it builds nothing, and a bridge that
            # cannot start degrades to a log line instead of failing the deployment.
            feishu_bridge = feishu_bridge_for(app, rt, binding_mgr)
            await feishu_bridge.start()
            try:
                yield
            finally:
                # The Feishu long connection must be down before the runtime is torn down:
                # it holds tasks on this loop and sends through the same event loop.
                await feishu_bridge.stop()
                # The runtime owns the bank client's connection pool; the checkpointer is
                # closed by the context manager above.
                await rt.aclose()
                rt.database.dispose()

    app = FastAPI(title="Bank Agent Runtime", version="0.1.0", lifespan=lifespan)
    app.state.runtime = rt

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = new_id("req")
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        # 设计 §7.3：带身份的响应不许进浏览器或中间层缓存。一个人退出、另一个人在同一台
        # 电脑上登录之后，回退键与缓存都不该把上一个人的会话、报告、账单再放出来。
        # SSE 自己声明了 `no-cache, no-transform`（它不能被缓冲），所以这里只补没有声明的。
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    def error_response(request, error):
        request_id = getattr(request.state, "request_id", new_id("req"))
        return JSONResponse(
            status_code=error.status,
            content={"request_id": request_id, "data": None, "error": error.as_dict()},
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return error_response(
            request,
            AppError(
                "INVALID_PARAMETER",
                "请求参数格式不正确",
                422,
                details={
                    "fields": [".".join(str(x) for x in error["loc"]) for error in exc.errors()]
                },
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        message = "接口不存在" if exc.status_code == 404 else "此请求不受支持"
        return error_response(request, AppError("HTTP_ERROR", message, exc.status_code))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        logger.error(
            "database_error request_id=%s type=%s", request.state.request_id, type(exc).__name__
        )
        return error_response(
            request,
            AppError("DATABASE_UNAVAILABLE", "数据服务暂不可用，请稍后重试", 503, retryable=True),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error(
            "unhandled_error request_id=%s type=%s", request.state.request_id, type(exc).__name__
        )
        return error_response(
            request, AppError("INTERNAL_ERROR", "服务暂时异常，请稍后重试", 500, retryable=True)
        )

    app.include_router(router)
    # Accounts are a separate surface from the conversation API for the same reason the audit
    # query is: they are the only routes that answer "who is calling" without being called on
    # behalf of an existing conversation.
    app.include_router(auth_router)
    # The audit query is a separate router because it is a separate kind of surface: it
    # returns the complete model and tool payloads the conversation API deliberately never
    # does, and keeping it in its own module is what makes that boundary visible.
    app.include_router(audit_router)
    app.include_router(card_router)
    app.include_router(bill_router)
    # The presentation control plane moves the simulated clock, so it is a person's surface
    # and not a model tool; the routes themselves refuse to run outside demo mode.
    app.include_router(demo_router)
    # Renewal reminders are a projection of the bank's subscription list, read by the
    # workspace shell so a charge can be announced before it happens.
    app.include_router(reminders_router)
    # The channel surface: the standalone MFA page a Feishu card's button links to, its
    # submission, and the binding code a user asks for from the web app. It is registered
    # next to the SPA routes because it is the same HTTP surface, but it is deliberately
    # token-authenticated rather than session-authenticated: the phone that opens the link
    # has no cookie, and the link itself is the credential.
    app.include_router(channels_router)
    # The onboarding surface is the web app's own: it is session-authenticated like the
    # binding code, and it only ever talks to this process.
    app.include_router(feishu_setup_router)
    return app


app = create_app()
