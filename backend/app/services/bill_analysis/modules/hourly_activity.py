"""模块2：交易活跃时段分析。"""

from collections import defaultdict
from datetime import timedelta

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class HourlyActivityModule:
    """时段分析模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        # 按小时统计
        hourly = defaultdict(lambda: {"count": 0, "income": 0, "expense": 0})
        for tx in transactions:
            hour = tx.hour
            hourly[hour]["count"] += 1
            if tx.is_income:
                hourly[hour]["income"] += tx.amount
            else:
                hourly[hour]["expense"] += tx.amount

        # 夜间统计（22:00-06:00）
        night_txs = [tx for tx in transactions if 22 <= tx.hour or tx.hour < 6]
        night_count = len(night_txs)
        night_amount = sum(tx.amount for tx in night_txs)

        # 连续夜间天数
        night_dates = set(tx.tx_time.date() for tx in night_txs)
        consecutive_days = HourlyActivityModule._count_consecutive_days(
            sorted(night_dates)
        )

        # 夜间主要对手
        night_counterparties = defaultdict(int)
        for tx in night_txs:
            night_counterparties[tx.counterparty_norm] += 1
        top_night_counterparties = sorted(
            night_counterparties.items(), key=lambda x: x[1], reverse=True
        )[:3]

        result = ModuleResult(
            module_id="hourly_activity",
            title="交易活跃时段分析",
            data={
                "hourly_distribution": [
                    {"hour": h, **hourly[h]} for h in range(24)
                ],
                "night_stats": {
                    "count": night_count,
                    "percentage": (
                        round(night_count / len(transactions) * 100, 1)
                        if transactions
                        else 0
                    ),
                    "amount_minor": night_amount,
                    "amount_percentage": (
                        round(
                            night_amount / sum(tx.amount for tx in transactions) * 100,
                            1,
                        )
                        if transactions
                        else 0
                    ),
                    "consecutive_days": consecutive_days,
                    "top_counterparties": [
                        {"name": name, "count": count}
                        for name, count in top_night_counterparties
                    ],
                },
            },
        )

        # 生成洞察
        if night_count > 0:
            pct = result.data["night_stats"]["percentage"]
            result.add_insight(
                f"夜间交易占比{pct}%，"
                f"连续夜间交易{consecutive_days}天，"
                f"主要对手为{', '.join(name for name, _ in top_night_counterparties[:2])}",
                "事实",
                1.0,
            )

            if pct > 20:
                result.add_insight(
                    f"夜间交易占比{pct}%，明显偏高，建议关注",
                    "推断",
                    0.8,
                )

        # 识别交易集中时段
        peak_hours = sorted(hourly.items(), key=lambda x: x[1]["count"], reverse=True)[
            :3
        ]
        if peak_hours:
            peak_hour_str = "、".join(f"{h}点" for h, _ in peak_hours)
            result.add_insight(
                f"交易主要集中在{peak_hour_str}",
                "事实",
                1.0,
            )

        return result

    @staticmethod
    def _count_consecutive_days(dates: list) -> int:
        """统计最长连续天数。"""
        if not dates:
            return 0

        max_consecutive = 1
        current_consecutive = 1

        for i in range(1, len(dates)):
            if dates[i] - dates[i - 1] == timedelta(days=1):
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 1

        return max_consecutive
