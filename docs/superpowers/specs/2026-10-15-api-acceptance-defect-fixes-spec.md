# API 验收缺陷修复 spec（api_acceptance_local 2026-10-15）

- 状态：**未实现（待评审，第 5 版）**。本文件只定义问题、判定与方案，未改动任何产品代码。
- 日期：2026-10-15（第 4 版 2026-10-16：第 2 版自审修订 + 第 3 版范围纪律自查 + 第 4 版四条待拍板项定案 + 第 5 版 spec↔plan 对齐审计）
- 基线：`dev` @ `13b1383`（= 验收报告所测的那个 commit）
- 工作树：`D:\ai_project\agent\.worktrees\acceptance-defects-round2`（分支 `fix/acceptance-defects-round2`）
- 上位文档：`D:/ai_project/agent/_accept/REPORT_api_acceptance_local.md`（77 例：64 PASS / 4 FAIL / 7 DEVIATE / 2 N/A）
- 复核报告：`D:/ai_project/agent/_accept/ROOTCAUSE_round2_acceptance_defects.md`（本 spec 的证据来源）
- 兄弟 spec：`docs/superpowers/specs/2026-10-15-acceptance-defect-fixes-spec.md`（对应 UI 验收报告，另一批缺陷；其 §6 明确把 W03/W13 划出范围，由本 spec 承接）
- 验收脚本：`D:/ai_project/agent/_accept/api/run_api.py` + `rig.py`（**不是 git 仓库**，纯文件）
- 评审记录：第 1 版自审发现 12 处漏洞/歧义（详见 §10），已逐条修正；第 3 版做了范围纪律自查（§1.1、§11）——把「我加的」与「报告要求的」分开；第 4 版把四条待拍板项定案（§10.1，**全部维持现状、不新增代码改动**）；第 5 版按用户 m00730 做了 spec↔plan 对齐审计（§10.2，**不新增需求、不改范围**）；本版仍待用户评审

---

## §0 结论

4 个 FAIL 里 3 个根因成立（其中一个比报告写的更深），1 个报告根因写错；7 个 DEVIATE 里 1 个真缺陷（W03）、1 个真不一致（T07）、1 个非代码缺陷（S07）、1 个模型波动（T01）、3 个属于「设计决策 vs 验收期望不一致」（C14/C18/W01）。

| 编号 | 报告判定 | 复核定性 | 一句话根因 |
|---|---|---|---|
| T08 | FAIL | **真缺陷（框架层，非 transfer 漏写）** | 网关按**声明视图**校验，模型视图把可选参数变成「必填但可空」，弱模型回字符串 `"null"`，声明视图里它是合法字符串 → 一路穿过到 `prepare()`，被当成真的 `from_account_id` 用 |
| T02 | 同族（首轮失败） | **真缺陷（与 T08 同源）** | `transfer_scheduled.status` 是枚举，字符串 `"null"` 过不了模块自己那道 `Draft202012Validator`，于是报「转账参数不完整」 |
| T03 | FAIL | **真缺陷（跨仓：agent + bank mock）** | agent 侧 `transfer_contacts` 拒绝空 query，bank 侧 `GET /contacts` 也强制 `min_length=1` → 两侧都没有「列出全部收款人」的路径 |
| S02 | FAIL | **真缺陷** | `subscription_detail` 只认 `subscription_id`，但模型可见的 `subscription_list` 文本里**没有** id；同模块的写工具早已支持 `merchant_name`（模块内部自相矛盾） |
| W13 | FAIL | **报告根因写错**（能力存在，是路由问题） | `wealth_order` 工具真实存在；但 wealth 的 `purpose` 里没有「订单」，而 supervisor 菜单**只**渲染 `purpose`，于是订单请求根本没被派发到 wealth |
| W03 | DEVIATE | **真缺陷（跨仓）且比报告更深** | bank `holding_view()` 不返回 `product_name`（调用方其实已经把 Product 加载出来了）；agent 侧 `Holding` 契约里根本没有 `product_name` 字段 → 全链路都没有产品名 |
| T07 | DEVIATE | **银行 mock 自相矛盾**（非 agent 缺陷）；定案：**维持现状**（§4.7） | `risk.require_confirmation()` 只校验 `isdigit()`，而 card / investment 都校验 `settings.mock_mfa_code`；但对齐会引入「错误验证码终结本轮并丢卡」的死路，故不改 |
| S07 | DEVIATE | **非代码缺陷** | 代码里**没有**近似匹配：`matched_subscriptions()` 用的是 `==` 精确比较；是模型自己把「网易云音乐」跳到「音乐会员」 |
| T01 | DEVIATE | **模型波动，非代码缺陷** | 只读意图识别不稳（UI 轮通过）；按提示词护栏处理 |
| C14 | DEVIATE | **设计决策 vs 验收期望**；定案：**维持现状**、改验收期望（§4.7） | 已挂失的卡再挂失时模型直接解释、不调工具；银行侧对重复挂失是**干净报错无副作用**（`"该卡不能再次挂失"`）→ 强制调工具反而会让文案变粗 |
| W01 | DEVIATE | **已定架构决策**；定案：**接受现状**、只改验收文档（§4.7） | 「模块带工具进场、只读也走网关 tool_call」是有意设计（`wealth/__init__.py:1-13` 有记录） |
| C18 | DEVIATE | **设计决策**；定案：**维持现状**、验收期望 FAIL → DEVIATE（§4.7） | 低于起转金额走交互纠正卡而非终态 FAILED，是有意设计 |
| 报告 §4 | — | **验收侧夹具/环境问题**，非产品代码 | SMTP 必须 STARTTLS、LLM 30s 超时、主网关 503（夹具 `available_minor` 漂移**已修**，见 §2.11） |

---

## §1 需求

**R1（框架层，P0）** 任何工具调用的参数占位符，必须在**一个地方**被统一归一化，且对**所有**工具生效（含 wealth 的原生只读工具）。
验收：网关级测试——对一个**已声明可选字符串属性**的 spec 传 `{"optional_str": "null", "optional_str2": "none", "num": 0}`，交给 handler 的实参只剩 `{"num": 0}`；`None` 被丢弃、`0/False/[]/{}` 被保留。（注：不能往 spec 里塞未声明的属性，声明视图 `additionalProperties=False` 会先拒。）

**R2（P0）** 转账金额超过可用余额时，必须返回「可用余额不足」，不得因为占位符落到「没有找到符合条件的付款账户」。
验收：`transfer_prepare({"amount_yuan":"9999.00","to_name":"老王","from_hint":"null","from_account_id":"null"})` → `INSUFFICIENT_BALANCE`。

**R3（P0）** 用户不提供任何条件时，收款人查询能列出全部收款人（agent + bank 两侧都放行）。
验收：`transfer_contacts({})` 返回该用户全部收款人；bank `GET /contacts`（无 `q`）返回 200 + 全量；带 `q` 的既有搜索行为不变。（报告 §3.1 记录过模型编造 `{"query":"*"}` → 银行返回空的假否定；「把 `*` 当通配符」的语义**不在本次范围**，见 A1。）

**R4（P0）** 订阅详情能用**展示名或商户名**定位，与同模块写工具的选择器口径一致。
验收：夹具 `sub_5d1e3b529d4c468aa18a` 的 `name="验收订阅"`、`merchant_name="验收商户"`；`subscription_detail({"name":"验收订阅"})` 与 `subscription_detail({"merchant_name":"验收商户"})` 都返回该笔详情；`{"subscription_id":"sub_5d1e..."}` 行为不变；三者都不给 → 不报 NOT_FOUND 而是 `VALIDATION`。

**R5（P0）** 「订单」类请求必须能路由到 wealth。
验收拆两层：**确定性部分**——supervisor 菜单（`describe_agents()` 的输出）里 wealth 那一行含「订单」（第 5 版修正：既有 `test_supervisor_handoff.py` 夹具只驱动 `parse_decision`、不跑真实模型，所以确定性测试只能钉菜单文本，见 §10.2）；**端到端部分**——重跑 `run_api.py` 的 W13「查一下订单 order_e95ad538496d4b86a7b5 的执行结果」，模型实际派发到 `wealth` 并调用 `wealth_order`（实施计划 Task 9）。（报告 §3.4 自己要求「在 PROMPT 里点明订单号形态」，改 purpose 即是。）

**R6（P0）** 持仓事实里必须带产品名。
验收：`wealth_holdings` 的 facts 里每条持仓含非空 `product_name`（summary 是聚合句，不在本需求内）。

**R7（P2，可选 = A3）** 把六个模块的 `purpose` 逐个对照它自己注册的**面向模型**的工具。**报告没有要求这一条**；本次只做 R5 的 wealth 一处。§2.6 的审计表仅供参考（它显示 wealth 还缺「账户余额/风险测评」、transfer 缺「账户」，但那些没有对应的失败用例）。

**R8（已定案：不做代码改动）** 转账路径的 MFA 口径**维持**「任意数字码通过」。
- 曾推荐与 card / investment 对齐（校验 `settings.mock_mfa_code`），复核后**自行否决**：转账的 confirm 节点 `backend/app/modules/transfer/workflow.py:430-441` 只处理 `CONFIRMATION_REQUIRED`（1007）；银行若回 `VERIFICATION_FAILED`（1008），会走 `conclude(..., TurnStatus.FAILED)` **直接终结本轮并把卡片丢掉**（用户得从头重述整笔转账）。要对齐就必须同时补一条「验证码错误 → 重新挂卡」分支（wealth 已有，transfer 没有）。
- 为一个报告自己判定为「银行模拟器设计，未变」的口径（`REPORT_api_acceptance_local.md:275`；`REPORT_ui_acceptance_new_domains_local.md:158-163` 结论相同），去改银行校验**并**新增一条转账失败分支，属于放大范围。
- 动作：只在 `bank_backend/app/domain/risk.py:44-56` 的 docstring 里写明「任意数字码通过」是转账/生日订单的第一版口径（**纯注释，不改行为**）。

