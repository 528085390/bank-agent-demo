# LLM 底座补充修复 Spec

状态：**补充修复**（待实现；增补 `2026-09-27-*` 四份 spec，**不替换、不重述**它们）  
日期：2026-10-03  
基线：`dev` @ `f89784107c3b2744da501193e3236e807dc81878`  
关联：`2026-09-27-conversation-llm-foundation-overview.md`、`2026-09-27-conversation-persistence-spec.md`、`2026-09-27-llm-audit-spec.md`、`2026-09-27-context-management-spec.md`

修订：2026-10-03 自审修订 —— 校正 5 处引用行号与 1 处 grep 结论，修正 §4.2 的变异方向与验收构造，把 §4.3 的占位上限换成具体数值，补 §2.3 的事务规则边界、写入点排除理由与迁移文档同步项。

修订：2026-10-03 范围收窄（S-2）—— 原先把 `backend/app/notification/*` 的五处日志点一并纳入，属**越界**：`2026-09-27-*` 四份 spec 从未提及邮件或通知（`grep -Ei '邮件|email|通知|notification'` 在四份 spec 中**零命中**），而审计 spec §5:58 自己列举运行日志归属时点的是 `backend/app/main.py`、`sse.py`、`tools/*`，**不含 `notification/*`**。S-2 因此收窄为**只限 Agent 自身路径**（工具边界与 Agent 运行时）的三处；五处通知日志移入 §3.2 (b) 的"越界观察"，保留记录但**不再作为本文件的验收项**。收窄后 S-2 的全部条目都落在 overview §1:11 的七个动作与审计 spec §5:58 列举的日志归属之内。

修订：2026-10-04 只做 LLM 治理（S-1 写入点）—— 按指示"先不管有关邮箱的东西，只做 llm 治理"，`PUT /settings/email` → `user_email.update` **移出 S-1 的写入点集合与全部验收标准**，改列为 §2.3"不进入首期集合的写入口"里的**推迟**条目（不是否决；审计 spec §2.3:35 的"业务或配置"文字上覆盖它，将来纳入须先改本文件）。S-1 的写入点因此只剩会话侧五个动作与 `llm_config.update`/`llm_config.clear`。至此**本文件不含任何邮箱相关内容**：S-2 的五处通知日志见 §3.2 (b) 的"越界观察"，S-1 的邮箱设置写入点见 §2.3 的推迟条目，两者都不写验收用例。

修订：2026-10-04 自审修订（与配套 plan 对读，并由一个独立子代理复核全部行号与"不存在"断言）—— 修掉 3 处自相矛盾：§2.3 的"恢复软删除会话**不新增**审计行"与紧随其后的"要写一条 `conversation.restore`"冲突（改为"不重放、不复制历史审计行"）；§2.3 未规定 `user_id` 的可空性而读取器一律按它过滤（补一段：列可空但写入器拒绝缺失）；§7 说指针句加在 overview §4 "**或**"审计 spec §2.3，而配套 plan 两处都加（改为"与"，并说明两处职责不同）。又修掉 4 处事实性措辞：§2.1 的"全仓仅两处文档命中"（本文件自身当然命中，原意是"四份既有 spec 里只有两处"）；§3.2 (b) 越界观察把 `email_service.py:51` 也算作"明文记录收件人与主题"（`:51` 只记计数与异常文本，实际是 `:47` 与 `:54`）；§4.1 的代码片段写成 `raise AppError(...)` 而非 `raise conversation_error(ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED)`；§4.4 第 1 行说 21 处命中"全部是造数据"（其中 `test_domain_agents.py:829` 是按函数名子串命中的测试定义）。另确认 §4.3 的上限数值与既有关系约束的算术自洽。

## 0. 本文件的定位与声明

本文件是一份**补充修复 spec**，不是新一期设计，也不是对既有 spec 的修订版。

2026-09-27 四份 spec 已按各自正文实现并冻结（持久化冻结点 `ed47abb`，上下文管理见该 spec §14）。2026-10-03 按 `2026-09-27-conversation-llm-foundation-overview.md` 逐条做了一次全量验收：全量测试 `3 failed, 752 passed, 2 skipped, 35 errors`，38 个非通过项逐条证明为云库冷连接抖动（同一条 `psycopg.OperationalError`、零断言帧、脱离 pytest 可复现、单独重跑 38/38 全绿），**没有发现任何代码缺陷**。

但验收同时确认了**若干既有 spec 已写、实现侧尚未落实或尚无见证**的条目。本文件把它们收拢成一份可实施的清单，并给出各自的验收标准。

三条纪律：

1. **本文件只增补。** 除 §2（S-1 的语义扩展）、§3（S-2 的收紧）与 §4.3（S-5 的新增绝对上限校验，§7 已注明这是唯一一处新增校验）这三处外，不改变 `2026-09-27-*` 任何既有条款。若某条与本文件冲突，以本文件为准，且实施时必须同步修订被覆盖的原文，不得让两份文件长期互相矛盾。
2. **只写"做什么"，不写"怎么做"。** 任务拆分与执行顺序由后续 plan 承担，本文件不指定提交粒度。
3. **每一条都必须可断言。** 凡本文件给出的验收标准，都要能用一条会变红的用例证明，且要说明"改哪里会变红"。无法断言的条目不得写进验收清单。

## 1. 验收结论：未落实项清单

| 编号 | 条目 | 性质 | 依据 |
|---|---|---|---|
| S-1 | `BusinessAuditEvent` 完全未实现 | **功能缺口**（唯一一处） | overview §4:48；审计 spec §2.3:33-39 |
| S-2 | Agent 自身路径的运行日志可能携带工具参数值 | **规范缺口**（实现与自身声明不一致） | overview §4:54；审计 spec §3:47 |
| S-3 | `unfinished` 恢复分支无测试 | 见证缺口 | overview §6:73 |
| S-4 | INV-1 只有单一见证 | 见证缺口 | 上下文管理 spec §10、§14.1 |
| S-5 | 上下文预算无绝对尺寸上限；两条 prompt 上限留余量 | 见证缺口 + 规范缺口 | 上下文管理 spec §4、§13 ⑬、§14.2 |
| S-6 | 三处"有实现无测试" | 见证缺口 | overview §1:12（隔离）、§3:34（不可猜测）、§3:38（thread_id 客户端不提交） |

**S-2 的范围**：只限 **Agent 自身路径**——工具边界（`backend/app/tools/*`）与 Agent 运行时（`backend/app/agents/runtime.py`）。`backend/app/notification/*` 的日志点**不在本文件范围内**，理由与移出清单见 §3.2 (b)。

§5 另附一条**已核销的误判**（"切换会话"），记录它为什么不进这份清单。

## 2. S-1 `BusinessAuditEvent`（唯一的功能缺口）

### 2.1 现状与证据

`2026-09-27-conversation-llm-foundation-overview.md:48` 把 `BusinessAuditEvent` 列为六类持久化记录之一：

```text
BusinessAuditEvent    Agent 自身真正改变的业务/配置动作；银行业务仍由 bank_backend 审计
```

`2026-09-27-llm-audit-spec.md:33-39`（§2.3）已给出定义、字段建议与边界：

> 只记录 Agent 服务自身改变的业务或配置，例如会话重命名、归档、删除恢复、LLM 配置修改。银行余额、转账、账单等业务动作仍由 `bank_backend.audit_logs` 记录。
>
> 字段建议：`event_id`、`user_id`、`conversation_id`、`run_id`、`request_id`、`action`、`resource_type`、`resource_id`、`result`、`reason`、`created_at`。
>
> Agent 侧不复制银行审计的 old/new 全量快照；通过银行返回的业务 ID、request ID 或 operation ID 关联。

实现侧**零落实**：

- grep `(?i)(business_audit|BusinessAuditEvent|businessaudit)` 于 `backend/` → **无匹配**（模型、迁移、路由、服务、测试全无）。四份 `2026-09-27-*` spec 里只有两处提到它（上引两行）；**这两处之外，仓库里再没有第三份文件描述过它**——本文件与配套 plan 是仅有的两处新增描述。（2026-10-04 自审修订：原文写"全仓仅两处文档命中"，但"全仓"把本文件自身与配套 plan 也算进去，而它们当然命中；原意是"四份既有 spec 里只有两处"，措辞已改正。）
- 迁移 `backend/app/storage/migrations/versions/` 的 `0007_conversation_persistence`、`0008_llm_interaction_audit`、`0009_context_summary` 均无对应表；当前 head 为 `0009_context_summary`。
- `backend/app/storage/models.py` 无对应模型；`backend/app/audit/` 无对应服务、路由或读取入口；`backend/tests/` 无任何用例。

其余五类记录均已实现：`ConversationMessage`（`backend/app/storage/models.py:175`）、`LlmCallAudit`（`:246`）、`AgentRunEvent`（`:294`）、`ContextSummary`（`:340`）、LangGraph checkpoint（`backend/app/storage/checkpointer.py:70`、`backend/app/main.py:44-45`）。

### 2.2 影响

- overview §1:14「模型调用、Agent/工具运行和**业务动作**可以按会话和运行追溯」目前只覆盖前两类。会话重命名/置顶/归档/软删除/恢复、LLM 配置修改这些 Agent 自己造成的业务与配置变更，只落在运行日志里。
- overview §1:15「普通运行日志与持久化审计分开，**不能互相替代**」在这一类动作上尚不成立：这类动作现在**只有**日志。
- 审计 spec §7:68-72 的五条验收标准不覆盖 `business_audit_event`，所以既有测试全绿并不能说明这一条已达成。这是"spec 写了、验收没写、实现没做"的三重空档。

