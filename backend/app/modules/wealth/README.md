# 理财子智能体

这个包是一个理财 Agent：它通过真实 HTTP 读写 `bank_backend` 的 PostgreSQL 数据，完成查询、风险
测评、推荐对比、申购、持仓收益和赎回。它可以**独立调用**（CLI／测试），也可以被主智能体当作
**领域图**调用（`ModuleSpec.graph_builder` → `workflow/graph.py`）——两条路走的是同一个
`WealthAgent`，没有第二套实现。

```text
WealthAgent.handle(context, request, *, session: SessionData, pending: PendingData | None,
                   conversation=None) -> tuple[WealthResponse, SessionData, PendingData | None]
build_wealth_agent(model: ModelClient, bank: WealthBank, clock: Clock, *,
                   tools: Mapping[str, ReadTool] = READ_TOOLS) -> WealthAgent
```

| 层 | 目录 | 职责 |
| --- | --- | --- |
| `contracts/` | `contracts/` | 请求／响应／银行协议／会话视图 DTO；**没有**授权字段 |
| `application/` | `application/runtime.py`、`factory.py` | 对外 `handle`、会话与待办、点击确认与核实、装配 |
| `workflow/` | `workflow/graph.py`、`nodes.py`、`state.py` | 领域图（`build_wealth_graph`）与内部读流程：理解、工具选择、追问、推荐、对比、预览 |
| `domain/` | `domain/rules.py`、`recommendation.py` | 金额规范、交易预检、风险硬约束、筛选与排序 |
| `infrastructure/` | `infrastructure/bank_adapter.py`、`tools.py` | `HttpWealthBank`（正式银行实现）与只读工具白名单 |
| `llm/` | `llm/toolcalls.py`、`decisions.py`、`prompts.py` | 模型工具表、工具调用解析、领域提示词 |
| `cli.py` | `cli.py` | 本地独立入口：只做输入输出与依赖装配 |

依赖方向固定：`application → workflow/llm/infrastructure/domain → contracts`。资金写只有一条路：
运行时的受控节点 → `HttpWealthBank`。图只到"待确认"为止，模型没有任何通道直接写钱。

## 与主智能体的关系（现状）

- **已接通**：主智能体经 `ModuleSpec.graph_builder` 把整轮交给本模块的领域图
  （`workflow/graph.py`），并把它当前会话的视图（摘要、近期轮次、语义线索）通过
  `app/context/handoff.py` 传进来。本模块同时把只读工具注册进基座的工具网关，所以每一次查询
  都是基座自己调用的原生工具（`tool_call`／`tool_result` 帧）。
- **模型自主选择工具**：模型看到一份完整工具表（名字、说明、参数 schema、副作用、输出概要），
  自己决定查哪个、问什么、什么时候结束；程序负责参数校验、业务前置条件与可执行性。
- **外层记忆**：理财内部的槽位、展示顺序与待办住在图 state 的 `wealth_*` 字段里，由基座的
  检查点保存（重启后按同一个 `thread_id` 读回来）；"用户上一轮说了什么"来自外层账本，由外层按
  `conversation_id` 保存并注入。恢复出来的只是语义线索，不构成任何交易授权。
- **点击确认已接通（原 C1 已解除）**：申购／赎回预览回来是 `TurnStatus.WAITING_CONFIRMATION` 加一份
  结构化确认卡片（操作、产品／持仓、金额、账户、待办编号），由基座写成检查点里的暂停并以 SSE
  `interrupt`（`kind=confirmation`）送到页面。用户点击后，前端向鉴权路由
  `POST /api/conversations/{id}/pending-actions/{pending_id}/decide` 提交
  `{question_id, decision, verification_code?}`，基座把它转成 `TurnAction`，由领域图在同一个
  线程（同一份检查点）上处理。聊天里的"确认"两个字走普通消息通道，那里没有任何能铸造授权的动作。
  - 待办**不再被撤下**，也不再回一句"不支持"：它是确认的绑定键，预览因此可以刷新后重看。
  - 改任何槽位即换意图版本，旧 `pending_id` 立即失效；重复点击（同一个 `Idempotency-Key`）由生命周期去重。
