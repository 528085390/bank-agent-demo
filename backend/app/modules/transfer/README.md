# 转账 Agent（平台工具 + 自有图）

用户说「从工资卡转 5000 给老王」，模块弹出一张**平台确认卡**；单笔或单笔+当日累计合计
超过 ¥1000 时卡上带验证码输入框（统一策略引擎口径，银行侧另有自己的大额阈值兜底）；
用户点「确认」并填码后，银行侧**只发生一次** `POST /transfers`（`confirm=true` + 用户填的那个
`mfa_code`）。点「取消」则银行一次写调用都没有。

用户说「明天 9 点给老王转 500 元」，模块先解析并冻结执行时刻，弹的是**定时转账确认卡**；
点击确认后创建银行任务单，不立即动钱，也不占当日累计。

用户说「四个人聚餐 300，AA 收款，我也有一份」，模块弹的是**AA 收款确认卡**；点击确认后只
创建一笔收款请求（钱向内收、发起时一分不出），每位参与者支付自己份额时才是真资金写。

设计文档：`docs/superpowers/specs/2026-10-13-transfer-card-form-spec.md`（含裁定与待开发项）。

## 接入方式

```python
# __init__.py
TRANSFER = DomainDeclaration(
    agent_id="transfer", display="转账 Agent", purpose="…",
    fields={amount_yuan, to_name, from_hint, remark, status, participants, participant_count, aa_request_id},
    graph_builder=build_transfer_graph,          # 整轮交给自己的图
)
MODULE = ModuleSpec(name="transfer", declaration=TRANSFER, tools=TOOLS, handlers=HANDLERS,
                    binder=bind_transfer)   # 装配：写门面进图、数据库进 transfer_prepare / transfer_aa_pay
```

**不声明 `turn`** —— `ModuleSpec` 不允许一次请求被两条路径回答。与 card／wealth 同构：

| 文件 | 职责 |
|---|---|
| `tools.py` | 十个平台工具 + 处理器（`(invocation, client)`）+ 金额解析 |
| `writes.py` | 模块自己的受控写路径：`submit_transfer`／`create_scheduled`／`cancel_scheduled`／`create_aa`／`pay_aa`／`cancel_aa`（**不走网关**） |
| `workflow.py` | `build_transfer_graph`：`decide → wait → process` |
| `__init__.py` | 声明与注册 |

## 十个工具

| 工具 | side_effect | 参数 | 银行调用 | 谁调 |
|---|---|---|---|---|
| `transfer_accounts` | READ | `{}` | `GET /accounts` | 模型 |
| `transfer_contacts` | READ | `{query?}` | `GET /contacts?q=` | 模型 |
| `transfer_scheduled` | READ | `{status?}` | `GET /transfers/scheduled` | 模型 |
| `transfer_prepare` | READ | `{amount_yuan, to_name, from_hint?, from_account_id?, to_account_id?, remark?}` | 只读解析，**不写** | 模型 |
| `transfer_schedule` | READ | `{amount_yuan, to_name, execute_date, execute_time?, from_hint?, from_account_id?, to_account_id?, remark?}` | 只读解析，**不写**；`execute_date` 支持「明天」「后天」或 `2026-11-01`，`execute_time` 默认 09:00 | 模型 |
| `transfer_cancel` | WRITE | `{task_id}` | `POST /transfers/scheduled/{id}/cancel` | 只由图的确认卡之后调用 |
| `transfer_aa` | READ | `{amount_yuan, participants? 或 participant_count?+include_me?, from_hint?, from_account_id?, remark?}` | 只读解析（查账户/联系人），**不写**；`participants` 最多 20 人，人数最少 2 | 模型 |
| `transfer_aa_status` | READ | `{aa_request_id}` | `GET /transfers/aa/{id}` | 模型 |
| `transfer_aa_pay` | READ | `{aa_request_id, participant_index?}` | `GET /transfers/aa/{id}` + `GET /accounts`，**不写** | 模型 |
| `transfer_aa_cancel` | WRITE | `{aa_request_id}` | `POST /transfers/aa/{id}/cancel` | 只由图的确认卡之后调用 |

