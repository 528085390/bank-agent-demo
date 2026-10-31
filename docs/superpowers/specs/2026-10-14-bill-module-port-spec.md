# 账单模块并入 dev：移植与改造 spec

- 日期：2026-10-14
- 状态：待用户审阅
- 来源分支：`dev_Analyze`（孤儿分支，HEAD `4d5db87a914c802bc79d0da40623395a81d4ec7a`）
- 目标分支：`feat/bill-module`（工作树 `D:/ai_project/wt-dev_Analyze`，基线 `dev` = `7886dedaf740ff596709e159859c33c054d84564`）
- 相关任务：把 dev_Analyze 的账单功能并回 dev
- 修订：第二轮（按已接入子 agent 的形态收敛，见 6.6；工具名带 `bill_` 前缀，装配移回组合根）

---

## 1. 目标

把 `dev_Analyze` 上的**账单功能**移植到基于 `dev` 的新分支，使其在 dev 的模块契约下**真正可用**，然后以普通合并的方式并回 `dev`。

"账单功能"是两半拼起来的，两半都移植：

- **子智能体半**：向底座声明 3 个工具（`query_transactions` / `analyze_bill_comprehensive` / `send_analysis_report`）+ handler，供对话中的"账单 Agent"调用。
- **传统 Web 半**：`/api/bill` 的 12 个 REST 端点、上传解析链路（7 种格式）、8 张账单表与迁移、以及前端的上传卡 / 报告渲染 / 邮箱设置。

**耦合点（决定了不能只并一半）**：子智能体的 3 个工具里**没有上传**；它读 `bill_transaction` 表，而写这张表的是 REST 上传链路。只并子智能体 = 一个永远查空表的空壳。

同时修掉审查中发现的 3 个阻断缺口，并清理死代码。

## 2. 非目标

- 不重构账单的业务实现（解析器、分析模块、PDF 生成逻辑一律原样搬）。
- 不把 `dev` 上领先的工作反向覆盖（详见 6.2 的"反向禁区"）。
- 不做新的产品功能（如账单导出、定时报告）。
- 不解决全部已知遗留缺口，只解决 3 个阻断缺口 + 明确列出的死代码（见 10）。

## 3. 背景：dev_Analyze 是什么

- `dev_Analyze` 是**孤儿分支**：唯一根提交 `4d5db87`（"feat: initial dev_Analyze branch with local project files"）。`git merge-base dev dev_Analyze` 为空，`dev` 有 465 个提交不在其上。
- 它的"底座"是 dev 的旧快照：存在单体 `backend/app/agents/supervisor.py`（1807 行），而 dev 已拆成包 `backend/app/agents/supervisor/{__init__,graph,handoff}.py`；dev 的 `agents/sdk.py`(732 行)、`agents/hitl.py`、`agents/decisions.py`、`modules/base.py` 的新形状、`storage/migrations/versions/0013_agent_run_event_call_id.py` 在 dev_Analyze 中都不存在。
- 因此"并回 dev"不能是普通 merge。策略：**工作树目录不变，分支换成基于 dev 的新分支 `feat/bill-module`**，用 `git checkout dev_Analyze -- <账单文件>` 把功能搬过来。`dev_Analyze` 保持只读来源。
- `git diff dev HEAD` 的 `a/` 侧就是真实 dev 文件（HEAD 是 root commit），文本层干净，风险全在**语义与范围**。

## 4. 核心判断：不能直接接入

账单子智能体的**实现**与 dev 现有的 card/subscription/transfer/wealth 差不多，但**接缝**撞车四处：

| | dev 的 card / subscription / transfer / wealth | dev_Analyze 的 bill |
|---|---|---|
| 工具声明 | `SPECS` + `ModuleSpec` | 一样（3 个 ToolSpec） |
| 依赖注入 | `build_registry(*, notification_service, database)` | dev 的 `build_registry` **没有这两个参数** |
| **图** | 每个都有 `graph_builder=` 自己的图（card `modules/card/workflow.py` 440 行 / transfer `modules/transfer/workflow.py` 429 行 / wealth `modules/wealth/workflow/graph.py` 505 行 / subscription `modules/subscription/workflow.py`） | **没有 graph_builder** |
| 循环原语 | `app/agents/sdk.py`（732 行公共面） | 依赖**已删除**的 supervisor 通用工具循环 + 已删除的 `ModuleSpec.turn` |

**致命的一环是"图"。** `backend/app/agents/subgraph_templates.py` 只有 `minimal_shell_graph`，其模块 docstring 是一条硬契约：

> "A domain that carries business tools must declare a graph_builder of its own (roadmap A5 §1); build_domain_graph refuses to fall back to a generic loop."

而 `minimal_shell_graph` 的 `conclude` 节点直接返回常量 `NO_TOOLS_SUMMARY = "该能力当前尚未接入，暂时无法处理此请求。"`

**结论：账单模块一旦按 dev 的规矩声明工具，就必须自带 `graph_builder`，否则用户说"分析我这个月账单"会收到"该能力当前尚未接入"。** 所以必须新写 `backend/app/modules/bill/workflow.py` 的 `build_bill_graph`。

## 5. 工作方式

- 工作树：`D:/ai_project/wt-dev_Analyze`（已切到 `feat/bill-module`）。**主工作区 `D:/ai_project/agent` 始终留在 `dev`，不切换、不污染。**
- 所有 git 操作用 `git -C D:/ai_project/wt-dev_Analyze ...` 寻址，不 `cd`。
- 流程：本 spec → 用户审阅 → writing-plans 出实施计划 → 在 worktree 实现 → 三件套全量测试通过 → 并回 dev。
- 依赖安装只发生在 worktree：后端 `uv sync`、前端 `npm ci`（前端锁文件是 `package-lock.json` → 用 npm，不用 pnpm）。

---

## 6. 后端设计

### 6.1 原样搬（新增文件，零改动）

用 `git checkout dev_Analyze -- <path>` 逐项取出。

**API 与 schema**
- `backend/app/api/bill.py`（12 端点）、`backend/app/api/bill_schemas.py`

**服务层**
- `backend/app/services/bill_task.py`、`bill_import.py`、`bill_parsing.py`、`bill_processing.py`、`bill_query.py`、`bill_audit.py`、`bill_content_hash.py`
- `backend/app/services/bill_analysis/`：`__init__.py`、`analyzer.py`、`models.py`、`preprocessor.py`
- `backend/app/services/bill_analysis/modules/`：`account_usage.py`、`cashflow.py`、`counterparty.py`、`hourly_activity.py`、`risk_profile.py`、`risk_transactions.py`、`type_analysis.py`
- `backend/app/services/bill_analysis/rules/`：`__init__.py`、`assessment.py`、`keywords.py`、`normalization.py`、`risk_rules.py`

**上传解析（billing）**
- `backend/app/billing/`：`__init__.py`、`contracts.py`、`detect.py`、`errors.py`、`sandbox.py`
- `backend/app/billing/handlers/`：`__init__.py`、`archive.py`、`docx.py`、`image.py`、`pdf.py`、`plaintext.py`、`registry.py`、`spreadsheet.py`（搬进来之后要在 6.4(3) 里改：保留前导说明行）

**分析解析**
- `backend/app/analysis/` 下招行流水解析相关新增文件（`cmb_parser.py`、`summarizer.py` 等；以 `git diff --name-status dev HEAD -- backend/app/analysis` 的 A 清单为准）

**存储与迁移**
- `backend/app/storage/bill_models.py`
- ~~`docs/contracts/sql/001_init.sql`~~ **不搬**：dev 与 dev_Analyze 逐字节相同（`git diff` 为空），dev 的这份已经有那 8 张账单表，由 `0001_billing_baseline.py` 直接 exec —— checkout 它是空操作，列进"原样搬"只会让人以为要搬。
- 账单迁移（见 6.4，需重编号）