- **确认框按银行的要求变形（原 D2 的解决方式）**：银行的写门禁要求 `confirm=true` 且验证码等于配置的
  演示码，所以 `HttpWealthBank.write_authorization()` 如实回答 `CLICK_AND_CODE`——**确认框上因此出现
  验证码输入**，用户在框里当场输入，运行时把它原样送进写请求的 `mfa_code`。银行改回无验证码通道之后，
  把那个方法改成回答 `CLICK` 即可：卡片上的验证码输入会自己消失，运行时的状态机与前端都不用改。
  - **不代填、不猜、不读配置**：缺验证码时运行时在**发出任何资金请求之前**返回 `VALIDATION` 并
    明确说"这笔需要验证码"，绝不会从配置里取一个演示码冒充用户输入。
  - **输错了能重输**：验证码被银行判错时，待办、快照与幂等键都还有效，所以预览原样交回来重新等确认
    （同一笔，幂等键不换），而不是把整个流程结束掉。
  - **验证码不进检查点**：它由鉴权路由接住，沿 `TurnContext.verification_code` 送到模块，从不进入
    `AgentState`——所以账本、检查点、模型上下文与审计里都没有它。
- **旧验证码入口仍然停用**：验证码现在是**确认的一个参数**，不是一条独立流程；`kind=verify` 保留在
  词表里只为能明确拒绝它。预览文案里那句"确认后还需要输入模拟验证码"已经删掉，改成
  "点击确认即提交给银行，不需要输入验证码；取消不会产生任何交易。"（要不要输入由卡片说了算。）
- **结果未知不会被说成失败**：一次写请求超时（结果未知）映射成 `TurnStatus.UNKNOWN`，不是
  `FAILED`——把一笔可能已经成交的交易报成"没办成"，用户会去重新下单。事件层
  （`subtask_end.status`）的词表仍被冻结为 SUCCEEDED/FAILED/DENIED，所以那一层还是 FAILED；
  真实的"未知"经 `domain_results[].turn_status` 与 `subtask_end.turn_status` 一起传出去，
  页面据此把它显示成"待核实"而不是"失败"。

---

## 一、按顺序准备（照这个顺序做即可复现）

### 1. 准备指定的数据库与 schema

本模块的正式路径是 **HTTP → bank_backend → PostgreSQL**。验收用**专用 schema**，不碰共享的
`public`，也不碰别人正在用的 schema。

```powershell
# 在共享 test 库上建一个本次验收专用的 schema（名字自己取，示例）
$env:SCHEMA = "test_wealth_accept_<你的后缀>"
psql ... -c "CREATE SCHEMA `"$env:SCHEMA`""
```

后续所有命令都用这两个变量指向它（**连接串含密码，不要回显、不要提交**）：

```powershell
$env:DATABASE_URL = "postgresql+psycopg://<用户>:<密码>@<主机>:5432/test"
$env:PGOPTIONS   = "-c search_path=$env:SCHEMA"
```

### 2. 执行银行迁移

```powershell
cd bank_backend
uv run alembic upgrade head        # 0001_bank_mock_domain → 0002_wealth_support
```

### 3. 显式初始化财富样例数据

服务启动**不会**写业务数据，必须显式执行：

```powershell
# 基础种子 + 模拟时钟（清空业务表并按数据库设计文档重建）
curl -X POST http://127.0.0.1:8001/api/seed/reset

# 理财专用演示数据：四款产品、u_wealth 的两个账户、样例持仓
uv run python -m scripts.seed_wealth_demo --allow-test-database
```

`seed_wealth_demo` 默认拒绝库名以 `test` 结尾的库，用 `--allow-test-database` 显式确认。
重复执行不会重复入金：数据齐全时校验后复用，只存在一部分时明确拒绝。

期望结果（本模块的验收前置条件）：

```text
a_wealth_main 2000000 分（20000 元）／a_wealth_backup 500000 分
样例持仓 h_wealth_sample：本金 100000 分、价值 100500 分
产品：p_demo_cash（灵活 low 0 月）、p_demo_stable（稳健 medium 3 月）、
      p_demo_growth（进取 high 12 月）、p_demo_closed（停售 low，模拟年化 9%）
