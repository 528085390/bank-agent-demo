# 账单领域模块：两个只读工具、一个冻结快照的写工具、一个隐藏的执行工具 + 自有图

本模块是「工具 + 自有图」形状的领域模块，和 `backend/app/modules/subscription/` 同构。它没有关键词
意图层：工具定义交给模型，整轮处理交给自己的领域图。发报告是**两段**的 —— 模型只能冻结，只有用户的
点击能执行。

```text
tools.py     SPECS / TOOLS / build_handlers(...)   # 两个只读工具、一个写工具、一个隐藏的执行工具与它们的处理器
workflow.py  build_bill_graph(...)                 # 模块自己的图
__init__.py  BILL / MODULE / build_bill_module(...) # 声明与装配
```

## 文件与职责

| 文件 | 内容 |
| --- | --- |
| `tools.py` | 四个工具的参数/输出 schema（`TRANSACTION_PAGE` / `ANALYSIS_REPORT` / `REPORT_PREPARATION` / `REPORT_DELIVERY`）、`SPECS` / `TOOLS`，以及 `build_handlers(*, database=None, notification_service=None)`：依赖给到哪个才登记哪几个工具 |
| `workflow.py` | `BillState`、`_HIDDEN`、`_BILL_PROMPT`、`declared_arguments()`、`authorization_question()`、`PENDING_EXPIRY_MINUTES` / `expired()`、`build_bill_graph(...)`，以及 `decide` / `wait` / `process` 三个节点、`activate` 循环与确认分支 `consume_confirmation()` |
| `__init__.py` | `BILL`（`DomainDeclaration`，带 `graph_builder=build_bill_graph`）、import 期不带工具的 `MODULE`，以及组合根调用的 `build_bill_module()` |

依赖方向固定：`__init__.py → workflow.py / tools.py → 基座`。模块自己没有第二套运行时，也没有独立入口。

**双形态**：`MODULE = ModuleSpec(name="bill", declaration=BILL)` 不带任何工具 —— `app/agents/domain.py:23`
在 import 期就跑 `build_registry().roster`，模块自己带工具会把数据库与 `NotificationService` 拖进
import 链。真正带工具的 bill 模块由**组合根**装配：`app/modules/__init__.py` 的 `production_modules()`
注入 `notification_service` 与 `database`，缺任一个就抛
`RuntimeError("账单模块需要数据库与通知服务；组合根必须显式注入")`。模块内不留默认实现。

## 工具（两个只读 + 一个写工具 + 一个隐藏的执行工具）

| 工具 | `side_effect` | 参数 | 输出 | 说明 |
| --- | --- | --- | --- | --- |
| `bill_query_transactions` | `READ` | `period_start` / `period_end`（必填）、`category` / `direction`（`debit` / `credit`）/ `page` / `page_size`（可选） | `TRANSACTION_PAGE` | 查询某段期间的流水明细 |
| `bill_analyze_comprehensive` | `READ` | `period_start` / `period_end`（必填）、`modules`（可选） | `ANALYSIS_REPORT` | 综合分析与分类统计 |
| `bill_send_report` | `WRITE` | `period_start` / `period_end`（必填）、`recipient`（可选） | `REPORT_PREPARATION` | **只冻结，不发信**：校验收件人、生成 `action_id`，把这次发送冻结成快照 |
| `bill_send_report_execute` | `WRITE` | 同上 | `REPORT_DELIVERY` | **对模型隐藏**、只能由确认按钮触发：生成 PDF 并 `send_email` |

四个工具名都匹配 `^bill_[a-z_]+$`。前两个只读工具读**本机已导入的流水表**（组合根注入的
`database`），不发银行接口；唯一出网/外发路径是执行工具里的 `notification_service.send_email()`。

