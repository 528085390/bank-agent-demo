# 生活事件领域模块：两个只读工具、一个预备工具、一个可见写工具、一个隐藏的执行工具 + 自有图

本模块是「工具 + 自有图」形状的领域模块，和 `backend/app/modules/card/`、`subscription/` 同构。
它**不声明 `ModuleSpec.turn`**，也没有关键词意图层：工具定义交给模型，整轮处理交给自己的领域图。
它负责生日链路：查联系人 → 按预算挑礼物 → 冻结订单 → 确认卡 → 银行落 `pending` 订单 →
拨钟跑批后转 `completed` 并产生借记流水。

```text
tools.py     SPECS / TOOLS / HANDLERS          # 两个只读、一个预备、一个可见写、一个隐藏执行工具
writes.py    资金写门面：下单与撤销
workflow.py  build_life_event_graph(...)       # 模块自己的图
__init__.py  LIFE_EVENT / bind_life_event / MODULE
```

## 文件与职责

| 文件 | 内容 |
| --- | --- |
| `tools.py` | `LEAD_DAYS`（2）/ `ORDER_HOUR`（8）/ `ACCOUNT_TYPE_LABELS`；`next_birthday()`、`days_until()`、`pick_products()`、`budget_minor()`、`account_label()`；银行读取器 `_contacts()` / `_products()` / `_accounts()` 与选择器 `_contact()` / `_product()` / `_paying_account()`；`_daily_accumulated_minor()`、`_execute_at()`、`_target_birthday()`；处理器 `birthdays()` / `gift_plan()` / `prepare_order()` / `cancel()` / `execute_order()`；`SPECS` / `TOOLS` / `HANDLERS`（五个工具） |
| `writes.py` | `cancel_order()` 与 `submit_order()`，两者都经 `WriteFacade`；`_order_amount()` 拒绝非正金额；`_policy_failure()` / `_bank_failure()` 把门面与银行的失败翻成 `ToolFailure` |
| `workflow.py` | `MAX_READ_ROUNDS` / `PENDING_EXPIRY_MINUTES` / `CONFIRMATION_TOOLS` / `ORDER_STATUS_LABELS` / `_HIDDEN` / `_EXECUTE_KEYS` / `_TEXT_KEYS` / `SYSTEM_PROMPT`；`LifeState`、`expired()`、`build_life_event_graph(...)`，以及 `decide` / `wait` / `process` 三个节点与 `route` 条件边 |
| `__init__.py` | `LIFE_EVENT`（`DomainDeclaration`，带 `graph_builder` 与四个展示字段）、`bind_life_event()`、`MODULE` |

依赖方向固定：`__init__.py → workflow.py / tools.py → 基座`，写路径再往下是
`tools.py → writes.py → WriteFacade → BankClient`。模块自己没有第二套运行时，也没有独立入口。

## 工具（两个只读 + 一个预备 + 一个可见写 + 一个隐藏执行）

| 工具 | `side_effect` | 参数 | 输出 | 说明 |
| --- | --- | --- | --- | --- |
| `life_birthdays` | `READ` | 无 | `contacts` 数组 | 查联系人登记的生日；每条带 `next_birthday` / `days_until`（按 `SystemClock.today()` 算） |
| `life_gift_plan` | `READ` | `budget_yuan`（可选，`string \| number`，例如 `200` / `"200.00"`） | `products` 数组、`budget_minor` | 按价格从低到高列礼物，预算内过滤；不传预算表示不限 |
| `life_prepare_order` | `READ` | `contact_id`（必填）、`product_code`（必填，enum `flowers` / `cake`）、`birthday_date`（可选）、`from_account_id`（可选） | `status` / `intent` / `requires_code` / `risk_level` / `reason` | **预备**：校验联系人、礼物、账户与日期，按当日累计判风险，冻结确认卡要的全部参数；**不写银行** |
| `life_cancel_order` | `WRITE` | `order_id`（必填） | `order` 对象 | 撤销一张未执行的生日订单；挂确认卡，点确认后才经资金写门面提交 |
| `life_execute_order` | `WRITE` | `contact_id` / `product_code` / `from_account_id` / `birthday_date` / `amount_minor`（全部必填） | `order` 对象 | **对模型隐藏**、只能由确认卡回调触发：提交 `life_prepare_order` 冻结的那一单 |

`life_prepare_order` 声明为 `READ` 是准确的：它只读三张银行数据（`GET /birthdays`、
`GET /mock-products`、`GET /accounts`），一个写请求都不发。真正动钱的是隐藏的
`life_execute_order`，它由 `process` 节点在用户点过确认卡之后直接调用。

