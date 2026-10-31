"""模块7：高频交易对手分析。"""

from collections import defaultdict
from datetime import timedelta

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class CounterpartyModule:
    """对手分析模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        if not transactions:
            return ModuleResult(
                module_id="frequent_counterparties",
                title="高频交易对手分析",
                data={"time_slices": {}, "tag_distribution": []},
            )

        # 时间切片
        latest_date = max(tx.tx_time for tx in transactions).date()
        time_slices = {
            "recent_7d": latest_date - timedelta(days=7),
            "recent_15d": latest_date - timedelta(days=15),
            "recent_1m": latest_date - timedelta(days=30),
            "recent_3m": latest_date - timedelta(days=90),
            "all": min(tx.tx_time for tx in transactions).date(),
        }

        # 对每个时间切片统计
        slice_results = {}
        for slice_name, start_date in time_slices.items():
            slice_txs = [tx for tx in transactions if tx.tx_time.date() >= start_date]
            if slice_txs:
                slice_results[slice_name] = CounterpartyModule._analyze_slice(
                    slice_txs
                )

        # 对手标签分布（基于全周期）
        tag_stats = defaultdict(lambda: {"count": 0, "amount": 0})
        total_count = len(transactions)
        total_amount = sum(tx.amount for tx in transactions)

        for tx in transactions:
            tag = tx.counterparty_tag
            tag_stats[tag]["count"] += 1
            tag_stats[tag]["amount"] += tx.amount

        tag_distribution = [
            {
                "tag": tag,
                "count_pct": round(stats["count"] / total_count * 100, 1),
                "amount_pct": round(stats["amount"] / total_amount * 100, 1),
            }
            for tag, stats in tag_stats.items()
        ]

        result = ModuleResult(
            module_id="frequent_counterparties",
            title="高频交易对手分析",
            data={"time_slices": slice_results, "tag_distribution": tag_distribution},
        )

        # 生成洞察
        if "all" in slice_results and slice_results["all"]["by_frequency"]:
            top_by_freq = slice_results["all"]["by_frequency"][0]
            result.add_insight(
                f"全周期内交易最频繁的对手为{top_by_freq['counterparty']}，"
                f"共{top_by_freq['count']}笔，累计{top_by_freq['total_amount']/100:.2f}元",
                "事实",
                1.0,
            )

        return result

    @staticmethod
    def _analyze_slice(transactions: list[ProcessedTransaction]) -> dict:
        """分析单个时间切片。"""
        # 按对手统计
        counterparties = defaultdict(
            lambda: {"count": 0, "total_amount": 0, "income": 0, "expense": 0}
        )

        for tx in transactions:
            cp = tx.counterparty_norm
            counterparties[cp]["count"] += 1
            counterparties[cp]["total_amount"] += tx.amount
            if tx.is_income:
                counterparties[cp]["income"] += tx.amount
            else:
                counterparties[cp]["expense"] += tx.amount

        # 按频次排序 TOP7
        by_frequency = sorted(
            [
                {
                    "counterparty": cp,
                    "count": stats["count"],
                    "total_amount": stats["total_amount"],
                    "income": stats["income"],
                    "expense": stats["expense"],
                    "tag": transactions[0].counterparty_tag,  # 简化处理
                }
                for cp, stats in counterparties.items()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )[:7]

        # 按金额排序 TOP7
        by_amount = sorted(
            [
                {
                    "counterparty": cp,
                    "count": stats["count"],
                    "total_amount": stats["total_amount"],
                    "income": stats["income"],
                    "expense": stats["expense"],
                    "tag": transactions[0].counterparty_tag,
                }
                for cp, stats in counterparties.items()
            ],
            key=lambda x: x["total_amount"],
            reverse=True,
        )[:7]

        return {"by_frequency": by_frequency, "by_amount": by_amount}