**核心与通知**
- `backend/app/core/bill_config.py`、`backend/app/core/bill_errors.py`
- `backend/app/notification/analysis_pdf.py`（856 行）

**模块与工具**（布局按 6.6 收敛成"已接入子 agent 的形态"）
- `backend/app/modules/bill/tools.py`（**新写**：把 dev_Analyze 252 行声明版里 `EMAIL_TOOLS`/`QUERY_TOOLS`/`ANALYSIS_TOOLS` 三组 dict 收敛成一个 `spec()` 工厂，工具名带 `bill_` 前缀；导出 `TOOLS`（4 个，含隐藏 execute）与 `build_handlers(*, database=None, notification_service=None)`）
- `backend/app/modules/bill/__init__.py`（**新写，约 40 行**：`BILL` 声明 + 无工具的 `MODULE` + `build_bill_module(*, notification_service=None, database=None)`）
- `backend/app/modules/bill/handlers.py`、`analysis_handlers.py`、`email_handlers.py`（原样搬，由 `tools.py` 汇总）
- `backend/app/modules/bill/workflow.py`（**新写**，见 6.3）
- `backend/app/modules/bill/README.md`（**新写**：照 `subscription/README.md`/`transfer/README.md`/`wealth/README.md` 的先例）
- `backend/app/modules/dates.py`（`parse_tool_date`，dev 没有）

### 6.2 需修改的既有文件（逐条，共 12 处）

1. `backend/app/modules/__init__.py`
   - 追加 `def production_modules(*, notification_service=None, database=None) -> tuple[ModuleSpec, ...]`：
     `tuple(build_bill_module(notification_service=notification_service, database=database) if module is BILL else module for module in ALL_MODULES)`。
   - **这是对 dev_Analyze 原设计的修正**：dev_Analyze 把 `notification_service`/`database` 加到 `build_registry()` 上并在其中替换 `BILL_MODULE`。但 `build_registry()` 本身就是一条无参生产路径（`backend/app/runtime.py:36-43` 的 `build_tool_gateway()` 默认 `build_registry()`；`backend/app/agents/domain.py:25` 的 `DOMAIN_ROSTER = build_registry().roster` 在 import 期无参执行）。把服务塞进 registry，会让同一个名字 `bill` 在不同调用点含义不同（生产 3+1 个工具 / 无参路径 0 个工具，而声明里挂着 `graph_builder` → 拿到空菜单的图仍会跑）。装配是**组合根**的职责。
   - 因此 `backend/app/modules/registry.py` **一行不改**（`build_registry(modules=None)` 签名保持）；`ModuleSpec.__post_init__`（`modules/base.py:112-135`，强制 `key == spec.name` 且 tools/handlers 键集相等）也无需放宽——声明版 `MODULE` 不带工具是合法的。
   - import 期不得拉起服务：`build_bill_module` 的 handler 依赖一律**函数内 import**（dev_Analyze 的 `handlers.py`/`analysis_handlers.py`/`email_handlers.py` 已是这个写法），否则 `registry → modules → bill → services` 会在 import 期成环。
2. `backend/app/runtime.py`
   - `Runtime.__init__` 里 `build_registry()` → `build_registry(production_modules(notification_service=self.notification, database=self.database))`，import 加 `from app.modules import production_modules`。
   - **只改这一行。** dev_Analyze 里 `build_turns` / `agent_graph_factory` 的差异是 dev 已删掉的旧物，不回搬。
3. `backend/app/main.py`
   - `from app.api.bill import router as bill_router` + `app.include_router(bill_router)`。
4. `backend/app/api/routes.py`
   - **只加** `GET /settings/email` 与 `DELETE /settings/email`（dev 只有 PUT）。
   - GET：`identity(request)` → `run_in_threadpool(runtime(request).notification.get_user_email, who["user_id"])` → `envelope(request, {"user_id": ..., "email": ...})`。
   - DELETE：`notification.clear_user_email(...)` → 返回 `email: None`。
   - docstring 说明为什么不放进 `/session`（`/session` 视图带缓存，改绑后缓存会旧）。
   - **不回搬** dev_Analyze 里 `hitl` / `_requires_code` / `_click_matches` / "确认本次理财操作" 的旧写法（dev 已改为从 `app.agents.hitl` import，确认文案已领域无关化）。
5. `backend/app/storage/migrations/env.py`
   - 加 `import app.storage.bill_models  # noqa: F401`（否则 alembic 不认账单表）。
6. `backend/app/notification/attachments.py`
   - 追加 `@dataclass(frozen=True, slots=True) class Attachment: filename: str; content: bytes`（内存附件，才能活过队列序列化）。
7. `backend/app/notification/email_service.py`
   - `from app.notification.attachments import Attachment`；`send` 的 `attachments: Optional[list[str]]` → `Optional[list[str | Attachment]]`；循环里 `isinstance(attachment, Attachment)` → 走新的 `@staticmethod _attach_bytes(msg, attachment)`（`MIMEBase("application","octet-stream")` + `set_payload(content)` + `encoders.encode_base64` + `Content-Disposition`），否则 `_attach_file`。
8. `backend/app/notification/service.py`
   - dev 已有 `get_user_email` / `set_user_email`；需新增 `clear_user_email(user_id)`（查 `AppUser`，未找到 `raise AppError("USER_NOT_FOUND","用户不存在",404)`，否则 `user.email = None`）、`send_email(*, recipient, subject, html, attachments=None, task_id=None) -> dict[str, Any]`（未配置 SMTP → `{"status":"FAILED","error":"SMTP 未配置"}`；有队列 → `{"status":"QUEUED",...}`；否则同步 `_send_and_record`）、以及 `_serialize_attachments` / `_deserialize_attachments` 与队列消费者的 `if task_type == "generic"` 分支。
9. `backend/pyproject.toml` + `backend/uv.lock`
   - 加 `python-multipart>=0.0.20,<1`、`pypdf>=5.0,<7`、`python-docx>=1.1,<2`（`reportlab>=4.0,<5` dev 已有）。
10. `backend/app/modules/bill/tools.py` + `backend/app/modules/bill/__init__.py`（**新写**，替换 dev 的 20 行 shell 版）
    - `tools.py`：把 dev_Analyze 声明版的三组 dict 收敛成一个 `spec()` 工厂（照 `card/tools.py` 与 `subscription/tools.py` 的同名工厂：`name=f"bill_{name}"`、描述尾部内嵌 `参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}`、`side_effect=SideEffect.WRITE if write else SideEffect.READ`、`output_schema`）；导出 `TOOLS = {item.name: item for item in SPECS}`（4 个，含隐藏 `bill_send_report_execute`）与 `build_handlers(*, database=None, notification_service=None) -> dict[str, ToolHandler]`（依赖非空时才登记对应 handler）。
    - `__init__.py`：`BILL = DomainDeclaration(agent_id="bill", display="账单 Agent", purpose="处理账单查询、账单分析、消费分类相关的请求", fields={...}, graph_builder=build_bill_graph)`（声明版原本没有 `graph_builder`，见 6.3）+ `MODULE = ModuleSpec(name="bill", declaration=BILL)`（**不带 tools/handlers**，供 import 期 roster 发现，照 dev 现状）+ `def build_bill_module(*, notification_service=None, database=None) -> ModuleSpec`（装配版，把 `TOOLS`/`build_handlers(...)` 一起交给 `ModuleSpec`）。
    - `purpose` **保留 dev 现有的一句话**（`dev:backend/app/modules/bill/__init__.py:17`）；dev_Analyze 那 60 行行为规则整体搬进 `_BILL_PROMPT`（6.3），**不要留在 `purpose`**：四个已接入模块的 `purpose` 都是一句话，它是给 Supervisor 路由看的，不是图里的系统提示词。
    - 工具名一律带 `bill_` 前缀（card/subscription/transfer 由工厂强制、wealth 用 `wealth_*`；dev_Analyze 的裸名不合形态）：`bill_query_transactions`（READ）、`bill_analyze_comprehensive`（READ）、`bill_send_report`（WRITE，prepare）、`bill_send_report_execute`（WRITE，隐藏）。