**R9（P1）** 两条提示词护栏：T01（只读账户查询不要反问）、S07（不得近似匹配商户名）。（C14 移到 §4.7；第 4 版已定案「维持现状 + 改验收期望」。）

**R10（验收侧，非产品代码）** 夹具与环境问题修复：SMTP STARTTLS、LLM 超时、网关端点、模型波动重试。

### §1.1 可选加固（**报告没有要求，默认不做**，需你点头才做）

上面 R1–R10 逐条都能追到报告的 FAIL/DEVIATE 条目或报告 §4 的环境记录。下面这些是我在写方案时顺手加的想法，**不属于任何报告条目**，为免夹带需求，单独列出：

| # | 内容 | 来源 | 默认 |
|---|---|---|---|
| A1 | 模型编造 `{"query":"*"}` 时不得被当成通配符 | 报告 §3.1 里的**证据**（不是建议） | 不做 |
| A2 | 「不要写 null / \"null\"」提示词补进 transfer 与 wealth | 三模块已有同款，属对称性补齐；T08 根因在框架层，补了也只是降低概率 | 不做 |
| A3 | 六模块 purpose × 工具覆盖审计 + 机械化测试 | 我加的；W13 只需要 wealth 补一个词 | 不做 |
| A4 | 把 card/subscription/bill 已有的三份 `_PLACEHOLDERS` 副本收敛到共享函数 | 纯去重，不改行为 | 不做 |
| A5 | transfer purpose 补「账户与余额查询」 | 我加的；T01 报告判为模型波动 | 不做 |
| A6 | 空收款人列表的新文案 | 我加的 | 不做 |
| A7 | `subscription_detail` 在 `{}` 时的行为定义 | 契约完备性，非验收要求 | **保留**（否则 R4 有歧义） |
| A8 | 单笔 `wealth_holding` 也验收产品名 | 我加的；它与列表共用同一个视图函数 | 不做 |

---

## §2 既有事实

### 2.1 两套视图与唯一收口点

| 位置 | 事实 |
|---|---|
| `backend/app/tools/schema.py:1-7` | 模块 docstring 自述：「strict mode has no notion of an optional property, so an optional one has to become a required nullable one」 |
| `backend/app/tools/schema.py:35-79` | `normalize_parameters(parameters) -> tuple[schema, can_be_strict]`；只归一化顶层；可选属性一律变成 required + `["string","null"]` |
| `backend/app/tools/gateway.py:191-193` | `arguments = dict(raw_arguments or {})` → `self._validate(spec, arguments)`（按**声明** `spec.parameters` 校验，见 `:327-344`） |
| `backend/app/tools/gateway.py:155` / `:182` / `:197-205` / `:209` / `:215` | `invoke` 收口：`raw_arguments=request.get("arguments")` → 校验 → 组 `ToolCall/ToolInvocation` → `_record_call(handle, invocation)`（记**归一化后**的实参）→ adapter |
| `backend/app/tools/gateway.py:219-253` | `_refuse` 在 `:236` 记的是 `request.get("arguments")`，即**它收到的那份参数**：对 wealth 原生工具＝模型原样；对 card/subscription/bill/transfer（模块自己先归一化过）＝模块归一化后的 |
| 结论 | 所有模块（含 wealth 原生工具）都经 `ToolGateway.invoke`，**这是唯一能一次覆盖全部工具的收口点**；归一化后「成功调用审计＝归一化后 / 被拒调用审计＝收到的原样」，这是**新增的不对称**，见 §8 |

### 2.2 四个模块的归一化实现对照（同一件事抄了四份，一份是残的、一份没有）

| 模块 | 位置 | 实现 | 实测行为（输入 `{"a":"null","b":None,"c":"","d":"none","e":0}`） |
|---|---|---|---|
| card | `backend/app/modules/card/workflow.py:65`（`_PLACEHOLDERS`）、`:68`（用于 `declared_arguments`） | 丢 None + 丢 strip().lower() 命中 `{"", "null", "none", "undefined"}` 的字符串 | `{"e": 0}` |
| subscription | `backend/app/modules/subscription/workflow.py:82`、`:169` | 同上 | `{"e": 0}` |
| bill | `backend/app/modules/bill/workflow.py:72`、`:126` | 同上 | `{"e": 0}` |
| transfer | `backend/app/modules/transfer/workflow.py:75-83` `_clean()`（`:273` 使用，`:282-283` 再用 `Draft202012Validator` 校验） | **只丢真 None**，没有 `_PLACEHOLDERS` | `{"a":"null","c":"","d":"none","e":0}` |
| wealth | 无 | **一处都没有**（`_text()` 在 `backend/app/modules/wealth/infrastructure/tools.py:124-127` 接受任意非空字符串） | 占位符会直接进业务查询（同类缺陷，验收计划未覆盖） |

`_clean()` 的 docstring（`transfer/workflow.py:76-81`）已经把机制写对了，只是实现漏了字符串占位符。`bill/tools.py:125-128` 有一段注释把「模型视图必填可空 vs 声明视图校验」讲得最清楚。

### 2.3 提示词层面的护栏也缺一份

| 模块 | 是否有「不要写 null / "null"」 | 位置 |
|---|---|---|
| card | 有 | `backend/app/modules/card/workflow.py:89` |
| subscription | 有 | `backend/app/modules/subscription/workflow.py:197` |
| transfer | **无** | `backend/app/modules/transfer/workflow.py:63-72` 通读无此句 |
| wealth | **无**（全文 0 处 `null`） | `backend/app/modules/wealth/llm/prompts.py:37` `READ_PROMPT` |

### 2.4 收款人查询（T03）两侧约束

| 位置 | 事实 |
|---|---|
| `backend/app/modules/transfer/tools.py:109-112` | `contacts()`：query 为空 → `ToolFailure(VALIDATION, "请提供要查询的收款人姓名、手机号或备注。")` |
| `backend/app/modules/transfer/tools.py:85-91` | `load_contacts()` 调 bank `GET /contacts?q=...` |
| `backend/app/modules/transfer/tools.py:294-299` | `transfer_contacts` 的 spec：`required=("query",)`、`minLength=1` |
| `bank_backend/app/api/routes.py:230-236` | `q: str = Query(min_length=1, max_length=100)` → 空 query 直接 422；`:235` 是 `search_contacts` 的**唯一**调用点 |
| `bank_backend/app/domain/account_service.py:152-154` | `search_contacts()` 自己再兜一层：`if not query or not query.strip(): raise ValidationError("q 不能为空")` |
| `backend/tests/transfer/test_transfer_tools.py:207` | `test_contacts_refuses_an_empty_query_without_calling_the_bank` **钉住了现在的拒绝行为**（断言 VALIDATION + `fake.requests == []`）→ 改行为必须同时改这条测试 |
| `bank_backend/tests/api/test_transfer_routes.py:122` | 银行侧「按姓名/手机/备注搜索」的既有测试，改 `q` 可选后必须仍通过 |
| `bank_backend/tests/api/test_transfer_routes.py:139-140` | `test_contacts_search_without_q_returns_422` **钉死了现在的 422**（断言 `GET /contacts` 无 q → 422 + 1012）→ 改行为必须同时改这条测试（第 1 版漏了它，见 §10.2） |

### 2.5 订阅详情（S02）

| 位置 | 事实 |
|---|---|
| `backend/app/modules/subscription/tools.py:64-72` | `subscription_text()`：模型可见文本只有 label/status/frequency/amount/due/renew，**没有 subscription_id**；label 形如 `"{name}（{merchant}）"` |
| `backend/app/modules/subscription/tools.py:93-111` | `detail()` 只按 `row.get("subscription_id") == wanted` 过滤：0 匹配 → `NOT_FOUND "没有找到对应的订阅。"`（`:101`）；>1 匹配 → `VALIDATION "请明确选择一笔本人订阅。"`（`:102-106`）——**读路径自己没有 0/1/many 的 park 机制**，那是写路径的事 |
| `backend/app/modules/subscription/tools.py:255-261` | `subscription_detail` 的 spec 只声明 `subscription_id`（required） |
| `backend/app/modules/subscription/tools.py:44-47` | `WRITE_PROPERTIES = {"subscription_id", "merchant_name"}`（写工具**两个选择器都支持**） |
| `backend/app/modules/subscription/tools.py:114-129` | `matched_subscriptions(rows, arguments)` 只按 `subscription_id` / `merchant_name` 精确比较（`==`），**不认 `name`** |
| `bank_backend/app/domain/subscription_service.py:42-56` | `subscription_view()` 同时返回 `subscription_id` / `name` / `merchant_name` …（数据都在） |
| 夹具 | `D:/ai_project/agent/_accept/api/fixture_bank.py:32` = `("sub_5d1e3b529d4c468aa18a","u_wealth","a_wealth_main","验收订阅","验收商户",2000,"monthly","2026-09-20","2026-10-20")` → **`name="验收订阅"`、`merchant_name="验收商户"`**；`:33` 音乐会员两个字段同名 |
| 实跑证据 | S02 里模型传的是 `subscription_id="验收订阅"`，即把**展示名**塞进了 id 参数 → 单加 `merchant_name` 修不好它 |

### 2.6 supervisor 路由（W13）与 purpose 覆盖审计

