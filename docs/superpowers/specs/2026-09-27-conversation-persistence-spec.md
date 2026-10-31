# 会话与消息持久化 Spec

状态：**已实现并冻结**（代码冻结点 `ed47abb`）  
日期：2026-09-27  
关联：`2026-09-27-conversation-llm-foundation-overview.md`

## 0. 冻结说明

本 Spec 已按下方正文实现。实现中的三处命名与语义由本节钉住，正文相应位置已加注：

1. **`conversation_message.metadata` 列名不变，模型属性名为 `meta`。** `metadata` 是
   SQLAlchemy declarative 类的保留属性（即 `Base.metadata`），同名属性会覆盖它，因此
   类上写 `meta`、列上写 `"metadata"`。对外 JSON 字段仍是 `metadata`。
2. **列表的 `archived` 与 `deleted` 是两个互不蕴含的 include 开关。** 默认各为 `false`；
   `archived=true` 只是把已归档的会话并入列表，不会带出软删除的会话，反之亦然。因此
   "已归档/已删除" 标记只在对应开关打开时可见。
3. **两个 `next_cursor` 语义不同。** 会话列表的是不透明 base64 游标，客户端原样回传；
   消息列表的是数字（最后一条的 `sequence`），正好是 `after=` 接受的参数。

另有两处**行为收紧**，是本节要求的准入规则对既有宽松行为的取代，已在实现中落地并测试：

- 普通消息在 interrupt 未答时不再"重复展示等待信息"，而是在写入前以
  `INVALID_INTERRUPT_ANSWER` 拒绝（§2.4、Plan §6.1）。拒绝不消耗 sequence，也不影响
  仍开放的问题。
- `mode="new_goal"` 在新旧两条路由上都以 `NEW_CONVERSATION_REQUIRED` 拒绝（§5），
  不再有"原地放弃目标"的宽松路径。

### 0.1 修订记录

**2026-09-28：错误码由七个扩为八个。** 上下文管理 spec（`2026-09-27-context-management-spec.md`
定稿）需要在模型调用前因预算不足而拒绝请求，该失败与既有七个码语义均不重合，因此新增
`CONTEXT_LIMIT_EXCEEDED`（409）。§4 的"固定为七个"已同步改为八个。

该码的 `retryable` 不是常量，而是由 `details.reason` 决定（`summary_failed` → true，
`over_budget` → false），这是本表唯一一个这样做的码；其余七个仍是固定值。

这次修订**只扩展错误码词表**，不改本 Spec 的任何数据模型、生命周期、准入规则或幂等语义：
账本仍然只增不改，执行状态仍然只来自 checkpoint，`context_summary` 是账本的派生视图而非
第二个状态源，因此不违反 §1 的边界。

## 1. 目标与边界

本阶段将登录 session 与聊天 conversation 分离，保存完整的用户/助手可见消息，并支持用户新建、切换和恢复旧会话。每个 conversation 绑定独立 LangGraph `thread_id`，checkpoint 继续由 LangGraph saver 管理。

本阶段只建立三类持久化事实：`conversation`、追加式 `conversation_message`、客户端写请求的 `conversation_submission`。`run_id` 是审计和排障使用的关联 ID，不建立 `conversation_run` 表，不在业务数据库复制图状态，不新增 RUNNING/FAILED 等运行状态机。

唯一执行状态来源是 LangGraph checkpoint。数据库事务不跨越 checkpoint 写入；因此本阶段不承诺任意进程崩溃后的自动重放、retry、checkpoint reset、工具副作用对账或跨进程接管。已持久化的消息不会因 checkpoint 变化或未来上下文摘要而删除。

## 2. 数据模型

### 2.1 `conversation`

建议字段：`conversation_id`、`user_id`、`title`、`pinned`、`archived_at`、`deleted_at`、`created_at`、`updated_at`、`last_message_at`、`next_message_sequence`、`thread_id`。

