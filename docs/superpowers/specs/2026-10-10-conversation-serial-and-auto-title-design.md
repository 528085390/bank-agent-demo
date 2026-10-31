# 会话独立编号与首轮自动命名 Spec

状态：已实现并合并进 dev（dev @ d084bad，2026-10-10 合并 feat/deferred-conversation-titling）；命名时机按 m00787 改为响应后台任务（见修订与 D-3）
日期：2026-10-10
基线：dev @ 90927ae6c390722e96cb61b3945cd44aee666024（工作区另有与本需求无关的未提交改动）
关联：2026-09-27-conversation-persistence-spec.md、2026-09-27-llm-audit-spec.md、2026-10-03-conversation-llm-foundation-supplement-spec.md

修订：

- 2026-10-10 初版。
- 2026-10-10 自审后精简：删掉「模型已配置」这一条永远不会触发的触发条件、删掉一条与其它用例重复的断言、删掉对不可能数据的防御性前端分支；把「刻意不做的事」单列，避免实现时顺手加复杂度。
- 2026-10-10 用户拍板（m00167）：历史会话不管、存量演示会话直接删除；自动改名照记业务审计、llm 审计不记；#N 徽标放标题前面。
- 2026-10-10 实施完成后按终审意见更正两处描述：§7 补上 backend/app/runtime.py 与 turns.py 的 AgentRuntimeLike.model；§10 改正 runtime.model 与 .inner 解包的关系（原文写反了）。
- 2026-10-10 用户拍板（m00787）：命名不再在 SSE 流内 await，改为本次 HTTP 响应的后台任务，流与会话锁立即结束；前端加一段有界的会话列表轮询把新标题取回来。本条推翻初版的 S-2.1「流结束之前」、§5 的两条机制禁令、D-3 与 §10 第一条风险。

## 0. 本文件的定位与声明

本文件只写「做什么」，不写「怎么做」；每一条都能用一条会变红的用例证明，并在「改哪里会变红」里指明位置。

四条已确认的用户决策：

- （m00001 提问，m00042 答复）编号走「存储里每用户稳定序号」（当时方案 A），不走「前端按列表位置显示」（方案 B）。
- （m00042）自动命名走「直接调用 LLM 概括」，不做本地截断；这次模型调用不进审计；失败即放弃，不做任何降级。
- （m00167）历史会话不补标题，存量演示会话一次性删除；自动改名本身照记业务审计；#N 徽标放在标题前面。
- （m00787）自动命名不在 SSE 流内等待：交给本次响应的后台任务执行，流与会话锁立即结束；前端做一段有界的列表轮询取回新标题。

本需求只有两件事：给会话一个稳定编号、首轮结束后把标题换成概括。下面所有条目都直接服务于这两件事；任何不服务于它们的机制都不进本文件（见 §5）。

## 1. 需求

- R-1 侧栏里每个会话都有独立编号，一列无法区分的「新会话」不再出现。
- R-2 用户与助手完成第一轮对话后，会话标题自动变成第一轮用户消息的概括。例：用户说「我要转账给张三」→ 标题变成「转账给张三」。

## 2. 既有事实（本文件的依据）

