# LLM 底座：会话、审计与上下文管理总览

状态：设计草案  
日期：2026-09-27  
范围：Agent 服务（`backend/`）；不重定义独立 `bank_backend/` 的业务审计模型。

## 1. 目标

把当前“登录 session 直接对应一个 LangGraph thread”的实现升级为可持久化的多会话底座：

- 一个用户可以创建、切换、重命名、置顶、归档、软删除和恢复多个聊天会话。
- 每个聊天会话的消息、摘要和 checkpoint 彼此隔离；运行状态只由 LangGraph checkpoint 表达。
- 原始消息永久追加保存；上下文压缩不得删除或覆盖原始消息。
- 模型调用、Agent/工具运行和业务动作可以按会话和运行追溯。
- 普通运行日志与持久化审计分开，不能互相替代。

## 2. 核心术语

| 术语 | 定义 |
|---|---|
| 登录 session | 认证层的短期身份凭证，绑定 `user_id`；不是聊天会话。 |
| conversation | 用户可见的聊天容器，归属于一个 `user_id`，拥有独立历史和生命周期。 |
| run | 一次图调用的关联范围，拥有 `run_id` 供模型/工具审计和排障关联；不是持久化生命周期实体，不建立 `conversation_run` 表或第二套状态机。interrupt resume 是新的图调用 ID，继续使用 conversation 的 checkpoint thread。 |
| message | 会话中的追加式可见消息；用户消息和助手最终回复进入消息账本。 |
| checkpoint | LangGraph 为恢复图执行保存的内部状态；不是消息账本，也不是审计凭证。 |
| context view | 某次模型调用实际获得的提示词视图，由摘要、最近消息和 LangGraph checkpoint 中当前 invocation 的必要状态组装。 |
| runtime log | 面向排障和监控的运行日志；允许轮转，不作为业务事实来源。 |
| audit record | 面向追溯、责任和合规的持久化事实记录；追加写入并受控访问。 |

## 3. 稳定 ID 与关联

- `user_id`：现有身份服务产生。
- `session_id`：现有登录 session，不能用来标识聊天会话。
- `conversation_id`：服务端生成，用户可见但不可猜测；所有会话 API 必须校验归属。
- `message_id`：服务端生成；同一 conversation 内有单调递增的 `sequence`。
- `run_id`：每次图调用新建，仅作关联 ID；不维护 attempt 或 run 状态。resume 使用新的 `run_id`，并继续同一 conversation checkpoint thread。
- `call_id`：每次模型或工具调用唯一。retry/attempt 策略不属于会话持久化阶段。
- `thread_id`：内部派生值，建议由固定前缀和 `conversation_id` 构成；客户端不提交。

持久化记录按职责关联 `conversation_id`、`message_id`、`submission_id`、`run_id` 和时间；不要求每条记录同时拥有所有 ID。跨服务业务操作使用 `run_id` 或明确的 `request_id` 关联，Agent 不复制银行服务的业务审计事实。

## 4. 数据所有权

```text
ConversationMessage   用户和助手可见的原始对话历史
LlmCallAudit          每次模型请求的完整 prompt/response 及调用元数据
AgentRunEvent         审计 spec 记录的 Agent 调度、工具、暂停和恢复事实；不作为运行状态投影
BusinessAuditEvent    Agent 自身真正改变的业务/配置动作；银行业务仍由 bank_backend 审计
ContextSummary        给模型使用的压缩视图及其覆盖范围
LangGraph checkpoint  可恢复图状态
Runtime log           排障、监控、告警
```

上下文摘要可以更新，其他审计与消息记录不得因摘要而删除。普通日志只记录 payload 的摘要、长度、哈希或关联 ID，不写完整 prompt/response。

`LangGraph checkpoint` 是 Agent 执行状态的唯一来源，负责保存可恢复的图状态。会话持久化不建立 `conversation_run` 生命周期投影；消息账本和客户端 submission 只保存用户可见事实及幂等关联，不复制或推导图状态。

上表的 `BusinessAuditEvent` 已由 `2026-10-03-conversation-llm-foundation-supplement-spec.md` §2 落成迁移 `0010_business_audit_event`。

## 5. 依赖与实施顺序

1. 会话与消息持久化：建立 `conversation_id`、消息账本和 thread 隔离。
2. 审计：使用 conversation/message/run/call ID 记录模型和运行行为；衔接现有 `bank_backend` 审计。
3. 上下文管理：以消息账本为事实来源，保存摘要版本并按预算组装 context view。

三个专项 spec 可以分别实施，但必须遵守本文件的 ID、所有权和不可变性约定。

## 6. 非目标

- 本阶段不做多 worker 分布式锁、跨实例事件恢复或任务队列。
- 本阶段不引入向量数据库和语义检索。
- 本阶段不把 `bank_backend` 的 `AuditLog` 迁移到 Agent 数据库。
- 本阶段不把完整 prompt/response 暴露给普通前端会话接口。
- 会话持久化首期不创建第二套 run 状态机，不实现 retry/attempt fencing、任意崩溃后自动重放、人工副作用核对、写工具意图账本、SSE durable replay 或跨进程接管。
- 服务重启后只恢复 checkpoint 明确可恢复的 interrupt；无法证明安全的未完成图调用不自动重放，返回恢复所需错误。更完整的副作用核对和恢复另立可靠性设计。