`bill_send_report` 是 `prepare`（`app/modules/email_handlers.py` 的 `prepare_send_report`）：它解析
两个日期、解析收件人、生成 `action_id = f"bill-report-{uuid4().hex}"`，返回
`ToolData(summary="准备发送账单报告到 …，请确认", display=facts=REPORT_PREPARATION)` —— facts 恰好是
`{action_id, period_start, period_end, recipient}`，一次都不发信。没有绑定邮箱时它直接
`ToolFailure(ErrorCode.VALIDATION, "您还没有绑定邮箱。…")`：**发不出去的请求在冻结那一步就失败**，
而不是等用户点完确认才失败。

收件人由 `resolve_recipient()` 决定：**显式 `recipient` 参数优先**，缺省才回落到
`notification_service.get_user_email(user_id)`。这个优先级正是「冻结」能成立的原因 —— 执行时把快照里的
收件人当显式参数传回去，于是冻结之后换绑邮箱也改不了已经确认的那一封。

`bill_send_report_execute` 是第四个工具：它留在网关绑定的工具目录里（`build_bill_module` 必须登记它，
否则网关校验不过），但被 `workflow.py` 的 `_HIDDEN` 从交给模型的清单里剔除（`tools` 与 `known` 都不含
它）。模型按名字调用它会被 `parse_domain_decision` 在任何工具运行前拒绝，`activate` 把这次拒绝转成
`finish("该账单操作不被允许。", ToolStatus.DENIED)`，所以只有确认按钮能走到它。

## 图

`build_bill_graph(model, spec, *, assembler=None, turn=None, client=None)` 返回模块自己的图：
`decide` → 条件边 → `wait`（`wait_for_user`）→ `process` → 条件边 → END。三个 seam 参数收下但不用 ——
账单没有自己的装配层与上游实现。

`activate` 循环在一次节点访问里跑「模型 → 工具 → 回喂 → 模型」：只读工具的结果会回到模型手里，让它
据此决定下一步；用户提问是循环的**返回值**，中断由 `wait` 节点做，所以停在问题上的激活能被原样重放。
收尾时 `finish()` 把 `subtask_end` 事件和带 `turn_status` 的 `domain_results` 写回基座（Supervisor 靠
`turn_status` 判断这一轮是否收口），并**显式清掉** `bill_call` / `bill_action` —— `idle_work_state()` 只清
通用通道，不清模块自己的槽。

`run_call` 只放行本模块声明的工具：未声明的名字、以及声明但对模型隐藏的 `bill_send_report_execute`，
一律 `finish("该账单操作不被允许。", ToolStatus.DENIED)`；参数不符合 schema 则记为 FAILED。这条判断是
纵深防御：模型点名的隐藏工具在 `activate` 里已被 `parse_domain_decision` 拒绝，根本到不了 `run_call`。
只读工具走一轮 `_READ_ROUND` 并把结果回喂给模型。

写工具（只有 `bill_send_report`）先冻结：生成 `key = f"bill:{uuid4().hex}"`，以这个幂等键调用
`prepare`；成功后把 facts 扩成 `bill_action` 快照（`pending_id` = `action_id`、`operation`、
`period_start` / `period_end`、`recipient`、`idempotency_key`、`expires_at`），连同
`bill_call`（工具名 + 参数）、`pending_question`、`pending_answer=None`、`turn_phase="waiting_for_user"`
写进 state，带着 `domain_thread` 与 `tool_rounds` 返回 `_PARKED`（spec §3.4：少了后两个字段，恢复的
激活会重新开始这一轮，而不是接着走）。

`authorization_question(action)` 生成确认卡负载：`kind="confirmation"`、非空的 `fields`（`decision`，
选项 `确认` / `取消`）、以及走 `TurnConfirmation.as_payload()` 的 `confirmation`。`pending_id` 用快照
自己的 `action_id`（`bill-report-…`），故意**不是** `CA-…` 形状，这样 `app/agents/hitl.py` 不会去走
银行验证码授权 —— 账单发信不需要那套。发信没有金额，但 `TurnConfirmation` 要求一个正整数分的
`amount_minor`，所以给 1 分：卡片显示的是期间与收件人，不是钱。前端复用既有通用路由
`POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`
（`app/api/routes.py:461`），**没有新增路由**。

