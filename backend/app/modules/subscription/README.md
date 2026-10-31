# 订阅领域模块：两个只读工具、四个冻结快照的写工具、一个隐藏的执行工具 + 自有图

本模块是「工具 + 自有图」形状的领域模块，和 `backend/app/modules/card/` 同构。它**不再声明
`ModuleSpec.turn`**，也没有关键词意图层：工具定义交给模型，整轮处理交给自己的领域图。

```text
tools.py     SPECS / TOOLS / HANDLERS        # 两个只读工具、四个写工具、一个隐藏的执行工具与它们的处理器
workflow.py  build_subscription_graph(...)   # 模块自己的图
__init__.py  SUBSCRIPTION / MODULE           # 声明与装配
```

## 文件与职责

| 文件 | 内容 |
| --- | --- |
| `tools.py` | `SUBSCRIPTION_STATUS`、`FREQUENCY_LABELS`、`OPERATIONS`、`subscription_label()` / `subscription_text()`、`load_subscriptions()`、四个写工具共用的选择器 `WRITE_PROPERTIES` 与纯函数 `matched_subscriptions()`，只读处理器 `subscriptions()` / `detail()`，四个写工具共用的 `prepare()`，以及隐藏的执行处理器 `execute()`；`SPECS` / `TOOLS` / `HANDLERS`（共七个工具） |
| `workflow.py` | `SubscriptionState`、`_HIDDEN`、`declared_arguments()`、`_needs_selection()`、`_SUBSCRIPTION_PROMPT`、`authorization_question()`、`PENDING_EXPIRY_MINUTES` / `expired()`、`build_subscription_graph(...)`，以及 `decide` / `wait` / `process` 三个节点、`activate` 循环、确认分支 `consume_confirmation()` 与选择分支 |
| `__init__.py` | `SUBSCRIPTION`（`DomainDeclaration`，带 `graph_builder`）与 `MODULE`（`ModuleSpec`，`tools=TOOLS`、`handlers=HANDLERS`） |

依赖方向固定：`__init__.py → workflow.py / tools.py → 基座`。模块自己没有第二套运行时，
也没有独立入口。`prepare()` 在调用时（而不是模块导入时）才向 `workflow.py` 取
`PENDING_EXPIRY_MINUTES`，因为 `workflow.py` 反过来从 `tools.py` 取展示常量。

## 工具（两个只读 + 四个写工具 + 一个隐藏的执行工具）

| 工具 | `side_effect` | 参数 | 输出 | 说明 |
| --- | --- | --- | --- | --- |
| `subscription_list` | `READ` | `status`（可选，enum `active` / `paused` / `cancelled`） | `subscriptions` 数组 | 查询本人订阅 |
| `subscription_detail` | `READ` | `subscription_id`（可选）、`name`（可选）、`merchant_name`（可选） | `subscription` 对象 | 查询指定订阅详情；三个选择器任选其一 |
| `subscription_cancel` | `WRITE` | `subscription_id`（可选）、`merchant_name`（可选） | `action` 对象 | 取消一笔订阅（不可恢复） |
| `subscription_pause` | `WRITE` | `subscription_id`（可选）、`merchant_name`（可选） | `action` 对象 | 暂停一笔订阅（可恢复） |
| `subscription_resume` | `WRITE` | `subscription_id`（可选）、`merchant_name`（可选） | `action` 对象 | 恢复一笔已暂停的订阅 |
| `subscription_charge` | `WRITE` | `subscription_id`（可选）、`merchant_name`（可选） | `action` 对象 | 立即为一笔订阅扣款 |
| `subscription_execute` | `WRITE` | `subscription_id`（必填）、`operation`（必填，enum `cancel` / `pause` / `resume` / `charge`）、`amount_minor`（可选，扣款时随点击传入的冻结金额） | `result` 对象 | **对模型隐藏**、只能由确认按钮触发：执行快照冻结的写操作 |

四个写工具的参数里 `subscription_id` 是**可选**的：用户可能没说是哪一笔，代码在恰好一笔时自动
选定，在多笔时先问用户选哪一笔（见「图」一节）。`merchant_name` 也是可选的，且是四个写工具
共用的 `WRITE_PROPERTIES` 里的第二个选择器——没有它，模型根本无法把用户口述的商户名交给
代码，图里的候选追问就成了死代码。它们都指向同一个处理器 `prepare()`，`operation` 由工具名
去掉 `subscription_` 前缀得到。`subscription_execute` 是第七个工具：它留在网关绑定的工具目录里，但被
`workflow.py` 的 `_HIDDEN` 从交给模型的清单中剔除（`tools` 与 `known` 都不含它），模型按名字调用它会在
任何工具运行前被 `parse_domain_decision` 拒绝，`activate` 把这次拒绝转成
`finish("该订阅操作不被允许。", ToolStatus.DENIED)`，所以只有确认按钮能走到它。