| 事实 | 位置 |
| --- | --- |
| DEFAULT_TITLE = "新会话"；MAX_TITLE_LENGTH = 120；clean_title 折叠空白并截断 | backend/app/conversation/service.py:42-43,115-121 |
| ConversationView 与 as_dict() | backend/app/conversation/service.py:124-150 |
| create(user_id, title=None)，事务内 db.add(row) | backend/app/conversation/service.py:312-330 |
| update() 先算 renamed 再写，并记 ACTION_RENAME | backend/app/conversation/service.py:417-451 |
| _locked() 取行锁；_record_change() 走调用者事务 | backend/app/conversation/service.py:727-740,742-760 |
| 列表排序 pinned DESC, COALESCE(last_message_at, created_at) DESC, conversation_id DESC（2026-10-13 修正） | backend/app/conversation/service.py:409-443 |
| Conversation 模型（conversation 表） | backend/app/storage/models.py:151-172 |
| 最新迁移 0010_business_audit_event | backend/app/storage/migrations/versions/0010_business_audit_event.py |
| 加列 + 唯一约束的迁移范式 | versions/0006_bank_customer_id.py:24-31；versions/0003_audit_sequence.py:12-21 |
| 0002 用 migrations/schema_v2.py 的 Base 建表；该文件不描述 conversation 表 | versions/0002_framework_runtime.py:5,14 |
| ModelClient 协议 complete(messages) -> ModelReply；ModelReply.text | backend/app/integration/model_client.py:104-122 |
| AuditedModelClient 每次 complete 都写 llm_call_audit；裸 adapter 在公开属性 .inner | backend/app/audit/model_client.py:50-85 |
| 「唯一入口」不变式：no path can quietly skip the audit boundary | backend/app/integration/llm_config.py:174-175 |
| stream()：final 分支先落库再 yield | backend/app/conversation/turns.py:241-245 |
| 前端收到 final 后仍等整个流结束，随后 settle→refreshConversations | frontend/src/conversations.ts:190-212；frontend/src/api.ts:173-176；frontend/src/App.vue:380-419 |
| 侧栏渲染与标题回退 | frontend/src/App.vue:590-620；frontend/src/types.ts:86-97；frontend/src/conversations.ts:307-310 |
| StreamingResponse 在流结束后才跑 background，客户端断连则跳过；TestClient 会一直等到 background 结束 | starlette/responses.py:255-283；starlette/testclient.py:423-428 |

## 3. 编号

### S-1.1 存储

conversation 表新增列 serial_no（整数、非空），并新增唯一约束 uq_conversation_serial_no_owner (user_id, serial_no)。

改哪里会变红：backend/app/storage/models.py 的 Conversation；backend/tests/test_conversation_serial_migration.py。

### S-1.2 分配规则

- 编号在用户内从 1 开始连续递增；不同用户互不影响（每个用户都从 #1 开始）。
- 编号只增不减：归档、软删除、恢复都不改变编号，也不回收；新会话取该用户当前最大编号 + 1。
- 取号与插入在同一事务内完成。同一用户并发创建两个会话，必须得到两个不同的编号，且都不失败。
- 用户的 app_user 行不存在时创建失败（fail closed），不得退化为猜测编号。

改哪里会变红：backend/app/conversation/service.py 的 create()（:312-330）；backend/tests/test_conversation_serial.py。

### S-1.3 迁移 0011_conversation_serial_no

- down_revision = "0010_business_audit_event"。
- upgrade 顺序：加可空列 → 回填 → 置 NOT NULL → 建唯一约束。
- 回填按用户分区、按 (created_at, conversation_id) 升序，从 1 开始。
- 已归档与已软删除的会话同样占号（编号是事实，不是可见性）。
- 不修改 migrations/schema_v2.py：该文件不描述 conversation 表，0002 的 create_all 只覆盖它自己声明的表。
- downgrade：删唯一约束 → 删列。
- 可重复执行：upgrade head 幂等；upgrade → downgrade → upgrade 不报错。

改哪里会变红：backend/app/storage/migrations/versions/0011_conversation_serial_no.py；backend/tests/test_conversation_serial_migration.py。

### S-1.4 接口

- 会话对象（POST /api/conversations、GET /api/conversations、GET /api/conversations/{id}、PATCH、DELETE、POST restore）都带 serial_no（整数）。
- serial_no 只读：PATCH 的请求体里没有这个字段（ConversationUpdate 是 extra="forbid"，传了会被现有 schema 以 422 拒绝，这是既有行为）；改 title 之后 serial_no 不变。

改哪里会变红：backend/app/conversation/service.py:124-150；backend/tests/test_conversation_api.py。

### S-1.5 呈现

- 侧栏每一行在标题左侧显示 #N 徽标；进入重命名输入框时徽标仍在。
- 编号不写进 title 文本（conversationLabel 不拼接编号），也不进任何审计载荷。

