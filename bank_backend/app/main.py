"""FastAPI 应用入口。

所有响应统一为 {code, message, data}；业务错误由 BankError 处理器转成文档规定的
错误码与 HTTP 状态，参数校验失败统一转为 1012/422。

访问控制分两层，都在 `_guard` 里：

* **服务凭证**（`X-Service-Token`）——证明请求来自我们自己的 app 后端。缺了它，客户级
  接口一律 401。这是「谁是调用方」，不是「调用方是谁」。
* **运维口令**（`X-Ops-Token`）——只加在清库这种**不可逆**、且 app 后端明确不需要的端点上。
  app 拿不到它，所以 app 被利用也清不掉别人的余额。

`/api/clock*` 刻意**不在**运维口令范围内：app 的演示控制面（前端「推进一天／一小时」按钮）
就是在代理这个接口，给它加第二把钥匙等于让按钮失效。推时钟是全局副作用，但可逆——再推
回去就行——而且仍受服务凭证保护，外部进程推不动。真正不可逆的只有 `POST /api/seed/*`。

用户身份不在这里判断：`user_id` 由调用方明确给出，各领域服务再校验资源归属。
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.card_actions import router as card_action_router
from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import (
    BankError,
    OpsUnauthorizedError,
    ServiceUnauthorizedError,
    ValidationError,
)

DESCRIPTION = """
面向 AI Agent 演示的完整功能 Mock 银行后端。首版使用 PostgreSQL，
不做终端用户登录、JWT 或真实 MFA；调用方必须明确给出 user_id，服务端校验资源归属。
所有调用需要服务凭证，清库与时钟另需运维口令。
金额统一为 CNY 整数分。所有写接口在数据库事务中更新状态，并支持幂等键。
"""

# 无需凭证：健康检查给容器编排用，文档路由给排查用。都不含任何客户数据。
_GUARD_EXEMPT = frozenset(
    {"/api/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)
# 需要运维口令的端点：只有不可逆的清库/重建种子。`/api/clock` 不在其中——见模块注释。
_OPS_PROTECTED = ("/api/seed",)


def _matches(expected: str, presented: str | None) -> bool:
    """常量时间比较；空预期值一律视为不匹配（= 拒绝）。"""

    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="模拟银行系统 API",
        version="1.3.0",
        description=DESCRIPTION,
        servers=[{"url": "http://localhost:8000", "description": "本地开发"}],
    )
    application.include_router(card_action_router, prefix="/api")
    application.include_router(router, prefix="/api")

    # 用中间件而不是逐路由依赖：60 多个端点，漏一个就是一个越权口子；中间件按路径
    # 白名单反向控制，新增端点默认是「受保护」，忘记加依赖不会变成漏洞。
    @application.middleware("http")
    async def _guard(request: Request, call_next):
        path = request.url.path
        if path not in _GUARD_EXEMPT:
            current = get_settings()
            if not _matches(current.service_token, request.headers.get("X-Service-Token")):
                refused = ServiceUnauthorizedError()
                return JSONResponse(status_code=refused.http_status, content=refused.to_payload())
            if path.startswith(_OPS_PROTECTED) and not _matches(
                current.ops_token, request.headers.get("X-Ops-Token")
            ):
                refused = OpsUnauthorizedError()
                return JSONResponse(status_code=refused.http_status, content=refused.to_payload())
        return await call_next(request)

    @application.exception_handler(BankError)
    async def _bank_error(_: Request, exc: BankError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = exc.errors()
        reason = details[0].get("msg") if details else None
        error = ValidationError(reason or "参数校验失败")
        return JSONResponse(status_code=error.http_status, content=error.to_payload())

    _ = settings
    return application


app = create_app()