### 2.3 目标行为

**表与迁移**

- 新增迁移 `0010_business_audit_event`，`0009_context_summary` 之后成为 head。
- 新增模型 `BusinessAuditEvent`，列按审计 spec §2.3 的十一个字段：
  `event_id`、`user_id`、`conversation_id`、`run_id`、`request_id`、`action`、`resource_type`、`resource_id`、`result`、`reason`、`created_at`。
- `event_id` 由 `new_id("baud")` 生成（沿用 `backend/app/core/types.py:14-15` 的 `f"{prefix}_{uuid4().hex}"` 形态），唯一。
- `conversation_id`、`run_id`、`request_id` 是**关联列而不是外键**，沿用 `backend/app/storage/models.py:253-255` 已经写明的取舍（"correlation columns are plain strings rather than foreign keys, on purpose"）。`conversation_id`、`run_id`、`request_id` 均可空——不是每个业务动作都发生在一个 run 里。
- **无 `updated_at` 列**，与 `ConversationMessage`（`backend/app/storage/models.py:175-206`，`:178` 注释 "A row is never edited: a correction is a new message."）同等不可变：更正就是追加一条新事件。
- 迁移落地时**必须同步 `docs/ARCHITECTURE.md`**：`:526` 的表格行（现为 "Alembic 迁移（HEAD = `0009_context_summary`）"）、`:542` 的迁移链末尾（现为 "…→ `0009_context_summary`（HEAD）"），以及 `:593` 那一形态的测试清单表（新增的迁移/审计测试文件按既有行格式补行）。另外 `backend/app/storage/migrations/versions/` 已有**文件名与 revision id 不一致**的先例（`0004_email_notification.py` 的 revision 实为 `0005_email_notification`），新迁移必须按 **revision id** 链接到 `0009_context_summary`，不要照文件名推断。

**写入点（首期最小集合）**

| 触发点 | 现有代码位置 | `action` |
|---|---|---|
| `PATCH /api/conversations/{id}` 改标题 | `backend/app/api/routes.py:231-242` | `conversation.rename` |
| `PATCH /api/conversations/{id}` 改置顶 | 同上 | `conversation.pin` |
| `PATCH /api/conversations/{id}` 改归档 | 同上 | `conversation.archive` |
| `DELETE /api/conversations/{id}` | `backend/app/api/routes.py:245-249` | `conversation.soft_delete` |
| `POST /api/conversations/{id}/restore` | `backend/app/api/routes.py:252-256` | `conversation.restore` |
| `PUT /settings/llm` | `backend/app/api/routes.py:135-146`（经 `backend/app/integration/llm_config.py:83 configure`） | `llm_config.update` |
| `DELETE /settings/llm` | `backend/app/api/routes.py:160-164`（经 `backend/app/integration/llm_config.py:98 clear`） | `llm_config.clear` |

`resource_type` 取 `conversation` / `llm_config`；`resource_id` 取 `conversation_id` 或配置名。一次 PATCH 同时改多个字段时，**每个真正发生变化的字段各写一条**，未变化的字段不写。审计 spec §2.3:35 的清单以"例如"开头，因此上表可以按同一口径扩展，但不得减少——尤其不得因为"改的是配置不是业务数据"而漏记 `llm_config`。

注意 `PUT /settings/llm` 会在 `configure` 内部触发一次模型探测调用（`backend/app/integration/llm_config.py:152 _build_and_probe`），因此它同时产生 `llm_call_audit` 与一条 `business_audit_event`：前者记录模型调用，后者记录配置变更，两者不可互相替代。

**不进入首期集合的写入口**，理由逐条写明，避免实施时随手扩大或缩小：

- `POST /api/conversations`（`backend/app/api/routes.py:189-193`，新建会话）："新建"这一事实由 `conversation` 行本身承载，且审计 spec §2.3:35 的示例清单（重命名、归档、删除恢复、LLM 配置修改）未包含它。
- `POST /settings/llm/test`（`backend/app/api/routes.py:149-157`，连通性探测）：只读不写配置；它触发的模型调用已由 `llm_call_audit` 记录。
- `POST /api/conversations/{id}/messages` 及其恢复入口：会话内容而非配置变更，由 `ConversationMessage` 与 `AgentRunEvent` 承载。
- `PUT /settings/email`（`backend/app/api/routes.py:167-173`）：**邮箱相关一律不做**（2026-10-04 指示"先不管有关邮箱的东西，只做 llm 治理"）。这一条只是**推迟**，不是否决——审计 spec §2.3:35 的"业务或配置"在文字上覆盖它，将来若要纳入，属于扩大本集合，须先改本文件再实现。本文件与配套 plan 的验收清单均不含它。

若后续要把新建会话纳入，属于**扩大**本集合，须先改本文件再实现。

**写入语义**

- **DB 支撑的变更**（`conversation.*`）的审计写入与业务变更在**同一次 `db.commit()`** 内提交：要么两者都生效，要么都不生效。注意 `backend/app/conversation/service.py:406-427 update` 等方法自带 `with self.database.sessions.begin() as db:`，实施时必须让审计写入复用同一个 session，不能另开一个事务。
- **内存变更不能事务化**（`llm_config.update`、`llm_config.clear`）：`backend/app/integration/llm_config.py:83-107 configure`/`clear` 只改 `self._config`、`self._mode` 并调用 `self._runtime.agent.set_model(...)`，**没有任何 DB 写**，因此不存在可共享的事务。对这两个端点，规则改为**顺序**：先在同一请求内把审计行提交成功，再让内存变更生效；审计写入失败则请求失败且配置保持原值。禁止"先改内存、审计失败后吞掉"。
- 审计写入失败必须让整个请求失败，沿用 `backend/app/conversation/turns.py:110-121 _record` 已经确立的 fail-closed 立场（该处 docstring 明确 "a run must not quietly degrade into 'only the log knows'"）。**禁止** `try/except: pass`。
- 被拒绝的请求（越权 → 404、已归档 → `CONVERSATION_NOT_WRITABLE`）发生在业务变更之前，**不写审计行**。这一取舍在此钉死，避免实现时两种做法各写一半。
- `user_id` 列**可空**，与 `LlmCallAudit.user_id`（`backend/app/storage/models.py:274`）和 `AgentRunEvent.user_id`（`:321`）一致；但**写入器拒绝缺失 `user_id`**（抛 `ValueError`，不是 `AppError`），因为读取器一律以 `where(BusinessAuditEvent.user_id == user_id)` 起始，一行 `user_id IS NULL` 的记录对任何调用者都永远不可见——与其静默写一行死数据，不如让写入点当场失败。其余列（`event_id`/`action`/`resource_type`/`result`/`created_at`）**非空**。
- 软删除 conversation **不删除**其审计记录，只改变普通用户可见性（审计 spec §6:64）。恢复软删除会话**不重放、不复制**历史审计行——恢复这个动作本身要写一条新的 `conversation.restore`，但被恢复的历史事件不重写、不复制。（2026-10-04 自审修订：原文写作"不新增审计行"，与紧随其后的"要写一条 `conversation.restore`"直接冲突；"不新增"说的是**不重放历史**，不是"恢复动作本身不留痕"。）

**访问与日志**

- 不开放给普通会话接口（overview §6:71）。查询入口沿用 `backend/app/audit/routes.py` 的受控形态：路由只做身份提取（`backend/app/audit/routes.py:64 @router.get("/llm-calls")` 与 `:98 @router.get("/run-events")` 把 `who["user_id"]` 交给 service，**该文件全文没有 `.where(`**），真正的归属过滤写在 service 里（`backend/app/audit/service.py:364` 用于 `LlmCallAudit`、`:408` 用于 `AgentRunEvent`）。新增的读取方法须以 `where(BusinessAuditEvent.user_id == user_id)` 起始，支持按 `conversation_id`、`action`、时间范围分页查询。
- 运行日志只记 `event_id`、`action`、`resource_id`，不记 old/new 快照（overview §4:54）。

**边界（不得越过）**

- 不复制银行审计：银行余额、转账、账单仍只在 `bank_backend.audit_logs` 记录（审计 spec §2.3:35、§7:72）。Agent 侧只通过银行返回的业务 ID / request ID / operation ID 关联，不写 old/new 全量快照。
- 不引入 `conversation_run` 或任何运行状态投影（overview §6:72）。

### 2.4 验收标准

