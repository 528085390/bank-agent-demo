"""渠道桥接层（飞书 IM）：平台无关的契约、防重、绑定、渲染与调度核心。

本包只暴露"与平台无关"的部分：飞书专用的 WSS 长连与事件解析将来落在
`app.channels.feishu`，好让调度核心不必反过来依赖某个平台 SDK。

导出随 Task 逐步补齐，这里只列已经落地的名字——提前把还没写的模块写进
`__init__`，会让 `import app.channels` 变成 import 链上的定时炸弹（`app.main`
和 `tests/test_import_targets_exist.py` 都会踩到）。
"""

from app.channels.contracts import CardActionClick, InboundMessage
from app.channels.dedup import ChannelDedup

__all__ = ["CardActionClick", "ChannelDedup", "InboundMessage"]
