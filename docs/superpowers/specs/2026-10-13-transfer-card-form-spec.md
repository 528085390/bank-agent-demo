# Spec 4 — 把 transfer 改成 card 形式（tools + 自有图，走 A）

日期：2026-10-13 · 基线：`dev` @ `b65ed9d`（工作区；其上已有 `ce1d39d` 的五处接入修复，并已含 `origin/dev` `da1be70` 全部内容）· 状态：待评审（评审通过后转 writing-plans）
前置：spec1（废除 JSON 文本决策协议）已落地；**card 与 wealth 都已经是「tools + 自有图」**，wealth 是本次的直接模板（它自带确认卡与验证码）。
本稿替代同日旧稿（基线 `dev` @ `1fa63ae`）：旧稿的 **P-1 / P-2 两条平台改动已全部作废**，原因见 §0。

## 0. 结论

- transfer 从 **turn 型**（`ModuleSpec(turn=build_turn)`）改成 **tools + 自有图型**（`ModuleSpec(tools=…, handlers=…)` + `DomainDeclaration(graph_builder=build_transfer_graph)`），与 card／wealth 同构。
- **平台侧零改动。** dev 已经自己长出了这条缝，形状比旧稿更好：
  - `backend/app/agents/supervisor.py:1784` 现在把三个对象交给模块图：`spec.declaration.graph_builder(model, spec, assembler=assembler, turn=turn, client=client)`（dev 提交 `b3ed3e4`；`backend/tests/test_graph_builder_seam.py` 钉住「三个关键字必需」）→ **旧稿 P-1（给 `ToolInvocation` 加 `verification_code`）作废**：验证码由模块图**直接**从 `turn.verification_code` 读（先例 `backend/app/modules/wealth/workflow/graph.py:376-377`）。
  - 模块图直接调平台的收尾函数 `_conclude_turn(...)`，确认卡仍由平台唯一实现 `_confirmation_pause` 产出（先例 `backend/app/modules/wealth/workflow/graph.py:314` → `backend/app/agents/supervisor.py:608-612`、`:621-653`）→ **旧稿 P-2（把卡片构造抽成 `confirmation_question()`）作废**：`_confirmation_pause` 一行不动。
- `bank_backend` **一行不动**；确认卡片、验证码、MFA 全部保留，最终仍是**一把** `POST /transfers`（`confirm=true` + `mfa_code=<用户点击时填的码>`）。
- 前端**零改动**：卡片仍由平台既有的 `kind="confirmation"` + `confirmation` 11 键 payload 驱动（`frontend/src/App.vue:117-135`、`:654-701`）。
- 基线 = `dev` @ `b65ed9d`（含 `ce1d39d` 的五处接入修复）；工作树 `verify/transfer-fix` @ `2ce88bf` 作废（§8）。

## 1. 需求

### 1.1 用户原话与边界

- 用户（m00674）：「我想把transfer 改成card形式」。
- 用户（m00699 / m00701）：「走A」「走A，写spec文档吧」。
- 走 A 的三条硬约束（用户已定）：**只改平台侧 + transfer 模块**；**不动 bank_backend**；**保留平台确认卡片 + 验证码 + MFA 一把 POST /transfers**。

### 1.2 验收标准（一句话）

用户说「从工资卡转 5000 给老王」，模块弹出**平台确认卡片**（大额时卡上带验证码输入框），用户点「确认」并填码后，银行侧只发生一次 `POST /transfers`，且请求里带 `confirm=true` 与用户填的那个 `mfa_code`；点「取消」则银行侧**一次写调用都没有**。

## 2. 既有事实（行号基于 dev @ `b65ed9d`）

### 2.1 transfer 现在是**唯一**的 turn 型模块

- `backend/app/modules/transfer/__init__.py`：`TRANSFER = DomainDeclaration(agent_id="transfer", display="转账 Agent", purpose="处理转账、汇款、收款人相关的请求")`（无 `fields`、无 `graph_builder`），`MODULE = ModuleSpec(name="transfer", declaration=TRANSFER, turn=build_turn)`。
- `backend/tests/test_module_registry.py:227-251` 现在的断言：`set(registry.catalog.names()) == set(card.tools) | set(wealth.tools)`、`len(card.tools) == 12`、`len(wealth.tools) == 7`、`all(module.tools == {} for module in ALL_MODULES if module.name not in {"card","wealth"})`、**`[module.name for module in ALL_MODULES if module.turn is not None] == ["transfer"]`**，docstring 明说「Transfer is the one module that still answers through the turn seam.」→ 迁移后这条要变成 `== []`（§6.2）。
- 私有层（迁移后要删／重写的部分）：`application/turn.py`（把私有 `TransferAgent` 包成 `TurnHandler`）、`application/runtime.py` + `workflow/graph.py`（模块自己的 LangGraph + 私有模型轮次）、`infrastructure/tools.py`（`READ_TOOLS`：`handler(bank, context, params)` 的**模块私有协议**）、`llm/decisions.py`（私有 JSON 解析）、`infrastructure/bank_adapter.py`、`contracts/*`、`domain/*`、`cli.py`、`example.py`。
- `backend/app/modules/base.py:138-142`：`if self.turn is not None and self.tools: raise ValueError(… "one request cannot be answered twice")` → **迁移必须去掉 `turn`**。

### 2.2 模块图的新缝（dev `b3ed3e4`，本次直接吃到的红利）

- `backend/app/agents/supervisor.py:1784`：`return spec.declaration.graph_builder(model, spec, assembler=assembler, turn=turn, client=client)`（`build_domain_graph` 与 `build_supervisor` 都新增了 `client` 形参）。
- `backend/app/agents/declaration.py:69`：`graph_builder: Callable[..., Any] | None`（不再是 `Callable[[Any, Any], Any]`）。
- `backend/app/agents/domain.py:60`／`:80`：`build_agent_graph(…, client=…)` 透传；`backend/app/runtime.py:68-101` 的 `agent_graph_factory(…, *, client=…)` 把组装根的 `Runtime.bank_client` 一路带下去。
- `backend/app/agents/supervisor.py:518-536`：`@contextmanager def domain_scope(spec, state, assembler, turn, message) -> Iterator[Handoff]` —— 模块图**自己开记忆作用域**的入口（`handoff.view()` 读会话、`handoff.publish()` 写回都在作用域内）。注释强调它必须是真 contextmanager，不能把子图再 `invoke` 一次。
- 约定由 `backend/tests/test_graph_builder_seam.py`（132 行）钉住：builder 的签名必须是 `(model, spec, *, assembler, turn, client)`（关键字必需，不传就大声失败），三个对象原样到达，且 `client=None` 时也要到达。

