# 模块接入接缝契约

> 版本：v3｜撰写日期：2026-09-22｜状态核对：2026-09-25
> 读者：领域模块实现者。本文约束模块与 Agent 底座的接入接口。
>
> **本文只定义模块与底座之间的接口，不描述当前业务功能是否已交付。**
> 逐工具怎么声明看 [`module-tool-contract-v1.md`](2026-09-22-module-tool-contract-v1.md)。
>
> ---
>
> **当前实现状态**：`build_registry()` 已就位，生产注册表有六个领域声明、`catalog.names() == ()` 且没有 handler。
> 工具协议、写安全和结果信封的接口约束已在代码与测试中实现，但没有任何生产业务工具使用这些机制。
> `ToolSpec.side_effect` / `output_schema`、`ToolCall.idempotency_key`、`ToolResult.error_code` 和
> `TrustedContext.bank_customer_id` 等机制已存在于协议与注册代码中；生产模块当前未声明这些工具机制。
>
> **接口机制存在 ≠ 业务工具已接入。** 当前**没有任何生产模块声明工具**
> （全仓 `app/` 里没有 `ToolSpec(...)`，六个模块包都只声明 `ModuleSpec(name=…, declaration=…)`），
> 所以 `side_effect` / `output_schema` / 幂等键这套机制目前是**惰性的**。
> 每个新增写工具都必须显式声明 `side_effect=SideEffect.WRITE`——见 §4.5.1 的警告。

---

## 0. 规则的等级

**每条规则都标了等级。没有等级的文字不进入验收标准。**

| 级别 | 含义 | 谁强制 | 判定标准（严格遵守） |
| --- | --- | --- | --- |
| **MUST** | 违反即缺陷 | 机器或契约调用方 | 可由注册表校验、运行时代码或测试强制；未自动强制的部分由实现者遵守并在评审中核对 |
| **SHOULD** | 偏离需说明理由 | reviewer | code review 把关，不写测试 |
| **DEFERRED** | 当前契约范围不含 | 无 | 已登记在 §7；实现业务工具前应明确处理相关能力 |

规则是否由机器强制，以各条规则注明的代码或测试为准。`MUST` 也可能要求调用方或实现者提供信息，不能仅凭等级推断已有自动校验。

**MUST 的降级规则（硬规则）**：

1. **指不到机械强制就降为 SHOULD。** 这让 MUST 可证伪，也顺手解决文档漂移——
   一条规则一旦无法被强制执行，它就会自动掉级，而不是留在文档里当装饰。
2. **强制点必须作用于本规则的主体。** 一处约束**别的表、别的对象**的校验不算数。
   典型反例（**撰写时的反例，现已成为历史**）：`app/storage/models.py` 与
   `migrations/schema_v2.py` 里确有 `CHECK (side_effect IN ('READ','WRITE'))`，
   但它约束的是 **`orchestration_step` 死表**，而撰写时 `ToolSpec` **没有** `side_effect` 字段——
   它管不到工具声明，不能当作"effect 必须是 READ/WRITE"的强制点。
   `ToolSpec.side_effect` 已存在，真正的强制点是
   `build_registry()`（§3.3 第 7 条）；那张死表上的 `CHECK` 今天依然管不到工具声明，
   所以它**仍然**不能作为强制点被引用。
3. **强制点必须能被指向具体符号**，不能是"某处应该有校验"。

**但有一类规则我明确降为 SHOULD**：它们管的是**协作纪律**而不是接口形状，
没有也不该有机械强制（§3.6、§4.4.1、§4.6、§4.7 里的相应条目）。
把它们标成 MUST 会让"MUST"这个词失去意义。

本文**不含代码行号**。要定位代码请按符号名搜。行号会随提交过期，符号名不会。

---

## 1. 这份契约解决什么问题

### 1.1 当前结构

生产工具通过模块注册表汇总，由共享 gateway 和 adapter 执行。协议提供工具声明、授权、身份注入、错误分类和结果信封；这些机制本身不代表业务工具已经接入。当前生产工具目录为空。

| 事实 | 位置（按符号名） |
| --- | --- |
| 全局共享 gateway，工具由模块注册表装配 | `app/runtime.py` 的 `build_tool_gateway`：调用 `build_registry()` 并使用 `BankToolAdapter`；当前模块目录为空 |
| 全局只有一个 adapter 实例，所有工具共用一个 | 同上 |
| 领域 roster 从模块注册表派生 | `app/agents/domain.py` 的 `DOMAIN_ROSTER = build_registry().roster`；模块清单位于 `app/modules/__init__.py` |

现有领域增加工具时，将声明与 handler 放入对应领域包；注册表负责汇总工具、授权和 handler。新增领域时需要更新模块清单。

`ToolSpec.side_effect`、`ToolCall.idempotency_key`、`ToolResult.error_code` 和 `ToolSpec.output_schema` 均已存在于协议中；这些机制只有在模块声明并注册工具后才会用于生产调用。

### 1.2 接入约束

1. **隔离模块改动面**：工具声明和 handler 归属对应领域包；注册表负责汇总。
2. **四个跨模块约定各只有一份实现**：身份、传输与错误映射、金额单位、结果信封。
3. **模块能在不依赖基座、不依赖 bank_backend、不依赖真实模型的前提下独立验证**。
4. 写类工具必须声明副作用和幂等策略；工具端点是否支持幂等须按实际 API 能力核对。
5. 结果信封字段在模型、checkpoint、SSE、前端和日志间按 §4.6 的规则流转。

### 1.3 非目标（DEFERRED，明确不做）

| 不做的事 | 归属 |
| --- | --- |
| 具体银行业务工具的实现 | 由领域模块按 `module-tool-contract-v1.md` 接入 |
| 逐工具的副作用/幂等/确认声明**内容** | 在 `module-tool-contract-v1.md` 逐工具填写 |
| **在接缝层强制确认 / MFA** | **DEFERRED，见下方说明** |
| **`display` 的完整块协议** | **DEFERRED，见 §4.7** |
| **跨模块消费的版本协议** | **DEFERRED，见 §4.4.4** |
| 真实模型端到端验收 | 工具接入后的系统级验收 |
| 存储层遗留表清理、通知模块整合 | 独立评估后实施 |
| `database.txt` 明文凭据轮换 | 独立立项（安全） |
| 多 worker / 分布式锁 | 维持设计文档的非目标 |
| 把同步 HTTP 客户端改成异步 | 见 §4.2.2 的取舍说明 |

> **为什么确认/MFA 不在接缝层做**：在接缝层强制"这个写工具必须先经用户确认"，
> 需要一份**带外的、不可伪造的确认凭据**（否则模型自己就能在参数里写 `confirmed: true`），
> 而那要求 Supervisor 区分"本轮是确认的 resume"与"本轮是新的请求"——这是一套独立设计。
> 假装它已解决比承认没做更危险。
>
> 接缝层保证的是**不会重复扣款**（幂等键，§4.5.2）与**不会静默串户**（§4.1.3）。
> 确认是产品流程，由模块用**既有的 `ask` interrupt + `FieldMeta`** 实现，
> 并在门 2 的逐工具契约里声明；门 1b 不新增确认机制。

---

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| **模块（Module）** | 一个领域 Agent 的全部自有代码：声明、工具、处理器、追问字段元数据。位于 `app/modules/<name>/` |
| **接缝（Seam）** | 模块与基座之间的固定接口：注册表、`BankClient`、结果信封、身份注入 |
| **单域脚手架（Harness）** | `tests/support/` 里的测试工具，让模块只跑自己的子图就能验证工具 |
| **生产工具** | 由模块声明并提供 handler、经注册表装配后可由 Agent 调用的工具 |

---

## 3. 注册接缝

### 3.1 目录结构

```text
backend/app/modules/
  __init__.py        ALL_MODULES: tuple[ModuleSpec, ...]   ← 每模块一行，无逻辑
  base.py            ModuleSpec / ToolHandler / ToolData / ToolFailure
  registry.py        build_registry()：校验并产出 roster 与 catalog
  money.py           金额展示格式化（唯一实现）
  account/           __init__.py（声明）+ tools.py（实现）
  transfer/  bill/  wealth/  card/  subscription/  life_event/
```

**当前状态**：生产环境已注册六个领域包，每个模块的 `tools` 均为空。新增工具应遵循本契约与逐工具契约；新增领域还需在 `app/modules/__init__.py` 注册对应模块。

### 3.2 `ModuleSpec`

```python
ToolHandler = Callable[[ToolInvocation, BankClient], ToolData]

@dataclass(frozen=True, slots=True)
class ModuleSpec:
    name: str                                            # 模块名，仅用于报错定位，如 "account"
    declaration: DomainDeclaration | None = None         # None = 只提供共享工具，不拥有领域
    tools: Mapping[str, ToolSpec] = field(default_factory=dict)
    handlers: Mapping[str, ToolHandler] = field(default_factory=dict)
    grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
```

- **MUST：工具声明只有一个真源 `ModuleSpec.tools`。** `DomainDeclaration.tools` 被删除。
- **MUST：`handlers` 的键集合必须等于 `tools` 的键集合**——注册表强制校验（§3.3 第 4 条）。
- **MUST：`grants` 缺省为"只给自己"**——`declaration` 非空且 `grants` 为空时自动等于
  `{declaration.agent_id: tuple(tools)}`。声明 `grants` 是为了表达跨领域共享工具：
  `ToolCatalog` 本来就支持一个 spec 授权给多个 agent，缺的只是模块侧的声明方式。
  现实依据：bank_backend 的模拟时钟（`/api/clock`、`/api/clock/run-tasks`）被定时转账、
  订阅扣款、生日订单共用同一张任务表，将来必然有跨领域工具。
- **MUST：`handler` 签名把 `BankClient` 作为参数注入**，模块自己不持有它——
  单测可以传假 client，不需要起服务。

#### 3.2.1 为什么删除 `DomainDeclaration.tools`

v1 让模块**同时**声明 `DomainDeclaration.tools` 与 `ModuleSpec.tools`，而 `build_registry()`
只校验后者与 `handlers` 一致——**前者迟早与后者漂移，且漂移了没有任何检查会发现**。

