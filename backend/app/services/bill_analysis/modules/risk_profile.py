"""模块1：客户基础信息与风险标签。"""

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class RiskProfileModule:
    """风险标签识别模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        # 统计各风控标签
        tag_stats = {}
        for tx in transactions:
            for tag in tx.risk_tags:
                if tag not in tag_stats:
                    tag_stats[tag] = {"count": 0, "amount": 0, "txs": []}
                tag_stats[tag]["count"] += 1
                tag_stats[tag]["amount"] += tx.amount
                tag_stats[tag]["txs"].append(tx.tx_id)

        # 构建风险标签列表
        risk_tags = []
        for tag, stats in tag_stats.items():
            confidence = stats["count"] / len(transactions) if transactions else 0
            risk_tags.append(
                {
                    "tag": tag,
                    "hit_count": stats["count"],
                    "amount_minor": stats["amount"],
                    "confidence": round(confidence, 2),
                    "evidence": stats["txs"][:3],  # 只取前3笔作为证据
                    "detail": f"检测到{tag}交易{stats['count']}笔，金额{stats['amount']/100:.2f}元",
                }
            )

        # 按命中次数排序
        risk_tags.sort(key=lambda x: x["hit_count"], reverse=True)

        # 账单完整性：真去看余额对不对得上。
        #
        # 这里原来是空转的 —— 循环体里是个 `pass`，`balance_gaps` 永远是 0，评分永远满分。
        # 等于什么都没检查，还给读者一个「账单完整」的假结论。没有余额列时如实标成
        # 「未评估」（score=None），而不是给满分。
        completeness_issues: list[str] = []
        with_balance = [
            tx for tx in sorted(transactions, key=lambda t: t.tx_time)
            if tx.balance_after is not None
        ]

        if len(with_balance) < 2:
            completeness_score = None
            completeness_issues.append("账单不含余额列，完整性未评估")
        else:
            gaps = 0
            for prev, cur in zip(with_balance, with_balance[1:]):
                moved = cur.amount if cur.is_income else -cur.amount
                if abs((prev.balance_after + moved) - cur.balance_after) > 1:  # 1 分容差
                    gaps += 1
            ratio = gaps / len(with_balance)
            completeness_score = max(0, round((1 - ratio) * 100))
            if ratio > 0.1:
                completeness_issues.append(
                    f"余额记录不连续（{gaps}/{len(with_balance)} 处对不上）"
                )

        result = ModuleResult(
            module_id="customer_risk_profile",
            title="客户基础信息与风险标签",
            data={
                "accounts": list(set(tx.account_no for tx in transactions)),
                "transaction_count": len(transactions),
                "risk_tags": risk_tags,
                "completeness_score": completeness_score,
                "completeness_issues": completeness_issues,
            },
        )

        # 生成洞察
        if not risk_tags:
            result.add_insight("未检测到明显风险标签", "事实", 1.0)
        else:
            for tag_info in risk_tags:
                result.add_insight(
                    f"疑似存在{tag_info['tag']}类交易特征，涉及{tag_info['hit_count']}笔，"
                    f"金额{tag_info['amount_minor']/100:.2f}元，"
                    f"置信度{tag_info['confidence']*100:.0f}%，建议关注",
                    "推断",
                    tag_info["confidence"],
                )

        return result
