# LLM 上下文管理 Spec

状态：**设计定稿（待实现）**  
日期：2026-09-27（定稿 2026-09-28）  
关联：`2026-09-27-conversation-llm-foundation-overview.md`、`2026-09-27-conversation-persistence-spec.md`（已冻结）、`2026-09-27-llm-audit-spec.md`

## 0. 定稿说明

草案（同日同名文件，见 git 历史）只写了方向，没有回答"视图从哪份历史读"和"摘要自己的输入怎么放得下"这两个会决定实现的问题。本节把定稿相对草案的七项决策钉住，正文相应位置已展开。

1. **压缩只做在 prompt 层（方案 A）。** `state["messages"]` 照旧只增不减；压缩只改变喂给模型的 transcript 文本。checkpoint 结构与 LangGraph 的 reducer、resume 语义一律不动。
2. **触发是自动的、同步的。** 预计 prompt 越过阈值时，在同一回合内、调用模型之前生成摘要。用户无感知；代价是那一回合多一次模型调用，延迟变长，已接受。
3. **预算是配置项 + 保守字符估算。** 不引入 tokenizer 依赖；估算方式必须随 run 事件记录，便于事后判断估算是否偏乐观。
4. **超限最多重试一次更激进的裁剪/摘要，仍失败返回稳定错误。** 绝不静默丢消息。
5. **context view 的"最近消息"从账本 `conversation_message` 读，不从 checkpoint transcript 读。** 这是草案没有写清、但必须选的一点：摘要的覆盖范围用 `sequence` 表达，只有视图也按 `sequence` 从账本读，两者才同源、边界才自洽。checkpoint transcript 与账本并不总是等长（持久化 spec §2.4 明确允许"答案已进账本但 dispatch 结果未知"），按 transcript 组装会让 `covered_to_sequence` 系统性错位。
6. **新增第八个会话错误码 `CONTEXT_LIMIT_EXCEEDED`。** 它和既有七个同属 `app/core/errors.py` 的 `ConversationErrorCode`，因此持久化 spec §4"错误码固定为七个"必须同步改为八个（见 §12）。
7. **预算默认值**：`model_context_limit=128000`、`reserved_output_tokens=4096`、`safety_margin=2048`、`summary_trigger_ratio=0.7`、`recent_message_budget=32768`，另有 `summary_max_tokens=1024`。可用输入预算 `input_budget = 121856`，触发线 `0.7 × input_budget ≈ 85300`。

**首期范围**：只压 Supervisor。领域 Agent 的 prompt（`DOMAIN_PROMPT`）只吃本次 invocation 的 `instruction` 与 `domain_results`，是短内容，不参与压缩；它们只加预算断言，超限即失败。

## 1. 目标与边界

当前 Supervisor 的 prompt 从 checkpoint transcript 直接组装（`describe_transcript(message_dicts(state["messages"]))`），长会话持续增长并最终超过模型限制。本 spec 定义模型调用前的 context view：以消息账本为事实来源，按 token 预算组合摘要、最近消息和 checkpoint 中当前 invocation 的 `domain_results`。

首期只做摘要 + 最近消息，不引入向量库和语义检索。

**本 spec 不做**：

- 不写、不改、不删 `conversation_message`；账本只增不改这条规则不因摘要而例外。
- 不写 checkpoint，不新增图状态通道，不改 `AgentState` 的 reducer 语义。
- 不建立运行状态投影，不引入 `conversation_run`，不维护 attempt/lease/状态机。
- 不做跨进程并发控制；沿用持久化 spec 的单进程 conversation lock。
- 不把摘要或完整 prompt 暴露给普通会话接口。
- 不做同一 conversation 内的 `new_goal` reset（持久化 spec §5 已拒绝）。
- **不改变既有 prompt 的措辞与信息集**。压缩的唯一效果是"少掉被摘要覆盖的历史"，逐行渲染格式、字段含义、给模型看的字面量都与今天一致（§3）。顺手调整提示词属于另一个改动，不搭这趟车。

## 2. 事实来源与所有权

| 事实 | 来源 | 本 spec 的角色 |
|---|---|---|
| 用户可见历史 | `conversation_message`（追加式账本） | 视图里"最近消息"的唯一来源；只读 |
| 摘要版本 | `context_summary`（新增） | 只增；新版本不覆盖旧版本 |
| 执行状态 | LangGraph checkpoint | 提供当前 invocation 的 `domain_results`（§3 第 3 项）；**不作为 transcript 来源** |
| 模型实际收到的输入 | `llm_call_audit.prompt_json` | 记录压缩后的完整 prompt，不做二次裁剪 |
| 运行事实 | `agent_run_event` | 记录组装预算与裁剪原因 |

四条由此推出的规则：

- **账本与 checkpoint 不必等长，且以账本为准。** 一条用户消息可以在账本里而不在 checkpoint 里（dispatch 前崩溃）。这类消息仍然真实发生过，进入视图是正确的；视图不需要与 transcript 对账，因为它不写回 transcript。
- **但"以账本为准"不等于"账本全量进视图"。** 这是换数据源时最容易踩坏的一条。持久化 spec §2.4 规定：某条答案已进账本但其 dispatch 结果未知时，用户可用**新 key** 重新作答，"**旧答案保留在账本但不注入 transcript**"。那句话用 transcript 表达这个保证，是因为当时 transcript 就是 prompt 来源；本 spec 把来源换成账本后，若照搬"账本全量"，模型会同时收到两条回答同一问题的 `user:` 消息——陈旧的那条被重新喂回去，保证被静默作废。因此视图必须**对每个 `question_id` 只保留最后一条 `message_kind='interrupt_answer'` 的消息**（见 §3）。这条过滤是账本读取的一部分，不是可选优化。
- **摘要不构成执行状态。** 它是账本的派生视图，存在业务库里，与 checkpoint 无关，因此不违反"数据库事务不跨越 checkpoint 写入"。
- **审计记录的是"实际发送了什么"，不是"理论上应该发送什么"。** 被摘要掉的历史不再出现在 prompt 里，但它完整保存在账本和 `context_summary` 中，可重建；审计不做压缩，也不因此变成摘要的摘要。

**无 conversation 时的退化边界**：session 路径（`invoke(..., turn=None)`）没有账本，`context_summary` 也无从挂靠。该路径退回 checkpoint transcript 组装，不生成摘要，预算守卫仍然生效。

生产代码里**没有**这条路径的调用方：`app/` 内唯一的图调用点是 `ConversationTurns.stream`，它恒传 `turn`；两条 HTTP 路由（`/api/conversations/{id}/messages` 与 `/api/chat/messages`）都经它，后者用 `conversation_for_session` 把登录 session 解析成 conversation。迁移工具 `backend/scripts/migrate_conversations.py` 只盘点 checkpoint 线程并建 conversation，不调用图。因此今天 `turn=None` 仅存在于测试中，退化行为不需要产品级验收，但实现必须让它不崩——因为测试与未来的迁移重放会走到它。

## 3. 上下文视图的组成

一次 Supervisor 模型调用的输入按以下顺序构造：

1. 固定系统提示词和 Agent/工具协议（`SUPERVISOR_PROMPT` 的固定部分，不变）。
2. 当前会话最新**有效**摘要段（可能为空）。
3. checkpoint 中当前 invocation 的结构化结果 `domain_results`。
4. 从摘要覆盖范围之后开始选取的最近完整轮次，按 `sequence` 从账本读出。
5. 当前用户消息（resume 时是本次答案）——**包含在第 4 项内**，因为它在图调用前已持久化，且必然是账本最后一行。

草案 §2 的六项与本节的对应关系：草案第 1 项原样保留；草案第 2 项细化"有效"的定义（§5.3）；草案第 4、6 项合并为第 4 项——账本已经同时提供历史轮次和当前消息，再单列一次会让"当前消息"既在范围内又在范围外，是草案的一处自相矛盾；草案第 5 项由 checkpoint 的 `domain_results` 承担，不另设机制。

**第 3 项只包含 `domain_results`，这是刻意的收窄。** 草案第 3 项写的是"目标、已收集结构化结果、待回答问题或恢复答案"，但今天的 `supervisor_node` 只把 `transcript`、`results`、`agents` 渲染进 prompt（`render(SUPERVISOR_PROMPT, ...)`）；`goal` 由领域 Agent 作为 `instruction` 消费，`pending_question` 与 `collected_inputs` 由领域 Agent 通过 `pending_context()` 消费。把这三者塞进 supervisor prompt 是**一次提示词行为变更**，不属于上下文压缩，本 spec 不做。压缩的任务是让既有 prompt 装得下，不是顺手改变模型看到什么。

第 3 项优先于第 2 项：checkpoint 里的结构化结果描述的是**正在进行的这次**调用，而摘要描述的是已经结束的历史；两者冲突时以 invocation 状态为准。

**第 4 项的渲染格式与今天完全一致**：逐行 `role: content`（`describe_transcript` 的既有形状），`role` 取账本的 `user` / `assistant`。不因为来源从 transcript 换成账本而新增标记——`message_kind='interrupt_answer'` 的消息仍渲染为 `user: <答案>`；账本 `metadata` 里的 `business_ids` 也不进 prompt，与今天一致。这条限制是刻意的：换数据源已经是一次足够大的改动，不应顺手改变模型看到的措辞。

**第 4 项的两条读取规则**（都是账本读取的一部分，不是后处理）：

