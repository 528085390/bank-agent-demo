"""理财 Agent 的领域层：推荐、对比、风险问卷和交易预检规则。

这一层只有规则，不调用银行、不调用模型（需求 §6.1）：`rules.py` 是风险问卷与适配硬约束，
`recommendation.py` 是筛选、排序与对比维度。依赖方向：`domain → contracts`。
"""

from __future__ import annotations

__all__: list[str] = []
