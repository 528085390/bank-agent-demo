# 订阅模块改造为 card 形状 · 设计

**状态**：已获用户批准（设计见 m00207，交付包方案选定 m00208 = b），**已按自审修订**
**基线**：`feat/subscription-agent` @ 5d06fc4（基点 dev 35ee5d2）
**上游**：`docs/superpowers/specs/2026-10-13-architecture-optimization-roadmap-spec.md`（A1 / A5 / R-2 / R-4）、`docs/superpowers/plans/2026-10-13-architecture-optimization-plan.md`

## 1. 目标

subscription 从「整轮型」（`ModuleSpec.turn`）改成「工具 + 自有图」，与 card / wealth / transfer 同形状：

1. 决策从模块自己的中文关键词表交还模型的原生 tool calling；
2. 会话槽位与待确认快照从进程内 `sessions` 字典搬进图状态，由父图 checkpointer 落库（路线图 R-2）；
3. 确认卡仍由模块签发，但按钮由模块自己的 `process` 节点消费；
4. `bank_backend` 一行不动。

改完之后生产路径上不再有任何 `ModuleSpec.turn` 使用者，路线图 A1 退化为纯删除（`docs/superpowers/plans/2026-10-13-architecture-optimization-plan.md` 的 Task 3 随之解封）。

## 2. 为什么确认卡必须发 kind="confirmation"

### 2.1 三条代码事实

1. **基座那条 turn 通道对 graph_builder 模块关闭**。`backend/app/agents/supervisor.py:838` 是 `act = action_handler(spec.turn)`，`:840-849` 在 `act is None` 时直接 `_conclude_without_tool(..., ToolStatus.DENIED)`。删掉 `spec.turn` 后 `_answer_confirmation` 再也走不通。
2. **按钮路由只认 kind="confirmation"**。`backend/app/api/routes.py:565`：`if not isinstance(pending, dict) or pending.get("kind") != CONFIRMATION_KIND: return False`，并额外要求 `pending["confirmation"]["pending_id"] == pending_id`（`:568-572`）。card 的 `kind="card_authorization"` 因此**走不了**这条路由，只能走普通消息路由提交 `{"decision": "确认"}`。
3. **前端只认两种形状**。`frontend/src/events.ts:125-143`：要么 `data.confirmation`（`asConfirmation` 要求非空 `pending_id`，`:53-68`），要么 card 形状的 `authorization`（`action_id` 匹配 `/^CA-[a-f0-9]{32}$/` 且 `permission_level` 为 yellow/red）。第三种 kind 不会被渲染成确认框。

### 2.2 这条通道对模块图本来就是开的（已逐行核实）

`_confirmation_pause` 只服务 turn 模块：它由 `backend/app/agents/supervisor.py:608-612` 在 `_conclude_turn` 内调用，而 `_conclude_turn` 只被 `_run_turn` / `_run_turn_action` 调用。graph_builder 模块的确认框走的是**另一条、更朴素的路**：

1. 模块自己的 `wait` 节点调基座 `wait_for_user`，`interrupt(dict(question))`（`supervisor.py:702-723`）；
2. `backend/app/agents/runtime.py:289-301` 的 `_one_question(interrupts)` 从 checkpoint 取那个 interrupt 的 `value` —— 要求**恰好一个** interrupt，且 value 是含 `question_id` 的 Mapping；
3. `recovery_hint`（`runtime.py:367-377`）把它作为 `pending` 交给路由；
4. `_click_matches`（`routes.py:557-572`）校验 `kind`、`question_id` 与 `confirmation.pending_id`。

所以模块只要照抄 `_confirmation_pause` 的问题形状（`supervisor.py:621-653`），按钮绑定就是白拿的。**这也意味着模块图必须只留下一个 wait 节点**：两个 interrupt 会让 `_one_question` 返回 None，卡片就永远匹配不上。

### 2.3 问题载荷（照抄）

```python
{
    "kind": "confirmation",
    "question_id": f"q_{uuid4().hex}",
    "agent_id": "subscription",
    "attempt": 1,
    "display": <预览文案>,
    "fields": [{
        "name": "decision", "label": "操作决定", "type": "select",
        "required": True, "options": ["确认", "取消"],
    }],
    "confirmation": TurnConfirmation(...).as_payload(),
}
```

**fields 不能为空**：前端 `toEvent` 会把没有字段的 interrupt 整条丢掉。

### 2.4 按钮回答是机器值

`backend/app/api/routes.py:499` 发的是 `{"resume": {"question_id": ..., "value": {"decision": body.decision}}}`，而 `decision` 在 `backend/app/api/schemas.py:90` 是 `Literal["confirm", "cancel"]`。所以 `process` 必须**同时**接受机器值 `confirm` / `cancel` 和卡片标签 `确认` / `取消`（后者来自普通消息路由，例如客户端把渲染出来的表单当普通回答提交）。

### 2.5 过期用墙上时钟