核实结论：`DomainDeclaration.tools` 在**生产代码里是死字段**。
`declaration.py` 只用它查重名；`tool_menu()` 读的是 `self.handle.tools`（handle / catalog 才是真源）；
`domain.py` 的六个声明全部 `tools=()`。测试里仅 2 处引用，其中一处的**名字本身**就在说
"菜单来自 handle 而不是 declaration"。

所以处置不是"加一致性校验"，而是**删掉这个字段**：删掉之后不存在两个真源，
也就没有"迟早漂移"这回事。`tool_menu()` 的行为不变。

### 3.3 注册表

```python
@dataclass(frozen=True, slots=True)
class Registry:
    modules: tuple[ModuleSpec, ...]
    roster: tuple[DomainDeclaration, ...]
    catalog: ToolCatalog
    handlers: Mapping[str, ToolHandler]

def build_registry(modules: Iterable[ModuleSpec] | None = None) -> Registry: ...
```

**MUST：构建时执行下面 9 条校验**，全部**抛 `ValueError` 并指出出错的名称**
（与 `ToolCatalog.build` 同一风格）。违反任意一条都是缺陷。

| # | 校验 | 防的问题 |
| --- | --- | --- |
| 1 | `ModuleSpec.name` 全局唯一 | 两个模块同名，报错定位不清 |
| 2 | `agent_id` 全局唯一 | 两个模块声明同一领域 |
| 3 | 工具名全局唯一 | 两个模块抢同一个工具名。**原本由 `DomainDeclaration` 查重，该字段删除后移到此处** |
| 4 | `handlers` 键集合 == `tools` 键集合 | 声明了工具却没有实现（或反之）。**由 `ModuleSpec.__post_init__` 强制**，不是 `build_registry()`——见下方说明 |
| 5 | 每个 `grants` 的键都是一个已声明的 `agent_id` | 拼错的 agent 名静默授权失败 |
| 6 | 每个 `grants` 的值都引用已声明的工具 | 由 `ToolCatalog.build` 复查 |
| 7 | `ToolSpec.side_effect` ∈ `{"READ", "WRITE"}` | 拼错成 `"WRITE "` 会被 `build_registry()` **直接拒绝**（非法取值），**不会**被当成读操作。真正会静默失效的是**省略**声明——默认值 `READ` 让**"写工具无幂等键则拒发"这道 fail-closed** 静默丢掉。注意**不是**"丢掉重试"：有键的瞬时失败在两种声明下都同样重试一次，**唯一**那条真实损失见 §4.5.1 |
| 8 | `ToolSpec.output_schema` 非空时本身是合法 JSON Schema | 与 Gateway 对 `parameters` 的立场一致：非法声明抛错，不放行 |
| 9 | **`ToolSpec.parameters` 的属性名不得是 `confirmed`、`idempotency_key`、`call_id`、`agent_id`** | **这四个名字都不该来自模型**：`call_id` 与 `idempotency_key` 由 Supervisor 铸，`agent_id` 由 Gateway 铸，而 `confirmed` **谁都不注入**——它是接缝"不让模型替用户确认写操作"的立场本身（§1.3）。模块若把它们声明成参数，模型就能自己伪造确认与幂等键，必须由校验兜住。**强制点是 `app/tools/protocol.py` 的 `RESERVED_PARAMETER_NAMES`（正是这四个名字，已逐个核对）被 `build_registry()` 使用**。**注意 `user_id` / `bank_customer_id` 不在这四个里**，见 §4.1.1 的身份约定 |

**这 9 条不都在同一门落地**——按依赖它们的协议字段分：

| 校验 | 强制点 | 执行时机 | 为什么 |
| --- | --- | --- | --- |
| 1、2、3、5、6 | `build_registry()` | 注册表构建期 | 需要跨模块视野 |
| 4 | `ModuleSpec.__post_init__` | 模块声明构造期 | 只需单个 `ModuleSpec` 就能判断 |
| 7、8 | `build_registry()` | 注册表构建期 | 检查副作用类型与输出 schema |
| 9 | `build_registry()` | 注册表构建期 | 检查模型不可提供的保留参数名 |

> **第 9 条现在是真的了。** 在 `2781598` 之前，本条只是文档里的要求：代码里没有
> `RESERVED_PARAMETER_NAMES`，也没有任何校验读它，而 `protocol.py` 的一处注释却写着
> 这个检查已经存在。**这就是"契约承诺了代码里没有的东西"的实例**——它也正是 §0
> 那条"MUST 指不到机械强制就降级"规则存在的理由。今天：`RESERVED_PARAMETER_NAMES`
> 是 `frozenset({"confirmed", "idempotency_key", "call_id", "agent_id"})`，
> `build_registry()` 对每个 `ToolSpec.parameters["properties"]` 求交集，非空即抛 `ValueError`。

> **第 4 条为什么不在 `build_registry()` 里**：它由 `ModuleSpec.__post_init__` 强制。
> `ModuleSpec` 是 frozen dataclass，**构造必然早于任何 `build_registry()` 看到它**，
> 所以校验点前移只会让非法声明更早失败，不会漏检。
>
> 把它同时写在两处（构造器 + 注册表）是本轮明确要消除的那种漂移：
> 两处会给出不同的错误文案，而"哪一处是真的"没有检查能回答。
> **MUST：第 4 条只有一个强制点。** 模块作者看到的是构造器抛出的错。
>
> 第 1、2、3、5、6 条留在 `build_registry()` 里，因为它们要**跨模块**才能判断
> （重名、grants 指向别的模块的 agent）——单个 `ModuleSpec` 看不到别的模块。

**MUST：`build_registry()` 在 import 时执行。** 这是刻意的：一个非法声明应该让应用**启动即失败**，
而不是等到某个用户提问时才失败。

### 3.4 装配点

```python
# app/runtime.py
def build_tool_gateway(registry=None, client=None) -> ToolGateway:
    registry = registry or build_registry()
    return ToolGateway(registry.catalog, BankToolAdapter(registry, client or BankClient.from_settings()))

# app/agents/domain.py
DOMAIN_ROSTER: tuple[DomainDeclaration, ...] = build_registry().roster
```

**MUST**：`DOMAIN_ROSTER` 保留原名与类型（对外名字与类型不变，现有测试不受影响），
但它从"手写元组"变成"注册表的派生结果"。**从此刻起，`runtime.py` 与 `domain.py`
不再被任何模块改动**——这就是隔离并行改动面的关键。

> 注意：**名字与类型**不变，但 `agents/__init__.py` **要改**——见下面那条惰性再导出的 MUST。
> 这两件事同时成立，不矛盾：`DOMAIN_ROSTER` 从 `app.agents` 拿到的仍是同一个元组。

**MUST：`app/agents/__init__.py` 的再导出必须是惰性的（`__getattr__`），不得急切 import `app.agents.domain`。**

这不是风格偏好，是上面那条 `build_registry().roster` 的**必要条件**。环是这样的：

```
app.modules.<某模块>  →  app.agents.declaration   （模块包需要 DomainDeclaration）
app.agents.declaration  →  app/agents/__init__.py  （导入子模块必然先执行包 __init__）
app/agents/__init__.py  →  app.agents.domain       （急切再导出）
app.agents.domain       →  build_registry()        （§3.3 要求在 import 时运行）
build_registry()        →  app.modules.registry    →  app.modules.base
```

`app.modules.base` 此时**正在初始化中**，于是 `import app.modules` 直接抛
`ImportError: cannot import name 'ModuleSpec' from partially initialized module 'app.modules.base'`。

为什么必须断在 `agents/__init__.py` 这一条边：它是这个环里**唯一纯便利、零消费者**的边
（全仓没有任何 `from app.agents import ...`），而另外两条边都是承重的——
模块包确实需要 `DomainDeclaration`，`build_registry()` 确实必须在 import 时跑。

**症状与危害**：完整 pytest 套件**不会**暴露它，因为 `conftest` 先 import 了 `app.runtime`，
把 `app.agents` 整个加载完了。但门 2 的六个模块团队会踩到：
`uv run python -c "import app.modules.<自己的模块>"`、REPL、不经过 conftest 的脚本、
以及任何以模块包为入口的冒烟测试，全部失败。这恰好砸在本轮要解锁的那件事上。

**机械强制**：一条在**全新解释器**里 import `app.modules` 的回归测试
（`subprocess` + `sys.executable`，必须用子进程——测试进程里 `app.agents` 早已加载，
同进程断言测不出这个环）。

### 3.5 协议层：只做兼容扩展

> **v1 在这里自相矛盾**：v1 写"`tools/protocol.py` 一行不改"，却在别处给它加 `bank_customer_id`。
> v2 起明确改为下面这条规则。

**MUST：`tools/protocol.py` 只允许"只增不改"的兼容扩展**——新增字段一律带默认值，
不删字段、不改现有字段的名字或类型、不改 `ToolAdapter` 的方法签名。

**MUST：`gateway.py`、`handle.py`、`catalog.py` 的三项检查一行不改，且本轮不为请求路径新增第四项。**
本轮的扩展不触碰这三项。

> **修正（门 1b 冻结时核对代码）**：本节此前把三项检查写成一条顺序链
> "存在性 → JSON Schema → 授权"。**那不是运行时的顺序，代码不支持这种读法**，已改为下面的准确描述。
> 之所以要改：§3.5 是门 2 六个团队动手前会读的那一页，**一个错的检查顺序心智模型，
> 正是"顺手加第四道检查"的来源**。

三项检查的实际形态：

- **存在性（declaredness）是构建期的事，不在请求期。** `ToolCatalog.build` 对同名工具重复声明抛
  `ValueError`（`catalog.py`），对"授权了一个未声明的工具"抛 `ValueError`（`catalog.py`）。
  请求期**没有任何一步叫"先检查存在性"**。
- **授权是结构性的，不是一个步骤。** `ToolCatalog.for_agent` 只把**已授权**的工具放进那个 agent 的
  `AgentToolHandle`；未授权的工具**根本不在 handle 里**。所以"越权"不是被某一步拦下，
  而是那个工具对这次调用**不存在**。