## 生日链路的四步

| 步骤 | 工具 | 银行调用 | 是否写 |
| --- | --- | --- | --- |
| 1 读联系人 | `life_birthdays` | `GET /birthdays` | 否 |
| 2 挑礼物 | `life_gift_plan` | `GET /mock-products`（银行的产品目录，含 `flowers` / `cake`） | 否 |
| 3 冻结订单 | `life_prepare_order` | `GET /birthdays` + `GET /mock-products` + `GET /accounts` | 否 |
| 4 点确认后提交 | `life_execute_order`（隐藏） | `POST /birthdays/orders` | **是** |
| 撤销 | `life_cancel_order` | `GET /birthdays/orders/{order_id}` 校验归属 → `POST /birthdays/orders/{order_id}/cancel` | **是** |

**下单时刻由代码算，不由模型给。** `_execute_at()` 取「生日前 `LEAD_DAYS`（2）天的
`ORDER_HOUR`（8）点」，与银行侧 `bank_backend/app/domain/birthday_service.py` 的口径一致：
礼物要提前两天送到。`prepare_order()` 里 `remaining < LEAD_DAYS` **或** `execute_at <= SystemClock.now()`
都直接 `VALIDATION` 拒绝，并给出「只剩 N 天，来不及了；可以换一个更晚的日期，或者直接联系人工处理」
这类人话——不会冻结一张提交时必然被银行拒掉的卡。

**日期口径。** 用户给的是历史出生年（例如 `1995-09-25`）时，`_target_birthday()` 用
`next_birthday()` 推到下一次生日；`2` 月 `29` 日在平年顺延到 `3` 月 `1` 日（礼物那年仍要送），
而不是丢弃。不传 `birthday_date` 就用联系人档案里的生日；档案里也没有就 `VALIDATION` 让用户直接说日期。

**付款账户。** 不传 `from_account_id` 时自动挑「`status == "active"` 且不是 `fixed`」的账户：
恰好一张就选定，零张 `NOT_FOUND`，多张 `VALIDATION` 并列出**完整账户 ID**（尾号会撞车，模型也
映射不回来）。传了就只在那张里找，找不到 `NOT_FOUND`。

**金额。** 取银行 `/mock-products` 里的 `price_minor`，模型没有改价入口；确认卡上的金额就是
提交给银行的金额，执行侧不重新定价。

## 确认与写安全

`prepare_order()` 冻结 `intent`（`contact_id` / `contact_name` / `product_code` / `product_name` /
`amount_minor` / `from_account_id` / `birthday_date` / `execute_at`），连同 `requires_code` /
`risk_level` / `reason` 一起回给图；图把它挂成一张 `kind=confirmation` 的卡，用户点确认才继续。

- **卡的寿命是 5 分钟**（`PENDING_EXPIRY_MINUTES`）。`expired()` 读不到 `expires_at` 就算过期
  （fail closed），用的是挂钟而不是银行时钟——测试里银行时钟可能停着不走。
- **要不要验证码由策略引擎说，不由模型说。** `prepare_order()` 调
  `classify_operation("birthday_order", amount_minor=..., daily_accumulated_minor=..., side_effect=SideEffect.WRITE)`，
  `decision.mfa_required` 原样进 `facts["requires_code"]`；`decision.reason` 写进卡片文案。
- **验证码不进工具参数。** `life_execute_order` 的 schema 里没有验证码字段；`process` 节点从
  `TurnContext.verification_code` 取，按关键字参数传给 `execute_order()`。工具参数会随 `tool_call`
  SSE 帧下发、也会整条进运行事件审计，声明在 schema 里就等于泄漏。
- **模型不能自己执行。** `_HIDDEN = frozenset({"life_execute_order"})` 把它从模型菜单里剔除
  （`tools` 与 `known` 都不含它）；模型按名字调用会在任何工具运行前被 `parse_domain_decision` 拒绝。
- **金额必须是正整数。** `_order_amount()` 对 `0` / 负数 / 非 `int` 抛 `VALIDATION`：零金额到了门面
  会被当成「不动钱」的写，跳过当日限额，等于绕开限额。
- **撤销不返还、也不二次累计。** `cancel_order()` 走 `operation="cancel_birthday_order"` 且**不带**
  `amount_minor`：门面照记 `fund_write_attempt/success`（审计里留「用户试过」的痕），但因为不动钱
  记 `daily_total_skipped`。日累计记的是「今天已承诺出去多少钱」，撤销只作废承诺。
