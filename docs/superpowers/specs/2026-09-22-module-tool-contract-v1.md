# 模块工具契约 v1
> 撰写日期：2026-09-22｜读者：领域工具实现者。当前六个领域包只有模块声明，生产工具尚未注册；
> 本文定义将来接入工具时要遵循的命名、声明、输入输出和测试约定。
> 姊妹文档：[模块接入接缝契约](2026-09-22-module-integration-seam-contract.md)——那份管**接缝**
> （注册、身份、适配器、错误码、信封、显示协议），本文管**逐个工具**。**两份合起来才是完整契约。**
>
> **规则等级**（判定标准是**强制手段**，不是重要性；完整定义见接缝契约 §0）：
> **MUST** = 能指到一处**今天就已存在**的机械强制（注册表校验或一条测试），本文每条 MUST 都注明强制点；
> **MUST（已实现校验）** = 由工具协议与注册表提供机械校验；这些校验当前已存在于代码中。
> **SHOULD** = reviewer 在 code review 时把关，不写测试；
> **DEFERRED** = 本期不做，已写明原因。

---
## 1. 本文怎么用
**接缝契约规定"信封长什么样"，本文规定"你这个工具的每一个格子填什么"。** 接缝契约里的机制
（错误码、`side_effect`、幂等键、`output_schema`、敏感值去向）都是**空槽**——不填值就没有意义。
本文就是那份填表说明。

**SHOULD：在写工具实现之前填，不是之后补文档。** 理由很具体：`side_effect` 决定这个工具
**能不能重试**。先写实现、后补声明，重试逻辑就建立在"不知道有没有副作用"之上。

**SHOULD：逐工具维护。** 每新增一个生产工具，就在本文记录它的输入输出、安全属性和测试数据。
逐工具契约应随生产工具一并维护，未实现的工具不要标为已交付。

---
## 2. 命名约定
接缝契约只做**机械检查**（工具名全局唯一），抓不到"两个模块用不同风格命名同一件事"。所以约定写在这里。
### 2.1 工具名
**MUST：格式 `<模块名>_<操作名>`，全小写，下划线分词，不得包含 `.` 或任何非标识符字符。** 查询工具用统一的资源名，写工具用动宾操作名；
单词操作（如 `create`、`freeze`）可以不再追加名词。

工具名就是模型看到的 `function.name`，原样上行，中间没有编码层。上游接口拒绝含 `.` 的函数名
（错误码 11152，表现为一次已经开始的会话里的 503），所以带点的声明在本地就失败。

```text
account_balance        # 查询类：资源名
transfer_create        # 写类：动宾结构
card_freeze            # 写类：单词操作
```

> **以上只是格式示例，不是工具清单**；你的工具名以你模块的实际设计为准，格式按下面的规则。

| 规则 | 等级 | 强制点 / 理由 |
| --- | --- | --- |
| 工具名非空 | **MUST** | `ToolSpec.__post_init__`（`app/tools/protocol.py`）抛 `ValueError` |
| 工具名是小写标识符，不含 `.` | **MUST** | `function_name`（`app/tools/schema.py`）抛 `ValueError`，由 `function_schema` 调用；`test_domain_protocol.py::test_a_declared_name_that_is_not_an_identifier_is_refused`、`test_card_module.py::test_every_declared_card_tool_name_is_a_dot_free_identifier` |
| 工具名全局唯一 | **MUST** | `ToolCatalog.build`（`app/tools/catalog.py`）抛 `ValueError`；`test_tool_catalog.py::test_a_catalog_refuses_a_duplicate_declaration` |
| 同一模块内不重复声明同一工具名 | **MUST** | `AgentTools.__post_init__`（`app/tools/catalog.py`：同一个 agent 的 specs 里重名直接抛错）、`ModuleSpec.__post_init__`（`app/modules/base.py`：`tools` 的键必须等于 `spec.name`）。`DomainDeclaration.tools` 不再作为工具清单 |
| `grants` 只能引用已声明的工具 | **MUST** | `ToolCatalog.build`；`test_tool_catalog.py::test_a_catalog_refuses_a_grant_for_an_undeclared_tool` |
| 前缀**等于**模块名，用 `_` 连接 | **SHOULD** | 无机械检查。全局唯一性只挡字面撞名，**不检查前缀** |
| 查询用资源名，写操作用动宾结构 | **SHOULD** | 无机械检查 |
| 同一概念在所有模块里**用同一个词** | **SHOULD** | 无机械检查；见 §2.3 |
| 不复述模块名；查询不加 `get_`/`list_`/`query_` 前缀 | **SHOULD** | `card_freeze` 而非 `card_freeze_card`；查询直接用名词 |
### 2.2 参数名