1. **陈旧答案过滤**：同一 `question_id` 存在多条 `interrupt_answer` 时，只保留 **sequence 最大**的那条。依据是持久化 spec §2.4 的既有意图（旧答案保留在账本但不注入 transcript），本 spec 只是把它翻译成账本语义。被过滤掉的消息仍完整留在账本，用户翻历史仍能看到，审计不受影响。
   **这条过滤对视图的保留范围和摘要的输入范围同样生效**（§7）。只在视图上过滤是不够的：陈旧答案一旦进入摘要输入，就会被摘要写进 `summary_text`，随后每次组装都从摘要里回到模型眼前——修复被自己的压缩机制撤销。这是"过滤必须在读取层统一实施"的原因。
2. **按 conversation 过滤**：只读本 conversation 的消息。账本本身按 `conversation_id` 分列，这条是查询条件而非额外过滤，但它取代了原先由"每个 conversation 一个 thread"提供的隔离保证，因此必须显式存在。

**领域 Agent 不组装 context view。** 它们只收到 `instruction` + `domain_results` + 工具菜单，并断言自身 prompt 在预算内。

## 4. Token 预算与估算

配置项（`app/core/config.py` 的 `Settings`，`.env` 可覆盖）：

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `model_context_limit` | 128000 | 模型上下文窗口（token） |
| `reserved_output_tokens` | 4096 | 留给模型输出的额度 |
| `safety_margin` | 2048 | 估算误差与协议开销的兜底 |
| `summary_trigger_ratio` | 0.7 | 触发摘要的预算占比 |
| `recent_message_budget` | 32768 | 摘要后保留的最近消息目标额度 |
| `summary_max_tokens` | 1024 | 单版摘要文本的长度上限 |

```text
input_budget      = model_context_limit - reserved_output_tokens - safety_margin   # 121856
summary_trigger   = summary_trigger_ratio * input_budget                           # ≈ 85300
```

**配置校验（启动即失败，不留到运行期）**：

- `recent_message_budget < summary_trigger`。裁剪后的目标若仍在触发线之上，摘要就永远收不到触发线以下，每次决策都会再触发一轮。默认值满足该式（32768 < 85300）。
- `0 < summary_trigger_ratio < 1`。等于或大于 1 时，摘要要到已经超过 `input_budget` 才被触发，那时预算检查必然失败，压缩机制形同不存在。
- `summary_max_tokens < input_budget`，否则摘要自身放不进预算（§5.2）。

**这些校验是必要而非充分条件。** 裁剪后的视图是 `固定段 + 摘要 + 保留范围`，而上式只比较了保留范围与触发线，**没有也不可能涵盖固定段**（它含当前用户消息与 `domain_results`，运行期才知道有多大）。当固定段很大时，视图裁剪后仍会高于触发线——这不是配置错误，而是这次请求的客观大小。因此启动校验只挡住"配置本身自相矛盾"，真正的防线是 §6.1 的运行时守卫：**触发摘要必须同时满足"剥离可行"**，否则会在无法收缩时反复触发。INV-11 只声称前一半。

**估算器**（单一函数，便于日后替换为真实 tokenizer）：

```text
tokens(text) = ceil(cjk_chars * 1.0 + other_chars * 0.3) + 4     # 每条消息 +4 协议开销
```

CJK 按 1 token/字符计；非 CJK 按 0.3 token/字符计（真实经验值约 0.25，取 0.3 是刻意高估）。估算器版本号（`heuristic_v1`）必须随 run 事件记录：估算偏乐观是这套方案最可能的失效模式，事后必须能判断是估算错了还是预算配小了。

预算判定的优先级：先保系统协议、当前 invocation 状态和当前用户消息；再保最近完整轮次；仍超预算才触发摘要或进一步裁剪。**任何情况下都不截断当前用户消息**——它装不下时直接返回 `CONTEXT_LIMIT_EXCEEDED`，不做静默截断。

## 5. 摘要模型与版本

### 5.1 `context_summary`

新增表（迁移 `0009_context_summary`）。字段：

| 字段 | 说明 |
|---|---|
| `summary_id` | 服务端生成，唯一 |
| `conversation_id` | 外键 → `conversation.conversation_id` |
| `version` | 同一 conversation 内单调递增，`(conversation_id, version)` 唯一 |
| `base_summary_id` | nullable，前一版 `summary_id`；v1 为 NULL |
| `covered_from_sequence` | 本版**新增推导范围**的起点（含） |
| `covered_to_sequence` | 本版新增推导范围的终点（含） |
| `summary_text` | **累积**摘要文本，代表 sequence 1..`covered_to_sequence` 的全部历史 |
| `source_hash` | 新增范围内账本消息的 SHA-256 |
| `model` | 生成该版摘要的模型名 |
| `created_at` | 写入时间 |

索引：`(conversation_id, version DESC)`。软删除/归档 conversation 不删除摘要；本阶段不提供摘要清理或重建 API。

- `base_summary_id` 是普通字符串列，**不建自引用外键**：它只用于回溯与排障，链的完整性由 §5.3 的应用层校验负责，而自引用外键会让"按顺序写入一串版本"变成一个顺序敏感的插入游戏，收益不抵复杂度。这一点与持久化 spec"不建循环外键"的取舍一致。
- `version` 分配：在写入事务内取 `max(version) + 1`，`(conversation_id, version)` 唯一约束是最后一道防线。**冲突即视为编程错误，抛异常并让整个 assembly 失败**，不设计"回滚后继续"的路径：单进程下 conversation lock 已经串行化了同一会话的全部写入（§6.2），冲突只可能来自锁被绕过的实现缺陷，而任何"继续"的解读都是有害的——沿用刚生成的（未提交）摘要文本会让下一版的 `base_summary_id` 指向一个文本来源不可复现的版本；改用已提交版本则会静默覆盖同一范围，且调用方无从得知自己漏了一块。把未定义分支固化进设计，比让它在缺陷出现时响亮地失败更糟。
- 分块摘要（§5.2）在一次组装内可能连续写入多个版本。**每块一个事务，逐块提交**，不是一个大事务：链的任意前缀都是连续的、因而都是有效的（§5.3 只要求相邻衔接与链首为 1），所以第 3 块失败时前 2 块应当保留——否则下一个请求会把同样的工作重做一遍，在持续故障下形成每次请求都全量重算的放大效应。失败的那一块不写，`CONTEXT_ASSEMBLED` 的 `reason` 记为 `summary_failed`，视图按已提交的最新版本组装。

### 5.2 版本链与"增量推导、分块表达"

- v1：`base_summary_id = NULL`，`covered_from_sequence = 1`。
- v_n（n>1）：`base_summary_id = v_{n-1}.summary_id`，`covered_from_sequence = v_{n-1}.covered_to_sequence + 1`。
- `summary_text` 始终是**累积**的：v_n 的文本覆盖 1..v_n.covered_to。而 `covered_from/to` 描述的是**这一版新读了哪些消息**。

**为什么必须增量，而不是每版重摘全量**：摘要调用自身也是一次模型调用，它的输入同样受 `input_budget` 约束。当会话长到需要摘要时，把 1..to 的全部历史再喂给摘要模型，必然同样超限——草案"生成新摘要"一句话里藏着这个死循环。

**但"增量"本身并不自动给出上界。** 这是本 spec 最容易写错的一点：v_{n+1} 的输入 = 上一版摘要 + 本次剥离范围，而剥离范围 = 触发时的 E − 保留范围。E 只被"每次 supervisor 决策都检查"这一条约束，**没有硬上界**：E 可以远超触发线，此时剥离范围仍可能超过 `input_budget`，摘要调用自己先超限——死循环并没有被消除，只是被推迟了。

因此剥离范围必须**再按完整轮次切块**，逐块连续生成版本：

```text
chunk_budget = recent_message_budget                       # 32768
from_seq = (S.covered_to_sequence if S is not None else 0) + 1
把 [from_seq, 切点] 按完整轮次切成连续块，每块估算 <= chunk_budget
对每块依次生成 v_{n+1}, v_{n+2}, ...：
    第 k 块的输入 = 第 k-1 块产出的摘要文本 + 第 k 块的消息
    （第 1 块的"上一版摘要文本" = S.summary_text，S 为 None 时为空）
```

`from_seq` 的写法同时覆盖两种情况：**增量**（`S` 存在，从上次覆盖点之后继续）与**全量重建**（`S` 为 None，即 §5.3 判定链不可用、或本会话首次摘要，从 sequence 1 起）。全量重建恰恰是最需要分块的场景——历史最长、最可能超预算——因此分块算法对它同样适用，且是唯一可行的做法；`v1` 的 `covered_from_sequence = 1` 正是这条路径的产物。

每块摘要调用的输入上界 = `summary_max_tokens` + `chunk_budget` + 固定模板 ≈ 34k token，恒小于 `input_budget = 121856`。**这才是"摘要输入一定有界"的真正来源**，也是版本链存在的理由：链不是省 token 的技巧，而是让摘要调用的输入可证明有界的唯一手段。

**单轮超过 `chunk_budget` 时的例外。** "按完整轮次切块"在某个轮次自身大于 `chunk_budget` 时无解：既切不出合法块，也不能把它留在保留范围（同样超 `recent_message_budget`）。规则是**该轮作为一个超尺寸块整体处理，不在轮次内部切分**——在轮内切分会让 `covered_to_sequence` 落在 user 消息上，违反 §5.4 的边界规则，代价比让一块变大更高。该块的输入因此可能超过 34k，但只有在超过 `input_budget` 时才真正失败（`reason=over_budget`）。

在当前代码下这条例外**不可达**：用户消息有 4000 字符上限（`app/api/schemas.py` 的 `ChatMessage.message` / `ConversationMessageIn.message` 均为 `max_length=4000`），助手回复受 `reserved_output_tokens=4096` 约束，因此单轮实际规模远小于 `chunk_budget`。它是**潜在**问题而非活跃问题——一旦放宽输入长度上限或调大 `reserved_output_tokens`，就必须重新审视这条。