旧代码用银行时钟（`GET /clock`）算日期。确认卡的 5 分钟有效期改用**墙上时钟**：银行是模拟时钟，可能停在某个固定日期，用它算过期会让演示里永远不过期，也会让「5 分钟」失去意义。到期判断抽成纯函数 `expired(action, now=None)` 以便测试直接构造过期快照，不用真等 5 分钟。

## 3. 形状

### 3.1 文件

```
backend/app/modules/subscription/
  __init__.py     SUBSCRIPTION 声明（graph_builder + fields）+ MODULE（tools / handlers）
  tools.py        ToolSpec 清单 + handlers + 选择器与中文渲染
  workflow.py     模块图：decide / wait / process + activate 循环
```

删除：`application/`（contracts / factory / runtime / turn）、`infrastructure/`（bank_adapter / mock_bank）、`cli.py`、`example.py`，以及仓库根的整个 `subscription/` 交付包。

### 3.2 工具清单（7 个，模型只见 6 个）

| 工具 | 副作用 | 银行端点 |
|---|---|---|
| `subscription_list` | 只读 | `GET /subscriptions`（可选 `status`） |
| `subscription_detail` | 只读 | `GET /subscriptions`（与 list 同端点，按 id 客户端筛选） |
| `subscription_cancel` | 写（只冻结快照） | — |
| `subscription_pause` | 写（只冻结快照） | — |
| `subscription_resume` | 写（只冻结快照） | — |
| `subscription_charge` | 写（只冻结快照） | — |
| `subscription_execute` | 写（**对模型隐藏**） | `POST /subscriptions/{id}/cancel|pause|resume|charge` |

**没有 abort 工具**（自审时砍掉的）。card 需要 `card_cancel` 是因为银行侧有 pending action 要释放（`POST /cards/actions/{id}/authorize {decision:"cancel"}`）；订阅没有银行侧待办，快照只在 checkpoint 里，所以「取消」就是清空状态并 `finish("本次操作已取消，未修改订阅。")`，不必绕一趟工具。

`POST /subscriptions/run-due` 也不进清单：它是模拟入口，留给脚本。

四个写工具的 handler 都叫 `prepare`：读一次该订阅、生成 `pending_id` 与 `expires_at`、渲染预览，返回 `facts={"action": {...}}`，**不打任何写接口**。

### 3.3 银行返回的真实形状（自审时逐行核对，别猜）

- `GET /subscriptions` 返回**裸数组** `list[dict]`，不是 `{"subscriptions": [...]}`（`bank_backend/app/domain/subscription_service.py:81-93`）。handler 要像 `card/tools.py:74-77` 的 `load_cards` 那样校验 `isinstance(data, list)`，否则抛 `ToolFailure(ErrorCode.PROTOCOL, ...)`。
- 记录主键字段叫 `subscription_id`，**不是** `id`（`subscription_service.py:42-56` 的 `subscription_view`）。字段全集：`subscription_id` / `user_id` / `from_account_id` / `name` / `merchant_name` / `amount_minor` / `frequency`（daily|weekly|monthly|yearly）/ `start_date` / `next_due_date` / `status`（active|paused|cancelled）/ `auto_renew` / `last_charge_id`。
- `amount_minor` 必须是**正整数分**：`TurnConfirmation.__post_init__`（`backend/app/agents/turn.py:92-93`）拒绝 `<= 0`。零元订阅要在 `prepare` 里先拦成 `ToolFailure`，别让确认卡构造期抛异常。
- 写端点 `cancel` / `pause` / `resume` / `charge` 都只吃路径参数，不收 `user_id`（`bank_backend/app/api/routes.py:959-1016`）；适配器仍然带上 `user_id` 是基座 `BankClient` 的规矩，不是银行的要求。

### 3.4 图与状态

拓扑与 card 逐行同构：

```
START → decide --route--> {wait, END}
wait → process
process --route--> {wait, END}
route(state) = "wait" if state.get("turn_phase") == "waiting_for_user" else END
```

- **decide**：`activate()` 循环「模型 → 工具 → 回喂 → 模型」，上限 `MAX_TOOL_ROUNDS`（=6）。只读结果回喂；写结果 park 成确认卡；模型自己不调工具就是收尾；到界走 `_overflow_outcome`。
- **wait**：基座 `wait_for_user`，只 `interrupt`、绝不碰模型，问题从 checkpoint 读回。
- **process**：读 `pending_answer`。`kind="subscription_selection"` → 补上选中的订阅 id 后回到 `activate`；`kind="confirmation"` → 校验（快照在 / 未过期 / 若回答带 pending_id 则必须相等）后，「取消」直接收尾、「确认」调 `subscription_execute`。

`SubscriptionState(AgentState, total=False)`：

```python
class SubscriptionState(AgentState, total=False):
    subscription_call: dict[str, Any] | None   # 模型那次还没解析出目标的调用
    subscription_choices: dict[str, str]       # 候选标签 → subscription_id
    pending_action: dict[str, Any] | None      # 冻结的待确认快照
```

