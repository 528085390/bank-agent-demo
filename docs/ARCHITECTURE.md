# 架构（当前实现）

> 状态：当前实现说明。最后核对：2026-10-29（§4.6 渠道桥接、§6 渠道端点、§13 `0022_channel_binding` 为当日增补）。
> 本文记录已实现的服务边界和运行行为；模块契约只定义接入规则，不代表业务工具已经实现。

## 1. 一句话架构

FastAPI 接收一次对话请求 → 派生该会话唯一的 LangGraph thread → Supervisor 用真实 LLM 选择领域 Agent → 领域 Agent 通过绑定身份的 Tool Handle 调用工具 → 事件以 6 种 SSE 事件流回前端，状态存入 PostgreSQL Checkpointer。

**当前能力边界**：对话底座、会话状态、领域 Agent 声明、工具网关、暂停/恢复和 Vue 前端已实现。**模块只有一种执行路径**：声明生产工具（`ModuleSpec.tools` + `handlers`，走工具网关与 `ToolCatalog`），需要自己决定编排时再声明一个 `graph_builder`（模块自己的 LangGraph 子图；`ModuleSpec` 上已没有第二种整轮处理器入口）。六个领域都声明 `graph_builder`，而且各自有自己的图：理财、转账、卡片、订阅、账单、生活事件都不再走空壳模板（`app/agents/subgraph_templates.py` 的单节点图现在只是「一个工具都没声明的领域」的兜底）。理财可以查询账户、持仓、产品与风险测评，写路径（申购、赎回与验证码确认）也已接入主平台；主 Agent 不直接调用理财的内部工具。卡片管理注册 12 个工具，支持查询、申请、交易限额、商户黑名单、冻结/解冻、挂失/解挂，以及独立确认与模拟验证。订阅管理注册 7 个工具（模型可见 6 个，执行工具只由代码调用），支持查询、取消、暂停、恢复与立即扣款，确认卡片由它自己的子图发放。生活事件注册 5 个工具（模型可见 4 个，`life_execute_order` 只由确认卡回调调用），支持查生日、按预算挑礼物、冻结订单与撤销订单；下单经资金写门面并受当日累计限额约束。卡片链路和实现边界见[卡片管理说明](card-management.md)，订阅链路见[订阅管理说明](subscription-agent.md)，生日链路见[生活事件子智能体说明](../backend/app/modules/life_event/README.md)。

`bank_backend/` 是独立实现的模拟银行服务，包含账户、转账、账单、卡、订阅、理财等业务 API、数据库和测试。卡片模块通过 `BankToolAdapter` 和 `BankClient` 调用它的卡片 API。模拟银行服务的启动说明见 [bank_backend/README.md](../bank_backend/README.md)。

## 2. 组件全景

```text
浏览器 (Vue 3)
   |  POST /api/chat/messages   (SSE 响应)
   v
FastAPI  app/api/routes.py
   |  认证会话 -> user_id / session_id
   |  thread_id = "chat:" + session_id
   v
AgentRuntime  app/agents/runtime.py
   |  进程内 per-session asyncio.Lock
   v
Supervisor Graph (LangGraph)  app/agents/supervisor/graph.py
   |
   +-- transfer      +-- bill        +-- wealth
   +-- card          +-- subscription +-- life_event
   |        （六个领域子图，各自绑定自己的 Handle）
   v
AgentToolHandle -> ToolGateway -> BankToolAdapter -> 模块 handler -> BankClient
   六个领域的 Handle 都注册了工具（卡片 12、订阅 7、账单 3、转账、理财、生活事件 5），通过 handler 调用银行 HTTP。

独立服务：bank_backend/（模拟银行 API；已进入卡片 Agent 调用链）

可选入口：飞书 IM 长连事件 → app/channels/engine.py → 同一套回合与授信路径（见 §4.6）

PostgreSQL LangGraph Checkpointer  app/storage/checkpointer.py
```

卡片写操作采用“准备 → 用户独立授权 → 执行”，银行自行验证授权。验证码不经过模型或聊天记录；SSE 的 interrupt 事件可携带公开的 authorization 元数据。

> **双重视角架构与安全防线全景**：  
> 有关系统在「任务编排（Supervisor DAG 规划）」与「权限分级（大模型碰不到的代码策略引擎与三大物理防线）」两重视角下的高保真 Mermaid 数据流图、状态机流转与安全矩阵剖析，详见专题架构文档：  
> 🔗 [权限分级与任务编排双重视角架构说明](architecture-map/orchestration-policy-architecture.md)。

## 3. 一次对话的完整生命周期

1. 前端 `POST /api/chat/messages`，body 为 `{session_id, message, client_context?}`。
2. `routes.chat` 校验 `body.session_id` 与认证会话一致，不一致返回 `SESSION_MISMATCH`(401)。
3. `AgentRuntime.invoke(session_id, user_id, message, client_context)` 被调用。
4. 进入该 `session_id` 的 `asyncio.Lock`，同一会话的并发请求在此串行。
5. 按调用者身份构建图（见 §7），`thread_id = "chat:" + session_id`。
6. 若**不是** resume 且**不是** `new_goal`，先查该 thread 是否正挂起：是则只重发挂起信息并返回，不重新规划。
7. 否则以 `stream_mode=["updates","custom"], subgraphs=True` 流式推进图。
8. 节点通过 custom stream 发布事件；`__interrupt__` 更新被翻译为 `interrupt` 事件。
9. `event_stream` 把事件编码为 SSE 帧发给前端。

**模型调用次数是结构决定的**，不是实现细节：

| 请求类型 | 模型调用 | 说明 |
| --- | ---: | --- |
| 闲聊/无需委派 | 1 | Supervisor 直接 `agent=null` 收尾 |
| 一次业务请求 | 3 | `Supervisor 决定 → 领域执行 → Supervisor 汇总` |
| 跨域协作 | 3 × 领域数 | 每个领域之后都回到 Supervisor 再决定 |
| 一次暂停的恢复 | +1 | 领域图自己的 process 节点调一次；`wait_for_user` 重放**不调**（见 §8） |

卡片委派会访问模拟银行。选卡和授权暂停由代码处理，恢复时不增加领域模型调用；普通补充信息恢复仍调用领域模型。生活事件（life_event）的生日链路也走自己的图：查联系人与挑礼物是只读工具（一轮最多 3 轮），冻结订单挂确认卡，点击后才经资金写门面提交，银行落 `pending` 订单。

**`thread_id` 只由 `session_id` 派生，而 `session_id` 由 cookie 决定**：前端刷新页面不会换 session，因此**同一个浏览器会一直续接同一个 thread 的 checkpoint 历史**，直到显式 `POST /api/session`。见 §5 关于 transcript 重放的说明。

## 4. 关键组件职责

### 4.1 Supervisor（`app/agents/supervisor/`）

唯一的跨域编排者。它读 transcript 与已收集结果，用**原生 tool calling** 决策（R-5）：

- 调用 `handoff` 工具（参数 `agent_id` + `instruction`）→ 委派给该领域 Agent
- 不调工具、只是说话 → 这段流出来的文本就是给用户的答复，本回合结束

选了未知 Agent 抛 `DecisionError`（不静默降级）。`MAX_DOMAIN_TURNS = 4` 限制单次请求的委派链长度，超限时用已收集结果收尾，而不是撞 LangGraph 的 recursion limit。

图结构：`START → supervisor → (领域节点 | END) → supervisor → ...`

**Supervisor 自己的 assistant 回复就是给用户的回复**，以 `final` 事件发布。

**它不知道任何领域工具**：它手里只有一个 `handoff` 工具，工具参数的枚举就是 `agent_id: purpose` 的领域菜单（`describe_agents`），prompt 里只有 transcript、已收集结果与这份菜单。因此它关于"某能力没接入"的说法**只能来自领域 Agent 的 summary**——要么同一轮刚拿到，要么 transcript 里更早的回复已经写着。这不是幻觉，是转述；但转述时用第一人称（"我这边无法完成"）会让它读起来像第一手知识。

### 4.2 领域 Agent（`app/agents/domain.py` + `declaration.py`）

固定六个，id 精确为：`transfer`、`bill`、`wealth`、`card`、`subscription`、`life_event`。

每个领域 Agent 也用**原生 tool calling** 决策（R-5），菜单是它自己的 Handle 加上平台给的 `ask_user`：

- 调用自己的某个工具 → 调自己的 Handle
- 调用 `ask_user`（`fields` + `display`）→ 向用户提问
- 不调工具、只有文本 → 这段文本就是本领域结论

**领域 Agent 之间不互相调用**，只与 Supervisor 交互。六个领域**不是**六个文件：声明集中在 `domain.py` 的 `DomainDeclaration`，编译逻辑在 `domain.py` / `supervisor/graph.py`。

**基座不再为领域建子图。** 领域要么经 `graph_builder` 拥有自己的图，要么落到空壳模板；"有工具却没有自己的图"是构建期错误（`build_domain_graph`）。三个职责单一的节点（decide / wait / process）是**领域图自己**的形态，基座只提供它们用的原语（`app/agents/sdk.py`，见 §8）。因此**一次暂停不产生额外模型调用**，重放也不会重新生成问题。

**会调模型的节点必须自己开归属作用域。** 审计行（`llm_call_audit`）的 `agent_id` 取自调用发生时所在的审计上下文；回合开始时它被设成 `supervisor`，领域子图节点会原样继承。所以在**注册处**用 `domain_node(spec, assembler, turn, node)` 包一层（`app/agents/sdk.py`）：包装器在 `domain_scope` 里跑节点，节点自己的 `agent_id` 就这样落到它写的每一行上。基座不能替模块开 —— 在子图外面包一层 `subgraph.invoke()` 会把模块移出 LangGraph 的 interrupt/stream/checkpoint 语义（`app/agents/supervisor/graph.py` 的注释）。理财自己开 `domain_scope`（它还要 `handoff.publish()` 回写记忆），卡片、转账、订阅、账单用 `domain_node`；不调模型的空壳模板不需要。

**工具菜单为空时 `ask`/`tool` 一律转成"该能力尚未接入"的 `summary`**（`NO_TOOLS_SUMMARY`，定义在 `app/agents/subgraph_templates.py`）。空壳模板现在只是兜底：六个生产领域都声明了工具，也都有自己的子图。理财、转账、卡片、订阅、账单、生活事件用自己的图回答整轮请求；卡片子图支持确定性的选卡、确认、取消与执行，订阅子图支持暂停、恢复、取消与立即扣款，生活事件子图支持查生日、挑礼物、冻结订单、撤销订单（确认卡带 MFA）；转账子图支持即时转账与定时转账的准备、账户/收款人选择，以及确认后创建（定时任务单不占当日累计）；AA 收款同图支持发起/查进度/付份额/取消（发起只是收款请求不占当日累计，付份额是真资金写、受单笔+当日累计动态升红，2026-10-30 接入）。 Supervisor 的跨域接力支持在后一个 handoff 的 instruction 里写 `{{previous_summary}}`，派发时用前一个领域已核验的 summary 替换；`tests/life_event/test_life_acceptance.py::test_bill_result_becomes_the_birthday_budget_across_two_domains` 覆盖“账单定预算 → 生日落单”两跳。