### 5.3 有效性校验

读"最新有效摘要"时回溯 `base_summary_id` 链并检查：

1. 相邻版本满足 `prev.covered_to_sequence + 1 == next.covered_from_sequence`；
2. 链首 `covered_from_sequence == 1`；
3. 最新版本的 `covered_to_sequence` 严格小于当前用户消息的 sequence（即 INV-1）。

任一条不成立，该版本链判为**不可用**：视图按"无摘要"处理并重新生成，原因记入 run 事件。校验只读，不修复、不删除任何版本。

**`source_hash` 不在热路径重算。** 它覆盖新增范围内 `(sequence, role, message_kind, content)` 的规范化序列化，在写入时留存，供离线核对与重建使用。不在每次组装时重算的理由是：账本只增不改（INV-4），已覆盖范围内的消息不可能变化，重算唯一能发现的是账本被账本之外的手段篡改——这属于离线核对职责，而热路径上重算需要读取整个历史区间，把每次模型调用的成本变成 O(会话总长)，正好抵消了本 spec 想省下的东西。链连续性校验只需要摘要表自身的行，是 O(版本数)，留在热路径。

### 5.4 摘要边界

- `covered_to_sequence` **必须落在一条 `role='assistant'` 的消息上**。这样既不会把一轮拆开，也不会把一条还没被回答的用户消息（含 dispatch 前崩溃留下的孤儿消息）摘要掉。
- 保留范围的起点是 `covered_to_sequence + 1`，它因此必然是某条 `role='user'` 且 `message_kind='message'` 的消息。**这条结论不受陈旧答案过滤影响**：`interrupt_answer` 只能出现在一次 interrupt 之后，而 interrupt 意味着该回合没有写 assistant 消息，所以一条 assistant 消息之后必然是下一回合的普通 user 消息。陈旧答案只可能出现在保留范围**内部**（被过滤掉、不参与渲染），不会成为范围的起点。
- **不变量 INV-1**：`covered_to_sequence < 当前用户消息的 sequence`。摘要永远不覆盖当前消息——否则用户刚说的话会被摘要掉，模型看不到原文。

## 6. 触发、组装与并发

### 6.1 触发点与算法

触发在 `supervisor_node` 内、调用模型之前，**每次 supervisor 决策都重新组装一次**：resume 后账本多了一条答案，且一次 invocation 内 supervisor 会被调用多次（`MAX_DOMAIN_TURNS=4`，控制流为 `supervisor → agent → supervisor`），视图必须每次都重算。

算法必须把两件事分开：**压缩**（把视图缩小，触发线是它的目标）与**预算判定**（能不能发出去，`input_budget` 是硬线）。混在一起就会出现"已经压到极限、仍高于触发线、于是再压一次"的空转。因此：

```text
1. 读账本（含 §3 的两条读取规则）与最新有效摘要 S（可能为 None）。
2. 组装视图，估算 E。
3. 若 E <= summary_trigger → 调用模型（reason=within_budget），结束。
4. 摘要尝试（最多两轮：常规一轮 + aggressive 一轮）：
   a. 候选保留范围 R = (S.covered_to, 最后一条]；S 为 None 时 R = 全部。
   b. 选定切点：从 R 的最早端按完整轮次剥离，剥离量 = **使保留部分降到
      budget_target 所需的最小量**；若保留部分已经 <= budget_target，剥离量为 0。
      budget_target 常规轮 = recent_message_budget，aggressive 轮 = 它的一半。
      「至少保留最后一轮」优先于 budget_target：若最后一轮本身就大于 budget_target，
      保留它并不再多剥（预算目标是软目标，保留当前轮是硬约束）。
   c. 若剥离量为 0（剥离范围为空）→ 本轮摘要无法收缩视图，跳过（不生成版本），进入 5。
   d. 否则按 §5.2 切块，逐块生成并提交连续摘要版本，完成后重读 S。
5. 用当前 S 重建视图，估算 E。
6. 若 E <= input_budget → 调用模型，结束。
7. 若 aggressive 轮尚未使用 → 回到 4（用减半的 budget_target）。
8. 否则 → 抛 CONTEXT_LIMIT_EXCEEDED（reason=over_budget）。
```

**步骤 4b 的"最小量"是承重细节，不是措辞。** 若实现改成"总是剥到某个固定保留轮数"，那么同一次 invocation 内的第二次组装会继续向前摘——因为 `S` 已推进、`R` 已缩小，但仍会被再次剥离——于是每次决策都生成一个新版本，正是 §4 想避免的版本膨胀。以"保留部分是否已达标"为判据，第二次组装自然得到剥离量 0，性质 2 才成立。

**关键性质（这三条都是可证明的，实现不得省掉）**：

- **`summary_trigger < E <= input_budget` 是合法终态，不得再次触发摘要。** 步骤 3 是唯一触发压缩的入口，步骤 6 是唯一决定能否发送的判据；两者用不同阈值是刻意的。压到极限仍高于触发线，只说明这次请求的固定段很大，不说明需要再压。
- **同一次 invocation 内不会重复生成版本。** 账本在一次 invocation 内是稳定的（用户消息在图调用前已落库，助手回复要到 `final` 才落库，而 `final` 之后本次 invocation 即结束），因此第二次组装时 `R` 与第一次相同；又因步骤 4b 只剥"最小必要量"，第一次摘要后保留部分已 <= budget_target，第二次的剥离量必为 0（4c），不会生成新版本。**这条性质依赖 4b 的"最小量"语义，实现不得改成"总是剥到固定轮数"。**
- **步骤 4c 为空时，aggressive 轮必然也为空。** 空意味着 `R` 只剩最后一轮；把 `budget_target` 减半不改变这个事实。因此实现可以直接跳过步骤 7 进入 8，省掉一次注定无效的模型调用尝试。

**固定段自身超预算时不做任何重试。** 固定段 = 系统协议 + `domain_results` + 当前用户消息。它既不参与摘要（§3 第 3 项优先于第 2 项），也不参与裁剪。固定段单独超 `input_budget` 时，剥离无论如何都不可能把它降下来，算法沿 4c（为空）→ 5 → 8 直接失败，`reason=over_budget`。

**剥离范围为空不等于失败，只等于"这次不压缩"。** 这是上一版 spec 写错的地方：它把"无法摘要"直接当成"无法服务"。真实语义是"没有可摘的更早轮次"，此时唯一正确的动作是去检查硬预算（步骤 5、6）——视图可能完全装得下，那就正常调用模型。

**摘要写在中途不会失效。** 摘要在图调用过程中写入，若该次 invocation 随后失败（工具错误、客户端断开），已写入的版本不需要回滚：它覆盖的是已经结束的真实历史，与这次 invocation 的结局无关。下一个请求读到它仍然正确。

`MAX_DOMAIN_TURNS` 触发的收尾回复不调用模型，因此不组装视图。

### 6.2 并发

沿用持久化 spec 的 conversation lock：`ConversationTurns` 在整回合（含图调用）持有它，因此同一 conversation 不会并发生成两个摘要版本，也不会两个请求同时写 `context_summary`。`(conversation_id, version)` 唯一约束是最后一道防线。摘要写入是独立事务，与 checkpoint 无关，不参与任何跨存储提交。

### 6.3 调用位置与依赖注入

组装器是一个可调用对象，由组合根在每次 invocation 时构造并注入 `build_supervisor`。要求：

- 组装器**同步**执行，与图节点一致（该节点本来就同步调用模型 HTTP）。
- 组装器通过注入的 `record(event_type, payload)` 回调上报 run 事件，`app/context` 不直接依赖 `app.audit`，便于单测替换。
- 组装器不缓存跨 invocation 的结果；同一 invocation 内的多次调用会重新读账本（§6.1 已证明这不会重复生成版本）。
- 组装器对测试图**零影响**：`test_agent_runtime.py` 的 `goal_graph` 等自带节点的图不经 `build_supervisor`，因此注入式设计让它们完全不受本 spec 影响。不要把它做成全局单例或模块级状态。

**两处接线改动是必需的，不能只靠"组合根注入"一句话带过**：

1. **`TurnContext` 需要新增 `sequence: int` 字段。** §5.3 规则 3 与 INV-1 都要求"当前用户消息的 sequence"，而 `TurnContext` 现有的字段是 `conversation_id / thread_id / message_id / content / run_id / resume / question_id`——**没有 `sequence`**。`Receipt` 里有它，但 `turns.py` 构造 `TurnContext` 时（`ConversationTurns.stream`）没有传。实现时必须从 `plan.receipt.sequence` 填充。这是本 spec 唯一的既有结构改动，且是纯增量字段。

2. **`AgentRuntime._graph_for(context)` 需要能拿到 `turn`。** 它现在只接收 `TrustedContext`（`_graph_for` / `graph_factory` 的签名都是 `(model, context)`），而 `conversation_id` 在 `turn` 里；`turn` 要到 `invoke` 内部才被使用。因此组装器不能只靠 `TrustedContext` 构造。可选的落法有两种，都不改变 `TrustedContext` 的既有语义：把 `turn` 作为参数传给 `_graph_for`（并让 `graph_factory` 多接一个参数），或在 `invoke` 内部构造组装器并把它绑进图工厂。**选哪种由实现决定，但 spec 要求组装器能同时看到 `turn.conversation_id` 与 `turn.sequence`。**

## 7. 摘要内容要求

摘要只保留后续对话需要的事实：用户目标、已确认偏好、已完成动作及业务 ID、未完成事项、已回答问题和约束。不得把不存在于源消息中的事实写入摘要；敏感字段遵循消息和工具数据边界。