### 2.3 验证码到模块图（不再是缺口）

- 点击路由 `backend/app/api/routes.py:461-543` `decide_pending_action`：`:510` `code = (body.verification_code or "").strip()`；`:515` `_click_matches`（要求 `pending.kind == CONFIRMATION_KIND` 且 `confirmation.pending_id` 相同，`:557-572`）；`:525` `_requires_code(pending)`（读 `pending.confirmation.requires_code`，`:546-554`）为真而没带码 → **422 VERIFICATION_CODE_REQUIRED**；`:535` `_turn_response(…, verification_code=code or None)` → `TurnContext.verification_code`。
- 它**只活在这次请求的内存里**：不进 `AgentState`、不进检查点、不进账本（`backend/app/agents/runtime.py` 里 `verification_code` 的注释明说；resume 的 envelope 只带 `resume` + `message_id` + `content`）。
- 模块图拿得到它：`turn` 现在就是 `graph_builder` 的关键字实参，图在**每次请求重建**（`backend/app/agents/runtime.py:179-199` `_graph_for`），所以 `process` 节点闭包里的 `turn` 就是本次点击的那个 → 先例 `backend/app/modules/wealth/workflow/graph.py:376-377`：`verification_code=(turn.verification_code if turn is not None and confirm else None)`。
- 注意用词：wealth 文档里的「**旧验证码入口已停用**」指的是它自己那条 `kind=verify` 独立流程（`backend/app/modules/wealth/contracts/requests.py:15`、`:48`），**不是**确认卡上的验证码；确认卡的码仍然照常要求与传递。

### 2.4 确认卡（模块图直接复用平台唯一实现）

- 平台确认卡片通道：`kind="confirmation"`（`backend/app/agents/turn.py:54`）+ `confirmation` 11 键 payload（`TurnConfirmation.as_payload()`，`turn.py:57-114`）。
- 模块图的用法（wealth 已跑通）：造 `TurnResult(status=TurnStatus.WAITING_CONFIRMATION, summary=…, confirmation=TurnConfirmation(…))` → `_conclude_turn(spec, result, writer, state, updates)`（`backend/app/modules/wealth/workflow/graph.py:314`）→ 平台 `backend/app/agents/supervisor.py:608-612` 调 `_confirmation_pause(spec, result)`（`:621-653`）挂出 `kind="confirmation"` 的 `pending_question`，并同时写 `subtask_end`（带 `turn_status`）与 `domain_results`。
- `_conclude_turn` 对 `WAITING_INPUT` **只写「在等谁」，不写问题**：要挂输入卡必须自己调 `_awaiting(...)`（wealth `graph.py:317-332` 的注释与代码）。
- 点击怎么回到模块图：`recovery_hint` 把图里的 **interrupt payload 原样**当 `hint.pending`（`backend/app/agents/runtime.py:367-377`），而 `routes.py:499` 的 resume 里**刻意不带验证码**（它进 fingerprint 与 replay）→ `Command(resume=…)` 直接回到模块自己的 `wait_for_user` → `process`。supervisor 的 `_answer_confirmation` 是 turn 专属，模块图里根本不存在，也不需要。
- **前端零改动**：卡片形状与 turn 型逐字相同。
- **这条组合已被测试钉住**：`backend/tests/test_wealth_write_api.py`（662 行）走的就是「生产注册表 + wealth 的 `graph_builder` + 平台 `kind="confirmation"` 卡片 + `POST /api/conversations/{id}/pending-actions/{pending_id}/decide`」，并覆盖了无码 422 且卡不变、银行 1007 回到等待确认后带码只成交一笔、过期卡 409、暂停中裸消息 409 且问题原样、resume 信封键集必须相等 —— transfer 的实现与测试按它的形状做（脚手架见 `:173-253`）。

### 2.5 模块图必须遵守的约束

- **工具名必须无点**：`backend/app/tools/schema.py:24` `_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")`，`:85-88` 不匹配直接 `ValueError`（provider 对含点名字回 11152/503）。→ transfer 的工具名是 `transfer_*`（对比 `card/tools.py:173` `name=f"card_{name}"`、wealth 的七个工具已改成下划线）。
- **必须发 `turn_status`**：`_already_concluded` 靠它判断模块是否收尾，漏发就会被反复派发；用 `_conclude_turn` 就自动带上（它写 `result.status.value`）。
- **工具 properties 不得撞保留名**：`RESERVED_PARAMETER_NAMES = {"confirmed","idempotency_key","call_id","agent_id"}`（`backend/app/tools/protocol.py:111-113`），`backend/app/modules/registry.py:96-103` 会 raise。
- **处理器契约**：`ToolHandler = Callable[[ToolInvocation, BankClient], ToolData]`（`backend/app/modules/base.py:100`），返回 `ToolData(summary, display, facts, business_ids)`（`:21-48`）。`backend/app/tools/bank_adapter.py:73` `handler(invocation, self._client)`；WRITE 且无 `invocation.call.idempotency_key` → FAILED `IDEMPOTENCY_KEY_REQUIRED`（fail closed）。
- **facts 不出服务端**：`_call_tool` 只把 `summary/display/business_ids` 写进 SSE 与审计；但**返回给图的对象仍带 `facts`**（card 就靠它读 `action`）。transfer 的意图解析结果走 `facts`。
- **模块图怎么拿身份与银行**：`spec.handle.trusted_context`（wealth `graph.py:273`）；读工具经 `_call_tool(spec, DomainOutcome(tool_name=…, arguments=…), writer, idempotency_key=…)`；写用 `graph_builder` 交进来的裸 `client`（wealth `graph.py:263` `HttpWealthBank(client)`）。

### 2.6 银行契约（本 spec 不改）