**理财另有一条只读草稿流程**（`build_read_graph` / `invoke_read_workflow`，`backend/app/modules/wealth/workflow/graph.py:150-194`）：它自己 `compile`、**不带 checkpointer、也不暂停**，由 `WealthAgent.handle` 在一次只读问答里直接 `invoke`。追问走 `ask_user → END`，由模块自己铸 `pending_id`，代码里没有 `interrupt()`。所以它不是第二条 resume 通道，也不是第二个 checkpointer——裁决 **R11**（见路线图 spec §8）。`backend/tests/wealth/test_read_flow_has_no_pause.py` 把它钉住：只读图里没有可暂停节点、不带 checkpointer、`app/modules/wealth/**` 里不出现 `interrupt` 这个入口，而且带着旧问题重开一轮也不会失败——没有检查点时任何 `interrupt()` 都会当场炸，所以"这一轮没失败"就是"这一轮没暂停"。

### 4.3 Tool Gateway 与 Handle（`app/tools/`）

`AgentToolHandle(agent_id, tools, trusted_context, gateway)` 是不可变绑定，由 Gateway 构造，**模型无法构造**。

**目录、授权和参数校验分布在构建期与请求期：**

1. **存在性是构建期的，不在请求期。** `ToolCatalog.build` 对同名工具重复声明抛 `ValueError`，对"授权了一个未声明的工具"抛 `ValueError`。请求期**没有**"先检查存在性"这一步。
2. **授权是结构性的，不是一步。** `ToolCatalog.for_agent` 只把**已授权**的工具放进那个 agent 的 handle；未授权的工具**根本不在 handle 里**，"越权"表现为该工具对这次调用不存在。
3. **请求期唯一有顺序的一对是"查找 → JSON Schema"**：`ToolGateway.invoke` 先 `handle.spec(tool_name)`（`None` → `_denied`），再用 `Draft202012Validator` 校验参数。这个查找**把"没有这个工具"与"没被授权"合并成同一个 `None`、同一句拒绝文案**——对调用方而言两者都是"你不能调它"，分开报会把目录结构泄漏出去。
4. 通过后才把 handle 携带的可信身份注入 `ToolInvocation` 交给 `ToolAdapter`。

**模型在请求里写的 `agent` 字段被完全忽略**——身份在构造 Handle 时就固定了，这是防越权的核心。

`ToolCatalog` 声明六个领域各自的工具：卡片 12 个、订阅 7 个（其中 `subscription_execute` 只由代码调用，不进模型菜单）、生活事件 5 个（其中 `life_execute_order` 只由确认卡回调调用），账单、转账、理财的工具也经同一目录授权。理财的只读草稿流程在自己的子图里直接调用这些工具，不经过网关。生产 adapter 是 `BankToolAdapter`（`app/tools/bank_adapter.py`）：按名称调用模块 handler、校验结果信封，并把失败转换成结构化 `ToolResult`。银行 HTTP 客户端是 `app/integration/bank_client.py`；`RecordingAdapter` 用于测试。卡片的 execute/cancel 工具由代码调用，不进入模型菜单。

`TrustedContext` 携带 `user_id`、`session_id` 与 `bank_customer_id`。最后一项是**银行认识这个用户的身份**，默认 `None`，由 `Identity.bank_customer_id(user_id)` 在 runtime 解析（解析失败降级为 `None`，不让整个回合失败）。**工具必须在 `None` 上失败关闭**，而不是省略银行的 `user_id` 参数——bank_backend 把它当可选参数，缺了会用它自己的默认客户回答，那是**跨客户读数据**，且 HTTP 200。

### 4.3.1 工具错误码

每个工具失败都带一个**稳定的、机器可读的 `error_code`**；测试断言 code，**绝不匹配中文**。

**四张表、两个词汇层**（条数为实测，非抄写）：

| 表 | 位置 | 键类型 | 条数 | 作用 |
| --- | --- | --- | --- | --- |
| `ErrorCode` | `app/tools/protocol.py` | 它自己 | **13** | 工具边界的错误词汇；前端分支、Supervisor、测试都用它 |
| `ERROR_MESSAGES` | `app/modules/base.py` | `ErrorCode` | **13**（与 `ErrorCode` 键集相等） | 基座自身拒绝与模块 handler 的中文文案 |
| `KIND_TO_ERROR_CODE` | `app/integration/bank_errors.py` | `BankErrorKind` | **11**（覆盖全部 kind） | 把银行失败映射成 `ErrorCode` |
| `BANK_ERROR_MESSAGES` | 同上 | **`BankErrorKind`** | **11** | 银行侧失败的中文文案 |

**11 与 13 对不上是设计，不是漏映射。** `BankErrorKind` 说的是**银行做了什么**（含 4 个瞬时 kind），
`ErrorCode` 说的是**系统据此得出了什么结论**：13 个里有 8 个由 `KIND_TO_ERROR_CODE` 产生
（7 个同名 + 那 4 个瞬时 kind 收敛成的第 8 个 `UPSTREAM_UNAVAILABLE`），另 5 个银行侧没有对应 kind
（`NO_CUSTOMER_BINDING`、`OUTPUT_SCHEMA_VIOLATION`、`INTERNAL`、`IDEMPOTENCY_KEY_REQUIRED`、`TOOL_DENIED`）。

**7 个同名 code 里，5 个的中文文案两侧不同、2 个恰好同句**（如 `INVALID_STATE`：银行侧"该业务当前状态不允许此操作。"，
系统侧"当前业务状态不支持该操作。"）。一个描述**银行返回了什么**，一个是**系统自己下的结论**。
两套措辞不会同时出现在同一次失败里——一次失败只走一条路。**不要"统一"它们。**
（实测：`NOT_FOUND` 与 `INSUFFICIENT_BALANCE` 两侧文案相同——同句是巧合，不是要求；
逐条核对见接缝契约 §4.4.3.1。）

`ToolFailure(code, message, *, retryable=False)` 在构造时校验 code 已注册（拼错的码在抛出点就炸）；
`error_code` 是 `ToolResult` 的**最后一个**字段（frozen dataclass，插在中间会让按位置构造的调用点静默错位）。

### 4.3.2 副作用与重试矩阵

`ToolSpec.side_effect`（`READ` / `WRITE`，**默认 `READ`**）决定一次瞬时失败能否重试：

| `side_effect` | 失败 `retryable` | 是否重试 | 理由 |
| --- | --- | --- | --- |
| `READ` | True | **重试一次** | 只读，重试无副作用 |
| `WRITE` | True | **仅当有幂等键时重试**，且复用同一个键 | 有键则重试安全；无键则被拒 |
| 任意 | False | 不重试 | 业务拒绝重试也不会变好 |

- 重试只有一层，在 `app.agents.sdk.run_tool`；**`BankClient` 内部绝不重试**。
- 重试用尽后抛 `UpstreamUnavailable`，所以客户端看到的最终结局是错误，不是"可以重试"的工具结果。
- **默认值是安全方向的反面**：忘声明的写工具会被当成读工具。
  `build_registry()` 能拒绝非法取值（`"WRITE "` 等），**但拒绝不了"省略"**——见接缝契约 §4.5.1。
  这个错没有机械强制，靠模块作者与 reviewer。

### 4.3.3 幂等键

- **在重试循环之外铸一次**（`sdk.run_tool`），随 `ToolCall` 送达适配器；**重试复用同一个键**。
- `call_id` **每次尝试都新铸**（`_call_tool`）。两者问的不是同一个问题：
  `call_id` 是"这是第几次尝试"，幂等键是"这是不是同一个意图"。
  **拿 `call_id` 当幂等键会让重试产生一个新键**，银行会当成两笔独立转账。
- `side_effect == WRITE` 的工具**没有幂等键时拒绝执行**（`IDEMPOTENCY_KEY_REQUIRED`），
  **在任何 HTTP 之前**——fail closed。
- 键**绝不来自模型**：模型的输出只成为 `arguments`，而 `build_registry()` 禁止工具把
  `idempotency_key` 声明成参数（`RESERVED_PARAMETER_NAMES`）。

- **资金写门面：** 转账 / 申购 / 赎回经 `app.integration.write_facade.WriteFacade`（契约见 [受控写路径](contracts/write-path.md)）。身份必须是 `bank_customer_id`；验证码不得进入 checkpointer / ledger；card authorize/execute 共享同一身份约定，审计由 Tool Gateway 打点。

### 4.3.4 信封投影

信封四字段（`ToolData`）的去向：

| 信封字段 | 模型上下文 | checkpoint | SSE | 前端 | 日志 |
| --- | --- | --- | --- | --- | --- |
| `summary` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `display` | ❌ | ❌ | ✅ | ✅ | ✅ |
| `facts` | ❌ | ❌ | ❌ | ❌ | ✅ |
| `business_ids` | **❌（目标）／✅（今天，见下）** | ✅ | ✅ | ✅（纯值） | ✅ |

**`facts` 绝不离开服务器。** 投影是**结构性**的（按字段，不是逐键打标签），所以可以用一条测试证明：
含 `SECRET_MARKER` 的 `facts` 不出现在模型提示词、checkpoint、SSE 帧里
（`tests/test_envelope_projection.py`）。

> ⚠️ **已知偏差：`business_ids` 今天仍然进模型提示词。** `sdk.conclude_domain` 把它写进 `domain_results`，
> 而领域图自己的一轮经 `describe_results`（= `json.dumps(整个列表)`）把它送进领域提示词。
> 投影堵的是另一条路（`sdk.run_tool` 只放 `summary`）。
> **不能靠删键修**：`_collect_business_ids` 读的正是这个键来构造 `final`。
> 面向模型和客户端的结果边界仍需在后续实现工具时复核。
> 已被 `test_the_business_ids_still_reach_the_model_through_domain_results` 钉住。
> 详见接缝契约 §4.6.1。

### 4.3.5 显示协议

前端**不再打印信封的原始 JSON**。今天渲染的是：

| 前端拿到 | 今天渲染什么 |
| --- | --- |
| `summary` | **渲染**（唯一必渲染项） |
| `display.blocks[].type == "text"` | **渲染** |
| `fields` / `table` / `notice` 块 | **跳过**（未知类型降级，不报错、不白屏） |
| 空 `blocks` / `display={}` | 只渲染 `summary` |
| `facts` | **不渲染**（后端已丢弃；前端即使收到也不读） |

当前前端仅渲染 `text` 块；其他显示块类型会被跳过。模块工具实现需要遵循这一现状，或先扩展并验证前端协议。逻辑在 `frontend/src/toolResult.ts`，由 `toolResult.test.ts` 锁定。

### 4.3.6 公共 grounding 守卫（`app/agents/grounding.py`）

模型可以决定"怎么说"，但不能决定"说什么数字和编号"。卡片、账单、订阅、生活事件四个域的模型收尾路径，在把自由文本交给 `finish()` / `conclude()` 之前统一调用 `guard_summary()`：

```text
模型摘要 ──> 提取业务编号 / 金额 ──> 与证据集合比对
                                      ├─ 全部命中：原样返回
                                      └─ 有未命中：替换为 BLOCKED_SUMMARY
                                                    + 本域 status=FAILED
                                                    + subtask_end.grounding_blocked
                                                    + run event GROUNDING_BLOCKED
```

证据只取本轮**工具真实返回**和用户自己说过的话，按优先级包括：

| 证据来源 | 说明 |
| --- | --- |
| `thread` 里的工具信封 | 只读 `status=SUCCEEDED` 的 `result`；工具名、参数不算证据 |
| `facts` | 工具产出的结构化事实，金额按最小单位换算后参与比对 |
| `business_ids` | 工具登记的业务编号，如 `task_id` / `txn_id` |
| 用户原话 | 用户自己说的金额可以作为复述依据，避免"已按您说的 1,500 元办理"被误判 |

