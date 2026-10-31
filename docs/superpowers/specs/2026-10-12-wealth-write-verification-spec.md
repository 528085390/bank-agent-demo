# Spec 3 — 理财写流程的基座与 API 层端到端验收（B2 版）

- 状态：**待执行**（§11 三项已裁定；§2.7 已按 B2 重写）
- 日期：2026-10-12
- 基线：`D:\ai_project\agent` @ `bc70879`（dev）
- 上位文档：`docs/superpowers/plans/2026-10-12-unify-tool-calling-plan.md`
- 兄弟 spec：`docs/superpowers/specs/2026-10-12-abolish-json-text-protocol-spec.md`（spec1，基座）、`docs/superpowers/specs/2026-10-12-wealth-native-tool-calling-spec.md`（spec2，理财改造为 B2 领域图）

## 0. 结论

理财写流程**已经打通**，缺的不是能力，是**这一条链上的自动化验收**：

- 模块内已经覆盖得很完整——`backend/tests/wealth/` **386 collected / 385 passed + 1 环境失败**（`test_cli` 需 `.env`，非行为回归；实测 `uv run pytest tests/wealth -q`），其中写流程四个文件 **103 个测试函数 / 收集 125 个用例**；
- 真机已经端到端跑通——`backend/tests/wealth_integration/` **44 条**（`--collect-only` 实测；真跑需银行进程与专用 schema）且历史全绿（真实 HTTP 银行 + 真实 PostgreSQL），
  另有本次（2026-10-12）经基座与前端的手工四轮实证（§2.5）；
- 但**从 HTTP/SSE 进来、经 Supervisor 派单、经确认卡回到模块**这条链上，**没有一条自动化用例**：
  `tests/test_domain_turn.py` 用的是替身 `TurnSpy`，`tests/test_pending_action_api.py` 用的是替身 `ConfirmingSpy`，
  而 `wealth_integration/test_acceptance.py` 的 **A15 明确断言“不启动 Supervisor”**（`test_A15_no_frontend_no_supervisor_no_platform_database`，`:1265`）。
  三层各自都对，接缝处没人看。

**spec2 把 wealth 改成 B2（与 card 对齐的领域图）之后，这个接缝换了个位置，但缺口原样存在**：
从「基座 ↔ `WealthTurn`」变成「基座 ↔ wealth 的领域图」。`WealthTurn` 与 `build_turn` 会被删除（spec2 §10），
`tests/test_domain_turn.py` 的 `TurnSpy` 与 `test_pending_action_api.py` 的 `ConfirmingSpy` 仍然是替身——
**没有任何一条用例把真实的 wealth 领域图接到基座或 HTTP 上**。本 spec 就补这一条。

本 spec 只补这条链的验收与回归：**不新增资金写能力、不改模块行为、不改事件契约**。

## 1. 需求

| # | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 真实 wealth 领域图接到基座图上能走完四轮 | 新增用例：追问中断 → 续跑 → 追问中断 → 确认中断 四态依次出现 |
| R-2 | 确认回环在 API 层成立 | `POST /api/conversations/{id}/pending-actions/{pending_id}/decide` 带正确验证码后写**恰好一次**；不带码被拒且银行收不到写 |
| R-3 | 写流程的 SSE 帧符合冻结契约 | 帧名与 `data` 字段集合逐帧等于 `EVENT_FIELDS` |
| R-4 | `turn_status` 三态如实投影 | `subtask_end.turn_status` 与 `domain_results[].turn_status` 同值；card 完成后不再被重复派单（§5） |
| R-5 | 模块行为零改动 | `tests/wealth` 386 条（385 passed + 1 需 `.env`）与 `tests/wealth_integration` 44 条全绿；条数不变（装配路径按 spec2 §12.4 改写不算行为改动） |
| R-6 | 真机冒烟可复现 | §6 的 S01–S06 有执行记录，含帧序列与余额前后值 |
| R-7 | B2 的三处行为变化被钉住 | 追问是 `interrupt kind=input` 且需 `resume` 信封；答案键与 `WEALTH_FIELDS` 完全一致；读路径有 `tool_call`/`tool_result`（§2.7） |

## 2. 既有事实

### 2.1 四层测试的现状