`expired(action, now=None)` 是纯函数，按**墙钟**（不是银行时钟，测试里银行时钟是模拟的、可能停着不走）
判断快照是否过期，`PENDING_EXPIRY_MINUTES = 5`。

## 确认按钮的执行路径

写工具只冻结快照并弹出确认卡；把点击变成执行的是 `process` 节点里的确认分支
`consume_confirmation(state, answers)`，它按顺序 fail-closed 地校验：

1. **快照在**：`bill_action` 必须是字典，且 `pending_id` 与 `operation` 都非空；缺一即视为失效快照。
2. **未过期**：`expired(action)` 按墙钟判断，过期即拒。
3. **`pending_id` 相等**：回答里若带 `pending_id`，必须与快照里冻结的那个相同。
4. **决定明确**：`confirm` / `确认`、`cancel` / `取消` 之外的回答一律拒绝。

任一不过都走 `finish(..., ToolStatus.DENIED)`，**不打任何工具**。校验通过后：

- **取消**（`cancel` / `取消`）：没有 abort 工具，直接
  `finish("本次操作已取消，未发送报告。")` —— 快照只活在 checkpoint 里，取消就是清空状态，没有任何
  待释放的东西，所以一次调用都不发生。
- **确认**（`confirm` / `确认`）：调用
  `invoke("bill_send_report_execute", {"period_start": 快照.period_start, "period_end": 快照.period_end,
  "recipient": 快照.recipient}, idempotency_key=快照.idempotency_key)`。
  **期间与收件人来自快照，幂等键复用快照冻结的那个**，都不重新解析、不重新生成；`invoke` 内部的重试
  也用同一个键。执行 handler 才生成 PDF 并 `send_email()`。

  这个键**不构成「同 key 就只发一封」的保证**：没有任何一层按它去重 —— 执行 handler 从不读
  `invocation.call.idempotency_key`，`app/tools/gateway.py` 只把它记进审计，
  `app/notification/service.py` 也没有去重。重放一次确认就会再发一封信，`tests/test_bill_graph.py`
  里那条用例钉着的就是这个事实。真正挡住重复点击的是 HTTP 层：点击不再指向当前待确认操作时
  `app/api/routes.py:538-551` 回 409，再加上会话锁。键的作用是让下游（银行幂等层、审计）认出
  「同一个意图」，不是让我们这边重放。

「冻结之后换绑邮箱」因此改不了结果：卡片上显示的是冻结时的收件人，点确认发出去的也是那一个。

## 测试

测试在 `backend/tests/test_bill_graph.py`。普通轮次通过 `backend/tests/support/domain_harness.py` 的
`run_domain(build_bill_module(...), ...)` 驱动模块自己的图（`InMemorySaver` checkpointer），不经过
Supervisor；需要**读回或改写 checkpoint** 的轮次（确认执行、幂等键重放、冻结收件人）用
`parked_graph()` 自建图 —— `run_domain` 的 `answer=` 只能恢复最后一个未答问题，读不到快照。

```powershell
cd backend
uv run pytest tests/test_bill_graph.py -v
```

## 业务边界

- 流水数据由用户上传后落在本机（组合根注入的 `database`），账单模块不查银行接口、不建自己的表。
- 查询与分析是只读的：`bill_query_transactions` / `bill_analyze_comprehensive` 不改任何状态。
- 会话线程、待办和槽位都在图 state 里（由基座 checkpoint），不是模块内的另一套存储。
- 报告里的数字只能来自工具结果；模型不得编造金额、商户、账户或收件人（`_BILL_PROMPT` 明说）。
- 期间解析统一走 `app/modules/dates.py` 的 `parse_tool_date()`，支持「9月13日 / 2026年9月13日 / 今天 /
  昨天 / 前天」这类原话；缺期间时图让模型用 `ask_user` 追问，而不是编一个默认区间。