| 位置 | 事实 |
|---|---|
| `backend/app/agents/prompts.py:16-44` | `SUPERVISOR_PROMPT`；菜单由 `:108-113` `describe_agents()` 渲染成 `"- {agent_id}: {purpose}"`，handoff 工具枚举在 `:132-168` |
| `backend/app/modules/wealth/__init__.py:26` | `purpose="处理理财产品、持仓、收益、赎回相关的请求"` —— **不含「订单」，也不含「账户」「风险测评」** |
| `backend/app/modules/wealth/infrastructure/tools.py:344-351` | `wealth_order` **已注册**（`method="get_order"`，handler `_order` 在 `:243-253`，description `:346`「order_id 必须来自业务返回，不能编造」） |
| 实跑证据 | `D:/ai_project/agent/_accept/api/out/api_wealth2.json:626` supervisor 答「订单查询不在我能直接办理的范围内…」，**从未派发到 wealth** |
| purpose 文本没有测试钉住 | 全仓 grep：只有 `backend/app/modules/wealth/__init__.py:26` 与 `docs/MAIN_AGENT_PROMPTS.md:135`（该表把行号写成 `:23`，已过期）出现该串；历史 spec `docs/superpowers/specs/2026-10-12-wealth-native-tool-calling-spec.md:362` 不要改 → **改 purpose 不会撞既有断言，但要同步改 docs** |

**purpose 覆盖审计表（仅供参考 = A3 的输入；「工具」列只列面向模型的工具，内部 execute/cancel 除外）**

| 模块 | purpose 里的业务词 | 面向模型的工具 | 未覆盖 | 处置 |
|---|---|---|---|---|
| wealth | 理财产品、持仓、收益、赎回 | `wealth_products``:284`、`wealth_product``:297`、`wealth_accounts``:307`、`wealth_holdings``:315`、`wealth_holding``:326`、`wealth_risk_profile``:336`、`wealth_order``:344` | **订单**（W13）、账户/余额、风险测评 | 三条都补进 purpose（W13 必需；另两条同属「purpose 是唯一路由信号」的同类风险） |
| transfer | 转账、汇款、收款人 | `transfer_accounts`、`transfer_contacts`、`transfer_scheduled`、`transfer_prepare`、`transfer_cancel` | 账户/余额 | 补「账户与余额查询」（T01 的只读账户查询也受益） |
| card | 银行卡查询、申请、日/月交易限额、冻结、解冻、商户黑名单、挂失、解挂 | `cards`、`detail`、`restrictions`、`apply`、`update_limit`、`update_restrictions`、`freeze`、`unfreeze`、`report_loss`、`release_loss`（+内部 `execute`/`cancel`） | 无 | 不改 |
| subscription | 订阅查询、识别、续费提醒、取消/暂停/恢复/代扣 | `list`、`detail`、`cancel`、`pause`、`resume`、`charge`（+内部 `execute`） | 无 | 不改 |
| bill | 账单查询、账单分析、消费分类 | `query_transactions``:131`、`analyze_comprehensive``:159`、`send_report``:194`（+内部 `send_report_execute``:218`） | 无 | 不改 |
| life_event | 生日、节日、生活提醒 | **无工具**（`backend/app/modules/life_event/__init__.py:1-6` 自述「declares no tools in this phase」） | — | 不改 |

### 2.7 持仓产品名（W03）

| 位置 | 事实 |
|---|---|
| `bank_backend/app/domain/investment_service.py:59-76` | `holding_view()` 返回 `product_id`，**不返回** `product_name` |
| `bank_backend/app/domain/investment_service.py:720-728` | 调用方 `_holding_view(self, session, holding, now)` 在 `:721` **已经** `product = session.get(Product, holding.product_id)` —— 数据在手，只是没往视图里放；`:357` / `:372` / `:394` 都走这个包装器。注意 `product` 可能为 `None` |
| `backend/app/modules/wealth/contracts/bank.py:212-247` | agent 侧 `Holding` 是 `@dataclass(frozen=True, slots=True)`：有 `product_id`（`:216`），**没有** `product_name` 字段；`from_bank()` 在 `:231-247` 逐字段映射 |
| `backend/app/modules/wealth/contracts/bank.py:280-305` | 对照：`WealthOrder` **有** `product_name: str | None`（`:287`）→ 同一份契约里两个视图口径不一致 |
| `backend/app/modules/wealth/infrastructure/tools.py:196-204` | `_holdings` 的 summary（`:201`）是「共 N 笔持仓，当前价值合计 X。」，facts 直接 `asdict(holding)` → 没有名字可显示 |
| `backend/tests/wealth/support.py:236-253` | `holding_payload()` 造的是银行响应体，**没有 `product_name` 键**；它被 `test_read_flow.py:200`、`test_bank_adapter.py:210/444/456/522/694`、`test_safety.py` 多处复用 → 加键是一处改、多受益 |
| `backend/tests/wealth/test_bank_adapter.py:521-536` | `test_holding_fields_map_from_the_bank_payload` 里 `assert holding == Holding(...)` —— dataclass 值相等，**加字段后这条必然要改**（否则 adapter 映射出 product_name 而期望对象没有） |
| `bank_backend/tests/acceptance/test_wealth_persistence.py:214-220` | 只断言 `holding_view` 的个别键（`redeemable_minor`/`realized_income_minor`），加键是**加法**，不破 |

### 2.8 MFA 口径（T07）

| 位置 | 事实 |
|---|---|
| `bank_backend/app/domain/risk.py:44-56` | `require_confirmation()` 只查 `mfa_code.isdigit()`；docstring 自述「模拟 MFA：任意非空数字码均可通过，不校验具体值」 |
| `bank_backend/app/domain/transfer_service.py:175` | 转账走 `require_confirmation` → 错误码 `000000` 也能过 |
| `bank_backend/app/domain/card_action_service.py:139` | card 校验真实值：`code.encode() != settings.mock_mfa_code` |
| `bank_backend/app/domain/investment_service.py:661-667` | investment 同样校验 `mock_mfa_code` |
| `bank_backend/app/core/config.py:30` | 默认 `mock_mfa_code="123456"` |
| `backend/app/modules/transfer/writes.py:45-48` | agent 侧只校验「验证码是数字」（否则 `CONFIRMATION_REQUIRED`），随后把 `mfa_code` 原样发给银行 → 错的数字码目前一定能过 |
| `backend/app/modules/transfer/workflow.py:430-441` | confirm 节点只处理 `failure.code == ErrorCode.CONFIRMATION_REQUIRED`（1007）→ `upgrade_card()` 重新挂卡；**其余任何失败** → `conclude(state, failure.message, TurnStatus.FAILED)`，**本轮终结、卡片丢弃**（对比 wealth：验证码错误会重新挂卡并清空输入框） |
| `bank_backend/app/domain/birthday_service.py:125` | 生日订单也调 `require_confirmation`，但 `mfa_code=None` 硬编码 → 按 settings 校验**改不到它** |
| 结论 | 转账的 1008（`VERIFICATION_FAILED`）路径**目前不可达**；一旦把 `risk.py` 对齐 `mock_mfa_code`，错误验证码就会回 1008 → 走 `conclude(FAILED)` **丢卡**。要对齐必须**先**补一条「验证码错误 → 重新挂卡」分支（wealth 已有，transfer 没有）→ 第 4 版定案：**不对齐，维持现状**（§4.7、§8） |

### 2.9 重复挂失（C14）

| 位置 | 事实 |
|---|---|
| `bank_backend/app/domain/card_action_service.py:296-298` | `elif operation == "report_loss":` → `if card.status not in (ACTIVE, FROZEN) or card.freeze_reason == "lost": raise BusinessError("该卡不能再次挂失")` |
| `bank_backend/app/domain/card_action_service.py:181` | `card.freeze_reason = "lost" if action.operation == "report_loss" else "manual"` |
| 结论 | 重复挂失是**干净拒绝、无副作用**；强制模型调工具后用户看到的是「该卡不能再次挂失」，比模型现在的解释（「已处于已挂失状态」）更粗 → 归入「设计决策 vs 验收期望」；第 4 版定案：维持现状、只改验收期望（§4.7） |

### 2.10 基线

工作树 `acceptance-defects-round2` @ `13b1383`：补上 `backend/.env` 后全量 **1765 passed / 50 skipped / 0 failed**（第 5 版更正）。口径核算：未补 `.env` 时的读数是 1764 passed / **1 failed** / 50 skipped，那 1 failed 就是缺 `.env` 的 `tests/wealth/test_cli.py::test_default_bank_url_comes_from_settings`（补后 32 passed）；遗留探针 `backend/tests/transfer/test_probe_round2.py` **不在**那次读数里（若在其中就会是 2 failed），是之后才写下的取证文件。Task 1 后为 1773 passed / 0 failed（= 1765 + 8 条新增）。
⇒ 这 4 个 FAIL **没有任何现有测试覆盖**；本 spec 的修复只与三处既有断言冲突，三处都要同步改：§2.4 的 `backend/tests/transfer/test_transfer_tools.py:207`（拒绝空 query）、§2.4 的 `bank_backend/tests/api/test_transfer_routes.py:139-140`（无 q 返回 422）、§2.7 的 `backend/tests/wealth/test_bank_adapter.py:521-536`（`Holding` 值相等断言）。

### 2.11 验收侧环境键的准确位置（第 1 版写得太含糊）

