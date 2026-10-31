# Spec — 架构优化路线图（对齐主流多 Agent + HITL 形态）

- 状态：**待评审**（相对 `4a1700c` 初版：**补全早期三条问题的处置，并改为优先采用主流方案**）
- 日期：2026-10-13
- 基线：`dev` @ `4a1700c`（本文件初版）/ 代码能力以 `d058148` 为准（transfer 仍为 `turn=`；card / wealth 已是 `graph_builder`）
- 范围：**底座与模块接入的架构**；不评审各子智能体业务规则、提示词或字段细节
- 实施计划：`docs/superpowers/plans/2026-10-13-architecture-optimization-plan.md`
- 相关：
  - `docs/ARCHITECTURE.md`
  - `docs/SPEC_SUPERVISOR_TOOL_CALLING.md`
  - `docs/superpowers/specs/2026-10-12-wealth-native-tool-calling-spec.md`
  - `docs/superpowers/specs/2026-10-13-transfer-card-form-spec.md`
  - `docs/card-management.md`
  - 外部参照（主流做法，非依赖锁定）：
    - LangGraph 多 Agent：[Supervisor](https://langchain-ai.github.io/langgraph/tutorials/multi_agent/agent_supervisor/) / [handoffs](https://langchain-ai.github.io/langgraph/how-tos/multi-agent-network/)
    - LangGraph HITL：[interrupt](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/add-human-in-the-loop/) + checkpointer
    - 可选库：[`langgraph-supervisor`](https://github.com/langchain-ai/langgraph-supervisor)（模式参考；是否引入见 §8 裁决）

---

## 0. 结论

当前架子**可行**，不必回到 v0.2 的 DAG + Execution Kernel，也不必整仓重写。

早期评审指出的三条结构性问题（两套编排、自研过重、接口不统一）**仍然部分成立**。本路线图要求用**主流多 Agent + HITL 形态**收口，而不是继续叠自研特例：

| 早期问题 | 是否还在（基线） | 本 spec 的主流解法 | 工作包 |
| --- | --- | --- | --- |
| 两套编排：Supervisor 自带工具循环 + 模块再套一层图；状态一半在检查点、一半在内存 | **部分还在**：card/wealth 已是子图+checkpoint；transfer 仍是 `turn=`+内存；底座仍保留「通用领域工具循环」第二条执行路径 | **只保留一条执行路径**：领域 = Supervisor 图上的 **compiled subgraph**（`graph_builder`），共用同一 checkpointer / `thread_id`；删除 `turn=`；通用领域工具循环降为内部实现细节或删除，禁止第三种「模块自己 `invoke` 无 checkpointer 的图」 | A1、A5 |
| 自研偏多：协议解析、确认状态机堆在 `supervisor.py` | **还在**：HITL / handoff / 流式 tool call 收集大量自研；模块还 import 私有函数 | **协议与 HITL 对齐官方能力**：决策只用模型原生 tool calling（已做）；暂停/恢复只用 LangGraph `interrupt` + checkpointer resume（已做，须收口到唯一确认通道）；模块交互面抽成公开 SDK；`supervisor.py` 降为「构图 + 路由」，逻辑迁到可测试的小模块；**评估**用 `langgraph-supervisor`（或同等 handoff 原语）替换手写 handoff 环，而不是再写一套 | A2、A3、A6 |
| 接口不统一：工具循环型 vs 整轮型 | **形态已变**：整轮 `turn=` 只剩 transfer；真正的分裂变成「通用工具循环」vs「`graph_builder` 自循环」，外加两套确认、两条写路径 | **一种模块契约、一种确认入口、一种写契约**：`ModuleSpec = tools + handlers + graph_builder`；`kind=confirmation` + `/decide`；读走 Tool Gateway，写走 Write Facade | A1、A3、A4 |

目标终态一句话：

> 一个 LangGraph 应用：Supervisor subgraph 只负责 handoff / 直接回答；每个领域是挂在同一张图、同一个 checkpointer 上的 subgraph；人工确认只走官方 interrupt 语义暴露出的唯一 HTTP 入口；读写都经网关契约；模块只依赖公开 SDK。

**Spec 4（transfer card 化）是 A1 的前置实现，不在本文件重写；但 Spec 4 实现时必须遵守 A2（只依赖 SDK）与 A5（禁止无 checkpointer 的自 `invoke`）。**

---

## 1. 需求与非目标

### 1.1 需求

| # | 需求 | 验收（架构级） |
| --- | --- | --- |
| R-1 | 单一执行路径 | 生产路径上不存在 `ModuleSpec.turn`；不存在「模块内 `graph.invoke` 且不挂父图 checkpointer」；文档只描述 subgraph 路径 |
| R-2 | 状态单一事实来源 | 跨轮业务状态 ∈ {父图 checkpointer 命名空间, 银行表, Runtime 单例}；进程内 `sessions` / `agent_cache` / 每请求新建 agent 上的计数 **不得**承载跨请求状态 |
| R-3 | 模块边界主流化 | 模块只依赖 `app.agents.sdk`；守卫测试禁止 `import app.agents.supervisor` |
| R-4 | HITL 主流化 | 用户可见确认只有 `interrupt` → `kind=confirmation` → `POST .../pending-actions/{id}/decide` → `Command(resume=...)`；card 独立 authorize 路由取消或降为内部实现 |
| R-5 | 协议不自研第二套 | 领域决策与 Supervisor 决策均只走原生 tool calling / `stream(tools=...)`；不得再引入 JSON-in-text 决策协议 |
| R-6 | 写路径有统一门面 | 资金写经 Write Facade（身份、幂等、验证码、错误映射、审计）；模块不得各养一套裸 HTTP 银行客户端作为长期形态 |
| R-7 | Supervisor 可替换/可瘦身 | handoff 环与领域挂载的接口足够薄，使得引入 `langgraph-supervisor`（或手写等价物收束到 <400 行路由文件）不需要改模块业务代码 |

### 1.2 非目标

- 不改各模块业务规则、提示词、金额文案。
- 不恢复 v0.2 DAG / Kernel / `/api/tasks`。
- 不在本文件实现 Spec 4 正文。
- 不强制本阶段改 bank_backend 内部（归属、行锁、真 MFA、日累计）—— **D1**。
- 不要求前端重做确认卡 UI（payload 保持 `confirmation` 11 键）。
- 不要求立刻锁定依赖 `langgraph-supervisor` 版本——先完成 A5/A2 的「官方语义对齐」，A6 再做引入/不引入的裁决落地。

---

## 2. 早期三条问题 → 现状映射（给评审对照）

### 2.1 「两套编排方式并存」

**当时指什么**

- 外层 Supervisor 图自己做工具循环；
- transfer / wealth 再各自 `StateGraph` +（曾）内存会话；
- 确认卡在 checkpointer，快照在内存 → 重启/多 worker 丢待办。

**现在**

| 模块 | 编排 | 跨轮状态 |
| --- | --- | --- |
| card | 父图 subgraph（`graph_builder`） | checkpointer + 银行 `card_actions` |
| wealth | 父图 subgraph | checkpointer（安全计数若挂在每请求 agent 上则仍违规，见 R-2） |
| transfer | **仍是 `turn=`** | 内存 `sessions` / `agent_cache`（Spec 4 计划迁出） |
| 底座 | 仍保留「无 `graph_builder` 时的通用领域工具循环」 | checkpointer |

**主流解法（本 spec）**

对齐 LangGraph 多 Agent：**子 Agent = 父图上的 subgraph 节点**，共享 checkpointer；用 `checkpoint_ns`（或 LangGraph 为 subgraph 提供的命名空间）隔离领域状态。  
禁止模块侧再 `compile().invoke(...)` 一条脱离父图的生命线。

### 2.2 「自研部分偏多」

**当时指什么**

- 自研 JSON 决策协议、确认状态机、流式拼接，堆进 `supervisor.py`。

**现在**

- JSON 文本决策协议已废除（好事，对齐 OpenAI/Anthropic tool calling）。
- 确认仍部分自研，但机制已是 LangGraph `interrupt`——问题转为「入口不唯一 + 逻辑散落私有函数」。
- 模块深度依赖 `_conclude_turn` 等私有符号 → 自研表面还在扩大。

**主流解法（本 spec）**

| 能力 | 主流来源 | 我们应保留的薄封装 |
| --- | --- | --- |
| 派活 | LangGraph supervisor / handoff 工具 | 一个 `handoff(agent_id, instruction)` tool + 路由边 |
| 领域自循环 | subgraph 内 ReAct/tool node | `graph_builder` 返回的 compiled subgraph |
| 人工确认 | `interrupt` + checkpointer + resume | HTTP `/decide` 适配层（身份、验证码、422） |
| 模型调用 | provider SDK `stream` + tools | `AuditedModelClient` 一层审计 |
| 模块编程面 | 官方「子图作者」API | **`app.agents.sdk`**（唯一允许的底座门面） |

不在模块里重写协议解析；不在模块里重写确认状态机。

### 2.3 「模块化对，但接口不统一」

**当时指什么**

- 「工具声明型」vs「整轮 turn 型」两套。

**现在**

- turn 型只剩 transfer；
- 新的分裂：通用工具循环 vs `graph_builder`；`confirmation` vs `card_authorization`；Gateway 读 vs 裸 client 写。

**主流解法（本 spec）**

只留一种对外模块契约（见 §3）；对内允许 subgraph 实现各异，但**不得再暴露第二种平台入口**。

---

## 3. 目标架构（主流形态）

```text
                    ┌─────────────────────────────────────────┐
                    │         单一 LangGraph 应用              │
                    │  thread_id = chat:{session_id}          │
                    │  checkpointer = PostgresSaver           │
                    │                                         │
  HTTP/SSE ────────►│  Supervisor（handoff | 直接回答）        │
  /decide resume ──►│       │                                 │
                    │       ├─ subgraph: transfer             │
                    │       ├─ subgraph: wealth               │
                    │       ├─ subgraph: card                 │
                    │       └─ …                              │
                    │              │                           │
                    │              ├─ 读：Tool Gateway          │
                    │              └─ 写：Write Facade         │
                    └─────────────────────────────────────────┘
                                        │
                                        ▼
                                 bank_backend
                          （资金与执行权威；加固见 D1）
```

### 3.1 硬规则

1. **一张图、一个 checkpointer、一条 resume 通道。**
2. **模块 = subgraph 作者**，只通过 `sdk` 使用 wait / call_tool / conclude / confirmation。
3. **HITL = interrupt 语义**；HTTP 只是适配器，不是第二套状态机。
4. **模型只做 tool calling**；禁止并行维护 JSON-text 决策。
5. **跨请求状态三选一**：checkpointer / 银行表 / Runtime 单例。

---

## 4. 工作包

### A1 — 单一接入：删掉 `turn=`（前置 Spec 4）

1. 落地 Spec 4：transfer → `tools` + `graph_builder`。
2. 删除或构建期拒绝 `ModuleSpec.turn`。
3. 更新 registry 测试与 `ARCHITECTURE.md`。

**主流对齐**：所有领域都是 supervisor 图上的 subgraph，不再有「整轮旁路」。

**验收**：`rg` 无生产 `turn=`；架构文档只描述 subgraph 路径。

---

### A2 — 模块公开 SDK（对标「子图作者 API」）

1. 新增 `backend/app/agents/sdk.py`，导出稳定 API（名称可议，语义固定）：
   - `wait_for_user` / `read_answer`
   - `call_tool`
   - `conclude_turn`（含挂平台确认卡）
   - `awaiting`（输入类 interrupt）
2. card / wealth / Spec 4 transfer **只**从 sdk import。
3. 守卫测试：`modules/**` 禁止 import `app.agents.supervisor`。

**主流对齐**：模块不依赖编排器内部，类似插件只依赖稳定 Host API。

**强烈建议**：与 Spec 4 **同批**，避免 transfer 成为第三个私有-import 客户。

---

### A3 — HITL 收口为唯一确认通道

1. 保留 card 银行 `card_actions`（prepare/authorize/execute）作为**银行侧** pending 模型。
2. 用户点击统一 `/pending-actions/{id}/decide`：同请求内 authorize + `Command(resume=...)`。
3. 删除 card 独立 authorize HTTP 入口；前端只认一种卡。

**主流对齐**：LangGraph HITL 教程的「interrupt → 人 → resume」；HTTP 不发明第二种生命周期。

---

### A4 — 受控写路径（Write Facade）

1. 契约：`bank_customer_id`、幂等键、验证码来源、错误码、审计落点。
2. 实现薄 `WriteFacade`；wealth / transfer 经它写；card 的 action 变体共享同一身份/审计约定。
3. 中期可选：Gateway 支持验证码后写工具也可注册为 WRITE——不阻塞本包。

**主流对齐**：工具网关 / 后端 BFF 对写操作的统一策略层；Agent 不直持生产凭据语义。

---

### A5 — 消灭「第二套编排」（通用循环 vs 自 invoke）【相对初版新增】

**问题**：即使 A1 删掉 `turn=`，底座仍可能保留「无 graph_builder 时走 supervisor 内通用工具循环」，或模块图在 builder 外再 `invoke`。

**做什么**

1. **规定**：有业务工具的领域必须提供 `graph_builder`，循环发生在 subgraph 内（与 `SPEC_SUPERVISOR_TOOL_CALLING` G7 / N3 一致：循环下放，两级保留）。
2. **删除或冻结** supervisor 内「逐步工具 → 回 Supervisor 重路由」的生产路径；空工具领域若只需直出 summary，用极简 subgraph 模板（sdk 提供 `passthrough`/`tool_loop` 模板之一），而不是另一套 ModuleSpec 形状。
3. **禁止** `graph_builder` 返回前/后对领域图做脱离父图 checkpointer 的 `invoke`；builder 只 `compile` 并交给父图挂载。
4. 文档写明与 LangGraph「subgraph as node」等价；若未来引入 `langgraph-supervisor`，领域图作为被调 agent 传入，而不是再包一层。

**验收**

- 架构测试或集成测试：领域 interrupt 后杀进程 → 新进程 resume 成功（Postgres checkpointer，不得只用 InMemorySaver）。
- 代码搜索：模块目录无「自建 `MemorySaver` / 无 checkpointer 的 `invoke` 完成整轮业务」。

---

### A6 — Supervisor 瘦身与主流编排原语【相对初版升级 D2】

**做什么（分两级，评审选一）**

**A6-a（必做，手写但主流形状）**

- 拆文件，而不是继续堆 `supervisor.py`：
  - `agents/supervisor/handoff.py` — handoff 工具定义与路由
  - `agents/supervisor/graph.py` — 构图、挂载 subgraph
  - `agents/hitl.py` — interrupt payload ↔ HTTP `/decide` 适配（可与 sdk 并存）
  - `agents/toolcalls.py` — 已存在，保持为唯一流式 tool call 收集器
- `supervisor.py` 或 `graph.py` 目标：**路由+构图 < 400 行**；模块零引用该文件。

**A6-b（可选，引入官方库）**

- Spike（≤ 1–2 天）：用 `langgraph-supervisor`（或当时官方推荐的 supervisor 预置）挂三个现有 subgraph，验证：
  - Postgres checkpointer
  - 自定义 handoff 指令字段
  - interrupt 跨子图 resume
  - 现有 SSE 事件模型是否可适配
- Spike 报告写入 `docs/superpowers/reviews/`；**通过才允许**换依赖，失败则保留 A6-a，不双轨。

**主流对齐**：优先「官方 supervisor 模式」；库不合适时，手写也必须是同一形状，而不是继续上帝对象。

---

## 5. 延期项

| ID | 债 | 说明 |
| --- | --- | --- |
| D1 | bank_backend 加固 | 归属校验、`FOR UPDATE`、幂等先占位 + `request_hash`、服务间认证、真 MFA、日累计；与 Agent 架子正交，另开 spec |
| D2 | （已升格） | 初版 D2「supervisor 拆分」并入 **A6**，不再当纯延期 |

---

## 6. 执行顺序

```text
A2（SDK）与 Spec 4 / A1 同批或略提前
    → A1 完成（无 turn=）
    → A5（删第二套编排 / 禁自 invoke）
    → A3（确认通道合一）
    → A4（Write Facade）
    → A6-a（supervisor 瘦身）→ 可选 A6-b spike
    →（并行）D1
```

---

## 7. 风险

| 风险 | 缓解 |
| --- | --- |
| Spec 4 先落地、再次复制私有 import | A2 同批；守卫测试 |
| A6-b 引入库拖垮现有 SSE/审计 | 强制 spike 门禁；不通过则停在 A6-a |
| A5 删通用循环影响尚未实现的空壳领域 | 提供 sdk 级「最小 tool-loop subgraph」模板 |
| 严格 msgpack / Postgres 恢复暴露 state 里的 dataclass | CI 开 `LANGGRAPH_STRICT_MSGPACK` + 杀进程恢复测试（挂在 A5 验收） |
| 与「平台零改动」叙事冲突 | Spec 4 可继续平台零改动；A2–A6 是**后续底座优化**，单独立项，不塞进 Spec 4 正文 |

---

## 8. 评审裁决点

1. **A2 是否与 Spec 4 强制同批？** 建议：**是**。
2. **A6-b 是否启动 spike？** 建议：A6-a 完成后再开；默认不阻塞比赛演示。**裁决（2026-10-14）：不启动，从实施计划移出，单独立项于 `docs/superpowers/specs/2026-10-14-langgraph-supervisor-spike-spec.md`。**
3. **A3 是否保留银行 `card_actions`？** 建议：**保留**，只并 HTTP 入口。
4. **A4 审计落点：** Gateway 打点 vs Facade 自写 —— 须定一个。
5. **空壳领域（bill / subscription / life_event）**：A5 时用最小 subgraph 模板，还是暂时不挂节点？建议：最小模板，避免再发明第四种接入。**裁决（2026-10-14）：挂最小模板**，落地为 `backend/app/agents/subgraph_templates.py` 的 `minimal_shell_graph`（单 `conclude` 节点、不调模型、不 interrupt）。
6. **A5 §3 与理财只读流程（`build_read_graph` / `invoke_read_workflow`）：裁决 R11 —— 不是违规，改写它没有收益。**
   §3 禁的是 `graph_builder` 对**领域图**（`build_wealth_graph`）做脱离父图 checkpointer 的 `invoke`。只读图不是领域图，且**从不暂停**：追问走 `ask_user → END`，模块自己铸 `pending_id`（`backend/app/modules/wealth/workflow/nodes.py:846-868` 只往 state 写 `wealth_asked_question` / `missing_slots`），代码里没有 `interrupt()`。因此它不是第二条暂停/恢复通道，也没有第二份会被重启分裂的记忆。
   反过来把它挂进父图是**有害**的：`WealthWorkflowState`（`backend/app/modules/wealth/workflow/state.py:458-512`）的 28 个通道含本轮计数器（`steps` / `model_calls` / `corrections` / `reads` / `gate_violations`）与累积的 `observations`，是一次性草稿；父状态里的 `wealth_work` 通道全仓零使用。把草稿挂进检查点等于把本轮计数器持久化，正是本 spec 抱怨的"状态一半在检查点、一半在内存"的反向版本。
   落地方式：**守卫测试** `backend/tests/wealth/test_read_flow_has_no_pause.py`（只读图节点集不含可暂停节点、`checkpointer is None`，外加一组对照：领域图 `build_wealth_graph` 确实带 `wait`，所以前两条断言不是恒真）。若要物理消灭这次 `invoke`，另开工作包 **A5-b**。

---

## 9. 附录：非目标重申 / 与业务审查边界

- 模型签名错误、串户、并发扣款等 **P0 业务/银行** 问题：接线类归 Spec 4；银行类归 D1；**不作为本 spec 的业务验收**。
- 本 spec 只保证：**编排、状态、HITL、模块边界与主流多 Agent 形态对齐。**
