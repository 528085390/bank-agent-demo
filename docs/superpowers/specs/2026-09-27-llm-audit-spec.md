# LLM 与 Agent 审计 Spec

状态：设计草案  
日期：2026-09-27  
关联：`2026-09-27-conversation-llm-foundation-overview.md`

## 1. 目标与边界

Agent 底座目前只有 Python `logging`，独立 `bank_backend` 有业务 `AuditLog`；Agent 自身没有生效的持久化审计表。本 spec 新增 Agent 侧审计域，记录模型调用和 Agent 运行事实，并用 ID 与银行服务的业务审计关联。

审计不是普通日志的升级版：审计记录追加保存、可检索、权限受控；运行日志仍用于排障和告警，允许轮转，不能作为唯一事实来源。

## 2. 记录类型

### 2.1 `llm_call_audit`

每次模型调用一条逻辑记录；重试每次独立一条 attempt。

字段建议：`audit_id`、`conversation_id`、`run_id`、`call_id`、`attempt`、`agent_id`、`provider`、`model`、`request_format`、`prompt_json`、`response_text`、`input_tokens`、`output_tokens`、`latency_ms`、`result`、`error_code`、`created_at`。

`prompt_json` 和 `response_text` 保存完整原始内容，普通会话接口不可返回。API key、连接凭据和未必要的认证秘密不得进入记录。敏感业务字段按既有工具数据边界脱敏或加密；完整内容的访问只给受控运维/审计角色。

### 2.2 `agent_run_event`

保存一次 run 的关键事件：`RUN_STARTED`、`AGENT_STARTED`、`TOOL_CALL`、`TOOL_RESULT`、`INTERRUPTED`、`RESUMED`、`RETRY`、`CONTEXT_ASSEMBLED`、`RUN_SUCCEEDED`、`RUN_FAILED`。这些是追加式审计事件，不是第二个执行状态源；当前图状态仍以 LangGraph checkpoint 为准。

`CONTEXT_ASSEMBLED` 由上下文管理 spec 引入（`2026-09-27-context-management-spec.md` §8.2），每次组装 context view 追加一条，`payload_json` 记录估算 token、预算、摘要版本与覆盖范围、保留消息范围和裁剪原因。它同样是证据而非状态：读它不能判断一次 run 处于什么阶段，只能说明那次模型调用看到的是什么。

字段建议：`event_id`、`conversation_id`、`run_id`、`call_id`、`event_type`、`agent_id`、`tool_name`、`status`、`payload_json`、`error_code`、`created_at`。

按已确认的审计范围，工具调用参数和完整结果都进入受控的 `payload_json`，失败原因和重试结果也要保留。模块私有 facts 不进入普通运行日志或前端；它们若属于一次工具结果，则只存在于受控审计存储，并应用同等或更严格的访问控制与加密策略。

### 2.3 `business_audit_event`

只记录 Agent 服务自身改变的业务或配置，例如会话重命名、归档、删除恢复、LLM 配置修改。银行余额、转账、账单等业务动作仍由 `bank_backend.audit_logs` 记录。

字段建议：`event_id`、`user_id`、`conversation_id`、`run_id`、`request_id`、`action`、`resource_type`、`resource_id`、`result`、`reason`、`created_at`。

Agent 侧不复制银行审计的 old/new 全量快照；通过银行返回的业务 ID、request ID 或 operation ID 关联。

本节的表已由 `2026-10-03-conversation-llm-foundation-supplement-spec.md` §2 落成迁移 `0010_business_audit_event`。

## 3. 写入规则

- 在模型 adapter 边界包裹 `complete()`，无论成功、失败或重试都写 `llm_call_audit`。
- 在 runtime/gateway 的状态边界写 `agent_run_event`；事件写入不依赖 SSE 是否成功发送。
- 先持久化 `RUN_STARTED` 和用户消息关联，再执行模型调用。
- 审计写入失败时 run 进入失败态并记录可见错误；不能悄悄降级为“只有日志”。
- 运行日志只带 ID、状态、耗时、错误类型和摘要哈希；禁止写完整 prompt/response、API key、验证码和未脱敏账号。

## 4. 查询与访问

首期不开放完整 prompt/response 或完整工具载荷的前端接口。内部支持按 `conversation_id`、`run_id`、`call_id` 和时间范围查询；查询必须分页并做权限校验。

会话历史接口只读取 `conversation_message`。运营排障接口可以读取脱敏后的运行事件；完整模型载荷需要更高权限并记录访问行为。

## 5. 与现有系统的关系

- `bank_backend/app/domain/audit_service.py` 继续负责银行业务操作审计。
- `backend/app/main.py`、`sse.py`、`tools/*` 的 Python logger 继续负责运行日志，但增加统一关联字段。
- LangGraph checkpoint 继续负责可恢复状态，不承担完整 prompt/response 审计。
- SSE 是展示传输，不保证审计；客户端断开不能撤销已写入审计。

## 6. 保留与安全

模型审计可能包含个人和金融信息，数据库访问需要最小权限，生产环境应加密存储或字段级加密。保留期限应按业务和合规要求配置；删除 conversation 不删除审计记录，只改变普通用户可见性。

## 7. 验收标准

- 每次模型调用成功、失败和每次重试都能按 run/call 查询到对应记录。
- 记录包含完整 prompt/response、模型、耗时、token（provider 未提供时为 null）和错误分类。
- Agent 工具、暂停、恢复、失败和完成都有运行事件，且不依赖 SSE 客户端在线。
- 普通日志和前端消息接口不泄露完整模型载荷、完整工具载荷或密钥。
- 银行业务审计仍只在 `bank_backend` 记录，Agent 侧可通过稳定 ID 关联而不重复造账。