- `conversation_id` 与 `thread_id` 由服务端创建；`thread_id` 全局唯一且客户端不可写。
- `next_message_sequence BIGINT NOT NULL DEFAULT 1`；追加消息时锁定 conversation 行、读取并递增。
- 会话列表按 `pinned DESC, COALESCE(last_message_at, created_at) DESC, conversation_id DESC` 稳定排序（2026-10-13 修正：原规则 last_message_at DESC NULLS LAST 会把还没写过消息的新会话沉到第一页之外，侧栏永远看不到它）。游标是这一排序里的一个位置，因此携带排序版本号：旧排序发出来的游标返回 422 INVALID_PARAMETER，而不是静默跳行。
- 软删除不级联删除消息和 checkpoint；本阶段不提供永久清理。

### 2.2 `conversation_message`

这是用户可见历史的事实账本，追加写入。建议字段：`message_id`、`conversation_id`、nullable `run_id`、`sequence`、`role`、`content`、`message_kind`、nullable `question_id`、`created_at`、`metadata`（模型属性名 `meta`，见 §0.1）。

- `(conversation_id, sequence)` 唯一且单调递增。所有用户消息、interrupt answer 和助手最终消息使用同一数据库序号分配器；每条可见消息追加时更新 `last_message_at`。
- 新请求产生的用户消息和对应助手最终消息使用同一 `run_id` 用于审计关联；历史导入时若无法可靠还原该 ID，允许为 null。`run_id` 是相关 ID，不是外键，也不表达生命周期。
- `message_kind=interrupt_answer` 时 `question_id` 必须非空；其他消息为空。首期公开 role 仅为 `user`、`assistant`。
- 消息不可编辑。prompt、模型原始 response 和工具私有 facts 不写入本表。
- 用户消息在图调用前持久化；助手最终回复在生成成功后持久化。Runtime 将普通用户消息以账本 `message_id` 和规范化可见内容写入 checkpoint transcript；resume answer 也作为新的用户消息追加，再通过 LangGraph `Command(resume=...)` 恢复 checkpoint。

### 2.3 `conversation_submission`

此表只解决客户端重试导致的重复写入和重复图调用，不记录运行状态。建议字段：`submission_id`、`user_id`、`operation_type`、`idempotency_key`、`request_hash`、`conversation_id`、`message_id`、nullable `run_id`、`created_at`。首期仅支持 `operation_type=conversation_message`；submission、conversation 和 message 关联在事务提交时必须完整，`run_id` 可为空。

- 唯一键为 `(user_id, operation_type, idempotency_key)`。不同用户可复用 key；同一用户跨 conversation 复用 key 会被检测。
- `request_hash` 为规范化请求的 SHA-256，输入包括 `conversation_id`、消息正文和规范化 `client_context`（包含 resume question/value）。同 key、同 hash 返回原 submission/message 及可能为空的 run_id；同 key、不同 hash 返回 `IDEMPOTENCY_CONFLICT`。
- 跨 conversation 并发首次提交由数据库唯一约束裁决。候选消息、sequence 和 submission 在一个事务内写入；唯一冲突时整体回滚，再在新事务读取胜出 submission 并比较 hash。不得留下候选消息或 sequence 递增。
- key 不因归档、软删除或请求完成而释放，不设自动过期。幂等命中必须发生在 sequence 分配和图调用之前。
- submission 不是任务队列或 durable outbox。消息事务提交后、图调用启动前若进程退出，同 key 重试只返回 receipt，不自动补跑；用户检查账本/checkpoint 后以新的 conversation 继续。本阶段不保证请求必达或外部写操作 exactly-once。
- 旧 `/api/chat/messages` 路径只兼容原请求体形状，不兼容缺少幂等 header 的旧客户端。切换前先发布携带 `Idempotency-Key` 的前端；服务端随后对新旧路由统一强制要求 header。缺少 key 返回 `IDEMPOTENCY_KEY_REQUIRED`，不降级为无法识别重复请求的“尽力而为”模式。

### 2.4 `run_id` 与 LangGraph checkpoint