- **请求期唯一有顺序的一对是"查找 → JSON Schema"**：`ToolGateway.invoke` 先用 `handle.spec(tool_name)`
  查（`None` 即 `_denied`），**再**用 `Draft202012Validator` 校验参数（`ToolGateway._validate`）。
  注意这个查找**把两种情况合并成同一个 `None`、同一句拒绝文案**："没有这个工具"与"这个 agent 没被授权"。
  这是刻意的：对调用方而言两者都是"你不能调它"，分开报会把目录结构泄漏给一个不该知道它的调用方。
- **三项检查的实质没有变，MUST 仍然成立**：三项都在、门 1b 一行没改。
  改的只是"它们的顺序"这个错误描述。

**允许的协议扩展**（每处都是加带默认值的字段，现有构造点与测试全部不受影响）：

| 类型 | 新增字段 | 门 | 用途 |
| --- | --- | --- | --- |
| `TrustedContext` | `bank_customer_id: str \| None = None` | 1a | §4.1 身份 |
| `ToolSpec` | `side_effect: str = "READ"` | 1b | §4.5 副作用声明 |
| `ToolSpec` | `output_schema: Mapping[str, Any] \| None = None` | 1b | §4.4.1 逐工具输出校验 |
| `ToolCall` | `idempotency_key: str \| None = None` | 1b | §4.5.2 幂等 |
| `ToolResult` | `error_code: str \| None = None` | 1b | §4.4.2 机器可读错误码 |

**MUST：`error_code` 追加在 `retryable` 之后**（而不是紧跟 `error`）：
`ToolResult` 是 frozen dataclass，插在中间会让**任何按位置构造的调用点静默错位**。
追加在末尾对位置与关键字两种写法都安全。

#### 3.5.1 `gateway.py` 的三处加法

v2 起草时以为协议扩展不碰 `gateway.py`。**核对后不成立**：
`ToolGateway.invoke` 是 `ToolInvocation` 的**唯一构造者**，
所以从 Supervisor 到适配器的任何新信息**必须经过它**。

**MUST：`gateway.py` 的改动只允许下面三处，不得更多。**

| # | 位置 | 加法 | 状态 |
| --- | --- | --- | --- |
| 1 | `invoke` 读 request 键 | 读 `idempotency_key` 并放进 `ToolCall` | **已落地（`58fc9ed`）** |
| 2 | `_denied` | 给被拒的结果补 `error_code` | **已落地（`d82689d`）** |
| 3 | `invoke` 末尾 | 用 `dataclasses.replace(...)` 把 `call_id` 盖回适配器返回的结果 | **已落地（`17013cc`）** |

**三处全部已落地，`gateway.py` 的改动到此为止。** 门 2 不得新增第四处——
新增信息要经过 Gateway，先走 §6.3 的变更流程，而不是顺手在 `invoke` 里加一段。

第 3 处的理由：客户端靠 `call_id` 配对 `tool_call` 与 `tool_result`，
而工具契约**不要求**适配器回显它。返回空 id 或别人的 id 时，以实际发出的那个为准。
**MUST：第 3 处不得回退**，它有测试锁定（`tests/test_tool_catalog.py` 里
`test_the_gateway_stamps_the_call_id_it_sent_regardless_of_the_adapter` 与
`test_a_denied_call_keeps_the_call_id_the_request_carried`）。

**SHOULD：第 1 处落地时只在该 `ToolCall(...)` 构造里加参数**，不要重写外层那个 `replace(...)`。
（`58fc9ed` 就是这么落地的：`invoke` 读 `request["idempotency_key"]`，只在 `ToolCall(...)` 里多传一个实参。）

**SHOULD：`handle.py` 与 `catalog.py` 一行不改**（`handle.call` 只透传 request）。没有测试锁定这条，靠 review。

> **为什么这是安全的**：`idempotency_key` 由 Supervisor 写入 request 映射，
> **不是模型输出**——模型的输出只成为内层的 `arguments`。
> 模型即使把 `idempotency_key` 写进 `arguments` 也无效：适配器只读 `call.idempotency_key`
> 这个专用字段，而 §3.3 第 9 条禁止工具把该名字声明成参数。

#### 3.5.2 编排层的必改点（仅管道）

**SHOULD：路由与决策逻辑一行不改。** Supervisor 选谁、领域怎么问模型、interrupt/resume
全部不动。改的只是**结果回传的管道**。

编排层有 3 处必改（**均已在门 1a 落地，`c233dd4`**），因为 `final.business_ids` 原本硬编码为空数组，
信封要真正可用就必须接通（§4.4.1）：

| 位置 | 改动 |
| --- | --- |
| `_run_tool` | 用信封的 `summary` 代替整个信封的 `json.dumps`（§4.6） |
| `_conclude` | 汇聚 `business_ids` |
| `supervisor_node` 的两个返回点 | 硬编码的 `"business_ids": []` 改为 `_collect_business_ids(results)`——去重并保持首次出现顺序 |

**SHOULD：`_collect_business_ids` 不做"是不是公开业务号"的判断。**
它无法分辨数据库主键与 `reference_no`；那条约束（§4.4.1）是模块的义务，
靠 code review 守，不要在这里加猜测性的启发式。

> **为什么协议层可以改而三项检查不能**：前者是**数据结构**，加带默认值的字段是
> 向后兼容的；后者是**校验逻辑**，那三项检查是全项目最扎实的部分，也是六个模块共同依赖的
> 唯一信任边界。改数据结构不削弱它，改校验逻辑会。

### 3.6 模块不得修改的共享文件

**SHOULD：模块不得直接修改** `app/runtime.py`、`app/agents/domain.py`、`app/tools/*`、
`app/integration/bank_client.py`、`app/modules/base.py`、`app/modules/registry.py`。

**SHOULD：模块只在自己目录内新增文件**，外加 `app/modules/__init__.py` 的一行注册
（这是唯一的共享文件改动，一行、无逻辑，冲突可忽略）。

需要新能力时走 §6.3 的变更流程。

---

## 4. 跨模块约定

### 4.1 身份：一次解析，绝不回退

#### 4.1.1 真源

**MUST**：`app_user` 新增一列 `bank_customer_id: str | None`。
迁移 `0006_bank_customer_id`，`down_revision = "0005_email_notification"`，
文件名与 `revision` **一致**。种子：`seed.py` 为 `user_0001` 写入 `DEMO_BANK_CUSTOMER`——2026-09-23 时为 `u_zhang`，2026-10-11 起为 `u_wealth`（理由见[数据归属契约](2026-09-23-module-data-ownership-contract.md) §4.0 的更新注）。

> **为什么不改 bank_backend 的默认值**：`bank_backend` 的 `default_user_id = "u_zhang"`
> 是它自己的演示约定。让主项目持有映射，两个项目的用户体系保持解耦；
> 将来加多用户只是加数据，不是加代码。

#### 4.1.2 传递

```python
@dataclass(frozen=True, slots=True)
class TrustedContext:
    user_id: str
    session_id: str
    bank_customer_id: str | None = None
```

**MUST**：解析发生在 `AgentRuntime.invoke`，通过注入的 resolver：

```python
# app/identity/service.py
class Identity:
    def bank_customer_id(self, user_id: str) -> str | None: ...

# app/runtime.py
self.agent = AgentRuntime(
    UnconfiguredModelClient(),
    graph_factory=agent_graph_factory(self.gateway),
    customer_resolver=self.identity.bank_customer_id,
)
```

`AgentRuntime` 通过 `asyncio.to_thread` 调用 resolver（同步查库不阻塞事件循环）。
resolver 未注入时 `bank_customer_id` 为 `None`；**resolver 抛异常时记录日志并返回 `None`**，
不让一次查询失败终结整个回合——但方向是 fail closed，见下。

#### 4.1.3 fail closed（本节最重要的一条）

bank_backend 的 `user_id` **可省略**，省略时用 `u_zhang`（GET 读 query，十条 POST 读 body，见 §4.2.1）。所以：

1. **MUST：`BankClient` 对每个请求显式携带 `user_id`，从不依赖 bank_backend 的默认值。**
   强制点：`request()` 的 `user_id` 是必填关键字参数（`tests/test_bank_client.py`）。
2. **MUST（门 1b）：`bank_customer_id` 为空 → 工具返回 `FAILED`，文案「当前用户未绑定银行客户号」，
   `error_code=NO_CUSTOMER_BINDING`，且不发任何 HTTP 请求。**
   - 「不发任何 HTTP 请求」**今天已强制**：`BankClient` 在 socket 之前 fail closed
     （`tests/test_bank_client.py` 对 `None` 与 `""` 都断言 `sent == []`）。
   - 「文案 + `error_code`」的强制点是**门 1b 的 `ErrorCode`**（**门 1b 前** `ToolResult.error_code`
     还不存在；门 1b 已加上，见 §3.5）。
     门 1a 期间由**模块自己的 handler** 抛 `ToolFailure("NO_CUSTOMER_BINDING", …)` 承担
     （`tests/test_bank_adapter.py`）。

否则一个没有绑定的用户会静默读到 `u_zhang` 的账户——这是本轮唯一可能造成"数据串户"的路径。

### 4.2 传输与错误映射

#### 4.2.1 客户端

```python
class BankClient:
    @classmethod
    def from_settings(cls) -> BankClient: ...
    def request(
        self,
        method: str,
        path: str,
        *,
        user_id: str,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> BankResponse: ...
    def close(self) -> None: ...
```

- **MUST：`user_id` 是必填关键字参数**，编译器强制每个调用点都传（对应 §4.1.3 第 1 条）。
- **MUST：`user_id` 要同时放进 query 和 body。** bank_backend 的 GET 路由读 query 参数，
  但**十条 POST 路由只读 `body.user_id`，完全忽略 query**（`routes.py` 的 `/transfers`、
  `/products/purchase`、`/cards/apply`、`/subscriptions`、`/birthdays/orders` 等）。
  只发 query 等于每次写操作都以银行自己的默认客户 `u_zhang` 执行——
  转账会从被点名的账户扣钱，而账本和审计记在 `u_zhang` 名下（§4.1.3 要防的正是这件事）。
  客户端在 body 里**覆盖**调用方给的值，所以模块无法冒用别的客户号。
- 设置（`app/core/config.py`）：`bank_backend_base_url: str = "http://127.0.0.1:8001/api"`、
  `bank_backend_connect_timeout: float = 2.0`、`bank_backend_read_timeout: float = 10.0`。