`prepare()` 只读一次订阅（`GET /subscriptions`），冻结一份快照（`pending_id`、`operation`、
`subscription_id`、`product_name`、`merchant_name`、`amount_minor`、`account_display`、
`frequency`、`next_due_date`、`expires_at`、`idempotency_key`），返回
`ToolData(summary="操作详情已准备，尚未修改订阅，等待用户确认。", display={"blocks": []},
facts={"action": {...}})`。它**从不调用银行的写接口**——把快照变成执行的是确认按钮触发的
`subscription_execute`，不是 `prepare()`。
`amount_minor` 不是正整数时先抛 `ToolFailure(ErrorCode.VALIDATION, ...)`，不会构造确认对象。

`matched_subscriptions(rows, arguments)` 是纯函数，按 `subscription_id` / `merchant_name`
过滤，缺省的选择器不参与过滤；它**返回候选列表，不折叠成一行、也不抛错**——0 / 1 / 多
笔的判断属于图（`run_call`），因为只有图能把「多笔」变成一次追问。`_select_subscription()` 是
`prepare()` 直接调用时的兜底，仍对 0 笔抛 `NOT_FOUND`、对多笔抛 `VALIDATION`。

只读工具的 `side_effect` 都是 `SideEffect.READ`，都通过基座 `BankClient` 出网——这是模块
**唯一的出网通道**。`load_subscriptions()` 调 `GET /subscriptions`，并校验响应是**裸数组**
（`isinstance(data, list)`）；不是数组就抛 `ToolFailure(ErrorCode.PROTOCOL, ...)`。记录主键字段
是 `subscription_id`（不是 `id`）。

银行返回的订阅记录字段：

| 字段 | 含义 |
| --- | --- |
| `subscription_id` / `user_id` / `from_account_id` | 主键、客户、扣款账户 |
| `name` / `merchant_name` | 订阅名与商户 |
| `amount_minor` | 金额，单位是分（正整数），展示时经 `format_minor()` |
| `frequency` | `daily` / `weekly` / `monthly` / `yearly` |
| `start_date` / `next_due_date` | 开始日期与下次扣款日 |
| `status` / `auto_renew` / `last_charge_id` | `active` / `paused` / `cancelled`、是否自动续费、最近一次扣款 |

## 图

`build_subscription_graph(model, spec, *, assembler=None, turn=None, client=None, writes=None)` 返回模块自己的
图：`decide` → 条件边 → `wait`（`wait_for_user`）→ `process` → 条件边 → END。

`activate` 循环在一次节点访问里跑「模型 → 工具 → 回喂 → 模型」：只读工具的结果会回到模型手里，
让它据此决定下一步；用户提问是循环的**返回值**，中断由 `wait` 节点做，所以停在问题上的激活能被
原样重放。收尾时把 `subtask_end` 事件和带 `turn_status` 的 `domain_results` 写回基座——
Supervisor 靠 `turn_status` 判断这一轮是否已经收口。

`run_call` 只放行本模块声明的工具：未声明的名字，以及声明但对模型隐藏的
`subscription_execute`，一律 `finish("该订阅操作不被允许。", ToolStatus.DENIED)`；参数不符合
schema 则记为 FAILED。这条判断是纵深防御：模型点名的隐藏工具在 `activate` 里已被
`parse_domain_decision` 拒绝并转成同一个 DENIED，根本到不了 `run_call`。只读工具走一轮 `_READ_ROUND`。

写工具在冻结快照前先解析目标（`_needs_selection()` 的判据是参数里没有 `subscription_id`）：
先只读一次 `subscription_list`，再交给 `matched_subscriptions()`。0 笔 →
`finish(..., FAILED)`；1 笔 → 把银行行的 `subscription_id` 写回参数，继续 `prepare()`；
多笔 → 返回 `_PARKED`，把 `subscription_call`（原始写调用）与 `subscription_choices`
（`subscription_label()` → `subscription_id` 的映射，标签只由银行行生成，模型换不了人）连同
`pending_question`（`kind="subscription_selection"`、非空 `fields`、`options` 是候选标签）、
`pending_answer=None`、`turn_phase="waiting_for_user"` 写进 state。确认停车与选择停车都带着
`domain_thread` 和 `tool_rounds`——后两个字段是 spec §3.4 的要求：少了它们，恢复的激活会重新
开始这一轮，而不是接着走。