1. 迁移往返：`0010_business_audit_event` 的 upgrade → downgrade → upgrade 通过；`alembic heads` 为 `0010_business_audit_event`。形态对齐 `backend/tests/test_audit_migration.py` 既有用例。
2. 写入覆盖：对 `PATCH`（改标题 / 改置顶 / 改归档各一次）、`DELETE`、`restore`、`PUT /settings/llm`、`DELETE /settings/llm` 发请求，`business_audit_event` 每次新增**恰好一行**，`action` 与 `resource_id` 与请求一致，`result == "ok"`（`result` 的取值域钉死为 `"ok"` / `"rejected"` / `"failed"`，本期只会写入 `"ok"`，因为被拒与失败都不留行，见验收 3 与 4）；`PUT /settings/llm` 那次同时新增恰好一条 `llm_call_audit`（探测调用），两条记录不互相替代。**不含 `PUT /settings/email`**（见 §2.3 的推迟条目）。
3. 被拒请求不写审计：越权 `PATCH` 得到 404、已归档会话发消息得到 `CONVERSATION_NOT_WRITABLE`，两者都不新增审计行。
4. fail-closed：注入审计写入异常后——对 DB 支撑的端点（`PATCH`/`DELETE`/`restore`），端点返回 5xx 且业务变更**未提交**（回滚断言）；对内存端点（`PUT`/`DELETE /settings/llm`），端点返回 5xx 且 `llm_config` 保持原值（调用前后各读一次配置，断言不变）。
5. 不可变性：`"updated_at" not in columns`（对齐 `backend/tests/test_conversation_messages.py:24` 用例内 `:29` 的断言写法）。另外做一次**评审检查**（不是测试）：grep 全 `backend/app` 无 `db.delete(BusinessAuditEvent`、无 `DELETE FROM business_audit_event`。grep 不能变红，因此它不构成验收，只作为实施时的自检项。
6. 删除不影响审计：软删除 conversation 后其审计行仍可读；恢复后仍只有原行加上恢复动作新增的那一行，历史行不新增。
7. 不外泄：`GET /api/conversations/{id}`、`.../messages` 与 SSE 事件字段闭集（`backend/app/agents/runtime.py:37-44 EVENT_FIELDS`）中不出现审计字段或 old/new 快照。
8. 变异实验：删掉任一写入点 → 验收 2 变红。验收 4 的 fail-closed 承载者要分两半说：对 `conversation.*`（`db=db`）由**共享事务**承担，不由 `record_business_event` 里那个 `raise` 承担——审计行经 `db=db` 与业务变更写进同一个 session，失败的 `flush()` 会毒化该 session，`with self.database.sessions.begin()` 退出时回滚，业务改动照样不落地、端点照样 5xx。因此「把审计写入改成 `try/except: pass`」**在 `db=db` 下不会让验收 4 变红**（2026-10-04 实测，三条 fail-closed 用例仍全绿）；让验收 4 变红的有效变异是**把审计写入移到另一条连接并吞掉异常**——即 §2.3:118 明令禁止的 best-effort 反模式——此时三条用例以「**200 且改动已提交**」失败。对内存端点（`PUT`/`DELETE /settings/llm`）则相反：它们以 `db=None` 调用 `record_business_event`，走 `backend/app/audit/service.py:282-285` 的 `with self.database.sessions.begin():`，**自起事务并提交**，没有共享事务可依赖，`backend/app/audit/service.py:295` 的裸 `raise` **就是唯一承载者**——`backend/app/integration/llm_config.py:93-97` 与 `:129-134` 的 docstring 自陈："there is no row to commit the swap alongside and no shared transaction to lean on … A change that cannot be recorded is a change that does not happen; that ordering is the whole guarantee."。（2026-10-04 更正：本条原文写「把审计写入改成 `try/except: pass` → 验收 4 变红」，实测不成立，详见 §9.2.1。）
9. 日志与审计不可互替（overview §1:15 目前缺反例见证）：构造一次审计写入失败的请求，断言请求失败、`business_audit_event` 无新行，且运行日志里留下一条带 `event_id`/`action` 的失败记录——即"只有日志知道"这种降级**不会**被当成成功；再断言该次失败没有被任何接口以 2xx 返回。

## 3. S-2 运行日志可能携带工具参数值

### 3.1 现状与证据

overview §4:54 要求：「普通日志只记录 payload 的摘要、长度、哈希或关联 ID，**不写完整 prompt/response**」。审计 spec §3:47 收紧为：「运行日志只带 ID、状态、耗时、错误类型和摘要哈希；**禁止写完整 prompt/response、API key、验证码和未脱敏账号**」。

全量验收逐条核对了 `backend/app` 的 37 个 logger 调用点，**无一处**传完整 prompt/response。唯一直接见证是 `backend/tests/test_output_schema.py:97`（`caplog` 断言哨兵串不在日志里）。

但发现两处不一致：

**（a）`gateway` 的拒绝日志会带上违规参数值**

```python
# backend/app/tools/gateway.py:58
logger.warning("tool call denied: %s", reason)

# backend/app/tools/gateway.py:313
return f"argument {location}: {first.message}"
```

`first.message` 是 jsonschema 的校验消息，**内嵌违规值本身**。仓库自己在 `backend/tests/test_output_schema.py:100-102` 写明了这个行为。实测（`Draft202012Validator` 校验 `{"amount": "SECRET_AMOUNT_9911"}`）：

```text
MSG: 'SECRET_AMOUNT_9911' is not of type 'integer'
```

同一个仓库的另一侧却明确脱敏——`backend/app/tools/bank_adapter.py:132` 的 docstring 写着 "the path and the failing keyword, never the value"（`:131` 是 `def _facts_violation` 签名）。两个工具边界的标准不一致。

现有测试只覆盖不含值的抱怨：`backend/tests/test_tool_catalog.py:143-144` 断言的是参数**名**（`"key"`、`"surprise"`）。

**（b）两处 `logger.exception` 记录完整 traceback**

`logger.exception`（保留 traceback，异常对象可能携带业务载荷）：

- `backend/app/tools/bank_adapter.py:97` —— `logger.exception("tool %s raised", name)`。同文件 `:95-96` 的注释自认异常可能携带业务数据或凭证；而 `:132` 的 docstring 又写着 "the path and the failing keyword, never the value"。同一文件内两种口径并存。
- `backend/app/agents/runtime.py:469` —— `logger.exception("could not resolve the bank customer for %s", user_id)`，同时明文记录 `user_id`。

审计 spec §3:47 明令禁止写"未脱敏账号"。这两处都属经评审或将评审的有意例外，但**没有任何用例证明它们不泄露**，也没有文档写明豁免范围。

**越界观察（移出本 spec，不作验收项）**：全量核对 37 个 logger 调用点时，另在 `backend/app/notification/*` 发现五处同类问题——`queue.py:100`、`service.py:237` 的 `logger.exception`（保留 traceback），以及 `email_service.py:47` 与 `:54` 明文记录邮件收件人地址与主题（这两处的收件人地址属审计 spec §3:47 的"未脱敏账号"；`:51` 只记 `attempt`/`retry_times` 与异常文本，不含收件人或主题）。这五处**不属于本文件的修复范围**：四份 `2026-09-27-*` spec 从未提及邮件或通知（grep `邮件|email|通知|notification` 零命中），审计 spec §5:58 列举运行日志归属时点的是 `backend/app/main.py`、`sse.py`、`tools/*`，**不含 `notification/*`**。记录在此以免丢失，由通知模块自己的 owner 处置；**本文件的验收清单不含这五处**。

### 3.2 目标行为

- （a）`backend/app/tools/gateway.py` 的拒绝日志改为只记**参数位置路径**、**校验关键字**（`first.validator`）与 `call_id`，不记 `first.message`。若需要保留可读性，重写为只含 keyword 与 `schema_path` 的短串，**不得**包含 `first.instance` 或 `first.message`。
- （b）上列**两处**逐个给出结论，二选一，并在本文件中钉死：
  - 改为 `logger.error("...: %s", error_code_for(exc))` 形态（对齐 `backend/app/audit/model_client.py:177-187 error_code_for` 的既有做法）；或
  - 保留原样，但写明豁免理由，并补一条"该异常消息含哨兵串时哨兵不出现在日志里"的用例。

  未逐个给出结论前，不得声称"日志不写 payload"已完全达成。**（b）的结论只需覆盖两处**；§3.2 (b) 的"越界观察"五处不在验收范围内。

### 3.3 验收标准

1. 构造一个参数值含哨兵串（如 `SECRET_AMOUNT_9911`）的非法工具调用，用 `caplog` 断言哨兵串**不在**日志文本里，且日志里**有** `call_id` 与违规参数**名**。写法对齐 `backend/tests/test_output_schema.py:97` 与 `backend/tests/test_tool_catalog.py:143-144`。
2. 上列**两处**各有一个"异常消息含哨兵串"的用例，断言哨兵串不出现在 `caplog`；被豁免的点在本文件里列名并写明理由。**越界观察的五处不在验收范围**，本文件不为它们写用例。
3. 变异实验：把 `first.message` 改回日志 → 验收 1 变红。

## 4. 见证缺口（S-3 ~ S-6）

以下四条**不需要新的产品行为**，只需要把已经写在 spec 里的性质变成会变红的用例，或补上缺失的上限。

**行号时效**：本节与 §0–§3 引用的行号以 spec 成文时（2026-10-03）的 HEAD 为准；实施期间 Task 10 往两个测试文件插入了用例（`backend/tests/test_conversation_api.py` +53/−1、`backend/tests/test_context_summary.py` +78/−0），因此这些行号对当前 HEAD 已不成立。**对当前 HEAD 成立的行号见 §9**；本节的行号保持原样，不再逐个回改。

### 4.1 S-3 `unfinished` 恢复分支无测试

**现状**：`backend/app/agents/runtime.py:341-344` 的分支

```python
if unfinished:
    raise conversation_error(ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED)
```

其中 `unfinished = tuple(getattr(state, "next", ()) or ())` 来自 `:260-270 _pending`（同处取 `state = await graph.aget_state(config)`、`interrupts = tuple(getattr(state, "interrupts", ()) or ())`）。grep `unfinished` 于 `backend/tests/` → **零命中**。该分支正是 overview §6:73「无法证明安全的未完成图调用不自动重放」的落点，结论目前只能由读码得出。

**目标行为**：不改代码，只补测试。

**验收标准**：一个用例，写一个"图已排好下一步（`state.next` 非空）但没有 interrupt"的 checkpoint，调用恢复准入，断言得到 `EXECUTION_RECOVERY_REQUIRED`（`backend/app/core/errors.py:44`，status 409），且 `pending_interrupt is None`、`recovery_required is True`、不产生新消息。断言形态对齐 `backend/tests/test_conversation_api.py:512-552`。

