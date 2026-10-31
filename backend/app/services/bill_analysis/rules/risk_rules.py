"""风控规则定义。"""

from dataclasses import dataclass
from typing import Callable


@dataclass
class RiskRule:
    """风控规则。"""

    rule_id: str
    name: str
    description: str
    risk_level: str  # 低/中/高
    check_func: Callable  # 检查函数：(tx, context) -> bool


def _text(tx) -> str:
    """判定用的文本。

    **商品列也拼进来** —— 对手方往往只是个笼统的商户名（「某科技有限公司」），
    「境外」「网贷」这类特征更常写在商品/摘要那一栏（「境外游戏充值」）。
    这几条规则原先只看 `summary + counterparty`，等于把最有信息的那一栏漏掉了。
    """

    return " ".join(
        str(getattr(tx, attr, "") or "") for attr in ("summary", "counterparty", "product")
    )


def rule_r001_large_amount(tx, context=None) -> bool:
    """R001：大额交易（单笔 > 50000元）。"""
    return tx.amount > 5000000  # 50000元 = 5000000分


def rule_r002_night_large(tx, context=None) -> bool:
    """R002：夜间大额（夜间22:00-06:00且金额 > 10000元）。"""
    return (22 <= tx.hour or tx.hour < 6) and tx.amount > 1000000


def rule_r003_frequent_small(tx, context) -> bool:
    """R003：频繁小额（1天内同一对手交易 > 5笔）。

    需要上下文：同一天同一对手的交易次数。
    """
    if context is None:
        return False
    same_day_count = context.get("same_day_same_counterparty_count", 0)
    return same_day_count > 5


def rule_r004_overseas(tx, context=None) -> bool:
    """R004：境外交易。"""
    keywords = ["境外", "overseas", "foreign", "international", "跨境"]
    text = _text(tx).lower()
    return any(kw in text for kw in keywords)


def rule_r005_suspected_loan(tx, context=None) -> bool:
    """R005：疑似网贷。"""
    from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS

    keywords = RISK_KEYWORDS["网贷"]
    text = _text(tx)
    return any(kw in text for kw in keywords)


def rule_r006_suspected_rental(tx, context=None) -> bool:
    """R006：疑似租机。"""
    from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS

    keywords = RISK_KEYWORDS["租机"]
    text = _text(tx)
    return any(kw in text for kw in keywords)


def rule_r007_round_number(tx, context=None) -> bool:
    """R007：整数金额（整千元，可能是人为操作）。

    **阈值和取模都用「分」**（`tx.amount` 的单位）：`10000000` 分 = 10 万元，
    `500000` 分 = 5000 元，`100000` 分 = 1000 元。以前注释写成「>= 5000」容易被读成
    5000 分（50 元），差了两个数量级。
    """
    if tx.amount >= 10_000_000:  # ≥ 10 万元
        return tx.amount % 10_000_000 == 0
    elif tx.amount >= 500_000:  # ≥ 5000 元
        return tx.amount % 100_000 == 0
    return False


def rule_r008_gambling(tx, context=None) -> bool:
    """R008：疑似博彩。"""
    from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS

    keywords = RISK_KEYWORDS.get("博彩", [])
    text = _text(tx)
    return any(kw in text for kw in keywords)


def rule_r009_bnpl(tx, context=None) -> bool:
    """R009：先用后付（Buy Now Pay Later）。"""
    from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS

    keywords = RISK_KEYWORDS["先用后付"]
    text = _text(tx)
    return any(kw in text for kw in keywords)


def rule_r010_overdue(tx, context=None) -> bool:
    """R010：逾期特征。"""
    from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS

    keywords = RISK_KEYWORDS["逾期特征"]
    text = _text(tx)
    return any(kw in text for kw in keywords)


# 风控规则注册表
RISK_RULES = [
    RiskRule("R001", "大额交易", "单笔交易金额超过50000元", "中", rule_r001_large_amount),
    RiskRule(
        "R002",
        "夜间大额",
        "夜间（22:00-06:00）且金额超过10000元",
        "高",
        rule_r002_night_large,
    ),
    RiskRule(
        "R003",
        "频繁小额",
        "1天内同一对手交易超过5笔",
        "中",
        rule_r003_frequent_small,
    ),
    RiskRule("R004", "境外交易", "交易摘要包含境外相关关键词", "中", rule_r004_overseas),
    RiskRule(
        "R005", "疑似网贷", "对手或摘要命中网贷关键词库", "高", rule_r005_suspected_loan
    ),
    RiskRule(
        "R006", "疑似租机", "对手或摘要命中租机关键词库", "中", rule_r006_suspected_rental
    ),
    RiskRule(
        "R007",
        "整数金额",
        "交易金额为整千或整万，可能是人为操作",
        "低",
        rule_r007_round_number,
    ),
    RiskRule(
        "R008", "疑似博彩", "对手或摘要命中博彩关键词库", "高", rule_r008_gambling
    ),
    RiskRule(
        "R009",
        "先用后付",
        "使用花呗、白条等先用后付工具",
        "中",
        rule_r009_bnpl,
    ),
    RiskRule(
        "R010",
        "逾期特征",
        "交易摘要包含罚息、催收等逾期关键词",
        "高",
        rule_r010_overdue,
    ),
]


def check_all_rules(tx, context=None) -> list[dict]:
    """检查所有风控规则。

    Args:
        tx: ProcessedTransaction 对象
        context: 上下文信息字典

    Returns:
        命中的规则列表，每项包含 rule_id, name, risk_level
    """
    matched = []
    for rule in RISK_RULES:
        try:
            if rule.check_func(tx, context):
                matched.append(
                    {
                        "rule_id": rule.rule_id,
                        "name": rule.name,
                        "risk_level": rule.risk_level,
                        "description": rule.description,
                    }
                )
        except Exception:
            # 某个规则检查失败，不影响其他规则
            continue
    return matched