- `bank_backend/app/domain/risk.py`：`YELLOW_THRESHOLD_MINOR = 100000`（1000 元）、`RED_THRESHOLD_MINOR = 500000`（5000 元）；`classify(amount_minor)` → ≥500000 红（`confirm_required=True, mfa_required=True`）/ ≥100000 黄（`confirm_required=True, mfa_required=False`）/ 绿（都 False）；`require_confirmation`：需要码而 `mfa_code` 不是数字 → `ConfirmationRequiredError`。**模拟 MFA：任意非空数字码通过。**
- `bank_backend/app/api/routes.py:221-243` `POST /transfers`（operation_id=createTransfer）；请求体 `bank_backend/app/api/schemas.py:37-45` `TransferRequest`（`confirm=False`、`mfa_code=None`、`amount_minor` gt=0 le=100000000）；幂等走 `Idempotency-Key` 头（`routes.py:230`）或 body 的 `idempotent_key`。
- 成功 payload（`bank_backend/app/domain/transfer_service.py:62-74`）含 `transaction_id / counterparty_transaction_id / reference_id / from_account_id / to_account_id / amount_minor / status="completed"`。
- `POST /transfers/scheduled/{task_id}/cancel`（`routes.py:308+`）**不调用** `require_confirmation`（`transfer_service.py:339-359`）→ 取消定时转账不需要 confirm/MFA。
- 读侧：`GET /accounts`（`routes.py:121`）、`GET /accounts/{id}/balance`（只回 `account_id/balance_minor/available_minor/currency`，`:152-166`）、`GET /contacts`（`:202`）、`GET /transfers/scheduled`（`:246`）。
- 平台侧调用：`backend/app/integration/bank_client.py:122-170` `request(method, path, *, user_id, params, json, idempotency_key)`（`user_id` 同时写 query 与 body，`:148-150`；`idempotency_key` → `Idempotency-Key` 头，`:151`）；错误码 1007 → `CONFIRMATION_REQUIRED`（`:29-35`）。

### 2.7 与 wealth／card 的差异（决定了本 spec 的形状）

1. **有码的写不能走工具网关**：网关的调用契约是 `ToolHandler(invocation, client)`，没有验证码通道；dev 的选择是「把 `turn` 与 `client` 交给模块图」，所以 transfer 的 `POST /transfers` 走**模块自己的受控路径**（wealth 的写也是这条形状）。无码的写（取消定时转账）仍可走网关，白拿审计与幂等。
2. **银行写端点不同**：card 是两段（`POST /cards/actions` 建动作 → `POST /cards/actions/{id}/execute`），pending 动作**存在银行侧**；transfer 是**一把** `POST /transfers`，所以「待确认的转账」只存在于**图的检查点状态**里。→ **取消转账不调银行**（本来就没写出去），这是设计，不是漏调用。
3. **金额闸门来源不同**：card 的 `permission_level` 由银行在 pending 动作上给出；transfer 没有预览端点，`requires_code` 只能由模块按 §2.6 的阈值算，并以银行的 `CONFIRMATION_REQUIRED` 兜底（见 §4.4）。

## 3. T-1 transfer：工具、处理器与写路径

### 3.1 工具清单（5 个，全部无点）

| 工具 | side_effect | 参数 | 银行调用 | 给模型看 | 走哪条路 |
|---|---|---|---|---|---|
| `transfer_accounts` | READ | `{}` | `GET /accounts` + 逐个 `GET /accounts/{id}/balance` | 是 | 网关 |
| `transfer_contacts` | READ | `{query: string}` | `GET /contacts`（按 query 过滤） | 是 | 网关 |
| `transfer_scheduled` | READ | `{status?: enum}` | `GET /transfers/scheduled` | 是 | 网关 |
| `transfer_prepare` | READ | `{amount_yuan, to_name, from_hint?, from_account_id?, to_account_id?, remark?}` | 只读解析（账户/联系人），**不写** | 是 | 网关 |
| `transfer_cancel` | WRITE | `{task_id}` | `POST /transfers/scheduled/{task_id}/cancel` | 是 | 网关（但图的 `run_call` 先拦成确认卡，点击后才调） |

- `POST /transfers` **不是工具**：它是模块写路径（§3.4），由图的 `process` 节点在点击确认之后调用。
- 写工具的做法：`transfer_cancel` **留在 schema 里**（模型可以提议取消），但图的 `run_call` 见到它**一律先挂确认卡**（`hang_cancel`：先用 `transfer_scheduled` 查到金额，查不到就如实收尾），点击确认后才经网关执行 —— 与 card 把 `card_execute`/`card_cancel` 摘出 schema 的做法不同，理由是「取消定时转账」没有别的可见入口，摘掉就等于不交付（评审 C-2）。因此「取消定时转账」是**可达**的：模型提议 → 图挂卡 → 用户点击 → 网关执行（幂等键在挂卡时铸一次，审计里有 `tool_call`/`tool_result`）。
- 身份：`customer(invocation)` 读 `invocation.context.bank_customer_id`，空则 `ToolFailure(NO_CUSTOMER_BINDING)`（card/tools.py:30-36 同款）。
- 幂等：网关的写工具必须带 `idempotency_key=invocation.call.idempotency_key`（`bank_client.py:151` → `Idempotency-Key` 头）。`backend/tests/test_domain_harness.py:212-255` 有「一次超时不能变成两次转账」的用例（关键断言在 `:253`），新 handler 照此办理。

### 3.2 金额解析

`amount_yuan` 是**人民币元字符串**（模型不做换算）。解析器与 card 同款：正则 `^[0-9]{1,7}(\.[0-9]{1,2})?$` → `Decimal` × 100 → int，`> MAX_TRANSFER_MINOR (100000000)` 拒绝（card/tools.py:61-70 `yuan_minor`）。展示用 `app.modules.money.format_minor`（`backend/app/modules/money.py:14-25`，`832000` → `"8,320.00 元"`）。

**决定**：`yuan_minor` 暂时在 transfer 的 tools 里自带一份（照抄 card 的实现与拒绝语义），**不**提到 `app/modules/money.py`——避免把 card 也拖进本次改动；若实现时觉得重复碍眼，允许在 `money.py` 增加 `parse_yuan` 并让两处共用，但那属于可选重构，不是验收项。

### 3.3 `transfer_prepare` 的返回（图的输入契约）

handler 把解析结果放进 `facts`（不出服务端），**不产生任何银行写调用**：

```python
# 解析成功
ToolData(summary="请确认这笔转账：5,000.00 元，从工资卡转给老王。",
         display=…,
         facts={"status": "ready",
                "intent": {"from_account_id": …, "to_account_id": …,
                           "amount_minor": 500000, "remark": …},
                "from_label": "工资卡", "to_label": "老王",
                "requires_code": True},
         business_ids={})

# 需要用户选一个（付款账户不唯一 / 收款人同名多条）
ToolData(summary="有多张卡符合条件，请选择要转账的账户。",
         display=…,
         facts={"status": "select",
                "field": "from_account",              # 或 "to_account"
                "candidates": [{"label": "工资卡 尾号 1234", "value": "a_1"}, …],
                "intent": {…已解析的部分…}},          # 回填用
         business_ids={})
```