构造提示：这种 checkpoint 需要**手工写**，不能靠正常调用产生。既有先例是 `backend/tests/test_interrupt_reset_integration.py`（用 `aupdate_state(..., as_node=...)` 在同一 saver、同一 thread 上造出带 `next` 的检查点）；实施时须先确认该手法确实能让 `state.next` 非空而 `state.interrupts` 为空，若不能就先证明这一点再写用例。

**注意**：`backend/tests/test_conversation_api.py:512-552 test_an_unnameable_pause_reports_that_recovery_is_required` 走的是 `:307 if interrupts:` 分支里「interrupt 存在但不可命名」那条路（`:526-529` 的节点 `interrupt({"kind":"input","display":"没有编号的问题"})` 没有 `question_id`），**不是** `:341-344` 的 `unfinished` 路；不得把它当成该分支已被覆盖的证据。

变异：把 `:341-344` 的 `raise` 改成 `pass` → 该用例变红。

### 4.2 S-4 INV-1 只有单一见证

**现状**：`backend/app/context/summary.py:153` 是 INV-1 的唯一守卫；唯一见证是 `backend/tests/test_context_summary.py:475 test_a_summary_covering_the_current_message_is_not_valid`（用例定义在 `:475`，触发守卫的断言在 `:481`）。上下文管理 spec §14.1 结尾已如实写明这一点。plan 的 §14 要求（`docs/superpowers/plans/2026-09-28-context-management-plan.md:2244`）是每种性质至少要在**两条互相独立**的用例上变红。

**目标行为**：补第二条**独立**见证，且必须来自另一条路径——不是"摘要校验"侧，而是"视图组装"侧。

**验收标准**：在**视图组装**路径上补第二条独立见证。构造方式必须让守卫真正被触发：令某一版摘要的 `covered_to_sequence` **等于**本次组装的 `current_sequence`（即"刚发出的这条消息已被摘要覆盖"这一非法状态），然后断言组装出的视图**含**这条消息的原文（对齐上下文管理 spec §13 验收 ⑥）。手工造版本可直接 `db.add(ContextSummary(...))`，写法见 `backend/tests/test_context_summary.py:488-503`。

**不能**写成"构造一条覆盖当前消息的摘要，再追加一条更新的用户消息"——追加之后 `covered_to_sequence < current_sequence`，守卫根本不会被求值，这样写出来的用例不是见证。

**也不得**把 `backend/tests/test_context_acceptance.py:179 test_inv1_a_message_added_after_a_summary_is_in_the_view` 当成已有的第二条腿：该用例在 `:190` 自己断言 `summary.covered_to_sequence < newest_message(...).sequence`，摘要合法地早于当前消息，`backend/app/context/summary.py:153` 不会被求值。**在补齐真正的第二条腿之前，任何文档、报告或提交信息都不得声称 INV-1 已有两条见证或已被充分覆盖。**

新用例与 `backend/tests/test_context_summary.py:475` 机制不同（不同文件、不同断言路径）。

变异：**删掉/中和** `backend/app/context/summary.py:153` 的拒绝分支（`if newest.covered_to_sequence >= current_sequence: return None`），使守卫永不触发 → **两条腿都**变红。**方向不能写反**：把该判定改成"恒真"会让它永远 `return None`，`backend/tests/test_context_summary.py:481`（断言 `is None`）**仍然绿**，变异存活，因此那不是有效变异。

### 4.3 S-5 上下文预算的绝对上限与两条余量盲区

**现状**：

1. `backend/app/context/budget.py:85-107 validate()` 与 `backend/app/core/config.py:71-80` 只做**关系约束**（`recent_message_budget >= summary_trigger`、`summary_max_tokens >= input_budget`），**没有绝对尺寸上限**：把 `model_context_limit` 配成 10 000 000（`input_budget` 是它的派生值，见 `backend/app/context/budget.py:50-54`），进程照样启动。
2. `backend/tests/test_context_acceptance.py:389` 断言 `estimate_message_tokens(SUMMARY_PROMPT) <= 300`（实测 269）→ 盲区约 **31 个 CJK 字符**；`:390-392` 断言模板 `<= 60`（数值在 `:391`，实测 35）→ 盲区约 **25 个 CJK 字符**。这是"护栏而非等号"的自觉取舍（上下文管理 spec §14.2 已记录），但盲区大小只存在于报告里，没有进入 spec。

**目标行为**：

- 给预算加**绝对上限**，使"把预算配成任意大"在进程启动即失败。
- 把两条 prompt 上限的余量从"人肉记得"变成 spec 里的明确数值与依据。

**验收标准**：

1. `validate_context_budget` 对超出上限的配置抛 `ValueError`，进程启动失败（形态对齐 INV-11 的既有用例，见上下文管理 spec §13 验收 ⑬）。
2. 上限取值在本文件钉死，依据随附（现默认值见 `backend/app/core/config.py:64-69`）：

   | 配置项 | 现默认值 | 绝对上限 | 依据 |
   |---|---|---|---|
   | `model_context_limit` | 128000 | **2000000** | 目前公开可得的最大上下文窗口在 1M–2M token 量级；2 000 000 已给默认值留 15.6× 余量，再往上不可能是真实模型窗口，只能是配置错误 |
   | `recent_message_budget` | 32768 | **1000000** | 它不得超过 `input_budget`，而 `input_budget <= model_context_limit`；取与上一行同量级，只挡"数量级错误"，不冻结当前取值 |
   | `summary_max_tokens` | 1024 | **100000** | 同上；且它必须小于 `input_budget`，自洽性已由既有关系约束兜住 |

   `reserved_output_tokens`、`safety_margin`、`summary_trigger_ratio` 已由 `ge=0` / `gt=0, lt=1` 约束，不加绝对上限。测试用**常量**断言（`<= 2000000` 等），不写等号。
3. 明确记录残余盲区的大小，并声明"小于余量的增长不会被抓"是已知取舍。

### 4.4 S-6 三处"有实现无测试"

| # | 实现 | 现状 | 验收标准 |
|---|---|---|---|
| 1 | `conversation_id` 不可猜测：`backend/app/core/types.py:14-15 new_id` = `f"{prefix}_{uuid4().hex}"` | grep `new_id\|unguess\|uuid4` 于 `backend/tests/` 有 **21 处**命中，**无一处断言 ID 的形态**。其中 20 处是造数据（`uuid4` 见 `backend/tests/conftest.py:2,115,156`、`test_audit_api.py:9,36`、`test_conversation_api.py:8,27,172`、`test_conversation_migration.py:348,358` 等）；第 21 处是**测试函数名**里的子串——`backend/tests/test_domain_agents.py:829 test_a_new_question_gets_a_new_id_but_keeps_counting_attempts()`，断言的是"新问题换新 id、attempts 继续累加"，与 ID 的**形态**无关。（2026-10-04 自审修订：原文写"全部是造数据"，把这条按名字命中的测试定义也算了进去。） | 断言 `new_id("conv")` 的形态（`conv_` + 32 位十六进制），两次调用不相等；断言两个不同用户的 `conversation_id` 不相等，且都不等于其 `session_id`（**不是**"不能由 `session_id` 推导"——那是不可断言的性质） |
| 2 | 摘要跨会话隔离：`backend/app/context/summary.py:139-141` 有 `conversation_id` 过滤；`backend/app/storage/models.py:340-367` 有 `UniqueConstraint("conversation_id","version")` | `backend/tests/test_context_summary.py` 的 21 个用例**全部单会话**，无跨会话断言 | 同一 `user_id` 下两个 conversation 各触发一次摘要，断言 A 的读取不返回 B 的版本，且 B 的 `covered_from/to` 不被 A 影响 |
| 3 | 提交 `thread_id` 被拒：`backend/app/api/schemas.py:6-7 RequestModel extra="forbid"`；`backend/app/conversation/service.py:131-142 as_dict()` 不含 `thread_id` | 无"客户端提交 `thread_id` 被拒"的用例——若某 schema 被改成 `extra="ignore"`，现有测试不会红 | 向 `POST /api/conversations/{id}/messages` 的 body 里塞 `thread_id`，断言 422 且 `code == "INVALID_PARAMETER"`（`backend/app/main.py:80-92` 的 `RequestValidationError` 处理器）、`details.fields` 里出现 `thread_id`，且不写 submission/message、不调用图 |

第 3 条另有旁证：`backend/tests/test_conversation_api.py:695` 断言 SSE 载荷不含 `thread_id`/`conv:`/`session_id`/`user_id`/`conversation_id`；`backend/tests/test_conversation_repository.py:39` 断言 `"thread_id" not in created.as_dict()`。这些覆盖的是**出站**方向，入站方向仍空缺。

## 5. 已核销的误判：会话"切换"

2026-10-03 的初次验收报告曾把"切换会话"列为缺口，理由是后端 grep `(?i)(switch|activate|select_conversation|current_conversation)` 无匹配。**该结论不成立，特此撤回并记录理由**，避免后续被重新打开：