改哪里会变红：frontend/src/types.ts:86-97；frontend/src/App.vue:592-604；frontend/src/style.css。

## 4. 首轮自动命名

### S-2.1 触发时机

- 一次回合产生 final 事件（回答已落库）之后，把命名登记为本次 HTTP 响应的后台任务，由它在流之外执行一次。
- final 帧必须先于命名调用发出：用户看到回答的时间不受命名影响。
- 命名不在流内 await：SSE 流照常结束、会话锁随之释放，命名还在进行时用户就能开始下一轮。
- 每轮最多尝试一次；不重试。
- 客户端在收到 final 后离开、或进程在命名完成前退出时，后台任务可能不执行：这是允许的失败，不补做（S-2.6）。
- 后台任务必须重新进入本回合的审计上下文：turns.py 里的 `with audit_context(context)` 是生成器局部的，流结束后已经退出；不重新进入，apply_auto_title 的业务审计行就拿不到 user_id 而以 ValueError 失败（backend/app/audit/service.py:261-263），标题会悄悄不写。

改哪里会变红：backend/app/conversation/turns.py 的 stream()（:276-279）与 DeferredNaming；backend/app/api/routes.py 的 _turn_response/_stream_turn（:372-422）。

### S-2.2 触发条件（两条同时成立）

1. 该会话中 role='user' 且 message_kind='message' 的消息恰好 1 条。
2. 该会话 title 仍等于默认标题（clean_title(None)，即「新会话」）。

说明：条件 1 只数 message_kind='message'，因此「首轮先被 interrupt、用户回答后收尾」的会话也能拿到标题（那种会话里 role='user' 的行有 2 条，其中 1 条是 interrupt_answer）；而用户已经问过第二件事时，条件 1 自然不成立。

条件 2 同时承担两件事：用户手动改过名就不覆盖，自动命名成功后也不会被下一次触发重复覆盖。

不需要「模型是否已配置」这个条件：模型没配置时回合在第一次模型调用就抛 MODEL_NOT_CONFIGURED（backend/app/integration/model_client.py:143-155），而 supervisor 只对上下文溢出重试、其余直接上抛（backend/app/agents/supervisor.py:230-248），因此到不了 final，该条件永远不会触发。

### S-2.3 输入

- 提示词只含该会话那唯一一条 message_kind='message' 的用户消息原文。
- 不把助手回答、工具结果、历史摘要放进提示词。
- 要求模型只输出标题本身：不要引号、不要句末标点、不要解释，不超过 20 个字符，使用与用户消息相同的语言。
- 不使用结构化输出（模型协议只有文本）。

### S-2.4 调用与审计例外

- 命名调用使用当前模型的裸适配器：当前 model 是 AuditedModelClient 时用它的 .inner，否则用 model 本身；因此这次调用不写 llm_call_audit，也不写其他审计行。
- 这是对 backend/app/integration/llm_config.py:174-175「唯一入口、任何路径都不能悄悄绕过审计边界」的显式例外，由用户批准（m00042）。例外必须在代码中就地注释并指向本文件，且全仓库只允许这一处绕过；它只覆盖「会话自动命名」这一条调用路径。

### S-2.5 结果写入

- 对模型返回文本复用 clean_title 规范化（折叠空白、截断 120）；结果为空或等于默认标题时视为失败。
- 写入是条件写：在同一事务内确认标题仍为默认、且用户请求数仍为 1 才写（用户可能在等待期间手动改了名，也可能已经发了下一条消息），任一不成立即放弃。
- 写入同时更新 updated_at。
- 写入成功后记一条业务审计 conversation.rename（与手动改名同类事实；service.py:48-52,742-760）。该审计不属于 S-2.4 的例外范围。

### S-2.6 失败与超时