| 项 | 事实 |
|---|---|
| SMTP | `backend/app/notification/config.py:30` 读 `SMTP_USE_SSL`（默认 `"true"` → SSL）；`backend/app/notification/email_service.py:71` 按它分流；**`backend/.env` 里没有这个键** → 要 STARTTLS 必须显式加 `SMTP_USE_SSL=false` |
| LLM 超时 | `backend/.env:11` `LLM_TIMEOUT_SECONDS=30`；`backend/app/core/config.py:40` `llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)` → 上限 120，改 60/90 都在范围内 |
| 网关 | `backend/.env:7` `LLM_BASE_URL=https://llm.example.com/v1`（当天 503）→ 换 `https://llm.example.com/v1`（报告用的可用端点） |
| `available_minor` 漂移 | **已经修好**：`D:/ai_project/agent/_accept/api/fixture_bank.py:90-91` 与 `cases_wealth.py:40-46` 都同时写 `balance_minor` 与 `available_minor`（后者带注释解释原因）。**只剩文档**还是单字段：`TEST_PLAN_card_wealth_chat.md:318`、`REPORT_ui_acceptance_new_domains_local.md:200-201` |

---

## §3 设计决定

**D1 归一化收口在网关，不在模块。**
在 `backend/app/tools/gateway.py:191-193` 之前插入一次 `normalize_arguments()`，然后 `_validate` 按声明视图校验。
- 理由：网关是唯一覆盖全部工具（含 wealth 原生工具）的收口点；现有四份手抄实现正好证明了「放在模块里」必然漏（wealth 就是漏的那份）。
- 被否决 A：继续在各模块补一份 —— 就是现在这个状态，第五次抄写不会让第六个模块不出问题。**报告 §3.2 自己给的 T08 建议正是这个被否决的方案**（「transfer/workflow.py 引入同样的 _PLACEHOLDERS 过滤（约三行改动）」）。
- **范围提示**：本次**必须做**的只有两处——网关收口（一次覆盖全部工具，含 wealth 的只读工具）+ transfer 自己那道重复校验（D4）；把 card/subscription/bill 已有的三份副本收敛到共享函数属于**去重**（A4），不做也不影响 T08/T02。
- 被否决 B：改 `normalize_parameters` 让可选参数真的可选 —— strict 模式要求所有属性都出现在 `required`，provider 会直接拒收 schema（`schema.py:5-6` 已写明）；「必填但可空」是 strict 的代价，代价本身没错，错的是回程没有归一化。

**D2 归一化语义沿用三模块既有实现，不发明新语义。**
`_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})`；丢弃规则：`value is None` → 丢；`isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS` → 丢；其余原样保留（`0`、`False`、`[]`、`{}` 必须保留）。
- 理由：语义已经跑了三个模块，改动语义等于同时改三个模块的行为，风险无收益。
- 注意：transfer 现状**保留** `""`/`"none"`，收敛后它的行为**会变**——这正是 T02/T08 的修复目标，不是回归（§4.1 第 4 条据此改写）。

**D3 抽成独立模块 `backend/app/tools/normalize.py`，`schema.py` 与 transfer workflow 都 import 它。**
- 理由：`schema.py` 是「声明 → 模型视图」的方向，归一化是反方向；放同一个模块会让「谁负责哪个方向」变模糊。
- 备选（可接受）：直接放在 `schema.py` 里并加注释——评审若倾向少一个文件，这条可以改，不影响其它决定。

**D4 transfer 的重复校验必须改用同一函数（不能只改网关）。**
`backend/app/modules/transfer/workflow.py:273` 的 `_clean()` 与 `:282-283` 的 `Draft202012Validator(declared.parameters).is_valid()` 在网关**之前**执行；只改网关，T02 仍然坏（枚举看到字符串 `"null"` 直接拒）。
- 理由：这一层是 transfer 独有的重复校验，必须和网关用同一套归一化，否则两个视图继续打架。

**D5 T03 两侧都改。**
bank 侧 `q` 变可选、空则返回全部；agent 侧 `transfer_contacts` 的 `query` 变可选（去掉 `minLength`），handler 空 query 直接列全部。
- 理由：只改 agent，bank 仍会 422；只改 bank，agent 的 spec 仍会先拒。`search_contacts` 只有一个调用点（`bank_backend/app/api/routes.py:235`），改动面小。
- 被否决：让 agent 换个接口（如 `list_birthdays`）绕开 —— 语义不对（只返回有生日的人），是更大的错误。

**D6 W13 只改 wealth 的 `purpose`，不改 supervisor 代码。**
wealth `purpose` 补「订单查询」，保留现有业务词；数据改了就把 `docs/MAIN_AGENT_PROMPTS.md:135` 同步（不然后来者照抄过期文本）。**「账户余额 / 风险测评」与 transfer 的账户词属 A3/A5，默认不做。**
- 理由：supervisor 的菜单与 handoff 枚举都由 `purpose` 派生（`prompts.py:108-113`、`:132-168`），改数据即可；改 supervisor 逻辑属于无根据的扩大改动。第 1 版说「当前 wealth 是唯一对不上的」是**错的**（§2.6 审计表）。
- 风险与边界：purpose 变长会让路由更"贪"，所以只加**用户会说出来的业务名词**（订单、账户余额、风险测评），不加泛词。

**D7 W03 两层数据 + 一层展示，summary 不动。**
bank `holding_view()` 返回 `product_name`（Product 已在 `:720-728` 加载，注意 `None` 容错）→ agent `Holding` 契约加 `product_name: str | None`（与 `WealthOrder` 对齐，**加在 dataclass 末尾带默认值**，否则所有位置参数构造点全炸）→ `_holdings` 的 facts 通过 `asdict(holding)` 自然带上。
- 理由：W03 的偏差是「持仓的产品列显示 `p_demo_cash`」＝ UI 渲染 facts；facts 有名字就够。summary 是聚合句，改它没有验收依据，且会牵动更多断言。
- 第 1 版写的「summary 里出现产品名」据此**撤销**。

**D8 S02 让 `subscription_detail` 接受三个选择器：`subscription_id` / `name` / `merchant_name`，不暴露内部 id 到模型可见文本。**
- 理由：模型手上的名称可能是**展示名**（`name`）也可能是**商户名**（`merchant_name`）——S02 实跑传的就是展示名；只加 `merchant_name` 修不好（§2.5）。读路径没有 park 机制，所以 detail 自己按「0 → NOT_FOUND、>1 → VALIDATION」处理。
- 被否决 A：只在 `subscription_text()` 里加 `subscription_id` —— 会把内部标识推给模型，且与写工具口径仍不一致。
- 被否决 B：第 1 版写的「复用 `matched_subscriptions()` 的候选选择机制」——读工具根本没有那个机制（它属于写路径），照抄会把 `>1` 的处理写错。

**D9 T01/S07 只改提示词，不改代码分支。**
- 理由：两者都不是代码缺陷（§0 定性），代码层加分支会把「模型波动」固化成规则；提示词是这类问题唯一的正当位置。
- C14 从本条**移出**（见 D10）——强制调工具会让文案更差。

**D10 四条曾待拍板项已定案：全部维持现状，不新增代码改动（第 4 版）。**
T07（转账 MFA 不对齐）、W01（接受网关 tool_call 设计）、C18（低于起转金额维持交互纠正）、C14（重复挂失维持现状）。定案理由见 §4.7 与 §10.1。这四条本来就不在 §11.4 的产品代码改动清单里，因此**本次实施范围不变**。

---

## §4 分项修复

### 4.1 T08 / T02 —— 占位符归一化（R1、R2、D1–D4）

改动点：
1. 新建 `backend/app/tools/normalize.py`：

```python
_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})

def normalize_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop the placeholders strict mode makes the model send for absent parameters."""
    normalized: dict[str, Any] = {}
    for name, value in (arguments or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS:
            continue
        normalized[name] = value
    return normalized
```

2. `backend/app/tools/gateway.py`：`:191` 处改为 `arguments = normalize_arguments(raw_arguments)`，保持 `:193` 的 `self._validate(spec, arguments)` 在其后（校验的是归一化后的实参）。
3. `backend/app/modules/transfer/workflow.py`：`:273` 改用 `normalize_arguments()`，删掉 `_clean()`（全仓只有 `:75` 定义与 `:273` 使用两处）。
4. **（可选 = A4，不做不影响 T08/T02）** 收敛副本：`card/workflow.py:65+68`、`subscription/workflow.py:82+169`、`bill/workflow.py:72+126` 的 `_PLACEHOLDERS`/`declared_arguments` 改为调用共享函数。做了的话 **card/subscription/bill 的行为必须逐字不变**；transfer 的行为按修复目标变化（从「只丢 None」变成「丢 None + 字符串占位符」）。
5. **（可选 = A2）** 提示词补一句（与 card `:89` 同款）：transfer `workflow.py:63-72`、wealth `llm/prompts.py:37`。T08 的根因在框架层，提示词只是降低概率，不做也能修好。

验收：`transfer_prepare` 带字符串占位符 → `INSUFFICIENT_BALANCE`（R2）；`transfer_scheduled` 带 `status="null"` → 不再报「参数不完整」（T02）；wealth 只读工具同样被覆盖（网关收口的直接结果）。（「四个模块对照表逐字一致」只在做 A4 时才需要。）

### 4.2 T03 —— 收款人列表（R3、D5）

