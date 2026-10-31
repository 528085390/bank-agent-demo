"""账单深度分析工具的 handler 实现。"""

from app.modules.base import ToolData, ToolFailure
from app.modules.bill.scope import resolve_scope
from app.modules.money import format_minor
from app.services.bill_analysis import BillAnalyzer
from app.tools.protocol import ErrorCode, ToolInvocation


def create_analysis_handlers(database):
    """创建分析工具的 handler。

    Args:
        database: Database 实例

    Returns:
        dict[str, ToolHandler]: 工具名 -> handler 的映射
    """

    def analyze_bill_comprehensive_handler(invocation: ToolInvocation, _client) -> ToolData:
        """深度分析账单（符合 ToolHandler 签名）。

        注意：
        - _client 参数被忽略（但必须存在以符合 ToolHandler 签名）
        - 使用闭包中的 database
        """
        args = invocation.call.arguments
        user_id = invocation.context.user_id
        modules = args.get("modules")

        period_start, period_end, document_id, task_id = resolve_scope(
            database, user_id, args
        )

        try:
            analyzer = BillAnalyzer(database)
            result = analyzer.analyze(
                user_id, period_start, period_end, modules, document_id=document_id
            )

            # 按文件分析时把任务号留在报告里：发信那一步要靠它定位同一份账单，
            # 否则邮件会按日期重算，把同期的另一份账单也算进来。
            if task_id:
                result["report_meta"]["task_id"] = task_id

            # 提取关键信息
            total_txs = result["report_meta"]["total_transactions"]
            module_count = len(result["modules"])

            # 提取风险标签（如果有）
            risk_tags = []
            for module in result["modules"]:
                if module["module_id"] == "customer_risk_profile":
                    risk_tags = module["data"].get("risk_tags", [])
                    break

            # 构建 summary
            summary_parts = [
                f"账单深度分析完成（{period_start} ~ {period_end}）",
                f"共{total_txs}笔交易",
                f"执行{module_count}个分析模块",
            ]

            # 两跳编排里后一个领域只拿得到 summary，收支数字必须进来，
            # 否则「按账单定生日预算」只能靠模型猜。外部口径剔除本人账户互转，
            # 与报告页展示的现金流入/流出是同一组数。
            external = (
                (result.get("report_meta") or {}).get("cashflow") or {}
            ).get("external") or {}
            income = external.get("income_minor")
            expense = external.get("expense_minor")
            net = external.get("net_minor")
            if all(isinstance(item, int) and not isinstance(item, bool) for item in (income, expense, net)):
                summary_parts.append(
                    "对外收入 "
                    f"{format_minor(income)}，对外支出 {format_minor(expense)}，"
                    f"净结余 {format_minor(net)}"
                )

            if risk_tags:
                summary_parts.append(
                    f"检测到{len(risk_tags)}类风险特征：{', '.join(t['tag'] for t in risk_tags[:3])}"
                )

            return ToolData(
                summary="，".join(summary_parts),
                display=result,  # 完整的分析结果放在 display 中，供前端渲染
                facts=result,  # 也保留在 facts 中
            )
        except ToolFailure:
            raise
        except Exception as exc:
            raise ToolFailure(ErrorCode.INTERNAL, f"账单分析失败：{str(exc)}") from exc

    return {"analyze_bill_comprehensive": analyze_bill_comprehensive_handler}