```

### 4. 启动 bank_backend（独立进程）

```powershell
cd bank_backend
uv run uvicorn app.main:app --port 8001        # 健康检查 http://127.0.0.1:8001/api/health
```

**银行进程必须连到第 1 步的 schema**：进程环境里带上 `DATABASE_URL` 与 `PGOPTIONS`。
`GET /api/health` 在业务数据未初始化时返回 503，这是"还没 seed"，不是"服务挂了"。

### 5. 构造独立 Agent

只要三样东西：一个 `ModelClient`、一个 `WealthBank`、一个 `Clock`。

```python
from app.integration.bank_client import BankClient
from app.modules.wealth.application.factory import SystemClock, build_wealth_agent
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import TrustedContext

bank = HttpWealthBank(BankClient("http://127.0.0.1:8001/api"))
agent = build_wealth_agent(model, bank, SystemClock())
context = TrustedContext(user_id="u1", session_id="s1", bank_customer_id="u_wealth")
```

`bank_customer_id` 就是身份，**必须显式绑定**：没有绑定就在发请求之前失败，不会退回银行默认客户。
`build_wealth_agent` 也不接受内存银行：缺依赖当场 `TypeError`，不会"看起来成功"。

### 6. 对话演示（`cli.py`）

```powershell
cd backend
# 依赖自检：银行只读探针 + 模型配置
.venv/Scripts/python.exe -m app.modules.wealth.cli --check `
    --bank-url http://127.0.0.1:8001/api --customer u_wealth

# 交互式对话（等待确认时输入 确认 提交、取消 放弃；没有验证码这一步）
.venv/Scripts/python.exe -m app.modules.wealth.cli `
    --bank-url http://127.0.0.1:8001/api --customer u_wealth

# 非交互：一次给几句话（脚本化演示、验收自检）
.venv/Scripts/python.exe -m app.modules.wealth.cli --bank-url http://127.0.0.1:8001/api `
    --customer u_wealth --message "有哪些可以灵活赎回的产品？"
```

入口只做输入输出与依赖装配：每一句话都变成一次 `handle`，CLI 里没有一句银行调用，也没有
"直接买产品"的捷径。终端会打印完整的确认预览（产品、金额、账户、余额、期限、赎回规则和确认
原文），等待确认时提示输入"确认"或"取消"；模型缺配置就报错，脚本测试模式必须显式 `--script <文件>`
并在终端打出"这些回复不是模型说的"横幅。

CLI 不再向用户索要验证码：确认即提交（当前银行能力不兼容时会如实打印
`原因：confirmation_transport_unsupported`）。`grep -n "verification_code\|needs_code\|CODE_PROMPT"
app/modules/wealth/cli.py` 没有任何结果。

### 7. 运行分层测试

三层**不能互相替代**（计划 §8.1）：

```powershell
# 第一层：Agent 离线（禁止出站网络，脚本模型 + HTTP 替身）
cd backend
.venv/Scripts/python.exe -m pytest tests/wealth -q

# 第二层：银行数据库（专用 schema、迁移、SQL 记录、事务、API）
cd bank_backend
uv run pytest -q

