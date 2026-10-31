# 订阅管理

订阅模块已通过 `BankClient` 接入独立模拟银行，声明 7 个工具并用自己的 LangGraph 子图发放确认卡片。它沿用现有 Supervisor、工具网关、会话账本与 checkpoint：主 Agent 只负责"这句话归订阅管"的派活与收尾，识别、确认与执行都在模块自己的图里完成。

## 功能与权限

| 操作 | 工具 | 代码判定的权限 |
| --- | --- | --- |
| 查询订阅列表、指定订阅详情 | `subscription_list` / `subscription_detail` | 查询直接执行 |
| 取消、暂停、恢复、立即扣款 | `subscription_cancel` / `subscription_pause` / `subscription_resume` / `subscription_charge` | 黄色：只读订阅、冻结快照并弹确认卡片，不调用银行写接口 |
| 执行已确认的操作 | `subscription_execute` | 只由模块代码在按钮确认后调用，不进入模型工具菜单 |

模型可见的是 6 个工具：两个只读工具与四个写工具；`subscription_execute` 是唯一真正写银行的动作，从不出现在交给模型的工具清单里。四个写工具共用同一个"准备"处理器：读取订阅、冻结 `pending_action` 快照、渲染预览，然后把确认卡片交给页面。

金额以整数分传递，必须为正整数，零金额在准备阶段就被拒绝（基座的 `TurnConfirmation` 不接受非正金额）。同一商户命中多笔订阅时，模块把实际查询结果作为候选让用户选择，禁止模型编造订阅 ID 或金额。

## 图与状态

模块图有三个节点（`decide` / `wait` / `process`，加上 `START` 共四个），按"是否允许碰模型"划分：

```
START → decide ──条件边──→ wait / END
                wait → process ──条件边──→ wait / END
```

- `decide`：用模型的 native tool calling 跑决策循环（最多 6 轮）。只读工具的结果回灌模型继续决策；写工具的结果是一张待确认卡片，图就此停住；没有工具调用时收尾。
- `wait`：唯一的暂停点，只 `interrupt()`，不碰模型——一次暂停不产生额外模型调用。
- `process`：读取按钮决定并校验快照，确认则执行，取消则结束。

图 state 在基座通道之上只加三个字段：`pending_action`（冻结的操作快照：`pending_id` / 操作 / 目标 / 金额 / 扣款账户 / 到期日 / 幂等键）、`subscription_call`（当前工具调用）、`subscription_choices`（候选 `label → subscription_id`）。

确认卡片有效期 5 分钟，按**真实 UTC 墙钟**计算：银行的"今天"是模拟时钟，不能用来判断用户等了多久。

## 确认链路

1. 写工具在 `pending_action` 里冻结操作、目标与一个 `pending_id`，并生成幂等键；此时银行一行未写。
2. `wait` 节点调用基座的 `wait_for_user`，`interrupt` 一份 `kind="confirmation"` 的问题：`question_id`、`display` 预览、`fields=[decision select 确认/取消]`，以及 `confirmation=TurnConfirmation(...).as_payload()`。
3. 页面点「确认」/「取消」→ `POST /api/conversations/{conversation_id}/pending-actions/{pending_id}/decide`。基座校验 `kind`、当前 `question_id` 与 `confirmation.pending_id` 都匹配后才恢复图。
4. `process` 节点再次校验快照存在、目标与操作齐全、未过期、`pending_id` 对得上。确认 → 调用 `subscription_execute`（复用快照里的幂等键，重试只重放不重复）；取消 → 直接结束"本次操作已取消，未修改订阅。"。两条路都不再调模型。

**为什么确认卡片这么发。** 基座只对声明了 `turn` 的模块走受控动作入口（`action_handler(spec.turn)`）；订阅声明的是 `graph_builder`、没有 `turn`，走那条路会被判成 `DENIED`。而按钮路由只接受 `kind="confirmation"`、并要求 `confirmation.pending_id` 与 URL 一致；前端也只渲染这一种确认形状。所以模块复用基座 `_confirmation_pause` 的载荷，前端不必为订阅加特例。

**为什么这条通道对模块图是开的。** 模块图的 `wait` 节点调用的是基座同一个 `wait_for_user`，恢复时按钮回答以 `resume`（带 `question_id`）送回同一个问题，`process` 节点即可读到决定。代价是图里**只能有一个** `wait` 节点：基座的 `_one_question` 要求恰好一个 `interrupt`，多一个卡片就匹配不上。

两条硬规则：

- **聊天框里打"确认"不算授权**：那是普通消息，基座以 409 `INVALID_INTERRUPT_ANSWER` 拒绝把它接到当前卡片上（见验收记录 S05），卡片保持打开，仍需点按钮。
- **同一笔待办复用同一个 `pending_id`**：重发卡片不换号，旧卡片上的按钮仍然有效。

## 数据声明：subscription

- 场景：确认前冻结、跨请求恢复、点击后幂等执行。
- 唯一所有者与权威来源：`bank_backend`；Agent 不直连银行业务表。遵循[数据归属契约](superpowers/specs/2026-09-23-module-data-ownership-contract.md)。
- 数据实体：银行的 `subscriptions` / `subscription_charges` / `transactions` 与 `idempotency_records`；Agent 侧不新建表，会话槽位随 checkpoint 走。
- 持久化原因：银行必须独立判断订阅状态与幂等，不能信任模型或浏览器传入的确认标记。
- 读写者：银行订阅服务与既有审计服务；Agent 经 `BankClient` 使用。取消/暂停不产生流水，立即扣款才产生。
- 事务：写端点按订阅行加锁；扣款以 `(subscription_id, due_date)` 唯一键保证一个账期只扣一次，重复点确认由 `Idempotency-Key` 吸收。
- 保留：演示数据保留至显式 `POST /api/seed/reset` 或重跑种子脚本。
- Agent checkpoint：只保留公开 `pending_id`、操作、目标与用户选择；银行仍是执行状态的权威来源。
- 迁移：银行订阅表是既有模型，本模块没有新增迁移。

