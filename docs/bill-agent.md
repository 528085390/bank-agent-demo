# 账单子智能体

账单模块已经交付并接入 Supervisor：`agent_id="bill"`，模型菜单里是三个 `bill_` 前缀工具，整轮由自己的子图 `build_bill_graph` 承接（`backend/app/modules/bill/__init__.py:19-44`）。它和转账／卡片／订阅／理财有一处根本区别：**账单数据不在 `bank_backend`，而在 Agent 自己的数据库里**。用户点页面右上角「上传流水」把文件交给后台的 REST 上传链路解析入库，Agent 只查询与分析已经入库的流水，不碰银行接口，也不参与上传（`backend/app/modules/bill/handlers.py:1-9`、`backend/app/modules/bill/tools.py:116-119`）。

## 功能与边界

| 操作 | 工具 | 代码判定的权限 |
| --- | --- | --- |
| 查一段时间内的交易明细，可按分类／借贷方向筛选、分页 | `bill_query_transactions` | 只读，直接执行 |
| 对一段时间做深度分析（7 个维度） | `bill_analyze_comprehensive` | 只读，直接执行 |
| 准备把分析报告发到邮箱 | `bill_send_report` | 写工具，但**只冻结**：不发信、不生成 PDF |
| 真正执行发送 | `bill_send_report_execute` | 隐藏工具：不进模型菜单，只由确认卡点击触发 |

几点说明：

- **查明细与做分析是两个工具，不是一回事。** `bill_query_transactions` 只取明细（`backend/app/modules/bill/tools.py:130-157`）；`bill_analyze_comprehensive` 跑分析模块并出报告，提示词要求「一轮里只调用一次」（`backend/app/modules/bill/tools.py:158-192`）。原来的 `query_bill_summary` 已删除——统计不等于分析（`backend/app/modules/bill/tools.py:121-122`）。
- **上传不经过 Agent。** 解压、解析、入库都在后台服务里完成（`backend/app/modules/bill/tools.py:118-119`）。所以只接入子智能体、不接上传链路，等于永远在查空表。
- **期间必填。** 两个查询工具都要求 `period_start` / `period_end`；日期允许用户原话（「9月13日」「昨天」），由 handler 侧的 `parse_tool_date` 解析（`backend/app/modules/bill/tools.py:46-54`、`backend/app/modules/bill/handlers.py:46-47`）。可选参数在 schema 里必须声明成可空，否则网关按原始声明校验会拒（`backend/app/modules/bill/tools.py:124-128`）。
- **工具名统一 `bill_` 前缀。** 内部实现叫 `query_transactions` / `analyze_bill_comprehensive`，登记时才加前缀（`backend/app/modules/bill/tools.py:242-252`）。

## 图与状态

```
START → decide ──条件边──→ wait(interrupt) / END
                            wait → process ──条件边──→ wait / END
```

- `decide`：原生 tool calling，最多 `MAX_TOOL_ROUNDS`（6，`backend/app/agents/sdk.py:88`）轮。只读结果回灌到 thread 继续下一轮；写工具不执行，先冻结（`backend/app/modules/bill/workflow.py:306-362`）。
- `wait`：整轮唯一的暂停点，用 `interrupt` 出确认卡。模型循环里从不 interrupt（`backend/app/modules/bill/workflow.py:1-15`）。
- `process`：收回答、校验快照、决定执行或结束（`backend/app/modules/bill/workflow.py:418-437`）。
- state 除公共字段外还有两个槽：`bill_call`（本轮冻结的调用）与 `bill_action`（待确认动作快照）。结束时必须显式清空，`idle_work_state` 不会替模块清自己的槽（`backend/app/modules/bill/workflow.py:90-101`）。

追问策略以声明为准（`backend/app/modules/bill/__init__.py:19-44`）：

- `period_start` / `period_end`：必填，用户没说清就先问，不能编日期（`backend/app/modules/bill/workflow.py:83`）。
- `intent`：只在用户说「分析流水」这类含糊说法时问「你想做什么」；说「查 / 看流水」就直接查明细，不问（`backend/app/modules/bill/__init__.py:24-29`）。
- `category`：声明成可选筛选条件（`required=False`），注释写明追问它属于「追问可选信息」、提示词已禁止（`backend/app/modules/bill/__init__.py:40-41`）。不过 `_BILL_PROMPT` 只要求「只把你确实打算用的可选参数放进参数」（`backend/app/modules/bill/workflow.py:85`），并没有逐字写「不要问分类」——分类会不会被追问，最终仍取决于模型。

## 确认与写安全

发报告是两段式的：`bill_send_report` 只冻结，确认后才由隐藏工具执行。

