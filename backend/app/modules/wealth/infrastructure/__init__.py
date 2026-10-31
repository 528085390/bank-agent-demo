"""理财 Agent 的外部适配层：正式实现走银行 HTTP，不缓存业务数据。

正式代码只有 `bank_adapter.py`（`HttpWealthBank`）。HTTP 传输替身只允许放在测试目录，
不会出现在这个包里。
"""

from __future__ import annotations

__all__: list[str] = []