- 任何失败（模型报错、返回空、超时、DB 错误、校验不通过）只记一条 warning 日志：不改变标题、不改变流、不重试、不降级（不截断用户原话、不写「未命名」）。
- 命名调用有独立的时间上限（默认 10 秒），超时即放弃；该上限不取用户配置的模型超时，它只约束后台任务本身，不再影响 SSE 流何时关闭。
- 命名失败不得让本次回合变成 RUN_FAILED，也不得让已经发出的 final 帧作废。

## 5. 刻意不做的事

范围（scope）：

- 不为历史会话补标题，也不为它们写任何数据迁移脚本；存量演示会话在实施时一次性删除（见 §7）。迁移只补编号。
- 不提供「重新生成标题」入口，不加任何设置项或开关。

机制（避免顺手加复杂度）：

- 命名只由一个响应级后台任务承载（S-2.1）；不引入任务队列、持久化任务、重试、补偿或降级路径。
- 不新增 SSE 事件类型、不做推送；前端只在提交结束后做一段有界的列表轮询（有停止条件与次数上限），新标题仍经既有的列表接口出现（D-3）。
- 不新增任何模型调用（除了那一次命名），不改动任何既有模型调用路径。
- 不做编号回收、重排、自定义前缀、跨用户全局编号。
- 不改变 thread_id / checkpoint / 幂等语义，不改变会话列表排序与分页规则。

## 6. 验收用例（每条都要求「改坏了会变红」）

| # | 断言 | 位置 |
| --- | --- | --- |
| A-1 | upgrade head 后 conversation.serial_no 非空，且存在 uq_conversation_serial_no_owner | backend/tests/test_conversation_serial_migration.py |
| A-2 | 回填后每个用户的编号为 1..n 且互不相同；已归档/已删除的会话也占号 | 同上 |
| A-3 | downgrade 后列与约束消失，再次 upgrade 成功 | 同上 |
| A-4 | 连续创建 3 个会话编号为 1,2,3；删除中间一个再创建得到 4 | backend/tests/test_conversation_serial.py |
| A-5 | 两个用户的编号各自从 1 开始 | 同上 |
| A-6 | 同一用户并发创建的两个会话编号不同且都成功 | 同上 |
| A-7 | 会话接口响应含 serial_no；PATCH 改 title 之后 serial_no 不变 | backend/tests/test_conversation_api.py |
| A-8 | 首轮 final 后标题变为模型返回的概括；且捕获到的命名请求只含 system 指令与用户第一轮原话 | backend/tests/test_conversation_auto_title.py |
| A-9 | 用户已手动改名时，首轮 final 不改标题 | 同上 |
| A-10 | 会话已有 2 条 message_kind='message' 的用户消息时不再自动命名 | 同上 |
| A-11 | 模型报错/超时/返回空时标题保持默认，回合仍以 final 正常结束 | 同上 |
| A-12 | 自动命名不新增任何 llm_call_audit 行（对照组：一次普通对话恰好 1 行） | 同上 |
| A-13 | 自动命名成功后存在一条 conversation.rename 业务审计 | 同上 |
| A-14 | 侧栏每行在标题左侧渲染 #N；重命名输入框出现时徽标仍在 | frontend/src/App.test.ts |
| A-15 | 编号不进入标题文本 | frontend/src/conversations.test.ts |
| A-16 | 命名不阻塞流与锁：流的生成器在命名调用发生之前就结束，此时会话锁可立即再次取得 | backend/tests/test_conversation_auto_title.py |
| A-17 | 后台命名仍写同样的事实：跑完后标题为概括、恰好一条 conversation.rename、llm_call_audit 不增行 | 同上 |
| A-18 | 会话标题仍是默认值时前端有界轮询列表；标题变化后停止轮询 | frontend/src/App.test.ts |

## 7. 影响面

后端：

- backend/app/storage/models.py（Conversation 加 serial_no）
- backend/app/storage/migrations/versions/0011_conversation_serial_no.py（新）
- backend/app/conversation/service.py（ConversationView 加 serial_no；create 取号；新增自动命名的条件写入口）
- backend/app/conversation/titling.py（新：提示词、规范化、无审计调用、超时）
- backend/app/conversation/turns.py（AgentRuntimeLike 增加 model 成员；final 之后触发一次）
- backend/app/runtime.py（Runtime 把 model 转发给回合层）
- backend/app/api/routes.py（把命名登记为 StreamingResponse 的后台任务）