- **MUST：`base_url` 里带 `/api` 前缀，模块只写资源路径。**
  bank_backend 把整个 router 挂在 `/api` 下（57 条路径全部带前缀，`/accounts` 不存在）。
  前缀只放在这一处，模块写 `request("GET", "/accounts", ...)`。
- **MUST：运行约定 bank_backend 用 8001。** 它默认也用 8000，与主项目 `scripts/serve.py` 冲突。

#### 4.2.2 为什么保持同步

**DEFERRED：把同步 HTTP 改成异步。**
`ToolAdapter.invoke` 是同步协议，图节点也是同步的。改成异步要动"最扎实的那一层"，而收益很小：
真正的线程占用者是模型调用（最长 30s），工具调用只有几十毫秒。
代价是同步调用跑在 LangGraph 的默认线程池上——这个天花板已经存在，本轮把它写进文档而不是改架构。

#### 4.2.3 只有一层重试

**MUST：`BankClient` 内部绝不重试。** Supervisor 已对 `retryable=True` 做"同一次 invocation
内重试一次"，否则变成两层、最坏 4 次调用。

#### 4.2.4 错误分类

依据 `bank_backend/app/core/errors.py` 与 `main.py`：

```python
class BankErrorKind(StrEnum):
    CONNECT, TIMEOUT, RATE_LIMITED, SERVER,          # retryable = True
    NOT_FOUND, INVALID_STATE, INSUFFICIENT_BALANCE,
    CONFIRMATION_REQUIRED, VALIDATION, CLIENT, PROTOCOL   # retryable = False
```

| 情形 | 判定依据 | kind | retryable |
| --- | --- | --- | --- |
| 连接失败 | `httpx.ConnectError` | `CONNECT` | **True** |
| 超时 | `httpx.TimeoutException` | `TIMEOUT` | **True** |
| 限流 | HTTP 429 | `RATE_LIMITED` | **True** |
| 服务端故障 | HTTP 5xx | `SERVER` | **True** |
| 资源不存在 | 404 或 `code == 1004` | `NOT_FOUND` | False |
| 业务状态非法 | 409 或 `code == 1005` | `INVALID_STATE` | False |
| 余额不足 | 409 或 `code == 1006` | `INSUFFICIENT_BALANCE` | False |
| 待确认 | 409 或 `code == 1007` | `CONFIRMATION_REQUIRED` | False |
| 参数校验 | 422 或 `code == 1012` | `VALIDATION` | False |
| 其他 4xx | status | `CLIENT` | False |
| 响应不可解析 / 缺 `code` | 协议错误 | `PROTOCOL` | False |
| 响应不可解析**且** status 是 429/5xx | 按 status 判定（同上两行） | `RATE_LIMITED` / `SERVER` | **True** |

> **最后一行是有意为之**：429/5xx 的响应体读不出来（例如代理返回的 HTML 错误页）仍然是传输层故障，
> 归成 `PROTOCOL` 会让一次短暂故障变成不可重试。只有**非** 429/5xx 的不可解析响应才算协议错误。

**MUST：判定顺序是 `code` 优先，HTTP status 兜底。**

#### 4.2.5 必须绕开的坑：`message` 不是原因

`BankError.to_payload()` 返回的是**类级默认 message**，不是 `reason`——
所以「用户不存在: u_zhang」这类细节**根本没进响应体**，客户端只会看到 `resource_not_found`。

- **MUST：不把 bank_backend 的 `message` 透给模型或用户。**
- **MUST：用户可见文案来自共享默认表 `BANK_ERROR_MESSAGES: Mapping[BankErrorKind, str]`**
  （`app/integration/bank_errors.py`）；模块需要更贴切的措辞时，
  自己捕获 `BankCallError` 并抛 `ToolFailure` 覆盖。

### 4.3 金额单位：边界只在 `_minor` 上，且禁浮点

两边的单位是**不同的**：

| 侧 | 单位 | 证据 |
| --- | --- | --- |
| bank_backend | 整数分（BIGINT） | `bank_backend` 的 README 与 `money_service.py` |
| 主项目 | `Numeric(18,2)` 元 | `app/storage/models.py` |

**MUST**：

1. 工具参数与结果里的金额字段**一律命名为 `*_minor` 且为 `int`**。
   名字本身是防错装置：模型看到 `amount_minor` 不会把 50 元写成 `50`。
   **这是 100 倍资金错误的主要防线。**
2. JSON Schema 里金额字段写 `{"type": "integer", "exclusiveMinimum": 0}`——
   浮点会被 Gateway 在第一道校验拒绝。
3. 元/分换算只有一处实现：`app/modules/money.py` 的
   `format_minor(minor: int, currency: str = "CNY") -> str`。
   **实现只能用整数运算**（`divmod(abs(minor), 100)`），不得出现浮点除法。
4. **模型不做算术**：信封里直接给它格式化好的字符串，领域提示词里加一句"金额不要自行换算或计算"。

> **陷阱**：`app/core/types.py` 有一个 `money()`，生产代码零调用（只有测试引用），
> 而 `notification/summary.py` 另有一份**语义相反**的 `money()`（失败静默归零 vs 抛错）。
> **MUST NOT 复用也不要"顺便统一"这两个**——它们属于待清理的残骸，不在本轮范围。

### 4.4 结果信封

`ToolResult.data` 目前是自由形状。**MUST** 定为：

```python
@dataclass(frozen=True, slots=True)
class ToolData:
    summary: str                              # 中文一句话结论：给 Supervisor 与 subtask_end
    display: dict[str, Any]                   # 前端渲染用；本期形状见 §4.7
    facts: dict[str, Any]                     # 精确值：*_minor、ID、日期。模块私有
    business_ids: dict[str, str] = field(default_factory=dict)   # {"<kind>": "<public reference>"}

    def as_dict(self) -> dict[str, Any]: ...
```

- `ToolResult.data` 仍是 `dict`，模块返回 `ToolData`，由 `BankToolAdapter` 调 `as_dict()` 落进去。
- **MUST：适配器校验信封**——缺 `summary`/`display`/`facts` 或类型不对 → **抛错**，
  而不是静默返回半个结果。

#### 4.4.1 四个字段的归属

v1 说 `display` 给前端、`facts` "供精确引用"，但**没有说谁能读 `facts` 的内部结构**，
也没有说这四个字段各自允许流向哪里。所以先把归属定死：

| 字段 | 谁消费 | 契约强度 | 内部结构 |
| --- | --- | --- | --- |
| `summary` | **基座**：Supervisor 与 `subtask_end` 事件 | **强**：纯中文短句，一句话结论 | 自由文本 |
| `business_ids` | **基座**：汇聚进 `final.business_ids` | **强**：值必须是稳定、公开、可被用户引用的业务号 | opaque 字符串 |
| `display` | **前端** | **强**：受显示协议约束（§4.7） | 见 §4.7 |
| `facts` | **仅模块自己** | **弱**：模块私有 | 由 `output_schema` 约束 |

**SHOULD：基座代码不得读取 `facts` 里的任何具体键。**
基座只做三件事：整体序列化进日志、按 §4.6 的规则决定是否进模型/checkpoint、转发。
任何"基座想从 `facts` 里取个字段来用"的需求都是错的——那说明它应该升级为 `summary`
或 `business_ids` 的一部分。

**MUST：`final.business_ids` 是 `string[]`，只放公开业务号的**值**，不放 `kind`。**
`kind` 是内部字段分类（`account_id`/`transfer_id`），暴露给用户就是内部 id 泄漏；
数据库主键也不得放入 `business_ids`，只能放在 `facts`。
若上游只有数据库主键，模块必须先取得稳定的 `reference_no` / `display_id`，
否则该工具的 `business_ids` 为空。

> **评审第 2 条的二选一，这里选"两者都要"，因为二选一不成立**：
> `summary` 与 `business_ids` 是**基座消费**的，不可能私有；
> `display` 要给前端渲染，必须有最小协议；
> 只有 `facts` 可以私有——但它需要 `output_schema` 兜住形状。

> **⚠️ 已记录的偏差（2026-09-24）：`business_ids` 目前也进模型提示词。**
> 本表说它归"基座"，§4.6 的去向表说它**不进模型上下文**——**后者今天不成立**，
> 详见 §4.6 末尾的"已知偏差"。这里保留本表不动：偏差不在"谁拥有这个字段"，
> 而在"承载它的那一行同时被两个受众读到"。修法见 §4.6。

#### 4.4.2 逐工具输出 Schema

`ToolSpec` 目前只有输入参数 Schema。**MUST** 新增一个**可选**的 `output_schema`，约束 `facts`：

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    side_effect: str = "READ"
    output_schema: Mapping[str, Any] | None = None             # 约束 facts
```

- **MUST：校验位置是适配器**（`BankToolAdapter.invoke`），不是 Gateway。
  Gateway 的三项检查必须保持不动（§3.5），而 `output_schema` 校验的是**工具自己产出的数据**。
- **MUST：校验对象是 `facts`**，不是整个 `data`。
- `None` 表示不校验（默认），模块可增量补齐；但按 §4.4.4，未声明 schema 的工具不能被其他模块消费。
- **MUST：校验失败返回 `OUTPUT_SCHEMA_VIOLATION`**（工具失败，客户端可读），不是静默通过。
  理由：`facts` 会进 checkpoint 与跨模块调用，形状错了要在**产生它的那一刻**失败，
  而不是等下游某次读取时莫名其妙地 KeyError。

#### 4.4.3 失败：`ToolFailure` 与错误码进协议

> **v1 的错误**：v1 规定 `code` 只进日志、"不进用户可见文案"，而 `ToolResult` **根本没有
> 承载 code 的字段**。后果是前端、Supervisor、测试**无法区分**"未绑定客户"、
> "余额不足"、"待确认"、"暂时不可用"——它们全都只是一个 `status=FAILED` 加一句中文。
> **稳定错误码必须进协议。**

```python
class ToolFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None: ...
```

`code` 是一个**稳定的、大写下划线的机器标识**，`message` 是**面向用户的中文文案**。
两者都进 `ToolResult`：

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    status: ToolStatus
    call_id: str | None = None
    data: dict[str, Any] | None = None
    error: str | None = None                    # message，面向用户
    retryable: bool = False
    error_code: str | None = None               # 新增，追加在末尾（§3.5）
```