| 层 | 文件 | 驱动力 | 覆盖 | 缺口 |
| --- | --- | --- | --- | --- |
| 模块单元 | `tests/wealth/`（16 个 .py，其中 13 个 `test_*.py`；386 collected / 385 passed + 1 需 `.env`） | 脚本模型 + 银行替身，直接调 `WealthAgent` | 槽位、问卷、推荐、对比、预览、确认、验证码、幂等、对账、越门禁 | 不经过基座 |
| 基座整轮 | `tests/test_domain_turn.py`（807 行） | **替身** `TurnSpy`(`:70`)/`ConfirmingTurnSpy`(`:93`) | 基座对整轮模块的契约：状态映射、暂停、恢复、按钮事件 | **没有真实 wealth** |
| API/SSE | `tests/test_pending_action_api.py`（**替身** `ConfirmingSpy`(`:58`)）、`tests/test_multi_agent_api.py`（合成 echo 模块） | 替身 / 合成模块 | 路由、幂等键、越权拒绝、账本、卡片刷新 | **没有真实写** |
| 真机 | `tests/wealth_integration/`（44 passed） | 真实 HTTP 银行 + 真实 PostgreSQL + `build_wealth_agent` | A01–A15 全矩阵（含 A14 真实模型） | **A15 刻意绕开 Supervisor** |

### 2.2 基座到领域图的调用链（B2 之后，接缝就在这里）

```
app/runtime.py:69  agent_graph_factory(gateway, turn_handlers=None)
                     ← 今天的形参里还有 *, protocol=（spec 1 S-9 删除；此处按删除后写）
  → app/agents/domain.py:54  build_agent_graph(..., turns=turn_handlers)
  → app/agents/supervisor.py:2029  build_domain_graph(model, spec, assembler=, turn=)
                                   （:2064 调 graph_builder）
       if spec.declaration.graph_builder is not None and spec.handle.tools:
           return spec.declaration.graph_builder(model, spec, assembler=assembler, turn=turn)
  → app/modules/wealth/workflow/graph.py  build_wealth_graph(model, spec, *, assembler, turn)
       decide → wait(wait_for_user) → process        ← 与 card/workflow.py:263-274 同形
  → app/modules/wealth/application/factory.py:26  build_wealth_agent(...)   ← 写逻辑仍在这里
```

**`app/runtime.py:57-66` 那段注释（“必须只造一次，否则每轮失忆”）在 B2 之后不再适用**：
状态从 `WealthTurn` 的进程内 `_pending_ids` 搬进图 state，由 checkpointer 持久化（生产是 `app/main.py:45-46 postgres_checkpointer`）。
这正是 B2 的收益（spec2 §2.1）。本 spec 的 V1-7 因此改成钉住**图 state 的跨轮可见性**，而不是“模块只造一次”。

### 2.3 确认卡的两条通道

1. **展示**：`_conclude_turn`（`supervisor.py:667`）在 `result.status is TurnStatus.WAITING_CONFIRMATION` 时
   写一个真正的 checkpoint interrupt（`:711-714`），SSE 上是 `interrupt` 帧，字段
   `{kind, question_id, agent_id, fields, display, attempt}`，`kind == CONFIRMATION_KIND == "confirmation"`（`app/agents/turn.py:54`）。
2. **授权**：只有 `POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`（`app/api/routes.py:461`），
   体是 `{question_id, decision: "confirm" | "cancel", verification_code?: str}`（`app/api/schemas.py:90-91`）；
   路由把它翻成客户端上下文 `{"resume": {"question_id": ..., "value": {"decision": ...}}}`（`:499`），`TurnAction(kind="confirm", pending_id=...)` 由图里的 process 节点生成（`card/workflow.py:212-261`），
   并在 `:525-526` 于卡片要求验证码而本次点击没带码时**在银行之前**拒绝。
   聊天里说“确认”走普通消息通道，暂停中的问题直接拒（`test_pending_action_api.py:235` 已钉住）。

**B2 之后第 2 条不变**（按钮事件仍走 `TurnAction` → 图的 process 节点），
**第 1 条多了一种中断**：`TurnStatus.WAITING_INPUT` 也变成 `interrupt`（`kind="input"`），
今天它只是一个 `subtask_end.turn_status`（§2.7）。