- 解析失败（账户不存在、余额不足、金额越界、收款人查不到）→ `ToolFailure`，`summary` 是给人看的中文说明。
- `requires_code = intent["amount_minor"] >= RED_THRESHOLD_MINOR`（500000，模块内命名常量，注释指向 `bank_backend/app/domain/risk.py`）。**银行仍是权威**：§4.4 用 `CONFIRMATION_REQUIRED` 兜底。
- 候选 label 一律由**银行数据**拼（含尾号），模型不能凭 label 指认他人的账户（card/workflow.py:164 的注释同款）。

### 3.4 写路径（模块自己的受控路径，不走网关）

新文件（建议 `backend/app/modules/transfer/application/writes.py`，或把现有 `application/runtime.py` 重写成这一职责；命名留给实现）：

```python
def submit_transfer(client, context: TrustedContext, intent: Mapping[str, Any],
                    pending: Mapping[str, Any], *, verification_code: str | None) -> BankResponse:
    """点击「确认」之后唯一一次银行写。intent 与 pending 都是挂卡时冻结的。"""

    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        # 银行只接受数字码；提前拒，给中文提示（银行仍会以 1007 兜底）。
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")
    return client.request(
        "POST", "/transfers",
        user_id=context.bank_customer_id,   # 银行侧的 user_id 是银行客户号（card 与所有读工具同款）；空值先抛 NO_CUSTOMER_BINDING
        json={"from_account_id": intent["from_account_id"],
              "to_account_id": intent["to_account_id"],
              "amount_minor": intent["amount_minor"],
              "remark": intent.get("remark"),
              "confirm": True, "mfa_code": code,
              "idempotent_key": pending["idempotency_key"]},
        idempotency_key=pending["idempotency_key"],
    )

def cancel_scheduled(client, context, task_id, *, idempotency_key) -> BankResponse: …
```

- **幂等键在挂卡时铸一次**并冻结进 `transfer_pending`，点击（含重试、重放、双击）复用同一把 —— wealth 同款（`backend/app/modules/wealth/application/runtime.py:294` `f"wealth-{uuid4().hex}"` 存进待办，`:920`／`:1064-1067` 取用）。银行侧因此**最多一笔**。
- 银行回 1007 `CONFIRMATION_REQUIRED`（需要码而没给、或码不合规）→ 图的 `process` **把卡片升级成 `requires_code=True` 再挂一次**（不能原样重挂：前端只在 `requires_code` 为真时出验证码输入框，原样重挂等于让用户永久补不上码），保留同一个 `pending_id` 与冻结的幂等键（§4.4）。
- **写不经工具网关**（照 wealth）：因此不产生 `tool_call`/`tool_result` 审计行，只有业务日志与 SSE 的收尾帧；如果要求写也进审计，需要平台给网关加一条验证码通道（旧稿 P-1），**本次明确不做**（§8、§9）。
- 取消定时转账**没有验证码**，所以它经网关的 `transfer_cancel` 执行，白拿审计与幂等（`idempotency_key` 同样在挂卡时铸一次）。

## 4. T-2 transfer：自有图

新建 `backend/app/modules/transfer/workflow.py`，形状照 wealth 的 `build_wealth_graph`（`backend/app/modules/wealth/workflow/graph.py:230-416`）：**收 `client`、用平台 `_conclude_turn` 收尾**（**不打开 `domain_scope`、不使用 `assembler`**：decide 只用 system+user 两条消息、从不读会话记忆 —— 与 card 一致；`test_graph_builder_seam.py` 只要求接收三个关键字，不要求使用）。图是**每次请求重建**的（`backend/app/agents/runtime.py:179-199`），不带 checkpointer。**一处自觉偏离**：wealth 在 `client is None` 时 `raise ValueError`（`graph.py:252-255`），transfer 按 plan Task 3 抛 `UpstreamUnavailable("TOOL_UNAVAILABLE")`（与 card 的 `invoke` 失败同款）——这是 plan 定的，spec 认可。

### 4.1 builder 签名与作用域

```python
def build_transfer_graph(model, spec, *, assembler=None, turn=None, client=None):
    # 建图**不**要求 client（基座为每个域、每次请求都重建一次图；一个模块不能因为这次没有
    # 银行客户端就让整张图建不起来）。失败推迟到真正要写银行的那一刻（process 里的那一次 POST）。
    …
    graph = StateGraph(TransferState)
    graph.add_node("decide", decide); graph.add_node("wait", wait_for_user); graph.add_node("process", process)
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
    return graph.compile()
```

- 三个关键字**必须**接（`backend/tests/test_graph_builder_seam.py` 的约定）；`turn` 只用来读 `verification_code`（§4.4）。
- `domain_scope(spec, state, assembler, turn, message)`（`backend/app/agents/supervisor.py:518-536`）是「模块图自己读/写会话记忆」的入口；**transfer 不用它**：这一轮没有跨轮槽位，decide 的 messages 只有 system(prompt)+user(goal/answers)，从不读 `assembler`。card 也是完全不开的（先例）。

### 4.2 状态

```python
class TransferState(AgentState, total=False):
    transfer_intent: dict[str, Any] | None    # 已冻结的转账意图（点击时就用它写）
    transfer_pending: dict[str, Any] | None   # {"pending_id": …, "idempotency_key": …}
    transfer_call: dict[str, Any] | None      # 选择题待回填的 transfer_prepare 调用
    transfer_choices: dict[str, str]          # 选择题的 label -> value
    transfer_field: str | None                # 选择题在问哪个字段
```

### 4.3 `decide`

