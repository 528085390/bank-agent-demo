"""年度总览（年报专用）。

年报和普通深度分析的区别不在"能算多少"，而在**先说清楚这一年**：收入、支出、
结余、储蓄率、钱花在哪、最大的几笔是什么，以及**数据覆盖了哪几个月**。

最后一项是刻意放在最前面的：用户可能只导入过几个月就要求"出年报"，报告必须如实
说明覆盖范围，而不是把三个月的数当成全年。
"""

from collections import Counter, defaultdict

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction

#: 年度榜单取前几名。
TOP_N = 5
#: 年度最大单笔取几条。
LARGEST_N = 3


class AnnualSummaryModule:
    """年度总览模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""

        # 一年之内不该跨年；真跨了（数据异常）就以出现最多的那一年为准。
        year = Counter(tx.tx_time.year for tx in transactions).most_common(1)[0][0]

        income = sum(tx.amount for tx in transactions if tx.is_income)
        expense = sum(tx.amount for tx in transactions if tx.is_expense)
        net = income - expense

        # 覆盖月份：这一年里哪些月有流水、哪些月没有。年报必须说清楚这一点。
        active_months = sorted(
            month for month in {tx.month for tx in transactions} if month.startswith(f"{year}-")
        )
        expected = [f"{year}-{month:02d}" for month in range(1, 13)]
        missing_months = [month for month in expected if month not in active_months]

        # 支出按分类聚合（消费口径）；收入不参与分类榜。
        categories: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "amount": 0})
        for tx in transactions:
            if tx.is_expense:
                categories[tx.category]["count"] += 1
                categories[tx.category]["amount"] += tx.amount
        top_categories = sorted(
            (
                {"category": name, "count": stats["count"], "amount_minor": stats["amount"]}
                for name, stats in categories.items()
            ),
            key=lambda item: item["amount_minor"],
            reverse=True,
        )[:TOP_N]

        # 对手方按金额聚合，笔数一起给出：一年里反复出现的人/商户本身就是线索。
        counterparts: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "amount": 0})
        for tx in transactions:
            name = tx.counterparty_norm or tx.counterparty or "未知"
            counterparts[name]["count"] += 1
            counterparts[name]["amount"] += tx.amount
        top_counterparties = sorted(
            (
                {"counterparty": name, "count": stats["count"], "amount_minor": stats["amount"]}
                for name, stats in counterparts.items()
            ),
            key=lambda item: item["amount_minor"],
            reverse=True,
        )[:TOP_N]

        largest = sorted(transactions, key=lambda tx: tx.amount, reverse=True)[:LARGEST_N]
        largest_transactions = [
            {
                "time": tx.tx_time.isoformat(),
                "counterparty": tx.counterparty or "未知",
                "direction": "收入" if tx.is_income else "支出",
                "amount_minor": tx.amount,
            }
            for tx in largest
        ]

        risk_hit_count = sum(1 for tx in transactions if tx.risk_tags)

        result = ModuleResult(
            module_id="annual_summary",
            title=f"{year} 年度总览",
            data={
                "year": year,
                "transaction_count": len(transactions),
                "income_minor": income,
                "expense_minor": expense,
                "net_minor": net,
                # 收入为 0 时没有"储蓄率"可谈：留 None，而不是编一个 0% 或 100%。
                "savings_rate_pct": round(net / income * 100, 1) if income else None,
                "active_months": active_months,
                "missing_months": missing_months,
                "top_categories": top_categories,
                "top_counterparties": top_counterparties,
                "largest_transactions": largest_transactions,
                "risk_hit_count": risk_hit_count,
            },
        )

        # 洞察：先覆盖范围，再结论。
        if missing_months:
            result.add_insight(
                f"数据覆盖 {len(active_months)} / 12 个月（缺 "
                f"{'、'.join(m[5:] + '月' for m in missing_months)}），"
                "以下年度数字只代表有数据的月份",
                "事实",
                1.0,
            )
        else:
            result.add_insight("数据覆盖全年 12 个月", "事实", 1.0)

        if income:
            result.add_insight(
                f"{year} 年收入 {income/100:.2f} 元、支出 {expense/100:.2f} 元、"
                f"结余 {net/100:.2f} 元，储蓄率 {result.data['savings_rate_pct']}%",
                "事实",
                1.0,
            )
        else:
            result.add_insight(
                f"{year} 年支出 {expense/100:.2f} 元，没有识别到收入",
                "事实",
                1.0,
            )

        if top_categories:
            top = top_categories[0]
            result.add_insight(
                f"年度最大支出类别为{top['category']}，"
                f"共 {top['count']} 笔、{top['amount_minor']/100:.2f} 元",
                "事实",
                1.0,
            )

        if risk_hit_count:
            result.add_insight(
                f"全年命中风控规则的交易 {risk_hit_count} 笔，建议结合清单复核",
                "推断",
                0.9,
            )

        return result