- 摘要 prompt 的输入 = 上一版摘要文本（v1 时为空）+ 本块消息 + 输出格式要求。**不含 `user_id`、`conversation_id`、`bank_customer_id` 或任何调用方身份**：摘要只描述对话内容，把身份写进去等于把身份复制进一份长期保留、会被反复读入上下文的派生文本。
- **本块消息必须经过与视图相同的 §3 读取规则**，尤其是陈旧答案过滤。摘要输入若含一条被取代的 `interrupt_answer`，该答案会被写进 `summary_text` 并在后续每次组装中回到模型眼前，等于绕过 §3 的过滤。同理，摘要只读本 conversation 的消息。
- **摘要输出必须是一个 JSON 对象**，形如 `{"summary": "..."}`。这不是风格偏好，而是被 `llm_response_format` 配置逼出来的：该配置为 `json_object` 时，adapter 会给**每一次** `complete()` 带上 `response_format={"type":"json_object"}`（`ChatCompletionsModelClient._response_format`），摘要调用也不例外。要求纯文本摘要会在该模式下被 provider 拒绝或返回 JSON 包裹的结果。统一要求 JSON 对象后，两种模式都能用现有的 `extract_json_object` 解析。
- 摘要调用直接使用 `model.complete(({system: SUMMARY_PROMPT}, {user: ...}))`，**不复用** `_model_text`——后者会附加"请按要求只输出一个 JSON 对象"的 Supervisor 指令，那是决策协议的措辞，不是摘要的。
- 摘要输出长度 ≤ `summary_max_tokens`，用估算器检查。**超长按生成失败处理**，不截断使用——一个自己撑爆预算的摘要会让不变量无法维持。是否同时向 provider 传 `max_tokens` 由实现决定，不传不影响正确性。
- 工具私有 `facts` 不进入摘要；只允许其公开 `summary` 或必要业务 ID 进入模型上下文。
- 摘要 prompt/response 自身进入 `llm_call_audit`，但它**不作为用户可见 assistant message，也不改变消息 sequence**。
- 摘要生成失败时不删除旧摘要，也不写新版本；调用方按 §8 处理。

## 8. 失败、回退与可观测性

### 8.1 失败路径

| 情形 | 行为 |
|---|---|
| 摘要模型调用失败（超时/限流/鉴权） | 不写新版本，保留旧版本；继续走 §6.1 步骤 5、6 的硬预算判定（可能成功，也可能以 `reason=summary_failed` 失败） |
| 摘要输出超长、非 JSON 或缺 `summary` 字段 | 同"调用失败"，记 run 事件 |
| 版本链校验不通过 | 按无摘要处理（`S = None`）并全量重建（§5.2）；不删除旧版本 |
| 剥离范围为空（R 只剩最后一轮，没有更早轮次可摘） | **跳过摘要，不重试**；继续检查硬预算（§6.1 步骤 5、6）——视图可能完全装得下 |
| 组装后仍超 `input_budget` | 一次 aggressive 轮（`budget_target` 减半）；若 4c 已判定为空则直接跳过 |
| 固定段自身超 `input_budget` | **不重试**，直接失败 |
| aggressive 轮后仍超 | `CONTEXT_LIMIT_EXCEEDED`（`reason=over_budget`） |
| provider 明确返回上下文超限 | 归为 `LLM_CONTEXT_OVERFLOW`，走同一次 aggressive 轮；第二次仍超限则同上 |

**摘要调用必须自己捕获异常，不能依赖上游重试助手。** §7 规定摘要不复用 `_model_text`（措辞不同），因此它也**不经过 `call_with_one_retry`**——而 `call_with_one_retry`（`app/agents/upstream.py`）正是把 `ModelClientError` 转成稳定 `AppError` 的地方。绕过它意味着适配器会抛出**原始** `ModelClientError`；若不显式捕获，它会穿出 `supervisor_node`，整个 run 以 `_failure_code` 记下的类名失败，而本 spec 承诺的是"保留旧版本继续"。

因此组装器必须捕获 `ModelClientError`（以及 `AppError`）并将其降级为 `summary_failed` 路径：不写新版本，继续走硬预算判定。**是否对摘要调用做一次上游重试由实现决定**，两种都符合本 spec——不重试更简单且失败可见，重试与既有上游策略一致；但无论选哪种，异常都不得穿出组装器。

**`CONTEXT_LIMIT_EXCEEDED` 的 `retryable` 由原因决定，不是一个常量。** 同一个码有两种成因，可重试性相反：

- `details.reason = "summary_failed"`：摘要调用失败是瞬时的（限流、超时），同一请求稍后重试可能成功 → `retryable=True`。
- `details.reason = "over_budget"`：给定同一账本与同一条消息，结果确定 → `retryable=False`，重试只会得到同一个错误。

因此这个码**不能**直接走 `conversation_error()` 的固定表（该函数把 `retryable` 硬编码为"仅 `PERSISTENCE_FAILED` 为真"），需要一个接受 `reason` 并据此决定 `retryable` 的构造函数。前端只把 `retryable` 当可选字段声明、不分支使用，因此这不构成前端契约变更。

**`CONTEXT_ASSEMBLED` 的写入失败按审计 spec 处理，不得降级。** 审计 spec §3 规定审计写入失败时 run 必须失败，不能悄悄变成"只有日志知道"。该事件在模型调用**之前**写入，因此写失败会终止整回合——这是刻意的，与 `RUN_STARTED` 同属"先持久化、再执行"的既有规则。

provider 超限的识别是 **best-effort**：按 HTTP 400 且错误体中的 code/type/message 含上下文长度标记来判断。识别不到时不会更糟——它表现为一次普通的上游失败，而自有估算守卫已经拦住了绝大多数情况。这条限制必须写进验收，避免日后被当成"已完全覆盖"。

### 8.2 可观测性

新增 run 事件 `CONTEXT_ASSEMBLED`（`RunEventType` 是封闭枚举，新增一项即扩展审计契约，审计 spec §2.2 的事件清单需同步）。每次组装追加一条，`payload_json` 至少包含：

```json
{
  "estimated_tokens": 41230,
  "input_budget": 121856,
  "summary_trigger": 85300,
  "estimator": "heuristic_v1",
  "summary_id": "sum_...",
  "summary_version": 3,
  "covered_from_sequence": 43,
  "covered_to_sequence": 88,
  "retained_from_sequence": 89,
  "retained_to_sequence": 104,
  "reason": "summarized",
  "summary_call_made": true,
  "aggressive": false
}
```

`reason` 取值：`within_budget`、`summarized`、`aggressive_crop`、`no_valid_summary`、`summary_failed`、`over_budget`、`no_conversation`。`agent_id` 为空（该事件属于会话而非某个领域 Agent）。`no_conversation` 是 session 路径（`turn=None`）的取值：该路径不做摘要（INV-7），但仍上报组装结果，使退化路径同样可观测。

**摘要调用必须能在审计里与 Supervisor 决策调用区分。** 两者的 `agent_id` 都是空，只靠 prompt 形状区分太弱。因此新增审计来源常量 `SOURCE_CONTEXT_SUMMARY`（`app/audit/context.py`），摘要模型调用套一层 `audit_context(current.merged(source=SOURCE_CONTEXT_SUMMARY))`，由组合根通过注入的 `summary_scope` 上下文管理器提供——这样 `app/context` 仍然不 import `app.audit`。`llm_call_audit.source` 因此能直接筛出摘要调用，而 `agent_run_event.source` 仍是 `conversation`（`CONTEXT_ASSEMBLED` 描述的是会话的组装动作，不是摘要那次模型调用）。

## 9. 与 Agent 状态和暂停恢复的关系

- checkpoint 中的待答问题、已收集输入和 domain results 优先于旧摘要；不在数据库另存一份运行状态。
- 暂停时保存摘要版本和覆盖范围；恢复时把答案追加为新 user message，再向同一 conversation checkpoint thread 发送 resume。新答案因此天然落在"最近消息"范围内，Supervisor 能看到原文。新的图调用使用新的 `run_id` 关联审计记录。
- 摘要不进入 checkpoint，不改变消息 sequence，不产生可见 assistant message。
- 同一 conversation 内的 `new_goal` reset 不属于本 Spec（持久化 spec §5 已以 `NEW_CONVERSATION_REQUIRED` 拒绝）。需要留意 `goal_reset_state()` 会用 `Overwrite([])` 清空 checkpoint transcript：**在方案 A 下这不再清空 Supervisor 看到的对话历史**（历史来自账本）。当前 `new_goal` 只在无 conversation 的 session 路径可达，而该路径退回 transcript，因此实际行为与今天一致；一旦将来给 conversation 路径开 `new_goal`，必须重新审视这条，因为届时"重置目标"将不再重置模型看到的历史。
- 工具私有 facts 不进入摘要；只允许其公开 summary 或必要业务 ID 进入模型上下文。

## 10. 不变量（自审清单）

