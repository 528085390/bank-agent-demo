"""主分析器：串联所有分析模块。"""

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.services.bill_analysis.models import AnalysisReport, ModuleResult, ProcessedTransaction
from app.services.bill_analysis.modules import (
    AccountUsageModule,
    AnnualSummaryModule,
    CashflowModule,
    CounterpartyModule,
    HourlyActivityModule,
    RiskProfileModule,
    RiskTransactionsModule,
    TypeAnalysisModule,
)
from app.services.bill_analysis.preprocessor import TransactionPreprocessor
from app.storage.bill_models import BillTask, StatementDocument, Transaction
from app.storage.database import Database


def _is_calendar_year(period_start: date, period_end: date) -> bool:
    """区间是不是某个完整自然年（1 月 1 日 – 12 月 31 日）。

    年报的口径由**区间**决定，不由模型声明：说「2025年」解析出来就是整年，说
    「2025年1月到11月」就不是年报，哪怕它同样叫"年度报告"。
    """

    return (
        period_start.month == 1
        and period_start.day == 1
        and period_end.month == 12
        and period_end.day == 31
        and period_start.year == period_end.year
    )


def cashflow_totals(transactions: list[ProcessedTransaction]) -> dict[str, dict[str, int]]:
    """The two totals the report shows, in minor units.

    ``all`` is what the bank statement's own credit/debit totals say: every row counts.
    ``external`` removes internal fund movements (wealth purchase/redemption, credit-card
    repayment, transfers between the user's own accounts) so the cash-flow trend reflects
    money entering/leaving the user's own book, not money moving between their pockets.
    """

    def totals(items: list[ProcessedTransaction]) -> dict[str, int]:
        income = sum(tx.amount for tx in items if tx.is_income)
        expense = sum(tx.amount for tx in items if tx.is_expense)
        return {
            "income_minor": income,
            "expense_minor": expense,
            "net_minor": income - expense,
        }

    return {
        "all": totals(transactions),
        "external": totals([tx for tx in transactions if not tx.is_internal]),
    }