编号按业务前缀识别（`txn_` / `task_` / `ord_` / `sub_` / `prod_` / `CA-` 等），金额支持 `¥` / `元` / `万元` 等形式并归一化到最小单位。**没有工具证据的规则问答不参与校验**——"单笔超过 1,000 元需要验证码"是常识答句，不是幻觉。命中拦截时返回不含编号和金额的中性安全句；审计侧写一条 `GROUNDING_BLOCKED` run event，`status=BLOCKED`，payload 带 `reason`，供排障和合规回放。

转账写操作的摘要来自代码或工具返回，不经过模型自由文本，因此不受这一守卫影响；理财域仍保留自己的 `ungrounded_citations` 引用校验。

### 4.4 Checkpointer（`app/storage/checkpointer.py`）

PostgreSQL，基于 `langgraph-checkpoint-postgres` 的 `LockedAsyncPostgresSaver`。

- 部署时用 `backend/scripts/init_checkpointer.py` 建表，**应用启动不建表**：`lifespan` 只调用 `require_checkpoint_tables()` 检查表是否存在，缺失则启动失败。
- **Windows 必须用 selector 事件循环**（`app/core/eventloop.py`），否则 psycopg 异步连接拒绝 Proactor loop。这里有两个入口，形状不同，缺一不可：
  - `configure_event_loop()` 装**策略**，供 `asyncio.run()`、pytest 和 `init_checkpointer.py` 使用。
  - `selector_loop()` 是**循环工厂**，供 uvicorn 使用。uvicorn 0.53 在 Windows 上硬编码 `asyncio.ProactorEventLoop` 且**完全不读策略**，所以只装策略无效——必须显式传 `--loop app.core.eventloop:selector_loop`。`backend/scripts/serve.py` 就是这么启动的，**README 的启动命令用它而不是裸 `uvicorn`**。

### 4.5 SSE 编码（`app/api/sse.py`）

只允许 6 个事件名，由冻结集合强制；出现其他名字抛 `ValueError`（这是图的 bug，不是客户端问题）。帧格式固定：

```
event: <name>
data: {"request_id": ..., "data": {...}, "error": null}

```

**唯一的无名帧**是传输失败错误信封（无 `event:` 行）。前端明确地把无名帧当作错误而非消息。

### 4.6 渠道桥接：飞书 IM（`app/channels/`，2026-10-29 增补）

飞书是 Web 之外的**第二个入口**，不是第二套业务。它只把「一条进站消息」或「一次卡片按钮点击」翻译成一次普通的网银回合：分级、限额、授信、账本、审计全部复用 §8.1 的同一条路径，渠道层**不复制 decide 逻辑**——复制一份就是第二套策略，两套迟早漂移，而漂移的那天用户点一次确认会出两次账。

```text
飞书开放平台 --WSS 长连（出站，无需公网 IP / 域名 / 入站端口）--> app/channels/feishu/runner.py
   |  im.message.receive_v1 / card.action.trigger
   v
app/channels/feishu/dispatcher.py   事件解析 -> InboundMessage / CardActionClick
   v
app/channels/engine.py              白名单 -> 防重 -> 绑定 -> 排他锁 -> 跑回合 -> 渲染 -> 节流刷新
   v
app/api/pending_actions.py          submit_prepared_turn / execute_pending_action_decision
```

三条硬边界（`engine.py` 的模块文档逐条写了对应位置）：

- **排他锁每条路径都要还。** 锁由 `submit_prepared_turn` / `execute_pending_action_decision` 拿到，流式分支在 `try ... finally: held.release()` 里交回；漏一次就把该会话在这个进程余下的生命周期里焊死。`release()` 幂等，重复调用无害。
- **命名排在 final 之后。** `DeferredNaming.run()` 必须在最终答复发出、锁释放之后执行。它是一次额外的模型调用，挡住发布就要让用户盯着「正在生成…」等到超时。
- **验证码不进卡片、不进日志、不进审计。** 卡片只由 `FeishuCardRenderer` 的白名单投影生成；需要验证码的操作只发一条**签名短链**，用户点开在手机页面上自己输入（`GET /api/channels/mfa`），凭据不回传到聊天、卡片或账本。

绑定与准入：

| 环节 | 行为 | 位置 |
| --- | --- | --- |
| 绑定 | 网银页面生成六位一次性绑定码（有效期 600 秒），用户在飞书发 `/bind 123456`；库里只存 `hmac(进程随机盐, 码)`，明文只在响应里出现一次 | `app/api/channels.py`、`app/channels/binding.py`、迁移 `0022_channel_binding` |
| 白名单 | `feishu_allow_from` 之外一律拒绝，且**不回显 open_id**（拒绝不该变成可枚举的名单探测接口）；演示环境可开 `feishu_auto_bind` 落到默认客户 | `app/core/config.py`、`app/channels/engine.py` |
| 防重 | 同一事件或消息重投时静默 ACK；幂等键由 `message_id` 派生，第二次交付被判 receipt；同一张卡片连点只跑第一次，其余返回「已经处理过了」 | `app/channels/dedup.py`、`app/conversation/turns.py` |
| 外发 | `send_fn(payload) -> message_id`，后续用 `update_message_id` 原地刷新同一张卡片；首投抛异常才算本轮失败并撤键，首投拿不到 id 则降级为只发最终一条 | `app/channels/feishu/dispatcher.py`、`app/channels/throttler.py` |

接入与启停：默认关闭（`feishu_enabled=false` 时一个对象都不建、一个字节都不多花）。开启后长连由 `FeishuBridge` 管生命周期，启动失败只记日志、绝不影响主服务；也可以不出进程地用扫码向导接入——`POST /api/channels/feishu/setup` 起设备码流，页面展示二维码，用户确认后凭据写进 `backend/.env.channels` 并**在同一个正在跑的进程里热启**长连（`app/channels/feishu/boot.py`、`setup.py`、`onboarding.py`）。部署侧不需要公网 IP、域名或入站端口，因为长连是出站的。HTTP 面见 §6，设计规格见 §15 的渠道桥接规格。

## 5. 状态模型（`app/agents/state.py`）

| 通道 | 归并方式 | 含义 |
| --- | --- | --- |
| `messages` | append（`add_messages`） | 对话记录 |
| `goal` | last value | Supervisor 交给当前领域的指令 |
| `active_agent` | last value | 本回合要跑的领域；`None` 表示收尾 |
| `domain_results` | append | 本次请求已收集的领域结果 |
| `turns` | last value | 本请求已用的委派次数 |
| `pending_question` | last value | 当前唯一的等待点；`wait_for_user` 从它读回问题。含 `question_id`（绑定问题）、`fields`、`display`、`attempt` |
| `pending_answer` | last value | `interrupt()` 的原始返回值（完整恢复对象），由领域图的 process 节点经 `sdk.read_answer` 校验 |
| `collected_inputs` | last value | 当前领域任务已接受的答案（支持多轮追问） |
| `turn_phase` | last value | `decide` / `waiting_for_user` / `process_answer`（协议取值，不是节点名） |

**只 checkpoint 编排所需的最小信息**。订单、转账、订阅等业务事实属于业务服务，绝不进这里。

`domain_results` 是 append 通道，写普通 `[]` 不会清空而是**什么都不追加**，因此 `new_agent_state()` 和 `goal_reset_state()` 都用 `Overwrite([])` 做真正的重置。结果只属于收集它的那次请求——跨请求携带会让下一个 Supervisor 误以为自己已经拿到过某些结果。

`pending_question` / `pending_answer` / `collected_inputs` / `turn_phase` 都是 **last value** 通道，所以重置就是直接写回 `idle_work_state()`（`None` / `None` / `{}` / `"decide"`），不需要 `Overwrite`。`new_agent_state()` 和领域收尾（`sdk.conclude_domain`）都写它，因此新请求或已结束的领域不可能留下一个没人等待的问题。

**等待通道写在子图自己的 checkpoint 命名空间里。** 一个领域暂停时，`pending_question` 只存在于该领域子图的状态中，父图读到的仍是 `None`。所以真正放弃暂停靠的是替换挂起任务（`as_node="reset_goal"`），而不是在父图把这些字段写成空——后者是空操作。子图节点每次调用都会拿到**全新命名空间**，因此新目标重新进入同一领域时不会继承旧问题。

**身份永不进状态**：`user_id` / `session_id` 只存在于 Handle 的 `TrustedContext` 中。

**transcript 会被完整重放给每一轮的 Supervisor。** `messages` 是 append 通道，跨请求累积，且每轮都经 `describe_transcript()` 全量塞进 Supervisor 的 prompt。由此产生两个必须知道的后果：

1. **旧话题会延续。** 上一轮回复里写过"转账能力尚未接入"，下一轮即使只发 `你好`，Supervisor 也能读到它并复述——**不需要任何领域 Agent 参与**。这是设计使然，不是串台，也不是幻觉。
2. **失败轮次的用户消息不会消失。** 用户消息在 `new_agent_state(message)` 里随请求一起落库；若这一轮随后失败（模型返回非 JSON → `DecisionError`）或被刷新掐断，那条消息**已经在 checkpoint 里**，会继续影响后面每一轮。

对话 transcript 仍完整保存在 checkpoint 里，但 Supervisor 的 prompt **不再由 transcript 组装**：有 conversation 的回合由 `app/context/` 按预算从 `conversation_message` 账本组装视图——一份覆盖早期历史的累积摘要加上最近若干完整轮次（`app/context/view.py`），checkpoint 里的 `messages` 只在无 conversation 的 session 路径上作为兜底。这条路径的边界是：**模型输入有界，checkpoint 大小仍然无界**——`state["messages"]` 只增不减（spec §11 第一条取舍）。

## 6. HTTP 契约

统一响应信封：`{"request_id": ..., "data": ..., "error": null}`，并在 `X-Request-Id` 头回传。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 探活；返回 `{status, model_configured, demo_mode}` |
| POST | `/api/auth/register` | 注册（用户名 + 密码），201；**不签发会话** |
| POST | `/api/auth/login` | 登录：会话 token 进 HttpOnly cookie，CSRF token 进响应体 |
| GET | `/api/auth/me` | 当前账号：登录名、角色、`bank_ready` |
| POST | `/api/auth/logout` | 撤销当前登录态并清 cookie |
| POST | `/api/auth/logout-all` | 撤销该账号的全部登录态 |
| POST | `/api/auth/password/change` | 改密；成功后撤销全部会话（含当前），必须重新登录 |
| POST | `/api/session` | 创建演示会话（需 `ALLOW_DEMO_LOGIN`，默认关闭 → 403 `DEMO_DISABLED`） |
| GET | `/api/session` | 当前会话身份 |
| GET | `/api/settings/llm` | 读模型配置（脱敏） |
| PUT | `/api/settings/llm` | 写模型配置 |
| POST | `/api/settings/llm/test` | 测试模型连通性 |
| DELETE | `/api/settings/llm` | 清除模型配置 |
| PUT | `/api/settings/email` | 设置用户邮箱 |
| GET | `/api/queue/stats` | 邮件队列统计 |
| POST | `/api/chat/messages` | **唯一聊天入口**，SSE 响应 |
| GET | `/api/audit/llm-calls` | 受控审计查询：模型调用（完整请求/响应） |
| GET | `/api/audit/run-events` | 受控审计查询：运行事件（完整工具载荷） |
| POST | `/api/channels/binding-code` | 当前登录用户生成一枚一次性飞书绑定码（600 秒，明文只返回一次） |
| GET | `/api/channels/mfa` | 手机核身页（卡片短链的落点，页面只带签名令牌；`no-store` + `no-referrer`） |
| POST | `/api/channels/mfa/submit` | 提交验证码；令牌一次性，用掉即作废，失败不释放 |
| GET | `/api/channels/feishu/status` | 飞书长连与配置状态（凭据脱敏） |
| POST | `/api/channels/feishu/setup` | 起扫码接入（设备码流），返回二维码与 setup_id |
| GET | `/api/channels/feishu/setup/{setup_id}` | 轮询扫码结果；授权成功后在同一进程热启长连 |
| DELETE | `/api/channels/feishu/setup/{setup_id}` | 取消本次扫码 |
| POST | `/api/channels/feishu/disconnect` | 停长连、抹掉 `backend/.env.channels` 里的凭据、清掉飞书绑定 |