1. 模型调 `bill_send_report(period_start, period_end, recipient?)`。工具解析日期、归一收件人，生成 `action_id = "bill-report-<uuid>"`，返回「准备发送账单报告到 X，请确认」。它不发信、不生成 PDF（`backend/app/modules/email_handlers.py:77-103`）。
2. 图把这次调用冻结成 `bill_action` 快照（期间、收件人、`idempotency_key`、`expires_at = now + 5 分钟`），然后挂起等待（`backend/app/modules/bill/workflow.py:238-263`、`:66`）。
3. 前端弹确认卡：`kind="confirmation"`、`operation="send_report"`、标题「确认发送账单报告」、按钮「确认 / 取消」。`pending_id` 是 `bill-report-…`，故意不是 `CA-…` 形状，所以不会走银行验证码授权；发信没有金额，但 `TurnConfirmation` 要求正整数分，于是填 1 分（`backend/app/modules/bill/workflow.py:141-184`）。
4. 用户点击后走 `POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`（`backend/app/api/routes.py:489-511`）。这是确认进入模块的唯一通道：聊天里打「确认」只是普通消息，到不了这个动作。
5. `process` 六步 fail-closed：快照必须是 dict、`pending_id` 与 `operation` 非空、未过期、回答里的 `pending_id` 若带必须相等、决定必须是 confirm 或 cancel，最后才用**快照里的参数**调 `bill_send_report_execute`（`backend/app/modules/bill/workflow.py:387-416`）。快照缺失、字段对不上或回答里的 `pending_id` 不匹配，一律回「这笔操作的确认信息已失效，请重新说明。」；过期回「这笔操作的确认信息已过期，请重新说明。」；取消则直接结束（`backend/app/modules/bill/workflow.py:391-403`）。
6. 点确认后用的仍是冻结时的 `idempotency_key`。**但它不保证「同 key 只发一封」**——`email_handlers` 从不读这个键；挡重复点击的是 HTTP 层的 409 与会话锁（`backend/app/modules/bill/workflow.py:196-203`、`backend/app/api/routes.py:529-556`）。

写安全总结：账单模块**没有改账单数据的写操作**，两个查询工具都是 `SideEffect.READ`；唯一的写是「发邮件」这个外部副作用，必须先冻结、再由用户在卡上确认（`backend/app/modules/bill/tools.py:21-43`）。

## 邮件报告

- 收件人：显式 `recipient` 优先（`"null"` / `"none"` / `""` 视为未填），否则读用户绑定邮箱；两者都没有就在冻结前拒：「您还没有绑定邮箱。请先在右上角设置里配置邮箱地址，然后我再发送报告。」（`backend/app/modules/email_handlers.py:43-75`）。
- 内容：点确认后现场跑 `BillAnalyzer`、生成 PDF、以附件发送，成功判据是 `status` 属于 `SENT` / `QUEUED`（`backend/app/modules/email_handlers.py:105-189`）。
- **SMTP 未配置时**：工具照常登记（登记条件是注入了 `NotificationService`，不是 SMTP 可用，`backend/app/modules/bill/tools.py:242-274`），冻结也照常成功；点确认后 `send_email` 返回 `{"status": "FAILED", "error": "SMTP 未配置"}`（`backend/app/notification/service.py:151-167`），execute 把它转成 `ToolFailure(INTERNAL, "SMTP 未配置")`。也就是说：**工具在、卡片能弹、发送失败**，不是「工具不登记」。`backend/tests/test_bill_notification.py` 钉住了这个行为。

## 数据声明：bill

遵循[模块数据归属契约](superpowers/specs/2026-09-23-module-data-ownership-contract.md)。

- 场景：用户上传账单流水后，在会话里查询明细、做深度分析、把报告发到邮箱。
- 唯一所有者与权威来源：Agent 侧数据库。账单模块不查银行接口、不建自己的表，读写都落在既有的账单表上（`backend/app/modules/bill/README.md:136`）。
- 数据实体：`bill_task`（上传任务）、`statement_document`（文件）、`bill_transaction`（流水行）、`bill_audit_event`（审计）、`bill_summary`（汇总）；另有三个只声明、未使用的表，仅为让 autogenerate 干净（`backend/app/storage/bill_models.py:50`、`:83`、`:111`、`:150`、`:177`、`:203`、`:219`、`:232`）。
- 持久化原因：流水只解析一次、要能反复查；报告里的每个数字都要能追回原始行。
- 读写者：写入在 REST 上传链路（`backend/app/api/bill.py:21-47`）；读取在 `BillQueryService` 与 `BillAnalyzer`（`backend/app/services/bill_query.py:137-191`、`backend/app/services/bill_analysis/analyzer.py:22-149`）。
- 事务：上传任务有状态机；流水按 `UNIQUE(statement_document_id, raw_record_hash)` 去重，文件按 `UNIQUE(user_id, source_file_hash)` 按用户去重（`backend/app/storage/bill_models.py:85-90`、`:113-119`、迁移 `backend/app/storage/migrations/versions/0015_bill_doc_tenant_unique.py:25-40`）。
- 保留：仓库里未见账单数据的删除或定期清理接口（以 `backend/app/api/bill.py` 现有路由为限）。
- Agent checkpoint：会话线程、待办、槽位都在图 state 里，不进账单表（`backend/app/modules/bill/README.md:138`）。
- 迁移：`0014_bill_compatibility` 把 `bill_transaction.amount` 从 `Numeric(18,2)` 换成整数分，并把源项目的 0007-0010 合并进来；两个账单迁移的 `downgrade()` 都故意不可逆（`backend/app/storage/migrations/versions/0014_bill_compatibility.py:1-24`、`backend/app/storage/migrations/versions/0015_bill_doc_tenant_unique.py:1-11`）。

