"""模块6：特殊风控交易清单。"""

from collections import defaultdict

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class RiskTransactionsModule:
    """风控交易清单模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        risk_transactions = []
        rule_hit_stats = defaultdict(int)

        for tx in transactions:
            # 直接读预处理阶段算好的结果（见 rules/assessment.py）。
            #
            # 这里以前自己再跑一遍 `check_all_rules(tx, context=None)` —— 既和「风险标签」
            # 那块用了两套判据，又因为 `context=None` 让 R003 那条规则永远命中不了。
            matched_rules = tx.matched_rules

            if matched_rules:
                # 提取规则信息
                rule_ids = [r["rule_id"] for r in matched_rules]
                rule_names = [r["name"] for r in matched_rules]
                highest_risk = max(
                    (r["risk_level"] for r in matched_rules),
                    key=lambda x: {"高": 3, "中": 2, "低": 1}.get(x, 0),
                )

                # 统计规则命中次数
                for rule in matched_rules:
                    rule_hit_stats[rule["name"]] += 1

                risk_transactions.append(
                    {
                        "tx_id": tx.tx_id,
                        "time": tx.tx_time.isoformat(),
                        "account": tx.account_no,
                        "summary": tx.summary,
                        "counterparty": tx.counterparty,
                        "direction": "收入" if tx.is_income else "支出",
                        "amount_minor": tx.amount,
                        "balance_minor": tx.balance_after,
                        "rules": rule_ids,
                        "reasons": ";".join(rule_names),
                        "risk_level": highest_risk,
                    }
                )

        # 按时间排序
        risk_transactions.sort(key=lambda x: x["time"], reverse=True)

        # 构建统计摘要
        summary = {
            "total_count": len(risk_transactions),
            "by_rule": dict(rule_hit_stats),
        }

        result = ModuleResult(
            module_id="risk_transactions",
            title="特殊风控交易清单",
            data={"transactions": risk_transactions, "summary": summary},
        )

        # 生成洞察
        if risk_transactions:
            result.add_insight(
                f"共命中{len(risk_transactions)}笔风控交易，"
                f"主要规则：{', '.join(list(rule_hit_stats.keys())[:3])}",
                "事实",
                1.0,
            )

            # 按规则类型生成洞察
            for rule_name, count in sorted(
                rule_hit_stats.items(), key=lambda x: x[1], reverse=True
            )[:3]:
                result.add_insight(f"{rule_name}：{count}笔", "事实", 1.0)
        else:
            result.add_insight("未检测到风控交易", "事实", 1.0)

        return result