| 规则 | 等级 | 强制点 / 理由 |
| --- | --- | --- |
| 参数必须满足声明的 `parameters` JSON Schema | **MUST** | `ToolGateway._validate`（Draft 2020-12）；`test_arguments_are_checked_against_the_declared_schema` |
| 声明的 schema 本身必须是合法 JSON Schema | **MUST** | `gateway.py` 构造校验器失败即抛 `ValueError`，不放行 |
| 金额参数声明为 `{"type": "integer", "exclusiveMinimum": 0}` | **MUST** | 声明后浮点会被 Gateway 第一道校验拒绝（同上） |
| **绝不声明 `confirmed`、`idempotency_key`、`call_id`、`agent_id`** | **MUST** | 接缝契约 §3.3 第 9 条：`build_registry()` 抛 `ValueError`；`test_module_registry.py::test_a_tool_may_not_declare_a_name_the_base_injects`。**这四个名字没有一个该来自模型**：`call_id` 与 `idempotency_key` 由 Supervisor 铸，`agent_id` 由 Gateway 铸，而 `confirmed` **谁都不注入**——它是接缝"不让模型替用户确认写操作"的立场本身（接缝契约 §1.3）。声明成参数就是**把伪造权交给模型** |
| 金额字段名以 `_minor` 结尾 | **SHOULD** | 无测试断言。接缝契约 §4.3 的命名约定，是防 100 倍资金错误的主要防线 |
| 引用银行侧资源用 `<资源>_id`；引用人用 `<角色>_name`，**不要用 `name`** | **SHOULD** | 与 bank_backend 字段名保持一致 |
| 日期用 `<语义>_date`，格式 `YYYY-MM-DD` | **SHOULD** | 无机械检查 |
| 分页用 `limit` + `offset` | **SHOULD** | bank_backend 用 limit/offset |
| 同一个东西在两个模块里必须同名 | **SHOULD** | 无机械检查。跨模块调用与前端依赖这个一致性 |
### 2.3 概念词表（推荐用词，不强制）
**SHOULD：新概念优先在这里选一个词。** 词表是**推荐用词**，不强制：它的价值是
"**同一个概念别用两个词**"，而不是"必须用我选的那个词"。**不填词表不构成交付缺陷**，
但同一模块内自相矛盾（一半 `create` 一半 `add`）**review 时应指出**。

| 概念 | 推荐用词 | 不要用 |
| --- | --- | --- |
| 创建 | `create` | `add`、`new`、`make` |
| 查询单个 | 名词本身 | `get`、`fetch`、`query` |
| 查询列表 | 复数名词 | `list`、`query_all` |
| 取消 | `cancel` | `delete`、`remove`、`abort` |
| 修改 | `update` | `modify`、`edit`、`change` |
| 冻结 | `freeze` | `lock`、`suspend`（`suspend` 留给暂停） |
| 暂停 / 恢复 | `pause` / `resume` | `stop`/`start`、`hold`/`unhold` |
| 收款人 / 收款方 | `payee` | `receiver`、`payee_party` |
| 账户 | `account` | `acct`、`card_account`（卡片是 `card`） |
| 交易 | `transaction` | `trade`（`trade` 留给理财）、`deal` |
### 2.4 追问字段名
**SHOULD：追问字段名也走上面两张表**——用户会看到字段的展示名，而字段名会出现在模型提示词里。
未在 `DomainDeclaration.fields` 里声明的字段有兜底（展示名 = 字段名本身），
所以**忘声明的后果是用户看到 `recipient_account_number` 这种内部标识**。

---
## 3. 工具清单表（每个模块填自己一行）
**SHOULD：每个模块填自己一行。** 这张表让命名冲突与能力归属冲突在写代码之前暴露出来——
注册表能拦下字面撞名，拦不下"两个模块各包了一层查余额但名字不同"。