- 切换是**客户端动作**，不是后端资源。后端契约只需要"列出会话 + 按 id 读取"，这两条已在 `2026-09-27-conversation-persistence-spec.md:110-111`、`:115` 定义并实现。
- 前端已实现且已测试：`frontend/src/App.vue:220 async function switchConversation(conversation: Conversation)`、`:590 @click="switchConversation(item)"`、`:487-488`（按 `lastUsed` 恢复上次会话）、`:160`（"Switching conversations must not carry anything across"）。
- 前端测试：`frontend/src/App.test.ts:279 describe('conversation switching', ...)`、`:290 it('carries no view state across a switch')`、`:320 it('does not leave the turn that was in flight over the conversation switched to')`。
- 持久化 spec §8:177 已把切换列为变异实验之一：「切换会话时不清空在途回合 → 上一个会话的回合渲染到新会话上」。

因此"切换"**不进 §1 清单**，也不需要在后端新增端点。

## 6. 明确不做（本补充修复不改变的边界）

以下均为 `2026-09-27-conversation-llm-foundation-overview.md` §6 的既有非目标，本文件**不**重新讨论、不松绑：

- 不做多 worker 分布式锁、跨实例事件恢复或任务队列（overview §6:68）。
- 不引入向量数据库和语义检索（overview §6:69）；不实现上下文管理 spec 中被称为"方案 B"的路线。
- 不把 `bank_backend` 的 `AuditLog` 迁移到 Agent 数据库（overview §6:70）。
- 不把完整 prompt/response 暴露给普通前端会话接口（overview §6:71）。
- 不创建第二套 run 状态机，不实现 retry/attempt fencing、任意崩溃后自动重放、人工副作用核对、写工具意图账本、SSE durable replay 或跨进程接管（overview §6:72）。
- 服务重启后只恢复 checkpoint 明确可恢复的 interrupt（overview §6:73）。
- 不改上下文管理的摘要算法、分块策略与版本链模型；本文件对上下文管理只补 S-4 的见证与 S-5 的上限。
- 不改 `backend/app/notification/*`：邮件通知既不出现在四份 `2026-09-27-*` spec 中，也不在 overview §1:11 的七个动作里；S-2 的"越界观察"只记录、不修，不写验收用例。
- **不做任何邮箱相关的事**（2026-10-04 指示）：`PUT /settings/email` 的审计写入点、`backend/app/notification/*` 的日志脱敏，全部推迟，不在本文件的写入点集合与验收清单里。本文件只做 LLM 治理与会话侧审计。

## 7. 与既有 spec 的关系

- **唯一一处语义扩展**是 §2（S-1）：overview §4:48 只"列出"了 `BusinessAuditEvent`，本文件把它变成可实施的表、写入点与验收标准。实施时应同步在 overview §4 **与**审计 spec §2.3 各加一句指向本文件——两处都要加，因为 overview §4:48 是"有哪些记录类型"的清单、审计 spec §2.3:33-39 是"这张表长什么样"的定义，只改一处会让读者在其中一边找不到下文。（2026-10-04 自审修订：原文写"或"，与配套 plan 里两处都加的做法不一致。）
- **唯一一处收紧**是 §3（S-2）：把 overview §4:54 的"日志不写 payload"从设计声明变成可断言的用例，并把 **Agent 自身路径上**的已知不一致（`gateway` 拒绝日志会带上违规参数值；`bank_adapter.py:97` 与 `agents/runtime.py:469` 的 `logger.exception`）逐条列出。`backend/app/notification/*` 的五处同类问题见 §3.2 (b) 的"越界观察"，**不在本文件范围**。
- §4 中 S-3、S-4、S-6 是纯见证补强，不改任何生产行为；**S-5 的绝对上限是新增校验**，是 §4 里唯一的例外，§0 纪律 1 已把它与 §2、§3 并列列出。
- 本文件给出的验收标准可独立于既有 spec 的验收清单执行；实施完成后，结论应写回本文件的"实现与验证"小节（届时新增），而不是改写 §0 的声明。

## 8. 实施顺序建议

S-1（唯一功能缺口，需迁移与模型）→ S-2（安全相关，改的是日志文本）→ S-3 → S-4 → S-5 → S-6。

S-2 与 S-1 无依赖，可并行；S-4 与 S-5 都落在 `backend/app/context/`，建议同一人顺序做，避免 `summary.py` 上的并行改动。

## 9. 实现与验证

本节是 §7 最后一条要求的落点。它**不改写 §0 的声明**，只记录十个 Task 实际做了什么、每条验收由什么见证、以及哪些事**没有**做。除本节之外，本文件有两处被就地更正或补充：§2.4 验收 8 的变异描述（见 §9.2 验收 8），以及 §4 正文开头新增的行号时效说明（对当前 HEAD 成立的行号已在本节 §9.1/§9.2/§9.4 逐条列出）；其余章节一字未动。

### 9.1 落地提交与一句话结论

| 项 | Task | 提交 | 结论 |
|---|---|---|---|
| S-1 | 1 | `98a532b` `03b65db` | 新增迁移 `0010_business_audit_event`（`down_revision = "0009_context_summary"`，revision id 与文件名一致）与模型 `BusinessAuditEvent`；`docs/ARCHITECTURE.md:526`（HEAD）、`:542`（迁移链）、`:600`（测试清单）同步。`03b65db` 是评审后的一条 docstring 更正（R51：模型的 `result` 注释与"本期只写 `ok`"的封闭取值域冲突，改注释不改行为）。 |
| S-1 | 2 | `ba270a5` | `AuditService.record_business_event` 与读取器落地：`db` 由调用方传入时只 `add`+`flush` **不 commit**（`backend/app/audit/service.py:279-281`），`db is None` 时才自己开事务（`:283-285`）；读取器一律以 `where(BusinessAuditEvent.user_id == user_id)` 起始（`:563`）；只读路由 `GET /api/audit/business-events` 先做归属校验（`backend/app/audit/routes.py:114-116`）。 |
| S-1 | 3 | `b06d069` `37f979b` | `conversation.rename`/`pin`/`archive`/`soft_delete`/`restore` 五个写入点接上，审计行与业务变更共用同一个 session（`db=db`）；`37f979b` 修掉验收 7 里"消息页那一半是空洞断言"的 Important 发现（只改测试，+16/−0）。 |
| S-1 | 4 | `8520790` `6d22d63` `672517a` | `llm_config.update`/`llm_config.clear` 两个内存写入点：`configure(..., on_change=...)` 在探测成功之后、`with self._lock:` 之前调用审计钩子，钩子抛异常则不生效；`6d22d63` 修掉钩子失败时探测 client 未关闭的问题并补 witness；`672517a` 收紧 docstring 措辞。 |
| S-2 | 5 | `d0d6a5a` `253af69` | `gateway._validate` 不再拼 jsonschema 的 `first.message`，改走新的模块级 `_complaint(error)`（`backend/app/tools/gateway.py:40-66`），只报路径、规则与键名；拒绝日志加 `call_id`（`:90`）。`253af69` 补上"`call_id` 真的到达日志"的见证（原交付物有一半无见证，R68）。 |
| S-2 | 6 | `409d2a7` | 两处 `logger.exception` 逐个结案，**都改**、无豁免：`backend/app/tools/bank_adapter.py:98` 改为 `logger.error("tool %s raised: %s", name, type(error).__name__)`，`backend/app/agents/runtime.py:469-471` 改为 `logger.error("could not resolve the bank customer for %s: %s", user_id, type(error).__name__)`；新增 `backend/tests/test_log_hygiene.py` 两条哨兵见证。 |
| S-3 | 7 | `4ebe104` | `unfinished` 恢复分支（`backend/app/agents/runtime.py:341-344`）有了见证，且证明"单节点图会让 witness 变空洞"这个陷阱被避开。 |
| S-4 | 8 | `1f97424` | INV-1 的第二条腿落在**视图组装**路径（`backend/tests/test_context_view.py:389`），与既有那条（`backend/tests/test_context_summary.py:553`）机制不同；此前任何"INV-1 已有两条见证"的说法都不成立，现在成立。 |
| S-5 | 9 | `8d18ddc` | 预算绝对上限落地：`model_context_limit ≤ 2000000`、`recent_message_budget ≤ 1000000`、`summary_max_tokens ≤ 100000`（`backend/app/context/budget.py:42-44`、`:127-142`），加在两条关系约束**之后**，`Settings` 经 `backend/app/core/config.py:71-80` 自动继承。 |
| S-6 | 10 | `b20be09` | 三处"有实现无测试"各有见证：`new_id` 形态与不碰撞、摘要的跨会话隔离、客户端提交 `thread_id` 被 422 拒绝。顺带按 R69 删掉 Task 7 里那条空洞的 `assert model.prompts == []`。 |

plan 本身另有四条 docs 提交：`c563776`（预执行扫描的十处 brief 缺陷）、`5643c36`（预执行前修正 Task 5 的文件清单与 Task 6 的先例引用）、`99bc898`（钉死 Task 4 的探测替身）、`babedc8`（钉死 Task 5 的 schema 违规形状，让 witness 有牙）。它们只改 plan，不改产品代码。

### 9.2 逐条验收的实测结果（含变异验证）

**§2.4 验收 1（迁移往返）** —— Task 1。`backend/tests/test_business_audit_migration.py` 在真库上跑 upgrade → downgrade → upgrade，断言表名、列名集合、索引名、`result` 的 CHECK 约束，并在 `:174` 以 `assert "updated_at" not in table.columns` 钉住不可变性。**残留**：Task 1 的红跑是一次 collection-time `ImportError`，没有任何个别断言被观察到变红——这是 brief 自己要求的"先看见红"形态的必然结果（模型还不存在）。