11. `backend/app/modules/bill/workflow.py` + `README.md`（**新写**）
    - `workflow.py`：`build_bill_graph`，见 6.3。
    - `README.md`：照 `subscription/README.md`/`transfer/README.md`/`wealth/README.md` 的先例，写清 3+1 工具、确认协议（prepare 冻结 / 点击确认后 execute）、注入依赖（`database`、`notification_service`）与图拓扑。
    - `email_handlers.py` 的发送逻辑按 6.3 拆成 prepare / execute（工具数 3 → 4）。
12. `backend/tests/test_business_audit_migration.py`
    - `:153-156` 的 `test_the_business_audit_revision_is_the_head_of_the_chain` 断言 `script.get_current_head() == "0013_agent_run_event_call_id"`，新增 head 必挂 → 改成**可达性断言**（`0013_agent_run_event_call_id` 可从当前 head 到达），遵循 `test_context_migration.py:169` 的原话 "Pinning the head here would re-break this file every time a revision is added"。

**反向禁区（dev 领先，一律不动）**——21 个 M 文件，已逐一 grep 确认与账单无关：
`backend/app/storage/checkpointer.py`、`storage/models.py`、`modules/base.py`、`tools/protocol.py`、`context/handoff.py`、`integration/{bank_client,bank_errors}.py`、`agents/{__init__,declaration,domain,turn}.py`、`modules/card/{routes,tools,workflow}.py`、`modules/life_event/__init__.py`、`modules/subscription/__init__.py`、`modules/transfer/{workflow,writes}.py`、`modules/wealth/{infrastructure/bank_adapter.py,infrastructure/tools.py,workflow/graph.py}`。

### 6.3 新写：`backend/app/modules/bill/workflow.py` 的 `build_bill_graph`

模板：`dev:backend/app/modules/subscription/workflow.py:210-559`（最接近——READ 工具 + WRITE 冻结快照 + 确认 park/resume，且**不需要**银行侧授权，与账单发信同类）。card 与 subscription 的图**拓扑完全相同**，只有 `run_call` 的业务语义不同；账单照抄拓扑，不要自创节点。

**签名与接缝**（照 `subscription/workflow.py:210-215`）
```python
def build_bill_graph(model, spec, *, assembler=None, turn=None, client=None):
    """账单自己的图。它不读基座的 assembler/turn/client 接缝，但必须接受这三个关键字，
    否则基座的调用点会 TypeError。"""
```

**图拓扑**（照 `subscription/workflow.py:551-559` 逐字）
```python
graph = StateGraph(BillState)
graph.add_node("decide", decide)
graph.add_node("wait", wait_for_user)      # app.agents.sdk.wait_for_user，不要自己写
graph.add_node("process", process)
graph.add_edge(START, "decide")
graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
graph.add_edge("wait", "process")
graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
return graph.compile()
```
`def route(state): return "wait" if state.get("turn_phase") == "waiting_for_user" else END`

**状态**（照 `subscription/workflow.py:204-207` 的形状）
```python
class BillState(AgentState, total=False):
    bill_call: dict[str, Any] | None      # 冻结的待确认调用：{"tool": name, "arguments": args}
    bill_action: dict[str, Any] | None    # 冻结快照：pending_id/operation/period/recipient/idempotency_key/expires_at
```
`AgentState`（`app/agents/state.py:83-141`）已提供 `pending_question`/`pending_answer`/`turn_phase`/`domain_thread`/`tool_rounds`/`collected_inputs`/`active_agent`/`domain_results`/`goal`。注意 `idle_work_state()`（state.py:144-161）**不会清模块自己的槽**，所以 `finish` 必须显式清 `bill_call`/`bill_action`（subscription 的 `finish` 就是这么做的，:230-259）。

**导入面**（全部已存在，无需新增底座能力）
```
langgraph.config: get_stream_writer
langgraph.graph: END, START, StateGraph
app.agents.decisions: DecisionError, parse_domain_decision
app.agents.sdk: MAX_TOOL_ROUNDS, DomainOutcome, TurnStatus, awaiting, call_tool, domain_outcome,
                may_retry, overflow_outcome, read_answer, tool_envelope, tool_outcome, wait_for_user
app.agents.state: AgentState, idle_work_state
app.agents.toolcalls: StreamTruncated, collect_stream
app.agents.upstream: UpstreamUnavailable
app.tools.protocol: SideEffect, ToolStatus
app.tools.schema: ASK_USER_TOOL_NAME, ask_user_spec, function_schema
```

**常量**
- `_BILL_TURN_STATUS = {SUCCEEDED: TurnStatus.COMPLETED.value, FAILED: TurnStatus.FAILED.value, DENIED: TurnStatus.UNSUPPORTED.value}` —— **turn_status 必填**，Supervisor 靠它判断某个 agent 是否已结论。
- `_READ_ROUND / _PARKED / _DONE`（一次工具调用对本次激活意味着什么）
- `PENDING_EXPIRY_MINUTES = 5`
- `_HIDDEN = frozenset({"bill_send_report_execute"})` —— 该名字留在 Gateway 校验的绑定 catalog 里，但**绝不进模型的工具菜单，也不接受模型直接调用**。
- `_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})` + `declared_arguments(arguments)`（schema 禁止额外属性，占位值必须先丢掉；照 `subscription/workflow.py:168-178`）。

**`_BILL_PROMPT`**（模块级常量，照 `subscription/workflow.py:191-204` 的结构写；`purpose` 字段是给 Supervisor 做路由用的，**不能当图里的系统提示词**）
- 角色：你是账单 Agent，只理解需求；工具名和参数以工具定义为准。
- 一次只办理一个操作；是否成功只根据工具结果判断，不要自行确认或声称已经发送，确认与执行由代码处理。
- 工具用法：查流水用 `bill_query_transactions`；要综合报告用 `bill_analyze_comprehensive`；要发报告用 `bill_send_report`，代码会弹出确认卡，**不要在回复里自己问确认，也不要替用户确认**（真正的发送由代码在用户点击确认后调 `bill_send_report_execute`，那个工具不在你的菜单里）。
- `period_start` / `period_end` 是必填：用户没说清期间就先用 `ask_user` 追问，**不要编造日期**（日期解析交给 `app/modules/dates.py` 的 `parse_tool_date`）。
- 只把你确实打算用的可选参数（`category`/`direction`/`page`/`page_size`/`modules`/`recipient`）放进参数；没打算用的不要放，也不要写成 `null` 或 `"null"`。
- 不要编造金额、商户、账户或收件人；报告里的数字只能来自工具结果。

**工具菜单与 `known`**（照 `subscription/workflow.py:339-342` 逐字三行）
```python
tools = [item for item in spec.handle.tools if item.name not in _HIDDEN]
schemas = tuple(function_schema(item) for item in tools) + (function_schema(ask_user_spec()),)
known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}
```

**节点语义**（每个节点都是 subscription 对应节点的账单版）
- `decide(state, *, answers=None)`（:437-450）：先发 `{"event": "agent_start", "agent_id": "bill"}`；`thread = list(state.get("domain_thread") or [])`；thread 非空 → 追加 `{"role": "user", "content": json.dumps({"user_answer": answers or {}}, ensure_ascii=False)}`（恢复：接着上一轮的线程跑，**不重启**）；否则 `thread = seed_thread(state, answers)`；最后 `return activate(state, thread, int(state.get("tool_rounds") or 0), answers or {})`。
- `wait` = `sdk.wait_for_user`（sdk.py:381-404）：`question = state["pending_question"]`（为空 → `DecisionError("等待节点没有可回答的问题")`）；`answer = interrupt(dict(question))`；写 `pending_answer` + `turn_phase="process_answer"`，并把答案以 ledger 的消息 id 并进 `messages`。**这个节点绝不调模型**——LangGraph 会重放中断节点，问题必须来自检查点，重新生成就可能把用户的答案交给另一个问题。
- `process(state)`（:499-546）：`question = state.get("pending_question") or {}`；`answers, problem = read_answer(state.get("pending_answer"), question)`；`problem` 非空 → `attempt = int(question.get("attempt", 1)) + 1`，`attempt > 3` → `finish("未获得有效输入，本次办理已结束。", FAILED)`，否则返回 `{"pending_question": {**question, "attempt": attempt, "display": f"{problem}\n{question['display']}"}, "pending_answer": None, "turn_phase": "waiting_for_user"}`；`question["kind"] == "confirmation"` → `consume_confirmation(state, answers)`；否则 `collected = {**(state.get("collected_inputs") or {}), **answers}` → `decide(state, answers=collected)`。