`POST /transfers`、`POST /transfers/scheduled`、`POST /transfers/aa`、`POST /transfers/aa/{id}/pay`
**不是工具**：它们是 `writes.py` 的受控路径，由图的 `process` 在用户点击之后发起。

## 一次转账怎么走

1. Supervisor 把这一轮交给转账图，`decide` 走**原生工具调用**（`collect_stream` + `parse_domain_decision`）。
2. 模型调 `transfer_prepare`：handler 解析付款账户与收款人，把结果放进 `facts`（**不出服务端**）：
   `{"status": "ready", "intent": {...}, "from_label": …, "to_label": …, "requires_code": …,
   "risk_level": "yellow"|"red", "reason": …}`，
   或 `{"status": "select", "field": …, "candidates": [...], "intent": {...}}`。
3. `select` → 图挂一张 `kind="transfer_selection"` 的选择题（选项 label 来自银行数据）；
   `ready` → 图用平台的 `_conclude_turn` 挂 `kind="confirmation"` 确认卡，并**冻结**意图与一把幂等键。
4. 用户点击 → `Command(resume=…)` 回到同一个 `wait_for_user` → `process`。
5. `confirm` → `writes.submit_transfer` 发一次 `POST /transfers`（`confirm=true`、`mfa_code`、复用冻结的幂等键）；
   `cancel` → 本地收尾「已取消，本次转账没有发起。」，银行零写调用。
6. 银行回 1007 `CONFIRMATION_REQUIRED` → 卡片**升级成 `requires_code=True`** 原样重挂（同一 `pending_id`、同一幂等键），
   再点一次并带码即可；失败则如实收尾。

定时转账走同一条图：`transfer_schedule` 只解析并冻结 `execute_at`；`ready` 卡片的 `operation` 是
`schedule_transfer`，确认后 `process` 调 `writes.create_scheduled` 发 `POST /transfers/scheduled`。
创建任务单不是资金移动，`WriteFacade` 不记当日累计；银行到执行时刻仍按当时的限额与风险规则处理。

## 验证码

- `requires_code` 来自统一策略引擎：`app.policy.classify_operation("transfer", amount_minor=…,
  daily_accumulated_minor=…, side_effect=WRITE)`。单笔金额 + 当日已累计（`daily_operation_total`）
  超过 ¥1000 即红标、要验证码；银行仍是权威，1007 兜底。
- 验证码**只**经 `graph_builder(..., turn=…)` 的闭包进入 `process`：不进图 state、不进检查点、不进账本、
  不进 SSE 帧、不进审计行（`tests/transfer/test_transfer_card_wiring.py` 有回归）。
- 需要码而点击没带码：路由层直接 422 `VERIFICATION_CODE_REQUIRED`，图都不会被进入。

## 取消定时转账

`transfer_cancel` **对模型可见**，但 `run_call` 一律先把它拦成一张 `requires_code=False` 的确认卡，
只有用户点击之后才经网关执行（白拿审计与幂等）。银行侧取消本来就不需要 confirm／MFA。

## 定时转账的创建

`transfer_schedule` 解析金额、收款人、执行日期与时间，支持「明天」「后天」或 `2026-11-01`，默认 09:00；
执行时刻必须晚于当前时间。用户点击确认后，`process` 调 `writes.create_scheduled` 发一次
`POST /transfers/scheduled`（`confirm=true`、复用冻结的幂等键）；银行红档仍要验证码。
创建的是任务单，不是即时资金移动，因此**不占当日累计**；到期执行时由银行按当时规则处理。
prompt 明确要求模型不要把「明天 9 点」这类预约意图降级成即时转账。

## AA 收款（发起 / 查进度 / 付份额 / 取消）

PRD 验收口径：**按人数拆分并生成收款请求，不是直接转出**。四条路径都在同一条图上：

