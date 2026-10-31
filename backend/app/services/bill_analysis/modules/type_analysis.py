"""模块4：交易类型占比分析。"""

from collections import defaultdict

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class TypeAnalysisModule:
    """类型占比分析模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        # 按分类统计（只统计支出）
        expense_txs = [tx for tx in transactions if not tx.is_income]
        categories = defaultdict(lambda: {"count": 0, "amount": 0})
        total_expense = sum(tx.amount for tx in expense_txs)

        for tx in expense_txs:
            cat = tx.category
            categories[cat]["count"] += 1
            categories[cat]["amount"] += tx.amount

        # 构建分类列表
        category_list = []
        for cat, stats in categories.items():
            category_list.append(
                {
                    "category": cat,
                    "count": stats["count"],
                    "count_pct": (
                        round(stats["count"] / len(expense_txs) * 100, 1)
                        if expense_txs
                        else 0
                    ),
                    "amount_minor": stats["amount"],
                    "amount_pct": (
                        round(stats["amount"] / total_expense * 100, 1)
                        if total_expense
                        else 0
                    ),
                }
            )

        # 按金额占比排序
        category_list.sort(key=lambda x: x["amount_minor"], reverse=True)

        # 识别高频私对私转账
        p2p_transfers = [
            tx
            for tx in transactions
            if tx.category == "转账" and tx.counterparty_tag == "个人"
        ]
        p2p_count = len(p2p_transfers)
        p2p_amount = sum(tx.amount for tx in p2p_transfers)

        result = ModuleResult(
            module_id="transaction_type_analysis",
            title="交易类型占比分析",
            data={
                "categories": category_list,
                "p2p_transfers": {
                    "count": p2p_count,
                    "amount_minor": p2p_amount,
                },
            },
        )

        # 生成洞察
        if category_list:
            top_category = category_list[0]
            result.add_insight(
                f"交易以{top_category['category']}为主，"
                f"占比{top_category['amount_pct']}%",
                "事实",
                1.0,
            )

            if p2p_count > 0:
                result.add_insight(
                    f"其中私对私转账{p2p_count}笔，金额{p2p_amount/100:.2f}元",
                    "事实",
                    1.0,
                )

        return result