**`activate`：整个模型循环活在这一次节点访问里**（照 `subscription/workflow.py:369-435`）
循环自己不中断：**问用户是循环的返回值，中断由 `wait` 节点做**——这条分工是"停在问题上的激活能被原样重放"的唯一原因。`pending` 形参是代码已经决定、必须先执行的那一次调用（用户点完确认之后的写操作），它走同一条执行路径，所以只读轮次的回喂逻辑只有一份。
```python
def activate(state, thread, rounds, answers, pending=None):
    summaries = []
    while True:
        if pending is None:
            messages = ({"role": "system", "content": _BILL_PROMPT}, *thread)
            try:
                text, calls = collect_stream(model, messages, schemas)
            except StreamTruncated:
                return finish("模型输出中断，本次没有办理任何操作。", ToolStatus.FAILED)
            try:
                decision = parse_domain_decision(text, calls, known)
            except DecisionError as error:
                logger.warning("bill decide rejected: %s | text=%r calls=%r", error, text[:200],
                               [(call.name, dict(call.arguments)) for call in calls])
                if len(calls) == 1 and calls[0].name not in known:
                    return finish("该账单操作不被允许。", ToolStatus.DENIED)
                return finish("模型没有给出可用的账单信息，请重新说明。", ToolStatus.FAILED)
            outcome = domain_outcome(decision)
            if outcome.ask is not None:
                # period_start/period_end 必填，模型会先追问；必须让它真的问出去，
                # 不能换成一句收尾文案（card 那边丢过用户真正问的事）。
                attempt = int((state.get("pending_question") or {}).get("attempt", 0)) + 1
                return awaiting(spec, outcome, get_stream_writer(), collected=answers or {},
                                attempt=attempt, domain_thread=thread, tool_rounds=rounds)
            if not outcome.wants_tool:
                return finish(outcome.summary or "请说明要查询哪段期间的账单；当前尚未办理任何操作。")
            if rounds >= MAX_TOOL_ROUNDS:
                # 与基座同序：到界的那一次模型调用仍然发生，模型总有一次用叙述收尾的机会。
                return finish(overflow_outcome(spec, summaries).summary)
            thread = [*thread, assistant_turn(text, calls)]
            name, args = outcome.tool_name, outcome.arguments
        else:
            name, args = pending
            pending = None
        kind, payload = run_call({"tool": name, "arguments": args})
        if kind == _PARKED:
            payload["domain_thread"] = thread      # 停下来的激活要把线程存进检查点
            payload["tool_rounds"] = rounds
            return payload
        if kind == _DONE:
            return payload
        tool = tool_outcome(payload)               # _READ_ROUND：把工具信封回喂给模型
        thread = [*thread, tool_envelope(DomainOutcome(tool_name=name), tool)]
        summaries.append(tool.summary)
        rounds += 1
```
配套两个小函数（照 `:344-367`）：
- `seed_thread(state, answers)`：一次激活的第一条用户消息 = `f"指令：{state.get('goal') or '按用户请求查询或分析账单'}\n已补充信息：{json.dumps(answers or {}, ensure_ascii=False)}"`。
- `assistant_turn(text, calls)`：text 非空 → `{"role": "assistant", "content": text}`；否则 `{"role": "assistant", "content": json.dumps({"tool": calls[0].name, "arguments": calls[0].arguments}, ensure_ascii=False)}`（扁平接缝没有 tool_calls 字段）。

**`run_call(call)` 的语义**（照 `:265-342`）
- `name, args = call["tool"], declared_arguments(call["arguments"])`；`declared = spec.handle.spec(name)`。
- `declared is None` 或 `name in _HIDDEN` → `_DONE` + `finish("该账单操作不被允许。", DENIED)`（纵深防御：`parse_domain_decision` 已经拒绝过模型没被提供的名字）。
- 参数校验不过 → `_DONE` + `finish("账单操作参数不完整或格式不正确，请重新说明。", FAILED)`。
- `declared.side_effect == SideEffect.READ` → `call_tool(...)` → `_READ_ROUND` + 结果本身（activate 用 `tool_outcome`/`tool_envelope` 回喂）。
- `declared.side_effect == SideEffect.WRITE`（只有 `bill_send_report`）→ **不执行**：调 prepare 语义的 handler 取冻结快照，返回 `_PARKED` + `{"bill_call": {"tool": name, "arguments": args}, "bill_action": snapshot, "pending_question": authorization_question(snapshot), "pending_answer": None, "turn_phase": "waiting_for_user"}`（照 `:322-335`）。

**`invoke` / `finish` / `from_result`**（照 `:217-262`）
- `invoke(name, arguments, idempotency_key=None)`：`key = idempotency_key or f"bill:{name}:{uuid4().hex}"`；确认后的 `bill_send_report_execute` **必须**传入 prepare 存进快照的 `idempotency_key`（不是新生成 uuid4）——同一张确认卡被重放时幂等键必须不变，否则会重复发信；因此**冻结那一步**（`run_call` 调 `bill_send_report`）铸造的 key 必须连同 `action_id` 一起写进 `bill_action`，`consume_confirmation` 再把它原样交给 execute（照 `subscription/workflow.py:282-345`，但账单没有 `_needs_selection` 那条前置分支）；`result = call_tool(spec, DomainOutcome(tool_name=name, arguments=arguments), get_stream_writer(), idempotency_key=key)`；`may_retry(spec, result)` → 用**同一个 key** 再试一次；仍 `may_retry` → `raise UpstreamUnavailable("TOOL_UNAVAILABLE")`。同一个 key 是关键：重试必须重放，不能重复发信。
- `finish(summary, status=ToolStatus.SUCCEEDED, ids=None)`：`turn_status = _BILL_TURN_STATUS[status]`；写 `subtask_end` 事件（`agent_id`/`status`/`summary`/`turn_status`）；返回 `{"active_agent": None, "domain_results": [{"agent_id": "bill", "summary": summary, "instruction": "", "business_ids": ids or {}, "turn_status": turn_status}], "bill_call": None, "bill_action": None, **idle_work_state()}`。
- `from_result(result)`：失败 → `finish(f"账单操作未成功：{result.error}", result.status)`；成功 → `finish(result.data["summary"], ids=result.data.get("business_ids"))`。

**确认卡载荷（`authorization_question`）**
```python
{
  "kind": "confirmation",              # 必须等于 CONFIRMATION_KIND，decide 路由的 _click_matches 会校验
  "question_id": f"q_{uuid4().hex}",
  "agent_id": "bill",
  "attempt": 1,
  "display": "请确认：发送账单分析报告\n期间：2026-09-01 至 2026-09-30\n收件人：a@b.com",
  "fields": [{"name": "decision", "label": "操作决定", "type": "select",
              "required": True, "options": ["确认", "取消"]}],   # 必须非空：前端会丢弃没有字段可答的 interrupt
  "confirmation": {
      "pending_id": action_id,          # 服务端生成并存在图状态里；decide 路由要求它等于请求里的 pending_id
      "operation": "send_report",
      "title": "发送账单分析报告",
      "period_start": ..., "period_end": ..., "recipient": ...,
      "confirm_label": "确认", "cancel_label": "取消",
      "expires_at": ..., "requires_code": False,
  },
}
```
- `pending_id` 用自己生成的 id（**不能**匹配 `^CA-[a-f0-9]{32}$`）→ `app/agents/hitl.py` 的 `_authorize_card_action_if_needed` 会跳过，账单发信不需要银行授权，这是期望行为。
- 复用 dev 已有的通用确认路由 `POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`（`backend/app/api/routes.py:461`）。它把点击转成 `context = {"resume": {"question_id": ..., "value": {"decision": ...}}}` 再走一次普通回合 —— **无需为账单新增任何路由**。