- **发起（`transfer_aa`）**：解析总额、参与人与收款账户，拆分规则与银行逐字对齐（平均拆分，
  余数给最后一名：100 元 3 人 → 33.33 / 33.33 / 33.34）。参与人**只有精确同名**的唯一联系人
  候选才绑账户 —— 模糊命中宁可不绑：把张三的模糊结果绑给李四，比「未绑定」糟糕得多。
  `include_me=true` 时「我」占最后一份，绑到**不同于收款账户**的活期账户（银行拒绝对同一账户
  自转）；名下没有第二个活期就只能未绑定。卡片是内在黄标（`aa_collect`），无码、**不占当日累计**
  —— 发起只生成收款请求，钱是向内收的。确认后 `process` 调 `writes.create_aa` 发一次
  `POST /transfers/aa`；回执带 `aa_request_id`，7 天内有效。
- **查进度（`transfer_aa_status`）**：渲染每人 待付/已付/已拒，标注「（我的一份）」。
  银行**没有 AA 列表端点**，编号来自发起回执或用户原话。
- **付我的份额（`transfer_aa_pay`）**：客户端前置校验（非参与者 / 已付 / 请求已关闭 / 我的份额
  绑在收款账户上 → 明确中文报错，**零银行写**）；多笔我名下的待付份额时挂选择题。
  这是**真资金写**：`aa_payment` 在策略引擎的资金写词表里，与即时转账同一套
  **单笔 + 当日累计 > ¥1000 动态升红**（红档落 `policy_decision` 审计行）；确认后
  `writes.pay_aa` 发一次 `POST /transfers/aa/{id}/pay`（付的是冻结份额，请求体不带金额），
  点击时限额复检、成功后记当日累计。
- **取消（`transfer_aa_cancel`）**：与 `transfer_cancel` 同构 —— 对模型可见但图先拦成确认卡
  （挂卡前先读一次详情），点击后才经网关执行 `POST /transfers/aa/{id}/cancel`，仅发起人可取消，
  未付份额转为已拒绝。

措辞防吞：用户话里出现 AA／平摊／均摊／人均／凑钱等标记时，**不走**即时转账直通（那里只有
收款人和金额，极易把「AA 收款 300 元」误当成一笔转账），一定交给模型解析。

### AA 的边界（如实告知）

- 无「我的 AA 列表」查询：银行没有该端点，状态与支付都需要编号。
- 参与人账户**不可后期绑定/改绑**；发起时未绑定的参与人无法经银行支付（只能线下自己处理）。
- 只支持等额拆分 + 余数给最后一名（银行契约），不支持按人自定义金额。
- 名下只有一个活期账户且它就是收款账户时，「我的一份」无法经银行支付
  （转出与转入不能相同）。

## 测试

```powershell
# 仓库根；用本机 PostgreSQL，快 10 倍
.\pytest-local.ps1 -q tests/transfer
```

| 文件 | 测什么 |
|---|---|
| `test_transfer_tools.py` | 金额解析、账户／联系人匹配、`transfer_prepare` 的 facts 契约、工具注册、取消工具 |
| `test_transfer_scheduled.py` | 定时转账的 `execute_at` 冻结、默认 09:00、过去时间拒绝、选账户保留执行时刻 |
| `test_transfer_aa.py` | AA：余数拆分、占位名、include_me 绑定、<2 人拒绝、进度渲染、付份额黄/红档与审计、前置拒绝零银行写、多份我的选择、取消 |
| `test_transfer_writes.py` | 一次写：请求体、幂等键、非数字码、银行错误映射、AA 三写（发起不动钱 / 付份额带金额 / 取消） |
| `test_transfer_graph.py` | 图：挂卡、`requires_code` 阈值、点击、取消、1007 升级、选择分支、终态 `turn_status`、AA 四路径与措辞防吞 |
| `test_transfer_card_wiring.py` | HTTP 全链路：SSE 帧、422、码不泄漏、双击、取消定时转账与审计、AA 发起卡与红档付份额 422→带码成功 |
| `test_transfer_policy_daily.py` | 装配根把数据库绑进 `transfer_prepare`／`transfer_schedule`、日累计越线升红 |

## 与平台的边界

- 写**不走网关**（网关没有验证码通道）→ 没有这次写的 `tool_call`／`tool_result` 审计行；只有 SSE 收尾帧与业务日志。
- `bank_backend` 一行不改；平台侧（`app/agents`、`app/tools`、`app/api`）与前端一行不改。