## 与主 Agent 的接口

| 方法 | 路径 | 谁调用 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/bill/upload` | 前端「上传流水」 | 建任务并落文件，可带期间与银行代码（`backend/app/api/bill.py:41-84`） |
| POST | `/api/bill/tasks` | 前端 | 只建任务（`backend/app/api/bill.py:28-38`） |
| POST | `/api/bill/tasks/{task_id}/upload-file` | 前端 | 往已有任务补文件（`backend/app/api/bill.py:94-107`） |
| POST | `/api/bill/tasks/{task_id}/begin-processing` | 前端 | 触发后台解析入库（`backend/app/api/bill.py:110-123`） |
| POST | `/api/bill/tasks/{task_id}/password` | 前端 | 加密文件提交口令（`backend/app/api/bill.py:126+`） |
| POST | `/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide` | 前端确认卡 | 确认／取消发报告（`backend/app/api/routes.py:489-511`） |

上传链路的端点都在 `backend/app/api/bill.py`；会话与确认走通用路由，账单模块没有自己的 HTTP 面。

## 最小底座扩展

- **组合根必须显式注入两个依赖。** `production_modules(*, notification_service, database)` 两个参数都是必填，缺一个就 `RuntimeError("账单模块需要数据库与通知服务；组合根必须显式注入")`——而不是静默少几个工具（`backend/app/modules/__init__.py:28-47`）。
- **import 期的 `MODULE` 不带工具。** `app/agents/domain.py:23` 在 import 期就会跑注册表，模块自带工具会把数据库与通知服务拖进 import 链；而且声明了业务工具就必须自带图，否则 Supervisor 直接拒绝（`backend/app/modules/bill/__init__.py:1-10`、`:46-47`）。
- **`build_bill_module` 的过滤是必需的。** `ModuleSpec` 强制 `tools` 与 `handlers` 键集相等，缺依赖时只能把声明一并摘掉，不能构造一个立刻抛错的模块（`backend/app/modules/bill/__init__.py:50-72`、`backend/app/modules/base.py:124-129`）。
- 账单模块没有新增权限、没有新增工具类型，只是把已有服务接进标准模块形状。

## 启动与演示

```powershell
./start.ps1 -Database local                   # 后端 + 前端 + 模拟银行一起起
cd backend; uv run python scripts/serve.py    # 或只起 Agent 后端
cd frontend; npm run dev -- --port 5173       # 前端
```

登录后先点右上角「上传流水」导入一份账单，再在聊天里试：

- 「查一下我 9 月的流水」
- 「看看这个月有没有异常交易」
- 「把 8 月的账单分析报告发到我邮箱」

## 测试

6 个账单测试文件（`backend/tests/test_bill_module.py`、`test_bill_graph.py`、`test_bill_notification.py`、`test_bill_analysis.py`、`test_bill_upload.py`、`test_bill_migration.py`）覆盖装配、确认协议、邮件、分析、上传与迁移：

```powershell
cd backend
$env:TEST_DATABASE_URL='postgresql+psycopg://postgres:123456@127.0.0.1:5432/test'
.\.venv\Scripts\python.exe -m pytest tests -q -k bill
```

`-k bill` 实测收集 52 个用例、全部通过（约 26 秒）。用 uv 环境时把最后一行换成 `uv run pytest tests -q -k bill`。

## 已知边界

- 上传、解压、解析、入库不在 Agent 里；子智能体只读已入库的流水（`backend/app/modules/bill/tools.py:116-119`）。
- 报告里的数字只能来自工具结果，提示词明令禁止编造金额、商户、账户与收件人（`backend/app/modules/bill/workflow.py:87`）。
- 幂等键不构成「同一操作只发一封」的保证，重复点击靠 HTTP 层 409 与会话锁挡（见「确认与写安全」第 6 条）。
- 账单模块的接入 spec 记录了若干已知缺口：`bill_password_ttl_seconds` 只有定义没有读取点；`docs/contracts/sql/001_init.sql` 与 ORM 模型存在列漂移；`/api/bill/tasks/{id}/summary` 与 `/analysis` 在前端已成孤儿端点；`bill_task_source_mode_check` 允许 EMAIL / LOCAL_SAMPLE，代码只走 manual_upload；分析规则里有未被调用的内部函数；前端没有任何账单测试（`docs/superpowers/specs/2026-10-14-bill-module-port-spec.md:519-526`）。
