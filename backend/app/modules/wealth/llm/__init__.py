"""理财 Agent 的模型协作层：提示词与结构化决策解析。

这一层只说"模型该输出什么形状"，不执行任何工具、不碰银行、不做资金写。决策的合法性
（动作词表、工具白名单、参数形状）在这里判定，真正的参数 schema 校验和事实校验由
`infrastructure/tools.py` 负责——`llm` 不导入 `infrastructure`（计划 §4.2 的依赖方向）。
"""

from __future__ import annotations

__all__: list[str] = []
