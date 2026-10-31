"""理财领域：它的声明、它的原生工具、它的领域图。

模块带着工具进场：七个只读工具（产品、单个产品、账户、持仓、单个持仓、风险测评、订单）。基座
因此自己就能调它们——每一次查询都是一次带 `tool_call`／`tool_result` 的网关调用，闸门、审计与
幂等都在基座里，模型不再只从提示词里"知道"有哪些能力。

整轮处理交给领域图（`graph_builder=build_wealth_graph`）：模型轮次、对话槽位、跨轮记忆与待确认
的写入都落在图 state 的 `wealth_*` 字段上，由基座的检查点保存。所以这个模块既声明工具、又声明
图，但**不再声明 `turn`**——`ModuleSpec` 不允许一次请求被两条路径回答。

申购与赎回的**写**仍然不在图里：图只到"待确认"，把预览当确认卡片交给页面；用户点击后带着
`pending_id` 回到图里，由运行时的受控节点完成提交。已停用的验证码入口在这里没有任何路径。
"""

from __future__ import annotations

from app.agents.declaration import DomainDeclaration
from app.modules.base import ModuleDeps, ModuleSpec, bind_writes
from app.modules.wealth.infrastructure.tools import READ_TOOLS, public_handlers
from app.modules.wealth.workflow.graph import build_wealth_graph
from app.modules.wealth.workflow.state import WEALTH_FIELDS

WEALTH = DomainDeclaration(
    agent_id="wealth",
    display="理财 Agent",
    purpose="处理理财产品、持仓、收益、赎回、订单查询、账户余额与风险测评相关的请求",
    fields=WEALTH_FIELDS,
    graph_builder=build_wealth_graph,
)


def bind_wealth(module: ModuleSpec, deps: ModuleDeps) -> ModuleSpec:
    """装配理财只要一样运行时依赖：图构建器的那把写门面。

    申购与赎回的资金写走 `HttpWealthBank`，门面由组合根注入、与转账共用同一份策略与审计
    （见 `wealth/workflow/graph.py` 的 `build_wealth_graph`）。工具都是只读的，不碰数据库。
    """

    return bind_writes(module, deps)


MODULE = ModuleSpec(
    name="wealth",
    declaration=WEALTH,
    tools={name: tool.spec for name, tool in READ_TOOLS.items()},
    handlers=public_handlers(),
    binder=bind_wealth,
)
