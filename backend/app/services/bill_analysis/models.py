"""账单分析数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class ProcessedTransaction:
    """预处理后的交易数据（标准化 + 派生字段）。"""

    # 标准化字段（来自 Transaction 表）
    tx_id: str
    tx_time: datetime
    account_no: str
    card_last4: str
    amount: int  # 金额（分）
    direction: str  # credit/debit
    balance_after: int | None
    summary: str
    counterparty: str
    counterparty_account: str
    currency: str
    raw_type: str
    #: 商品/摘要列，参与分类匹配（对手方往往只是笼统的商户名）。
    #: 放在无默认值的字段之后 —— 它带默认值，插在前面会让 dataclass 直接报 TypeError。
    product: str = ""

    # 基础派生字段
    category: str = "未分类"
    category_confidence: float = 0.3
    hour: int = 0  # 0-23
    month: str = ""  # YYYY-MM

    # 标记字段
    is_internal: bool = False
    is_credit_repay: bool = False
    is_refund: bool = False
    is_wealth: bool = False

    # 归一化字段
    counterparty_norm: str = ""
    counterparty_tag: str = "未知"

    # 风控字段
    risk_tags: list[str] = field(default_factory=list)
    risk_level: str = "低"
    #: 命中的风控规则，每项是 dict（rule_id / name / risk_level / description），
    #: 由 `rules/assessment.py` 的 `assess()` 填充。以前是个从没被赋过值的死字段。
    matched_rules: list[dict] = field(default_factory=list)

    @property
    def amount_yuan(self) -> Decimal:
        """金额（元）。"""
        return Decimal(self.amount) / 100

    @property
    def is_income(self) -> bool:
        """是否收入。"""
        return self.direction == "credit"

    @property
    def is_expense(self) -> bool:
        """是否支出。"""
        return self.direction == "debit"


@dataclass
class ModuleResult:
    """单个分析模块的结果。"""

    module_id: str
    title: str
    data: dict
    insights: list[dict] = field(default_factory=list)

    def add_insight(self, text: str, insight_type: str = "事实", confidence: float = 1.0):
        """添加洞察。"""
        self.insights.append({
            "text": text,
            "type": insight_type,
            "confidence": confidence
        })


@dataclass
class AnalysisReport:
    """完整的分析报告。"""

    report_meta: dict
    modules: list[ModuleResult]
    text_report: str
    audit: dict

    def to_dict(self) -> dict:
        """转换为字典。"""
        return {
            "report_meta": self.report_meta,
            "modules": [
                {
                    "module_id": m.module_id,
                    "title": m.title,
                    "data": m.data,
                    "insights": m.insights
                }
                for m in self.modules
            ],
            "text_report": self.text_report,
            "audit": self.audit
        }