- prompt 明说「不需要询问确认、验证码，代码会处理」（card/workflow.py:215 同款），并说明：金额用元字符串；缺信息用 `ask_user` 追问 `amount_yuan / to_name / from_hint / remark`；一次只办一笔；**不要声称已经转账成功**；**只做即时转账**——用户要「明天/每月/定时」这类预约时，不要当作即时转账执行，直接说明当前不支持预约（§10）；取消定时转账用 `transfer_cancel`（代码会要求用户确认，不要自己声称已取消）。
- schemas = **全部工具**的 `function_schema` + `function_schema(ask_user_spec())`（`transfer_cancel` 也在内）；`text, calls = collect_stream(model, messages, schemas)`（`StreamTruncated` → 收尾 `"模型输出中断，本次没有发起任何转账。"`，FAILED）；`known = {可见工具名} | {ASK_USER_TOOL_NAME}`。
- `outcome = _domain_outcome(parse_domain_decision(text, calls, known))`：
  - `outcome.ask` → 字段必须都在 `spec.declaration.fields` 里，否则收尾 `"请说明转账金额、收款人和付款账户。"`；然后 `_awaiting(spec, outcome, writer, collected=answers or {}, attempt=attempt)`（attempt 取 `pending_question.attempt + 1`，card/workflow.py:244-247 同款）。
  - `outcome.tool_name == "transfer_prepare"` → §4.4。
  - `transfer_cancel` 的提议 → `run_call` **先拦**：`hang_cancel` 用 `transfer_scheduled` 查到该 task（查不到 → 如实收尾 `FAILED`），挂一张 `requires_code=False` 的确认卡；点击确认后才经网关执行，点取消则银行零写调用。模型永远不能直接执行这笔写。
  - 其它工具名 → `_call_tool` 调一次，用结果收尾（读工具的答案就是 summary）。**读工具失败**也直接收尾，不重试到死。
  - 没有工具也没有 ask → 若模型给了非空 summary，就**用它的原话**收尾（例如「当前不支持预约转账。」），否则收尾 `"请说明要转账的金额、收款人与付款账户；当前尚未发起任何转账。"`；状态一律 `UNSUPPORTED`（事件层是 DENIED，这条路径没有任何写调用）。

### 4.4 挂卡与点击消费

```python
def prepare(state, call):
    result = invoke("transfer_prepare", call["arguments"])      # 网关读工具
    if not result.succeeded:
        return conclude(state, summary=result.summary, status=TurnStatus.FAILED)
    facts = result.data["facts"]

    if facts["status"] == "select":                             # §4.5
        …

    intent = dict(facts["intent"])
    confirmation = TurnConfirmation(
        pending_id=f"tp_{uuid4().hex}", operation="transfer", title="确认转账",
        amount_minor=intent["amount_minor"], product_name=facts["to_label"],
        account_display=facts["from_label"], requires_code=bool(facts["requires_code"]),
    )
    conclusion = _conclude_turn(                            # 平台唯一实现挂出 kind="confirmation"
        spec,
        TurnResult(status=TurnStatus.WAITING_CONFIRMATION,
                   summary=result.data["summary"], confirmation=confirmation),
        get_stream_writer(), state, (),
    )
    conclusion["transfer_intent"] = intent                   # 冻结：点击时不再重新解析用户原话
    conclusion["transfer_pending"] = {"pending_id": confirmation.pending_id,
                                      "idempotency_key": f"transfer-{uuid4().hex}"}
    return conclusion

def process(state):
    question = state.get("pending_question") or {}
    answers, problem = _read_answer(state.get("pending_answer"), question)
    if problem:                                              # 照抄 card/workflow.py:254-267
        …
    if question.get("kind") == "transfer_selection":         # §4.5
        …
    if question.get("kind") == CONFIRMATION_KIND:
        kind = _action_kind(question, answers)               # 机器值 confirm/cancel 与卡片标签都认
        if kind is None:
            return conclude(state, summary="请明确确认或取消。", status=TurnStatus.UNSUPPORTED)
        if kind is TurnAction.CANCEL:
            return conclude(state, summary="已取消，本次转账没有发起。")   # 银行零写调用
        try:
            response = submit_transfer(client, spec.handle.trusted_context, state["transfer_intent"],
                                       state["transfer_pending"],
                                       # 验证码只跟着确认走，且只来自基座从这次调用里读到的那个值：
                                       # 它随 TurnContext 闭包进来，从不进图 state，也不进检查点。
                                       verification_code=(turn.verification_code if turn is not None else None))
        except ToolFailure as failure:
            if failure.code == ErrorCode.CONFIRMATION_REQUIRED:
                # 升级卡片：前端只在 requires_code 为真时出验证码框；pending_id 与冻结的幂等键都不换。
                upgraded = TurnConfirmation(
                    pending_id=confirmation.pending_id, operation="transfer", title="确认转账",
                    amount_minor=intent["amount_minor"], product_name=confirmation.product_name,
                    account_display=confirmation.account_display, requires_code=True)
                conclusion = _conclude_turn(
                    spec,
                    TurnResult(status=TurnStatus.WAITING_CONFIRMATION,
                               summary=f"{failure.message}\n这笔转账需要验证码，请填写后再次确认。",
                               confirmation=upgraded),
                    get_stream_writer(), state, ())
                conclusion.update({"transfer_intent": intent, "transfer_pending": pending})
                return conclusion
            return conclude(state, summary=failure.message, status=TurnStatus.FAILED)
        return conclude(state, summary=…, status=TurnStatus.COMPLETED,
                        facts={"transaction_id": …, "amount_minor": …},
                        business_ids={"transaction_id": …})

    collected = {**(state.get("collected_inputs") or {}), **answers}
    return decide(state, answers=collected)                  # kind="input" 的补充信息
```

- `conclude(state, summary, status=COMPLETED, facts=None, business_ids=None)` 是本模块的小助手：造 `TurnResult` → `_conclude_turn(spec, result, get_stream_writer(), state, ())` → 再叠 `idle_work_state()` 并清掉 `transfer_intent/transfer_pending/transfer_call/transfer_choices/transfer_field`（wealth `land` 同款）。**不要自己手写 `subtask_end`**，否则容易漏 `turn_status` 而被反复派发。
- `_action_kind` 复用平台实现（`backend/app/agents/supervisor.py:866-883`），**不重复写标签比对**。
- `CONFIRMATION_REQUIRED` **升级卡片**（`requires_code=True`，同一 `pending_id`、同一冻结幂等键），并且**走 `_conclude_turn`** 而不是裸返回 state —— 否则这一轮没有 `subtask_end`/`domain_results`，用户看不到卡为什么又回来了（评审 C-1/C-3）。
- 双击保护在**路由层**（`decide` 路由的 `Idempotency-Key`，`backend/tests/test_pending_action_api.py:11` 钉住「a double click is one submission」），再叠上 §3.4 的写幂等键，银行侧最多一笔。

### 4.5 歧义选择（`status="select"`）

与 card 的 `card_selection` 同构（card/workflow.py:163-191）：挂一个**普通问题**，`kind="transfer_selection"`，`fields=[{name: "selection", label: 付款账户/收款人, type: "select", required: True, options: [label, …]}]`，options 来自银行数据。`process` 里校验所选 label 在 `transfer_choices` 内，否则收尾 `"所选账户不在当前候选列表中。"`（UNSUPPORTED）；命中则把 value 按 `transfer_field`（`from_account` → `from_account_id`，`to_account` → `to_account_id`）回填进 `transfer_call.arguments` 并**重新调一次 `transfer_prepare`**（此时应 `status="ready"`，走 §4.4 的挂卡分支）。