| 编号 | 不变量 | 违反后果 |
|---|---|---|
| INV-1 | `covered_to_sequence < 当前用户消息 sequence` | 当前消息被摘要掉，模型看不到用户刚说的话 |
| INV-2 | 版本链连续：v1.from=1，v_n.from = v_{n-1}.to + 1 | 历史出现空洞，摘要"记得"不存在的事或忘记存在的事 |
| INV-3 | 保留范围的**读取区间** sequence 连续无洞，且含当前用户消息；渲染集合允许因 §3 的陈旧答案过滤而缺少个别行 | 视图与账本读取不一致，模型推理依据缺失 |
| INV-4 | `conversation_message` 只增不改，本 spec 不写该表 | 违反持久化 spec 的账本不可变约定 |
| INV-5 | 摘要不写 checkpoint、不改 sequence、不产生可见消息 | 摘要变成第二套执行状态 |
| INV-6 | 每次摘要调用在 `llm_call_audit` 中可查 | 模型调用漏审，违反审计 spec |
| INV-7 | 无 conversation 时不做摘要 | 摘要挂靠到不存在或错误的容器 |
| INV-8 | 组装后 `estimated_tokens <= input_budget` 才调用模型 | 请求超限失败，且失败原因不可解释 |
| INV-9 | 摘要文本 ≤ `summary_max_tokens` | 摘要自身撑爆预算，压缩失效 |
| INV-10 | 裁剪只影响 prompt；账本与审计不留洞 | 静默丢消息 |
| INV-11 | 触发摘要时必须同时满足"剥离可行"（§6.1 步骤 4c）；启动校验 `recent_message_budget < summary_trigger` 是必要非充分条件 | 无法收缩时反复触发，版本膨胀且视图并未变小 |
| INV-12 | 每次摘要调用的输入 ≤ `summary_max_tokens + chunk_budget + 固定模板`（单轮超 `chunk_budget` 的例外见 §5.2） | 摘要调用自己超限，压缩机制在长会话上失效 |
| INV-13 | 摘要 prompt 不含任何调用方身份 | 身份被复制进长期保留、反复读入上下文的派生文本 |
| INV-14 | 视图对每个 `question_id` 只含最后一条 `interrupt_answer` | 陈旧答案被重新喂给模型，作废持久化 spec §2.4 的保证 |
| INV-15 | 摘要调用抛出的 `ModelClientError` / `AppError` 不穿出组装器 | run 以类名失败，而 spec 承诺保留旧版本继续 |

## 11. 已知取舍与非目标

- **checkpoint 的 `messages` 仍无限增长。** 这是方案 A 的直接代价：`state["messages"]` 只增不减，长会话的内存与 checkpoint 存储成本随会话线性增长。本 spec 只保证**模型输入**不超限，不保证**checkpoint 大小**有界。后续若要收敛，需要方案 B（摘要写回 checkpoint 并裁剪旧轮次），届时必须重新处理 `add_messages` 的 id 合并、resume 重放和子图 namespace——本 spec 的 `context_summary` 版本链和边界规则可以原样复用。
- **累积摘要是对全部历史的逐轮有损重压缩，信息会随会话增长而衰减。** `summary_text` 覆盖 1..`covered_to`（§5.2），却恒受 `summary_max_tokens = 1024` 约束（§7），于是每一版都在压缩"上一版的压缩结果"：会话越长，同样 1024 token 要承载的历史越多，早期事实（尤其业务 ID、用户明确说过的约束）越容易被稀释掉，且这种衰减**不可逆、不可检测**——`source_hash` 只证明范围正确，不证明内容完整。INV-12 的输入上界**依赖**这个上限成立，所以它不是可忽略的实现细节，而是该不变量成立的前提。若需要长期保真，需要分层/多级摘要或原文检索，均属本 spec 非目标。
- **同步摘要增加该回合的延迟**（一次额外模型调用），已接受。
- **字符估算不精确**，靠 `safety_margin` 与 `summary_trigger_ratio` 的余量兜底；偏乐观时表现为提前触发摘要而非超限。
- **provider 超限识别是 best-effort**（§8.1）。
- **摘要质量无自动评估**，不设回归基线；`source_hash` 只保证范围正确，不保证内容正确。
- 不引入向量库和语义检索（后续扩展：可从 `conversation_message` 和 `context_summary` 建独立索引，检索结果作为视图的附加证据，不修改账本，不改变本 spec 的 ID 与审计关联）。
- 不做摘要的跨会话复用、不做多 worker 下的分布式摘要锁。

## 12. 与已冻结 spec 的一致性

本 spec 遵守持久化 spec 的全部约束：账本不可变、执行状态只来自 checkpoint、不建运行状态投影、`run_id` 只作关联、软删除不级联删除。

**唯一需要改动"已冻结"文档的地方**是持久化 spec：其 §4 写"错误码固定为七个"，新增 `CONTEXT_LIMIT_EXCEEDED` 后是八个，该句与 §0 的冻结说明已同步更新（见其 §0.1 修订记录）。`app/core/errors.py` 的三张表（枚举、文案、状态码）各加一项：

```text
CONTEXT_LIMIT_EXCEEDED  →  409  "对话内容超出模型上下文限制，本次请求未执行"
retryable 由 details.reason 决定：summary_failed → true；over_budget → false
```

其余相邻改动都落在未冻结的文档与模块上：

- 审计 spec（状态为设计草案）§2.2 的 `agent_run_event` 事件清单加入 `CONTEXT_ASSEMBLED`（已改）。
- `app/audit/context.py` 新增来源常量 `SOURCE_CONTEXT_SUMMARY`（§8.2）。
- `app/core/errors.py` 的 `conversation_error()` 需要一条接受 `reason` 的构造路径，因为该码的 `retryable` 不是常量（§8.1）。

## 13. 验收标准

- 长会话触发摘要后，原始消息仍可完整分页读取；`conversation_message` 无删除、无更新。
- 每次模型调用均在预算内组装，且 `CONTEXT_ASSEMBLED` 事件能说明摘要版本、覆盖范围和保留范围。
- 摘要生成失败不破坏旧摘要、不写新版本、不影响 checkpoint。
- 同一 conversation 暂停/恢复不丢失当前问题或答案；图状态只从 checkpoint 读取，不建立数据库状态投影。
- 超限错误可识别、可重试，且不会静默丢弃消息。
- INV-1 可断言：构造一条摘要后追加新用户消息，视图必须含该消息原文。
- INV-2 可断言：连续三次摘要后，版本链的 `covered_from/to` 无缝衔接且无重叠。
- INV-3 可断言：视图保留范围的 sequence 连续，且覆盖当前消息。
- INV-4 可断言：**摘要生成动作本身不写账本**；触发摘要的回合，账本增量恰为该回合自身的 user/assistant 两条，且既有行的内容哈希不变。（不能写成"行数与哈希完全不变"——该回合自身必然新增两条，那样写按字面永远失败。）
- INV-6 可断言：一次摘要生成对应一条 `llm_call_audit`，且不产生新的 `conversation_message`。
- INV-7 可断言：session 路径（`turn=None`）不写 `context_summary`。
- INV-9 可断言：构造一个试图输出超长摘要的模型替身，不写入版本且不影响旧版本。
- INV-11 可断言：把 `recent_message_budget` 配成大于触发线时，进程启动即失败；**并**断言"压到极限仍高于触发线"的请求只调用一次模型、不生成第二个版本（§6.1 的关键性质）。
- INV-12 可断言：构造历史很长的会话，摘要调用链每块的输入估算都不超过 `summary_max_tokens + chunk_budget + 固定模板`；且在此场景下不出现"摘要调用自己超限"。
- INV-13 可断言：摘要调用的 prompt 文本中不出现 `user_id` / `conversation_id` / `bank_customer_id`。
- INV-14 可断言：同一 `question_id` 在账本中有两条 `interrupt_answer` 时，视图只含 sequence 较大的那条；被过滤的那条仍在账本中可读。
- INV-15 可断言：让摘要模型抛 `ModelClientError`，回合不因此以类名失败，且旧摘要版本不变。
- 分块前缀可断言：第 3 块摘要失败时，前 2 块已提交且版本链仍连续可用。
- 全量重建可断言：`S = None`（首次摘要或链校验失败）时，从 sequence 1 起按 §5.2 分块，产出的 v1 满足 `covered_from_sequence == 1`。
- 空剥离范围可断言：`R` 只剩最后一轮且视图 `<= input_budget` 时，正常调用模型、不生成版本、不抛错（§6.1 的关键性质）。
- 摘要调用可区分：一次摘要生成的 `llm_call_audit.source` 为 `context_summary`，同一回合的 Supervisor 决策调用为 `conversation`。
- `CONTEXT_LIMIT_EXCEEDED` 的两种原因可区分：`summary_failed` 时 `retryable=true`，`over_budget` 时 `retryable=false`。
- 领域 Agent 的 prompt 不含 transcript，其规模只由本次 invocation 的短内容界定。
- 摘要调用不改变消息 sequence：触发摘要的回合只增加该回合自身的 user/assistant 两条消息。

## 14. 实现与验证

代码冻结点 `58a513a`（分支 `dev`，Task 9 之后；Task 10 只改文档，不动代码）。迁移
`0009_context_summary`；估算器 `app/context/estimator.py`；预算与轮次
`app/context/budget.py`；摘要与版本链 `app/context/summary.py`；组装与上报
`app/context/view.py`；接线 `app/agents/runtime.py`、`app/conversation/turns.py`、
`app/agents/supervisor.py`。

实际落点（`docs/ARCHITECTURE.md:259` 的过时描述已改写，实现后不再称"未做 transcript 裁剪或摘要"）：

```text
backend/app/context/estimator.py         启发式估算器（heuristic_v1）
backend/app/context/budget.py            input_budget / 触发线 / 轮次边界、最小剥离量与分块
backend/app/context/summary.py           摘要生成、分块版本链写入与校验
backend/app/context/view.py              context view 组装（含陈旧答案过滤）与 CONTEXT_ASSEMBLED 上报
backend/app/storage/models.py            ContextSummary
backend/app/storage/migrations/versions/0009_context_summary.py
backend/app/agents/runtime.py            TurnContext 加 sequence；组装器接线（§6.3 的两处改动）
backend/app/conversation/turns.py        构造 TurnContext 时传 plan.receipt.sequence
backend/app/agents/supervisor.py         supervisor_node 改用注入的组装器
backend/app/integration/model_client.py  LLM_CONTEXT_OVERFLOW 分类
backend/app/core/config.py               六个预算配置项 + 启动校验
backend/app/core/errors.py               CONTEXT_LIMIT_EXCEEDED 与按 reason 的 retryable
backend/app/audit/service.py             RunEventType.CONTEXT_ASSEMBLED
backend/app/audit/context.py             SOURCE_CONTEXT_SUMMARY
docs/ARCHITECTURE.md                     第 259 行"当前代码未做 transcript 裁剪或摘要"已改写为"模型输入有界，checkpoint 大小仍然无界"
```