改动点：
1. `bank_backend/app/api/routes.py:230-236`：`q: str | None = Query(default=None, max_length=100)`。
2. `bank_backend/app/domain/account_service.py:152-170`：`search_contacts(user_id, query=None)`——空 query 时不加 `ilike` 条件，返回该用户全部联系人（沿用现有 `order_by(Contact.id)`）。
3. `backend/app/modules/transfer/tools.py:294-299`：`transfer_contacts` 的 `query` 变可选（去掉 `required` 与 `minLength`）。
4. `backend/app/modules/transfer/tools.py:109-112`：空 query 不再抛错，改为不带 `q` 调 `load_contacts`。（空结果沿用现有文案「没有找到匹配的联系人。」即可，**不新增文案要求** = A6。）
5. `backend/tests/transfer/test_transfer_tools.py:207`：把「拒绝空 query」改成「空 query 列出全部」，断言 `fake.requests` 里是**不带 q** 的 `GET /contacts`。
6. `bank_backend/tests/api/test_transfer_routes.py:139-140`：把 `test_contacts_search_without_q_returns_422` **整条替换**成「无 q 返回全部」（断言 `GET /contacts` 无 q → 200 + 全量）；不能与旧断言并存，否则 bank 套件 1 failed。

验收：`transfer_contacts({})` 列出全部；bank `GET /contacts`（无 q）200 + 全量；带 q 的既有搜索行为不变（`bank_backend/tests/api/test_transfer_routes.py:122` 仍绿，`:139-140` 改成「无 q 返回全部」）。

### 4.3 S02 —— 订阅详情按 id / 展示名 / 商户名（R4、D8）

改动点：
1. `backend/app/modules/subscription/tools.py:33-40`：新增 `SUBSCRIPTION_NAME_PROPERTY`（description「订阅名称，来自查询结果里的 name」），与既有 `SUBSCRIPTION_ID_PROPERTY` / `MERCHANT_NAME_PROPERTY` 并列。
2. `backend/app/modules/subscription/tools.py:255-261`：`subscription_detail` 的 spec 改成三个**可选**选择器 `subscription_id` / `name` / `merchant_name`（去掉 `required`）。**不加 `anyOf`**（第 5 版定死）：`spec()` 的 `constraints` 虽然能塞 `anyOf`（`card/tools.py:208` 就是那么用的），但加了以后 `subscription_detail({})` 会在网关的**声明视图**被直接拒成 `TOOL_DENIED`，与 R4 要求的 `VALIDATION`「请明确选择一笔本人订阅。」冲突——所以三个属性都可选、由 `detail()` 自己判 0/1/many。
3. `backend/app/modules/subscription/tools.py:93-111`：`detail()` 改为三选择器过滤（每个给了的选择器都要命中对应字段：`subscription_id` → `row["subscription_id"]`，`name` → `row["name"]`，`merchant_name` → `row["merchant_name"]`）；0 匹配 → 保持 `NOT_FOUND "没有找到对应的订阅。"`；>1 匹配 → 保持 `VALIDATION "请明确选择一笔本人订阅。"`。
4. 不改写工具的选择器集合（`WRITE_PROPERTIES`，`tools.py:44-47`）——本需求只对齐读工具。
5. `backend/app/modules/subscription/workflow.py:194`（**第 5 版之后的控制器补充**，见 §10.4）：提示词现在写「要看某一笔的详情用 `subscription_detail`，并给出查询结果里的 `subscription_id`」，但 `subscription_text()`（`tools.py:64-72`）渲染的列表文本只有「名称（商户名）：状态，频率扣款 金额，下次扣款 日期，续费方式。」——**根本没有 `subscription_id`**。这正是 S02 里模型把展示名「验收订阅」塞进 `subscription_id` 的直接原因：提示词要求它给出一个查询结果里不存在的字段。改成「…要看某一笔的详情用 `subscription_detail`，用查询结果里的订阅名称或商户名定位（原样照抄）」。

验收：`{"name":"验收订阅"}`、`{"merchant_name":"验收商户"}`、`{"subscription_id":"sub_5d1e3b529d4c468aa18a"}` 三种都返回同一笔详情；`{}` → `VALIDATION`（>1 匹配时）或返回唯一一笔；既有 `subscription_id` 行为不变。

### 4.4 W13 —— 订单路由（R5、D6）

改动点：
1. `backend/app/modules/wealth/__init__.py:26`：purpose 补「订单查询」，其余业务词不动（最小改动）。
2. `docs/MAIN_AGENT_PROMPTS.md:135`：同步新的 wealth purpose（该表还把行号写成 `:23`，一并更正）。
3. `wealth_order` 已存在，**不要**新建工具（报告 §3.4 的「加工具」建议基于错误前提）。

验收：确定性——`describe_agents()` 菜单里 wealth 那一行含「订单」（Task 5 的测试）；端到端——重跑 W13，模型实际派发到 wealth 并调用 `wealth_order`（Task 9）。（A3 的六模块审计与测试、A5 的 transfer purpose 默认不做。）

### 4.5 W03 —— 持仓产品名（R6、D7）

改动点：
1. `bank_backend/app/domain/investment_service.py:59-76`：`holding_view()` 增加 `product_name` 参数/输出；`_holding_view()`（`:720-728`）把已加载的 `product.name` 传进去（`product is None` 时给 `None`）。
2. `backend/app/modules/wealth/contracts/bank.py:212-247`：`Holding` **在末尾**加 `product_name: str | None = None`，`from_bank()` 里 `product_name=payload.get("product_name")`（`WealthOrder` `:287` 同款）。
3. `backend/app/modules/wealth/infrastructure/tools.py:196-204`：`_holdings` 的 facts 走 `asdict(holding)` 自动带上；summary **不改**。
4. 测试同步：`backend/tests/wealth/support.py:236-253` `holding_payload()` 加 `"product_name": "演示灵活理财"`；`backend/tests/wealth/test_bank_adapter.py:521-536` 的期望 `Holding(...)` 加同字段。

验收：`wealth_holdings` 的每条 facts 含非空 `product_name`；UI 的产品列不再只有 `p_demo_cash`。（单笔 `wealth_holding` 共用同一个视图函数，自然带上，不另立验收项 = A8。）

### 4.6 提示词护栏（R9、D9）

| 编号 | 位置 | 追加内容（要点） |
|---|---|---|
| T01 | `backend/app/modules/transfer/workflow.py:63-72` | 用户只是查询账户/余额/收款人时，**直接调用** `transfer_accounts`/`transfer_contacts`，不要反问、不要进入转账准备 |
| S07 | `backend/app/modules/subscription/workflow.py:191-200`（`:194` 是 detail 段） | 商户名/订阅名必须与列表返回**完全一致**才能选中；不确定就说明没找到，不得近似匹配 |

验收：重跑 `run_api.py` 对应组，T01/S07 转 PASS（模型波动项允许重试后判定）。

### 4.7 四条曾待拍板项（第 4 版已定案：全部维持现状）

定案依据：用户 m00386「刚刚那些我没回答的，按你推荐的做法做，我的意思是不要把整个项目越补越乱」。四项都属于「验收期望 vs 设计意图」这一类，改代码的收益都低于它带来的连带风险，因此**一律不改产品行为**，只做注释/文档同步。

| 条目 | 定案 | 理由（关键证据） | 动作 |
|---|---|---|---|
| **T07** 转账 MFA | **不对齐**，维持「任意数字码通过」 | 只改 `bank_backend/app/domain/risk.py:44-56` 会让错误验证码回 `VERIFICATION_FAILED`(1008)，而 `backend/app/modules/transfer/workflow.py:430-441` 只处理 `CONFIRMATION_REQUIRED`(1007) → `conclude(..., FAILED)` **终结本轮并丢卡**；要对齐就必须额外补一条「验证码错误 → 重新挂卡」分支（wealth 已有，transfer 没有） | `risk.py:44-56` docstring 写明该口径（纯注释） |
| **W01** 理财只读 tool_call | **接受现状** | 模块带工具进场是有意设计，`backend/app/modules/wealth/__init__.py:1-13` 有记录 | 验收计划期望改为「理财只读轮次允许出现 tool_call」 |
| **C18** 低于起转金额 | **接受现状**（交互纠正卡） | 交互纠正是有意设计，上限分支本来就符合期望 | 验收计划 C18 期望 FAIL → DEVIATE |
| **C14** 重复挂失 | **接受现状**（模型如实解释） | 银行对重复挂失只回「该卡不能再次挂失」（`bank_backend/app/domain/card_action_service.py:296-298`），**干净拒绝、无副作用**；强制调工具反而把文案变粗 | 验收计划期望改为「允许模型直接如实解释」 |

> 这四条都**不在** §11.4 的产品代码改动清单里，定案后本次实施范围与第 3 版一致。
> 若日后确要对齐 T07：必须**先**补 transfer 的 `VERIFICATION_FAILED` 重新挂卡分支、**再**改校验，顺序反了就会造出一条死路（见 §8）。

### 4.8 验收侧夹具/环境（R10，非产品代码）

`D:/ai_project/agent/_accept/` 下：
1. `SMTP_USE_SSL=false` 写进 `backend/.env`（键当前不存在，默认 SSL 与 2525 STARTTLS sink 冲突）。
2. `backend/.env:11` `LLM_TIMEOUT_SECONDS=30` → 60（上限 120，见 `backend/app/core/config.py:40`）。
3. `backend/.env:7` `LLM_BASE_URL` 从 503 的 `https://llm.example.com/v1` 换成 `https://llm.example.com/v1`。
4. 对「模型波动」类失败**手工重跑一次并单独分类**（S04/C05/T01 各出现过一次假红）；**不改 `run_api.py` 的分类逻辑**，重跑与记录见实施计划 Task 9 Step 4。
5. **（可选，实施计划 Task 8 Step 5）** 文档同步（避免后来者照抄错 SQL）：`TEST_PLAN_card_wealth_chat.md:318`、`REPORT_ui_acceptance_new_domains_local.md:200-201` 的复位 SQL 只改 `balance_minor`；api 侧夹具**已经**是双字段（§2.11），无需再改代码。