## 5. T-3 声明切换与删除

```python
# backend/app/modules/transfer/__init__.py
TRANSFER = DomainDeclaration(
    agent_id="transfer",
    display="转账 Agent",
    purpose="处理转账、汇款、收款人相关的请求",
    fields={
        "amount_yuan": FieldMeta(label="转账金额"),
        "to_name": FieldMeta(label="收款人"),
        "from_hint": FieldMeta(label="付款账户"),
        "remark": FieldMeta(label="备注"),
        "status": FieldMeta(label="定时转账状态", options=("pending", "completed", "cancelled", "failed")),
    },
    graph_builder=build_transfer_graph,
)
MODULE = ModuleSpec(name="transfer", declaration=TRANSFER, tools=TOOLS, handlers=HANDLERS)
```

- **去掉 `turn=`**（`base.py:138-142` 不允许 turn 与 tools 并存）→ `backend/app/runtime.py:56-65` 的 `build_turns` 在生产注册表上返回 `{}`（wealth 已经是图，transfer 是最后一个 turn 模块）。
- `__init__.py` 的 docstring 现在写的是 turn 接入与「小额只点击、大额点击+验证码」——改成 tools + 自有图，并写清：确认卡是平台的 `kind="confirmation"`；验证码由平台经 `graph_builder` 的 `turn` 关键字到达模块图；`POST /transfers` 由模块写路径在点击后发起（不走网关）。

**删除／保留**：

| 路径 | 处置 | 原因 |
|---|---|---|
| `application/turn.py` | 删 | turn 接入被自有图取代 |
| `application/runtime.py`、`application/runtime_old.py`、`application/factory.py` | 重写／删 | 私有模型轮次被图的 `decide` 取代；其中**只有写路径**（`POST /transfers`、取消）值得留下，重写成 §3.4 的薄模块 |
| `workflow/graph.py`、`workflow/nodes.py`、`workflow/state.py` | 删 | 私有图被 `modules/transfer/workflow.py` 取代 |
| `llm/decisions.py`、`llm/prompts.py` | 删 | 决策走平台 `parse_domain_decision` + 原生 tool calling |
| `infrastructure/tools.py` | 删 | `READ_TOOLS` 是模块私有协议，被平台 `ToolSpec`/`ToolHandler` 取代（新 tools 放在 `modules/transfer/tools.py`，镜像 card） |
| `infrastructure/bank_adapter.py` | 改写或删 | 读工具直接拿平台 `BankClient`；写路径直接用 `client.request` |
| `cli.py`、`example.py` | 删 | 依赖上面那些私有层；要保留演示就等图落地后另开一个用平台入口的小脚本 |
| `contracts/*`、`domain/*` | 删 | 新 handler 直接读银行 payload dict、自己校验；若某条规则值得留（如金额上限），搬进新 `tools.py` 并在 commit 里说明 |
| `DELIVERY.md`、`QUICKSTART.md`、`README.md`、`智能转账模块功能修复报告.md` | 改写 | 现在描述的是 turn 型；README 还声称存在 `tests/transfer_integration` 目录（远端没有） |

## 6. 测试面

### 6.1 新增

`backend/tests/transfer/test_transfer_card_wiring.py`（走**平台真实入口**，不用 Mock 图；脚手架照 `backend/tests/test_wealth_write_api.py:173-253`，并且**必须 `create(client, title=…)`** —— 不带标题会触发后台自动命名，多问一次模型并把脚本模型的一条回复吃掉，见该文件 `:57-62`）：

1. **注册面**：`build_registry()` 后 transfer 的工具名全部 `startswith("transfer_")` 且无点；`catalog.for_agent("transfer").names` = 5 个；`module.turn is None`；`transfer_cancel` 在 `catalog` 里存在，**也在模型可见 schema 内**（图会拦成确认卡）。
2. **图到卡**：用 `tests/support/scripted_model.ScriptedModel` 脚本 `("call","handoff",…) → ("call","transfer_prepare",…)`，断言 SSE `interrupt` 帧 `kind == "confirmation"`、`confirmation.pending_id` 存在、`confirmation.requires_code` 与金额阈值一致（4999.99 → False，5000.00 → True）。
3. **小额点击**（`test_pending_action_api.py` 的 `paused()/decide()` 生命周期照抄）：POST `pending-actions/{pending_id}/decide`，`decision="confirm"` 无码 → 银行收到 `confirm=true` 且没有 `mfa_code`（或 `None`）。
4. **大额点击**：`requires_code=true`，不带码 → **422 VERIFICATION_CODE_REQUIRED** 且**图没有被进入**（银行零写调用）；带 `verification_code="123456"` → 银行收到 `mfa_code="123456"`，且 `POST /transfers` **只发生一次**。
5. **取消**：`decision="cancel"` → 银行写端点零调用，summary 说明未发起。
6. **取消定时转账**：①端到端 —— 模型提议 `transfer_cancel` → 先出确认卡、银行零写调用 → 点确认 → 银行收到一次 `POST /transfers/scheduled/{task_id}/cancel` 且带冻结的 `Idempotency-Key`；点取消 → 零写调用。②网关级 —— 在 `audit_context(...)` 里直调该工具，审计里能看到这次 `tool_call`/`tool_result`（直连调用必须带审计上下文，否则读不回来）。
7. **银行兜底**：银行回 1007 `CONFIRMATION_REQUIRED` 时，卡片**升级为 `requires_code=True`** 重挂（`pending_id` 不变、这一轮有 `subtask_end`），再点一次带码成功。
8. **验证码不泄漏**：SSE 帧与审计行里 grep 不到那个码；`verification_code` 不出现在图 state、检查点与账本消息里。
9. **双击/重放**：同一张卡点两次 → 银行侧只发生一次 `POST /transfers`（同一把幂等键）。
10. **歧义选择**：两个同名联系人 → 先出 `transfer_selection`，选完出确认卡。
11. **预约意图不落成即时转账**：脚本里用户说「明天 9 点转 5000」，断言**银行写端点零调用**、summary 如实说明不支持预约（定时转账的创建本次不做，登记为待开发功能，见 §12 D-1）。

### 6.2 改写（会被打破的既有断言）