会话来自 `bank_session` cookie 或 `X-Session-Id` 头；两者同时给出且不一致 → `SESSION_MISMATCH`。cookie 属性：`httponly`、`samesite=strict`、`path=/api`、`secure` 由 `COOKIE_SECURE` 决定。

渠道的两张核身页（`/api/channels/mfa*`）返回 HTML，给手机浏览器直接打开，**不走统一信封**；其余 `/api/channels/*` 都是 JSON 信封。核身页的 URL 与隐藏字段里都是凭据，所以响应头固定 `Cache-Control: no-store, private` 与 `Referrer-Policy: no-referrer`，页面不加载任何外部资源或脚本。

### 身份与会话（`app/identity/`，2026-10-28 增补）

注册登录由 `app/identity/` 承担，路由在 `app/api/auth.py`。它只回答三件事：这个凭据对不对、这个登录态还有效吗、这个用户是谁——不碰对话，也不碰银行。

- **两条 cookie，路径不同**：`bank_session`（HttpOnly，`path=/api`）是凭证；`bank_csrf`（页面 JS 可读，`path=/`）是 CSRF 双提交的凭据。后者必须让页面读得到，否则刷新之后所有写操作都是 `CSRF_TOKEN_MISSING`。写请求要在 `X-CSRF-Token` 里回带。
- **库里存摘要，不存 token**：`login_session.token_hash` / `csrf_hash` 都是 sha256；`session_id` 是内部标识（出现在外键与日志里），不能当凭据用。
- **每个请求查库**：不做进程内缓存，撤销才能立刻生效。超时是两个闸——空闲 30 分钟（`auth_session_idle_minutes`）与绝对 24 小时（`session_hours`），都在 `Identity.authenticate` 里判。
- **注册与银行客户在同一事务**：`bank_customer_pool` 用 `FOR UPDATE SKIP LOCKED` 领一格；领到后调银行 `POST /api/pool/provision` 补位，池空则直接为该用户建号绑定。跨库只做加法，补位失败不影响注册结果（`app/identity/pool.py`）。
- **身份注入点没变**：仍然是 `Identity.bank_customer_id(user_id)`，未绑定时返回 `None`，工具按契约 fail-closed；银行侧 `require_user_id` 不再回落默认客户。
- **失败语义收敛**：`SESSION_REQUIRED`(401) 覆盖"没登录/已撤销/已过期/账号被禁用"四种情况；登录失败只有 `INVALID_CREDENTIALS`，不做"账号不存在"与"密码错"的区分。

**不存在** `/api/tasks*`、`/api/runs`、`/api/clock`，也没有回滚或交接端点——它们在 Task 4 被删除，且有回归测试盯着。`/api/audit/*` 是**只读**的审计查询（§7.1），不是被删掉的那套治理接口：它没有确认、没有回滚、没有状态机。

> **一个例外，而且它不是那套治理接口回来了**：`POST /api/conversations/{id}/pending-actions/{pending_id}/decide`
> 是**领域模块自己的**待确认操作的按钮事件（§8.1），不是控制面的确认状态机。区别在于它没有审批流、
> 没有任务表、没有第二个执行入口——它就是一次普通的回合提交，只是带了一个绑定到预览的
> `pending_id`，由产生预览的那个模块自己校验。老的 `/api/confirm`、`/api/mfa/*` 仍然不存在。

### 请求模型

所有请求模型 `extra="forbid"`，未知字段返回 **422**（不是静默忽略）。

```jsonc
// POST /api/chat/messages
{
  "session_id": "string",
  "message": "string",            // 1..4000 字符
  "client_context": {              // 可选
    "mode": "new_goal",            // 可选：放弃当前挂起目标，重新开始
    "resume": { "value": { } }     // 可选：回答当前暂停
  }
}
```

客户端**永远不发送 thread id**——thread 由服务端从会话派生。

### 错误码

`SESSION_REQUIRED`(401)、`SESSION_MISMATCH`(401)、`LLM_NOT_CONFIGURED`(503)、`DEMO_DISABLED`(403)、`MODEL_NOT_CONFIGURED`、`INVALID_PARAMETER`(422)、`HTTP_ERROR`、`DATABASE_UNAVAILABLE`(503)、`INTERNAL_ERROR`(500)、`STREAM_INTERRUPTED`（仅 SSE 传输失败）。

账号相关错误码单列在 `app/identity/errors.py`（一个码 = 一句固定文案 + 一个固定状态）：
`INVALID_CREDENTIALS`(401)、`LOGIN_NAME_TAKEN`(409)、`LOGIN_NAME_INVALID`(422)、`PASSWORD_POLICY`(422)、
`LOGIN_RATE_LIMITED`(429)、`CSRF_TOKEN_MISSING`(403)、`CSRF_TOKEN_INVALID`(403)、`ORIGIN_REJECTED`(403)、
`CURRENT_PASSWORD_INVALID`(422)、`PASSWORD_UNCHANGED`(422)、`FORBIDDEN_ROLE`(403)。

`app/api/sse.py` 会将流式处理中的非 `AppError` 异常转换为 `STREAM_INTERRUPTED` 错误信封；具体异常类型记录在服务端日志中。

## 7. SSE 事件契约

**事件词表固定**：权威是 `backend/app/agents/runtime.py` 的 `EVENT_FIELDS`，未登记的事件名在运行时会抛错；`backend/app/api/sse.py` 的 `EVENT_NAMES` 是它的镜像，另含一个尚无后端发布者的预留名 `clock`。data 字段固定：

| 事件 | data 字段 |
| --- | --- |
| `agent_start` | `agent_id` |
| `tool_call` | `agent_id, tool_name, call_id, arguments` |
| `tool_result` | `agent_id, tool_name, call_id, status, data, error, error_code` |
| `interrupt` | `kind, question_id, agent_id, fields`（字段元数据对象数组）, `display, attempt`，待确认操作另带 `confirmation` |
| `subtask_end` | `agent_id, status, summary, turn_status, grounding_blocked`（`turn_status` 是模块子图自己的状态，其他发布方为 `null`；`grounding_blocked` 仅在公共 grounding 拦截时为真，其余为 `null`） |
| `plan` | `plan_id, nodes`（`nodes` 是节点字典数组，每项 `id, label, owner, depends_on, status, artifact, error`） |
| `task_update` | `plan_id, node_id, status, artifact, error`（`status` ∈ `pending` / `running` / `waiting_confirm` / `done` / `failed` / `skipped`） |
| `reply_delta` | `text` |
| `narration` | `text` |
| `final` | `text, business_ids`（另带 `terminal_reason`、`error_status`） |

**`tool_result` 的三个易错点**：

- **`data` 是投影后的信封，只有三个字段**：`{summary, display, business_ids}`。
  **`facts` 绝不离开服务器**——帧会被浏览器、devtools 网络面板和截图读到。
- **`error_code` 是 `error` 的兄弟，在事件的顶层，不在 `data` 里。** 它描述的是**这次调用**，
  不是信封；前端就是按这个位置分支的。
- **失败或被拒的帧 `data` 是 `null`**，不是 `{summary: null, display: null, business_ids: null}`。
  `data` **存在**本身就在说"某个工具返回了信封"；造一个全 null 的投影会让**失败**与
  "成功但什么都没返回"无法区分，并让客户端那条 `data === null` 分支**恰好在唯一该生效的场景里走不到**。

- `final.text` 是**完整**助手文本，前端一次性渲染，并按 Markdown 渲染（`frontend/src/markdown.ts`：
  `html: false` + 链接白名单 + 显式标签白名单，回复里的原始 HTML 只会作为文字显示）。
- **`interrupt.kind = "confirmation"` 是"等用户点一下"的暂停**：`confirmation` 是模块自己生成的展示投影
  `{pending_id, operation, title, amount_minor, product_name, account_display, confirm_label, cancel_label, expires_at, requires_code, code_label}`，
  基座不读也不改它。它随问题一起进检查点，所以刷新页面后确认框还在，而且显示的就是用户当时看到的那一笔。
  `requires_code` 决定卡片上有没有验证码输入（见 §8.1）。`pending_id` **不是授权凭据**：真正的授权是
  下面那条鉴权路由上的按钮事件，由模块自己校验归属、会话、意图版本与有效期。`fields` 里那一个
  `decision` 字段是协议用的，页面用两个按钮回答它。
- **`subtask_end.turn_status` 是模块自己的状态**，与冻结的事件词表并列：事件词表只有
  SUCCEEDED/FAILED/DENIED，没有"既非成功也非失败"这个词，所以一次结果未知的资金写在那里只能落在
  FAILED；页面据 `turn_status === "unknown"` 把它显示成"待核实"，而不是"失败"。
- **`plan` 与 `task_update` 是一张图的两半**：一次派发多个领域时，`plan` 把整张图（含 `depends_on`）一次送出，`task_update` 随后按节点推进状态。恢复（resume）时基座先补发 `plan` 再发 `running`，所以刷新页面不会丢图；节点状态只由基座根据派发、`__interrupt__` 与整轮收尾写入，领域智能体不参与。
- `interrupt.question_id` 标识**用户问题**而不是某一次 interrupt 事件：非法答案导致的重新等待沿用原 ID，只有领域生成了新的字段问题时才换新 ID。`attempt` 是该问题的追问轮次，不是上游失败次数。
- `interrupt.fields` 是**对象数组**而不是字符串数组，每项 `{name, label, type, required, options}`。`name` 是回答时的键，`label` 是给用户看的文案。模型只报字段名，其余由模块的 `DomainDeclaration.fields` 声明，未声明时回落（`label` = `name`、`type` = `text`、`required` = 真）。`type` 取 `text`/`number`/`date`/`select`，前端按它渲染控件。富化发生在 `supervisor` 创建问题的**唯一**边界；重新追问时不重新富化，以免悄悄改掉用户正在看的问题的标签。
- **没有** `message` 事件；**没有**心跳帧；**没有**事件 id。文本增量只有 `reply_delta`（正式回复）与 `narration`（派发旁边的解说）两种，流的是**用户可见文本**，决策 JSON 本身不流式（见上面「为什么不做 token 级流式」）。
- 事件不携带 `thread_id` / `user_id` / `session_id`。
- `final.business_ids` 是本轮工具产生或读取的业务对象 ID 列表（去重、保持首次出现顺序），**只发布值不发布键**；无业务工具时为空数组。
- 旧事件名 `intent`、`step_start`、`step_end` **不属于**本协议。`plan` 曾是旧名，D2 起是正式事件（见上表），语义完全不同：旧名指一步规划，现在指整张任务图。

**为什么不做 token 级流式**：模型输出的是**给机器看的 JSON 决策**（`{"agent":null,"reply":"..."}` 或 `{"agent":"bill","instruction":"..."}`），不是给用户看的文本。逐 token 推给前端会先露出半个 JSON；而且一次请求有 1–3 次调用，只有含 `reply` 的那次才有用户文本，中途还可能 `interrupt` 暂停。要做需要先改协议设计（增量 JSON 解析、字段顺序假设、已显示文本的撤回）。

