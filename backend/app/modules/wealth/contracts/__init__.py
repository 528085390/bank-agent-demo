"""理财 Agent 的契约层：外部输入、对外响应和银行协议。

依赖方向：`application → workflow/domain/infrastructure/llm → contracts`。
这一层不导入上层，也不建立服务、连数据库或读环境变量；它只有类型、常量和纯映射。
"""

from __future__ import annotations

__all__: list[str] = []