`pending_action` 的内容：`pending_id` / `operation` / `subscription_id` / `product_name` / `merchant_name` / `amount_minor` / `account_display` / `frequency` / `next_due_date` / `expires_at` / `idempotency_key`。

**park 时必须一起写 `domain_thread` 与 `tool_rounds`**（card/workflow.py 同款）。它们是 `AgentState` 的既有字段；只写问题不写这两样，resume 时 `activate` 就没有对话可续，模型会重新开始一轮而不是接着上次的进度。

## 4. 关键词层逐条去向

| 现有规则 | 去向 |
|---|---|
| 聊天里打「确认」不算授权 | 不变，基座结构性保证（按钮走 `pending_answer`，不走聊天文本） |
| 目标必须唯一命中，否则追问 | 不变，`matched_subscriptions` + park `kind="subscription_selection"` |
| 意图分类（cancel / pause / resume / charge / reminder / list / help） | **删**，模型选工具名 |
| `GIVE_UP_WORDS`（算了 / 不用了 / 就这样吧） | **删**，模型不调工具直接回话 |
| 「拿不准的动词落只读或可逆侧」（单独「扣款」= 查询、「停掉」= 暂停不取消） | 从词表**顺序**改成提示词 + 工具描述里的一条规则 |
| 待办 5 分钟过期、卡片重发复用 pending_id | 不变，进 `process` 校验 |

**安全论证**：本方案的风险是模型把「取消」听成「暂停」。但四个写操作**全部**要过确认卡，用户看到的是代码渲染的预览（操作 + 目标 + 金额 + 下次扣款日），不是模型的话。误判的后果上限是「弹错一张卡」，不是「钱动了」。要守的只有一条：写工具的结果一律 park、绝不回喂，别让只读请求变成写请求。

## 5. 交付包收进基座（方案 b）

仓库根 `subscription/` 是全仓唯一的自包含交付包（根目录只有 backend / bank_backend / docs / frontend / subscription）。改完之后模块离不开模型，`cli.py` / `example.py` 的「一次模型都不调」卖点消失，`01-走一遍流程.md`、`02-架构与tool层.md`、`05-对接主Agent接口清单.md` 里的说法全部作废。

处置（**全部发生在 Task 5**；Task 1 只删 backend 里的旧形状，仓库根 `subscription/` 原地不动）：

| 现在 | 去向 |
|---|---|
| `subscription/tests/test_subscription_agent.py`（27 用例） | 重写为 `backend/tests/test_subscription_module.py` |
| `subscription/tests/test_live_bank.py`（5 用例） | 移为 `backend/tests/test_subscription_live_bank.py`，改走 `run_domain(..., adapter=BankToolAdapter(registry, 真客户端))` |
| `subscription/tests/test_turn_wiring.py`、`support.py`、`conftest.py` | 删（基座有同名覆盖） |
| `subscription/pytest.ini` | 删 |
| `subscription/scripts/seed_demo_customer.py` | 移为 `bank_backend/scripts/seed_subscription_demo.py`（与 `seed_wealth_demo.py` 同惯例） |
| `subscription/scripts/run_bank_backend.py`、`integration_main_agent.py`、`ui_smoke.py` | 删 |
| `subscription/01..05*.md`、`README.md` | 并成 `docs/subscription-agent.md`（与 `docs/card-management.md` 同惯例） |
| `docs/ARCHITECTURE.md` 第 10 行「生产上只有理财走 turn」 | 改成「生产上已无整轮模块」 |

## 6. 明确不做

- 不改 `bank_backend`：不给订阅加 pending action 表（那是路线图 D1 的事）。因此订阅的确认只能由模块自己签发 —— 快照存 checkpoint，绑定靠 `pending_id` + `question_id`。
- 不做路线图 A2（`app/agents/sdk.py`）：本模块会像 card 一样 import 基座的私有名（`_call_tool` / `_awaiting` / `_read_answer` …），那是 A2 的活。本次只是**不再新增第二种形状**。
- 不做 A3（统一 `card_authorization` 与 `confirmation`）：订阅这次落在 `confirmation` 一侧，card 仍在另一侧，两套并存由 A3 收。
- 不动验证码链路：订阅写操作不需要验证码（`requires_code: false`），真 MFA 是 D1。

## 7. 实现时确认的细节

- `TurnConfirmation` 各字段的必填与默认值以 `backend/app/agents/turn.py` 为准；`as_payload()` 的 11 键形状不能手写。
- `subscription_charge` 的幂等键必须**一次调用内固定**（重试复用同一个），照 `backend/app/modules/card/workflow.py:151-157` 的 `invoke`。
- `subscription_list` 返回的订阅可能很多：facts 给全量，摘要把每一笔都逐行列出来（`subscription_text` 拼接）。