### 2.4 状态投影的现状

- `TurnStatus`（`app/agents/turn.py:26-46`）六值：`completed` / `waiting_input` / `waiting_confirmation` /
  `unsupported` / `failed` / `unknown`；`:49 TERMINAL_TURN_STATUSES = frozenset(status.value for status in TurnStatus)`。
- `_conclude_turn`（`supervisor.py:686-697`）同时发 `subtask_end.turn_status`（模块自己的状态）与
  `domain_results[].turn_status`（`supervisor.py:703`）；事件层的 `subtask_end.status` 仍是冻结的
  `SUCCEEDED/FAILED/DENIED` 三值（`_TURN_EVENT_STATUS`，`app/agents/supervisor.py:141`）。
- `_already_concluded`（`supervisor.py:1909-1924`）靠 `domain_results[].turn_status in TERMINAL_TURN_STATUSES` 判“这个领域已经说完”。

### 2.5 真机四轮实证（2026-10-12，经基座 + SSE + 前端；B2 之前）

| 轮 | 用户输入 | `subtask_end.turn_status` | 其他帧 |
| --- | --- | --- | --- |
| ① | 「我想申购1000元理财产品」 | `waiting_input` | 追问用钱时间与流动性 |
| ② | 「6个月内不用，不需要随时赎回」 | `completed` | 列 3 只候选（`p_demo_cash`/`p_money_fund`/`p_yuebao`） |
| ③ | 「选第2只，货币基金」 | `waiting_input` | 追问扣款账户（`a_wealth_main` 19,500.00 / `a_wealth_backup` 5,000.00） |
| ④ | 「用第二个账户 a_wealth_main」 | `waiting_confirmation` | `interrupt kind=confirmation`，带 `pending_id=wp_986e43737a034ceca9410a858b32b83f`、`amount_minor=100000`、`requires_code=true` |

读路径对照：「我有哪些理财产品」47 帧 = `agent_start(wealth)` + `subtask_end` + `reply_delta × 25` + `final`，
**0 个 `tool_call`/`tool_result`/`narration`**（整轮模块自己办完，基座不产生工具帧）。

**B2 之后这两行对照都变**（§2.7 第 3 条）：读路径会多出 `tool_call`/`tool_result`，第 ①/③ 轮会变成 `interrupt kind=input`。
§6 的 S01/S02 与 §3 的 V1-1/V1-2 都按 B2 后的形状写。

### 2.6 已发现的缺陷（本 spec 的 §5 验收对象）

`app/modules/card/workflow.py:95-113` 的 `finish()` 自己发 `subtask_end`（`{"event": "subtask_end", "agent_id": "card",
"status": status.value, "summary": summary}`，**不带 `turn_status`**）并直接返回 `domain_results`，
不经过 `_conclude_turn`。于是 `_already_concluded`（`supervisor.py:1922`）对 card 永远为假，
Supervisor 可以把已经结束的 card 再派一次，空转到 `MAX_DOMAIN_TURNS`，`final.terminal_reason` 变成 `max_turns`
（`supervisor.py:168 TERMINAL_MAX_TURNS = "max_turns"`）。修法已在 spec1 列为 **T-10**；本 spec 补验收用例。

### 2.7 B2 给本 spec 带来的三处改动

| # | B2 之前（本 spec 上一版） | B2 之后 |
| --- | --- | --- |
| 1 | 脚手架 `build_turn(bank)` 造真实 `WealthTurn` | `WealthTurn`/`build_turn` 已删；脚手架走 `build_registry()` + `graph_builder`，银行替身由图构建注入（spec2 §10） |
| 2 | 第 ①/③ 轮是 `subtask_end.turn_status == "waiting_input"` | 第 ①/③ 轮是 `interrupt kind="input"` + `pending_question`，恢复必须发 `resume` 信封 |
| 3 | 读路径 0 个 `tool_call`/`tool_result` | 7 个只读工具改走基座 `_call_tool`，读路径有 `tool_call`/`tool_result` |