- `backend/tests/test_module_registry.py:227-251`：`set(registry.catalog.names()) == set(card.tools) | set(wealth.tools) | set(transfer.tools)`；`all(module.tools == {} for module in ALL_MODULES if module.name not in {"card","wealth","transfer"})`；**`[module.name for module in ALL_MODULES if module.turn is not None] == []`**（函数名与 docstring 里的「one turn owner / the one module that still answers through the turn seam」一起改）；`:212-224` 的 roster 顺序断言不变。
- `backend/tests/test_card_module.py`：`assert registry.catalog.for_agent("transfer").names == ()` 改成 transfer 前缀断言；**「工具名无点」不需要再抄一份**：`test_card_module.py:114-126` 已经用 `build_registry().catalog.names()` 覆盖全量 catalog（`:123`、`:126`），transfer 的工具已在其内。
- `backend/tests/transfer/test_transfer_behavior.py`、`test_transfer_module.py`：测的是被删掉的私有协议/私有图 → 按新结构改写或删除（保留其中仍有意义的 domain 规则用例，改成直接测 handler）。
- `backend/tests/test_import_cycle.py:28` 的 `from app.modules.transfer import MODULE` 不变。

### 6.3 平台测试夹具的迁移（实施时新增，见 plan 的执行记录）

`transfer` 过去是「唯一还走 turn 的模块」，平台测试普遍把它当作**通用工具循环**的替身域（`grants={"transfer": ["echo"]}`）。它现在声明了 `graph_builder`，这些测试会被转账的真实图接管。修法是给它们一个**没有模块图**的 roster：`tests/support/domain_harness.py` 的 `generic_roster()` 把每个 declaration 的 `graph_builder` 置空，各测试的 graph 工厂改用它（`test_domain_agents`、`test_supervisor_protocol`、`test_supervisor_streaming`、`test_cross_domain_orchestration`、`test_multi_agent_api`、`test_conversation_api`、`test_conversation_runtime`、`test_conversation_audit`、`test_interrupt_reset_integration`、`test_audit_end_to_end`、`test_conversation_auto_title`）。这些测试测的是 Supervisor／基础循环的契约，不是转账模块。

### 6.4 回归

- 全量 `pytest backend/tests` 通过，记录数字（注意：本机用 `backend\.venv\Scripts\python.exe`；仓库新加了 `pytest-local.ps1`，跑测试优先用它）。
- wealth 的确认卡与写路径用例（`backend/tests/wealth/*`）全部通过 —— 本次**没有**动平台，它们不该有任何变化。
- `test_domain_agents.py` / `test_domain_harness.py` 里 `gateway.handle("transfer", CONTEXT)` 的既有调用不变。

## 7. 验收（端到端）

1. **小额（<1000 元）**：卡片出现，`requires_code=false`（卡上无验证码框），点确认 → 一次 `POST /transfers`（`confirm=true`、无 `mfa_code`）→ 成功；点取消 → 银行零写调用。
2. **中额（1000–4999.99 元）**：`requires_code=false`，确认后银行按 `confirm_required` 放行。
3. **大额（≥5000 元）**：`requires_code=true`；不带码点击 → 422 且未进图；带数字码 → `mfa_code` 到银行 → 成功。
4. **码不是数字**：写路径判 `CONFIRMATION_REQUIRED`，卡片升级为 `requires_code=True` 重挂，不产生银行写调用。
5. **验证码不进 SSE、不进审计、不进检查点**（grep + 审计行断言）。
6. **transfer 不再声明 turn**，`build_turns(registry, client) == {}`；wealth 仍是图。
7. 前端不改一行，卡片与验证码框正常渲染（人工冒烟，结果归档到 `backend/tests/transfer/`）。

## 8. 明确不做

- **不改 `bank_backend` 一行**（含 risk.py 阈值、`POST /transfers` 契约）。
- **不动平台**：不给 `ToolInvocation`/`AgentToolHandle`/`gateway.handle` 加验证码字段（旧稿 P-1 作废），不动 `_confirmation_pause`（旧稿 P-2 作废），不改 `card`／`wealth`。
- **不做定时转账的创建**（`POST /transfers/scheduled`）：只保留列表（`transfer_scheduled`）与取消（`transfer_cancel`）；并且 prompt 明确要求模型**不要把预约意图当即时转账执行**（宁可如实说不支持）。**用户 2026-10-13 裁定：本次不做，登记为「待开发功能」——设计输入见 §12 D-1。**
- **不把验证码放进 arguments / AgentState / 账本 / 检查点**。
- **不改前端**（`frontend/src/App.vue` 与 `App.test.ts` 有与本次无关的**未提交**本地改动，谁都不许碰）。
- **不把 transfer 的图做成平台通用图**：`graph_builder` 只挂在 transfer 上。
- **不迁移工作树分支 `verify/transfer-fix` @ `2ce88bf`**：它的五处接入修复**已经由 `ce1d39d` 落进 dev**（factory / llm·decisions / workflow·nodes 三处随本次重写消失；`Account.from_balance_view`（`GET /accounts/{id}/balance` 只回 4 字段）在新 handler 里不再需要——直接读 payload dict；registry 断言那处见 §6.2；它新增的 `backend/tests/transfer/test_transfer_platform_wiring.py` 会随新结构改写）。**分支作废，不 merge、不 push。**
- **不新增第三个决策路径**：决策仍只有原生 tool calling 一条（spec1 已定）。

## 9. 风险

| 风险 | 处置 |
|---|---|
| 写不走网关 → 审计里没有这次 `POST /transfers` | 照 wealth 的既有形状（模块运行时的写也没有网关审计行）；SSE 收尾帧 + 业务日志留痕。要恢复网关审计就得给网关加验证码通道（旧稿 P-1），本次明确不做；若评审要求，改成「有码的写也走网关」＝重开 P-1 |
| 验证码泄漏到 SSE／审计／检查点 | 码只经 `turn.verification_code` 闭包进模块图，从不进 arguments/state；§6.1 第 8 条是回归测试。另外**模块的失败 summary 永不包含验证码原文**（`ToolFailure(CONFIRMATION_REQUIRED, "验证码只能是数字。")` 不含码，银行 1007 的 message 也不含码） |
| `requires_code` 与银行阈值漂移（模块算 False、银行仍要码） | 模块内常量 + 注释指向 `bank_backend/app/domain/risk.py`；§6.1 第 2 条参数化锁 4999.99/5000.00；**银行 1007 时把卡片升级成 `requires_code=True` 再挂**（§4.4）——「原样重挂」在 False 方向是坏的，会让用户永久补不上码 |
| 模块图漏发 `turn_status` → 被反复派发 | 收尾一律走 `_conclude_turn`（它写 `turn_status`）；§6.1 第 1/2 条覆盖 |
| 双击/重放导致两次转账 | 路由层 `Idempotency-Key`（已有测试）＋ 写幂等键在挂卡时冻结、点击复用（§3.4）；§6.1 第 9 条覆盖 |
| 「明天9点转 5000」被当作即时转账执行 | 用户已裁定本次不做预约创建（登记为待开发，§12 D-1）；prompt 明确要求不支持预约时如实说明，**拒绝把预约意图降级成即时转账**；§6.1 第 11 条钉住「银行写端点零调用」 |
| 模型直接执行取消定时转账（绕过确认） | `transfer_cancel` 在 schema 里可见，但 `run_call` 见到它**一律先挂确认卡**，只有用户点击才经网关执行；§6.1 第 6 条端到端钉住「点击前银行零写调用」 |
| 删私有层时误删仍被引用的东西 | `backend/tests/test_import_cycle.py` 与全量 pytest 兜底 |

