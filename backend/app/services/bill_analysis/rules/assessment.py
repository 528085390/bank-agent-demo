"""交易的风控判定：关键词标签与规则引擎在**同一次判定**里出结果。

系统原来有两套互不相通的判定 —— `RISK_KEYWORDS`（7 个标签）在预处理阶段打标，供
`risk_profile` 模块聚合出「风险标签」；`RISK_RULES`（10 条规则）只在 `risk_transactions`
模块里跑一遍，生成「风控交易清单」。同一笔交易可能被一边标「网贷」、被另一边完全忽略，
报告里那两块于是各说各话。

这里把两者收进一个入口 `assess()`：同时跑标签与规则，输出一份结果（标签 + 命中规则 +
等级），两个模块都只读它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.bill_analysis.rules.keywords import RISK_KEYWORDS
from app.services.bill_analysis.rules.risk_rules import RISK_RULES

#: 等级由高到低。命中的最高一级就是这笔交易的等级。
_LEVEL_ORDER = ("高", "中", "低")
NO_RISK = "无"

#: 标签 → 等级。与 `RISK_RULES` 里同名规则的定级保持一致，免得同一件事两边说法不同。
_TAG_LEVEL = {
    "网贷": "高",
    "博彩": "高",
    "逾期特征": "高",
    "租机": "中",
    "先用后付": "中",
    "民间借贷": "中",
    "回收中介": "低",
}

#: 「逾期特征」描述的是账户状态（罚息、催收），不是这一笔交易的性质，所以它不参与
#: 逐笔打标 —— 与原来 `_extract_risk_tags` 的行为一致。
_STATUS_ONLY_TAGS = frozenset({"逾期特征"})


@dataclass(frozen=True)
class RiskAssessment:
    """一笔交易的风险判定结果。"""

    tags: list[str] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    level: str = NO_RISK

    @property
    def rule_ids(self) -> list[str]:
        return [rule["rule_id"] for rule in self.rules]

    @property
    def reason_text(self) -> str:
        """给报告用的一句话：命中了什么。"""

        parts = [rule["name"] for rule in self.rules]
        if self.tags:
            parts.append("、".join(self.tags))
        return "；".join(parts)


def _text_of(tx) -> str:
    """判定用的文本。商品列也拼进来 —— 对手方往往只是个笼统的商户名。"""

    return " ".join(
        str(getattr(tx, attr, "") or "") for attr in ("summary", "counterparty", "product")
    )


def matching_tags(tx) -> list[str]:
    """命中的风控标签。"""

    text = _text_of(tx)
    return [
        tag
        for tag, keywords in RISK_KEYWORDS.items()
        if tag not in _STATUS_ONLY_TAGS and any(kw in text for kw in keywords)
    ]


def matching_rules(tx, context: dict | None = None) -> list[dict]:
    """命中的规则。

    单条规则抛错只跳过它自己 —— 一笔交易的判定不该因为某条规则崩掉而整份丢失
    （原实现也是这个取舍，这里保留）。
    """

    hits: list[dict] = []
    for rule in RISK_RULES:
        try:
            if rule.check_func(tx, context or {}):
                hits.append(
                    {
                        "rule_id": rule.rule_id,
                        "name": rule.name,
                        "risk_level": rule.risk_level,
                        "description": rule.description,
                    }
                )
        except Exception:  # noqa: BLE001 - 见 docstring
            continue
    return hits


def assess(tx, context: dict | None = None) -> RiskAssessment:
    """一次算清这笔交易的风险：标签、命中规则、等级。"""

    tags = matching_tags(tx)
    rules = matching_rules(tx, context)

    levels = [rule["risk_level"] for rule in rules]
    levels += [_TAG_LEVEL[tag] for tag in tags if tag in _TAG_LEVEL]
    level = next((candidate for candidate in _LEVEL_ORDER if candidate in levels), NO_RISK)

    return RiskAssessment(tags=tags, rules=rules, level=level)