SSE 是**在线展示通道**：不持久化、不可重放、无投递保证。断线后重发消息即可，或由业务页面用业务 ID 查询。

## 7.1 审计：模型与工具的完整载荷

SSE 是投影，审计是原件。两者由**同一次调用**分别写出，互不派生：帧给浏览器看，审计行给排障和合规看。

### 两个表（迁移 `0008_llm_interaction_audit`）

| 表 | 一行是什么 | 关键字段 |
| --- | --- | --- |
| `llm_call_audit` | **一次**模型调用尝试 | `call_id`（唯一）、`conversation_id`/`run_id`/`user_id`/`source`、`prompt_json`、`response_json`、`response_text`、`result`、`error_code`、`latency_ms`、`input_tokens`/`output_tokens` |
| `agent_run_event` | 一次 run 的一个事实 | `event_id`、`conversation_id`/`run_id`/`call_id`、`event_type`、`agent_id`、`tool_name`、`status`、`payload_json`、`error_code` |

**两张表都没有外键，且都不更新。** 审计行必须能在会话被删除后继续可写可读，删除会话也**绝不**级联删除审计；所以关联列是普通字符串加自己的索引。`AuditService` 只有 insert/append，没有 update/delete——能改的审计不是证据，更正就是再写一行。

### 全量采集入口

| 入口 | 记录什么 | `source` |
| --- | --- | --- |
| `AuditedModelClient`（`app/audit/model_client.py`） | 每次 `complete()`：完整 messages、影响请求的参数、完整 provider 响应、标准化文本、耗时、token | 由当前审计上下文决定 |
| 会话回合（`ConversationTurns.stream`） | `RUN_STARTED`/`RESUMED`/`AGENT_STARTED`/`INTERRUPTED`/`RUN_SUCCEEDED`/`RUN_FAILED`/`RUN_ABORTED` | `conversation` |
| 旧 session 路径（`AgentRuntime.invoke` 无 `turn`） | 同上，但**不伪造** conversation id | `session_chat` |
| `ToolGateway` | adapter 调用前后各一条：完整参数 + 完整 `ToolResult`（含 `facts`）、注入的 trusted context、幂等键；拒绝路径也写 | 继承当前上下文 |

`AuditedModelClient` 包在**每一条**模型客户端安装路径外面（设置面板测试、设置面板安装、环境启动 probe），所以摘要调用、配置测试和启动探测都自动被覆盖。**API key 从不进入任何记录**：装饰器只拿到 messages 和适配器自报的请求参数，凭据留在适配器自己的客户端对象里。

### 上下文与关联

`app/audit/context.py` 用 `ContextVar` 承载当前交互的 `conversation_id`/`run_id`/`user_id`/`source`。回合在调用图之前设置它，结束（正常、异常、或客户端中途断开）后**恢复**上一个值，所以复用同一 worker 的下一个请求不会串到上一个会话。设置接口的测试和 probe 保留发起者 `user_id`，但不关联 conversation/run；启动 probe 没有发起用户，这些关联值为 `null`。

`run_id` 只是**关联 ID**，不是生命周期实体：resume 产生新的 `run_id`，继续同一个 conversation 的 checkpoint thread。**执行状态的唯一来源仍然是 LangGraph checkpoint**，审计事件只记事实，不用来重建或推断状态。

### 查询

`GET /api/audit/llm-calls`、`GET /api/audit/run-events`：按 `conversation_id`/`run_id`/`call_id`/`event_type`/`since`/`until` 过滤，`limit`（≤200）+ `cursor` 分页，**始终**以调用者自己的 `user_id` 过滤。指定 `conversation_id` 时先按归属校验该会话，别人的会话返回与其他会话路由一致的 `NOT_FOUND`——否则"空页 vs 404"就能枚举出 id 是否存在。

原始 prompt、provider 响应和工具 `facts` 可能含个人及金融信息。数据库需使用最小权限；生产部署还需配置静态/字段加密、保留期限，并将完整载荷查询限制在受控网络与审计运维流程内。当前服务按会话所有者隔离查询，未实现独立的运维角色或审计读取行为日志。

审计写入失败**不降级**：`AuditService` 写不进去就抛，回合随之失败。规范明确禁止"悄悄退回只有日志知道"。

会话回合还会在公共 grounding 拦截时追加一条 `GROUNDING_BLOCKED` run event（`status=BLOCKED`，payload 含 `reason`），与 `subtask_end.grounding_blocked` 同源，见 §4.3.6。

客户端在普通推理轮点“停止”或断开 SSE 时，回合以 `RUN_ABORTED`（`status=ABORTED`，`error_code=ABORTED`）收尾，与 `RUN_FAILED` 区分；确认卡提交阶段不提供停止入口，避免把已提交的银行写误当成可撤销。

## 8. 暂停与恢复

暂停协议由**领域自己的图**实现：基座不再为领域建子图（§4.2），只提供原语（`app/agents/sdk.py` 的 `wait_for_user`、`read_answer`、`next_attempt`、`pending_context`、`conclude_domain`）。领域图把三个职责单一的节点按"是否允许碰模型"划分：

```text
decide（调模型）-- 结论/工具 --> END
   |
   +-- ask --> wait_for_user（只 interrupt，绝不调模型）
                   |
                   +-- answer --> process（调模型）-- 结论/工具 --> END
                                                  +-- ask --> 回到 wait_for_user
```

`wait_for_user` 从 checkpoint 读回 `pending_question` 再 `interrupt()`，所以**重放不产生模型调用**，问题与用户答案稳定对应。答案以合法 JSON（含 `pending_question` 与 `user_answer`）交给 process 节点。没有工具、也没有自己的图的领域走空壳模板：单节点、不调模型、不 interrupt，因此不可能产生暂停。

**恢复请求按问题绑定**，目标形状：

```json
{"resume": {"question_id": "q_...", "value": {"field": "user-value"}}}
```

process 节点经 `sdk.read_answer` 依次校验外层对象、`question_id` 是否等于当前 `pending_question.question_id`、`value` 是否为对象、字段集合是否完全相等、是否为空。三类失败被刻意区分：

- **`question_id` 不匹配**（答的是别的问题，或本 thread 已无等待点）→ 恢复协议错误，返回 SSE `INVALID_INTERRUPT_ANSWER`。重新提问是错的：客户端不是搞错了字段，而是在回答一个已经不存在的问题。
- **ID 对但 `value` 不可用**（缺字段、额外字段、空值、非对象）→ **重新建立同一个 interrupt**（复用同一 `question_id`，`attempt` 递增），并把原因写进 `display`。不发 SSE error。
- **答案没问题，是上游瞬时失败**（一次重试后仍失败）→ 同样复用原 `question_id` 重建 interrupt，但 **`attempt` 保持不变**：这不是新一轮提问，也不是用户的错，用掉用户的提问预算会让一次上游抖动错误地终结一个用户明明答对了的领域。

`MAX_ASK_ATTEMPTS = 3` 兜底：第 3 次非法答案使 `attempt` 超过上限，转为"未能获得有效信息"的收尾（`subtask_end.status = FAILED`），不再建立 interrupt。`attempt` 跨问题单调递增——若每个新问题都从 1 重新计数，一个不断换问题问的模型就永远撞不到上限。**工具菜单为空时 `ask`/`tool` 一律转为能力未接入的 `final`**，因此空目录不可能产生暂停。

前端收到 `interrupt` 事件渲染输入卡；用户提交后带 `{question_id, value}` 再发一次，服务端把**完整恢复对象**转成 `Command(resume=...)`，不提前拆掉 `question_id`。过渡期兼容旧的 `{"resume": {"value": ...}}`：runtime 只在**恰好存在一个等待问题**时补上它的 ID，猜错会答非所问。

### 8.1 待确认操作：按钮事件而不是聊天文本

模块自己的子图可以返回 `TurnStatus.WAITING_CONFIRMATION` 加一份 `TurnConfirmation`。
基座据此写一个 `kind="confirmation"` 的 `pending_question`（含模块那份投影）并路由到 `wait_for_user`，
于是预览变成**一次真正的检查点暂停**：SSE 收到 `interrupt`，刷新页面能从
`GET /api/conversations/{id}` 的 `pending_interrupt` 读回同一张卡片。

**卡片按操作的要求变形。** 投影里的 `requires_code` 来自模块对银行能力的声明
（`WealthBank.write_authorization()`）：为真时卡片上出现验证码输入，用户当场输入，确认按钮在输入前
保持禁用；为假时就是一张纯确认框。两件事——卡片长什么样、提交时带什么——由**同一个值**驱动，
所以它们不会对不上（卡片问了银行不要，用户就在为一个不存在的门禁输入秘密；银行要了卡片不问，
用户点了也提交不了）。

用户点击后：

```
POST /api/conversations/{id}/pending-actions/{pending_id}/decide
     { "question_id": "q_...", "decision": "confirm" | "cancel", "verification_code": "..." }
```

这条路由只做三件事，都是**服务端自己**的判断：

1. **绑定校验**：`question_id` 必须是检查点里那个暂停，`pending_id` 必须是那份投影里的待办编号。
   两者都对不上就是 `INVALID_INTERRUPT_ANSWER`（409）——一张过期的卡片不会被应用到"现在开着的
   那一笔"上。
2. **必填项校验**：卡片说要验证码而这次点击没带，就是 `VERIFICATION_CODE_REQUIRED`（422），
   **在任何东西被写下来之前**拒绝——一次不可能成功的点击不该消耗序号、也不该在账本里留一行。
3. **交给同一条生命周期**：消息、幂等键、会话锁、账本和 SSE 都走 `_turn_response`，所以按钮事件
   和一条消息在持久化上没有第二套实现。

**幂等键由客户端给**，和消息路由一样：同一个键的第二次点击是同一次提交的重放（直接回执），而
**输错验证码之后的重试是一次不同的请求**，不能被去重掉——这正是服务端派生键做不到的事，因为从外面
看两次点击一模一样。重复提交还有两道保险：模块自己的 `submitting`／`receipt` 守卫，以及银行按同一个
幂等键去重。

**验证码不进检查点。** 它由路由接住，沿 `TurnContext.verification_code` 送到模块子图的 `process` 节点，
再进 `TurnAction`。它**从不进入 `AgentState`**，而图状态每个节点后都会被 checkpoint——所以账本、
检查点、模型上下文与审计里都没有它。

模块子图的 `process` 节点把决定交给模块的受控动作入口（`action_kind` + `TurnAction`，见 `app/agents/sdk.py`），
拿到的是 `TurnAction(kind, pending_id, verification_code)`：金额、产品、账户都以模块冻结的快照为准，
聊天里的一句"确认"走的是普通消息通道，在那里模型没有任何能铸造授权的动作。

模块没有动作入口，或者点击没落在卡片给出的两个选项上时，基座如实收尾（`DENIED` + 一句话），
而不是把点击伪装成一次成功。

**兼容只针对字段完全缺失。** `question_id` 存在但不可读（`""`、`"   "`、`null`）是 `INVALID_INTERRUPT_ANSWER`，绝不回退到"自动绑定当前问题"——客户端已经对"在回答哪个问题"做了声明，读不懂的声明不等于没声明，而回退恰好会造成 `question_id` 要防的那种错配。为了让 `null` 与"未发送"可区分，路由解析用 Pydantic 的 `model_fields_set` 保留字段存在性（`exclude_none=True` 会把显式 `null` 抹掉，两者就无法分辨了）。