## 10. 裁定（已定）

1. 走 A：只改 transfer 模块，`bank_backend` 与平台**都零改动**。
2. 验证码缝隙 = **平台已有的 `graph_builder(..., turn=…)` 关键字**；模块图直接读 `turn.verification_code`（旧稿的 `ToolInvocation.verification_code` 作废）。
3. 确认卡 = **平台 `kind="confirmation"`**（不是 card 的 `card_authorization`），形状由平台唯一实现 `_confirmation_pause` 产出，模块图通过 `_conclude_turn` 调用它（旧稿的 `confirmation_question()` 作废）。
4. 金额闸门：小额/中额/大额**统一挂卡**；`requires_code = amount_minor >= 500000`，银行 `CONFIRMATION_REQUIRED` 兜底。
5. 取消 = **本地收尾**，不调银行（转账在 `POST /transfers` 之前不存在）；取消**定时**转账则走网关的 `transfer_cancel`（可见工具，但图先挂确认卡；银行侧本来就不要 confirm/MFA）。
6. 模型只说元字符串与收款人姓名；账户/联系人 ID 由 handler 解析，歧义由 `transfer_selection` 动态选择解决。
7. 写路径：`POST /transfers` 走**模块自己的受控路径**（用 `graph_builder` 交进来的 `client`，幂等键挂卡时冻结），因为网关没有验证码通道。
8. 基线 = `dev` @ `b65ed9d`（含 `ce1d39d` 的五处修复）；`verify/transfer-fix` @ `2ce88bf` 作废（该分支与其工作树已于 2026-10-13 删除，内容逐字已含于 dev）。
9. 定时转账的**创建**本次不做，登记为**待开发功能**（§12 D-1）；本次只保留查询与取消。
10. **用户 2026-10-13 裁定**：① 取消定时转账**保持可达** —— `transfer_cancel` 留在模型 schema 里，图的 `run_call` 一律先把它拦成确认卡，点击后才经网关执行（**不采用**「删掉该工具、本版不交付取消」的备选）；② **不打开 `domain_scope`、不使用 `assembler`**（与 card 一致）；③ 实现方式定为 **Native**（在同一个会话里按 plan 逐任务实现，最后一次全新上下文整体 review）。**但用户同时明确要求「先别执行」—— 实现必须等用户发起，本 spec 与 plan 只作为待执行的设计。**

## 11. 执行顺序

1. **T-1** `modules/transfer/tools.py`（4 个读工具 + `transfer_cancel` + `transfer_prepare` 的 facts 契约）+ 写路径（§3.4）+ 单测（解析/歧义/限额/幂等）。
2. **T-2** `modules/transfer/workflow.py`（图：`decide`/`wait`/`process`、挂卡、点击消费、选择分支）+ 单测（小额/大额/取消/重挂/选择/双击）。
3. **T-3** 切声明（去掉 `turn`、挂 `graph_builder`）+ 删私有层 + 改 `__init__.py` docstring 与 README。
4. 改写既有测试（§6.2），全量 pytest。
5. 端到端手工冒烟（§7），结果归档。

## 12. 待开发功能（Deferred，已明确标记）

用户 2026-10-13 裁定：下面这条**本次不做**，但**明确登记为「等待开发」的功能**——将来接手时从这里读设计输入，不要再从零调研。

### D-1 定时转账的创建（`POST /transfers/scheduled`）

- **为什么不做**：本次范围是「把 transfer 从 turn 型改成 card 型」，不新增业务能力；预约创建要额外引入「把自然语言时间解析成绝对时间」这条链路（见下），风险与工作量都超出这次改造。
- **现状（银行侧已就绪，不用改）**：`../bank_backend/app/api/routes.py:264-288`（201）；`TransferService.create_scheduled(...)`（`../bank_backend/app/domain/transfer_service.py:228`，入参含 `execute_at: datetime`，:235），**风险闸门与即时转账完全一致**：`:256-259 decision = risk.classify(amount_minor)` → `risk.require_confirmation(decision, confirm=confirm, mfa_code=mfa_code, action="定时转账")`。→ 小额/中额/大额阈值、`confirm=true`、`mfa_code` 的规则原样复用，模块不需要另造一套。
- **接口**：`ScheduledTransferRequest`（`../bank_backend/app/api/schemas.py:48-54`）= `TransferRequest` + `execute_at`（**必须带时区**，schema 强制 tz-aware）。
- **要补的东西（预估约半天）**：
  1. 一个可见写工具 `transfer_schedule`（或把 `execute_at` 加进写路径），按 §3.4 的形状走**模块自己的受控路径**（有码的写不能走网关），幂等键同样在挂卡时冻结、点击复用。
  2. `TRANSFER` 的 `fields` 加 `execute_at`（`FieldMeta(label="执行时间", type="text")`），prompt 里声明「预约 = 先解析成绝对时间，再挂卡确认」。
  3. **最难的一步：模型不知道「现在」**。平台目前**没有**把当前时间注入任何 prompt（`backend/app/agents/` 里 grep 不到时间注入，只有内部 `utcnow()` 时钟）。所以「明天 9 点」这类相对时间要么由平台在 prompt 里给出 `now`（含时区），要么只接受绝对时间（如「2026-10-14 09:00+08:00」）。**先做这个决定，再动手。**
  4. 测试面：§6.1 第 11 条的加强版（预约意图 → 挂卡 → 确认后**只**调 `POST /transfers/scheduled`、`execute_at` 带 tz、幂等键复用）；建完能在 `transfer_scheduled` 列表里查到。
- **本次的兜底**（已在做）：prompt 明确要求模型**不要把预约意图降级成即时转账**；§6.1 第 11 条钉住「银行写端点零调用」；§9 风险表也有这一行。