**`consume_confirmation(state, answers)`**（照 `subscription/workflow.py:452-497` 的校验顺序，一律 fail closed）
1. `action = state.get("bill_action")`；不是 dict → `finish("这笔操作的确认信息已失效，请重新说明。", DENIED)`。
2. 快照缺 `pending_id` 或 `operation` → 同上 DENIED（缺字段不能 KeyError 成内部错误）。
3. `expired(action)`（超过 `PENDING_EXPIRY_MINUTES`）→ `finish("这笔操作的确认信息已过期，请重新说明。", DENIED)`。
4. `answers.get("pending_id")` 非空且 != `action["pending_id"]` → DENIED。
5. `decision = confirmation_answer(answers)`（本地小函数，同时接受机器值 `confirm`/`cancel` 与卡面标签 `确认`/`取消`，照 `:452-460`）：`cancel` → `finish("本次操作已取消，未发送报告。")`；既非 confirm 也非 cancel → `finish("请明确确认或取消。", DENIED)`。
6. `confirm` → `invoke("bill_send_report_execute", {"period_start": ..., "period_end": ..., "recipient": ...}, idempotency_key=action["idempotency_key"])` → `from_result(result)`。

**handler 拆分（工具 3 → 4）**

| 工具名 | 可见性 | SideEffect | handler 职责 |
|---|---|---|---|
| `bill_query_transactions` | 可见 | READ | 原样（`handlers.py`） |
| `bill_analyze_comprehensive` | 可见 | READ | 原样（`analysis_handlers.py`） |
| `bill_send_report` | 可见 | WRITE | **改为 prepare**：校验已绑邮箱（未绑 → `ToolFailure(VALIDATION, "您还没有绑定邮箱。请先在右上角设置里配置邮箱地址，然后我再发送报告。")`）、解析期间、生成 `action_id`、返回 `ToolData(summary="准备发送…", facts={action_id, period_start, period_end, recipient})`；**不发邮件** |
| `bill_send_report_execute` | **隐藏**（`_HIDDEN`） | WRITE | **execute**：原 `email_handlers.py` 的 PDF 生成 + `send_email` 全流程 |

- 好处：邮箱未绑定时在**冻结阶段**就失败（用户不必点了确认才知道没绑邮箱），且"发信"这一步不可能被模型直接触发。
- `build_module` 需把隐藏工具一并注册进 `tools`/`handlers`（Gateway 校验要用），但图里把它从模型菜单剔除。

**为什么不用更省事的做法**：把发信工具（原 `send_analysis_report`）声明成 `SideEffect.READ` 直接调，虽然代码少，但（a）语义不诚实；（b）`sdk._may_retry` 对 READ 允许重试 → 一次网络抖动可能重复发信；（c）与 card/subscription 的既定写协议不一致，review 时会被质疑。
### 6.4 三个阻断缺口

**(1) alembic 双 head**
- dev 现状：单一 head `0013_agent_run_event_call_id`（`backend/app/storage/migrations/versions/0013_agent_run_event_call_id.py:32-33`，`down_revision="0012_llm_call_audit_stream"`）。
- dev_Analyze 现状：单一 head `0013_bill_compatibility`（`down_revision="0012_llm_call_audit_stream"`）→ 直接合并会产生两个 head。
- **做法**：账单迁移落为 `backend/app/storage/migrations/versions/0014_bill_compatibility.py`，`revision="0014_bill_compatibility"`（23 字符）、`down_revision="0013_agent_run_event_call_id"`；**dev 的 0013 文件一字不动**。结果单链：`0012 → 0013_agent_run_event_call_id → 0014_bill_compatibility`。
- 注意 `alembic_version.version_num` 是 `String(32)`：新增 revision id 一律保持 ≤≤32 字符（真实上限是 alembic_version.version_num = String(32)；基线里 `0007_conversation_persistence` 已是 29 字符）。
- 附带：`backend/tests/test_business_audit_migration.py:153-156` 必须改（见 6.2 第 12 条）。
- `0014_bill_compatibility` 的 `downgrade()` 保持原样直接 `raise`（不可回滚）。

**(2) 跨用户同文件上传必失败**
- 根因：`docs/contracts/sql/001_init.sql:109-110` 的 `CONSTRAINT statement_document_source_file_hash_key UNIQUE (source_file_hash)` 是**全局唯一**，而 `backend/app/services/bill_import.py:227-234` 的文件级去重 SELECT **带 `BillTask.user_id == task.user_id` 过滤** → 第二个用户上传同一文件时 SELECT 必空 → INSERT 撞全局 UNIQUE → `sqlalchemy.exc.IntegrityError` → `bill_processing.py:99-101` 的裸 `except` 把任务置为 `FAILED/INTERNAL`（`core/bill_errors.py` 里 `DUPLICATE_IMPORT → 409` 定义了但**全仓无 raise 点，是死码**）。
- **做法**：
  1. 新迁移 `backend/app/storage/migrations/versions/0015_bill_doc_tenant_unique.py`（revision `0015_bill_doc_tenant_unique`，27 字符；`down_revision="0014_bill_compatibility"`）：
     - `statement_document` 加 `user_id String(64)`（与 `bill_task.user_id` 同型，后者已被 `0014` 从 Uuid 改成 String(64)）；
     - 回填 `UPDATE statement_document SET user_id = (SELECT user_id FROM bill_task WHERE bill_task.id = statement_document.task_id)`（回填后仍有 NULL 的行需要显式处理：统一按 §9 已定的口径：设为 NOT NULL，**孤儿行让迁移报错（fail loudly）**，不吞、不静默留 NULL；实施前先在测试库跑一次孤儿行计数确认是 0）；
     - `drop_constraint("statement_document_source_file_hash_key")` → `create_unique_constraint("uq_statement_document_user_file", ["user_id", "source_file_hash"])`。
  2. `backend/app/storage/bill_models.py` 的 `StatementDocument` 同步加列与 `UniqueConstraint("user_id", "source_file_hash", name="uq_statement_document_user_file")`。
  3. **兜底（必做）**：`bill_import.py` 的 `db.flush()` 处 `except IntegrityError → raise_bill_error(DUPLICATE_IMPORT, "该账单文件已导入过")`，让 `bill_processing.py:97` 的 `AppError` 分支把 409 真正落到任务上；SELECT-then-INSERT 仍有竞态，在兜底里补一次"重查后判定重复"的处理（或 `pg_advisory_xact_lock`）。
- 为什么不用另外两个方案：去掉 `user_id` 过滤、命中即复用 → 文件哈希变成跨用户存在性 oracle，且第二个用户名下没有自己的 document（`get_summary`/`get_transactions` 全按 user_id 过滤）会读不到数据；删掉全局唯一 → 丢失文件级幂等。两者都比现状语义更差。