验证命令与结果（`backend/` 下；时间戳 2026-10-02，本机实测）：

```text
cd backend
uv run pytest -q                               # 3 failed, 745 passed, 2 skipped, 42 errors in 3678.71s
uv run ruff check app tests scripts            # All checks passed!
uv run ruff format --check app tests scripts   # 145 files already formatted

cd frontend
npm test                                       # 102 passed (5 files)
npm run build                                  # vue-tsc clean, built in 1.33s
```

`uv run pytest -q` 的这一轮**不是全绿**，如实记录：45 个非通过项全部是云端测试库的连接抖动，不是断言失败。3 个 `FAILED` 的 traceback 是
`psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly` 抛在测试体内（`select pg_catalog.version()` 与一条 `SELECT ... FOR UPDATE`），
42 个 `ERROR` 全部发生在 fixture setup/teardown，报错文本只有
`psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly`、`psycopg.errors.ConnectionTimeout: connection timeout expired` 与 SQLAlchemy 的包装。
按名字重跑这 45 个 node id：3 个 `FAILED` → **`3 passed in 26.19s`**；42 个 `ERROR` → 分三批重跑 **`17 passed in 266.06s`**、**`18 passed in 216.29s`**、**`7 passed in 63.50s`**，合计 42 通过、0 未通过。
（`tests/` 共 792 项 = 745 + 3 + 42 + 2 skipped，与 `--collect-only -q` 的 `792 tests collected` 一致。）同一环境此前 Task 8 的全量跑给出 `733 passed, 2 skipped, 39 errors`，同样零断言失败。

变异实验（改一行生产代码 → 对应用例变红 → `git checkout -- <path>` 还原 → 重跑确认 PASS；完整表格见 `docs/superpowers/plans/2026-09-28-context-management-plan.md` 末尾的"Task 10 变异实验结果"）：

- **摘要范围覆盖当前消息（INV-1）**：`app/context/summary.py:153` 的 `if newest.covered_to_sequence >= current_sequence: return None` 改成 `if False and ...` →
  `tests/test_context_summary.py::test_a_summary_covering_the_current_message_is_not_valid` 变红，断言为 `test_context_summary.py:481: assert Summary(summary_id='sum_eb3cee...', version=1, covered_from_sequence=1, covered_to_sequence=2, ...) is None`。还原后 `21 passed`。
- **版本链断链（INV-2）**：`app/context/summary.py:216-217` 的版本递增校验改成 `if False and ...` → `tests/test_context_summary.py::test_a_version_number_gap_in_the_chain_is_not_valid` 变红（`:591 assert Summary(summary_id='sum_jumped', version=3, ...) is None`）；
  `app/context/summary.py:212-213` 的 `if parent is None: raise _ChainBroken` 改成 `break` → `test_a_dangling_base_link_is_not_valid` 变红（`:625 assert Summary(summary_id='sum_orphan', version=1, ...) is None`）。还原后各 `1 passed`。
- **剥离范围不切块（INV-12）**：`app/context/summary.py:179` 的 `for chunk in chunk_for_summary(strip, chunk_budget):` 改成 `for chunk in [list(strip)]:` →
  `tests/test_context_summary.py::test_a_long_strip_is_summarized_in_consecutive_chunks` 变红（`:154 assert [(1, 1, 4)] == [(1, 1, 2), (2, 3, 4)]`）**且** `tests/test_context_acceptance.py::test_inv12_every_summary_call_stays_within_its_own_budget` 变红（`:294 assert 1255 <= ((100 + 500) + 200)`）。还原后 `2 passed`。
- **剥离量改成固定轮数（§6.1 性质 2）**：`app/context/budget.py:168` 的 `max_k = min(len(rounds) - 1, _leading_closed_rounds(rounds))` 改成 `min(len(rounds), _leading_closed_rounds(rounds))` →
  `tests/test_context_budget.py::test_the_strip_is_the_minimal_number_of_whole_rounds`（`:92 AssertionError: only the first round is stripped`，`assert [1, 2, 3, 4] == [1, 2]`）与
  `::test_the_last_round_is_never_stripped_even_when_it_alone_exceeds_the_target`（`:100`）变红，另外 `tests/test_context_view.py` 三个用例因摘要桩耗尽而红（`:63 AssertionError: the script ran out of summaries`）。还原后 `28 passed`。
  同一条"固定轮数"写成 `max_k = 2` 的等价形式**不等价**：它会额外让 `test_an_unclosed_round_is_never_stripped` 变红，并让 `app/context/budget.py:177` 抛 `IndexError: list index out of range`——那是变异自身的越界，不是被测性质。
- **陈旧答案不过滤（INV-14）**：`app/context/ledger.py:66` 的 `return [row for row in messages if not superseded(row)]` 改成 `return list(messages)` →
  `tests/test_context_ledger.py` 三个用例与 `tests/test_context_acceptance.py::test_inv14_the_model_never_sees_a_superseded_answer` 变红；
  后者的断言是 `test_context_acceptance.py:340: AssertionError: assert '陈旧的补充' not in '你是银行智能体的总调度...'`。还原后 `9 passed`。

本轮另补了两条探针，结论写入计划末尾，也在此登记：

- **INV-1 的守卫只有 `test_context_summary.py:481` 一个见证。** `test_context_acceptance.py::test_inv1_a_message_added_after_a_summary_is_in_the_view`（断言 `"独一无二的新消息" in model.decision_prompts[-1]`）**不钉住** §14 要证明的"摘要范围覆盖当前消息"：直接放行 `summary.py:153` 后它仍全绿，叠加轮次边界变异（`budget.py:146` 未闭合轮改成 `closed=True` 再加 `budget.py:168` 的 `min(len(rounds), ...)`）后它依然全绿——该用例只证明"新消息出现在视图里"，而这件事由摘要写入路径保证。
- **`budget.py:174-178` 的提前返回不是无操作。** 只删这一处（`if False and retained <= budget_target:`）会让激进剥离的停止点改变，`tests/test_context_view.py::test_an_aggressive_pass_keeps_fewer_rounds_than_a_normal_one`（`:286 assert 21 > 21`）与 `::test_a_fitting_aggressive_pass_reports_the_crop_and_only_once`（`:348 assert 'summarized' == 'aggressive_crop'`）变红。

**已核实的实现风险（换数据源会改变既有断言成立的理由）**：

- `test_conversation_api.py` 的跨会话隔离断言（`"甲的问题" not in model.prompts[1]`）与同会话复用断言（`"第一轮" in model.prompts[1]`）走的是 HTTP conversation 路径，换源后仍应成立（账本本就是按 conversation 隔离且追加的），但**成立的理由从"checkpoint transcript 隔离"变成"账本按 conversation 过滤"**。这类断言应当在实现时补一句注释说明它现在证明什么，否则日后有人改坏账本读取也看不出是哪个不变量被破坏。
  **更正（Task 12，变异实测）：这条预测对这两条用例不成立。** `test_conversation_api.py` 装的是 `tests/test_multi_agent_api.py:57` 的 `agent_for(...)`，其 `:61` 不传 `assembler_factory`，故 `app/agents/runtime.py:177-181` 算出 `assembler = None`、`app/agents/supervisor.py:214` 得到 `view is None`，`prompt_for` 便在 `app/agents/supervisor.py:177` 走 `describe_transcript(message_dicts(state.get("messages")))`——**transcript 仍来自 checkpoint，与账本无关**。实测：在 `app/context/ledger.py` 的 `read_ledger` 函数体首行插入 `raise AssertionError("PROBE: read_ledger was called")` 后，这两条用例仍 `2 passed`（`read_ledger` **一次都没被调用**），而同一变异下 `tests/test_conversation_audit.py` 变 `9 failed`（探针有效）。因此"成立的理由变了"**没有发生在这个文件上**：跨会话隔离成立的理由是**两条 conversation 各有自己的 `thread_id`**（checkpoint 命名空间分开），同会话复用成立的理由是**同一 thread 的 checkpoint 累积**（`app/agents/state.py:164` 的 `{"messages": [turn] if message else []}` 由 LangGraph 的 message reducer 按 id 合并）。两处注释已由 Task 12 按真实机制重写（见下）。账本读取路径的实际覆盖是 `tests/test_conversation_audit.py` 与 `tests/test_context_integration.py` / `tests/test_context_acceptance.py`（`wired_agent` 手工把 `_assembler_factory` 接到 `rt.build_context_assembler`，见 `tests/test_context_integration.py:33`）——**`test_conversation_api.py` 全文件不覆盖组装器**。
- `test_cross_domain_orchestration.py` 的 `STALE not in model.prompts[-1]`（goal reset 后旧消息不再出现）走的是 **session 路径**（`invoke` 不带 `turn`），该路径按 §2 退回 transcript，因此断言与今天一致。**这条断言在 conversation 路径上会失效**——方案 A 下历史来自账本，`goal_reset_state()` 清空 transcript 不再清空模型看到的历史。这正是 §9 记录的那条待重新审视项，实现时不得把该测试改成 conversation 路径来"顺手统一"。
- `test_agent_runtime.py` 的 `goal_graph` 是自带节点的测试图，不经 `build_supervisor`，因此只要组装器是注入 `build_supervisor` 的（§6.3），它完全不受影响。这是注入式设计的一个额外收益，实现时不要改成全局单例。

