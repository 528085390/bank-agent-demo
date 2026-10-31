"""账单的处理范围：**这一份账单**，还是**某一段时间**。

分析、发信两个工具认同一套规则：

- 参数里有 ``task_id``（前端在「分析/发送这份流水」时自动带上的任务号）→ 范围是这份任务
  对应的账单文档，期间取文档解析出来的真实起止。任务号来自消息文本、可能被别人转述，
  所以归属校验在这里做 —— 读不到别人的账单。
- 否则要 ``period_start`` / ``period_end``：按日期区间跨该用户已入库的账单，既有行为。

放在一处而不是两个 handler 各写一份：两条路一旦漂开，就会出现「屏幕上是这份、邮件里是
那段时间」这种用户看不出来的差异。
"""

from __future__ import annotations

import uuid
from datetime import date

from app.modules.base import ToolFailure
from app.modules.dates import parse_tool_date
from app.services.bill_import import document_for_task
from app.tools.protocol import ErrorCode

#: 模型爱把「没给」写成这些字面量；它们不是任务号。
_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})


def resolve_scope(
    database, user_id: str, args: dict
) -> tuple[date, date, uuid.UUID | None, str | None]:
    """把工具参数解析成 ``(起, 止, 文档 id, 任务号)``，两者都没有就报 VALIDATION。

    ``文档 id`` 为 None 表示这次按日期区间取数（跨文档）；给了它表示只取那一份。
    """

    raw_task = args.get("task_id")
    task_id = raw_task.strip() if isinstance(raw_task, str) else ""
    if task_id and task_id.lower() not in _PLACEHOLDERS:
        with database.sessions() as db:
            doc = document_for_task(db, task_id, user_id)
            if doc is None:
                raise ToolFailure(
                    ErrorCode.NOT_FOUND,
                    "没有找到这次导入的流水，请重新上传后再试。",
                )
            # 在会话内取好值：离开 with 之后只剩下一个 detached 实例。
            return doc.period_start, doc.period_end, doc.id, task_id

    if not args.get("period_start") or not args.get("period_end"):
        raise ToolFailure(
            ErrorCode.VALIDATION,
            "请说明要处理的期间，或指明刚导入的是哪一份账单。",
        )
    # 日期走共享解析器（app/modules/dates.py）：模型把「9月13日」「今天」原样传进来
    # 也算数，三个账单工具认同一套写法。
    return (
        parse_tool_date(args["period_start"], "period_start"),
        parse_tool_date(args["period_end"], "period_end"),
        None,
        None,
    )