---

## §5 测试

新建：
- `backend/tests/test_tool_argument_normalization.py`：网关级——按某个已声明 spec 传字符串占位符被丢弃、`None` 被丢弃、`0/False/[]/{}` 被保留、wealth 原生工具同样被覆盖、**被拒调用记录的是它收到的原样参数**（`gateway.py:236`）、成功调用记录的是归一化后的（`gateway.py:209`）。
- **（可选 = A3）** `backend/tests/test_module_purpose_covers_tools.py`：`PURPOSE_KEYWORDS = {agent_id: {tool_name: ("必须出现的业务词", ...)}}` 显式表 + 遍历模块注册表断言；排除内部工具（`execute` / `send_report_execute`）与无工具模块（life_event）。**只有采纳 R7 才建。**
- bank 侧：`bank_backend/tests/api/test_transfer_routes.py` 加「无 q 返回全部」；`bank_backend/tests/domain/test_investment_service.py` 加「`holding_view` 带 `product_name`」。

修改：
- `backend/tests/transfer/test_transfer_tools.py:207`：拒绝空 query → 列出全部（**必须改**，否则与 T03 修复直接冲突）。
- `bank_backend/tests/api/test_transfer_routes.py:139-140`：无 q 返回 422 → 无 q 返回全部（**必须改**，否则 bank 套件 1 failed）。
- `backend/tests/transfer/test_transfer_tools.py`：加「字符串占位符不会导致 NOT_FOUND、会走到余额校验」（钉住 T08）——**必须走真实边界**（`build_registry` + `BankToolAdapter` + `ToolGateway`）：修复在网关，直调 `prepare()` 看不到它，且 `prepare()` 无条件 `GET /accounts` 会让原稿的 `assert fake.requests == []` 永远不可能成立（第 5 版修订，已落地于 545b1c6）；加「`status="null"` 不再报参数不完整」（钉住 T02）。
- `backend/tests/test_subscription_module.py`：加 detail-by-name 与 detail-by-merchant_name（S02）；若 `backend/tests/test_subscription_live_bank.py` 断言了 detail 的 spec 形状，同步更新。
- `backend/tests/test_supervisor_handoff.py` 或 `backend/tests/test_domain_agents.py`：加「订单 → wealth」路由（W13）。
- `backend/tests/wealth/support.py:236-253`：`holding_payload()` 加 `product_name`。
- `backend/tests/wealth/test_bank_adapter.py:521-536`：期望 `Holding(...)` 加 `product_name`。
- `backend/tests/wealth/test_read_flow.py`（`:200` 一带）：断言 `wealth_holdings` 的 facts 带 `product_name`。
- **（可选 = A4）** 四个模块的归一化对照测试：同一输入在 card/subscription/bill/transfer 得到相同结果（防收敛时行为漂移）。

保持不变：
- 全量 passed 不得减少：补 `.env` 后的真基线是 **1765 passed / 50 skipped / 0 failed**（第 5 版更正；原 1764 那次读数**不含**探针，那 1 failed 是缺 `.env`），加上本计划新增的测试后必须 ≥ 1765 + 新增数（`.env` 必须在位，见 §2.10）。

---

## §6 范围边界

不做：
1. 不新建 `wealth_order` 工具（它已存在，报告 §3.4 的说法需要更正）。
2. 不改 supervisor 的提示词结构或 handoff 逻辑（只改模块 `purpose` 数据）。
3. 不引入模糊匹配（S07 的代码路径本来就没有模糊匹配，加它才是引入缺陷）。
4. 不改 W01 的架构、不改 C18 的交互设计、不改 C14 的当前行为、不对齐 T07 的 MFA 口径（D10，已定案维持现状）。
5. 不在归一化里处理嵌套结构（只做顶层，与 `normalize_parameters` 的既有约定一致）。
6. 不改 `_holdings` 的 summary 文案（D7）。
7. 不动 `D:/ai_project/agent/_accept/` 之外与本次缺陷无关的验收脚本逻辑（§4.8 只做环境/夹具修正）。
8. **不夹带 §1.1 的可选加固**（除非你点头）：六模块 purpose 审计与测试（A3）、transfer/wealth 的「不要写 null」提示词（A2）、`*` 的通配符语义（A1）、空列表文案（A6）、三份副本去重（A4）、transfer purpose 补词（A5）、单笔持仓验收项（A8）。

---

## §7 与验收报告的差异

1. **W13 的根因写错**：报告 §3.4（report lines 236-239）说「根因：没有『按订单号查订单详情』的工具/能力」，并建议「给 wealth 域加订单查询只读工具」；实际 `wealth_order` 已注册（`wealth/infrastructure/tools.py:344-351`）。真因是 supervisor 按 `purpose` 路由、而 wealth 的 purpose 不含「订单」。**建议同时更正报告**，否则后来者会重复实现一个已存在的工具。
2. **T08 的修法**：报告 §3.2（report lines 219-225）建议「transfer/workflow.py 引入同样的 `_PLACEHOLDERS` 过滤（约三行改动）」——本 spec 否决（D1）：那是第五份手抄，wealth 那份至今没有；收口必须在网关。
3. **W03 比报告更深**：报告只说「展示层缺产品名」，实际 bank 视图、agent 契约两层都没有这个字段。
4. **S02 比报告更深**：报告只说「模型把展示名当 id」，修法不能只加 `merchant_name`——夹具里 `name="验收订阅"`、`merchant_name="验收商户"` 是两个字段（§2.5）。
5. **S07 不是代码缺陷**：报告写「被近似匹配」，实际是 `==` 精确比较，模型自己做了映射。
6. **T07 不是 agent 缺陷**：是银行 mock 内部口径不一致。
7. **T01 定性为模型波动**（提示词护栏处理），不是确定性代码缺陷；**C14 定性为「设计决策 vs 验收期望」**（§2.9），报告对它的「结果正确且无副作用」判断成立。
8. **T03 的假否定**：报告 §3.1 记录模型曾编 `{"query":"*"}` → 银行返回空 → 子任务 SUCCEEDED 但摘要「没有找到匹配的联系人。」；本 spec 的 R3 把这条纳入验收（`*` 不得被当通配符）。
9. 报告 §4 列的环境/夹具问题属于验收侧，不在产品代码修复范围（§4.8 单列）；其中 `available_minor` 漂移**在 api 夹具里已经修好**，只剩文档。

---

## §8 风险与回滚

| 风险 | 说明 | 缓解 |
|---|---|---|
| 归一化误伤合法值 | 若某工具真要接受字面量 `"null"`/`"none"` 作为**业务值**，会被丢弃 | 逐一核对过：transfer 的可选字符串是 `from_hint`/`from_account_id`/`to_account_id`/`remark` + 枚举 `status`；wealth 唯一的可选参数是枚举 `risk_level`；card/subscription/bill 已按同语义跑了很久。新增测试钉住「只丢完全相等的占位符，`"Null 先生"` 这类不丢」 |
| 归一化改变审计内容（不对称） | 成功调用审计记归一化后的实参（`gateway.py:209`）；被拒调用审计记 `request` 里的原样参数（`:236`），对 wealth 是模型原样、对四个模块是模块归一化后的 | **有意保留**（被拒时看得到模型真正发了什么）；在网关级测试里显式钉住这个不对称，避免后来者误改 |
| 收敛副本时行为漂移 | card/subscription/bill 与 transfer 的现状语义不同（transfer 保留 `""`/`"none"`） | 对照测试逐字比对四种输入；明确 transfer 的变化是修复目标 |
| T03 放宽后返回过多 | 用户无条件下列出全部收款人 | 沿用既有排序；夹具量级很小；如担心可加数量上限（本 spec 暂不加） |
| purpose 变长导致路由更贪 | 多补了「账户余额/风险测评」两个业务词 | 只加用户会说出来的业务名词，不加泛词；靠 §5 的 purpose 测试与重跑验收观察 |
| `Holding` 加字段的位置 | `frozen=True, slots=True` 的 dataclass，字段顺序即构造签名 | 新字段**加在末尾且带默认值**；`test_bank_adapter.py:521-536` 的值相等断言同步更新 |
| T07 日后若要对齐 MFA | **顺序错就造死路**：只改 `risk.py` 的校验，错误验证码会回 1008，而 `transfer/workflow.py:430-441` 只处理 1007 → `conclude(..., FAILED)` 终结本轮并丢卡 | 必须**先**补 transfer 的 `VERIFICATION_FAILED` 重新挂卡分支、**再**改校验；本次不做，故当前无此风险 |
| 提示词护栏无效 | 提示词不是硬保证（T01 本就是波动） | 护栏只是降低概率；真值由重跑验收确认 |

回滚：D1–D4 的改动集中在 `normalize.py` + `gateway.py:191` 一处 + 四个模块的收敛点，单点 revert 即可；T03/W03 是跨仓两处小改；T07/W01/C18/C14 已定案维持现状，本次不动。

---

## §9 实施顺序

1. P0 框架层：4.1（R1/R2/T02）→ 跑 §5 新测试 + transfer 全量。
2. P0 产品代码：4.2（T03）、4.3（S02）、4.4（W13）、4.5（W03）。
3. P1 护栏：4.6。
4. P1 验收侧：4.8（夹具/环境）。
5. §4.7 四条已定案「维持现状」，**无代码步骤**：只做验收侧文档同步（C14/C18/W01 的期望口径）与 `risk.py` 的口径注释。
6. 全量回归 + 重跑 `D:/ai_project/agent/_accept/api/run_api.py` 的 card/wealth/transfer/subscription/bill 五组，比对 64 PASS/4 FAIL/7 DEVIATE 的基线。