`run_id` 是一次图调用的 dispatch correlation ID，只用于关联可能产生的模型/工具审计记录，不创建对应的状态投影或状态转换。服务在调用 LangGraph 前生成并关联 `run_id`；它表示一次调用尝试已准备 dispatch，不证明图已开始、完成或成功。若进程在数据库关联后、实际调用前退出，submission 仍可有该 ID，但不得据此推断执行状态。恢复 interrupt 会产生新的 `run_id`，但继续使用同一 conversation 的 checkpoint thread。conversation 的线程隔离以数据库保存的 `thread_id` 为准。

- 普通用户消息和 interrupt answer 均使用账本中的 `message_id` 作为 LangGraph message ID，并把账本中的规范化可见内容写入 checkpoint transcript。LangGraph 的 message reducer 按 ID 合并，避免同一消息重放造成 transcript 重复。resume answer 还必须匹配 checkpoint 当前开放的 `question_id`；该写入和 interrupt 消费由 checkpoint 管理，不在数据库另建状态。
- 请求准入只读取 LangGraph checkpoint，不写数据库运行状态：checkpoint 明确等待 interrupt 时只接受匹配其 `question_id` 的 resume；普通消息或 question 不匹配的 resume 在写入消息/submission 前以 `INVALID_INTERRUPT_ANSWER` 拒绝。checkpoint 显示未完成任务但无法证明处于 interrupt 时，在写入前返回 `EXECUTION_RECOVERY_REQUIRED`。只有 checkpoint 明确可接受普通消息或匹配 resume 时，才进入消息持久化和图调用流程。
- 服务重启后可根据 checkpoint 展示明确仍开放的 interrupt，并允许用户提交答案。若某条答案已进入消息账本但其 dispatch 结果未知，同 key 重放只返回 receipt，不自动重新调用图；如果 checkpoint 仍开放该问题，用户可明确重新作答（使用新 key），旧答案保留在账本但不注入 transcript。checkpoint 未明确开放问题时返回 `EXECUTION_RECOVERY_REQUIRED`。
- checkpoint 显示仍有未完成任务、但不能证明处于 interrupt 时，不自动重放。API 返回 `EXECUTION_RECOVERY_REQUIRED`；本阶段不提供 retry、接管或人工核对管理 API。消息仍可读取，用户可新建 conversation 继续其他工作。
- 不维护数据库侧的 active run、attempt、lease、heartbeat、outcome resolution 或人工解除封锁字段。

## 3. 生命周期与并发

- 创建：生成空 conversation 和独立 thread。
- 新消息：认证 owner，校验会话可写，先处理幂等 key，再读取 checkpoint 确定请求可准入；被拒绝的请求不写 submission/message。通过后在单一数据库事务中追加 user message、submission（`run_id=NULL`）并更新 conversation 时间。随后生成 invocation correlation ID，在调用 LangGraph 前用短事务同时填入 submission.run_id 和触发 message.run_id，再调用图。关联事务失败时不调用图并返回 `PERSISTENCE_FAILED`；原 submission/message 仍可由相同 key 查询，但不自动补跑。这个字段只标识准备 dispatch 的调用尝试，不是运行状态或执行结果证明。
- interrupt answer：要求非空 `question_id`，Runtime 对照该 conversation checkpoint 的开放问题；匹配后先持久化答案，再恢复同一 checkpoint thread。Runtime 将账本 `message_id` 和规范化答案内容带入 interrupt 消费节点，使其以相同 message ID 追加到 checkpoint transcript。dispatch 前的关联事务同时设置 submission.run_id 和答案 message.run_id。
- final：助手消息携带触发该图调用的同一 `run_id`，并与 `last_message_at` 在同一数据库事务提交；之后发送 SSE `final`。
- 首期仅支持一个 Agent 进程，conversation lock 覆盖同会话的 checkpoint 准入、消息写入和图调用；数据库行锁和唯一约束保护数据分配。多进程接管和分布式锁不在本阶段。
- 新请求期间进程崩溃或 SSE 连接中断时，不自动重新调用图。用户消息保留，checkpoint 保留；是否能继续只依据 checkpoint 的明确状态，不从数据库推断或重建图状态。客户端断开可能使当前流处理被取消，本阶段不承诺服务端继续执行。
- 消息/submission 已提交不代表图调用必然完成；receipt 只确认该 key 对应的持久化用户消息，不承诺后台任务仍在运行或已成功。

