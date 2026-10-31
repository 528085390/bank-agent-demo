"""理财 Agent 的工作流层：内部多步推进、状态与预算。

`state.py` 是这一层自己的词汇表，`nodes.py` 是每一步做什么，`graph.py` 把它们连成
LangGraph 内部工作流。依赖方向：`workflow → infrastructure/llm → contracts`。
"""

from __future__ import annotations

__all__: list[str] = []