---

## §10 修订记录（第 1 版 → 第 2 版）

自审（用户要求「审查一遍有没有漏洞或表意不清」）发现并修正：

| # | 第 1 版的漏洞/歧义 | 第 2 版的修正 |
|---|---|---|
| 1 | **S02 修不好**：D8 只加 `merchant_name`，但 S02 实跑传的 `subscription_id="验收订阅"` 是夹具的 `name` 字段（`merchant_name` 是「验收商户」） | D8/§4.3/R4 改为三个选择器（id / name / merchant_name），并给出夹具字段事实（§2.5） |
| 2 | **§4.3 描述错误**：写「复用 `matched_subscriptions()` 的候选选择机制」——读工具没有那个机制（它属于写路径） | §4.3 改成 detail 自己处理 0（NOT_FOUND）/ >1（VALIDATION） |
| 3 | **§8 审计描述不准**：说归一化会改变 `_refuse` 记录的内容 | §2.1/§8 改为「成功调用记归一化后、被拒调用记它收到的原样」的不对称，并说明模块路径与 wealth 路径的差别；§5 的断言同步改正 |
| 4 | **R7 不可机械化**：「purpose 里找到对应的业务词」无法写成测试 | R7/§5 改为显式 tool→关键词映射表；§4.4 给出审计表（第 3 版进一步把 R7 整体降为**可选 A3**——报告并没有要求它） |
| 5 | **D6/§4.4 判断错误**：说「当前只有 wealth 对不上」，漏了 wealth 的账户/风险测评与 transfer 的账户 | §2.6 审计表列出全部缺口（第 3 版把 D6 **收窄回「wealth 只补订单」**，其余列为可选 A3/A5，见 §11） |
| 6 | **C14 处置会降低文案质量**：§4.6 要求「已挂失仍要调工具」，而银行只会回「该卡不能再次挂失」 | C14 从 D9/§4.6 移到 §4.7（当时标注为待拍板，**第 4 版已定案为维持现状**），推荐「接受现状 + 改验收期望」；新增 §2.9 证据 |
| 7 | **§4.8 让后来者做已完成的事**：要求改夹具的 `available_minor` 漂移，实际 api 夹具已经修好（`fixture_bank.py:90-91`、`cases_wealth.py:40-46`） | §2.11 记录事实；§4.8 只保留文档同步，并把 SMTP/超时/网关落到确切键位（`backend/.env:7/:11`、`notification/config.py:30`） |
| 8 | **§5 测试没有落到文件**：W03 只写「`test_bank_contract.py` 等」 | §5 逐条点名文件与行号，含 `support.py:236`、`test_bank_adapter.py:521-536` 这两处**必须改**的测试 |
| 9 | **§4.1 第 4 条「逐字不变」自相矛盾**：transfer 收敛后行为本来就要变 | §4.1 第 4 条改为「card/subscription/bill 逐字不变，transfer 按修复目标变化」；D2 补一句 |
| 10 | **D7 越界**：要求 `_holdings` 的 summary 带产品名，而 W03 的偏差是 UI 的产品列（facts） | D7/§4.5 收敛为 facts 带名字、summary 不动；§6 加一条边界 |
| 11 | **R1 验收例子会先被拒**：往 spec 塞未声明属性会撞 `additionalProperties=False` | R1 的验收改为「对一个已声明可选字符串属性的 spec」 |
| 12 | **§7 缺报告自身的修法对照**：没说报告 §3.2 的建议正是被否决的方案 | §7 第 2 条补上；同时补 T03 的 `{"query":"*"}` 假否定细节 |

### §10.1 第 3 版 → 第 4 版（2026-10-16）：四条待拍板项定案

用户 m00386：「刚刚那些我没回答的，按你推荐的做法做，我的意思是不要把整个项目越补越乱」。据此把 §4.7 的四条定案如下，**全部维持现状、不新增任何代码改动**：

| # | 条目 | 定案 | 与第 3 版相比 |
|---|---|---|---|
| 13 | T07 转账 MFA | **不对齐**（第 3 版我的推荐是「对齐」，复核后自行推翻） | 只改 `risk.py` 会让错误验证码终结本轮并丢卡（`transfer/workflow.py:430-441` 只处理 1007）；对齐需额外补一条重新挂卡分支 → 范围过大。报告 `:275` 本就钉为 `DEVIATE` |
| 14 | W01 理财只读 tool_call | **接受现状**，只改验收文档 | 与第 3 版推荐一致 |
| 15 | C18 低于起转金额 | **接受现状**，验收期望 FAIL → DEVIATE | 与第 3 版推荐一致 |
| 16 | C14 重复挂失 | **接受现状**，验收期望改为「允许模型直接如实解释」 | 与第 3 版推荐一致 |

连带修改：§0 表中这四行的定性加「定案」标注；§1 R8 由「待拍板」改为「不做代码改动」并写明否决理由；§3 D10 由「不阻塞 P0，等拍板」改为「已定案维持现状」；§6 边界、§8 风险/回滚、§9 步骤 5、§11.1 的 R8 行同步。
**产品代码改动清单（§11.4，11 个文件）不变** —— 这四条本来就不在里面。（注：第 5 版之后因 §10.4 第 24 条的提示词修正，清单变为 **12 个**，见 §11.4。）

### §10.2 第 4 版 → 第 5 版（2026-10-16）：spec ↔ plan 对齐审计

用户 m00730：「重审两份文档是否对齐」。逐条核对实施计划 `docs/superpowers/plans/2026-10-16-api-acceptance-defect-fixes.md`（807 行、9 个 Task）后修正。**5 条全是「两文档口径不一致」或「漏了会被跑红的既有断言」，没有新增需求、没有扩大范围**：

| # | 发现 | 修正 |
|---|---|---|
| 17 | **bank 侧还有一条被钉死的断言**：`bank_backend/tests/api/test_transfer_routes.py:139-140` 的 `test_contacts_search_without_q_returns_422` 断言「无 q → 422」，Task 3 改完必红；§2.4 只点了 `:122`，§2.10 还断言「不会与既有断言冲突」 | §2.4 补一行、§2.10 改成「三处冲突」、§4.2 加改动点 6、§5 加进「必须改」清单、§11.4 的测试计数改为 3 处 |
| 18 | **R5 的验收在确定性测试里无法证明**：既有 `test_supervisor_handoff.py` 夹具只驱动 `parse_decision`（12 个解析层测试），不跑真实模型，没有任何确定性测试能证明「模型会选 wealth」 | R5/§4.4 的验收拆成「菜单文本（确定性，Task 5）」+「重跑 W13（端到端，Task 9）」 |
| 19 | **§4.3 的 `anyOf` 建议与计划的定死相反**：计划明确不加 `anyOf`（加了 `subscription_detail({})` 会被声明视图拒成 `TOOL_DENIED`，与 R4 的 VALIDATION 冲突），而 §4.3 原文写「若支持就加」 | §4.3 改动点 2 改为「**不加 anyOf**」并写明原因；计划的引用同步改为「§4.3 改动点 2」 |
| 20 | **§4.8 的 item 4/5 在计划里没有对应步骤** | item 4 明确为「手工重跑一次并单独分类」（计划 Task 9 Step 4）；item 5 标注「可选，计划 Task 8 Step 5」 |
| 21 | **§5 的「1764 passed 不得减少」口径漂移**：当时的判断是「1764 含遗留探针、真基线 1759」——**该判断本身是错的**（见 §10.3） | §2.10 与 §5 先改成「≥ 1759 + 新增数」，Task 1 落地后按实测更正为「≥ 1765 + 新增数」 |

计划侧同步修正（详见计划文件末尾「对齐审计修订」一节）：2 个 blocker（上面 #17、以及 Task 6 Step 1 把持仓断言写成 `p_demo_cash` 而 `_purchase()` 默认返回 `p_yuebao`）＋ 5 个 minor（R2 无单测、R5 只被菜单文本代理、`0/False/[]/{}` 只覆盖 `0`、§4.8 item 4/5 未落地、`grep _clean` 检查做不到）＋ 3 个 cosmetic（§4.3.2/§7.1 两个不存在的引用、探针条数与基线口径）。

### §10.3 第 5 版之后的实测更正（2026-10-16，Task 1 落地后）

两处**由实测数据推翻的第 5 版判断**，已同步改回：

| # | 第 5 版的判断 | 实测更正 | 依据 |
|---|---|---|---|
| 22 | 基线口径：1764 **含**遗留探针的 5 条通过用例，删探针后真基线 **1759** | 1764 **不含**探针；真基线是补 `.env` 后的 **1765** | Task 1 落地后全量 = 1773 passed / 50 skipped = 收集 1823 条；新增测试恰 8 条（`git diff -U0` 里 `+def test_` 计数），1823 − 8 = 1815 = 1764 + 1 failed + 50 skipped，说明那次读数里没有探针（有就会是 2 failed）。探针是那次基线之后才写下的取证文件 |
| 23 | Task 1 Step 2(a) 的 T08 单测「直调 `prepare()`，并断言 `fake.requests == []`」 | 必须**走真实边界**（`build_registry` + `BankToolAdapter` + `ToolGateway`）；那句断言**修前修后都不可能成立** | 修复在网关（本 spec D1 否决在模块里补归一化），直调测不到被修的那层；且 `prepare()` 无条件 `load_accounts()`（`backend/app/modules/transfer/tools.py:184` → `:79` 的 `GET /accounts`），`FakeBankClient` 会把这次读也记进 `requests`。已落地于 545b1c6（`backend/tests/transfer/test_transfer_tools.py:207-239`） |

