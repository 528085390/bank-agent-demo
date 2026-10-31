"""装配一个可以独立调用的理财 Agent。

工厂只做依赖装配：接 `model`、`bank`、`clock` 三个已经存在的依赖，返回 `WealthAgent`。
它**不**创建共享的 `AgentRuntime`、不打开检查点、不连 Agent 平台数据库，也不在银行不可用时
退回内存实现——`bank` 必须是 `WealthBank`（正式实现是 `HttpWealthBank`），缺依赖就在装配处
报错，而不是变成一次"看起来成功"的假查询。

`clock` 现在就注入：待办有效期是运行时的判断。任务6 用它铸 `waiting_input` 的过期时间，
任务7 用它做多轮追问的过期与取消。

依赖方向：`application → workflow/llm/infrastructure → contracts`。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.integration.model_client import ModelClient
from app.modules.wealth.application.runtime import Clock, SystemClock, WealthAgent
from app.modules.wealth.contracts.bank import WealthBank
from app.modules.wealth.infrastructure.tools import READ_TOOLS, ReadTool

__all__ = ["Clock", "SystemClock", "build_wealth_agent"]


def build_wealth_agent(
    model: ModelClient,
    bank: WealthBank,
    clock: Clock,
    *,
    tools: Mapping[str, ReadTool] = READ_TOOLS,
) -> WealthAgent:
    """把三个依赖装成一个独立 Agent。

    三项检查都是"缺依赖就报错"：`bank` 走协议检查（运行时可检查的 `WealthBank`），`model` 与
    `clock` 检查必需的方法存在。这里不做任何降级——一个没有银行的理财 Agent 无法诚实地回答
    任何理财问题。
    """

    if not isinstance(bank, WealthBank):
        raise TypeError("bank 必须是 WealthBank（正式实现为 HttpWealthBank），不退回内存实现")
    if not callable(getattr(model, "stream", None)):
        raise TypeError("model 必须是 ModelClient，需要一个 stream(messages, *, tools=...) 方法")
    if not callable(getattr(clock, "now", None)):
        raise TypeError("clock 必须提供一个 now() 方法")
    return WealthAgent(model=model, bank=bank, clock=clock, tools=tools)
