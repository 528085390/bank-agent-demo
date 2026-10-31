"""产品推荐与对比：从银行产品事实里筛出候选、排序，并给出可核对的原因。

这一层只做"筛选、排序、截断和对比维度"：输入是银行返回的 `Product`，输出是普通数据，不调用
银行、不调用模型，也不重算任何金额（金额只做比较，展示走 `rules.py`）。

两条排序与展示立场（需求 §7.2）：

- 排序按**风险较低、期限较短、产品编号稳定**，不按模拟年化参数。演示年化参数不是收益预测，
  用它排序会把停售的 9% 产品排到最前面，这正是需求要避免的。
- 最多三个候选；没有候选就返回空候选，由调用方如实说明，**不放宽风险凑数**。落选的每一只都带
  原因（不适配，或符合条件但排在三个候选之后），所以"为什么没有推荐它"也来自事实而不是模型。

依赖方向：`domain → contracts`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.modules.wealth.contracts.bank import Product
from app.modules.wealth.domain.rules import MAX_CANDIDATES, RISK_ORDER, Fit, evaluate

__all__ = ["BEYOND_LIMIT_REASON", "Recommendation", "Scored", "compare", "recommend", "rows"]

# 符合条件但超过三个候选时给用户的解释。
BEYOND_LIMIT_REASON = (
    "已经符合您的条件，但候选最多三个；按风险、期限和产品编号排序后未列入本次候选。"
)


@dataclass(frozen=True, slots=True)
class Scored:
    """一个银行产品加它的适配结论。产品字段原样来自银行，结论来自 `rules.evaluate`。"""

    product: Product
    fit: Fit


@dataclass(frozen=True, slots=True)
class Recommendation:
    """一次推荐的结果：候选（最多三个）与落选的产品（每只都带原因）。"""

    candidates: tuple[Scored, ...]
    not_recommended: tuple[Scored, ...]


def _evaluate_all(
    products: Sequence[Product],
    *,
    risk_level: str | None,
    amount_minor: int | None,
    horizon_months: int | None,
    liquidity: str | None,
) -> tuple[Scored, ...]:
    return tuple(
        Scored(
            product=product,
            fit=evaluate(
                product,
                risk_level=risk_level,
                amount_minor=amount_minor,
                horizon_months=horizon_months,
                liquidity=liquidity,
            ),
        )
        for product in products
    )


def rank_key(scored: Scored) -> tuple[int, int, str]:
    """排序键：风险较低、期限较短、产品编号稳定。不认识的等级排在最后。"""

    return (
        RISK_ORDER.get(scored.product.risk_level, len(RISK_ORDER)),
        scored.product.duration_months,
        scored.product.product_id,
    )


def recommend(
    products: Sequence[Product],
    *,
    risk_level: str | None,
    amount_minor: int | None,
    horizon_months: int | None,
    liquidity: str | None,
) -> Recommendation:
    """筛出适配的产品，排序后最多保留三个；其余落在 `not_recommended` 并说明原因。"""

    scored = sorted(
        _evaluate_all(
            products,
            risk_level=risk_level,
            amount_minor=amount_minor,
            horizon_months=horizon_months,
            liquidity=liquidity,
        ),
        key=rank_key,
    )
    suitable = [item for item in scored if item.fit.suitable]
    beyond = tuple(
        Scored(
            product=item.product,
            fit=Fit(suitable=True, reasons=item.fit.reasons + (BEYOND_LIMIT_REASON,)),
        )
        for item in suitable[MAX_CANDIDATES:]
    )
    return Recommendation(
        candidates=tuple(suitable[:MAX_CANDIDATES]),
        not_recommended=tuple(item for item in scored if not item.fit.suitable) + beyond,
    )


def compare(
    products: Sequence[Product],
    *,
    risk_level: str | None,
    amount_minor: int | None,
    horizon_months: int | None,
    liquidity: str | None,
) -> tuple[Scored, ...]:
    """按调用方给的顺序给出对比行。

    不适配的产品也在其中（需求 §7.2 允许客观对比），但带 `suitable=False` 与原因：对比本身
    不构成"适合您"的结论，也不解锁任何资金操作。
    """

    return _evaluate_all(
        products,
        risk_level=risk_level,
        amount_minor=amount_minor,
        horizon_months=horizon_months,
        liquidity=liquidity,
    )


def rows(scored: Sequence[Scored]) -> list[dict[str, Any]]:
    """把结论摊平成 facts 里的行：银行字段原样搬运，适配结论与原因来自 `rules`。"""

    return [
        {
            "product_id": item.product.product_id,
            "name": item.product.name,
            "risk_level": item.product.risk_level,
            "duration_months": item.product.duration_months,
            "min_amount_minor": item.product.min_amount_minor,
            "max_amount_minor": item.product.max_amount_minor,
            "redeem_rule": item.product.redeem_rule,
            "expected_return": item.product.expected_return,
            "status": item.product.status,
            "suitable": item.fit.suitable,
            "reasons": list(item.fit.reasons),
        }
        for item in scored
    ]
