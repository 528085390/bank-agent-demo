"""bill agent 的工具处理器：交易明细查询。

符合基底的 ``ToolHandler`` 签名 ``(invocation, client) -> ToolData``：``client``
参数被忽略（账单查询只读本地数据库，不经过 bank_backend），数据库句柄通过闭包捕获，
与 ``email_handlers`` 捕获 ``NotificationService`` 是同一套路。

职责边界（见《修正方案》）：agent 只查询与分析**已入库**的数据；
上传、解压、解析、入库属于后台服务，不经过 agent。
"""

from __future__ import annotations

import logging

from app.core.errors import AppError
from app.modules.base import ToolData, ToolFailure
from app.modules.bill.scope import resolve_scope
from app.services.bill_query import BillQueryService
from app.tools.protocol import ErrorCode, ToolInvocation

logger = logging.getLogger(__name__)


def _user_id(invocation: ToolInvocation) -> str:
    user_id = invocation.context.user_id
    if not user_id:
        raise ToolFailure(ErrorCode.NO_CUSTOMER_BINDING, "无法确定当前用户身份")
    return user_id


def _not_found(exc: AppError, message: str) -> ToolFailure:
    """把查询服务的业务错误映射成工具错误码。"""

    if exc.code == "NOT_FOUND":
        return ToolFailure(ErrorCode.NOT_FOUND, message)
    return ToolFailure(ErrorCode.VALIDATION, exc.message)


def create_bill_tool_handlers(database):
    """工厂函数：用闭包捕获数据库句柄，返回工具名 -> handler 的映射。"""

    service = BillQueryService(database)

    def query_transactions_handler(invocation: ToolInvocation, _client) -> ToolData:
        args = invocation.call.arguments
        user_id = _user_id(invocation)

        # 与分析、发信共用同一套范围规则：带任务号就查这一份账单，否则按日期区间。
        period_start, period_end, document_id, task_id = resolve_scope(database, user_id, args)

        try:
            result = service.get_transactions(
                user_id,
                period_start,
                period_end,
                category=args.get("category"),
                direction=args.get("direction"),
                # `or` 而不是 get 的默认值：这些参数声明成可空之后模型会显式传 `null`，
                # 而 `args.get("page", 1)` 拿到的是 None —— `int(None)` 会直接崩。
                page=int(args.get("page") or 1),
                # 翻页由页面表格负责；模型传 100/200 会把整段流水塞进一页，
                # 页面就没有分页可用了。这里固定每页 20 条，保持 UI 一致。
                page_size=20,
                document_id=document_id,
            )
        except AppError as exc:
            raise _not_found(
                exc,
                "该时间段没有交易明细。若尚未上传该周期的流水，"
                "请提示用户点击界面右上角「上传流水」上传后再查。",
            ) from exc

        return ToolData(
            summary=f"查询到 {result['total']} 笔交易（第 {result['page']} 页，共 {len(result['items'])} 条）",
            # 表格翻页需要范围：按日期查的用起止日期，按文件查的用任务号。任务号随消息
            # 一起保存，换设备/刷新后仍能直接翻页，不必重新问模型。
            display={
                **result,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "task_id": task_id,
            },
            facts={
                "total": result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
                "items": result["items"],
                "task_id": task_id,
            },
        )

    # 注意映射键必须与 QUERY_TOOLS 里的工具名一一对应：
    # 声明了却没有 handler，Agent 一调用就会失败。
    return {
        "query_transactions": query_transactions_handler,
    }