**(3) 账户维度是假数据**
- 根因链：`backend/app/analysis/cmb_parser.py:72-85` 把 `account_holder=""`、`account_no=""` 写死（`:77` 注释自认 TSV 表格不含户名/账号）→ `statement_document.account_no_masked` / `account_holder_masked` 恒空 → `backend/app/services/bill_analysis/preprocessor.py:112-114` 又写死 `account_no="主账户"`、`card_last4="****"` → 下游 `analyzer.py:70/:120`、`modules/account_usage.py:20/34/57`、`risk_profile.py:71`、`risk_transactions.py:41`、`notification/analysis_pdf.py:436-450` 全读假值。
- 真值其实在数据里：招行流水的账号/户名就在**前导说明区**，而 `backend/app/billing/handlers/spreadsheet.py:95-103` 的 `_read_sheet` 只留 `values[0]` 作表头、`:116-131` 只渲染表头+数据行，前导行被当作"前 N 行是说明，已跳过"丢掉（`cmb_parser.py:48-51`）。
- **做法（三处打通 + 下游降级）**：
  1. `backend/app/billing/handlers/spreadsheet.py:95-103`（及 `:80-92` 的 `_csv_sheet`）保留前导行进 sheet dict，`:116-131` 的 `_render_sheets_as_text` 带下去。
  2. `backend/app/analysis/cmb_parser.py:93-131` 让被跳过的行内容一并返回，用正则提取账号（`6225` 开头 16–19 位）与户名（"户名"/"姓名" 后跟值），填进 `:72-85` 的 `DocumentInfo`。
  3. `backend/app/services/bill_analysis/preprocessor.py:112-120` **删掉硬编码**，`process()` 加带默认值的形参（`account_no=""`）；`analyzer.py:47-67` 查 `Transaction` 时一并取出 `statement_document.account_no_masked`（该列已落库但全仓无人读取）并下传。
  4. 下游诚实降级：`modules/account_usage.py:54-63` 无账户信息时文案改"账单未提供账户信息，按全部交易汇总"；`analyzer.py:120` `report_meta.accounts` 为空时置 `[]`（`:146` 的空报告已有先例）；`notification/analysis_pdf.py:436-450` 的"账户"列显示"未提供"。
- **无论真值是否提取到，`preprocessor.py:113` 的 `"主账户"` 必须删除**（否则真值也会被硬编码盖掉）。

### 6.5 死代码与清理（后端）

- 不搬 `.uv-cache/sdists-v9/editable/.../*.whl`（构建产物，本来只在 dev_Analyze 的未跟踪缓存里）。
- `backend/app/services/bill_analysis/rules/risk_rules.py:169 check_all_rules`、`rules/normalization.py:64 extract_merchant_name`、`services/bill_query.py:193-238 analyze_spending`（整段注释）——**原样保留**，不在这轮删除（属于实现内部，删了要重跑分析测试，收益低）。若实施时发现确实零引用且删除无测试影响，可顺手删，但**不作为验收项**。

- `backend/app/modules/bill/__init__.py` 的 20 行 shell 版**被替换**（不是被删）：`MODULE = ModuleSpec(name="bill", declaration=BILL)` 这一行必须保留，因为 `agents/domain.py:25` 的 `DOMAIN_ROSTER = build_registry().roster` 在 import 期就要发现 bill。

### 6.6 形态对齐（对照已接入的 transfer/card/subscription/wealth）

**标准形态**（逐个 `git show dev:...` 核过）：`modules/<name>/__init__.py`（声明 + `MODULE`）＋ `modules/<name>/tools.py`（`ToolSpec` + handler 映射）＋ `modules/<name>/workflow.py`（`build_<name>_graph`）＋ 可选 `routes.py`（只放平台 REST）＋ `README.md`，注册只有 `modules/__init__.py` 的 `ALL_MODULES` 一行。

工具接缝**只注入 `BankClient`**：`ToolHandler = Callable[[ToolInvocation, BankClient], ToolData]`（`modules/base.py:99`），`bank_adapter.py:34/:43-73` 只传 `(invocation, self._client)`；`TrustedContext`（`tools/protocol.py:92-104`）只有 `user_id`/`session_id`/`bank_customer_id`，没有任何服务对象；四个有工具模块的 `tools.py` 里**没有任何 `database`/`Session` 引用**（grep 确认），`wealth/application/factory.py` 的 docstring 还明写它"不连 Agent 平台数据库"。

**账单与这个形态的四处偏差，本轮全部收敛**：
1. **接线位置**（真错误，见 6.2 第 1 条）：装配放组合根 `production_modules()`，`registry.py` 一行不改。
2. **工具命名**：改带 `bill_` 前缀（见 6.2 第 10 条）。
3. **文件布局**：声明进 `tools.py`、图进 `workflow.py`、`__init__.py` 只留声明与 `MODULE`/`build_bill_module`（见 6.1、6.2 第 10/11 条）。
4. **`purpose` 语义**：一句话（路由用），行为规则进 `_BILL_PROMPT`。

**两处刻意与形态不同，写进实现注释以免被当成漏改**：
- **账单工具确实需要平台依赖**：`bill_query_transactions`/`bill_analyze_comprehensive` 读平台库的 `bill_transaction`，`bill_send_report` 用 `NotificationService`。已接入模块的工具只打银行端点，所以它们的 handler 只吃 `BankClient`；账单 handler 额外吃 `database`/`notification_service`，**由 `build_bill_module` 在组合根闭包绑定**，不去动 `ToolHandler` 签名，也不往 `TrustedContext` 里塞服务（两者都不改）。
- **账单的 12 个 REST 端点放 `backend/app/api/bill.py`**，不放 `modules/bill/routes.py`：它是平台侧的上传/解析/查询流程（前端直接调用），与 agent 工具无关；dev 有 `app/audit/routes.py` 这个先例。card 的 `routes.py` 之所以在模块里，是因为它服务 agent 侧卡操作（且已退役成 410）。

**图接缝的形态**（照 `card/workflow.py:474-482`、`subscription/workflow.py:551-559`）：`decide`/`wait`/`process` 三节点、两条 `add_conditional_edges(..., route, {"wait": "wait", END: END})`、`wait` 直接复用 `sdk.wait_for_user`。`build_domain_graph`（`agents/supervisor/graph.py:400-435`）只在 `declaration.graph_builder is not None` 时用它，否则 `minimal_shell_graph`；有工具而无 `graph_builder` 会直接 `raise ValueError`——所以账单**必须**声明 `graph_builder=build_bill_graph`（这正是 dev_Analyze 声明版缺的那一行）。

---

## 7. 前端设计

### 7.1 文件清单

**新增 8**（原 12 减去 `BillWorkbench.vue`、`style.css.bak`、`style.css.bak2`，且 `style.css` 是修改）
`frontend/src/AnalysisResult.vue`(744)、`EmailSettings.vue`(416)、`SendAnalysisModal.vue`(325)、`analysisResult.ts`(227)、`analysisResult.test.ts`(119)、`bill.ts`(209)、`dateFields.ts`(48)、`dateFields.test.ts`(71)

**修改 4**
- `frontend/package.json` + `package-lock.json`：加 `echarts ^6.1.0`、`vue-echarts ^8.3.2`（dev 都没有）。
- `frontend/src/api.ts`：只加通用 `upload<T>(path, form)`（`POST /api${path}` 的 multipart 封装，`bill.ts:186` 用）；**不加** `uploadBill()`（零调用者）。
- `frontend/src/App.vue`：按 7.2 的 hunk 分组移植。
- `frontend/src/style.css`：末尾**纯追加** `.interrupt-date` / `.interrupt-choices`(`.interrupt-choice`) / `.bill-entry` / `.bill-card` 全家（`.bill-head`/`.bill-hint`/`.bill-note`/`.bill-steps`/`.bill-fields`/`.bill-field`/`.bill-file-input`/`.bill-file`/`.bill-actions`/`.bill-message`(`.error`)/`.bill-progress`）/ `.settings-email` / `.settled-reports`。

**明确不移植**
- `frontend/src/BillWorkbench.vue`（见 7.3）
- `frontend/src/style.css.bak` / `style.css.bak2`（手工备份，全仓零引用）
- `frontend/src/App.test.ts` / `events.test.ts`：HEAD 版是**旧语义**（`describe('card authorization')` + `FakeServer.cardAuthorization`），dev 版才是新的（`describe('card confirmation via decide')`）。dev 版不动。
- `frontend/src/events.ts` / `types.ts`：HEAD 的 +17/+10 行分别是 `ui_action` 死通道与 card 授权收窄，**都不搬**，两文件保持 dev 原样。