前端：

- frontend/src/types.ts（Conversation.serial_no）
- frontend/src/App.vue（#N 徽标；提交结束后有界轮询列表）
- frontend/src/conversations.ts（默认标题常量与轮询停止条件）
- frontend/src/style.css（.conversation-serial）

测试：A-1..A-18。

一次性数据清理（不是代码路径，不写进迁移）：实施时把演示库里已有的会话软删除（deleted_at = now()），因此它们不需要标题、也不会占住侧栏；执行前先把 SQL 与预计影响行数给用户过目。

## 8. 决策与理由

- D-1 编号存成真实列，不用列表位置：归档、删除、筛选、分页都会让位置变化，编号必须稳定。
- D-2 取号用「锁用户行 + 事务内取最大值」：唯一约束只能保证不重号，不能保证不失败；行锁把「取号 + 插入」串行化，代价是创建会话时多一次行锁（低频操作）。
- D-3（2026-10-10 m00787 改写）命名登记为本次响应的后台任务，不在流内 await：流与会话锁在回答发出后立即结束，前端的 settle→refreshConversations 因此拿不到新标题，所以前端在提交结束后做一段有界的列表轮询（1 秒一次、最多 5 次，标题一变就停）。初版 D-3（流内 await、靠流结束后的那次刷新取回标题）被用户否决：它让流和会话锁都多等一次 LLM 调用（实测约 2.7 秒，上限 10 秒），而写标题本身是毫秒级。备选「请求内 asyncio.create_task」被否：TestClient 每个请求新建并关闭事件循环，任务会随之消失（starlette/testclient.py:423-428），生产上也缺少与响应生命周期绑定的语义。
- D-4 不做降级：用户明确要求（m00042）。
- D-5 不写 llm_call_audit：用户明确要求（m00042），并记录为显式例外。
- D-6 业务审计照记 conversation.rename：用户要求的是「这次模型调用不记录」；标题变更本身仍是需要留痕的事实，且已有 ACTION_RENAME 语义。已由用户确认（m00167）。

## 9. 原待复核项（已于 2026-10-10 拍板，m00167）

- U-1 历史会话不补标题 → 用户：历史的不管，存量演示会话直接全部删除。删除是一次性数据清理，不是代码路径（§7）。
- U-2 自动改名是否写业务审计 conversation.rename → 用户：业务审计要记，llm 审计不用。即保留 S-2.5 最后一条与 A-13、A-12。
- U-3 编号徽标的位置 → 用户：放前面。即标题左侧 #N（S-1.5）。

## 10. 风险

- 命名不再影响流何时关闭，也不再占用会话锁；代价是「回答已经结束、标题还是默认值」的一小段窗口（通常 1–3 秒），由前端的有界轮询兜住。轮询次数用尽而标题仍未变时，标题就停在默认值，不重试；但后台命名仍在服务端写库，下一次列表刷新（下一轮提交结束后的 settle、切换列表筛选、刷新页面）会把标题带回来。
- 后台命名是尽力而为：客户端在 final 之后立刻断开、或进程在命名完成前退出时，Starlette 会跳过后台任务，标题不更新（用户 m00042 的「失败就失败」）。
- titling 模块若引入循环导入，由 backend/tests/test_import_cycle.py 兜住。
- 命名调用**确实**经过 runtime.model（即 AuditedModelClient 包装器）；是 titling.py:55 的 `getattr(model, "inner", model)` 把它解包成裸适配器，llm_call_audit 才保持干净。若将来有人去掉这处解包，A-12 会变红——这正是该用例的用途。
- 用户消息原文会进入命名提示词，模型可能被诱导输出奇怪文本；由 clean_title 截断到 120，且不写进任何审计与日志。