# 第三层：真实 HTTP 端到端（银行独立进程 + 本模块进程）
cd backend
$env:WEALTH_TEST_BANK_URL      = "http://127.0.0.1:8001/api"
$env:WEALTH_TEST_BANK_SCHEMA   = $env:SCHEMA
$env:WEALTH_TEST_BANK_DATABASE_URL = $env:DATABASE_URL    # 直连核验用
$env:WEALTH_ACCEPT_LLM         = "1"                      # 只在要跑真实模型时设
.venv/Scripts/python.exe -m pytest tests/wealth_integration -q
```

第三层里的 `test_acceptance.py` 就是计划「七、最终验收矩阵」A01–A15 的执行体；它要求 schema
处于**刚 seed 完**的状态，并在模块开始时断言这一点（不满足时给出重建命令）。它真的会花钱
（申购／赎回），跑完可以用第 3 步的两条命令把演示数据复位。

---

## 二、模拟边界与限制（如实说明）

- 银行业务数据全部是**模拟**：产品、收益率、验证码都是配置值，不接真实资金、行情或短信。
- 模型密钥只存在于 `backend/.env`（已 gitignore）；CLI 只打印模型名与 base URL，不打印密钥。
- 会话、待办与意图版本住在图 state 里（`wealth_*`），由基座的检查点保存，同一个 `thread_id`
  重启后读回来；只有 Agent 侧的"本进程写锁定"是进程内存，重启即丢。银行里的测评、余额、持仓、
  订单和幂等记录不会丢，但重启后要重新查询、重新确认。
- Agent 侧的写锁定与银行持久化的 `SecurityState` 是两套状态，互不替代，本模块也不改银行阈值。
- 本模块注册的七个工具全是**只读**的（`wealth_products`、`wealth_product`、`wealth_accounts`、
  `wealth_holdings`、`wealth_holding`、`wealth_risk_profile`、`wealth_order`），没有任何写工具：
  整轮由 `ModuleSpec.graph_builder` 交出的领域图承担，点击确认由基座路由转成 `TurnAction` 后交给
  同一张图，基座只做搬运与鉴权，不替它判断业务。只读回合因此会出现 `tool_call`／`tool_result`
  帧；以 interrupt（提问卡）结束的回合**也会**发出 `subtask_end`——它的 `turn_status` 是
  `waiting_input`、摘要就是那句追问（事件层的 `status` 仍是冻结词表里的 `SUCCEEDED`）；承载查询结果的
  那条 `turn_status=completed` 子任务行属于**用户答完追问后的完成轮**（依 2026-10-13 提交 `03fd141`）。

### 银行验证码门禁：现在怎么处理的

银行的写门禁在 `bank_backend/app/domain/investment_service.py::_require_verification`：

```python
if not confirm:
    raise ConfirmationRequiredError(f"{action} 需要 confirm=true 才会执行")
if not mfa_code or mfa_code != self.settings.mock_mfa_code:
    raise VerificationFailedError(f"{action}的模拟验证码不正确")
```

**本仓库没有改银行**，也不需要改：适配器如实声明 `CLICK_AND_CODE`，确认框因此向用户要验证码，
用户输入的值随确认一起提交。链路是完整的——只是最后一步要用户输入一个码，而不是"点了就成交"。

如果将来希望**大额才要验证码**（"不用的就确认框"），改动只有一处：让
`HttpWealthBank.write_authorization()` 按 `amount_minor` 分级，例如小额回答 `CLICK`、大额回答
`CLICK_AND_CODE`。运行时的状态机、确认卡片和前端都不用动——"要不要验证码"只有这一个判断源。

**不要**用"自动填入固定验证码"绕过它：那既触发银行的验证失败计数，又让用户以为一笔并非自己确认的
提交已经发生。

### 手工真实链路脚本

`backend/scripts/e2e_confirmation_chain.py` 是**手工**验证入口（不是 pytest），它按顺序跑完整条链并
逐跳打印：用户原话 → 预览卡片（含 `requires_code`）→ 刷新后重读 → 聊天里的"确认"被拒 →
不带验证码的点击被拒 → 输错验证码后预览重新交回 → 输入正确验证码并确认 → 同一个键重复点击 →
账本。`--code` 是要输入的码（默认银行配置的演示码，脚本**不去读银行配置**：一次自己去取码的运行
证明不了"用户提供了它"）。前置条件是银行（:8001，刚 seed 完的 schema）与主助手（:8000，指向它）
都在跑。实测结果与截图见
[端到端验收记录](../../tests/wealth_integration/验收记录.md) 的复核轮附录。
