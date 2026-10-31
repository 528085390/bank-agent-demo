"""邮件工具的 handler 实现（符合 ModuleSpec 的签名要求）。

这些 handler 符合基底的签名：handler(invocation, client) -> ToolData
但不使用 BankClient，而是通过闭包捕获 NotificationService。

发信拆成两步（Task 9）：`bill_send_report` 只**冻结**这次操作 —— 校验收件人、生成
`action_id`、把要发的东西写进 `facts`，然后交给用户确认；真正的 PDF 生成与发信在
`bill_send_report_execute` 里，只有确认那一下才会走到。冻结阶段一次都不发信，
连 PDF 都不生成 —— 那是 execute 的活。

支持的邮件类型：
1. 交易流水（CSV/Excel）
2. 账单报告（PDF/Excel）
3. 深度分析报告（PDF）
"""

from __future__ import annotations

import logging
from uuid import uuid4

from app.modules.base import ToolData, ToolFailure
from app.modules.bill.scope import resolve_scope
from app.notification.analysis_pdf import (
    build_analysis_report_pdf,
    format_report_time,
    safe_analysis_filename,
)
from app.tools.protocol import ErrorCode, ToolInvocation

logger = logging.getLogger(__name__)


def create_email_handlers(notification_service):
    """工厂函数：创建邮件工具的 handler，通过闭包捕获 NotificationService。

    返回的 handler 符合基底的签名：handler(invocation, client) -> ToolData
    但忽略 client 参数，使用闭包中的 notification_service。

    Args:
        notification_service: app.notification.service.NotificationService 实例

    Returns:
        dict[str, ToolHandler]: 工具名 -> handler 的映射
    """

    def resolve_recipient(invocation: ToolInvocation) -> str:
        """这次发信发给谁：显式 `recipient`，否则用户绑定的邮箱；都没有就报 VALIDATION。

        prepare 与 execute 共用这一处 —— execute 拿到的收件人必须与用户在确认卡上看到的
        是同一个，两份实现迟早会漂。
        """

        args = invocation.call.arguments
        recipient = args.get("recipient")

        # 模型把「不指定」写成字符串的情况：参数声明是可空的，它有时传的是字面量
        # `"null"` / `"none"`，而不是 JSON 的 `null`。那不是地址，当没写处理 ——
        # 否则这一封信会被发到一个叫 `null` 的收件人上。
        if isinstance(recipient, str) and recipient.strip().lower() in {
            "",
            "null",
            "none",
            "undefined",
        }:
            recipient = None

        # 如果未指定收件人，使用用户绑定的邮箱
        if not recipient:
            # 身份在 `context` 上，不是 invocation 自己 —— ToolInvocation 只有
            # agent_id / call / context，写 `invocation.user_id` 会直接 AttributeError。
            user_email = notification_service.get_user_email(invocation.context.user_id)
            if not user_email:
                raise ToolFailure(
                    ErrorCode.VALIDATION,
                    "您还没有绑定邮箱。请先在右上角设置里配置邮箱地址，然后我再发送报告。",
                )
            recipient = user_email
        return recipient

    def prepare_send_report(invocation: ToolInvocation, _client) -> ToolData:
        """冻结一次发信：校验收件人、生成 `action_id`，**不发信**。

        工具参数:
            task_id: 这份账单的任务号（可选）；给了就按这份文件发，和屏幕上的报告同一口径
            period_start / period_end: 要分析的时间段（YYYY-MM-DD，与 task_id 二选一）
            recipient: 收件人邮箱（可选，默认使用用户绑定的邮箱）

        冻结阶段只做四件事：解析范围、归一收件人、取收件人、生成 `action_id`。
        `BillAnalyzer` / `build_analysis_report_pdf` / `send_email` 一个都不碰 ——
        用户还没点确认，这一轮就不该有任何东西发出去。
        """
        args = invocation.call.arguments
        # 有任务号就按那份账单（范围与屏幕报告一致）；否则按日期区间。这条规则与
        # 分析工具共用，见 app/modules/bill/scope.py。
        period_start, period_end, _document_id, task_id = resolve_scope(
            notification_service.database, invocation.context.user_id, args
        )
        recipient = resolve_recipient(invocation)
        action_id = f"bill-report-{uuid4().hex}"
        frozen = {
            "action_id": action_id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "recipient": recipient,
            # 冻结之后换绑邮箱改不了这一封，任务号同理：执行时用的就是冻结下来的这份。
            "task_id": task_id,
        }
        return ToolData(
            summary=f"准备发送账单报告到 {recipient}，请确认",
            display=dict(frozen),
            facts=dict(frozen),
        )

    def execute_send_report(invocation: ToolInvocation, _client) -> ToolData:
        """按时间段生成深度分析报告并发送到邮箱（PDF 格式）—— 确认之后才走这一步。

        报告**在这里生成**，不接受模型传进来的分析结果：那份结果跨了一轮对话，模型
        拿不到也复述不出来（它只会在参数校验那里被判 DENIED）。模型只需说清分析哪
        一段时间。
        """
        from app.services.bill_analysis import BillAnalyzer

        args = invocation.call.arguments
        user_id = invocation.context.user_id
        # 与分析工具同一套范围规则：快照里带任务号就按那份账单重算，内容与屏幕上的报告
        # 一致；没有任务号才按日期区间（既有行为）。
        period_start, period_end, document_id, _task_id = resolve_scope(
            notification_service.database, user_id, args
        )
        recipient = resolve_recipient(invocation)

        # 分析 + 生成 PDF
        try:
            analyzer = BillAnalyzer(notification_service.database)
            analysis_result = analyzer.analyze(
                user_id, period_start, period_end, document_id=document_id
            )
            pdf_bytes = build_analysis_report_pdf(analysis_result)
        except Exception as exc:
            logger.error(f"生成深度分析报告失败: {exc}", exc_info=True)
            raise ToolFailure(ErrorCode.INTERNAL, f"生成报告失败: {str(exc)}") from exc

        # 邮件正文用分析结果自带的期间
        date_range = analysis_result.get("report_meta", {}).get("date_range", {})
        period_str = f"{date_range.get('start', '-')} ~ {date_range.get('end', '-')}"

        # 构造邮件内容
        subject = f"账单深度分析报告 - {period_str}"
        # 与 PDF 同一个格式化：generated_at 是 UTC，正文里要写本地时间。
        generated_at = format_report_time(
            analysis_result.get('report_meta', {}).get('generated_at')
        )
        body = f"""您好，

这是您的账单深度分析报告。

分析区间：{period_str}
交易总数：{analysis_result.get('report_meta', {}).get('total_transactions', 0)} 笔
生成时间：{generated_at}

报告详情请查看附件PDF文档。

此邮件由系统自动发送，请勿回复。
"""

        # 准备附件。附件必须是 Attachment 对象（文件名 + 字节），不是 dict ——
        # 队列模式下真正发信的是另一个进程，路径式附件在那边不存在。
        from app.notification.attachments import Attachment

        filename = safe_analysis_filename(date_range)
        attachment = Attachment(filename=filename, content=pdf_bytes)

        # 发送邮件
        try:
            result = notification_service.send_email(
                recipient=recipient,
                subject=subject,
                html=body,
                attachments=[attachment],
                task_id=f"analysis_{user_id}_{period_str}",
            )
        except Exception as exc:
            logger.error(f"发送深度分析邮件失败: {exc}", exc_info=True)
            raise ToolFailure(ErrorCode.INTERNAL, f"邮件发送失败: {str(exc)}") from exc

        # 返回结果。`send_email` 回的是 {notification_id, status, error}，
        # 成功与否看 status，没有 success 这个键。
        if result and result.get("status") in {"SENT", "QUEUED"}:
            return ToolData(
                summary=f"深度分析报告（PDF）已发送到 {recipient}",
                display={
                    "recipient": recipient,
                    "period": period_str,
                    "format": "PDF",
                    "queued": result.get("queued", False),
                },
                facts={
                    "recipient": recipient,
                    "period": period_str,
                    "format": "pdf",
                    "notification_id": result.get("notification_id"),
                    "queued": result.get("queued", False),
                },
            )
        else:
            error_msg = result.get("error") if result else "邮件发送失败"
            raise ToolFailure(ErrorCode.INTERNAL, error_msg)

    # 返回工具名 -> handler 的映射。键必须与 bill/tools.py 的工具名一一对应：
    # 冻结那一个走模型菜单，执行那一个是隐藏的（由确认动作直接调用）。
    return {
        "bill_send_report": prepare_send_report,
        "bill_send_report_execute": execute_send_report,
    }