**错误码表分成两半，各放该放的地方**：

| 内容 | 位置 | 理由 |
| --- | --- | --- |
| `ErrorCode`（枚举） | `app/tools/protocol.py`，紧挨 `ToolStatus` | 它是**工具边界的词汇**。放这里还消除了一处循环依赖：`gateway.py` 需要它，而 `gateway.py` 不该 import `app.modules`（`app/modules/__init__.py` 会拉起全部模块包） |
| `ERROR_MESSAGES`（中文文案） | `app/modules/base.py`，与 `BANK_ERROR_MESSAGES` 同层 | 它是**面向用户的措辞**，不是协议 |

```python
class ErrorCode(StrEnum):
    # 身份（§4.1.3）
    NO_CUSTOMER_BINDING = "NO_CUSTOMER_BINDING"
    # 与 BankErrorKind 一对一，银行失败不丢语义
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    VALIDATION = "VALIDATION"
    CLIENT = "CLIENT"
    PROTOCOL = "PROTOCOL"
    # 瞬时故障，且 Supervisor 已经用掉了那一次重试
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    # 工具自身的问题
    OUTPUT_SCHEMA_VIOLATION = "OUTPUT_SCHEMA_VIOLATION"
    INTERNAL = "INTERNAL"
    # 写安全（§4.5.2）
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    # 未到适配器就被拒
    TOOL_DENIED = "TOOL_DENIED"
```

**MUST：`ToolFailure(code, message, *, retryable=False)` 在构造时校验 code 已注册**：
拼错的码会变成一个前端永远无法分支的码，且失败看起来像普通失败——所以在抛出点就炸。

#### 4.4.3.1 四张表、两个词汇层（2026-09-24 逐行核对代码后写成）

上一版只说"错误码表分成两半"，漏掉了银行侧那两张。**四张表的键集、条数与读者如下**
（数字来自对代码的实测，不是抄写）：

| 表 | 位置 | 键类型 | 条数 | 谁产生 | 谁读 |
| --- | --- | --- | --- | --- | --- |
| `ErrorCode` | `app/tools/protocol.py` | 它自己 | **13** | 协议 | 前端、Supervisor、测试、`ToolFailure` 的构造校验 |
| `ERROR_MESSAGES` | `app/modules/base.py` | `ErrorCode` | **13**，与 `ErrorCode` 键集**完全相等** | 基座自己 | 基座的拒绝路径；模块 handler 也可复用 |
| `KIND_TO_ERROR_CODE` | `app/integration/bank_errors.py` | `BankErrorKind` | **11**，覆盖全部 kind | 适配器 | `BankToolAdapter.invoke` |
| `BANK_ERROR_MESSAGES` | 同上 | **`BankErrorKind`，不是 `ErrorCode`** | **11** | `BankClient`（经 `message_for`） | `BankCallError.message` |

- `ERROR_MESSAGES` 与 `ErrorCode` 的键集相等有测试锁定：
  `test_error_codes.py::test_every_error_code_has_a_message`。
- `KIND_TO_ERROR_CODE` 覆盖全部 kind 有测试锁定：
  `test_error_codes.py::test_every_bank_error_kind_maps_to_a_code`。
- `BANK_ERROR_MESSAGES` 与 `BankErrorKind` 的键集相等有测试锁定：
  `test_bank_client.py::test_every_kind_has_a_chinese_message_and_none_are_english`。

**11 与 13 对不上是设计的结果，不是漏映射。** 两个词汇描述两件事：

- **`BankErrorKind`（11 个）说的是"银行做了什么"**：`CONNECT`/`TIMEOUT`/`RATE_LIMITED`/`SERVER`/
  `NOT_FOUND`/`INVALID_STATE`/`INSUFFICIENT_BALANCE`/`CONFIRMATION_REQUIRED`/`VALIDATION`/`CLIENT`/`PROTOCOL`。
- **`ErrorCode`（13 个）说的是"系统据此得出了什么结论"**：其中 **8 个**由 `KIND_TO_ERROR_CODE`
  产生（7 个与 kind 同名，第 8 个是那 4 个瞬时 kind 收敛成的 `UPSTREAM_UNAVAILABLE`），
  另 **5 个**银行侧根本没有对应 kind：`NO_CUSTOMER_BINDING`、`OUTPUT_SCHEMA_VIOLATION`、
  `INTERNAL`、`IDEMPOTENCY_KEY_REQUIRED`、`TOOL_DENIED`。7 + 1 + 5 = 13。
  （实测：`KIND_TO_ERROR_CODE` 的**值集合**恰好是这 8 个，剩下 5 个正是上面列出的那 5 个。）

**哪张表给谁用**（照你的代码位置选）：

| 你在写什么 | 用哪张 | 为什么 |
| --- | --- | --- |
| 模块 handler 要拒绝一次调用 | 抛 `ToolFailure(code, message)`：`code` 取 `ErrorCode`，`message` 你自己写（或复用 `ERROR_MESSAGES[code]`） | 适配器把 `failure.message` 原样下发，`failure.code` 进 `error_code` |
| 银行调用失败 | **不要自己映射** | `BankClient` 已抛 `BankCallError(kind, message_for(kind))`，适配器再查 `KIND_TO_ERROR_CODE` |
| 基座自己的拒绝（无幂等键、`output_schema` 违规） | `ERROR_MESSAGES[ErrorCode.X]` | 适配器就是这么做的 |
| 前端分支 / 测试断言 | `ErrorCode` 的值 | 永远不匹配中文 |

**7 个同名 code 的中文文案刻意不同，不要"修"它**：`BANK_ERROR_MESSAGES` 里
`INVALID_STATE` 是"该业务当前状态不允许此操作。"，`ERROR_MESSAGES` 里是"当前业务状态不支持该操作。"；
`CONFIRMATION_REQUIRED` 两侧分别是"该操作需要额外确认。"与"该操作需要您再次确认。"。
一个在描述**银行返回了什么**，一个是**系统自己下的结论**。
（`NOT_FOUND` 与 `INSUFFICIENT_BALANCE` 恰好两侧同句——同句是巧合，不是要求。）

> **已核实：不存在"同一次失败对用户说出两句互相矛盾的话"的路径。** 一次失败只走一条路——
> 银行侧失败取 `BankCallError.message`（来自 `BANK_ERROR_MESSAGES`），模块自己的拒绝取
> `ToolFailure.message`，基座的拒绝取 `ERROR_MESSAGES`；三者互斥，同一帧里只有一个 `error` 字符串。
> 两套措辞只会出现在**不同触发原因**下，而各自都说得对。

**MUST：模块不得定义模块私有错误码**，也不得仅在文档中"自定义"一个未注册字符串。
新增错误码必须先同时加入 `ErrorCode`、`ERROR_MESSAGES` 和错误码映射测试，
再被逐工具契约引用；否则注册表或适配器应拒绝该声明。

**用户可见文案仍只来自 `message`**，`error_code` 面向代码（前端分支、测试断言、监控）。
前端按 code 分流交互（如 `CONFIRMATION_REQUIRED` 提示重新确认、
`NO_CUSTOMER_BINDING` 引导绑定），而不是匹配中文。

**`retryable` 与 code 的对应**（`retryable` 默认 `False` 是刻意的：
重试一个有副作用的工具会重复它的效果）：

| 情形 | code | retryable |
| --- | --- | --- |
| 瞬时故障：`CONNECT` / `TIMEOUT` / `RATE_LIMITED` / `SERVER` | `UPSTREAM_UNAVAILABLE` | **True**——这正是唯一会重试的情形 |
| 业务拒绝：`NOT_FOUND` / `INVALID_STATE` / `INSUFFICIENT_BALANCE` / `CONFIRMATION_REQUIRED` / `VALIDATION` / `CLIENT` / `PROTOCOL` | 同名 code | False |
| 身份未绑定 | `NO_CUSTOMER_BINDING` | False |
| 工具自身的问题 | `OUTPUT_SCHEMA_VIOLATION` / `INTERNAL` / `IDEMPOTENCY_KEY_REQUIRED` | False |
| 未到适配器就被拒 | `TOOL_DENIED` | False |

> **决定 `retryable` 的是 `BankErrorKind`，不是 code**。`UPSTREAM_UNAVAILABLE` 这个码
> 恰恰**只**在 `retryable=True` 时出现——它标注的是"这次失败本可重试"。
> 而 Supervisor 重试用尽后**抛 `UpstreamUnavailable`**，所以客户端看到的最终结局是一个错误，
> 不是一个"可以重试"的工具结果。两者不矛盾：`tool_result` 事件描述的是**那一次尝试**，
> `final` 描述的是**整个请求**。

#### 4.4.4 跨模块消费（DEFERRED 具体版本协议）

**MUST**：跨模块消费必须通过提供方已注册的工具，由正常的 Gateway/适配器路径调用；
**禁止直接 import 另一个模块的 handler、读取其私有状态或约定未声明的 `facts` 键。**

**MUST**：`output_schema=None` 的工具只能供本模块内部使用；它的 `facts` 不得成为跨模块输入。

**DEFERRED**：完整的跨模块消费版本协议（登记提供方工具名、`output_schema` 版本标识、
所需字段，以及破坏性变更通知流程）。**理由：今天没有任何真实跨模块调用。**
等第一个消费者出现时再冻结具体版本协议；在此之前靠 code review 守上面两条 MUST。

> 这条是刻意降级的：为一个**还不存在的消费者**冻结版本协商机制，会产出没人验证过的规则。
> 门 2 的逐工具契约里预留了"是否被别的模块消费"一栏，第一个消费者出现时在那里登记。

#### 4.4.5 处理器抛其他异常

**MUST**：适配器捕获未预期异常 → `ToolResult(status=FAILED, error="工具内部错误",
error_code="INTERNAL", retryable=False)`，**异常原文只进日志**（可能带业务数据）。
与仓库既有的"provider 原文只进日志"立场一致。

### 4.5 副作用、幂等与重试

> **本节自 2026-09-24 起生效**（门 1b 于该日完成）。

#### 4.5.1 `side_effect`

```python
class SideEffect(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
```