**§2.4 验收 2（写入覆盖）** —— Task 3 覆盖会话侧五个动作，Task 4 覆盖 `llm_config` 两个动作。每个写入点恰好新增一行，`action`/`resource_id` 与请求一致，`result == "ok"`；`PUT /settings/llm` 那次同时新增恰好一条 `llm_call_audit`，两条记录不互相替代。变异：**删掉 `restore` 写入点** → `3 failed`，`backend/tests/test_business_audit.py:463` 红（`assert ['conversation.soft_delete'] == ['conversatio...tion.restore']`）。Task 4 侧四次变异各自只杀死对应用例：**A** 删 `configure` 里的 `on_change()` 调用 → `2 failed`（`backend/tests/test_business_audit.py:704`、`:772`，`assert 0 == 1`）；**B** 把钩子移到 `_build_and_probe` 之前 → `backend/tests/test_business_audit.py:744` 红（`assert [{'action': 'llm_config.update', ...}] == []`，这是"先审计再生效"的钉子）；**C** 删 `clear` 的钩子 → `2 failed`（`:812`、`:869`）；**D** 去掉钩子失败路径的 `try/except` → `:920` 红；评审者自己加的 **E**（把安装块移到钩子之前）→ `:921` 红（`assert 'model' == 'unconfigured'`）。没有任何用例在"删掉它要见证的行为"之后仍然绿。

**§2.4 验收 3（被拒请求不写审计）** —— Task 3。越权 `PATCH` 得 404、已归档会话发消息得 `CONVERSATION_NOT_WRITABLE`，两者都不新增审计行。这三个路由在 `backend/app/api/routes.py:231-252` / `:255-269` / `:272-281` 都是先 `who = await identity(request)` 再开 `audit_context`，被拒发生在业务变更之前。

**§2.4 验收 4（fail-closed）** —— Task 3 覆盖会话侧，Task 4 覆盖内存侧。**这一条的承载者分两半：会话侧（`db=db`）由共享事务承担，内存侧由 `backend/app/audit/service.py:295` 的 re-raise 承担；详见下面验收 8 与 §9.2.1。** 会话侧：注入审计写入失败后端点返回 5xx 且业务变更未提交，回滚断言在 `backend/tests/test_business_audit.py:543`（改标题）、`:575`（软删除）、`:589`（恢复）三例。内存侧：`overlong_business_event_id(monkeypatch)`（`:677-694`，只 patch `prefix == "baud"` 的 `new_id`）让真的 `record_business_event` 在 `flush()` 时被真 Postgres 以 `StringDataRightTruncation`（`DataError`）拒绝，端点 503 且 `llm_config` 前后两次读取相等（`backend/tests/test_business_audit.py:759`、`:856`）。**这里必须记一笔**：模块级的 `overlong_event_id`（`:529-536`）对 PUT 用例不可用——它连 `new_id("aud")` 一起 patch，探测自己的审计行会先溢出，`_build_and_probe` 在 `backend/app/integration/llm_config.py:218` 把它转成 502，钩子根本不会跑；用它会得到红而不是假绿（`assert len(failures) == 1` 会失败），但红的原因是错的。

> 删掉 `backend/app/audit/service.py:295` 的 re-raise → `backend/tests/test_business_audit.py:781` 与 `:869` 以 `assert 200 >= 500` 变红（内存侧），三条会话侧用例仍全绿。

**§2.4 验收 5（不可变性）** —— Task 1。`"updated_at" not in columns` 是断言；grep 全 `backend/app` 无 `db.delete(BusinessAuditEvent`、无 `DELETE FROM business_audit_event` 只是评审自检项，grep 不能变红，**不构成验收**。

**§2.4 验收 6（删除不影响审计）** —— Task 3。软删除后审计行仍可读，恢复后只有原行加上恢复动作新增的那一行，历史行不重放、不复制。

**§2.4 验收 7（不外泄）** —— Task 3 与 Task 4。断言的是响应与 SSE 字段闭集同八个**审计专属**字段名（`AUDIT_ONLY_FIELDS`）不相交，不是字面的十一列：`ConversationView.as_dict()`（`backend/app/conversation/service.py:139-150`）合法发布 `conversation_id`/`created_at`，`MessageView.as_dict()`（`:167-178`）合法发布 `run_id`/`created_at`，字面十一列交集会让一个**正确**的实现变红（R59）。变异（评审者独立复现）：给 `MessageView.as_dict()` 加 `"reason": None,` → `backend/tests/test_business_audit.py:631` 红（`assert {'reason'} == set()`）。这条 Witness 本身曾是空洞的——`conversation_for` 只调 `create()`，消息页为空，`set(keys_of(page))` 恒为 `set()`——`37f979b` 补了真实消息行与结构性非空守卫（`:626`），并由评审者独立复现确认。

**§2.4 验收 8（变异实验）** —— 已在本节更正，见下面"验收 8 的更正"。

**§2.4 验收 9（日志与审计不可互替）** —— Task 4。用同一个 `overlong_event_id` 注入点让**真的** `record_business_event` 内部失败，`backend/app/audit/service.py:286-295` 打出 `business_audit_write_failed event_id=... action=... resource_id=...`（三个 `%s`、三个参数、**不含异常文本**），随后裸 `raise`；异常由 `backend/app/main.py:99-107` 的 `ExceptionMiddleware` 处理成 503 且不重抛，`business_audit_event` 无新行。这里注入点必须落在 `record_business_event` **内部**（不是 monkeypatch 它的替身），否则那段日志代码根本不执行——这条正是 R67 裁决的理由。

**§3.3 验收 1（工具拒绝日志不含参数值）** —— Task 5。红跑 `1 failed, 18 passed in 1.58s`，`backend/tests/test_tool_catalog.py:165`（`assert secret not in caplog.text`），捕获到的旧格式行是 `WARNING app.tools.gateway:gateway.py:58 tool call denied: argument key: {'nested': 'SECRET_AMOUNT_9911'} is not of type 'string'`。变异（作者与评审者各做一次）：把 `_validate` 尾巴改回 `f"argument {location}: {first.message}"` → `:165` 再次红。

**§3.3 验收 3（变异实验）** —— 即上一条的变异。修复轮另加一条**互相独立**的见证：`gateway.py:90` 改回 `"tool call denied: %s"` → `backend/tests/test_tool_catalog.py:167` 红（`assert 'call_id=c-7' in 'tool call denied: argument key: failed type'`）；两次变异各自只杀死一条断言，另一条仍绿。

**§3.3 验收 2（两处日志点各一个哨兵用例）** —— Task 6。两处**都改**、无豁免。红跑 `2 failed in 2.88s`，`backend/tests/test_log_hygiene.py:34`（工具侧）与 `:84`（运行时侧），哨兵 `SENTINEL_LEAK_4c17` 在改前的渲染回溯里可见；评审者把两处还原后独立复现同一个 `2 failed in 2.51s`，并确认 `:33`/`:83` 的 `"RuntimeError" in caplog.text` 在改前就是绿的，属 guard 不是 witness。这一条是红在前任务，按 brief 不做变异实验。

**§4.1（S-3）** —— Task 7。绿在前：`2 passed, 27 deselected, 1 warning in 22.16s`，首跑即绿，断言未被削弱。变异 ①：注释掉 `backend/app/agents/runtime.py:341-344` 的 `raise` → `backend/tests/test_conversation_api.py:663` 红（`assert 200 == 409`）；变异 ②：去掉用例里的 `aupdate_state` 播种 → `:660` 红（`assert False is True`）。**brief 的一处预测不准**：变异 ① 只杀死 409 那条，不杀死 `recovery_required`——后者来自独立的 `recovery_hint` 路径（`backend/app/agents/runtime.py:351-361`）。净效果仍成立：用例里每条断言都至少被两次变异中的一次杀死。

**§4.2（S-4）** —— Task 8。绿在前：`uv run pytest tests/test_context_view.py -q` → `13 passed in 147.15s`，首跑即绿。变异（方向必须是"删掉守卫"而不是"让判定恒真"）：删掉 `backend/app/context/summary.py:153-154` 的 `if newest.covered_to_sequence >= current_sequence:` / `return None` → `backend/tests/test_context_view.py:441` 红，观察到 `AssembledView(estimated_tokens=11, retained_from_sequence=None, retained_to_sequence=None, reason='within_budget')`；`:445` 是第二条被变异杀死的见证（transcript 从 `"user: " + content` 变成 `'（暂无）'`）。评审者独立复现同一结果，并确认单条 `ContextSummary`（`version=1`、`base_summary_id=None`、`covered_from_sequence=1`）让 `backend/app/context/summary.py:210` 的 `while` 不进入、`:213`/`:215`/`:217` 三个 `_ChainBroken` 都不可能触发，所以 `:153` 是**唯一**的拒绝原因——这正是这次变异不可省的原因："链断了"和"守卫拦下了"都表现为同一个 `REASON_NO_VALID_SUMMARY`。

**§4.3（S-5）验收 1（绝对上限）** —— Task 9。两段式红跑（诚实形态）：(A) 只加 import 未加常量是 collection `ImportError`；(B) 只声明三个常量而不加校验 → `4 failed, 20 passed in 1.38s`，四条 `DID NOT RAISE`，落在 `backend/tests/test_context_budget.py:140`、`:151`、`:159`、`:182`。绿：`24 passed in 1.18s`（单文件）与 `58 passed in 307.17s`（三文件）。变异逐条删除，各自只杀死对应用例：删 `model_context_limit` 上限（`backend/app/context/budget.py:127-131`）→ `2 failed`（`:140`、`:182`）；删 `recent_message_budget` 上限（`:133-137`）→ `1 failed`（`:151`）；删 `summary_max_tokens` 上限（`:138-142`）→ `1 failed`（`:159`）；**D** 三个 `>` 全改 `>=` → `4 failed`（`:178` ×3 加 `:151`），边界用例非空洞。**假见证风险已在代码里写清并解释**：`test_a_recent_message_budget_beyond_the_absolute_ceiling_is_rejected` 故意同时把 `model_context_limit` 抬到上限，否则默认值下派生的 `summary_trigger` 是 85299，**既有**关系约束先触发且消息里就含 `recent_message_budget`，`match=` 会在新上限被删掉时照样通过；评审者用变异 B 证明了该注释成立。