| 模块 | 工具名 | bank_backend 端点 | 方法 | effect | 状态 |
| --- | --- | --- | --- | --- | --- |
| account | `account.balance` | `/accounts/balance` | GET | READ | 示例，**未冻结** |
| transfer | `transfer.create` | `/transfers` | POST | WRITE | 示例，**未冻结** |
| bill | _待填_ | | | | 未开始 |
| wealth | _待填_ | | | | 未开始 |
| card | _待填_ | | | | 未开始 |
| subscription | _待填_ | | | | 未开始 |
| life_event | _待填_ | | | | 未开始 |

> 上述 `account.balance` 和 `transfer.create` 仅为格式示例，未在生产代码中声明、实现或接入。

**填写时注意：**

1. **SHOULD：一个工具只打一个端点。** 打多个端点说明它是两个工具，或者它需要事务——后者在接缝层不支持。
2. **SHOULD：先查别人的行。** 你要的端点已被别的模块的工具打了，要么 `grants` 共享它（接缝契约 §3.2），
   要么说明你的用法为什么不同。
3. **MUST：effect 必须是 `READ` 或 `WRITE`。** 强制点是接缝契约 §3.3 第 7 条的
   **注册表校验**：非法取值会被 `build_registry()` 拒绝。
   **不要依赖默认值**——`side_effect` 默认是 `READ`，忘声明会让一个写工具**静默地按读工具处理**。

   > ⚠️ **注册表拦不下"省略"。** 它只能拒绝一个**非法取值**；`"READ"` 是合法值，
   > 不写也合法。所以"声明了 effect"这件事没有机械强制——见 §4"副作用"里那段警告。

   > ⚠️ **一处容易误认的强制点**：`app/storage/models.py` 与 `migrations/schema_v2.py` 里确实有
   > `CHECK (side_effect IN ('READ','WRITE'))`，但它约束的是 **`orchestration_step` 表**
   > （旧表），**不是 `ToolSpec`**。`ToolSpec` 已有 `side_effect` 字段，
   > 所以本规则的强制点是注册表校验，**仍然不是那张死表的 `CHECK`**——
   > 它管不到工具声明，别拿它当依据。

---
## 4. 逐工具契约模板
**每个工具一节，复制下面的模板填写。** 五项都要填，不要跳过。

````markdown
### <工具名>

| 项 | 值 |
| --- | --- |
| 模块 | |
| bank_backend 端点 | `METHOD /path` |
| 端点文档 | `bank_backend/API接口文档.md` 第 __ 节 |
| effect | `READ` / `WRITE` |
| 状态 | 未开始 / 草稿 / **已冻结** |

#### 1. 输入（`ToolSpec.parameters`）

- Schema 起手式 `{"type": "object", "properties": {}, "required": []}`（**MUST** 通过 Gateway 的 Draft 2020-12 校验）
- 金额字段 `{"type": "integer", "exclusiveMinimum": 0}`（**MUST**），名字以 `_minor` 结尾（**SHOULD**）
- 每个参数的来源：**全部由模型填**——`arguments` 里只有模型给的参数。幂等键与
  `call_id`/`agent_id` **都不是参数**，它们不在 `arguments` 里，而是从 `ToolCall`/`invocation`
  上取（见 §4.1 的 handler 骨架）
  （**MUST**：不得把 `confirmed`/`idempotency_key`/`call_id`/`agent_id` 声明成参数——
  `build_registry()` 会抛 `ValueError`，见 §2.2）
- **`user_id` 不是注入的，也绝不允许声明成工具参数。** 身份走 `TrustedContext`：
  handler 必须读 `invocation.context.bank_customer_id`（§4.1 骨架），**不能**从
  `invocation.call.arguments` 里取。⚠️ **注册表拦不住这个**——`user_id` / `bank_customer_id`
  **不在** `RESERVED_PARAMETER_NAMES` 里（那里只有 `confirmed`、`idempotency_key`、`call_id`、
  `agent_id`），所以一个把 `user_id` 声明成参数的工具**会通过注册表校验**，然后由**模型**填值。
  **这是逐工具 review 的必查项**：`parameters` 里出现 `user_id`/`bank_customer_id` 就拦下来。

#### 2. 输出

- `facts` 由 `ToolSpec.output_schema` 约束，起手式 `{"type": "object", "properties": {}, "required": []}`
- 这个工具**被别的模块消费吗**？是 → `output_schema` 必填且**不能改**；否 → 可以留 `None`，
  但那就意味着**别的模块不得读它的 `facts`**（**SHOULD**）