`ToolSpec.side_effect` 默认 `"READ"`——**默认值的选择是安全方向的反面，必须由校验兜住**：
模块忘记声明写工具时，默认会得到 `READ`，于是**"写工具无幂等键则拒发"这道 fail-closed 静默失效**
（这条真实损失见下方警告框）。**注意它不是"丢掉重试"**：一次**带键**的瞬时失败在两种声明下
都同样重试一次、且带同一个键——所以别把这条读成"READ 会重试、WRITE 不会"。
所以注册表**强制校验取值合法**（§3.3 第 7 条），并在门 2 的逐工具契约里**逐个工具显式声明**。

**MUST：不允许依赖默认值。** 默认值的存在只是为了不破坏现有构造点，不是为了让人省略。

> ### ⚠️ 门 2 最容易犯、后果最贵的一个错
>
> **每个写工具都必须显式声明 `side_effect=SideEffect.WRITE`。
> 省略它，写工具会被整条链路当成读工具，而 `READ` 恰好是默认值——
> 注册表能拒绝一个非法取值，但拒绝不了一次省略。**
>
> **省略之后到底丢什么**（逐行核对代码后的准确说法，不是吓唬人）：
>
> 1. **丢掉 fail-closed 的"无键拒发"——但只在"没经过 Supervisor 的调用方"身上丢。**
>    `BankToolAdapter.invoke` 的拒绝是有前提的：`spec.side_effect == SideEffect.WRITE`
>    时才检查幂等键。声明成 `READ` 的写工具**直接跳过这段**。
>    走 Supervisor 时这不产生任何差别：`_run_tool` 对**每个**工具（读也包括）都在重试循环
>    之外铸一次键，所以**键总是在**，即使声明成 `READ` 也没有东西会被拒绝。
>    真正丢掉的场景是**绕开 Supervisor 的调用方**（测试、脚本、跨模块调用、将来任何新入口）：
>    那种调用方没有键，声明 `WRITE` 时适配器**在任何 HTTP 之前**就拒绝，声明 `READ` 时
>    调用照常进行——银行被调用而**这次调用没有 `Idempotency-Key`**；若它瞬时失败，
>    `_may_retry` 看到的是 `READ`、放行重试，于是第二次**无键**调用可能重复那笔写。
>    这正是 `test_a_write_without_a_key_is_refused_before_any_request` 用
>    `assert ran == []` 和 `assert client.requests == []` 锁住的那条保证。
> 2. **`_may_retry` 的 `WRITE` 分支经适配器不可达，所以它不是"省略声明"的另一条后果。**
>    `supervisor._may_retry` 确实对 `WRITE` 拒绝"因缺键而失败"的那次重试，但适配器构造的
>    `IDEMPOTENCY_KEY_REQUIRED` 结果**不带 `retryable`**（`bank_adapter.py` 里那个
>    `ToolResult` 没有传 `retryable`，取默认 `False`），而 `_may_retry` 在**到达**该分支之前
>    就先返回了：`if result.status is not ToolStatus.FAILED or not result.retryable: return False`。
>    所以这条分支只对**自己抛 `ToolFailure(IDEMPOTENCY_KEY_REQUIRED, retryable=True)` 的
>    handler** 生效，而生产代码里没有这样的 handler——仓库自己的测试就是这么写的
>    （`backend/tests/test_write_safety.py` 里
>    `test_a_write_failing_for_a_missing_key_is_never_retried` 的 docstring：
>    "适配器自己的拒绝是 `retryable=False`，该分支经它不可达"）。
>    **两条真实损失只有一条**：上面的 fail-closed 拒发。
>
> **不要把它简化成"一次超时变成两笔转账"。** 那个说法对今天的代码不成立，
> 而且理由恰恰是本轮的核心设计：**幂等键是在重试循环之外铸一次、重试复用同一个键**
> （§4.5.2）。所以对一次**带键**的瞬时失败，声明 `WRITE` 与省略声明
> **行为完全相同**——两者都会重试一次，且两次带的是同一个键。
> 重试是否真的产生第二笔转账，取决于 bank_backend 是否按该键去重（§7），
> 而不取决于 `side_effect` 声明。
>
> **那为什么还必须声明**：因为"有键"这件事是**上游的善意**，不是接缝能强制的。
> 今天只有 Supervisor 会铸键，而 `_run_tool` 每次都铸。可接缝同时也要挡住
> **没经过 Supervisor 的调用方**——测试、脚本、跨模块调用、将来任何新的入口。
> 对那种调用方，声明就是**唯一**让它被拒绝而不是被执行的东西。
> 一条只在"所有人都守规矩"时才安全的保证不是保证。
>
> **今天这套机制是惰性的**：全仓 `app/` 里没有 `ToolSpec(...)`，六个模块包都只声明了
> `ModuleSpec(name=…, declaration=…)`，没有任何模块声明工具。**所以第一个声明工具的模块，
> 就是第一个可能踩到这个错的地方**——而不是"反正现在没人用，以后再说"。
> 这一条没有机械强制（`build_registry()` 只能拦下拼错的取值），
> 只能靠模块作者读到这段、以及 reviewer 对照逐工具契约的清单表——
> 见 §0 对"没有机械强制的规则"的处理。

#### 4.5.2 幂等键：为什么不能用 `call_id`

**这是一个会导致重复扣款的具体缺陷，不是风格问题。**

Supervisor 的 `_call_tool` 每次调用都新铸 `call_id`，
而 `_run_tool` 在失败且 `retryable` 时会**再调一次** `_call_tool`。

于是：**如果模块拿 `call_id` 当幂等键，重试恰好会产生一个不同的键**，
bank_backend 会把它当成两次独立的转账——**这正是幂等要防的那个场景，而 `call_id` 做不到。**

**MUST**：

1. **幂等键由 Supervisor 在重试循环之外铸一次**，随 `ToolCall` 送达适配器——
   **但适配器从不把它放上线，它只是把 `ToolCall` 转交给模块自己的 handler。**
   `BankToolAdapter` 从头到尾没有读过这个键（它只在写工具**缺键**时拒绝），
   所以键真正到达银行靠的是 **handler 自己转发**：
   `client.request(..., idempotency_key=invocation.call.idempotency_key, ...)`。
   照抄骨架见逐工具契约 **§4.1**。**漏掉这一行的后果是静默的**：`ToolCall` 确实带了键，
   于是 §4.5.1 那道 fail-closed 检查照样通过，**没有任何测试会失败**——
   除非模块在逐工具契约 §4「测试」项里写下"两次请求的 `idempotency_key` 相同"那条断言。
2. **重试复用同一个键**——这是重试与"用户又点了一次"的区别所在。
3. 键的来源是**本轮请求 + 这次工具调用的意图**，在进入重试循环前生成一次。
4. **`side_effect == "WRITE"` 的工具在没有幂等键时拒绝执行**，
   返回 `IDEMPOTENCY_KEY_REQUIRED`——fail closed，与 §4.1.3 同一立场。

#### 4.5.3 重试策略矩阵

**MUST**：重试条件为

| `side_effect` | 失败 retryable | 是否重试 | 理由 |
| --- | --- | --- | --- |
| `READ` | True | **重试一次**（现状不变） | 只读，重试无副作用 |
| `WRITE` | True | **仅当有幂等键时重试**，且复用同一个键 | 有键则重试安全；无键则 `IDEMPOTENCY_KEY_REQUIRED` |
| 任意 | False | 不重试 | 现状不变 |

> **别把这张表读成"READ 会重试、WRITE 不会"。** 在**正常路径**上两者都会重试一次，
> 因为 `_run_tool` 对**每个**工具（读也包括）都在重试循环之外铸一次键——
> 所以 `WRITE` 行里"仅当有幂等键"这个前提，走 Supervisor 时**总是成立**。
> 两行的真正差别只在**没经过 Supervisor 的调用方**（脚本、测试、跨模块调用、
> 将来任何新入口）身上显形：那时 `WRITE` 会 fail closed，`READ` 不会。
> 这正是 §4.5.1 警告框要防的场景，也是为什么"省略声明"不能靠"反正有键"来兜。

**MUST：`BankClient` 内部仍绝不重试**（§4.2.3）——重试永远只有一层。

### 4.6 敏感值去向：按信封字段做结构性投影

评审指出 `facts` 的边界没有定义。核实后发现**问题比"没有定义"更严重：
五条透传路径（门 1b 前）全都在跑，且全部是无约束的全量透传。**

| 去向 | 门 1b 前的行为 |
| --- | --- |
| **模型上下文** | **在跑**：`facts`、`display`、`business_ids` **全都进了模型** |
| **LangGraph checkpoint** | **在跑**：整个信封落 Postgres |
| **SSE** | **在跑**：整个信封进浏览器 |
| **前端** | **在跑**：`facts` 以原始 JSON 显示给用户 |
| **日志 / 审计** | 需要保留：排障要求完整信息 |

> 上表记的是**门 1b 前**的旧状态，本节其余部分（投影机制）已自 2026-09-24 起生效（§6.3 第 1 条）。

> 注意第一行：不只是 `facts`——**`display` 与 `business_ids` 也一起进了模型提示词**。
> 而 `business_ids` 恰恰是 §4.4.1 要"只放值、不放 kind"的那个字段，
> 它进模型上下文意味着模型能看到账号与交易号。

**门 1b 的机制：按信封字段做结构性投影（不是逐字段打标签）**

v2 初稿写的是"每个字段声明 PUBLIC/INTERNAL/SENSITIVE/SECRET 四级"。**核对后改为结构性投影**：
四级标签要求模块给 `facts` 里每个键都标级，成本高、容易漏、且**无法用一条测试证明"没有敏感值进模型"**——
因为漏标一个键就是一次泄漏。

信封的四个字段本身已经是天然的边界，**把它们的去向定死就够了**：

| 信封字段 | 模型上下文 | checkpoint | SSE | 前端 | 日志 |
| --- | --- | --- | --- | --- | --- |
| `summary` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `display` | ❌ | ❌ | ✅ | ✅ | ✅ |
| `facts` | ❌ | ❌ | ❌ | ❌ | ✅ |
| `business_ids` | **❌（契约目标）／✅（今天，见下方偏差）** | ✅ | ✅ | ✅（纯值） | ✅ |

**MUST：两条实现改动，都不需要模块做任何声明**