第 2 条的连带在基座侧是硬约束：`_read_answer`（`backend/app/agents/supervisor.py:1134-1170`）要求
`value` 的键集合与问题的 `fields` **完全相等**，否则回 `"补充信息与当前问题不匹配。"` 并重问；
信封指错问题则抛 `AppError("INVALID_INTERRUPT_ANSWER", ..., 409)`。
所以理财的追问字段必须来自 `WEALTH_FIELDS`（spec2 §8），前端必须发 `resumeFor(question, value)`——**这条通道已经存在**（`frontend/src/App.vue:592`，card 的 `ask_user` 今天就在用，见 spec2 §10.1），本 spec 只需 V2-7 钉住「裸聊天消息被拒」。

## 3. V-1 基座层写流程用例

**新建 `backend/tests/test_wealth_graph_base.py`**（**不起 HTTP**：不建 `TestClient`、不起前端；只借 `test_pending_action_api.py:78-109` 那段脚手架里**除 `TestClient` 之外**的图装配形态）。

> 与 spec2 §12.4 的 `test_wealth_graph.py` 分工：那个测**模块自己**（`build_wealth_agent` + 图的装配语义、
> 重启不失忆、thread_id 隔离）；本文件测**经基座**（`AgentRuntime` + `build_agent_graph` + 派单 + 事件）。
> 两者不重复：本文件的每条断言都含 `agent_start`/`subtask_end`/`final` 这类基座产物。

脚手架照 `tests/test_pending_action_api.py:78-109` 的 `paused(...)` 形态，把 `ConfirmingSpy` 换成真实模块：

```python
from app.agents.domain import build_agent_graph
from app.modules.wealth.workflow.graph import build_wealth_graph
from tests.wealth.support import BankDouble

bank = BankDouble()
registry = build_registry()                   # 生产模块集：MODULE 声明 tools/handlers/graph_builder
gateway = ToolGateway(registry.catalog, RecordingAdapter({}))
# 银行注入点（C-3）：build_registry 只收 modules（app/modules/registry.py:38），没有 bank 形参。
# 银行只能经 spec2 W-0 的 client= 一路透传进来：
#   Runtime.bank_client → agent_graph_factory(..., client=bank) → 内层 factory(...) →
#   build_agent_graph(..., client=bank) → build_domain_graph(..., client=bank) →
#   graph_builder(model, spec, assembler=..., turn=..., client=bank)
# 参数名以理财计划 Task 1 Step 3 落地的签名为准；**不要**写 partial(agent_graph_factory(gateway), bank=bank)
# —— 基线 agent_graph_factory 没有 bank 形参，那样是 TypeError（spec3 旧稿的错误示范，已删）。
runtime = AgentRuntime(model, graph_factory=agent_graph_factory(gateway, client=bank),
                       checkpointer=InMemorySaver(),
                       customer_resolver=rt.identity.bank_customer_id)
```

模型替身用 `tests/support/scripted_model.py`：spec2 落地后财富决策走 `stream(tools=)`，
脚本按 `("call", name, json.dumps(arguments))` 步骤写（spec1 §12 已把该替身的 `stream` 列为必做项）。

| # | 用例 | 断言 |
| --- | --- | --- |
| V1-1 | `test_the_first_turn_asks_for_the_missing_slot` | `subtask_end.turn_status == "waiting_input"`；**有** `interrupt` 帧且 `kind == "input"`、`fields` 是 `WEALTH_FIELDS` 的子集；`final` 有文本 |
| V1-2 | `test_the_answer_round_trips_and_the_candidates_arrive` | 带 `resume` 信封投第二答后 `turn_status == "completed"`；候选列表在 `final.text`；`domain_results[0]["turn_status"] == "completed"` |
| V1-3 | `test_the_preview_becomes_a_confirmation_pause_not_a_message` | 第四投后 `turn_status == "waiting_confirmation"`；图的 `__interrupt__` 是 `kind == "confirmation"` 且带 `pending_id`；`final.text` **不含**待确认金额（预览走卡片，不走气泡） |
| V1-4 | `test_a_chat_message_cannot_confirm_a_preview` | 暂停中投「确认」→ 拒绝（`INVALID_INTERRUPT_ANSWER`）；银行替身写次数 **0** |
| V1-5 | `test_the_button_event_writes_once` | `TurnAction(kind="confirm", pending_id=...)` + 正确验证码 → `completed`；银行替身写次数 **恰好 1**；`business_ids` 带订单号 |
| V1-6 | `test_a_second_click_does_not_write_again` | 同一 `pending_id` 再点一次 → 仍是 1 次写，返回同一订单号 |
| V1-7 | `test_the_graph_state_carries_the_slots_across_turns` | 两投之间槽位不丢；**图 state**（而非进程内对象）里有槽位；换 `thread_id` 看不到 |
| V1-8 | `test_the_verification_code_never_reaches_the_model` | 遍历 `model.messages`，任何一条都不含验证码字面量 |

