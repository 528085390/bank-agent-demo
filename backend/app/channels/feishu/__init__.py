"""飞书（lark-oapi）渠道接入：进站解析、出站发送、长连接生命周期。

两层刻意分开：

* `dispatcher` 是**纯函数式**、可脱离网络测试的对象：给它一段飞书事件的 JSON，它吐出
  一条 `InboundMessage` / `CardActionClick`；外发通过注入的 `send_fn` 完成，测试里换成
  一个只记账的替身即可。
* `runner` 只负责把 lark-oapi 的 ws 长连接跑在**独立工作线程**里，并且能干净地停下。

`lark_oapi` 在这两个模块的**顶层一律不 import**：没装依赖、没有凭据时，
`import app.channels.feishu` 仍然成功（单元测试与本地开发都不该被 SDK 绑住）。真正启动
时才在函数内部 import；import 失败只记一条 error 日志，绝不让应用起不来。
"""

from app.channels.feishu.dispatcher import TEXT_ONLY_GUIDE, FeishuDispatcher, FeishuSendError
from app.channels.feishu.runner import FeishuRunner

__all__ = ["TEXT_ONLY_GUIDE", "FeishuDispatcher", "FeishuRunner", "FeishuSendError"]