- **`summary`**（**SHOULD**）：中文一句话结论模板，例如"活期账户余额 8,320.00 元。"
  **只放结论**，不放 ID、不放账号、不放内部字段名；它会进模型上下文与 `subtask_end` 事件（接缝契约 §4.6）
- **`business_ids`**（**SHOULD**）：只放**稳定、可被用户引用**的公开业务号（如 `reference_no`/`display_id`），
  不能是数据库主键。它会在 `final.business_ids` 里以**纯值**形式给用户看
- **`display`：块协议 DEFERRED。** 完整的显示块协议（四种块 `text`/`fields`/`table`/`notice` 与块级字段约束）
  本期**不冻结形状**。模块可以留空 `{}` 或最小结构。
  **前端今天渲染的是 `summary`，外加 `display.blocks` 里的 `text` 块**；
  `fields`/`table`/`notice` **会被跳过**（未知类型降级，不报错也不白屏）——
  写了这三种块用户看不到，别以为不报错就等于生效。接缝契约 §4.7.1。
  原因：显示协议未定稿，冻结形状会阻塞模块并行。
  **默认选择就是空 `blocks` + 一句好 `summary`。**

> **最常犯的错**：把账号、对手方、内部 ID 写进 `summary`。`summary` 会进模型上下文，
> 意味着它会出现在发给外部 LLM 的请求里。**`summary` 只放结论，不放标识。**
> 四个字段的去向由接缝契约规定，不要在这里重复。你只需要回答两个问题：
> 这个值**该让用户看到**吗（→ `display`）；该**让模型知道**吗（→ 写进 `summary` 的结论句）。

> ⚠️ **`business_ids` 的偏差要如实知道**：接缝契约 §4.6 的目标是"`business_ids` 不进模型上下文"，
> **但今天它确实进**——`_conclude` 把它写进 `domain_results`，而领域提示词把整个
> `domain_results` 序列化出去（接缝契约 §4.6.1，有测试钉住）。
> **所以不要把敏感业务号当成"只给客户端"就可以随便放**：今天它同时到模型。
> 精确值该进 `summary` 的进 `summary`；`business_ids` 只放**本来就可以给用户看**的公开业务号。

#### 3. 副作用

- `side_effect` = `READ` / `WRITE`（**MUST**，见 §3）

> ### ⚠️ 声明你的工具是不是写工具——**这是本文最重要的一条**
>
> **一个写工具如果不声明 `side_effect=SideEffect.WRITE`，整条链路就会把它当成读工具。
> `READ` 恰恰是默认值，所以"什么都没写"是最省事、也最危险的选择。
> 注册表能拒绝一个非法取值，但拒绝不了一次省略。**
>
> **省略之后具体丢什么**（按代码逐行核对，不是笼统的"不安全"）：
>
> 1. **丢掉"无键拒发"这道 fail-closed——只在"没经过 Supervisor 的调用方"身上丢。**
>    `BankToolAdapter.invoke` 只在 `spec.side_effect == SideEffect.WRITE` 时才检查幂等键；
>    声明成 `READ` 的写工具**直接跳过这段**。走 Supervisor 时这不产生任何差别：
>    `_run_tool` 对**每个**工具（读也包括）都在重试循环之外铸一次键，所以**键总是在**。
>    真正丢掉的是**绕开 Supervisor 的调用方**（测试、脚本、跨模块调用、将来任何新入口）：
>    它们没有键，声明 `WRITE` 时适配器**在任何 HTTP 之前**就拒绝，声明 `READ` 时调用照常发出，
>    银行被调用而这次调用**没有 `Idempotency-Key`**；若它瞬时失败，`_may_retry` 看到 `READ`
>    会放行重试，第二次**无键**调用就可能重复那笔写。
>    （`tests/test_write_safety.py::test_a_write_without_a_key_is_refused_before_any_request`
>    用 `assert ran == []` 和 `assert client.requests == []` 锁住的就是这条。）
> 2. **`_may_retry` 的 `WRITE` 分支经适配器不可达，所以它不是"省略声明"的另一条后果。**
>    适配器构造的 `IDEMPOTENCY_KEY_REQUIRED` 结果**不带 `retryable`**（取默认 `False`），
>    而 `_may_retry` 在**到达**那个分支之前就先返回了；它只对**自己抛
>    `ToolFailure(IDEMPOTENCY_KEY_REQUIRED, retryable=True)` 的 handler** 生效，
>    而生产代码里没有这样的 handler（`tests/test_write_safety.py` 里
>    `test_a_write_failing_for_a_missing_key_is_never_retried` 的 docstring 就是这么说的）。
>    **所以唯一一条真实损失就是上面的 fail-closed 拒发。**
>
> **那为什么还必须声明**：因为"有键"是**上游的善意**，不是接缝能强制的。
> 今天只有 Supervisor 会铸键，可接缝同时也要挡住**没经过 Supervisor 的调用方**——
> 对那种调用方，声明就是**唯一**让它被拒绝而不是被执行的东西。
> 一条只在"所有人都守规矩"时才安全的保证不是保证。
>
> **一句话记住要做什么**：写工具就显式写 `side_effect=SideEffect.WRITE`，
> 并在 §3 清单表的 `effect` 列填 `WRITE`。**不要依赖默认值。**
> 这是本文唯一一条"没有机械强制、只能靠你读到这里"的规则——
> 正因为如此，它也是最容易漏、漏了最贵的一条。
>
> 如果你在**读**代码而不是写代码：看到 `side_effect` 缺省（等于 `READ`）的工具，
> 先问一句"它真的只读吗"。**缺省不是声明。**