⚠️ **恢复会从该节点开头重放**，因此 interrupt 之前不能有不可逆副作用。工具调用是唯一有副作用的步骤，且位于 interrupt 之后。

恢复节点从 checkpoint 读取原问题并调用 `interrupt()`，恢复时不重新生成问题；恢复答案包含 `question_id` 并由图内校验。

**普通消息遇到挂起对话**：服务端重发当前挂起信息并返回，不重新规划、不静默丢弃暂停。

**`mode="new_goal"` 放弃旧目标**：runtime 先执行**一次**状态覆盖（`aupdate_state(..., as_node="reset_goal")`），再沿正常 `astream()` 路径写入新消息。`reset_goal` 节点无副作用、**不从 `START` 进入**、也不在 Supervisor 的 conditional route 里，它只是这次覆盖的合法锚点。覆盖必须真正替换挂起任务：给一个挂起的 thread 传新输入**不会**消费它的 interrupt，图只是从 `START` 重跑，暂停仍在原地，下一次普通消息会被当成对它的回答。`messages` / `domain_results` 是 append 通道，必须用 `Overwrite` 才清得掉；`collected_inputs` 等 last-value 通道写 `{}` / `None` 即可。该操作只改图状态，不删除历史 checkpoint 或业务事实。

**工具事件语义**：每次 `tool_call` 的 `call_id` 是**本次调用新生成**的，对应的 `tool_result` 原样复用它。`call_id` 由**网关统一盖章**（`replace(result, call_id=call_id)`）：工具契约并不要求 adapter 回传它，而客户端要靠它配对两个事件，所以服务端不能把这件事托付给 adapter 的自觉——adapter 返回 `None`、空串或别的 ID 时，客户端看到的仍是服务端发出的那个。`subtask_end.status` 取 `SUCCEEDED` / `FAILED` / `DENIED`：只有领域正常完成或工具成功才是 `SUCCEEDED`；工具失败与追问超限是 `FAILED`；工具网关明确拒绝是 `DENIED`。

**上游失败：分类决定是否重试**（`app/agents/upstream.py`）。`ModelClientError` 携带稳定的 `code` 与 `retryable`，provider 的类名只进 `detail`——供应商措辞不稳定，不能用来决定是否再花一次请求。分类按 HTTP 状态优先、异常类名兜底；状态可能落在 `exc.status_code`，也可能落在 `exc.response.status_code`，两种 provider 形状都读。

| 失败 | code | 可重试 |
| --- | --- | --- |
| 超时 / 408 | `LLM_TIMEOUT` | 是 |
| 连接失败 | `LLM_CONNECTION` | 是 |
| 429 | `LLM_RATE_LIMITED` | 是 |
| 5xx | `LLM_UPSTREAM_ERROR` | 是 |
| 401 / 403 | `LLM_AUTH` / `LLM_PERMISSION` | 否 |
| 其他 4xx | `LLM_BAD_REQUEST` | 否 |
| 未知异常 | `LLM_UNKNOWN` | 否 |

规则很窄：**只有明确标记为瞬时的失败才重试，且只在同一次 invocation 内重试一次**。重试次数是局部变量，不进 checkpoint，因此没有失败计数字段，也没有可对账的账本。不可重试的失败立即上报，因为重复认证/参数/契约错误只会重复失败。

- 模型瞬时失败 → 立即重试一次；仍失败时，若当前有问题（领域图的 process 节点）则**复用原 `question_id` 与 `attempt`** 重建 interrupt，提示"暂时无法处理，请重试"，客户端可复用刚才的答案；若没有问题（`supervisor`、领域图的 decide 节点）则冒出为 SSE `LLM_UPSTREAM_ERROR`。
- **第二次失败按自身分类判定，不继承第一次的结论。** 第一次超时、第二次鉴权失败时报的是 `retryable=false` + `upstream_code=LLM_AUTH`，而不是"暂时不可用"——让客户端去重发一个刚被按规则拒绝的请求是错的。只有两次都瞬时失败才是真正的"服务不可用"。无论哪条路径，最多调用两次。
- 工具失败 → **只有 `ToolResult.retryable=True` 才重试**，且重试是新的调用尝试，使用新的 `call_id`，两次调用各自发布 `tool_call`/`tool_result`（失败那次确实发生过，改写成"没发生"会掩盖 adapter 真实收到的调用）。其余 `FAILED` 与 `DENIED` 一律不重试——把所有 `FAILED` 都当瞬时故障会重复执行有副作用的工具。
- 未分类异常 → SSE `INTERNAL_ERROR`，provider 原文只进日志（它可能带请求内容）。

## 9. 并发与部署边界

- 首期**单 worker**。进程内按 `session_id` 的 `asyncio.Lock` 串行同一会话的推进。
- 这是**进程内锁**，不是分布式锁；不支持多 worker 同 session 并发。
- 图**按调用构建**（`_graph_for(context)`），因为领域子图编译时绑定了某个调用者的 `AgentToolHandle`，共享编译好的图会串身份。长期存活的是模型、checkpointer 和 per-session 锁。
- 无 RunRegistry、无 run_id、无 cancel API、无事件表、无 outbox、无跨进程锁、无宕机后事件恢复。需要水平扩展时另立设计。
- 飞书长连也跑在同一个后端进程里，与 Web 共用同一份绑定管理器和同一把 per-session 锁；进程内随机盐意味着**两个 worker 各有各的盐**，网页生成的绑定码在另一个进程里永远核销不了，所以这条入口同样只支持单 worker。

## 10. 配置与数据库

配置见 `app/core/config.py`（pydantic-settings，`extra="ignore"`）。它读的是**相对工作目录**的 `.env`，而应用从 `backend/` 启动，所以模板放在 `backend/.env.example`，实际配置是 `backend/.env`；仓库根目录的 `.env` 不会被读取。`.env` 可覆盖数据库、SMTP、Redis 和演示模式等配置。

`database_url` 的默认值由 `app/core/database_file.py` 从仓库根目录的 `database.txt` 构造，库名是 `app/core/config.py` 里的 `DATABASE_NAME`（= `test`）。要指向其他数据库时可设置 `DATABASE_URL`。

`database_file.py` 是凭据解析的**唯一实现**：`config.py`、`tests/conftest.py`、`scripts/init_test_database.py` 都用它。要换口令只改 `database.txt` 一个文件。

Agent 服务与模拟银行服务使用各自的 PostgreSQL 数据库和 Alembic 迁移链：

| 环境 | 数据库 | 说明 |
| --- | --- | --- |
| Agent 服务默认 | `test` | Agent 服务默认配置；测试用例使用独立 schema |
| 模拟银行服务默认 | `bank_db` | 由 `bank_backend` 管理，使用独立迁移链 |

命名约定：`bank_agent_*` 属于主项目（agent），`bank_*` 属于子项目（模拟银行）。

根目录 `database.txt` 提供默认连接凭据，不含数据库名；库名由各服务配置。该文件包含明文凭据且已提交，公开仓库或对外分发前必须轮换凭据。`TEST_DATABASE_URL` 可覆盖 Agent 测试库。

渠道配置同样在 `app/core/config.py`：`feishu_enabled`（默认 `false`，关闭时渠道对象一个都不建）、`feishu_app_id` / `feishu_app_secret` / `feishu_encrypt_key` / `feishu_verification_token`、`feishu_allow_from`（逗号分隔的 open_id 白名单）、`feishu_whitelist_enabled`、`feishu_default_bound_user` 与 `feishu_auto_bind`（演示用），以及核身短链的 `channel_mfa_base_url`——它必须是手机能打开的地址，配成 `127.0.0.1` 时启动会告警。扫码接入的凭据写在 `backend/.env.channels`（不入库），不覆盖 `backend/.env`。

模型配置：运行时通过 `/api/settings/llm*` 写入（`LlmConfigManager`），**只存在进程内存中，从不落盘**——重启后端即丢失，必须重新配置。**没有 planner 模式，没有关键词路由降级**——未配置模型时请求明确失败（`MODEL_NOT_CONFIGURED`）。

模型客户端的 `response_format` 配置支持 `json_object`；默认 `json_schema` 当前不会向 provider 发送格式约束，模型输出由提示词约束并经 `extract_json_object()` 解析。

## 11. 明确不存在的东西

以下是**架构上刻意不存在**的，不是禁用或空实现。有源码级与接口级回归测试盯着：

- 目录：`app/gateway/`、`app/domain/`、`app/kernel/`、`app/capabilities/`、`app/orchestration/`（工具在 `app/tools/`；审计在 `app/audit/`，见 §7.1）
- 控制面：`RuntimeControl`、`ExecutionKernel`、`ModelPlanner`、旧 `MockGateway`、`/api/tasks*`
- 治理：确认、MFA、回滚、交接、恢复策略、风控
> 这里的"确认"指**控制面的确认状态机**（审批流、任务表、独立的确认/执行接口）。领域模块自己的
> 待确认操作不在此列：它的按钮事件是一次普通回合提交，见 §8.1 与上面的例外说明。
> **`app/audit/` 与"审计仓储不存在"曾经同时为真，现在不是了。** 被删掉的是旧控制面的那套
> 治理仓储；现在存在的是 §7.1 的**只追加**审计域（模型调用 + 运行事件），它没有状态机、
> 没有确认、没有回滚，也不被任何执行路径读取来做决定。`/api/audit/*` 是只读查询。

> **"幂等存储"不存在，与工具协议中的幂等键不矛盾**：底座提供的是**请求侧的幂等键字段**与
> "重试复用同一个键"的语义（§4.3.3），它把键**发**给银行；**本服务侧没有去重存储**，
> 也没有"同一键只处理一次"的服务端强制。去重是否发生取决于 `bank_backend`。
> 所以别把"工具带幂等键"读成"系统已经防重"。
- 编排附加物：RunRegistry、任务状态机、`task_thread_map`、事件表、cursor、重放
- 能力体系：capability registry、capability→tool 映射表、业务字段总表
- Agent 与模拟银行分别运行；仓库未提供容器编排配置。

安全、权限、MFA、风控、审计以后以 **Gateway 插件和业务服务能力**接入，不写进 LangGraph 节点。

## 12. 代码地图