## 4. V-2 API/SSE 层确认回环用例

**新建 `backend/tests/test_wealth_write_api.py`**，把 `test_pending_action_api.py:78 paused()` 里的
`ConfirmingSpy` 换成真实的 wealth 模块，其余（`create_app(rt.settings, runtime=rt)`、
`agent_graph_factory(gateway)`、`X-Session-Id` 头、`Idempotency-Key`）照抄。

| # | 用例 | 断言 |
| --- | --- | --- |
| V2-1 | `test_the_write_flow_streams_the_documented_frames` | 每帧键集合**恰好等于** `EVENT_FIELDS[name]`（表在 `test_multi_agent_api.py:53-62`）；`interrupt` 帧只有 `kind`/`question_id`/`agent_id`/`fields`/`display`/`attempt`。**`confirmation` 不在事件流里（I-22）**：它只在 `GET /api/conversations/{id}` 的 `pending_interrupt.confirmation`（`test_pending_action_api.py:133-140`），所以另加一条断言钉住 GET 投影；确认轮的帧序列是 `agent_start` → `subtask_end` → `final` |
| V2-2 | `test_the_click_completes_the_write_and_the_ledger_records_the_answer` | 写 1 次；账本多两条（`interrupt_answer` + `assistant`）；`GET /api/conversations/{id}` 的 `pending_interrupt` 变 `null` |
| V2-3 | `test_a_click_without_the_code_is_refused_before_the_bank` | 卡片 `requires_code=true` 而点击不带码 → 409 或可读错误；**银行写 0 次** |
| V2-4 | `test_a_wrong_code_is_reported_and_no_second_write_happens` | 错误码如实报告（不宣称成功、不宣称失败）；余额不变 |
| V2-5 | `test_the_card_survives_a_graph_rebuild` | `rt.agent = rebuild()` 后 `pending_interrupt.confirmation.pending_id` 与用户看到的一致 |
| V2-6 | `test_a_click_on_a_stale_pending_is_refused` | 改金额换 `pending_id` 后点旧编号 → 拒绝，写 0 次 |
| V2-7 | `test_a_plain_message_cannot_answer_an_open_input_interrupt` | 第 ① 轮之后投裸聊天消息（不带 `resume`）→ 409 `INVALID_INTERRUPT_ANSWER`；`pending_question` 不变 |
| V2-8 | `test_the_resume_envelope_keys_must_match_the_question` | `resume.value` 的键少一个 / 多一个 → 重问（不 500、不丢槽位） |

## 5. V-3 `turn_status` 投影与 T-10

1. **spec1 的 T-10 落地**：`app/modules/card/workflow.py:95-113` 的 `finish()` 补 `turn_status`
   （由 `TurnStatus` 决定，成功即 `"completed"`），并让它的 `domain_results` 条目带上同一字段。
2. **验收用例**（放进基座计划 Task 2 新建的 `backend/tests/test_turn_status_projection.py`，**只能追加不同名用例**）：
   - `test_a_finished_card_turn_is_not_dispatched_again`：**由基座计划 Task 2 拥有（T-10）**，本 spec 不重复定义；
   - `test_the_event_status_and_the_turn_status_are_both_published`：`subtask_end.status ∈ {succeeded, failed, denied}` 而 `subtask_end.turn_status ∈ TurnStatus`，且 `domain_results[].turn_status` 与之一致；
   - `test_wealth_waiting_confirmation_is_terminal_for_the_dispatch`：wealth 的 `waiting_confirmation` 在 `TERMINAL_TURN_STATUSES` 里，不会被重复派单。

   （C-2：同一文件里重复定义同名 `def` 会触发 ruff F811，且 Python 只保留后一个定义、静默丢掉基座的断言。验收计划 Task 1 Step 3 已按这条改写。）