实现后的复核结果：

- 第 2、3 条成立：`backend/tests/test_cross_domain_orchestration.py:170,173,248,251` 仍全部走 `runtime.invoke(SESSION, USER, ...)`（session 路径），`:206`/`:222` 的 `STALE not in model.prompts[-1]` 原样保留；`backend/tests/test_agent_runtime.py:261` 的 `goal_graph` 仍是自带节点的测试图，签名只被机械地放宽为 `goal_graph(model, context, assembler=None, turn=None)`，从不调用 `build_supervisor`。
- 第 1 条**原预测不成立；两处注释已由 Task 12 按真实机制重写**。Task 11 曾据上面的预测把注释写成"`read_ledger` 按 `conversation_id` 过滤才是隔离成立的理由"，该说法经变异实测被证伪——`read_ledger` 在这两条用例里从未被调用（见本条第 1 项的更正）。现在的注释说明：跨会话隔离来自两条 conversation 各自的 `thread_id`，同会话复用来自同一 thread 的 checkpoint 累积，且该文件不装组装器、因此不是账本读取路径的证据。Task 11 的提交 `docs(context): explain what the ledger-backed isolation assertions prove`（`7ad71d9`）留下的错误说法由 Task 12 的提交 `docs(context): correct what the isolation assertions actually prove` 更正。

### 14.1 整分支终审与其缺口处置（Task 13）

整分支终审（`.superpowers/sdd/2026-09-28-context-management-plan/final-review.md`）裁决为 **`With fixes`：0 Critical / 3 Important / 6 Minor**。三个 Important 都不是产品行为缺陷，而是"§13 点名的验收条款没有被真正钉住"；Task 13 逐条复核后补齐，**不改任何生产代码**（提交 `test(context): close the witness gaps the final review named`）。

本轮修掉的六处：

- **INV-4 的"既有行内容哈希不变"半边（Important 1）**：`tests/test_context_acceptance.py` 新增模块级 `row_fingerprint(rt, conversation_id)`（按 `(sequence, role, message_kind, content)` 取指纹），`test_inv4_a_summarizing_turn_adds_exactly_its_own_two_rows` 在原有行数断言之外加 `assert before_fingerprint == [row for row in after_fingerprint if row in before_fingerprint]`。原断言只有 `message_count(...) == before_rows + 2 * turns`，它区分不了"摘要器什么都没追加"与"摘要器就地改写了一行"。**变异实测**：临时在 `app/context/summary.py` 的 `_commit` 里就地改第一条账本行的 `content` 后，行数断言仍通过而新断言变红（`test_context_acceptance.py:245: AssertionError: every row that was already there is still there, with the same content`，`At index 0 diff: (1, 'user', 'message', ...)`），已用 `git restore` 还原。
- **INV-12 量错了对象（Important 2）**：原断言只量 `RoutingModel.summary_prompts`（`messages[-1]` 一条），而上界 `TIGHT.summary_max_tokens + TIGHT.chunk_budget + 200` 里的 `200` 是魔数——它在替一个**完全在测量之外**的系统 prompt 兜底。现改为：`tests/support/scripted_model.py` 的 `RoutingModel` 新增 `summary_messages: list[tuple[dict, ...]]`（记整次调用，`summary_prompts` 语义不变），断言对整次调用求和，上界每一项都量出来或取自 `TIGHT`：`estimate_message_tokens(SUMMARY_PROMPT)` = **269**、`estimate_message_tokens(render(SUMMARY_USER_TEMPLATE, previous="", conversation=""))` = **35**、`max(TIGHT.summary_max_tokens, estimate_tokens(NO_PREVIOUS_SUMMARY))` = **100**、`chunk_allowance = max(estimate_tokens(_describe_ledger(chunk)) for chunk in chunk_for_summary(rows, TIGHT.chunk_budget))` = **607**，合计 **1011**；TIGHT 下最坏一次真实调用实测 **921**（`input_budget = 4000`），默认预算下约 34k（`input_budget = 121856`）。所以这条上界是**回归护栏**，不是"生产曾经接近过极限"。上界成立的依据是分块从头贪心（`app/context/budget.py:192-205`），故任何前缀的分块都只能落在账本自身分块已有的块上——**读码结论，未做变异验证**。**变异实测**：把 `app/context/summary.py` 的 `for chunk in chunk_for_summary(strip, chunk_budget):` 改成 `for chunk in [list(strip)]:`（不切块）后该用例变红（`test_context_acceptance.py:384: AssertionError: a summary call grew past its own budget: 1528 > 1011`），已还原。注意**反方向的变异不被这条用例捕获**：把 `app/context/budget.py:192` 的 `chunk_for_summary` 改成直接 `return [list(messages)]`（分块失效）时，`chunk_allowance` 会随输入一起变大、上界同步放宽，该用例仍 `1 passed`；这个变异由 `tests/test_context_summary.py:154 test_a_long_strip_is_summarized_in_consecutive_chunks` 捕获（`assert [(1, 1, 4)] == [(1, 1, 2), (2, 3, 4)]`）。
- **INV-6 的审计查询没有会话范围（Important 3）**：`tests/test_context_acceptance.py` 的 `test_inv6_a_summary_call_is_audited_and_adds_no_visible_message` 与 `tests/test_context_integration.py` 的 `test_a_summary_call_is_audited_under_its_own_source` 都查 `llm_call_audit` 全表，而该表跨用例共享——任何更早的用例留下的 `source == "context_summary"` 行都能让断言永远绿。两处都加上 `.where(LlmCallAudit.conversation_id == created["conversation_id"])`。**变异实测**：把 `app/audit/context.py:108` 的 `summary_context()` 改成 `carries = AuditContext(source=SOURCE_CONTEXT_SUMMARY)`（丢掉会话归属）后，两条用例都变红（`test_context_acceptance.py:269: AssertionError: the summary call is auditable under its own conversation`；`test_context_integration.py:116: AssertionError: assert 'context_summary' in {'conversation'}`），已还原。
- **领域 prompt 见证结构性不可证伪（Minor 5）**：原用例先用 `"三种输出之一"`（`DOMAIN_PROMPT` 的标记）选出领域 prompt，再断言其中不含 `"历史摘要（覆盖较早的对话"`（`SUPERVISOR_PROMPT` 的表头）——两个字符串分属两个模板，任何 prompt 都不可能同时满足选择器与反例。现改为正面断言：按 `DOMAIN_PROMPT.split("{", 1)[0]` 的固定前缀过滤 `model.prompts`，**先断言过滤结果非空**（选择器退化时不再静默变绿），再逐条断言其中没有 `user: `/`assistant: ` 前缀的行、没有摘要表头、没有摘要正文。原用例仍在证明它该证明的事：领域 Agent 不需要压缩，是因为它从一开始就拿不到 transcript。**变异实测**：在 `app/agents/prompts.py` 的 `DOMAIN_PROMPT` 里插入一行 `user: LEAKED TRANSCRIPT LINE` 后该用例变红（`test_context_acceptance.py:517: AssertionError: no transcript line reaches a domain prompt`；该断言与消息行位于 `:517-519`），已还原。
- **INV-13 的 `None` 安全（Minor 7）**：`app/identity/service.py:57` 返回 `str | None`，原写法 `assert rt.identity.bank_customer_id(session["user_id"]) not in prompt` 在绑定缺失时抛 `TypeError` 而不是断言失败；现先绑定局部变量并 `assert customer is not None`，再断言 `customer not in prompt`。
- **INV-9/INV-15 的 docstring 过头（Minor 8）**：两条用例的会话本来就没有旧版本，"什么都没写"与"旧版本还在"是同一次观察。按 brief 推荐**收窄 docstring**，并各自明确指出"旧版本保留"由 `tests/test_context_summary.py:243 test_a_failure_with_a_base_keeps_the_base` 负责（先 `generate` 出 `base`，再让摘要失败，断言 base 仍是 newest）。断言体未动。

本轮**接受并记录、不改代码**的两条：

- **INV-5 的 checkpoint 半边由构造保证（Minor 6）**：`backend/app/context/*.py` 中 `checkpoint|put(|thread_id|AgentState|TypedDict` **零匹配**——组装器拿不到 thread id，结构上写不了 checkpoint。补一条 checkpoint 断言需要把 `rt` 的 checkpointer 接进用例，收益低于引入新耦合的风险，故只在此记录。**依据是 grep 结果，未做变异验证。**
- **`leading_strip` 的表头未闭合轮边界（Minor 9）**：`app/context/budget.py:208` 的 `_leading_closed_rounds` 在第一个 `closed=False` 处停、`:168` 用它界住 `max_k`；`app/conversation/turns.py:174` 只在 `admission.resuming` 时写 `message_kind="interrupt_answer"`，而正常"暂停→恢复"流程里中断自己的 assistant 行先落地、已闭合该轮，故终审者也未能构造出可达的生产序列。后果仅为"这一次组装不压缩"，§6.1 step 4c 明确宣告其合法，硬预算闸门随后兜住。**潜在且未证明可达**，不改代码。

未解决、不得视为已修的一条：**INV-1 的守卫仍然只有 `test_context_summary.py:481` 一个见证**（R32 记录的状态，见上文"本轮另补了两条探针"第 1 条）。Task 13 没有改变这一点。

### 14.2 Task 13 增量复审与其处置（Task 14）

Task 13 的增量复审（`.superpowers/sdd/2026-09-28-context-management-plan/task-13-review.md`，复审对象 `a778b98` → `3672dae`，基线 `55 passed, 1 warning in 693.78s`）裁决 **`With fixes`：0 Critical / 1 Important / 2 Minor**。三个 Important 复审者都逐条做了真变异复跑，确认**真的关闭**，无 Critical：