**§4.3（S-5）验收 3（两条 prompt 上限的余量记档）** —— 这一条**没有任何 Task 承担**（plan 的 Task 9 只做了三条绝对上限），它只能落在本节，故在此补上：

- `backend/tests/test_context_acceptance.py:389` 断言 `estimate_message_tokens(SUMMARY_PROMPT) <= 300`，实测 **269** → 余量约 **31 个 CJK 字符**。
- 同文件 `:390-392` 断言用户模板 `<= 60`，实测 **35** → 余量约 **25 个 CJK 字符**。

这两条是"护栏而非等号"的自觉取舍（上下文管理 spec §14.2 已记录）：等号会冻结今天的措辞，而不是守卫它。**已知取舍**：小于余量的增长不会被抓——例如给 `SUMMARY_PROMPT` 再加 30 个 CJK 字符，这两条断言仍然全绿。该文件 `:385-388` 的注释已写明这一点，此处把它变成 spec 里的明确数值与依据。

**§4.4（S-6）** —— Task 10。绿在前，三条保证各由一次变异证明：**1** `backend/app/core/types.py:15` 改成返回常量 id → `backend/tests/test_contracts.py:79`（`assert first != second`）与 `backend/tests/test_conversation_repository.py:70`（第二次 `create` 撞 `conversation_conversation_id_key` 的 `UniqueViolation`）双双红；**2** 去掉 `backend/app/context/summary.py:139-141` 的 `.where(ContextSummary.conversation_id == conversation_id)` → `2 failed, 21 deselected in 16.49s`，`backend/tests/test_context_summary.py:505`（乙读到甲的摘要）与 `:549`（`assert '甲的摘要' == '乙的摘要'`）红——这是 brief 警告过的**唯一有效方向**，"让查找恒返回 `None`"会让 `is None` 那半边继续绿；**3** `backend/app/api/schemas.py:7` 的 `extra="forbid"` 改 `"ignore"` → `2 failed, 29 deselected in 18.27s`，`backend/tests/test_conversation_api.py:210` 与 `:232`（`assert 200 == 422`，响应是带脚本回复的 SSE `final`，即回合**真的跑了**）红。

**验收 8 的更正（本文件唯一一处就地更正既有措辞的地方）** —— 见下节。§4 正文开头新增的行号时效说明是**新增**，不是更正。

### 9.2.1 验收 8 的更正：`db=db` 下 fail-closed 由共享事务承担

§2.4 验收 8 原文写「把审计写入改成 `try/except: pass` → 验收 4 变红」。**这在 `db=db` 的实现下不成立**，2026-10-04 实测：审计行与业务改动共用同一个 session，失败的 `flush()` 已经毒化该 session，之后 `_message_count(db, conversation_id)` 一执行就抛（`ERROR app.main:main.py:101 database_error request_id=req_79022f6569b34723aea5d689ea0b5dfe type=InvalidRequestError`），`with self.database.sessions.begin()` 在退出时回滚，业务改动照样不落地、端点照样 5xx——**三条 fail-closed 用例在"吞掉异常"变异下仍全绿（`3 passed`）**。

让它们变红的有效变异是**把审计写入移到另一条连接并吞掉异常**，即 §2.3:118 明令禁止的 best-effort 反模式：此时三条用例以「**200 且改动已提交**」失败（`backend/tests/test_business_audit.py:551` 的 `"title":"改过的标题"`、`:579` 的 `"deleted":true`、`:595` 的 `"deleted":false`）。

所以结论是：**对 `conversation.*`（`db=db`），验收 4 的 fail-closed 由共享事务承担**，`record_business_event` 里那个 `raise` 是**纵深防御**，不是这一半的承载者；**对内存端点（`PUT`/`DELETE /settings/llm`，`db=None`，自起事务并提交），`backend/app/audit/service.py:295` 的 re-raise 是唯一承载者**——终审独立实测：删掉它后 `backend/tests/test_business_audit.py:781` 与 `:869` 以 `assert 200 >= 500` 变红（响应 200 且改动已提交），三条会话侧用例仍全绿。§2.4 验收 8 的措辞据此更正；实现与测试都不需要改，需要改的是这份 spec 自己的描述。

### 9.3 诚实的残留

- **Task 3 的 Step 2 红跑证据不可复核。** 只留下计数（`9 failed, 18 passed, 1 warning, 1 error in 260.72s`），`_e2e/` 里没有任何 console artifact；九个 node id 是从实现者报告重建的（只有第 9 条的断言体被逐字捕获，第 3 条被 pytest 截断成 `- As...`）。红跑的**存在**由评审者从代码独立推导过，内容不可审计。
- **Task 3 的 flake 重跑记录 `4 passed` 看似与三个 error 不符，实际正确。** `backend/tests/test_conversation_audit.py:457` 把 `event_type` 参数化在 `["TOOL_CALL", "TOOL_RESULT"]` 上，只有 `[TOOL_RESULT]` 抖动；不带 selector 点名该 id 会同时收集两条，所以三个 error id 重跑得到四个测试。
- **Task 4 的 3 文件绿跑（`96 passed … 664.67s`）没有被独立复现。** 评审者只重跑了 `backend/tests/test_business_audit.py`（`35 passed`）；计数与 `_e2e/task4-green.txt` 自洽，但那一次完整会话没有被第二个人看过。评审者也声明无法核实实现者"显式路径 `git add`"的说法，以及四个红跑断言行里除两个之外的行。
- **Task 1 的红跑是 collection-time `ImportError`**（见验收 1），没有个别断言被观察到变红。
- **Task 6 的日志点从此完全丢失回溯。** `backend/app/agents/runtime.py:469-471` 只记异常**类型名**，S-2 的取值纪律下这是可接受的，但这是运维损失最大的一个点：`OperationalError` 的 "password authentication failed" 这类消息从此不再出现在日志里，排查只能靠类型名。按 Task 6 评审者的建议，作为残留记档，**不改代码**。
- **Task 7 的 `assert model.prompts == []` 曾经是空洞的**（评审者的实验 B 证明：即使回合跑过它仍是 `[]`）。它已由 Task 10 按 R69 从该用例中按内容删除；同一形状的断言在新加的消息路由用例里**是**有诊断价值的，因为那个用例装的是生产图（`agent_for` → `agent_graph_factory` → `build_agent_graph`）——两处看起来一样，诊断价值相反，原因是 graph factory 不同。
- **Task 9 的报告保真度**：`task-9-report.md:111` 引用的 node id 少一个 `absolute` 词（同报告 `:92` 是对的），无代码影响。
- **CJK 在 Windows 控制台的回溯里显示为乱码**（Task 8），源文件是正确的 UTF-8。

### 9.4 未被覆盖的盲区

- **spec §3.2(b) 的"越界观察"五处不在本计划的验收范围，本期未修，不得声称已修**：`backend/app/notification/queue.py:100`、`backend/app/notification/service.py:237` 的 `logger.exception`（保留 traceback），以及 `backend/app/notification/email_service.py:47` 与 `:54` 明文记录邮件收件人地址与主题（`:51` 只记 `attempt`/`retry_times` 与异常文本）。四份 `2026-09-27-*` spec 从未提及邮件或通知，审计 spec §5:58 列举运行日志归属时也不含 `notification/*`。
- **Task 6 的两处日志点没有豁免项**——`backend/app/tools/bank_adapter.py:97` 与 `backend/app/agents/runtime.py:469` 都改了。spec §3.2(b) 给的两条路（改形态 / 保留并写豁免理由）里走的是第一条，因此不存在"被豁免、只写理由"的点。
- **plan 自己留档的一条未修**：`backend/app/notification/service.py:236` 的 `error = str(exc)` 会把异常文本写进 `email_notification` 行并随 `POST` 响应返回。属通知模块自己的问题，本期未动。
- **Task 10 的"不可猜测"只见证形态与不碰撞，不是熵。** 一个时钟种子计数器能同时通过两条断言；变异 1 只证明**常量** id 会被抓住。统计检验会带来抖动，spec 也没要求。
- **Task 10 的三条 Minor 都留给最终评审分流（R72），不需要代码改动**：① 报告的变异 3 措辞把"非空洞"与"承载性"混为一谈（该断言非空洞但**被支配**：任何走到模型的路径必然已经过了校验，所以它自己不可能是用例变红的原因；代码注释 `backend/tests/test_conversation_api.py:246-248` 是准确的）；② `backend/tests/test_conversation_repository.py:72` 的 `mine.conversation_id != theirs.conversation_id` 不可能自己失败，因为 `conversation_id` 是 UNIQUE，第二次 create 先抛 `IntegrityError`；③ `:75` 的 `conversation_id != session_id` 只靠前缀（`conv_` vs `sess_`）就能满足，抓不到"由 session 派生"。②③ 是 brief 与 §4.4 逐字钉死的，收紧它们等于偏离已批准的 spec。
- **Task 7 Minor ③**：`backend/tests/test_conversation_api.py:659`/`:664`/`:665` 是 guard 不是被变异杀死的 witness（其中 `:665` 真的可证伪）。
- **Task 4 的两条 guard 不是证据**：`backend/tests/test_business_audit.py:836-853` 的 filler（见 §9.5）与 `:921` 的 `mode == "unconfigured"`（在未变异的代码上被 `:917` 的 `pytest.raises` 蕴含）。
- **以下两条曾列入本节，已于收口修复波关闭，现在不再是盲区**：**R70②** —— `backend/tests/test_context_view.py:404-406` 曾把 `ExplodingModel` 说成承重件，`2749c95` 已改正（同提交改正了 R70① 在 `:398` 的反向括号）；**R71** —— `backend/tests/test_context_budget.py:186-189` 的护栏断言曾只覆盖 `MAX_MODEL_CONTEXT_LIMIT`，`:182` 的 `match="model_context_limit"` 也曾不能把失败归因到上限；`1469738` 已把前者参数化到三条常量、把后者改成 `match="absolute ceiling"`。修复波自证：把 `backend/app/context/budget.py:43` 的 `MAX_RECENT_MESSAGE_BUDGET` 冻成 `32_768` → `backend/tests/test_context_budget.py:197` 红（`assert 32768 >= (10 * 32768)`），而改前那条只断言 `MAX_MODEL_CONTEXT_LIMIT` 的用例对该变异是**绿**的。