## 6. V-4 真机冒烟清单（人工执行，结果归档）

前置与 `backend/tests/wealth_integration/验收记录.md` §1 相同（银行独立进程 + 专用 schema + `seed_wealth_demo`），
另加后端与前端进程。逐条记录 **SSE 帧序列** 与 **余额前后值**。

| 编号 | 场景 | 期望（B2 之后） |
| --- | --- | --- |
| S01 | 「我有哪些理财产品」 | `tool_call`/`tool_result` **各至少 1 个**（B2 第 3 条）；`final.text` 有真实持仓金额 |
| S02 | 四轮写流程（§2.5） | ① `interrupt kind=input` → ② `completed` → ③ `interrupt kind=input` → ④ `interrupt kind=confirmation`；每次 `resume` 的键与 `WEALTH_FIELDS` 一致 |
| S03 | 点确认 + 正确验证码 | `completed`；余额减少恰好一笔 |
| S04 | 点确认 + 错误验证码 | 如实报告；余额不变 |
| S05 | 确认框出现后刷新页面 | 卡片仍在（`pending_interrupt` 与 `pending_id` 不变） |
| S06 | 点取消 | 余额不变；无订单产生 |

归档位置：`docs/superpowers/reviews/2026-10-12-wealth-write-smoke.md`（新建）。

## 7. 与 spec1 / spec2 的交界

- **依赖方向**：spec1 改基座（删 `json_text`、删降级、抽 `app/agents/toolcalls.py`、修 decide 的双 try），
  spec2 把理财改成 B2 领域图，**spec3 只加用例、只加一处 card 的 `turn_status`（T-10）**。
- **强依赖 spec2 的 W-0**：`build_domain_graph` 必须把 `assembler`/`turn`/`client` 三个关键字参数传给 `graph_builder`（spec2 §7.1 + C-3），
  否则图拿不到 handoff、`TurnContext.thread_id` 与银行替身，V-1/V-2 无法构造。
- **本 spec 对 spec2 提一条硬要求**：`build_wealth_graph` 必须留一个**银行替身的注入点**（`client=` 关键字参数），
  否则 `BankDouble` 接不进图，V-1/V-2 只能退回替身，本 spec 就白写了。这一点写进 spec2 §10 的验收。
- **执行顺序**：spec3 的 V-1/V-2 必须在 spec2 的 W-5..W-9 之后写（`WealthTurn` 那时已不存在）。
- **不重复**：`wealth_integration/` 的 A01–A15 **断言与场景保持原样**（它证明的是模块 ↔ 真实银行），A15 的「不启动 Supervisor」是它的立论，**不改**；但 60 处 `agent.handle(` 的新签名与 A15 独立入口的装配函数随 spec2 的 W-6 一起改（I-10，spec2 §12.4 T-14/§17）；基座层由本 spec 的 V-1/V-2 补。
- **T-10 归属**：spec1 §12 已列 T-10，spec3 §5 只加验收用例，避免两处各改一遍代码。

## 8. 验收

```powershell
cd backend
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run pytest tests/wealth -q                      # 385 passed + 1 需 .env（装配路径按 spec2 §12.4 改写，断言不变）
uv run pytest tests/wealth_integration -q          # 44 passed（需银行进程与专用 schema）
uv run pytest tests/test_wealth_graph_base.py tests/test_wealth_write_api.py tests/test_turn_status_projection.py -q   # 本 spec 新增（第三个文件由基座计划 Task 2 建，本 spec 只追加不同名用例）
uv run pytest -q                                   # 全量回归
```

- 新增用例全绿；`tests/wealth` 与 `tests/wealth_integration` 的既有**条数**不变；
- `final_gate.log` 里 `psycopg.errors.ConnectionTimeout` 仍只出现在共享云库相关用例，不算断言失败；
- §6 的 S01–S06 有归档记录。

## 9. 明确不做