### 7.2 `App.vue` 的 hunk 分组（HEAD 侧行号）

| 组 | hunk | 内容 | 处置 |
|---|---|---|---|
| 账单核心 | #1 `:7,13-14` | `onUnmounted` import + lucide `Receipt` | 搬 |
| | #2 `:27-36` | 引入 AnalysisResult / EmailSettings / `isAnalysisReport` / dateFields 7 符号 / bill.ts 7 符号 | 按需拆开搬 |
| | #3 `:61-87` | `userEmail` / `billOpen` / `bill` / `billForm` / `billFile` / `billPeriodGuessed` / `billGuide` / `billPassword` / `billBusy` / `billError` / `billPoll` / `autoAnalyzed` | 搬 |
| | #7 `:779-781` | `submitTurn` 调 `keepSettledReports` | 搬（**Task 13**，与 #11 同生共死） |
| | #11 `:888-937` | `settledReports` / `promptedSendReports` WeakSet / `hasPromptedSend` / `markSendPrompted` / `settledReportsOf` / `keepSettledReports` | 搬 |
| | #18 `:1270-1520` | **巨型块，必须按函数切三组**：① 通用日期/选择（`dateParts`/`choiceCards`(=`pending.agent_id==='bill'`)/`dateYears`/`monthOptions`/`setDatePart`/`chooseOption`/`dayOptions`）② 报告发送（`reportSentInTurn`/`sendReport`）③ 账单卡（`billRunning`/`billRemainingAttempts`/`stopBillPolling`/`startBillPolling`(1s)/`refreshBill`/`autoAnalyze`/`billAction`/`openBill`/`closeBill`/`createBillTask`/`createAndUpload`/`confirmBillApplication`/`pickBillFile`/`uploadBillFile`/`submitBillPassword`/`cancelBill`） | 搬（分切；**第 ① 组推迟到 Task 14** —— ① 只被 `#5` / `#24` 使用，而这两处都在 Task 14，提前搬会让 Task 12 的 `vue-tsc` 因 `noUnusedLocals` 变红） |
| | #19 `:1523-1524` | `onMounted` bootstrap + `onUnmounted(stopBillPolling)` | 搬 |
| | #20 `:1533,1539,1541-1544` | 顶栏"上传流水" `.bill-entry` 按钮 + 挂 `<EmailSettings :current-email="userEmail" @update="handleEmailUpdate">` | 搬（"模型设置"→"系统设置"的标题文案可弃） |
| | #22 `:1662` | `tool_result` 分支渲染 `<AnalysisResult>`，旧 summary/blocks 退 else | 搬（**高风险**） |
| | #23 `:1677-1688` | settled-reports 块 | 搬 |
| | #26 `:1781-1890` | 整段 `<section v-if="billOpen" class="bill-card">`（选文件/周期猜测/`billGuide` 招行 APP 指引/WAITING_APPLICATION/WAITING_UPLOAD/WAITING_PASSWORD 含密码剩余次数/`billRunning` 进度条/COMPLETED/FAILED/CANCELLED） | 搬 |
| 报告渲染依赖 | #4 `:233-241` | `analysisResultOf(result)=isAnalysisReport(result.data?.display)` + `toolResultHasContent` 提前返回 true | 搬（**全移植中语义风险最大**：改的是通用 tool_result 判定，dev 上所有工具结果的渲染分支都跟着变） |
| | #6 `:553,558-566` | `loadHistory(id, {fresh})` 用 `'#'+Date.now()` 绕开 `share()` 去重 | 搬 |
| | #9 `:858` | `settle` 改调 `loadHistory(..., {fresh:true})` | 搬 |
| | #10 `:870-878` | `settle` 新增 `arrived` 判定后 `keepSettledReports` + 清 live | 搬 |
| 通用卡表单 | #5 `:331-341` | `setPending` 给 `type==='date'` 字段播种 `dateParts[field.name]=todayParts()`、`answers=composeDate(...)` | 搬（账单问题卡的 `period_start/period_end` 依赖；dev 的 `App.vue` 目前**没有**任何 date 处理，只有一处 `:type` 三元） |
| | #24 `:1704-1748` 的两段 | `v-else-if="field.type==='date'"` 年月日三联 + `field.type==='select' && choiceCards` 按钮式选择卡（`chooseOption`，不自动提交） | 只搬这两段；**同 hunk 里的 card select 段不动** |
| 邮箱基础设施 | #15 `:1145` | `startSession` await `loadUserEmail()` | 搬 |
| | #16 `:1160-1166` | `bootstrap` await `loadUserEmail()` | 搬 |
| | #17 `:1216-1235` | `openLlmSettings` await `loadUserEmail()` + `loadUserEmail()`(`GET /settings/email`) + `handleEmailUpdate`（'邮箱设置已保存'/'已清除邮箱绑定'，3s） | 搬（依赖 6.2 第 4 条的 GET/DELETE 端点） |
| **不搬：card 旧语义** | #12 `:964-967`、#13 `:981-1006`、#14 `:1069`、#25 `:1755-1761` | `submitCardAuthorization`（`POST /card-actions/{action_id}/authorize` + 红级 6 位验证码）、'确认本次理财操作' 文案、模拟验证码 input + `pane.cardAuthorizationError` | 不搬（dev 已是 `pending-actions/decide` 版） |
| **不搬：死代码** | #8 `:813-815` | `else if (event.name === 'ui_action') → openBill()` | 不搬 |
| | #21 `:1560-1565` | 被注释的 `<BillWorkbench>` 块 | 不搬 |

### 7.3 删除 `BillWorkbench.vue`

- 全仓 `git grep BillWorkbench` 只有 `App.vue:1563` 的注释文字与 `:1564`（在 `1560-1565` 的 HTML 注释里），**没有任何 `import`** → 那段注释不是"解开就能跑"。
- 它是前端 `/summary`(`BillWorkbench.vue:32`) 与 `/analysis`(`:34`) 的唯一调用者，但**不是**用户看报告的主线：顶栏"上传流水"(`App.vue:1533`) → `bill-card`(`:1781-1890`) → `autoAnalyze` → 工具 `bill_analyze_comprehensive` → 报告由 `AnalysisResult.vue` 渲染在对话里（live `:1662` / settled `:1677-1688`）。
- 接线会与 `App.vue:64-70` 与 `:1560-1563` 记录的既定决策（"bill_summary 单文档缓存与深度分析口径不一致、常显示 0.00，改成自动深度分析、报告出现在对话里"）冲突，且与 `bill-card` 形成两条并行建任务路径、争用同一 `billOpen` 标志。
- 删除的唯一损失：`GET /api/bill/tasks/{id}/summary` 与 `/analysis` 在前端成为孤儿端点。**后端接口保留**，用户入口不受影响。

### 7.4 前端死代码清理

- `ui_action` 整条通道不移植（`App.vue:813-815`、`events.ts:119-129`、`types.ts:89-95`+`:147`）。后端已自证其死：`backend/app/modules/bill/__init__.py:67-71` 注释写明本仓库 `DomainDeclaration` 没有 `actions` 字段（主分支去掉了整条界面动作通道）；`git grep ui_action dev -- backend` 退出码 1；`open_bill_upload` 只存在于提示词文本 `modules/bill/__init__.py:30,35,68`。
- 不移植 `api.ts` 的 `uploadBill()`（零调用者）。
- `bill.ts` 的无外部调用导出（`formatAmount:94`/`formatSigned:101`/`formatNet:111` 三者内部互用、`getTransactions:199`）及专用类型 `BillTransactionView:64`/`BillFilters:81`/`BillPage:75`、注释掉的死块 `bill.ts:41-62` 与 `:197-198`(getSummary) —— 实施时整组删除（纯函数，无风险）。
- `App.vue:64-70` 的"不再展示收支统计"注释是**决策记录，保留**。