归档和软删除只改变会话可见性及是否接受新提交，不取消已开始的图调用。已开始的请求可以完成并追加最终消息；已归档/软删除 conversation 不接受新消息。软删除 conversation 可通过 restore 恢复后继续访问原消息和 checkpoint。

## 4. HTTP 契约

所有接口从登录 session 解析 `user_id`，不信任请求体身份。所有 conversation/message/submission 查询均校验 owner；无记录和无权限对外使用相同错误。

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/conversations` | 新建会话；`data` 为会话对象本身 |
| `GET` | `/api/conversations` | 稳定游标分页列表（`archived`/`deleted` 为两个独立 include 开关，见 §0.2） |
| `GET` | `/api/conversations/{id}` | 会话元数据及从 checkpoint 读取的安全恢复提示；`data` 为 `{conversation, pending_interrupt, recovery_required}` |
| `PATCH` | `/api/conversations/{id}` | 修改标题、置顶或归档状态 |
| `DELETE` | `/api/conversations/{id}` | 软删除 |
| `POST` | `/api/conversations/{id}/restore` | 恢复软删除会话 |
| `GET` | `/api/conversations/{id}/messages` | 按 sequence 游标读取可见消息（`next_cursor` 为数字，见 §0.3） |
| `POST` | `/api/conversations/{id}/messages` | 首次提交通过当前请求返回 SSE |

除 `GET /api/conversations/{id}` 外，单个会话的读写都以会话对象本身作为 `data`。错误码固定为八个：
`IDEMPOTENCY_KEY_REQUIRED`、`IDEMPOTENCY_CONFLICT`、`CONVERSATION_NOT_WRITABLE`、
`INVALID_INTERRUPT_ANSWER`、`EXECUTION_RECOVERY_REQUIRED`、`PERSISTENCE_FAILED`、
`NEW_CONVERSATION_REQUIRED`、`CONTEXT_LIMIT_EXCEEDED`；每个码各有一个状态码和一句面向用户的文案，集中在
`app/core/errors.py`。最后一个由上下文管理 spec 引入（见 §0.1），其余七个的语义与状态码不变。

POST 必须携带 `Idempotency-Key`。首次新提交返回 `Content-Type: text/event-stream` 并通过当前请求发送图事件。相同 key/hash 的重复 POST 必须在发送 SSE headers 前返回 `200 application/json` receipt（`submission_id`、`message_id`、nullable `run_id`、`duplicate=true`），不再次调用图；相同 key、不同 hash 返回 `409 application/json` 错误，错误码为 `IDEMPOTENCY_CONFLICT`。客户端必须先检查 response `Content-Type` 再选择 SSE parser 或 JSON receipt parser。receipt 只确认用户消息已持久化，不表示图仍在运行或已完成。客户端收到 receipt 后刷新账本；若没有 assistant final，只能显示“结果未确认”，不能从 receipt 推断成功/失败。首期不提供状态查询或流重连，因此此限制属于明确的产品降级。

本阶段不提供 run 查询、retry 或持久化 SSE replay/cursor API。SSE 断开可能影响当前流处理；客户端可重新读取消息并显示结果未确认，但不能依靠重连取回事件。durable event log 和断线后续传另列可靠性工作，不属于会话消息账本。

## 5. `new_goal` 边界

本阶段不支持在同一个 conversation 中原地 reset LangGraph thread，也不定义 `new_goal` 与已有 interrupt/执行的交互。若请求 `client_context.mode=new_goal`，API 返回 `NEW_CONVERSATION_REQUIRED` 且不写 submission/message、不调用图。用户要开始独立任务时创建新 conversation，以获得新的 thread 和隔离上下文。未来若需要同一会话内重置目标，必须单独设计不依赖跨数据库/checkpoint 原子提交的 thread-generation 方案。

## 6. 迁移与兼容

- 先盘点现存 `chat:{session_id}` checkpoint thread，并用可靠的 session 到 user 映射创建默认 conversation；无法确认 owner 的 thread 留在管理迁移清单，不猜归属。
- 迁移后的 conversation 可直接绑定原 thread ID，不复制 checkpoint；新 conversation 使用新格式 thread ID。
- 历史消息只导入 checkpoint 中可可靠还原的 user/assistant 可见消息。无法映射的消息标记账本不完整；旧消息 `run_id` 可为 null。
- 为避免消息与 run 互相引用，本模型不建立 `conversation_run` 表或循环外键。迁移先建 conversation，再建 message/submission；外键只从消息和 submission 指向 conversation/message。
- 历史 thread 盘点、owner 映射、回填工具和旧路由 header 兼容测试是 runtime 切换前置条件。未通过门槛时保留旧 runtime。

## 7. 非目标与验收

本阶段不实现：`conversation_run` 生命周期状态机、同 run retry/attempt fencing、崩溃后自动 replay/接管、人工 outcome reconciliation API、写工具意图账本、exactly-once 外部副作用、同 conversation `new_goal` reset、跨进程并发以及 SSE durable replay。

验收标准：

- 用户可以创建、切换、归档、软删除并恢复多个隔离 conversation。
- 消息账本追加式持久化，序号稳定；摘要/上下文压缩不会删除账本消息。
- 同用户跨会话复用幂等 key 能冲突；重复提交不追加消息、不重复调用图。
- resume answer 必须匹配 checkpoint 当前开放的 `question_id`，且作为 user message 持久化。
- 同一 conversation 重用其 LangGraph thread；不同 conversation 不共享 checkpoint。
- 迁移门槛先于 runtime 切换；未知归属历史不错误绑定。
- 进程崩溃时不自动重放图；不宣称写工具副作用 exactly-once 或任意运行自动恢复。

## 8. 实现与验证

代码冻结点 `ed47abb`（分支 `dev`）。迁移 `0007_conversation_persistence`；存储服务
`app/conversation/service.py`；回合生命周期 `app/conversation/turns.py`；runtime 接入
`app/agents/runtime.py`；迁移工具 `backend/scripts/migrate_conversations.py`。

验证命令与结果：

```text
cd backend
uv run pytest -q                      # 577 passed, 2 skipped
uv run ruff check app tests scripts   # All checks passed!
uv run ruff format --check app tests scripts   # 116 files already formatted

cd frontend
npm test                              # 101 passed (5 files)
npm run build                         # vue-tsc clean
```

三个机制由变异实验证明测试真的会变红（改回旧行为 → 对应用例失败 → 按哈希校验还原）：

- 账本 `message_id` 不再写入 checkpoint transcript → 同一消息在 transcript 中出现两次。
- 幂等短路被移除 → 重复提交重新调用图，并返回 SSE 而不是 receipt。
- 切换会话时不清空在途回合 → 上一个会话的回合渲染到新会话上。

`test_checkpointer_integration.py` 覆盖真实 PostgreSQL checkpointer；迁移工具验证 dry-run、
重复执行与中途恢复，并已用真实数据库 + 真实 checkpointer 做过一次端到端演练（升级 →
dry-run → 回填 → 重复执行 → 无主线程），以及 `0007` 的 upgrade → downgrade → upgrade
往返。

浏览器实测：`scripts/serve.py` + `npm run dev`，真实模型下发送消息、刷新页面恢复账本、
新建第二个会话（不串历史）、归档（标记 + 禁止发送）、软删除、恢复，以及两个独立筛选开关
（`?archived=true&deleted=false`）。