- **撤销前先核归属。** `process` 先用 `GET /birthdays/orders/{order_id}` 读那一单（银行这个 GET 收
  `user_id` 却不把它传给 service），按返回体里的 `user_id` 与当前客户比对；别人的单与不存在的单
  回同一句文案，不泄漏存在性。已完成 / 已取消 / 已失败的单按 `ORDER_STATUS_LABELS` 说清状态再拒绝。
- **幂等键**取自 `invocation.call.idempotency_key`，由网关在重试之外铸一次。

## 图

`LifeState` 只加三个槽：`life_intent`（冻结的订单快照）、`life_pending`（待确认的动作）、
`life_action`（撤销时冻结的订单）。三个节点：

- `decide`：模型用原生 tool calling 决策，一轮里最多连续跑 `MAX_READ_ROUNDS = 3` 轮只读工具
  （查生日 + 挑礼物 + 兜底一次）。`life_prepare_order` 成功 → 冻结快照、挂确认卡 → `route` 去 `wait`。
- `wait`：`wait_for_user` 从 checkpoint 读回 `pending_question` 再 `interrupt()`，重放不产生模型调用。
- `process`：读回答案。确认卡走 `consume_confirmation()` → 调隐藏的 `life_execute_order` → `conclude`；
  普通补充信息回到 `decide`。答案非法时最多重问 `MAX_ASK_ATTEMPTS` 次，超限以 `FAILED` 收尾。

```text
START → decide ─┬─(waiting_for_user)→ wait → process ─┬→ wait
                └→ END                                 └→ END
```

## 阈值与日累计

**模块里没有任何金额阈值常量。** 唯一来源是 `app/policy/types.py` 的
`DAILY_RED_THRESHOLD_MINOR`，经 `app/policy/evaluator.py` 的 `classify_operation()` 使用；
当日累计只读 `app/storage/daily_total.py` 的
`DailyTotalStore.get_accumulated_minor(session, user_id, business_date)`，`business_date` 取
`SystemClock.today()`。

`_daily_accumulated_minor()` 有两条降级，都是 fail-closed 的：

- 没注入数据库（单测里直接调 handler）→ 返回 `0` 并打 `debug`：卡仍按 0 判，真到提交时门面与
  银行会再判一次。
- 读累计失败 → 返回 `DAILY_RED_THRESHOLD_MINOR + 1` 并打 `warning`：读不到累计**不能**让一笔写
  看起来更安全。

## 装配

`bind_life_event(module, deps)` 是本包自己提供的装配器，组合根（`app/modules/__init__.py` 的
`production_modules`）只把 `ModuleDeps` 交进来，不认识生活事件需要什么：

1. `bind_writes(module, deps)`：把 `deps.writes` 绑进 `graph_builder`，图里的提交与撤销都要过门面。
2. `dataclasses.replace` 换掉三个 handler：`life_prepare_order` ← `partial(prepare_order, database=deps.database)`
   （确认卡要先读当日累计，否则按 0 算）、`life_execute_order` 与 `life_cancel_order` ←
   `partial(..., writes=deps.writes)`。`HANDLERS` 是 import 期的模块级常量，只能这样替换。

## 测试

`backend/tests/life_event/` 七个文件、62 个用例：

| 文件 | 覆盖 |
| --- | --- |
| `test_life_tools.py` | 只读工具与预备工具：日期口径、预算解析、账户选择、风险判定、拒绝文案 |
| `test_life_graph.py` | 图：只读轮数、确认卡、过期、验证码、撤销、补充信息恢复 |
| `test_life_execute_order.py` | 隐藏执行工具：五键校验、金额、门面调用、验证码不进参数 |
| `test_life_composition.py` | `bind_life_event` 的装配与 `ModuleSpec` 形状 |
| `test_life_acceptance.py` | AC-1 / AC-2 / AC-3：识别联系人、落 `pending` 订单、拨钟跑批转 `completed` 并出流水 |
| `test_life_acceptance_live.py` | 同上，但打真实银行 HTTP（`live` 标记） |

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://postgres:123456@127.0.0.1:5432/test"
$env:NO_PROXY=""
cd backend; pytest tests/life_event -q
```

真实银行那条还要起 `bank_backend`（`_accept/README.md` 有完整步骤），并确认演示数据里
种子联系人是**爱人**（`c_partner`），不是别的名字。