1. **`_run_tool` 用信封的 `summary` 字段，不再 `json.dumps` 整个信封。**
   这一处同时堵住**模型上下文**与 **checkpoint** 两条路径，
   因为 `domain_results` 是 checkpoint 的通道而它只装 summary。
2. **`tool_result` 事件下发 `{summary, display, business_ids}`，不下发 `facts`。**
   这一处堵住 **SSE** 与 **前端**。

**为什么这个方案更好**：它是**结构性**的，可以用一条测试证明——
"工具返回含 `SECRET_MARKER` 的 `facts` → 断言该标记不出现在模型提示词、checkpoint、
SSE 帧、前端数据里"。逐字段打标签做不到这一点（漏标即泄漏）。

#### 4.6.1 已知偏差：`business_ids` 今天仍然进模型提示词（2026-09-24 核实，**未修**）

**上表 `business_ids` 那一行的"❌"是契约目标，不是今天的现状。** 今天的现状是 ✅：

```text
_conclude          写 domain_results: [{agent_id, summary, instruction, business_ids}]
  → _domain_turn   用 describe_results(domain_results) 渲染领域提示词
  → describe_results  =  json.dumps(results, ensure_ascii=False, indent=2)   # 整个列表
  ⇒ business_ids 的值（如 acc_1、交易号）原样出现在模型提示词里
```

**它没有被这次投影修好，因为投影修的是另一条路。** `_run_tool` 现在只把 `summary` 放进
`domain_results`（上面第 1 条），但 `_conclude` **另外**写进了 `business_ids`，
而 `_domain_turn` 是把整个 `domain_results` 列表 `json.dumps` 出去。所以模型在
Supervisor 的第二次决策里仍然读得到这些 id。

**它也不能靠"把那个键删掉"来修**：`_collect_business_ids` 读的正是
`domain_results[...]["business_ids"]`，用来构造 `final` 事件。删掉它会直接破坏客户端契约
（§4.4.1 的 `final.business_ids`）。

**真正的修法**：把**面向模型的结果行**与**面向客户端的结果行**分开——一个只带 `summary`，
一个带 `business_ids`。这是**门 2 的工作，不在门 1b 范围内**。

> 这条偏差已被一条测试**钉住**：
> `tests/test_envelope_projection.py::test_the_business_ids_still_reach_the_model_through_domain_results`。
> 它的 docstring 写明这是"对 §4.6 的已知、刻意的偏差"，断言的是**今天存在的行为**——
> 所以**下一个真正修好它的人会让这条测试失败**，被迫连带更新文档，而不是让泄漏悄悄消失。
>
> **不要**把本节读成"已经合规"，也**不要**把上表改成"只到客户端"。**一份有偏差的文档是诚实的，
> 一份不准的表不是。**

#### 4.6.2 `tool_result` 帧的字段（含失败帧的形状）

**MUST：帧的字段位置固定如下**（客户端按位置分支，不按内容猜）：

| 帧字段 | 值 | 位置 |
| --- | --- | --- |
| `agent_id` / `tool_name` / `call_id` | 与 `tool_call` 配对 | 顶层 |
| `status` | `SUCCEEDED` / `FAILED` / `DENIED` | 顶层 |
| `data` | 成功：`{summary, display, business_ids}`；**失败或被拒：`null`** | 顶层 |
| `error` | 面向用户的一句话（或 `null`） | 顶层 |
| `error_code` | 机器可读码（或 `null`） | 顶层，**与 `error` 平级** |

- **MUST：`error_code` 是 `error` 的兄弟，不在 `data` 里。** 它描述的是**这次调用**，
  不是信封。门 2 的前端就是按这个位置分支的——把它塞进 `data` 会让失败帧无从读取。
- **MUST：失败/被拒的帧 `data` 是 `null`，不是三个 `null` 字段的投影。**
  `data` **存在**本身就在说"某个工具返回了一个信封"；造一个
  `{summary: null, display: null, business_ids: null}` 会让**失败**与
  "成功但什么都没返回"变得无法区分，也会让客户端那条 `data === null` 分支
  **恰好在它唯一该生效的场景里永远走不到**。
- `facts` **不在帧里**，任何状态都不在。帧会被浏览器、devtools 网络面板和截图读到。

**SHOULD：`display` 会到前端，所以不要往 `display` 里放用户不该看到的东西**
（内部 ID、他人账号）。`display` 是给用户看的，`facts` 是给自己用的。

**SHOULD：日志保留完整信封**（排障需要），但既有立场不变：异常原文只进日志。

> **这是一次行为变更**：`domain_results` 的内容会变小，模型看到的信息随之减少。
> 门 1b 的测试必须覆盖"模型仍能拿到它需要的信息"（否则模块会以为工具白调了）。
> 模块需要模型知道精确值时，把它写进 `summary`——`summary` 本来就是给基座消费的强契约字段。

### 4.7 显示协议

> **本节自 2026-09-24 起生效**（门 1b 于该日完成）。

> **v1 的错误**：v1 说"前端读 `display`（已由 `tool_result.data` 原样转发）"——
> **转述了一个转发，虚构了一个渲染**。核实：前端渲染的是整个信封的原始 JSON，
> `display` 今天没有任何消费者。结果就是 `facts` 里的账号与内部 ID 以 JSON 形式直接显示给用户。

**门 1b 必须做的一件事（MUST）：前端停止打印原始 JSON。**
这是**安全项**，不是外观项：它直接决定用户能不能看到 `facts` 里的账号与内部 ID。

**已落地（`1d57ae8`、`6f50c81`）**：前端不再打印信封 JSON，改为渲染
`display.blocks` 里的 `text` 块 + `summary`，未知类型跳过；`facts` 不再出现在页面上。
机械强制是 `frontend/src/toolResult.test.ts`。

**`display` 的形状（MUST）**：`display` 是一个**类型化组件列表**，
前端只认类型，不认模块。

```json
{
  "blocks": [
    {"type": "text",  "text": "您名下活期账户余额 8,320.00 元。"},
    {"type": "fields", "items": [{"label": "余额", "value": "8,320.00 元"}]},
    {"type": "table", "columns": ["日期", "金额"], "rows": [["2026-09-01", "-120.00 元"]]},
    {"type": "notice", "level": "warning", "text": "该笔转账将在 24 小时后到账。"}
  ]
}
```

> **⚠️ 上面四种块里，今天只有 `text` 真的会渲染。** 见下面"本期实际实现"。

- **MUST：空 `blocks` 合法。** 此时前端只显示 `summary`。
  **这是本期模块的默认选择**——模块可以增量补齐，与 §5.2 的字段兜底、
  `output_schema` 默认 `None` 同一思路。
- **MUST：`value` 一律是已格式化的字符串**（金额用 `format_minor`，§4.3），
  **前端不做任何数值格式化与单位换算**。理由与"模型不做算术"一致：格式化只有一处实现。
- **MUST：未知 `type` 必须降级，不得报错**——前端遇到不认识的 `type` 就跳过该块。
  这条是**前向兼容的唯一保障**，否则后端加一种块类型就会让旧前端白屏。
- **SHOULD：`display` 不是信任边界**——它是文本与表格，前端不执行其中的任何内容。

#### 4.7.1 本期实际实现：只有 `summary` + `text` 块（其余 DEFERRED）

**门 1b 计划的 Task 11（四种块的完整协议）是刻意不实施的**，计划里就标着
"**DEFERRED，本任务不实施**"。所以今天的真实渲染范围是：

| 前端拿到 | 今天渲染什么 |
| --- | --- |
| `summary` | **渲染**（这是唯一的必渲染项） |
| `display.blocks` 里的 `{"type":"text"}` | **渲染** |
| `display.blocks` 里的 `fields` / `table` / `notice` | **跳过**（未知类型降级，不报错、不白屏） |
| `display.blocks` 为空 `[]` 或 `display` 为 `{}` | 只渲染 `summary` |
| `facts` | **不渲染**（后端投影已经丢掉它；前端即使收到也不读） |

**所以模块今天写 `fields`/`table`/`notice` 块不会报错，但用户看不到它们。**
要让用户看到卡片，必须走下面的补齐流程。

**DEFERRED：四种块的完整协议**（块级字段约束、`table` 的行列数一致性与非空 `columns`、
`fields` 每项的 `label`/`value` 类型、`notice` 的 `level` 枚举、后端的块级校验）。

**降级理由**：本期**只有 `summary` 的消费者**，没有模块真的要产出卡片。
为一个还不存在的展示需求冻结四种块的字段级协议，等于产出没人验证过的规则——
而它一旦冻结，后续每次调整都要走 §6.3 的变更流程。

**第一个真正要出卡片的模块出现时**，按 §6.3 补齐这份协议并同时落地：
① 后端块级校验（返回 `OUTPUT_SCHEMA_VIOLATION`）；② 前端四种块的渲染。
**届时两端都要做**，因为理由不同——后端对**生产端**严格（模块写出渲染不出来的块时立刻失败，
而不是静默什么都不显示），前端对**消费端**宽容（旧前端遇到新后端的新块类型时跳过，而不是白屏）。

**前端改动（门 1b）**：从"打印整个信封 JSON"改为"渲染 `display.blocks`，未知类型跳过"；
`facts` 不再显示。

> **这是第二处行为变更**：用户看到的内容从"原始 JSON"变成"结构化展示"。
> 门 1b 覆盖：空 blocks、未知类型降级、`facts` 不再出现。
> **注意措辞**：是"结构化展示"，不是"卡片"——卡片是 DEFERRED 的那部分。

---

## 5. 追问字段结构化（唯一真正阻塞模块并行的前端项）

### 5.1 问题

`PendingQuestion.fields` 是裸字符串数组，前端拿它**同时当 key 和标签**用：

```html
<label v-for="field in turn.interrupt.fields" :key="field">
  <span>{{ field }}</span>                            <!-- 用户看到 recipient_name -->
  <input v-model="answers[field]" type="text" ... />
</label>
```

后果：

1. 六个模块各写各的字段名，用户会看到 `recipient_account_number` 这类内部标识——
   **内部 id 泄漏给用户的同类问题，只是发生在输入侧**。
2. 全是 `type="text"`，金额、日期没有输入约束。
3. 必填无法在前端拦住：空值会以「未填写」提交，服务端拒绝、`attempt++`，
   **白白烧掉一次追问预算**（上限只有 3 次）。

