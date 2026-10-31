"""模块3：银行卡/账户使用统计。"""

from collections import defaultdict

from app.services.bill_analysis.models import ModuleResult, ProcessedTransaction


class AccountUsageModule:
    """账户统计模块。"""

    @staticmethod
    def analyze(transactions: list[ProcessedTransaction]) -> ModuleResult:
        """执行分析。"""
        # 按账户统计
        accounts = defaultdict(
            lambda: {"count": 0, "income": 0, "expense": 0, "dates": set()}
        )

        for tx in transactions:
            acc = tx.account_no
            accounts[acc]["count"] += 1
            accounts[acc]["dates"].add(tx.tx_time.date())
            if tx.is_income:
                accounts[acc]["income"] += tx.amount
            else:
                accounts[acc]["expense"] += tx.amount

        # 构建账户列表
        account_list = []
        for acc, stats in accounts.items():
            net = stats["income"] - stats["expense"]
            account_list.append(
                {
                    "account_no": acc,
                    "count": stats["count"],
                    "income_minor": stats["income"],
                    "expense_minor": stats["expense"],
                    "net_minor": net,
                    "net_flow": "流入" if net > 0 else "流出" if net < 0 else "平衡",
                    "active_days": len(stats["dates"]),
                }
            )

        # 按交易笔数排序
        account_list.sort(key=lambda x: x["count"], reverse=True)

        result = ModuleResult(
            module_id="account_usage",
            title="银行卡/账户使用统计",
            data={"accounts": account_list},
        )

        # 生成洞察
        known = [account for account in account_list if account["account_no"]]
        if known:
            main_account = known[0]
            result.add_insight(
                f"主要使用卡为{main_account['account_no']}，"
                f"交易{main_account['count']}笔，"
                f"净{main_account['net_flow']}{abs(main_account['net_minor'])/100:.2f}元，"
                f"活跃天数{main_account['active_days']}天",
                "事实",
                1.0,
            )
        else:
            # 账单没提供账户身份（解析器读不到，库里也是空）时，不能把「未知」说成某个
            # 账户的统计。这份汇总确实覆盖了全部交易，但它们的归属是未知的。
            result.add_insight("账单未提供账户信息，按全部交易汇总", "事实", 1.0)

        return result