1. **不新增资金写能力**：`tool_table()` 里仍然没有 purchase/redeem 执行工具（spec2 §12.3 T-8 的既有断言保持）。
2. **不改事件契约**：`EVENT_FIELDS` 八张表、`subtask_end` 三字段 + `turn_status`、`final` 四字段，一个不加。
3. **不动 `wealth_integration/` 的断言与场景**：包括 A15 的「不启动 Supervisor」；但调用签名与装配函数随 spec2 W-6 改（I-10）。
4. **不把真机银行塞进 CI**：§6 是人工冒烟，自动化只到 `BankDouble`。
5. **不改模块的幂等、对账、待办与门禁**：一行不改（spec2 §9.2 已列，本 spec 沿用）。
6. **不重写 spec2 已覆盖的用例**：模块内的装配语义归 spec2 §12.4，本 spec 只写“经过基座/HTTP”的那一层。

## 10. 风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 脚本模型与真实模型的自由度差距 | 真机四轮能过、脚本用例过不了（或反过来） | §6 的人工冒烟与 V-1/V-2 同时保留，两者不互相替代 |
| `build_wealth_graph` 没有银行注入点（`client=`） | `BankDouble` 接不进图，V-1/V-2 只能退回替身 | §7 已列为对 spec2 §10 的硬要求；实现时先补注入点（含 W-0 的整条透传链）再写用例 |
| 追问变成 `interrupt kind=input` 后，V-1 的脚手架要发 `resume` 信封 | 用例写成了“再投一条消息”，测试假绿 | V1-2 明确用 `resume`；V2-7 专门钉住裸消息被 409 拒 |
| 确认卡的 checkpoint 依赖 `InMemorySaver` | 重建图后卡片丢失 | V2-5 断言重建后仍能读到同一 `pending_id` |
| `tests/wealth` 的 386 条里已有同类断言 | 重复用例、维护成本 | §3/§4 只写“经过基座/HTTP”的用例，模块内已覆盖的一律不重写 |
| spec2 的搬家把写流程带坏，本 spec 的用例是唯一能发现的 | 发现得晚 | 本 spec 的 V-1/V-2 与 spec2 的 W-5..W-9 在同一条分支上连续落地，不跨分支 |

## 11. 裁定（已定，2026-10-12）

用户 2026-10-12 经选项确认：

| # | 问题 | 裁定 |
| --- | --- | --- |
| Q1 | V-1/V-2 是否也要接真实 HTTP 银行（而非 `BankDouble`）？ | **只接 `BankDouble`**：真机部分交给 §6 人工冒烟与既有 `wealth_integration`，否则每个用例都要起银行进程与专用 schema |
| Q2 | T-10（card `finish()` 补 `turn_status`）代码改在 spec1 还是 spec3？ | **spec1**（它已列 T-10），spec3 只加 §5 的验收用例 |
| Q3 | §6 的真机冒烟是否必须每次发版前重跑？ | **只在改动涉及写流程或基座派单时重跑**，结果按版本归档到 `docs/superpowers/reviews/` |

**随 B2 追加（无需再裁定，是 B2 的必然结果）**：§2.7 的三条改动是本 spec 用例的写法依据；
其中第 2 条（追问变中断 + `resume` 信封）**不需要前端改造**：通道已存在（spec2 §10.1），前端只加一条回归。

## 12. 执行顺序

1. V-3 的 T-10 随 spec1 落地（`card/workflow.py` 补 `turn_status`）；
   0. 前置：spec2 的 W-0 已把 `assembler`/`turn`/`client` 透传到 `graph_builder`（含 `Runtime.bank_client` → `agent_graph_factory` 的接线，C-3）；
2. spec2 的 W-5..W-9 落地（`WealthTurn` 删除、图与状态就位）；
3. 写 `tests/test_wealth_graph_base.py`（V-1，8 条），先跑通 `interrupt kind=input` 与 `completed` 两态；
4. 写 `tests/test_wealth_write_api.py`（V-2，8 条），重点是 V2-3 的“银行之前就拒绝”与 V2-7 的裸消息被拒；
5. 写 §5 的三条 `turn_status` 验收用例；
6. 跑 §8 的全套命令并留 `final_gate.log`；
7. 执行 §6 的 S01–S06 并归档到 `docs/superpowers/reviews/2026-10-12-wealth-write-smoke.md`。