### 9.5 推迟项（逐条列出，不得读作"已覆盖"）

1. **`PUT /settings/email` → `user_email.update` 的审计写入点：本期未做。** 它是 spec §2.3 的推迟条目（2026-10-04"先不管有关邮箱的东西，只做 llm 治理"）。现状可核：`backend/app/api/routes.py:187-191` 仍只调 `runtime(request).notification.set_user_email`，没有任何审计写入。
2. **分页逻辑的第三份拷贝 `backend/app/audit/service.py:583-593`（R52）：接受现状，抽取推迟。** 前两份是 `llm_calls`（`:494-502`）与 `run_events`（`:540-548`），游标过滤 `:572-582` 同样重复。理由是 brief 明确要求逐行镜像 `run_events`。
3. **`backend/tests/test_business_audit.py:836-853 test_the_settings_panel_payload_never_carries_audit_fields` 是 filler，无法变红，不是证据**（Task 4 Minor 2）。它在 `:853` 的断言被上方 `:844-852` 的七键相等蕴含，而那个相等在 `backend/tests/test_llm_settings.py:317-331` 已存在。它作为验收 5 的具名 guard 保留，但**不得计入证据**。
4. **`backend/app/integration/llm_config.py:208-225`（`_build_and_probe` 的三个 `except` 分支）与 `:77-81`（`test()`，无 `try/finally`）里既有的未关闭 client：先于本计划存在，本计划未动。** 评审者实测过其影响有界（httpx/openai 客户端都是引用计数回收，不是无界 FD 泄漏）。
5. **`backend/app/tools/gateway.py:57-59` 的 `required` 会多列名字**：schema `required: ["a","b"]`、输入只缺 `a` 时会打出 `argument (root): missing a, b`，把**存在**的 `b` 说成缺失。**`backend/app/tools/gateway.py:60-66` 的 `additionalProperties` 会把被 `patternProperties` 匹配的合法键算成 extras。**（Task 5 Minors ①②）评审者确认 ① 被 brief 逐字钉死（改它等于改 spec），② 当前**不可达**——全 `backend/` grep `patternProperties` 零命中。将来要修 ② 必须显式过滤 `error.schema.get("patternProperties", {})`，**不得**用 `jsonschema._utils` 私有 API。
6. **两个抽取候选**：第三份分页拷贝（同第 2 条）；以及 `gateway._complaint`（`backend/app/tools/gateway.py:40-66`）与 `backend/app/tools/bank_adapter.py:131 _facts_violation` 的重复。后者按 **R66** 决定**保持分离**：`_facts_violation` 的输出被 `backend/tests/test_output_schema.py:122` 只读钉住，合并会改动一个已被检查的字符串。
7. **R70/R71 已由收口修复波修复，不再是推迟项**（`2749c95`、`1469738`），详见 §9.4 末条。保留本编号以便追溯，避免第 8 条的编号漂移。
8. **Task 4 的一条 Minor 是报告/文档措辞，已在本节纠正**：`task-4-report.md` 曾把 `backend/app/integration/llm_config.py:103-105` 的 docstring 类比说成"exactly as the client it would have replaced is closed on the way in"，但参照物 `:122-123` 是**有条件**的（`if previous is not client`）且走成功路径、在换入**之后**；新的 close 是**无条件**的、走失败路径、在换入**之前**。已在 `672517a` 里把 "exactly" 去掉。

### 9.6 两条诚实的不修复

1. **§0:20 的历史测试统计（`3 failed, 752 passed, 2 skipped, 35 errors`）在仓库内不可核实。** 它是 2026-10-03 那次验收的现场记录；重跑不会复现同样的抖动分布，`_e2e/` 里也没有对应 artifact。本节不假装它可复现——它是一份历史记录，不是一条可重放的断言。
2. **§4.3 的两条 prompt 上限余量不是"已修"，是"已记档的取舍"。** 它们在本节 §9.2 的 §4.3 验收 3 里被写成明确数值与依据，但断言本身仍是 `<= 300` / `<= 60`，小于余量的增长不会被抓。这是有意保留的，不是漏项。

### 9.7 全量测试与抖动判定

命令 `cd backend && uv run pytest -q`，2026-10-04 14:49:17 → 15:47:03（外层 `ELAPSED_SECONDS=3466`，pytest 自报 3462.07s）：

```
847 passed, 2 skipped, 4 warnings, 5 errors in 3462.07s (0:57:42)
```

收集数 854 = 847 + 2 + 5，算术自洽。对照 2026-10-03 基线 `3 failed, 752 passed, 2 skipped, 35 errors in 3567.01s`：通过数多了 95（Tasks 3–10 新增用例），failed 从 3 归零，error 从 35 降到 5，耗时基本持平（3462s vs 3567s）。

**判定用的判别式**沿用 plan 的 Global Constraints：不看 `FAILED` / `ERROR` 标签，只看**回溯里有没有断言帧**。五个 error 全部是 fixture 阶段（`ERROR at setup of`），没有一条进入测试体，因此没有任何断言帧。

| node id | 断言帧 | 失败层 | 单节点重跑 |
|---|---|---|---|
| `tests/test_audit_repository.py::test_a_record_with_no_context_at_all_is_recorded_as_unrelated` | 无 | `tests/conftest.py:127` `command.upgrade(Config("alembic.ini"), "head")` → `psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly` | `1 passed in 10.17s` |
| `tests/test_audited_model_client.py::test_a_failed_audit_write_does_not_hide_the_provider_failure` | 无 | `tests/conftest.py:120` `with admin.begin() as connection:` → 同上 `psycopg.OperationalError` | `1 passed in 7.78s` |
| `tests/test_audited_model_client.py::test_the_recorded_time_is_an_absolute_instant` | 无 | `tests/conftest.py:61` `with engine.connect():` → `psycopg.errors.ConnectionTimeout: connection timeout expired`（`connect_timeout: 10`），被 `require_test_database` 转成 `pytest.UsageError` | `1 passed in 8.47s` |
| `tests/test_context_migration.py::test_upgrading_creates_the_summary_table_with_its_columns` | 无 | 同上 `ConnectionTimeout` → `UsageError`（`tests/test_context_migration.py:43` 的 `summary_schema` fixture） | `1 passed in 7.75s` |
| `tests/test_multi_agent_api.py::test_a_resume_value_that_is_not_an_object_still_reaches_the_graph` | 无 | `tests/conftest.py:130` `seed(runtime.database, settings)` → `app/storage/seed.py:14` `db.execute(text("SELECT pg_advisory_xact_lock(1709202601)"))` → 同上 `psycopg.OperationalError` | `1 passed, 1 warning in 14.12s` |

五个单节点重跑全部通过（合计 48.29s）。这与 2026-10-03 基线把 38 个非通过项全部判为云库冷连接抖动是同一结论。**本次没有发现任何真实失败。**

2 个 skip 是环境性的、与本轮改动无关，且与基线的 2 个一致：`tests/test_email_queue.py:176` 的 `pytest.importorskip("fakeredis")` 报 `SKIPPED [2] tests\test_email_queue.py:176: could not import 'fakeredis': No module named 'fakeredis'`，即收集序第 593/594 位的 `:184 test_enqueue_and_priority` 与 `:202 test_worker_failure_retry_and_dlq`。同文件 `:140` / `:156` 的两条 `skipif`（reportlab / openpyxl）本次**没有**跳过——两个包都已安装。

4 个 warning 也全部与本轮改动无关：3 条 `PytestReturnNotNoneWarning`（`tests/test_email_send.py::test_config` / `::test_template_render` / `::test_send_email` 返回了 `bool` 而不是 `None`）与 1 条 `DeprecationWarning`（`.venv\Lib\site-packages\starlette\testclient.py:53` 的 `anyio.abc.BlockingPortal` 别名）。

**取证边界**：本次运行的进度输出在采集通道里被截断过一行（`[ 25%]`），所以两个 skip 的位置是**事后**用 `uv run pytest --collect-only -q` 的 854 条 node id 顺序定位的，再用 `uv run pytest -q -rs tests/test_email_queue.py` 证实（`10 passed, 2 skipped in 5.52s`）。缺口在采集通道，不在运行本身。
