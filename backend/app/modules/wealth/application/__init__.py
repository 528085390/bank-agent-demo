"""理财 Agent 的应用层：对外运行时与依赖装配。

`runtime.py` 是唯一对外入口（`WealthAgent.handle`），`factory.py` 只做依赖装配。
依赖方向：`application → workflow/llm/infrastructure → contracts`。
"""

from __future__ import annotations

__all__: list[str] = []
