"""交易数据预处理器。"""

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from app.analysis.rules import UNCATEGORIZED, categorize
from app.services.bill_analysis.models import ProcessedTransaction
from app.services.bill_analysis.rules.assessment import assess
from app.services.bill_analysis.rules.keywords import (
    COUNTERPARTY_TAG_KEYWORDS,
    INTERNAL_TRANSFER_KEYWORDS,
)
from app.services.bill_analysis.rules.normalization import normalize_counterparty
from app.storage.bill_models import Transaction


class TransactionPreprocessor:
    """交易数据预处理器。

    职责：
    1. 去重
    2. 标准化转换
    3. 交易分类增强
    4. 内部交易识别
    5. 对手归一化
    6. 风控标签提取
    """

    def process(
        self,
        transactions: list[Transaction],
        user_accounts: set[str] | None = None,
        accounts_by_transaction: Mapping[UUID, str] | None = None,
    ) -> list[ProcessedTransaction]:
        """预处理交易数据。

        Args:
            transactions: 原始交易列表
            user_accounts: 用户账户集合（用于识别内部交易）
            accounts_by_transaction: 交易 id → 该交易所属账单文档上的脱敏账号
                （``statement_document.account_no_masked``），由调用方下传。
                映射里没有的交易落到空串 —— 这里不编账户名。

        Returns:
            预处理后的交易列表
        """
        user_accounts = user_accounts or set()

        # 1. 去重
        deduped = self._deduplicate(transactions, accounts_by_transaction)

        # 2. 转换为处理格式
        processed = [self._to_processed(tx, accounts_by_transaction) for tx in deduped]

        # 3. 增强分类
        for tx in processed:
            tx.category, tx.category_confidence = self._enhance_category(tx)

        # 4. 标记内部交易
        for tx in processed:
            tx.is_internal = self._is_internal(tx, user_accounts)

        # 5. 归一化对手名称
        for tx in processed:
            tx.counterparty_norm = normalize_counterparty(tx.counterparty)

        # 6. 对手标签
        for tx in processed:
            tx.counterparty_tag = self._tag_counterparty(tx)

        # 7. 风控判定：关键词标签与规则引擎在同一次判定里出结果（见 rules/assessment.py）。
        #    规则里有需要「同日同对手笔数」的，所以先算一份上下文。
        risk_context = self._risk_context(processed)
        for tx in processed:
            assessment = assess(tx, risk_context.get(tx.tx_id))
            tx.risk_tags = assessment.tags
            tx.matched_rules = assessment.rules
            tx.risk_level = assessment.level

        # 8. 特殊交易标记
        for tx in processed:
            tx.is_credit_repay = self._is_credit_repay(tx)
            tx.is_refund = self._is_refund(tx)
            tx.is_wealth = self._is_wealth(tx)

        return processed

    def _deduplicate(
        self,
        transactions: list[Transaction],
        accounts_by_transaction: Mapping[UUID, str] | None = None,
    ) -> list[Transaction]:
        """去重：按交易自己的身份 ``raw_record_hash``，不再按业务字段猜重复。

        ``raw_record_hash`` 就是入库层判重用的那个指纹（``booked_at`` + 金额 + 对手方，
        见 ``bill_import._record_hash``）。两行哈希相同，说明它们本来就是同一笔交易 ——
        入库层遇到这种行会直接跳过；分析层这里只是兜住历史数据与并发插入留下的重复。
        哈希不同则是两笔真实交易，必须都算。

        以前的键是 ``(账户, booked_date, 金额, 方向, 对手, 类型)``，缺了交易时刻：
        同一天、同一商户、同一金额的两笔（两趟同价公交、同一家店买两次同样的东西）
        会被合并成一笔，而「查询流水 / 账单统计」数的是原始行，于是两处单数对不上。
        账户仍留在键里：``analyzer.py`` 的账户列表是从幸存行推出来的，不同账户的
        同名交易不能互相吞掉（账户名与 ``_to_processed`` 用同一个表达式取）。
        """
        seen = set()
        unique = []
        for tx in transactions:
            key = (
                (accounts_by_transaction or {}).get(tx.id, ""),
                tx.raw_record_hash,
            )
            if key not in seen:
                seen.add(key)
                unique.append(tx)
        return unique

    def _to_processed(
        self, tx: Transaction, accounts_by_transaction: Mapping[UUID, str] | None
    ) -> ProcessedTransaction:
        """转换为 ProcessedTransaction。"""

        # 时刻优先用 `booked_at` —— 账单原文带的时分秒，现在会一路存到库里。
        #
        # 以前这里写的是 `hour = 12`，注释还写着「因为 Transaction 没有具体时间」：
        # 解析那一步把时间切掉了，于是所有交易都被当成中午 12 点，时段分析全成假数据。
        # 老数据没有 `booked_at`，退回当天 00:00 —— 那些行确实不知道几点，
        # 编一个 12 点比留 0 点更糟（0 点至少明显不是真值）。
        tx_time = tx.booked_at or datetime.combine(tx.booked_date, datetime.min.time())
        hour = tx_time.hour
        month = tx.booked_date.strftime("%Y-%m")

        # 账户身份来自这笔交易自己的账单文档（``statement_document.account_no_masked``）。
        # 以前这里写死一个占位账户名和一个占位末四位：Transaction 表确实没有账户号
        # 字段，但那不是编一个的理由 —— 下游会把这些假值当真数据。``card_last4``
        # 全仓没有读取点，同样留空，而不是留一个假的末四位。
        account_no = (accounts_by_transaction or {}).get(tx.id, "")

        return ProcessedTransaction(
            tx_id=str(tx.id),
            tx_time=tx_time,
            account_no=account_no,
            card_last4="",
            amount=tx.amount,
            direction=tx.direction,
            balance_after=tx.balance_after,
            summary=tx.transaction_type,
            counterparty=tx.counterparty,
            product=tx.product or "",
            counterparty_account="",
            currency=tx.currency,
            raw_type=tx.transaction_type,
            hour=hour,
            month=month,
            category=tx.category,  # 保留原有分类
        )

    def _enhance_category(self, tx: ProcessedTransaction) -> tuple[str, float]:
        """增强交易分类。

        Returns:
            (分类, 置信度)
        """
        text = tx.summary + tx.counterparty

        # 优先级1：和入库/查询共用同一套词表（app.analysis.rules，48 类，含 33 个银行
        # 交易类别）。深度分析此前自带一份 10 类的词表，于是同一份数据在「查账单」和
        # 「深度分析」两处显示的类别体系对不上。
        category = categorize(tx.raw_type, tx.counterparty, tx.product)
        if category != UNCATEGORIZED:
            return category, 0.9

        # 优先级2：沿用入库时已经落好的分类（置信度0.6）
        if tx.category and tx.category != UNCATEGORIZED:
            return tx.category, 0.6

        # 优先级3：根据方向判断（置信度0.4）
        if tx.is_income:
            # 收入默认分类
            if "工资" in text or "薪" in text:
                return "工资", 0.7
            return "其他收入", 0.4
        else:
            # 支出默认分类
            return "其他", 0.3

    def _is_internal(self, tx: ProcessedTransaction, user_accounts: set[str]) -> bool:
        """识别内部交易。"""
        text = tx.summary + tx.counterparty

        # 条件1：对手账号是用户自己的
        if tx.counterparty_account in user_accounts:
            return True

        # 条件2：摘要包含内部转账关键词
        if any(kw in text for kw in INTERNAL_TRANSFER_KEYWORDS):
            return True

        # 条件3：信用卡还款
        if "信用卡" in tx.counterparty and "还款" in tx.summary:
            return True

        # 条件4：理财申赎（视为内部资金移动）
        if any(kw in text for kw in ["申购", "赎回", "余额宝", "理财"]):
            return True

        return False

    def _tag_counterparty(self, tx: ProcessedTransaction) -> str:
        """对手标签分类。"""
        name = tx.counterparty + tx.counterparty_norm

        # 按优先级匹配
        for tag, keywords in COUNTERPARTY_TAG_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                return tag

        # 默认：个人
        return "个人"

    def _risk_context(self, transactions: list[ProcessedTransaction]) -> dict[str, dict]:
        """按交易算出风控规则需要的上下文。

        R003「频繁小额」要「同一天、同一对手的交易笔数」—— 这个数只有看过整批交易才知道，
        单看一笔算不出来。原实现把 `context=None` 一路传下去，那条规则于是**从来没命中过**。
        """

        counts = Counter((tx.tx_time.date(), tx.counterparty_norm) for tx in transactions)
        return {
            tx.tx_id: {
                "same_day_same_counterparty_count": counts[
                    (tx.tx_time.date(), tx.counterparty_norm)
                ]
            }
            for tx in transactions
        }

    def _is_credit_repay(self, tx: ProcessedTransaction) -> bool:
        """是否信用卡还款。"""
        text = tx.summary + tx.counterparty
        keywords = ["信用卡还款", "自动还款", "代扣还款", "信用卡-还款"]
        return any(kw in text for kw in keywords)

    def _is_refund(self, tx: ProcessedTransaction) -> bool:
        """是否退款。"""
        keywords = ["退款", "退货", "撤销", "退单"]
        return any(kw in tx.summary for kw in keywords)

    def _is_wealth(self, tx: ProcessedTransaction) -> bool:
        """是否理财申赎。"""
        keywords = ["申购", "赎回", "基金", "理财", "余额宝转入", "余额宝转出"]
        text = tx.summary + tx.counterparty
        return any(kw in text for kw in keywords)