## 与主 Agent 的接口

接入只改 `backend/app/modules/subscription/__init__.py`：`ModuleSpec(name="subscription", declaration=SUBSCRIPTION, tools=TOOLS, handlers=HANDLERS)`，声明里带 `graph_builder=build_subscription_graph`。基座把绑定了身份的 `TrustedContext` 注入每次工具调用，模块不自己猜用户。

银行端点（前缀 `BANK_BACKEND_BASE_URL`，默认 `http://127.0.0.1:8001/api`）：

| 方法 | 路径 | 谁调用 | 说明 |
| --- | --- | --- | --- |
| GET | `/subscriptions` | `subscription_list` / `subscription_detail` | `?status=active` 可过滤；返回裸数组；`subscription_detail` 在结果里按 `subscription_id` 客户端过滤 |
| GET | `/subscriptions/{id}` | —（未使用） | 银行有此端点，但模块不调用它 |
| POST | `/subscriptions/{id}/cancel` | `subscription_execute` | 取消 |
| POST | `/subscriptions/{id}/pause` | `subscription_execute` | 暂停 |
| POST | `/subscriptions/{id}/resume` | `subscription_execute` | 恢复 |
| POST | `/subscriptions/{id}/charge` | `subscription_execute` | 立即扣本期，走 `Idempotency-Key` 头 |

银行的 `POST /subscriptions/run-due`、`POST /subscriptions`（创建）与 `GET /clock` 不在这 7 个工具里：前两个是模拟入口，时钟由银行内部使用。

出站只有一个口子 `BankClient`（`backend/app/integration/bank_client.py`）：`request(method, path, user_id=..., json=..., idempotency_key=...)`。`user_id` 在 query 与 body 各发一次（GET 读 query、POST 读 body），幂等键走 `Idempotency-Key` **请求头**（放 body 会被判 422）。模型由主 Agent 统一配置，模块只使用它做 native tool calling，不自己连模型。

## 最小底座扩展

`DomainDeclaration.graph_builder` 是可选构图入口，默认领域仍用通用子图；订阅声明自己的图后，确认卡片与恢复都走模块图自己的节点。`interrupt` 复用基座既有的 `kind="confirmation"` 载荷，没有新增事件类型，也没有把确认参数加进公共 `ToolCall`。银行的订阅表与接口本就有，未新增 pending-action 表；`ModuleSpec.turn` 的接线保留在底座，但已无领域声明它。

## 启动与演示

三个进程：银行后端（`:8001`，库 `bank_db`）、主 Agent（`:8000`）、前端（`:5173`）。仓库根目录 `start.ps1` 可一键起前两个，也可以手动：

```powershell
cd bank_backend
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

前端演示会话固定为 `user_0001`，绑定的银行客户是 `u_wealth`，而银行自带种子只给 `u_zhang` 造了订阅，所以先补一次演示数据：

```powershell
cd bank_backend
.venv\Scripts\python.exe -m scripts.seed_subscription_demo --dry-run
.venv\Scripts\python.exe -m scripts.seed_subscription_demo
# 演示几轮后想从头再来（只删本脚本造的演示客户订阅）
.venv\Scripts\python.exe -m scripts.seed_subscription_demo --reset
```

模型在主 Agent 侧配置（`backend/.env` 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`，或页面「设置 → 模型」）。起来后在页面里试：

- "我有哪些自动扣费的订阅？"
- "最近有什么订阅要续费？"
- "帮我取消 Netflix 的订阅。" → 确认卡片 → 点「确认」。
- "取消订阅" → 追问"要操作哪一个" → 说"Spotify" → 确认卡片。

不开浏览器也能验收整条链路。`backend/scripts/ui_smoke.py` 发的是前端一模一样的请求（`/api/session` → `/api/conversations` → `/messages` → `pending-actions/decide`），会边跑边打印子 Agent 结论、确认卡片与银行真实状态；它需要运行中的后端与真实模型，属于真机验收而不是单测：

```powershell
cd backend
.venv\Scripts\python.exe scripts\ui_smoke.py
```

对真实银行跑模块级联调（未设置环境变量时跳过）：

```powershell
cd backend
$env:SUBSCRIPTION_TEST_BANK_URL = "http://127.0.0.1:8001/api"
.venv\Scripts\python.exe -m pytest tests/test_subscription_live_bank.py -q -s
```

## 真机验收记录

订阅模块的端到端真机验收（任务6）记录在 [backend/tests/subscription_integration/验收记录.md](../backend/tests/subscription_integration/验收记录.md)：
逐条给出用户原话、SSE 事件序列、确认卡片载荷、按钮决定请求与银行行的前后值，并列出尚未验证的性质与已发现的缺陷。

真机验收脚本：`backend/scripts/e2e_subscription_confirmation.py`（`--setup` 停在卡片并打印会话号；`--from-card` 重启后读回卡片；`--capture` 写前端抓取 JSON；`--preview` 验证未确认的预览不写银行）。