| 路径 | 职责 |
| --- | --- |
| `app/main.py` | `create_app`、lifespan、异常处理器、请求 ID |
| `app/runtime.py` | `Runtime` 装配（database/identity/notification/gateway/bank client/agent/llm） |
| `app/agents/runtime.py` | `AgentRuntime`：thread 派生、per-session 锁、事件适配、银行客户身份解析 |
| `app/agents/supervisor/__init__.py` | Supervisor 包的导出面：图、路由、handoff 与兼容再导出 |
| `app/agents/supervisor/graph.py` | Supervisor 图装配、领域子图挂载、路由与终止判定 |
| `app/agents/supervisor/handoff.py` | 一次流式决策回合：user turn 规则、delta 合并、决策 → `SupervisorOutcome` |
| `app/agents/hitl.py` | 暂停负载 ↔ HTTP 适配：卡片动作识别、验证码要求、卡片授权 |
| `app/agents/domain.py` | 六个领域 Agent 的装配；`DOMAIN_ROSTER` 由注册表派生 |
| `app/agents/declaration.py` | `DomainDeclaration`（含 `fields`）/ `DomainSpec` / `FieldMeta` / `resolve_fields` |
| `app/agents/prompts.py` | Supervisor 与领域提示词模板 |
| `app/agents/decisions.py` | 模型 JSON 决策解析与校验 |
| `app/agents/state.py` | `AgentState` 通道与重置语义、`PendingField` |
| `app/agents/turn.py` | 回合结果契约：`TurnStatus` / `TurnResult` / `TurnConfirmation` / `TurnAction` / `TurnHandler` / `action_handler`（模块 handler 与确认事件共享的类型） |
| `app/api/routes.py` | HTTP 端点（含按钮事件 `POST /api/conversations/{id}/pending-actions/{pending_id}/decide`） |
| `app/api/schemas.py` | 请求模型 |
| `app/api/sse.py` | SSE 编码与事件白名单 |
| `app/audit/service.py` | `AuditService`（只追加）/ `AuditReader`（按 owner 过滤的分页查询）/ `jsonable`（严格序列化） |
| `app/audit/context.py` | `AuditContext` 与 `audit_context`：`ContextVar` 承载当前回合的关联，结束即恢复 |
| `app/audit/model_client.py` | `AuditedModelClient`：包住每一次 `complete()`，成功/失败各写一行 |
| `app/audit/routes.py` | 只读审计查询路由（`/api/audit/*`） |
| `app/modules/__init__.py` | `ALL_MODULES`：六个模块的固定顺序（新增领域只改这里） |
| `app/modules/base.py` | `ModuleSpec` / `ToolData` / `ToolFailure` / `as_envelope` / `ERROR_MESSAGES` |
| `app/modules/registry.py` | `build_registry()`：唯一的装配点，产出 roster / catalog / handlers，并执行 9 条构建期校验（含 `side_effect`、`output_schema`、保留参数名） |
| `app/modules/money.py` | 金额最小单位与显示格式化 |
| `app/modules/<name>/` | 六个领域模块各自的声明，以及它自己的工具、handler 或 `graph_builder` 子图（生产上六个领域都声明 `graph_builder` 且各自有图：理财、转账、卡片、订阅、账单、生活事件） |
| `app/tools/protocol.py` | `ToolSpec/ToolCall/ToolResult/ToolAdapter/TrustedContext`、`ErrorCode`、`SideEffect`、`RESERVED_PARAMETER_NAMES` |
| `app/tools/catalog.py` | 工具目录与 per-agent 授权（构建期拒绝重复声明与未声明的 grant） |
| `app/tools/gateway.py` | 请求期检查（查找 → JSON Schema）与身份注入、`call_id` 盖章、工具审计（调用前写参数、调用后写完整结果、拒绝也写） |
| `app/tools/handle.py` | 不可变 Handle（授权是结构性的：未授权工具不在 handle 里） |
| `app/tools/bank_adapter.py` | 生产 adapter：按名路由到模块 handler、校验信封、转失败 |
| `app/tools/recording.py` | 测试 adapter |
| `app/storage/checkpointer.py` | PostgreSQL checkpointer 与建表检查 |
| `app/storage/migrations/` | Alembic 迁移（HEAD = `0013_agent_run_event_call_id`） |
| `app/notification/` | 邮件与账单摘要（独立于编排） |
| `app/integration/bank_client.py` | 唯一的银行出站口：身份注入、错误分类、绝不重试 |
| `app/integration/bank_errors.py` | `BankErrorKind` 与用户可见中文文案表 |
| `app/integration/model_client.py` | 模型客户端与配置管理 |
| `app/core/eventloop.py` | Windows 上的事件循环策略与 uvicorn loop 工厂 |
| `app/core/database_file.py` | 读已提交的 `database.txt` 拼 DSN |
| `app/identity/service.py` | 账号身份：注册（并发领客户）、登录、登录态校验与撤销、改密 |
| `app/identity/password.py` | 登录名归一化、密码策略、argon2id 哈希/校验、`needs_rehash` 就地升级 |
| `app/identity/tokens.py` | 会话与 CSRF token 的生成、sha256 摘要存储、常量时间比较 |
| `app/identity/ratelimit.py` | 登录失败限速：账号 + IP 双维度，计数落库，5 次 / 15 分钟锁 |
| `app/identity/pool.py` | 银行客户池补位：领走一格补一格，池空时直接为用户建号绑定 |
| `app/api/auth.py` | `/api/auth/*` 路由：解析请求、调 `Identity`、装信封、写认证审计 |
| `app/channels/engine.py` | 渠道调度核心：白名单 → 防重 → 绑定 → 排他锁 → 回合 → 渲染 → 节流刷新；授信仍走 `app/api/pending_actions.py` |
| `app/channels/binding.py` | `ChannelBindingManager`：一次性绑定码（只存 hmac 摘要，600 秒）、`/bind` 核销、失败计数锁定、会话换发与自动绑定 |
| `app/channels/dedup.py` | `ChannelDedup`：事件键 / 消息键 / 点击键三类防重键（前两类共用一个窗口，点击键自己一个窗口）、历史水位线、失败时撤键 |
| `app/channels/render/` | `base.py` 渲染契约、`feishu.py` 卡片渲染（白名单投影、`MAX_TEXT_CHARS`、状态配色） |
| `app/channels/throttler.py` | `StreamThrottler`：流式刷新按最小间隔与最小增量合并，最终帧强制发出 |
| `app/channels/mfa.py` / `code_cards.py` | 核身短链令牌（签名 + 一次性 nonce）与「需要验证码的卡片」登记 / 回写 |
| `app/channels/feishu/` | 平台实现：`boot.py` 长连句柄与热启、`dispatcher.py` 事件解析与发送、`runner.py` 连接、`setup.py` 设备码流、`onboarding.py` 扫码编排 |
| `app/api/channels.py` | `/api/channels/*`：绑定码、手机核身页与提交（HTML 页面固定 `no-store` / `no-referrer`） |
| `app/api/feishu_setup.py` | `/api/channels/feishu/*`：状态、扫码接入、取消、断开 |
| `scripts/serve.py` | **文档指定的启动入口**（见 §4.4） |
| `scripts/init_checkpointer.py` | 建 checkpoint 表 |
| `scripts/init_test_database.py` | 初始化测试库 |
| `frontend/src/App.vue` | 单文件前端：事件流、输入卡、模型设置面板 |
| `frontend/src/toolResult.ts` | 工具结果的显示投影：`summary` + `text` 块，未知块类型跳过，`facts` 不读 |

## 13. 数据库迁移

链：`0001_billing` → `0002_framework` → `0003_audit_sequence` → `0004_base_without_governance` → `0005_email_notification` → `0006_bank_customer_id` → `0007_conversation_persistence` → `0008_llm_interaction_audit` → `0009_context_summary` → `0010_business_audit_event` → `0011_conversation_serial_no` → `0012_llm_call_audit_stream` → `0013_agent_run_event_call_id` → `0014_bill_compatibility` → `0015_bill_doc_tenant_unique` → `0016_daily_operation_total` → `0017_daily_total_single_row` → `0018_auth_credentials` → `0019_drop_display_name` → `0020_merge_bill_period_and_auth`（同时合并 `0018_bill_task_period_nullable` 那条分支）→ `0021_email_notification_task_id` → `0022_channel_binding`（HEAD）。

⚠️ 文件名 `0004_email_notification.py` 内的 `revision` 是 `0005_email_notification`，**文件名与 revision 不一致**。以 revision 为准；后续新增迁移接在当前 HEAD 之后。

`0008` 增加 §7.1 的两张审计表。它们**没有外键**（审计必须能在会话删除后继续可写可读），`downgrade` 按创建逆序删表；`tests/test_audit_migration.py` 在真库上跑 upgrade → downgrade → upgrade 的往返。

`0010` 增加 `business_audit_event`：Agent 服务自身改变的业务与配置（会话重命名/置顶/归档/软删除/恢复、LLM 配置修改），银行业务动作仍由 `bank_backend.audit_logs` 记录。`user_id` / `conversation_id` / `run_id` / `request_id` 都是普通字符串而**无外键**，理由同 `0008`（审计必须在会话删除后继续可写可读，且什么都不级联）；`result` 由 `ck_business_audit_event_result` 限定为 `ok` / `rejected` / `failed`；不存 old/new 全量快照。`downgrade` 逆序先删三个索引再删表；`tests/test_business_audit_migration.py` 在真库上跑同样的往返。

`0011` 给 `conversation` 加 `serial_no`：每个 owner 从 1 起连续、只增不减、**不回收**（归档或软删除都不会把编号让给别人，所以昨天叫「第三个」的那行今天还叫第三个）。编号是**行**的性质而不是列表的性质，所以回填必须替历史会话补号：按 `(user_id, created_at, conversation_id)` 升序走一遍。升级顺序固定为 加可空列 → 回填 → 置 `NOT NULL` → 建唯一约束 `uq_conversation_serial_no_owner`；`downgrade` 先删约束再删列。`tests/test_conversation_serial_migration.py` 在真库上跑这条往返，并钉住链的走向。

`0012` 给 `llm_call_audit` 加 `stream`（`nullable=False` + `server_default=false`）：审计行本来就说得出「发了什么、回了什么」，却说不出「怎么回的」——流式与缓冲的 `response_text` 一模一样。既有行一律读作**非流式**，因为该列存在之前流式根本不存在。

`0013` 把 `agent_run_event.call_id` 从 `String(64)` 放宽到 `String(256)`：Supervisor 给每次工具调用铸 `f"{agent_id}:{tool_name}:{uuid4().hex}"`，相邻两列声明的宽度给出上界 `64 + 1 + 128 + 1 + 32 = 226`。`agent_id = 'subscription'`（12 字符）时它 7 个工具里有 5 个铸出 65-66 字符，而审计在适配器之前落库（`gateway.py:209` 在 `:215` 之前），于是 INSERT 抛 `psycopg.errors.StringDataRightTruncation`，工具**从未运行**。revision id 保持在 28 字符以内，因为 `alembic_version.version_num` 是 `String(32)`。`tests/test_audit_migration.py` 钉住宽度的往返（`256` → downgrade 回 `64` → 再升回来），`tests/test_agent_run_event_call_id.py` 钉住模型自己声明的宽度放得下每个已注册工具铸出的 id。

`0022` 新增 `channel_binding`：平台身份（飞书 `open_id`）与 `app_user.user_id` 的唯一关系表。唯一约束 `uq_channel_platform_user` 建在 `(channel, platform_user_id)` 上——同一个 IM 账号不能被两个人认领，`channel` 进键是为了以后加第二个渠道时复用同一张表；`user_id` 与 `conversation_id` 都是真外键（绑一个不存在的用户或会话，数据库直接拒绝，而不是等到调用时才失败）；`conversation_id` 可空，因为绑定先于会话存在——渠道在首次接触时开会话，再回写。两个时间戳都不用 server default，由 ORM 在插入与更新时填（渠道层从回调线程写这行，不该自己记着打时间戳）。`downgrade` 先删索引再删表，`tests/channels/test_channel_binding_storage.py` 在真库上跑往返。

## 14. 测试地图

`backend/tests/` 覆盖 Agent 运行时、模块接缝、工具网关、银行客户端、邮件功能和数据库集成。数据库集成用例需要可达的 PostgreSQL；缺少测试数据库凭据时相应用例会 skip。前端测试见 `frontend/`，模拟银行测试见 `bank_backend/tests/`。

> **不要并发跑两个 pytest 进程。** `conftest.py` 的 `rt` fixture 在**共享**数据库上建私有 schema 并跑 `alembic upgrade head`，两个进程会互相撞掉，失败方报出的是 `ERROR at setup`（`relation "app_user" does not exist`），且每次撞掉的用例不同——看起来像回归，其实不是。串行跑即可。