---

## 8. 验收标准

### 8.1 先量 dev 基线（区分"我引入的失败"与"dev 本来就红的"）

在**主工作区**（`D:/ai_project/agent`，dev，已有 `.venv`/`node_modules`）跑：

```powershell
# 后端（仓库根，脚本自带 TEST_DATABASE_URL 与 -Root/-Parallel 支持）
./pytest-local.ps1 -Parallel 8
# 前端
npm --prefix frontend run test
npm --prefix frontend run build
```

记录通过的/失败的测试名，形成基线清单。`backend/tests/test_context_migration.py:177` 实测钉的是**根** revision（`0001_billing` 的 `down_revision=None`），2026-10-15 基线全绿；移植后若它变红即新回归。

### 8.2 在 worktree 装依赖并跑全量

```powershell
cd D:/ai_project/wt-dev_Analyze/backend ; uv sync
cd D:/ai_project/wt-dev_Analyze/frontend ; npm ci
# 三件套
D:/ai_project/wt-dev_Analyze/pytest-local.ps1 -Root D:/ai_project/wt-dev_Analyze -Parallel 8
npm --prefix D:/ai_project/wt-dev_Analyze/frontend run test
npm --prefix D:/ai_project/wt-dev_Analyze/frontend run build   # vue-tsc --noEmit && vite build
```

**通过判据**：三件套结果中，失败集合 ⊆ 8.1 记录的 dev 基线失败集合；账单相关测试与迁移测试全绿。

### 8.3 必须新增的测试

**后端（5 项）**
1. 迁移链单 head 且 `0013_agent_run_event_call_id` 可从 head 到达（替代被改掉的 head 断言）。
2. 跨用户同文件上传（**按 §6.4(2) 的最终做法：唯一键改为 `(user_id, source_file_hash)`**）：用户 A 上传文件 X 成功 → 用户 B 上传同一文件 X **也成功**，且两人各自拥有自己的 `statement_document` 行与交易行（任务状态不是 `INTERNAL`）；**同一用户**重复上传同一文件走文件级去重、返回 duplicate 而不新增行；去重查询空手而归后的 INSERT 竞态（`IntegrityError` 后重查仍不命中）以 `DUPLICATE_IMPORT`(409) 落到任务上，不是 `INTERNAL`。
3. 账户真值：`cmb_parser` 能从含前导说明区的样例中提取账号/户名；`preprocessor` 在无账户信息时不再产出 `"主账户"`。
4. 账单工具端到端：`bill_analyze_comprehensive` 走 `bank_adapter` 分发返回 `ToolData`；`bill_send_report`(prepare) 在未绑邮箱时**抛** `ToolFailure(VALIDATION)` 且**不**产生发送（handler 是 raise 不是 return：`modules/base.py:98` 的 `class ToolFailure(Exception)`，由 `tools/bank_adapter.py:65-76` 转成 `ToolResult.error_code`）；`bill_send_report_execute` 在绑定邮箱时调用 `send_email` 且成功判据为 `status in {"SENT","QUEUED"}`。
5. **形态测试**（新增 `backend/tests/test_bill_module.py`，照 `backend/tests/test_card_module.py:99-146` 的写法）：
   - `MODULE`（无参、import 期用）的 `tools == {}` / `handlers == {}`（`ModuleSpec.tools` 是 dict，见 `modules/base.py:108-109`）；
   - `build_bill_module(notification_service=..., database=...)` 的 4 个工具名逐个匹配 `^bill_[a-z_]+$`，且 `set(tools) == set(handlers)`；
   - `build_registry(production_modules(...))` 之后 bill 的 handle 里有这 4 个名字（**这条正是能抓住"装配放错位置"的测试**：装配若留在 `build_registry` 内部，无参路径就只剩 0 个工具）；
   - `bill_send_report_execute` 不在 `build_bill_graph` 交给模型的菜单里（对 `_HIDDEN` 过滤后的 `schemas` 断言）。

**前端（2 项）**
6. `dateFields.test.ts` / `analysisResult.test.ts`（随移植带入，必须绿）。
7. `App` 级：`tool_result` 带 `display` 报告时渲染 `AnalysisResult`（覆盖 7.2 的 #4 + #22 这对高风险改动），以及账单问题卡渲染 `type==='date'` 的年月日三联。

### 8.4 手动冒烟（可选，建议做一次）

起后端 + 前端，走一遍：上传流水 → 选周期 → 自动深度分析 → 报告渲染在对话里 → 点"发送到邮箱" → 确认卡 → 收到/入队邮件。

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| #4 改通用 `tool_result` 判定 | dev 上**所有**工具结果的渲染分支都可能变 | 前端全量 + 8.3 第 6 项专项测试；实施时把 #4 单独成一个提交，便于回退 |
| `App.vue` hunk 移植与 dev 的 card 表单改造同区域 | 语义冲突（不是文本冲突） | 严格按 7.2 的分组；card 的 4 个 hunk 一个都不搬；`App.test.ts` 保持 dev 版并作为回归闸 |
| `0015` 回填 `statement_document.user_id` | 孤儿行（`task_id` 指向不存在的 `bill_task`）会让 NOT NULL 回填失败 | **已定**：回填用 `UPDATE statement_document d SET user_id = t.user_id FROM bill_task t WHERE d.task_id = t.id`，**不吞孤儿行**——dev 从未跑过账单链路，账单表在任何有意义的环境里都是空的，孤儿行只可能是人为造出来的，此时迁移报错（fail loudly）比静默留 NULL 更诚实；实施前先在测试库 `SELECT count(*) FROM statement_document d LEFT JOIN bill_task t ON d.task_id = t.id WHERE t.id IS NULL` 确认是 0 |
| SELECT-then-INSERT 竞态 | 极端并发下仍可能 IntegrityError | 兜底 except 已把 409 落到任务上；必要时 `pg_advisory_xact_lock` |
| 新依赖（echarts / vue-echarts / python-multipart / pypdf / python-docx） | 锁文件变更、构建体积 | 只加不升级其它依赖；`uv sync` / `npm ci` 在 worktree 内验证 |
| `0014` 的 `downgrade` 直接 raise | 迁移不可回滚 | 保持原样（与 dev_Analyze 一致），在 spec 与迁移注释里写明 |
| 装配位置/形态偏差（dev_Analyze 把服务塞进 `build_registry`） | 生产与无参两条路径的 bill 工具集不一致，图拿到空菜单仍会跑 | 6.6 收敛为组合根 `production_modules()`；8.3 第 5 项形态测试把这条钉住 |
| 全量测试耗时 | 本地并行约 2m16s（云库 1h11m） | 用 `-Parallel 8`；主工作区与 worktree 不要同时跑（同一个 `test` 库、schema 前缀隔离但不共享锁） |

## 10. 已知缺口（本轮不处理，记录在案）

1. `backend/app/core/bill_config.py` 的 `bill_password_ttl_seconds` 只有定义、无任何读取点（TTL 形同虚设）。**本轮不动**：接上它会改动上传密码流程的状态机，超出移植范围。
2. `docs/contracts/sql/001_init.sql` 与 `storage/bill_models.py` 的模型漂移：`bill_task.session_id/password_secret_ref/password_expires_at/completed_steps/progress`、`statement_document.original_filename/parse_issues`、`bill_transaction.raw_amount_text` 未映射。**保持现状**。
3. `GET /api/bill/tasks/{id}/summary` 与 `/analysis` 在前端成为孤儿端点（后端保留）。
4. `bill_task_source_mode_check` 允许 `EMAIL`/`LOCAL_SAMPLE`，但代码只走 `manual_upload`。
5. `backend/app/services/bill_analysis/rules/risk_rules.py:169 check_all_rules` 等内部死函数（见 6.5）。
6. 前端没有任何账单测试覆盖（本轮只补 8.3 的 2 项）。