- **仅 WRITE 需要**幂等策略（**SHOULD**）：

| 问题 | 回答 |
| --- | --- |
| 幂等键怎么传？ | 随请求头 `Idempotency-Key`（bank_backend 所有写端点都支持） |
| **重试时复用同一个键吗？** | **必须复用**——这是接缝契约 §4.5.2 的硬要求 |
| 同一个键重复提交，bank_backend 返回什么？ | （填实测结果，不要猜） |
| 用户的"又点了一次"和"重试"怎么区分？ | 前者是新一轮请求（新键），后者是同一次调用（同键） |

- 重复调用语义（**SHOULD**）：用户连点两次"提交" → ？；同一请求被重试一次 → ？；
  同一幂等键在 24 小时后再次使用 → ？；涉及多张表的操作中途失败 → **接缝层不支持跨端点事务**，
  要么单端点完成，要么在"错误"项声明补偿动作
- 重试策略（**SHOULD**）：`retryable=True`（网络/超时/限流/5xx）时 READ 重试一次、WRITE 仅当有幂等键时重试；
  `retryable=False`（业务拒绝）**不重试**——重试不会让"余额不足"变成"余额充足"（接缝契约 §4.5.3）
- 需要确认的工具（**SHOULD**）：接缝层不提供确认机制（接缝契约 §1.3、§10 风险 11），
  用既有的 `ask` interrupt + `FieldMeta` 实现。写清：需要确认吗、问哪些字段（写进 `DomainDeclaration.fields`）、
  用户答什么才算确认、**确认能被模型绕过吗**

> ⚠️ 如果"确认"只是让模型在参数里传一个 `confirmed: true`，那**它不构成确认**——模型可以自己填。
> 必须依赖**用户真实输入过**这个事实（`ask` 的 resume 路径）。

#### 4. 错误

| 触发条件 | `error_code` | `retryable` | 用户文案 |
| --- | --- | --- | --- |
| | 使用全局 `ErrorCode` 中已登记的码；新增码必须先加入共享枚举和文案表 | | |

- **SHOULD：错误码只取全局 `ErrorCode` 已登记的码。** 新增码必须同时加入 `ErrorCode`、文案表与
  错误码映射测试，不许只在代码里用
- ⚠️ **新增码不等于你可以自己改共享文件。** `ErrorCode` 在 `app/tools/protocol.py`、文案表在
  `app/modules/base.py`——接缝契约 **§3.6 明确禁止模块修改这两个文件**（以及 `app/tools/*` 其余部分），
  所以"加一个码"必须走**接缝契约 §6.3 的变更流程**（写清原因、受影响字段、调用方、迁移方式，
  由接缝负责人确认），不是在自己的 PR 里顺手加
- **SHOULD：`retryable` 只标真正可重试的失败。** 业务拒绝标成可重试只会重复同一次失败