`process` 在 `kind="confirmation"` 之前先认 `kind="subscription_selection"`：回答里的
`subscription_choice` 必须落在 `state["subscription_choices"]` 里，否则
`finish(..., DENIED)`；通过后把候选映射出的 `subscription_id` 写回原始写调用的参数，用
`activate(..., pending=(原工具名, args))` **跳过模型**直接重进激活循环。于是选择本身是一次
只读绕行，终点仍是确认卡；写操作依旧只能由确认按钮触发。

`authorization_question(action)` 生成 spec §2.3 的确认卡负载（`kind="confirmation"`、非空的
`fields`、`confirmation` 走 `TurnConfirmation.as_payload()`）；`expired(action, now=None)` 是
纯函数，按**墙钟**（不是银行时钟）判断快照是否过期，`PENDING_EXPIRY_MINUTES = 5`。

## 测试

测试在 `backend/tests/test_subscription_module.py`，通过 `backend/tests/support/domain_harness.py`
的 `run_domain(MODULE, ...)` 驱动模块自己的图（`InMemorySaver` checkpointer），不经过 Supervisor（例外：需要读回或改写 checkpoint 的测试——停车轮次、确认执行与失效/篡改快照——用 `parked_graph()` 自建图与 `InMemorySaver`；取消点击不需要读回 checkpoint，仍走 `run_domain()`）：

```powershell
cd backend
uv run pytest tests/test_subscription_module.py -q
```

## 业务边界

- 订阅主数据、扣款记录、定时任务**全部住在 `bank_backend`**：Agent 侧没有任何订阅表，也不自建
  时钟，自动扣款由银行侧的模拟时钟驱动。
- 查询是只读的：`subscription_list` / `subscription_detail` 不写银行状态。
- 会话线程、待办和槽位都在图 state 里（由基座 checkpoint），不是模块内的另一套存储。
- 没有客户号绑定时，工具抛 `ToolFailure(ErrorCode.NO_CUSTOMER_BINDING, ...)`，不会拿空 user_id
  去调银行。

## 确认按钮的执行路径

写工具只冻结快照并弹出确认卡；把点击变成执行的是 `process` 节点里的确认分支
`consume_confirmation()`，它按顺序校验：

1. **快照在**：`pending_action` 必须是字典，且 `subscription_id` 与 `operation` 都非空；缺一
   即视为失效快照。
2. **未过期**：`expired(action)` 按墙钟判断，过期即拒。
3. **`pending_id` 相等**：回答里若带 `pending_id`，必须与快照里冻结的那个相同。

任一不过都走 `finish(..., ToolStatus.DENIED)`，**不打任何银行接口**。校验通过后：

- **确认**（`confirm` / `确认`）：调用 `invoke("subscription_execute", {"subscription_id": ...,
  "operation": ..., "amount_minor": 快照里的金额}, idempotency_key=快照里的 idempotency_key)`。
  幂等键**复用快照冻结的那个**，不重新生成；`invoke` 内部的重试也用同一个键。执行 handler 打
  `POST /subscriptions/{subscription_id}/{operation}`。

扣款（`charge`）是资金写，所以它走 `WriteFacade` 而不是 `BankClient` 直连：门面在银行调用
**之前**按当日累计再判一次日限额（越过 ¥1,000.00 就 `CONFIRMATION_REQUIRED`，此时一次 POST
都没发），成功后才记当日累计、写 `fund_write_success` 业务审计行。金额只认快照冻结的那个——
点击时回读银行会让扣款与用户确认过的卡片不一致；缺失或非正数直接 `VALIDATION` 拒绝，不给
「猜一个金额」的余地。取消 / 暂停 / 恢复不移动钱，仍直连银行、也不进当日累计。

同一张卡在**冻结**时就会判一次：`prepare()` 对 charge 读当日累计，越限直接
`ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, CHARGE_OVER_CAP_MESSAGE)`，不冻结快照。订阅没有
验证码通道（图忽略 turn、`execute` 从不下发码），一张点不过的卡比当场拒绝更糟。只有绑了
数据库的模块才做这一步（`bind_subscription`）；没绑时当日累计无从得知，卡片退回按金额单判。
- **取消**（`cancel` / `取消`）：没有 abort 工具，直接
  `finish("本次操作已取消，未修改订阅。")`——快照只活在 checkpoint 里，取消就是清空状态，银行侧
  没有待释放的东西，所以一次写调用都不发生。

按钮（`app/api/routes.py:499` 的 `{"decision": body.decision}`，`schemas.py:90` 固定为
`confirm` / `cancel`）与手写消息（`确认` / `取消`）四种写法都接受。