判定：两条都是**文档口径错、产品代码没错**，修正不扩大实施范围；计划 Task 1 Step 2(a) / Global Constraints / Task 9 Step 1 / 完成判据 1 与 spec §2.10 / §5 / §10.2 第 21 行已全部同步。

### §10.4 第 5 版之后的控制器补充（2026-10-16，Task 3 运行期间）

派发 Task 4–8 之前逐条核对 brief 的 file:line（只读）时发现一处**会让 R4 修了也不生效**的遗漏：

| # | 发现 | 修正 |
|---|---|---|
| 24 | `backend/app/modules/subscription/workflow.py:194` 的提示词要求模型「并给出查询结果里的 `subscription_id`」，但 `subscription_text()`（`backend/app/modules/subscription/tools.py:64-72`）渲染的列表文本里**没有 `subscription_id`**（只有「名称（商户名）」）。这正是 S02 里模型把展示名「验收订阅」塞进 `subscription_id` 的直接原因——提示词让它填一个查询结果里不存在的字段。只加 `name` / `merchant_name` 两个选择器，模型仍可能照旧填 `subscription_id`，S02 复跑就还是 NOT_FOUND | §4.3 加**改动点 5**（把那句改成「用查询结果里的订阅名称或商户名定位（原样照抄）」）；计划 Task 7 Step 2 同步；§11.4 加第 12 行 |

判定：这是**让 R4 真正生效的必要一句**，不是新增需求——它改的是同一文件里一条现在就不可能被执行的指令；该文件本来就在 T01/S07 提示词任务（§4.6 / 计划 Task 7）的 Files 里，改动量一句话、无新功能面。§11.4 的清单因此从 11 个文件变成 **12 个**（第 12 个是提示词文件）。

同一次核对还确认：Task 4/5/6/7/8 的其余 file:line 与夹具**全部命中、签名对得上**（含 `build_registry` 签名、`Registry.roster` 的 `DomainDeclaration`、`_purchase()` 默认 `p_yuebao~/「余额宝」、`holding_view()` 与 `_holding_view()`、`test_read_flow.py` 的 `_agent(model, bank, clock)`、`TEST_PLAN` 的 C14 `:150` / C18 `:173-174` / W01 `:198`、`backend/.env` 缺 `SMTP_USE_SSL`）；仅两处行号漂移（transfer PROMPT 的目标句在 `:67` 而非 `:66`；`risk.py` 的 docstring 在 `:49`），两处都已在 brief 里改成按文本定位。

### §10.5 实施期间发现的测试环境口径与活文档漂移（2026-10-16，Task 3 完成后）

**一、bank mock 套件的测试库口径（计划原文错，已改）**

计划 Global Constraints 原文写「bank mock 测试命令：cd bank_backend 后 uv run pytest -q（基线 301 项通过）」。实测：仓库里**没有** `bank_backend/.env`，bank 的默认 settings 指向远端阿里云 RDS（`rm-cn-rct4ynao80001jso.rwlb.rds.aliyuncs.com`），本机跑出 4 failed / 322 passed / **5 errors**（ERROR 全是连不上 RDS）。改用本地库后：

```powershell
cd bank_backend
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres:123456@127.0.0.1:5432/test'
uv run pytest -q
```

实测 **1 failed, 329 passed, 0 errors**。那唯一一条失败是 `bank_backend/tests/acceptance/test_card_agent_http.py::test_production_agent_executes_card_scenarios_over_real_http`，**在 HEAD（本次改动之前）同样失败**（Task 3 实现者用 stash 验证：子进程 `build_wealth_graph` 需要 bank client，本机缺配置）→ 环境用例，不是本次改动引入。

判定：**文档口径错、产品代码没错**，判据改成「**不得新增失败**」，那条环境失败可以留着。计划 Global Constraints / Task 3 / Task 6 Step 6 / Task 9 Step 2 / 完成判据 1 已同步。

**二、两处活文档漂移（并入 Task 8 Step 5 的可选文档同步，不回头扩已提交的 Task 3）**

| 文件 | 漂移 |
|---|---|
| `backend/app/modules/transfer/README.md:35` | `transfer_contacts` 的参数列仍写 `{query}`；T03 之后不传 query 会列出全部，应为 `{query?}`（同表 `:36` 用 `{status?}` 表示可选） |
| `bank_backend/API接口文档.md:163-164` | 仍把 `q` 写成 required / `minLength=1`，并声明 422；T03 之后两句都过时 |

这两处都**不在** Task 3 brief 的 Files 里（实现者正确地没有自行扩大范围），也不改变功能与验收，因此并入 Task 8 的可选文档同步（与复位 SQL 文档同一批）。

---

## §11 范围纪律自查（第 3 版新增）

用户要求：「确保你没有新增我的需求，或者复杂化需求」。逐条对照如下。

### 11.1 本次范围的每一条都能追到报告

| 本 spec | 报告出处 | 报告原话（摘要） |
|---|---|---|
| R1/R2（T08/T02 归一化） | §3.2（:219-225） | 「transfer/workflow.py 引入同样的 `_PLACEHOLDERS` 过滤（约三行改动），并补一条单测」 |
| R3（T03 列全部收款人） | §3.1（:212-217） | 「contacts() 在 query 为空时列出该用户全部收款人…；PROMPT 与实现对齐」 |
| R4（S02 详情认展示名/商户名） | §3.3（:227-232） | 「让 subscription_detail 同时接受展示名/商户名（先解析再查）」 |
| R5（W13 订单路由） | §3.4（:234-239） | 「给 wealth 域加订单查询只读工具并在 PROMPT 里点明订单号形态」 |
| R6（W03 持仓产品名） | §3.5（:251） | 「数据正确，展示层缺产品名（模型可见摘要里就没有展示名）」 |
| R9（T01/S07 护栏） | §3.5（:247、:249） | T01「只读意图识别不稳」；S07「属过度匹配风险（方案期望明确『没有找到』）」 |
| R8（T07 定案：不改 agent） | §3.5（:248） | 「银行模拟器…**不是 agent 缺陷**」→ 只记录口径，不改 agent |
| R10（夹具/环境） | §4（:255-263） | 6 条环境/夹具问题（本 spec 取其中 5 条，第 6 条 harness 缺陷报告自己已修） |

### 11.2 相对报告，两处「改法不同」（不是加需求）

1. **T08 的收口位置**：报告建议在 transfer 里加三行；本 spec 改为网关一次收口。依据是 m00078 的要求——「确认问题的修复是实打实必要的而不是在错误的架构上补漏」；照报告的三行方案改，wealth 那份仍然漏（§2.2）。
2. **W13 的修法**：报告建议「加订单查询工具」；本 spec 只改 purpose，因为 `wealth_order` 已经存在（§2.6），加工具等于重复实现。

### 11.3 相对报告，我加过的东西（已全部剥离到 §1.1，默认不做）

A1 `*` 通配符语义、A2 transfer/wealth 的「不要写 null」提示词、A3 六模块 purpose 审计+测试、A4 三份副本去重、A5 transfer purpose 补词、A6 空列表文案、A8 单笔持仓验收项。
**A7（`subscription_detail({})` 的行为）保留**——不写清 R4 就有歧义，且它不改变任何行为，只是把契约定义完整。

### 11.4 复杂度自查：必做的产品代码改动清单

| # | 文件 | 改动量 |
|---|---|---|
| 1 | `backend/app/tools/normalize.py` | 新建，约 12 行 |
| 2 | `backend/app/tools/gateway.py` | `:191` 一行 + import |
| 3 | `backend/app/modules/transfer/workflow.py` | `:273` 改一次调用，删 `_clean()`（`:75-83`） |
| 4 | `backend/app/modules/transfer/tools.py` | `contacts()` 空 query 分支（`:109-112`）+ spec 去 required（`:294-299`） |
| 5 | `bank_backend/app/api/routes.py` | `:231` 一个参数 |
| 6 | `bank_backend/app/domain/account_service.py` | 空 query 不加 `ilike`（`:152-170`） |
| 7 | `backend/app/modules/subscription/tools.py` | `detail()` 过滤改三选择器（`:93-111`）+ spec（`:255-261`）+ 一个 property |
| 8 | `backend/app/modules/wealth/__init__.py` | `:26` purpose 加一个词 |
| 9 | `backend/app/modules/wealth/contracts/bank.py` | `Holding` 末尾加一个字段 |
| 10 | `bank_backend/app/domain/investment_service.py` | `holding_view()` 多带一个字段（`:59-76`、`:720-728`） |
| 11 | `docs/MAIN_AGENT_PROMPTS.md` | `:135` 同步 purpose（文档，非代码） |
| 12 | `backend/app/modules/subscription/workflow.py` | `:194` 一句话（提示词要求给出查询结果里不存在的 `subscription_id`；见 §4.3 改动点 5 / §10.4）——该文件本来就在 §4.6 的提示词任务里 |

`backend/app/modules/wealth/infrastructure/tools.py` **不需要改**：`_holdings` 走 `asdict(holding)`，契约加了字段它自动带上——这也是 D7 不动 summary 的原因。

测试：1 个新建（归一化）+ **3 处**必须改的既有断言（`backend/tests/transfer/test_transfer_tools.py:207`、`bank_backend/tests/api/test_transfer_routes.py:139-140`、`backend/tests/wealth/test_bank_adapter.py:521-536`）+ 4 处新增断言；A3 的 purpose 测试是可选的第 2 个新建文件。