### 5.2 设计：模型仍只说字段名，元数据由模块提供

**MUST：不让模型产出结构化字段。** 模型继续输出裸名字
（`{"ask":{"fields":["recipient_name"],"display":"..."}}`），由代码补全元数据。
理由与底座既有原则一致——模型负责决策，代码负责控制；让模型写 UI 元数据只会引入新的幻觉面。

元数据**归属 `DomainDeclaration`**：

```python
@dataclass(frozen=True, slots=True)
class FieldMeta:
    label: str                        # 面向用户的中文标签，如"收款人姓名"
    type: str = "text"                # text | number | date | select
    required: bool = True
    options: tuple[str, ...] = ()     # 仅 select 使用
```

`DomainDeclaration` 增加 `fields: Mapping[str, FieldMeta] = field(default_factory=dict)`。

> **对早期讨论的一处修正**：原先设想放在 `ModuleSpec.fields`。
> 字段元数据是"领域对自己提问方式的声明"，与 `display`/`purpose` 同类，
> 应该和它们放在一起。**一个真源，不是两个。**

**MUST NOT：从 `ToolSpec.parameters` 的 JSON Schema 推导类型**——看着优雅，实际不成立：
`ask` 不携带工具名，追问发生在调用工具**之前**，此时无法确定该用哪个工具的 schema。

**MUST：未声明的字段有兜底**——`{name: 字段名, label: 字段名, type: "text", required: True, options: []}`。
这条兜底很重要：**六个模块可以并行开工，谁先声明谁先受益，没声明的不会报错也不会阻塞**。

**SHOULD**：模块应声明自己所有追问字段的 `FieldMeta`，否则用户会看到 `recipient_name` 这类内部标识。
（SHOULD 而非 MUST：兜底路径不报错，没有机械强制能拦住"忘声明"。）

### 5.3 SSE 契约变更

**MUST**：`interrupt.fields` 从 `string[]` 变为对象数组：

```json
{"kind":"input","question_id":"q_...","agent_id":"transfer","attempt":1,
 "display":"请补充收款人信息",
 "fields":[{"name":"recipient_name","label":"收款人姓名","type":"text","required":true,"options":[]}]}
```

这是**破坏性变更**，但只有一个消费者（本仓前端，同一次改动内更新），
且符合仓库既有的"不做双协议"立场（README 明确废掉旧事件）。
**MUST NOT：不保留裸字符串兼容分支。**

### 5.4 改动边界

后端改动的**实施步骤**在门 1a 计划里（Task 12–16），本节只固定契约事实：

- **MUST**：富化的**唯一边界**是 Supervisor 构造追问的那一刻。
  模型输出的裸名字协议**不变**，解析层（`decisions.py` 的 `as_fields()`）不需要改。
- **MUST**：重问路径上 checkpoint 里的 `fields` **已经是对象**，原样沿用，不再当作裸名字。
- **MUST**：`agents/runtime.py` 与 `api/sse.py` 无需改动（`fields` 原样透传）。
- **MUST**：服务端的答案校验独立于前端（前端不是信任边界）。
- **SHOULD**：必填校验放前端——空值禁用提交，而不是提交「未填写」去消耗追问预算。

---

## 6. 交付与验收

### 6.1 每个模块的固定交付（五件，缺一不算完成）

| # | 交付 | 依赖 | 验证什么 |
| --- | --- | --- | --- |
| 1 | `ModuleSpec`（declaration + tools + handlers + fields） | 无 | 声明合法：`build_registry()` 通过 |
| 2 | 单测：生产 `BankToolAdapter` + `FakeBankClient` + 单域脚手架 | **无** | 工具被调用、参数正确、信封完整、**错误码正确**、幂等键被透传 |
| 3 | 集成测试：真 `BankClient` 打 8001 | 需本地 bank_backend | 真实 HTTP、真实数据、身份映射生效 |
| 4 | `display` 产出（**可为空**，块协议 DEFERRED） | 无 | `facts` 不出现在页面上；未知块类型降级不白屏 |
| 5 | 逐工具契约填写（`module-tool-contract-v1.md`） | 无 | effect / 幂等 / 重试 / fixture 逐工具声明 |

模块单元测试应使用生产 `BankToolAdapter` 和 `FakeBankClient`，隔离验证 handler 与结果信封。需要确认真实 HTTP 调用时，再运行独立 `bank_backend` 并执行集成测试；测试跳过时不能记作集成通过。

**MUST：第 2 件的适配器必须是生产的 `BankToolAdapter` 配一个 `FakeBankClient`**——
**不是 `RecordingAdapter`**。`RecordingAdapter` 从固定答复里返回数据，会绕过模块自己的 handler，
测出来的是空气。

### 6.2 当前接入状态

接缝协议、注册表校验、适配器和单域测试脚手架已实现。六个领域包当前只有声明，没有生产 `ToolSpec`、handler 或授权，因此生产工具目录为空，Agent 对话不会访问 `bank_backend`。

接入一个业务工具仍需：在领域包声明 `ToolSpec` 并实现 handler；按逐工具契约补齐身份、输入输出、写安全和错误行为；增加单域测试；需要验证 HTTP 集成时运行独立的 `bank_backend` 并执行集成测试。完成这些工作后，才可把该工具描述为已由 Agent 接入。

### 6.3 契约冻结与变更流程

1. **SHOULD：契约接口按 v3 管理。** §3–§5 是工具接入时的接口约束；当前实现以代码和测试为准，本文不以历史提交号或阶段日期作为强制依据。
2. **SHOULD：姊妹文档 [`module-tool-contract-v1.md`](2026-09-22-module-tool-contract-v1.md)
   按模块增量维护**：每新增一个工具，在 [`module-tool-contract-v1.md`](2026-09-22-module-tool-contract-v1.md) 补充逐工具的输入/输出 Schema、effect、幂等和测试数据。
3. **SHOULD：变更必须先写"原因、受影响字段、调用方、迁移方式"**，由接缝负责人确认后再合并。
   不要只在群里改接口。
4. **SHOULD：契约变更后，受影响工具的测试必须重跑。**
5. **SHOULD：新增一条 MUST 规则时，同时指出它的机械强制点**（哪条注册表校验、哪个测试）。
   指不出来就标 SHOULD（§0）。

---

## 7. 后续模块必须接管（DEFERRED，本期不解决）

下面的能力不由当前接缝契约实现。**"已写入契约"不等于"已经具备安全防护"；接入生产工具前应逐项评估。**

| 后续模块 | 必须补上的能力 | 本期保留的契约事实 |
| --- | --- | --- |
| 安全 / 风控 | 写操作的不可伪造确认凭据（确认 token、意图绑定、resume 绑定、MFA） | 工具是否需要确认、确认字段和用户可见文案写在逐工具契约；接缝只提供 `ask` interrupt + `FieldMeta` |
| 安全 / 交易基础设施 | 幂等键的服务端强制、重复提交、并发请求、超时后未知结果和跨 worker 防重 | `side_effect`、`idempotency_key` 字段以及"重试复用同键"的语义已固定 |
| 安全 / 数据 | 字段级脱敏、访问控制、数据保留期限 | 信封四字段的**结构性投影**已固定（§4.6）；**但 `business_ids` 今天仍进模型提示词，见 §4.6.1 的已知偏差** |
| 审计 | 审计留痕、可追溯的操作日志 | 日志保留完整信封（§4.6） |
| 前端 | 四种显示块的完整协议与渲染 | 空 blocks 合法、未知类型降级、`facts` 不上前端（§4.7） |
| 平台 | 跨模块消费的版本协议 | 禁止直接读别的模块内部状态（§4.4.4） |
| 平台 | 多 worker / 分布式锁 | 维持设计文档的非目标 |
| 安全 | `database.txt` 明文凭据轮换、git 历史清理 | 接缝不引入新的凭据处理 |

---

## 8. 风险

| # | 限制 | 接入时的处理 |
| --- | --- | --- |
| 1 | `interrupt.fields` 破坏性变更 | 只有一个消费者，同批更新；不留兼容分支。前端 fixture 一并改 |
| 2 | `build_registry()` 在 import 时执行 | 刻意为之：非法声明应启动即失败。风险是 import 顺序耦合，用测试锁定 |
| 3 | 同步 HTTP 跑在 LangGraph 线程池 | 已存在的天花板，本轮只写进文档。并发会话数受线程池上限约束 |
| 4 | `bank_customer_id` 缺失时工具不可调用 | `BankClient` fail closed；启用银行工具前确认演示用户映射和身份测试 |
| 5 | 测试只验证 adapter 与 fake client，不能证明银行 HTTP 集成 | 需要端到端验证时单独启动 `bank_backend` 并运行真实 HTTP 集成测试 |
| 6 | Agent 服务仍有与本接缝无关的旧存储表和独立邮件通知代码 | 它们不表示银行业务工具已接入；清理或整合需按各自模块评估 |
| 7 | `side_effect` 默认值是 `READ` | 注册表拒绝非法取值，但无法判断工具作者遗漏声明；逐工具契约和评审必须确认每个工具显式声明副作用 |
| 8 | 模型可能需要工具返回的精确业务值 | handler 应将后续决策必需的信息放入 `summary` 或受 schema 约束的 `facts`，并由测试验证传递行为 |
| 10 | **确认 / MFA 不在接缝层做** | 见 §1.3。接缝只保证"不重复扣款"与"不串户"。**这个缺口必须让写模块团队明确知道** |
| 11 | **`facts` 是模块私有，但跨模块调用需要稳定形状** | 由 `output_schema` 约束（§4.4.2）。未声明 `output_schema` 的工具，其 `facts` 不得被其他模块消费 |
| 12 | **契约可能与代码漂移** | 以代码和测试为当前行为真源；变更工具协议时同步更新本文，并核对对应的契约测试 |
| 13 | `business_ids` 可能进入模型可见的跨域结果 | §4.6.1 记录此数据流及测试约束；提供工具时避免在 `business_ids` 放入敏感或不应供模型使用的值 |
| 14 | 前端只渲染已支持的 display block 类型 | 未知块会跳过；模块应提供兼容的 `summary`，并通过前端测试确认用户可见结果 |