#### 5. 测试

| 用例 | 输入 | 期望 |
| --- | --- | --- |
| 正常 | | `status=SUCCEEDED`，`facts` 通过 `output_schema` |
| 身份未绑定 | `bank_customer_id=None` | `NO_CUSTOMER_BINDING`，**未发 HTTP** |
| 上游拒绝 | FakeBankClient 返回 4xx + 业务 code | 对应 `error_code`，**未重试** |
| 上游可重试失败 | FakeBankClient 连续两次抛超时 | 重试一次后 `UPSTREAM_UNAVAILABLE` |
| 输出违规 | handler 返回不符合 schema 的 `facts` | `OUTPUT_SCHEMA_VIOLATION` |
| （WRITE）幂等 | 第一次可重试失败，第二次成功 | 两次请求的 `idempotency_key` **相同** |
````

### 4.1 handler 骨架：身份与幂等键都只能从 `invocation` 里取

**照抄这一段**（写工具尤其）：

```python
def handler(invocation, client):
    return client.request(
        "POST", "/transfers",
        user_id=invocation.context.bank_customer_id,          # identity: never from arguments
        idempotency_key=invocation.call.idempotency_key,      # the retry-safe key
        json={...},
    )
```

**handler 漏掉 `idempotency_key` 会静默丢掉整条保证。** 适配器**不检查** handler 有没有转发它——
`ToolCall` 确实带了键，所以接缝契约 §4.5.1 那道 fail-closed 检查照样通过，**没有任何测试会失败**，
除非你这个模块在 §4「测试」项里写下最后那条 WRITE 幂等断言（"两次请求的 `idempotency_key` 相同"）。
换句话说：键由 Supervisor 铸、由 `ToolCall` 携带，**但只有 handler 能把它放到线上**——
`BankToolAdapter` 从头到尾没有读过这个键，它只在写工具**缺键**时拒绝。

> **读示例不能照抄到写工具上。** `backend/tests/test_domain_harness.py` 里能看到的
> `client.request(...)` 示例是**读**（`"GET", "/accounts/balance"`），它的
> `idempotency_key` 本来就可以是 `None`——那是诚实的读，不是写 handler 的模板。
> 仓库里也**没有生产的写 handler** 可供照抄（`app/` 里今天没有 `ToolSpec(...)`）。

---
## 5. 已填示例
### 5.1 `transfer.create`（写）
> **示例，非真实交付。**

**模块** `transfer`｜**bank_backend 端点** `POST /transfers`｜**effect** `WRITE`｜**状态** 示例，非真实交付

- **输入**：`payee_account: string`、`amount_minor: integer (exclusiveMinimum: 0)`、
  `payee_name: string`（可选）——**不声明 `confirmed`**（§2.2）
- **输出**：`output_schema` = `{"transfer_id": string, "status": string, "amount_minor": integer}`；
  `summary` = `已向张伟转账 500.00 元，交易号 T-20260922-0001。`；
  `business_ids` = `{"transfer_reference": ...}`（用户可引用的 `reference_no`/`display_id`，不是主键）；
  `display` 本期留 `{}`
- **副作用**：WRITE。幂等键随 `Idempotency-Key` 头传递；**重试复用同一个键**；
  同键重复提交返回**同一笔**转账（不是新的一笔）
- **错误**：`NO_CUSTOMER_BINDING`、`INSUFFICIENT_BALANCE`、`INVALID_STATE`、
  `IDEMPOTENCY_KEY_REQUIRED`（无键拒绝）、`CONFIRMATION_REQUIRED`；均 `retryable=False`
- **测试**：§4 的六条 fixture 全填，其中 WRITE 幂等用例断言两次请求的 `idempotency_key` 相同
- **确认**：**需要**。用 `ask` 问 `payee_account` + `amount_minor` + `payee_name`，
  由用户在界面上真实输入后 resume 才继续。**不靠模型传 `confirmed`**

> **这个例子说明为什么接缝层必须做幂等键**：没有它，一次超时重试就是**两笔转账**。
> 而 `call_id` 做不到（它每次重试都变，接缝契约 §4.5.2）。

---
## 6. 模块填写状态

> **2026-10-19 更新：下表是 2026-09-22 首次填写时的快照，保留作历史记录。当前实际状态见本节末尾的「已交付工具」。**

