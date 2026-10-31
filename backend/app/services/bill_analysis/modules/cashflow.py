"""模块5：月度收支趋势分析。"""

from collections import defaultdict

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class CashflowModule:
    """月度收支趋势分析模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        # 排除内部交易
        external_txs = [tx for tx in transactions if not tx.is_internal]

        # 按月统计
        monthly = defaultdict(lambda: {"income": 0, "expense": 0})
        for tx in external_txs:
            month = tx.month
            if tx.is_income:
                monthly[month]["income"] += tx.amount
            else:
                monthly[month]["expense"] += tx.amount

        # 构建月度列表
        monthly_list = []
        for month in sorted(monthly.keys()):
            stats = monthly[month]
            net = stats["income"] - stats["expense"]
            monthly_list.append(
                {
                    "month": month,
                    "income_minor": stats["income"],
                    "expense_minor": stats["expense"],
                    "net_minor": net,
                    "is_deficit": net < 0,
                }
            )

        result = ModuleResult(
            module_id="monthly_cashflow",
            title="月度收支趋势分析",
            data={"monthly_data": monthly_list},
        )

        # 生成洞察
        deficit_months = [m for m in monthly_list if m["is_deficit"]]
        if deficit_months:
            for month_data in deficit_months:
                result.add_insight(
                    f"{month_data['month']}月净现金流为负"
                    f"{abs(month_data['net_minor'])/100:.2f}元，收支倒挂，需关注",
                    "推断",
                    0.9,
                )
        else:
            result.add_insight("各月收支健康，未出现收支倒挂", "事实", 1.0)

        return result