- **Important 1（INV-4 既有行指纹）**：变异（M1）在 `app/context/summary.py` 的 `_commit` 里就地改写第一条账本行的 `content` 后，**只有新断言变红**（`test_context_acceptance.py:245: AssertionError: every row that was already there is still there, with the same content`，`At index 0 diff: (1, 'user', 'message', 'REWRITTEN-2') != (2, 'assistant', 'message', '好')`），行数断言在同一变异下仍通过。
- **Important 2（INV-12 只量 user 那一条、上界含魔数 `+200`）**：变异（M2）把 `app/context/summary.py:179` 的 `for chunk in chunk_for_summary(strip, chunk_budget):` 改成 `for chunk in [list(strip)]:`（不切块）后变红（`test_context_acceptance.py:384: AssertionError: a summary call grew past its own budget: 1528 > 1011`），失败信息里的 `1011` 与 §14.1 记的四项之和逐字一致。**但**该提交新写的 docstring 里留了一句被证伪的检测声明——见下条 Important。
- **Important 3（摘要审计没有会话范围）**：变异（M3）把 `app/audit/context.py:108` 改成丢掉会话归属后，`test_context_acceptance.py:269` 与 `test_context_integration.py:116` **两条都红**，且残余集合 `{'conversation'}` 证明收敛后的查询不是空查询。

复审新发现的一条 Important 与两条 Minor，本轮（Task 14，提交 `test(context): make the summary-call bound's claim true`）全部处置，**不改任何生产代码**：

- **I-1（Important）`test_context_acceptance.py:362-364` 声称的检测能力被证伪**：原文说这条上界能抓"有人养大 `SUMMARY_PROMPT`、模板或预算"。这是不可能的——上界的第一、二项就是运行时量出的 `estimate_message_tokens(SUMMARY_PROMPT)` 与现渲染的 user 模板，第三项也含运行时预算，被约束的常量长大时上界同步长大。复审实证：`SUMMARY_PROMPT` 加 4000 字符后 `estimate_message_tokens == 4269`（**已超 `TIGHT.input_budget = 4000`**）而 `test_inv12` 仍 `1 passed in 18.56s`；模板加 600 字符仍 `1 passed in 18.95s`。**处置**：按复审建议第 1 种修法，在 `bound` 之前新增两条**固定上限**断言——`assert estimate_message_tokens(SUMMARY_PROMPT) <= 300, "the summarizer's system prompt grew"` 与 `assert (estimate_message_tokens(render(SUMMARY_USER_TEMPLATE, previous="", conversation="")) <= 60), "the summarizer's user template grew"`（上限取自已提交的实测 269 与 35，留余量而非等号），并把那段 docstring 改成实话：本上界能抓的是**分块被关掉或被绕过**，抓不到常量增长；预算自身的关系约束由 `app/core/config.py:71-80` 的 `validate_context_budget` 负责。**本轮变异实测**：`SUMMARY_PROMPT` 加 4000 字符后 `test_inv12` **变红**（`test_context_acceptance.py:389: AssertionError: the summarizer's system prompt grew`，`assert 4269 <= 300`）；`SUMMARY_USER_TEMPLATE` 加 600 字符后 **变红**（`test_context_acceptance.py:390: assert 635 <= 60`）；两次均已 `git restore`。即复审的 M6b/M8 在修后**不再能绿**。
- **M-1（Minor）本文件 `:496` 的变异行号不准**：原记 `test_context_acceptance.py:516` 且把 `:519` 说成"docstring 增行后的位置"。实测在 `3672dae` 上 `assert not any(` 在 `:517`、消息行在 `:519`。**已改为 `:517`，并把括号改成"该断言与消息行位于 `:517-519`"**（避免同一处两个互相矛盾的行号）。
- **M-2（Minor）领域 prompt 用例的 docstring 把守卫说成见证**：`:517-519` 的 `role: content` 检查是真数据流见证（M4 能红）；`:520`/`:521`/`:522` 被说成"模板守卫、不见证泄露路径"。**这条处置记录本身有误，已由 Task 15 更正**：Task 14 复跑时用的变异体是把 `state.get("summary")` 拼进渲染后的领域 prompt，而 `AgentState`（`app/agents/state.py:80-95`）根本没有 `summary` 键，该表达式恒为空串——**那是一次空操作，"仍 `1 passed`" 不是对"拼接摘要"这条路径的测量结果**。Task 15 的真变异实测（行号相对 Task 14 提交态 `15472e8`）：把摘要**正文**追加到 `backend/app/agents/supervisor.py` 的 render 调用点之后，`:550` **变红**（`AssertionError: no summary body`）；摘要**表头**同样让 `:549` 变红（`AssertionError: no summary header`）；早先领域结果文本让 `:551` 变红（`AssertionError: no earlier domain's result`）；把同样三段文本分别**插进 `DOMAIN_PROMPT` 模板本身**，同样分别红在 `:550`/`:549`/`:551`。即这三条**确实见证**"摘要/结果文本进入渲染后的领域 prompt"这条泄露路径（模板与调用点两条来路都能触发），只有"模板里被塞进 transcript 行"那一路由 `:546` 的 `role: content` 检查负责。docstring 已按"哪条对什么敏感"重写（不写死行号），断言体未动。

本轮**未改变**的一条：§14.1 结尾那句"**INV-1 的守卫仍然只有 `test_context_summary.py:481` 一个见证**"**保持不变**——Task 14 没有碰它，也没有新增任何 INV-1 见证。

### 14.3 Task 14 增量复审与其处置（Task 15）

Task 14 的增量复审（`.superpowers/sdd/2026-09-28-context-management-plan/task-14-review.md`，复审对象 `3672dae` → `15472e8`）裁决 **`With fixes`：0 Critical / 1 Important / 2 Minor**，三条**全在文档层**，断言体与生产代码无错。复审自跑基线为 `test_context_acceptance.py` 全文件三次：`1 warning, 16 errors in 28.31s` / `15 passed, 1 warning, 1 error in 297.17s` / `14 passed, 1 warning, 2 errors in 310.14s`，共 19 个 node 级失败**全部零断言帧**，按 node id 重跑 3/3 通过。

本轮（Task 15，提交 `docs(context): say what the domain-prompt guards really witness`）**只改文档**：不改任何断言体、不改任何生产代码（变异实验除外，且全部 `git restore`）。三条处置如下。

- **Important 1（docstring 把三条检查说成"不见证泄露路径"的守卫，且其 `(measured)` 证据是一次空操作变异）**：复审的 E6 复跑 Task 13 的 M12（在 `supervisor.py` render 末尾追加 `+ (state.get("summary") or "")`）确实 `1 passed`，但探针 E11 显示 `AgentState` 键集合 `['active_agent', 'collected_inputs', 'domain_results', 'goal', 'messages', 'pending_answer', 'pending_question', 'turn_phase', 'turns']` **不含 `summary`**，该表达式恒为空串——**变异是空操作，什么都没量到**；docstring 括号里"因为 fixture 里那次领域调用发生在首次摘要之前"只是巧合时序，不是那次绿的成因。**处置**：按"哪条对什么敏感"重写整段（不写死行号）——四条都能红，`role: content` 检查对"模板里被塞进 transcript 行"敏感，另三条对"摘要/结果文本进入渲染后的领域 prompt"敏感。**Task 15 真变异实测**（全部在 Task 14 提交态 `15472e8` 上；行号相对该提交态，改写 docstring 后已各自下移 8 行；每次跑前跑后 `Get-Process pytest,python,uv` 计数均为 0；每次变异后 `git restore`，末次 `git diff --name-only -- backend/app` 为空）：在 `backend/app/agents/supervisor.py` 的 render 调用点追加摘要正文 → `:550` 红（`AssertionError: no summary body`）；追加摘要表头 → `:549` 红（`AssertionError: no summary header`）；追加结果文本 → `:551` 红（`AssertionError: no earlier domain's result`；首次跑为**零断言帧**的云库抖动 `psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly` → `tests\conftest.py:61`，按 node id 重跑后红，记"flaked 1 次，全部零断言帧"）；在 `backend/app/agents/prompts.py` 的 `DOMAIN_PROMPT` 模板里插入 `user: LEAKED TRANSCRIPT LINE` → `:546` 红（`AssertionError: no transcript line reaches a domain prompt`）；同样地**模板**里分别插入摘要正文/表头/结果文本 → `:550`/`:549`/`:551` 分别红。**四条检查全部真红，不存在"永不可能失败"的守卫**；三条摘要/结果检查对**模板**与**调用点**两条来路都敏感。
- **Minor 1（spec §14.2 `:519` 把同一条空操作证据写进了永久记录）**：已在本节上文 M-2 那条记录里就地更正，注明该变异体在 `AgentState` 上取不到 `summary` 键、表达式恒为空串，故"仍 `1 passed`"并非对"拼接摘要"路径的测量结果，并补上 Task 15 的真变异结果。
- **Minor 2（docstring 自相矛盾：先说三条 `do not witness a leak path`，又说 `本领域已处理` 值得断言，理由是领域 prompt 确有 `{results}` 槽位、泄露进去会是同一个 bug）**：随 Important 1 的整段重写一并消除——新 docstring 不再出现"不见证泄露路径"的说法，并明确 `本领域已处理` 这条对"结果文本进入渲染后的 prompt"敏感（实测可红）。

本轮**未改变**的一条：§14.1 结尾那句"**INV-1 的守卫仍然只有 `test_context_summary.py:481` 一个见证**"**保持不变**（R32）——Task 15 只动 docstring 与 spec 记录，没有碰它，也没有新增任何 INV-1 见证。R24 / R29 / R30 / R35 同样未改判。