**2026-09-22 的快照。** 示例工具不属于已交付功能；六个已注册领域当时都没有生产工具。

| 模块 | 清单表 | 逐工具契约 | 状态 |
| --- | --- | --- | --- |
| account | 示例 | 示例 | 未注册；仅供示例 |
| transfer | 未开始 | 未开始 | 只有模块声明，无工具 |
| bill | 未开始 | 未开始 | 只有模块声明，无工具 |
| wealth | 未开始 | 未开始 | 只有模块声明，无工具 |
| card | 未开始 | 未开始 | 只有模块声明，无工具 |
| subscription | 未开始 | 未开始 | 只有模块声明，无工具 |
| life_event | 未开始 | 未开始 | 只有模块声明，无工具 |

工具协议、错误码、写安全和信封校验已由底座提供，但这些能力在没有生产工具时不会执行。接入业务模块仍需完成每个工具的声明、handler、身份绑定、API 测试和端到端集成。

### 已交付工具（2026-10-19 实测，按 `ModuleSpec.tools` 键集统计）

| 模块 | 生产工具数 | 工具名 |
| --- | --- | --- |
| transfer | 5 | `transfer_accounts` / `transfer_contacts` / `transfer_scheduled` / `transfer_prepare` / `transfer_cancel` |
| bill | 4 | `bill_query_transactions` / `bill_analyze_comprehensive` / `bill_send_report` / `bill_send_report_execute`（最后一个为内部工具，不进模型可见面） |
| wealth | 7 | `wealth_products` / `wealth_product` / `wealth_accounts` / `wealth_holdings` / `wealth_holding` / `wealth_risk_profile` / `wealth_order` |
| card | 12 | `card_cards` / `card_detail` / `card_restrictions` / `card_apply` / `card_update_limit` / `card_update_restrictions` / `card_freeze` / `card_unfreeze` / `card_report_loss` / `card_release_loss` / `card_execute` / `card_cancel`（后两个为内部工具） |
| subscription | 7 | `subscription_list` / `subscription_detail` / `subscription_cancel` / `subscription_pause` / `subscription_resume` / `subscription_charge` / `subscription_execute`（最后一个为内部工具） |
| life_event | 0 | — 仍只有声明，无工具 |

逐工具契约以各模块 `tools.py` 的 `SPECS` 为准；接口级验收见 `D:/ai_project/agent/_accept/TEST_PLAN_card_wealth_chat.md` 与 `REPORT_api_acceptance_local.md`。

---
## 7. 冻结与变更
1. **SHOULD：逐工具增量冻结。** 一个工具填完 §4 全部 5 项、且对应测试全绿，把状态改成"已冻结"。
   **冻结是逐工具的，不是逐模块的。**
2. **SHOULD：冻结后改契约要走变更流程**：写清"原因、受影响字段、调用方、迁移方式"，
   由接缝负责人确认（接缝契约 §6.3 第 3 条）。
3. **SHOULD：`output_schema` 一旦被别的模块消费，改动即为破坏性变更**——必须同时通知消费方。
4. **SHOULD：新错误码、新概念词都要登记**（§2.3、§4 的"错误"项），不许只在代码里用。
5. **DEFERRED：安全与审计事项单独登记，不在本期实现。** §4"副作用"里的确认要求、幂等语义和字段去向
   仍必须填写，因为它们是模块间接口信息；但确认凭据、防重放与并发防重、字段级脱敏、审计留痕、访问控制
   和保留期限属于接缝契约 §7 的后续安全/审计工作，不能把本文件的填写状态当作这些能力已经完成。

---
## 附录：本文与接缝契约的对应

| 本文的项 | 接缝契约里的机制 |
| --- | --- |
| §4 "输入" | `ToolSpec.parameters`（Gateway 校验） |
| §4 "输出" | `ToolSpec.output_schema` 约束 `facts`；`summary` 是基座消费字段 |
| §4 "副作用" | `ToolSpec.side_effect`；`ToolCall.idempotency_key`；`_run_tool` 的单层重试 |
| §4 "错误" | `ToolResult.error_code`；`ErrorCode` 枚举 |
| §4 "测试" | 单域脚手架 `run_domain` + `FakeBankClient` |
| §4 确认（副作用内） | **接缝层不提供**，用既有 `ask` interrupt——§1.3 |
