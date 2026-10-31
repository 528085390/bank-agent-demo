# 受控写路径契约（Write Facade）

- 状态：生效（A4）
- 日期：2026-10-13
- 更新：2026-10-23 订阅扣款并入资金写路径（W-5 的适用面 + 「谁调用谁」）；同日增加 W-6（日限额执行时复检）；W-6 的资金写信号改为正数 `amount_minor`（日累计表不再分操作类型）
- 更新：2026-10-30 AA 收款并入转账写路径：`create_aa`（发起，只生成收款请求，`amount_minor=None` 不占日累计）、`pay_aa`（付我的份额，真资金写，带正数 `amount_minor`，受 W-6 执行时复检）、`cancel_aa`（取消，不动钱）
- 实现：`backend/app/integration/write_facade.py`
- 相关：`docs/superpowers/specs/2026-10-13-architecture-optimization-roadmap-spec.md` §A4

## 1. 目的

资金写（转账、申购、赎回等会动钱的银行 POST）必须经过**同一道门面**：身份、幂等、验证码来源、错误映射、审计落点一致。模块不得长期各自直打裸 `BankClient` 做资金写。

本契约不是第二套 `BankClient`。传输、重试策略、信封解析仍只在 `BankClient`；Facade 只做写策略。

## 2. 硬规则

| # | 规则 | 说明 |
| --- | --- | --- |
| W-1 | 身份 = `bank_customer_id` | 发请求前必须有非空 `TrustedContext.bank_customer_id`；注入为银行 `user_id`（query + body）。缺绑定 → `NO_CUSTOMER_BINDING`，**不得**省略 `user_id` 让银行用默认客户。 |
| W-2 | 资金写必须有幂等键 | 键由运行时铸造，经 Facade 校验；缺/空 → `IDEMPOTENCY_KEY_REQUIRED`，请求不发出。Header `Idempotency-Key` 与 body `idempotent_key`（若业务体需要）由调用方/客户端按现有银行约定组装；Facade 保证键存在并交给 `BankClient`。 |
| W-3 | 验证码不进状态 | 验证码（`mfa_code` / `verification_code`）只存在于当次 HTTP 请求体与调用栈。不得写入 checkpointer、ledger、会话快照、模型上下文。审计载荷必须脱敏。 |
| W-4 | 错误映射一致 | 传输/银行失败仍经 `BankCallError` + `KIND_TO_ERROR_CODE`；策略失败用 `WritePolicyError`（`NO_CUSTOMER_BINDING` / `IDEMPOTENCY_KEY_REQUIRED` / `CONFIRMATION_REQUIRED`）。领域适配器再译成 `WealthBankError` / `TransferBankError` / `ToolFailure`。资金写不在 Facade 或 `BankClient` 内重试。 |
| W-6 | 日限额在执行时复检 | 确认卡冻结时的判定不等于执行时的判定：卡挂起之后，当日累计可能已被别的写推过 `DAILY_RED_THRESHOLD_MINOR`（`app/policy/types.py`，与 `classify_operation` 同一个常量，不是第二套阈值）。`WriteFacade.send` 在银行调用与 `fund_write_attempt` 之前，若这次写带了正数 `amount_minor`（真的动钱，这就是资金写信号）、没有验证码、且 `已累计 + 本次 > 阈值`，抛 `CONFIRMATION_REQUIRED`：银行不会被调用，也不留 attempt 行。读当日累计失败按「已超限」判（Fail-Closed，与 `transfer_prepare` 同一口径）。没接数据库的门面跳过这段（它无从知道当日累计）。 |
| W-5 | 审计落点 | **资金写（wealth / transfer / 订阅扣款）**：可选 `WriteFacade` audit hook（attempt / success / failure），载荷含 path、user_id、idempotency_key、脱敏 body。**Card action 变体**（prepare / authorize / execute）：身份约定同 W-1；审计由 Tool Gateway 打点（已有路径），不强制再走 Facade send——card 是银行 pending-action 模型，不是直接资金 POST。 |

## 3. 谁调用谁

```text
wealth HttpWealthBank.purchase/redeem  ──► WriteFacade.send ──► BankClient.request
transfer submit_transfer/create_scheduled/cancel_scheduled + create_aa/pay_aa/cancel_aa ──► WriteFacade.send ──► BankClient.request
subscription 扣款点击（charge）──► WriteFacade.send ──► BankClient.request（取消/暂停/恢复仍直连）
card tools (execute/authorize/cancel) ──► require_customer_id + BankClient（Gateway 审计）
只读 GET / 非资金 POST（问卷等）        ──► BankClient（可继续绕过 Facade）
```

## 4. 验证码来源

- 仅来自用户当场输入（HITL `/decide` 或模块确认入口），运行时传入写方法参数。
- Facade / 适配器**不得**从配置、环境或数据库填入演示码。
- 是否需要验证码由各适配器的 `write_authorization`（或 card 银行门禁）决定；Facade 不解读业务授权形态，只保证若 body 含秘密字段则审计脱敏。
- Facade 只用「这次调用有没有带码」这一个布尔量做 W-6 的复检：它不决定要不要码、不校验码的对错（那是银行的事），也不留存码。

## 5. 非目标

- 不替代 Tool Gateway；不把读工具改造成写 Facade。
- 不实现 bank_backend 归属/行锁/真 MFA（见路线图 D1）。
- 不要求 card 的 prepare→authorize→execute 改成 `WriteFacade.send`（共享身份与审计约定即可）。