| 文件 | 覆盖 |
| --- | --- |
| `test_agent_runtime.py` | thread 派生、锁、事件适配、同步模型不阻塞事件循环、`new_goal` 清除旧目标、身份解析（含失败降级为 `None`） |
| `test_tool_catalog.py` | 目录、Handle 隔离、Gateway 校验、越权拒绝 |
| `test_module_registry.py` | 模块声明与 `build_registry()` 的校验（声明与 handler 一致、名字唯一、授权默认自授、roster 顺序）、信封与 `ToolFailure` |
| `test_money.py` | 金额最小单位格式化：无浮点、零与负数、未知币种、非整数报错 |
| `test_domain_agents.py` | 六个领域子图构建与决策、三节点暂停流、问题绑定与追问上限（含 `attempt` 累计）、工具 `call_id`/`status`、上游失败重试与回边、空工具能力边界 |
| `test_domain_harness.py` | 单域脚手架本身：真实 adapter + 假银行，工具路由、信封、失败、字段元数据 |
| `test_business_ids.py` | 工具结果里的 `business_ids` 一路到 `final`，只发布值不发布键 |
| `test_grounding.py` | 公共 grounding：工具金额/编号可复述、伪造金额/编号被拦截、无工具证据不误判、用户话与 `facts`/`business_ids` 算证据 |
| `test_interrupt_fields.py` | 问题字段的类型与 `resolve_fields` 的兜底 |
| `test_upstream_failures.py` | 上游失败分类表、`call_with_one_retry` 的重试/不重试边界 |
| `test_cross_domain_orchestration.py` | Supervisor 调度、跨域 artifact 传递、结果作用域 |
| `test_multi_agent_api.py` | API 契约、6 事件字段、resume 信封与错误码分层、`LLM_UPSTREAM_ERROR`/`INTERNAL_ERROR` 帧、404 |
| `test_interrupt_reset_integration.py` | 真库上 `new_goal` 放弃挂起 interrupt、旧问题不可再回答、重入同一领域拿到新问题 |
| `test_bank_client.py` | 错误分类、`code` 优先于状态、身份必填、绝不重试、银行英文文案不外泄 |
| `test_bank_adapter.py` | 适配器：信封校验、失败转换、未声明工具 |
| `test_error_codes.py` | `ErrorCode` 与 `ERROR_MESSAGES` 键集相等、`KIND_TO_ERROR_CODE` 覆盖全部 kind、`error_code` 的位置与默认值、拒绝帧的 code |
| `test_write_safety.py` | `side_effect` 校验、无键写工具**不发 HTTP**、重试复用同一个键且 `call_id` 不同、`_may_retry` 的 WRITE/READ 分支 |
| `test_output_schema.py` | `output_schema` 只约束 `facts`、违规报 `OUTPUT_SCHEMA_VIOLATION`、非法 schema 在注册期被拒 |
| `test_envelope_projection.py` | 结构性投影：`facts` 标记不进模型/checkpoint/SSE 帧；`business_ids` 的已知偏差被钉住 |
| `test_identity_binding.py` | `Identity.bank_customer_id` 的绑定与"无绑定即 `None`" |
| `test_trusted_context.py` | 可信身份的形状与默认值 |
| `test_import_cycle.py` | 冷解释器下 `import app.modules` 不触发导入环 |
| `test_api.py` | 旧控制面确已消失 |
| `test_contracts.py` | 纯契约（无需数据库） |
| `test_checkpointer_integration.py` | PostgreSQL checkpointer 真库集成 |
| `test_llm_settings.py` | 模型配置读写与测试 |
| `test_audit_repository.py` | 审计存储：成功/失败记录的完整载荷、工具事件保留 `facts`、同会话可追加多条、无会话记录、序列化器的转换与**拒绝**、服务只能追加 |
| `test_audit_migration.py` | `0008` 真库往返：表与列、查询索引、downgrade 只删审计表、up→down→up |
| `test_audited_model_client.py` | 审计装饰器：请求原样入库、完整 provider 响应、失败记录后原样抛出、一次重试两条记录、`close()` 透传、上下文继承与恢复、API key 不入库 |
| `test_tool_audit.py` | Gateway 审计：成功/FAILED/DENIED/结构拒绝各写完整结果、重试两次独立 `call_id`、`facts` 完整保留、调用前先写、审计写失败则调用失败 |
| `test_conversation_audit.py` | 回合级关联：message↔run↔call↔event、暂停与 resume 产生新 run、失败回合仍有事件、断线不回滚、上下文在回合间恢复 |
| `test_audit_api.py` | 审计查询：完整重建、越权不可见、他人会话返回 404、过滤与分页、游标与 limit 校验、会话接口不泄露审计载荷 |
| `test_audit_end_to_end.py` | 端到端：一次真实工具回合从用户消息追到工具私有载荷；SSE 不含 `facts` 与原始模型对象；设置面板 probe 落入同一审计并带自己的 `source` |
| `test_channels_api.py` | 绑定码端点：登录态、明文只返回一次、响应形状 |
| `tests/channels/test_binding.py` | 绑定码生成 / 过期 / 重用 / 并发核销、失败次数锁定、只存 hmac 摘要、绑定指向不存在用户被拒、自动绑定默认 fail-closed |
| `tests/channels/test_dedup.py` | 事件键 / 消息键 / 点击键、历史水位线（秒与毫秒）、TTL 与淘汰、`forget_message` / `forget_click` 撤键 |
| `tests/channels/test_engine.py` | 调度全链路：未绑定引导、白名单先于一切、重投静默 ACK、同一卡片 30 次连点只跑一次、验证码不上卡也不进日志、失败释放锁、命名排在 final 之后、会话轮换 |
| `tests/channels/test_render_and_throttler.py` | 卡片白名单投影、金额格式、需要验证码时改发短链、流式节流与重复帧抑制 |
| `tests/channels/test_code_cards.py` | 核身回执写回同一张卡、已点击卡片回收、没有 message_id 不登记 |
| `tests/channels/test_feishu_dispatcher.py` | 事件解析（秒/毫秒时间戳）、白名单、重投、点击原地更新、2200 忙重试、首投失败与拿不到 id 的两条兜底 |
| `tests/channels/test_feishu_setup_flow.py` | 设备码流：扫码载荷、轮询 pending / slow_down / 授权成功、`.env.channels` 写入与备份 |
| `tests/channels/test_feishu_onboarding_api.py` | 扫码接入端点：必须登录、不泄设备码、只热启一次、取消幂等、断开清凭据与绑定 |
| `tests/channels/test_channel_binding_storage.py` | `channel_binding` 迁移与约束：单 head、唯一键、两个外键、时间戳、downgrade 往返 |
| `tests/channels/test_e2e_feishu.py` | TC-FS-01..08：不开监听端口、白名单拦截、绑定码核销、单条消息原地刷新、小额一次确认、大额带短链、重投只办一次、失败不外泄也不焊锁 |
| `test_email_send.py` / `test_email_queue.py` | 邮件发送与队列 |
| `test_context_estimator.py` | 启发式估算：CJK 与非 CJK 的权重、向上取整、消息级固定开销、`ESTIMATOR_VERSION` |
| `test_context_ledger.py` | 账本读取：只取窗口内的行、按 `sequence` 升序、陈旧答案只留最新一条 |
| `test_context_budget.py` | 预算算术与配置校验、轮次切分（尾轮未收轮）、`leading_strip` 的"已达标就停"、`chunk_for_summary` 的贪心分块 |
| `test_context_vocabulary.py` | 两个 `context_limit` 错误码的文案、状态码与 `retryable` 差异、`CONTEXT_ASSEMBLED` 词表 |
| `test_context_migration.py` | `0009_context_summary` 真库往返：表与列、唯一约束、`version DESC` 索引、downgrade |
| `test_context_summary.py` | 摘要生成与版本链：分块、覆盖范围取自实际发送的消息、`base_summary_id` 链的四类断裂、`source_hash`、provider 失败不外逃 |
| `test_context_view.py` | 视图组装：触发线内不动、压缩到预算、激进剥离、`no_valid_summary`、reason 与上报载荷 |
| `test_context_integration.py` | 接线：assembler 真的进了 Supervisor 的 prompt、无会话路径、`AuditedModelClient` 下的摘要调用 |
| `test_context_acceptance.py` | spec §13 的 INV 条款端到端：链无缺口、视图含当前消息、陈旧答案不可见、失败摘要不拖垮回合 |
| `test_business_audit_migration.py` | `0010_business_audit_event` 真库往返：表与列、`result` 的 CHECK 约束、三个索引、模型无 `updated_at`、downgrade 只删本表、up→down→up |
| `test_conversation_serial_migration.py` | `0011_conversation_serial_no` 真库往返：`serial_no` 列与 `NOT NULL`、`uq_conversation_serial_no_owner` 唯一约束与列序、模型声明与迁移一致、历史行按 owner 与创建顺序补号、downgrade 先删约束再删列、链的走向（`get_current_head()`） |
| `test_agent_run_event_call_id.py` | `0013_agent_run_event_call_id` 的宽度契约：`call_id` 列容得下相邻两列声明的上界、每个已注册工具铸出的 id 都放得下、无声明的模块跳过而非断言 |
| `test_business_audit.py` | 业务审计的写入与查询：动作与结果记录、按 owner/会话/动作回溯、越权不可见 |
| `conftest.py` | 随机 schema fixture、凭据解析、残留清理 |

`tests/support/` 是共享测试基建，不是测试：

| 路径 | 职责 |
| --- | --- |
| `tests/support/domain_harness.py` | `run_domain()` / `FakeBankClient` / `field_names()` / `without_fields()`：模块团队独立验证自己的工具 |
| `tests/support/scripted_model.py` | `ScriptedModel`：按脚本回放的模型；`RoutingModel`：按 `SUMMARY_MARKER` 把摘要调用与决策调用分流，并分别记录 `prompts` / `summary_prompts` / `decision_prompts` |

## 15. 文档地图

| 文档 | 状态 |
| --- | --- |
| **本文 `docs/ARCHITECTURE.md`** | 当前已实现架构与 Agent 服务运行说明 |
| [模块接入接缝契约](superpowers/specs/2026-09-22-module-integration-seam-contract.md) | 模块到 Agent 基座的接口约束；不表示生产工具已接入 |
| [模块工具契约](superpowers/specs/2026-09-22-module-tool-contract-v1.md) | 领域工具的命名、声明与测试要求 |
| [数据归属契约](superpowers/specs/2026-09-23-module-data-ownership-contract.md) | Agent 服务与独立模拟银行的数据边界和迁移协作 |
| [模拟银行说明](../bank_backend/README.md) | 模拟银行服务的独立运行方式 |
| [模拟银行 API](../bank_backend/API接口文档.md) | 模拟银行 HTTP API |
| [受控写路径契约](contracts/write-path.md) | 资金写身份、幂等、验证码、错误映射与审计落点（Write Facade） |
| [渠道桥接设计规格](superpowers/specs/2026-10-27-im-channel-bridge-design.md) | 飞书 IM 长连的调度契约、三条硬边界与验收用例；现状实现见 §4.6 |
| [赛题符合度分析](COMPLIANCE-ANALYSIS.md) | 逐条对照赛题要求的独立复核：完成度评分、代码与接口证据、P0/P1 清单（2026-10-26） |

旧框架设计、一次性实施计划、过期联调记录和不再使用的账单契约已从仓库清理。账单迁移所需的基线 SQL 保留在 `docs/contracts/sql/001_init.sql`，由 `0001_billing` 迁移读取。