class BillAnalyzer:
    """账单深度分析器。"""

    def __init__(self, database: Database):
        self.database = database
        self.preprocessor = TransactionPreprocessor()

    def analyze(
        self,
        user_id: str,
        period_start: date,
        period_end: date,
        modules: list[str] | None = None,
        *,
        document_id=None,
    ) -> dict:
        """执行完整分析。

        Args:
            user_id: 用户ID
            period_start: 开始日期
            period_end: 结束日期
            modules: 要执行的模块列表，None表示全部
            document_id: 给了就只分析**这一份账单文档**里的交易；不给则按日期区间跨文档
                查询（既有行为）。

        Returns:
            分析报告字典
        """
        # 1. 查询原始交易数据。账号挂在账单文档上（statement_document.account_no_masked）——
        #    以前只取 Transaction，账户维度于是永远拿不到真值。
        #
        #    `document_id` 是「分析刚导入的这份流水」的实现：只按文档取数，不受同区间
        #    其它账单影响。日期区间仍是另一条入口（用户直接说「分析最近三个月」）。
        with self.database.sessions() as db:
            scope = (
                [Transaction.statement_document_id == document_id]
                if document_id is not None
                else [
                    Transaction.booked_date >= period_start,
                    Transaction.booked_date <= period_end,
                ]
            )
            rows = db.execute(
                select(Transaction, StatementDocument.account_no_masked)
                .join(BillTask, BillTask.id == Transaction.task_id)
                .join(
                    StatementDocument,
                    StatementDocument.id == Transaction.statement_document_id,
                )
                .where(BillTask.user_id == user_id, *scope)
                .order_by(Transaction.booked_date)
            ).all()

        if not rows:
            # 返回空报告
            return self._empty_report(user_id, period_start, period_end)

        raw_transactions = [tx for tx, _ in rows]

        # 同一时段可能跨多份账单，账号挂在每笔交易自己的文档上：按交易 id 归属，
        # 而不是给整个时段认一个号 —— 认一个号会把 A 卡的账单盖到 B 卡的交易上，
        # 也会把「读不到账号」的那份账单一起算进 A 卡。读不到的键干脆不进映射，
        # 预处理时落到空串（下游按「未知」诚实降级）。
        accounts_by_transaction = {tx.id: account_no for tx, account_no in rows if account_no}

        # 2. 预处理
        processed = self.preprocessor.process(
            raw_transactions, accounts_by_transaction=accounts_by_transaction
        )

        # 3. 提取账户列表（没有账户信息时是空列表，不是 [""]）
        accounts = sorted({tx.account_no for tx in processed if tx.account_no})

        # 4. 执行分析模块
        module_results = []

        # 年报：区间正好是某个完整自然年时才算。年度总览放在最前面 —— 报告要先说
        # 「这一年怎么样」，再进入各维度。
        annual = _is_calendar_year(period_start, period_end)
        if annual and (modules is None or "annual_summary" in modules):
            module_results.append(AnnualSummaryModule.analyze(processed))

        # 模块1：风险标签识别
        if modules is None or "risk_profile" in modules:
            result = RiskProfileModule.analyze(processed)
            module_results.append(result)

        # 模块2：时段分析
        if modules is None or "hourly_activity" in modules:
            result = HourlyActivityModule.analyze(processed)
            module_results.append(result)

        # 模块3：账户统计
        if modules is None or "account_usage" in modules:
            result = AccountUsageModule.analyze(processed)
            module_results.append(result)

        # 模块4：类型占比
        if modules is None or "type_analysis" in modules:
            result = TypeAnalysisModule.analyze(processed)
            module_results.append(result)

        # 模块5：月度趋势
        if modules is None or "cashflow" in modules:
            result = CashflowModule.analyze(processed)
            module_results.append(result)

        # 模块6：风控交易
        if modules is None or "risk_transactions" in modules:
            result = RiskTransactionsModule.analyze(processed)
            module_results.append(result)

        # 模块7：对手分析
        if modules is None or "counterparty" in modules:
            result = CounterpartyModule.analyze(processed)
            module_results.append(result)

        # 5. 生成文字报告
        text_report = self._generate_text_report(module_results, processed)

        # 6. 构建完整报告
        report = AnalysisReport(
            report_meta={
                "date_range": {
                    "start": period_start.isoformat(),
                    "end": period_end.isoformat(),
                },
                "accounts": accounts,
                "total_transactions": len(processed),
                # 「年报」不是另一套计算，而是同一套分析在整年区间上的**产品口径**：
                # 前端与 PDF 据此换标题、加年度总览，不再把年报渲染成一份普通报告。
                "report_type": "annual" if annual else "standard",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                # 两种口径都放在报告里，前端/PDF 不再各自猜：
                # all = 银行流水合计（含理财申赎、信用卡还款、内部转账）；
                # external = 对外收支（剔除内部资金往来）。
                "cashflow": cashflow_totals(processed),
            },
            modules=module_results,
            text_report=text_report,
            audit={
                "operator": user_id,
                "query_conditions": {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        return report.to_dict()

    def _empty_report(self, user_id: str, period_start: date, period_end: date) -> dict:
        """生成空报告。"""
        return {
            "report_meta": {
                "date_range": {
                    "start": period_start.isoformat(),
                    "end": period_end.isoformat(),
                },
                "accounts": [],
                "total_transactions": 0,
                "report_type": "annual" if _is_calendar_year(period_start, period_end) else "standard",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "cashflow": {
                    "all": {"income_minor": 0, "expense_minor": 0, "net_minor": 0},
                    "external": {"income_minor": 0, "expense_minor": 0, "net_minor": 0},
                },
            },
            "modules": [],
            "text_report": "该时间段没有交易记录。",
            "audit": {
                "operator": user_id,
                "query_conditions": {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _generate_text_report(
        self, modules: list[ModuleResult], transactions: list
    ) -> str:
        """生成文字报告。"""
        lines = [
            "# 账单深度分析报告\n",
            f"**分析时段**：{transactions[0].tx_time.date()} 至 {transactions[-1].tx_time.date()}"
            if transactions
            else "",
            f"**交易笔数**：{len(transactions)}笔\n",
        ]

        for module in modules:
            lines.append(f"## {module.title}\n")

            # 添加洞察
            if module.insights:
                lines.append("**风控解读批注**：\n")
                for insight in module.insights:
                    icon = {
                        "事实": "📊",
                        "推断": "🔍",
                        "建议": "💡",
                    }.get(insight["type"], "•")
                    lines.append(f"{icon} {insight['text']}\n")
            else:
                lines.append("暂无特殊洞察。\n")

            lines.append("")  # 空行

        return "\n".join(lines)
