# 理财整轮接入底座 Spec

状态：已实现（2026-10-11；实现见「修订」，验收见 plan Task 7）
日期：2026-10-11
基线：dev @ `93d084de3ffb65b39cb6e494c1af4779e7611bb0`（工作区另有 5 个已修改文件与 2 个未跟踪项，见 §0.3）
关联：`docs/wealth/README.md`、`docs/wealth/理财子智能体从零到实现计划.md`、`docs/superpowers/specs/2026-10-10-conversation-serial-and-auto-title-design.md`

修订：
- 2026-10-11 初版。
- 2026-10-11 自审修订：删掉「用 `asyncio.to_thread` 包住同步 `handle`」的设想——底座现有的模型调用本来就发生在同一个同步节点里（`backend/app/agents/supervisor.py:625`），没有额外的桥接要做；补上 `WealthAgent.set_model`（现在没有这个方法，而底座运行期会换模型）。
- 2026-10-11 实现（plan Task 1–6 全部 accepted，Task 7 完成验收）。
- 2026-10-11 实现后修订（**短回答死循环**，用户拍板「方案 1 + 1b」）：§8 的「基座只发 `MESSAGE`」不再成立——上一轮是 `WAITING_INPUT` 时 `WealthTurn` 改发 `ANSWER`（带 `pending_id`），失效则回退 `MESSAGE`；并补上 `shown` 的跨轮地面真值。§8 关键点 1、2 与 §11 的对应条同步改写。
- 2026-10-11 实现后修订（**推荐金额被当成订单金额**，实测发现）：理财自己的会话槽位里 `amount_minor` 一名两用——推荐阶段是「打算投多少」（筛选条件），申购／赎回是「这一笔多少钱」（订单参数）。推荐轮把 20 万写进会话后，申购节点从会话读到它，而模型看到提示词里已经有「金额」就不再写，于是 20 万被当成订单金额去预检（会话 `conv_fae9467075474653af8d1e4f52530b7a`；同一条路径在另一个会话里又碰巧是对的——概率性缺陷）。修法**在理财模块内部**，不属于 S-1..S-7 的任何一条接缝：计划金额改名 `plan_amount_minor`，`amount_minor` 退出 `SLOTS`，只能随 `prepare_purchase`／`prepare_redeem` 进入会话（`normalize_slots(allowed=...)`），于是推荐轮结束时会话里没有它，申购节点按 `PURCHASE_SLOTS` 算出缺它并追问（除非模型在 `prepare_purchase` 里显式写了金额）；追问（`ask_user`）仍然允许点名 `amount_minor`——「还没有填写的槽位」那份清单只遍历 `SLOTS`、永远不含它，所以提示词里把 `amount_minor` 点为追问规则的唯一例外。落点 `backend/app/modules/wealth/workflow/state.py:108`（`SLOTS`）与 `:120`（`ASKABLE_SLOTS`）、`backend/app/modules/wealth/llm/prompts.py:73`。
- 2026-10-11 已知限制（**不修**，实现后审查记录）：`slot_lines` 只渲染 `SLOTS` 里的槽位，所以会话里已经记住的订单金额对模型不可见；后一笔申购／赎回若不写金额，会被上一笔留下的值满足（`backend/app/modules/wealth/workflow/nodes.py:552`）而不追问。写路径本身 fail-closed，且预览里会显示金额供用户确认，故留作已知限制。

## 0. 本文件的定位与声明

本文件只写「做什么」，不写「怎么做」；每一条都能用一条会变红的用例证明，并在「改哪里会变红」里指明位置。

### 0.1 这一版解决什么

前端实测（会话 `conv_d1499a8f52d5414b843f770b99705f94`）里，用户问「有什么产品？」拿到的是「抱歉，这个请求暂时没能完成：共查到 2 只产品，其中可灵活赎回 0 只。」。根因不是工具错、也不是银行错，是底座和领域之间少了一段：

- 底座替领域跑了一次工具调用，只把工具的 `summary` 当结论（`backend/app/agents/supervisor.py:569-571`），`facts` 里的明细（产品名、期限、预期收益）从来没进过主 agent 的提示词；
- 领域 agent 自己也看不到自己刚查回来的结果——每次委派只给它一次模型调用（`backend/app/agents/supervisor.py:603-625`，没有 observation 回环），于是每一圈都重复同一个调用；
- 跑满 `MAX_DOMAIN_TURNS=4`（`backend/app/agents/supervisor.py:58`）之后由一条**不调用模型**的兜底文案收场（`backend/app/agents/supervisor.py:211-214`、`:844-861`）。

这一版把那一段补上：**一轮理财请求整段交给理财 agent（它自己的模型循环、自己的会话槽位、自己的风险门禁），它给出结论之后，主 agent 的模型再过一遍，把结论和明细讲给用户。**

### 0.2 六条已拍板的决策

| # | 决策 | 出处 |
|---|---|---|
| D-1 | 待办/追问归理财自己管：基座只传 `session_id`，用户下一句进来由理财自己的 `pending_prompt` 续上。代价：前端「补充信息卡片」退化成普通对话 | 用户拍板（选项 1） |
| D-2 | 写路径（申购/赎回确认 + 验证码）不接，fail-closed：如实告知「尚未接入」，不发任何写请求 | 用户拍板 |
| D-3 | 那 7 个只读工具**不再**声明在 `ModuleSpec.tools` 里；主 agent 不保留直调能力，工具归理财 agent 的整轮内部使用 | 用户改判（否决了我保留声明的建议） |
| D-4 | 理财会话第一版不落库（进程内存）。重启丢的是「上一句问过什么」，不影响余额/持仓/订单 | 用户拍板 |
| D-5 | 顺手修 `_closing_reply`：不再把写给 Supervisor 看的内部文案套上「抱歉」前缀抛给用户 | 用户拍板 |
| D-6 | 验收两条都要：pytest 覆盖接缝 + 前端真实跑一遍截图里的三问 | 用户拍板 |

用户另外否决过一次我的主张（保留原文）：领域答复**不要原样透传**，要给主 agent 过一遍模型，这样在主 agent 看来任务是完成的有结果的。§5 就是按这条设计的。

### 0.3 与工作区现有未提交改动的关系

撰写本 spec 时 `git status --porcelain` 有 5 个已修改文件（`backend/app/modules/wealth/__init__.py`、`backend/app/storage/seed.py`、`backend/tests/test_identity_binding.py`、`backend/tests/test_interrupt_reset_integration.py`、`backend/tests/test_module_registry.py`）和 2 个未跟踪项（`backend/pytest_full.txt`、`test_reports_20261007/`）。其中 3 个文件正是本 spec 要再改的：`wealth/__init__.py`（D-3）、`test_module_registry.py`（D-3）、`test_interrupt_reset_integration.py`（注释）。另外 `seed.py` / `test_identity_binding.py` 的改绑（演示用户 `user_0001` → `u_wealth`）与本 spec 无关，不要回退。

**收尾状态（2026-10-11，已合并进 dev）**：那 5 个已修改文件全部了结——3 个被本 spec 的 Task 6 取代（`backend/app/modules/wealth/__init__.py` 改成 `turn=build_turn`、`backend/tests/test_module_registry.py` 改写、`backend/tests/test_interrupt_reset_integration.py` 只留注释），`backend/app/storage/seed.py` / `backend/tests/test_identity_binding.py` 的改绑**已单独提交**（`chore(seed): bind the demo user to the wealth demo customer`，没有混进本 spec 的任何一个提交）。两个陈旧日志 `backend/pytest_full.txt`、`backend/pytest-dev-merge.txt` 已删除；`test_reports_20261007/` 保留（2026-10-07 的全面测试报告，仍未跟踪）。

## 1. 需求

- R-1 一个模块可以声明「整轮」：这一轮由模块自己跑完，底座不再替它做单次工具调用。
- R-2 整轮的结果（状态、给用户看的话、明细、业务号）完整进入主 agent 的提示词。
- R-3 主 agent 的模型基于 R-2 的材料写出给用户的回复；模型不可用时退回领域的结论原文，不退化成「抱歉…」。
- R-4 领域说「我在等用户补充」时，用户下一句仍然路由到同一个领域。
- R-5 只读工具不再出现在底座 ToolCatalog 里（D-3）。
- R-6 写路径如实拒绝（D-2）。
- R-7 其他 5 个领域（transfer / bill / card / subscription / life_event）的行为一个字都不变。

## 2. 既有事实（本文件的依据）

| 事实 | 位置 |
|---|---|
| `ModuleSpec` 只有 `name` / `declaration` / `tools` / `handlers` / `grants` 五个字段，没有承载「整轮」的字段 | `backend/app/modules/base.py:102-110` |
| `ModuleSpec.__post_init__` 已经在做「声明了没人能到的东西就报错」的校验 | `backend/app/modules/base.py:112-135` |
| 底座每次委派只给领域一次模型调用，没有把工具结果回喂的回环 | `backend/app/agents/supervisor.py:603-625` |
| 领域交给 Supervisor 的结论就是工具 `summary` 的原文，`facts` 被丢在 `domain_results` 之外 | `backend/app/agents/supervisor.py:569-571`、`:583-593` |
| envelope 投影只留 `summary`/`display`/`business_ids`，`facts` 从不出服务端 | `backend/app/agents/supervisor.py:705-716` |
| `_already_concluded` 只认 `NO_TOOLS_SUMMARY`，重复但无用的工具摘要不触发，必然跑满 4 圈 | `backend/app/agents/supervisor.py:830-841`、`:58` |
| 撞上限后走 `_closing_reply`，注释明写这条路上不调用模型 | `backend/app/agents/supervisor.py:211-214`、`:844-861` |
| `describe_results` 就是把 `domain_results` 原样 `json.dumps` 进提示词 | `backend/app/agents/prompts.py:122-127` |
| 理财模块**已经**实现了「工具结果回喂模型 + 程序化推荐」：`read_messages(observations=...)`、`recommend_node` 由程序按槽位+银行测评筛选 | `backend/app/modules/wealth/workflow/nodes.py:199-211`、`:380` |
| 理财自己的预算与状态字段都已就位 | `backend/app/modules/wealth/workflow/state.py:8-9`、`:368-416` |
| `WealthAgent` 的会话状态是进程内字典，键是 `(user_id, session_id)` | `backend/app/modules/wealth/application/runtime.py:336-345`、`:494-502` |
| `WealthAgent.handle` 同步、永不抛异常，末尾 `_settle` | `backend/app/modules/wealth/application/runtime.py:347` |
| `_respond` 的 `COMPLETED` 分支已经带 `facts` 与 `business_ids` | `backend/app/modules/wealth/application/runtime.py:1354-1360` |
| `WAITING_INPUT` 的 `summary` 就是那句追问原文 | `backend/app/modules/wealth/application/runtime.py:1325-1337` |
| `WealthAgent` **没有** `set_model`，模型在构造期钉死 | `backend/app/modules/wealth/application/runtime.py:331`、`:421` |
| 底座运行期会换模型（配置面板保存后 `set_model`） | `backend/app/integration/llm_config.py:117`、`:121`；`backend/app/agents/runtime.py:201-204` |
| 底座构造期用的是 `UnconfiguredModelClient()`，真模型是后来注入的 | `backend/app/runtime.py:86` |
| 理财与底座共用同一个 `ModelClient` 协议，不需要模型适配层 | `backend/app/modules/wealth/application/factory.py:18`；`backend/app/integration/model_client.py:119-122` |
| `AgentState` 没有跨轮记忆字段；没被写过的通道保留 checkpoint 里的旧值 | `backend/app/agents/state.py:80-95`、`:137-171` |
| `build_domain_specs` / `build_agent_graph` 只被关键字参数调用，加 keyword-only 参数安全 | `backend/tests/support/domain_harness.py:148`、`:162` 等 |
| `app.agents.__all__` 有一个独立 oracle 断言，加公开名字必须同步改 | `backend/tests/test_import_cycle.py:66-92` |

## 3. S-1 一个模块可以声明「整轮」

**现状与证据。** `ModuleSpec` 只有工具这一种「对外能力」的表达方式（`backend/app/modules/base.py:102-110`）。理财的能力显然比一次工具调用大：它有九种输出的模型循环、槽位、风险门禁、推荐（`backend/app/modules/wealth/workflow/graph.py:99-134`）。

**目标行为。**

1. 新增 `backend/app/agents/turn.py`，只放领域↔底座这一层的契约类型：

   ```python
   class TurnStatus(StrEnum):
       COMPLETED = "completed"          # 办完了，summary/facts 可以给用户看
       WAITING_INPUT = "waiting_input"  # 在等用户补充；summary 就是那句追问
       UNSUPPORTED = "unsupported"      # 这条流程模块自己也没接；summary 是如实说明
       FAILED = "failed"                # 没办成；summary 是给用户看的失败说明

   TERMINAL_TURN_STATUSES = frozenset(status.value for status in TurnStatus)

   @dataclass(frozen=True, slots=True)
   class TurnResult:
       status: TurnStatus
       summary: str
       facts: dict[str, Any] = field(default_factory=dict)
       business_ids: dict[str, Any] = field(default_factory=dict)

   TurnHandler = Callable[[TrustedContext, ModelClient, str], TurnResult]
   TurnFactory = Callable[[BankClient], TurnHandler]
   ```

2. `TurnResult.__post_init__`：`status` 必须是 `TurnStatus`，`summary` 必须是非空字符串，`facts`/`business_ids` 必须是 `dict`。理由与 `ToolData` 一致（`backend/app/modules/base.py:20-47`）：这两样东西的下游是提示词和用户界面，脏数据要在源头拦。

3. `ModuleSpec` 增加第六个字段 `turn: TurnFactory | None = None`（`backend/app/modules/base.py`）。默认 `None` 时行为与本 spec 之前完全一致。

4. `ModuleSpec.__post_init__` 增加两条校验：
   - `self.turn is not None and self.tools` → `ValueError(f"module {self.name!r} declares both a turn handler and tools: one request cannot be answered twice")`。两种声明表达的是同一件事——谁掌握这一轮的控制权——同时声明必然有一边永远不跑。
   - `self.turn is not None and self.declaration is None` → 与既有的「declares tools nobody can reach」同一条理由：没有 declaration 的模块进不了 roster，Supervisor 永远路由不到它。

5. `DomainSpec` 增加字段 `turn: TurnHandler | None = None`（`backend/app/agents/declaration.py:59-85`）。

6. `app/agents/__init__.py` 的惰性重导出表加四个名字：`TurnHandler` / `TurnFactory` / `TurnResult` / `TurnStatus` → `app.agents.turn`（`TERMINAL_TURN_STATUSES` 只给 supervisor 用，不导出）。**同时**把 `backend/tests/test_import_cycle.py:74-88` 的 oracle 集合补上这四个名字——那个测试的注释明说它是独立 oracle，不是从 `__all__` 读的。

**注意（实现时的坑）。** `TERMINAL_TURN_STATUSES` 必须是**字符串**的 frozenset（`status.value`），不能是枚举成员的集合：`enum.Enum` 的 `__hash__` 用的是成员名（`hash("COMPLETED")`），`"completed" in {TurnStatus.COMPLETED}` 是 `False`。下游拿到的 `turn_status` 一律是 `.value`。

**改哪里会变红：** `backend/app/modules/base.py:102-110`、`:112-135`；`backend/app/agents/declaration.py:59-85`；`backend/tests/test_module_registry.py`（新增两条校验用例）；`backend/tests/test_import_cycle.py:74-88`。

## 4. S-2 底座把整轮交给领域

**现状与证据。** `domain_decide` 无条件走 `_with_tools_available(spec, _domain_turn(...))`（`backend/app/agents/supervisor.py:274-291`），没有第二条路。

**目标行为。**

1. `domain_decide(model, spec, context=None)` 分流：

   ```python
   writer({"event": "agent_start", "agent_id": spec.agent_id})
   if spec.turn is not None:
       return _conclude_turn(spec, _run_turn(spec, context, model, state), writer)
   outcome = _with_tools_available(spec, _domain_turn(model, spec, state))
   ```

2. 新增 `_run_turn(spec, context, model, state) -> TurnResult`：把用户这一轮的原话（`_current_request(state)`，`backend/app/agents/supervisor.py:781-789`）交给 `spec.turn(context, model, message)`，并用 `except Exception` 接住一切——领域整轮里跑着它自己的模型循环和银行调用，那两件事都会失败，不能让异常穿过图。接住之后记一条 `logger.exception("domain turn crashed: agent=%s type=%s", spec.agent_id, type(error).__name__)`，返回 `TurnResult(status=TurnStatus.FAILED, summary="本次处理未能完成，请稍后重试。")`（新常量 `TURN_FAILED_SUMMARY`）。

3. 新增 `_conclude_turn(spec, result, writer) -> dict[str, Any]`：

   ```python
   writer({"event": "subtask_end", "agent_id": spec.agent_id,
           "status": _TURN_EVENT_STATUS[result.status].value, "summary": result.summary})
   return {
       "domain_results": [{
           "agent_id": spec.agent_id,
           "summary": result.summary,
           "turn_status": result.status.value,
           "facts": dict(result.facts),
           "business_ids": dict(result.business_ids),
       }],
       "active_agent": None,
       "awaiting_agent": spec.agent_id if result.status is TurnStatus.WAITING_INPUT else None,
       **idle_work_state(),
   }
   ```

   `facts` 必须原样进 `domain_results`：主 agent 就是靠它把明细讲给用户的，上一次联调里丢掉的正是这一段。返回形状（含 `"active_agent": None` 与 `**idle_work_state()`）沿用 `_conclude`（`backend/app/agents/supervisor.py:583-593`）。

   `_TURN_EVENT_STATUS` 是新增的模块级映射（`Mapping[TurnStatus, ToolStatus]`）：`COMPLETED`/`WAITING_INPUT` → `ToolStatus.SUCCEEDED`、`UNSUPPORTED` → `ToolStatus.DENIED`、`FAILED` → `ToolStatus.FAILED`。**事件里的 `status` 必须是 `ToolStatus`**：`docs/ARCHITECTURE.md:422` 冻结的就是 `SUCCEEDED` / `FAILED` / `DENIED`，而前端把 `subtask_end` 和 `tool_result` 渲染在同一条活动流里（`frontend/src/App.vue:656-661`），两处用两套词会读起来像两个系统。真正的整轮状态仍然以 `turn_status` 进 `domain_results`——那是给主 agent 看的，不是给前端看的。

4. 非整轮的 `_conclude` 也要写 `"awaiting_agent": None`（`backend/app/agents/supervisor.py:583-593`）：这个通道是 last-value，不写就留着上一次的值，会让下一次路由误判。**注意 `domain_results` 是 append 通道而 `awaiting_agent` 是 last-value，两者可以同一次返回。**

5. `build_domain_specs(gateway, context, *, roster=DOMAIN_ROSTER, turns=None)`（`backend/app/agents/domain.py:26-37`）：`turns: Mapping[str, TurnHandler] | None`，按 `declaration.agent_id` 取；取到就填进 `DomainSpec.turn`。

6. `build_agent_graph(..., turns=None)`（`backend/app/agents/domain.py:40-61`）把 `turns` 转交给 `build_domain_specs`，并把 `context` 一并交给 `build_supervisor`。

7. `build_supervisor(model, domains, *, assembler=None, turn=None, context=None)`（`backend/app/agents/supervisor.py:898-931`）：把 `context` 传进 `domain_decide`。开头加一条接线校验——`if any(spec.turn is not None for spec in domains) and context is None: raise ValueError("a turn handler needs the trusted context of the request")`——在图构建期就炸，而不是在用户这一轮里炸。`build_domain_graph(model, spec, context=None)`（`:934-960`）同步改。

8. **不引入 `asyncio.to_thread`。** `domain_decide` 是同步节点，底座现有的阻塞模型调用本来就发生在同一个节点里（`backend/app/agents/supervisor.py:625`），理财的同步 `handle` 放进来是同一种东西，不是新的桥接问题。（初版设想里我写了 `asyncio.to_thread`，自审删掉。）

9. **整轮模块不进 wait/process 通道。** `_conclude_turn` 返回 `idle_work_state()`（`turn_phase="decide"`），`domain_route`（`:597-600`）因此返回 `END`，`wait`/`process_answer` 对整轮模块不可达。这是不变量，不是巧合：整轮模块自己不产生底座级的追问（D-1 把追问留给模块自己）。

**改哪里会变红：** `backend/app/agents/supervisor.py:274-291`、`:583-593`、`:898-960`；`backend/app/agents/domain.py:26-61`；新增 `backend/tests/test_domain_turn.py`。

## 5. S-3 主 agent 过一遍领域结论

**现状与证据。** `describe_results` 把 `domain_results` 原样 `json.dumps` 进提示词（`backend/app/agents/prompts.py:122-127`），而 `domain_results` 里从来没有过 `facts`（`backend/app/agents/supervisor.py:583-593`）。提示词又明确禁止主 agent 编造（`backend/app/agents/prompts.py:40`），于是它只能一遍遍升级指令重复索取。

**目标行为。**

1. `SUPERVISOR_PROMPT`（`backend/app/agents/prompts.py:13-42`）在既有的「已经拿到的结果不要重复索取」旁边补两条：
   - 「已经给出结论的领域（结果里有 `turn_status`）不要再派给它；直接用 `agent=null` 把它的 `summary` 和 `facts` 转述给用户。」
   - 「`facts` 里的明细（产品名称、期限、预期收益、风险等级等）要讲给用户，但不得增删事实。」

2. `_already_concluded(results, agent_id)`（`backend/app/agents/supervisor.py:830-841`）扩成两种结论都认：

   ```python
   for item in results:
       if item.get("agent_id") != agent_id:
           continue
       if item.get("summary") == NO_TOOLS_SUMMARY:
           return True
       if item.get("turn_status") in TERMINAL_TURN_STATUSES:
           return True
   return False
   ```

   作用：模型若无视提示词、又把同一个整轮模块派一次，底座在这里拦住并直接走 `_closing_reply`，不再跑第二圈。

3. **三条保险**（对应 R-3）：
   - `facts` 必须进主 agent 的提示词（上面第 1 条 + §4 第 3 条）；
   - 提示词约束「不得增删事实」；
   - 主 agent 的模型调用失败时，退回领域的结论原文而不是「抱歉…」：`decide` 的 `except AppError`（`backend/app/agents/supervisor.py:234-236`）在 `any(item.get("turn_status") for item in results)` 成立时返回 `_final_from(results, writer, turns)`，否则照旧 `raise`。**守卫是必须的**——它只对整轮结论生效，另外 5 个领域的模型失败仍然向上抛（R-7）。

4. **不动的部分**：主 agent 的循环结构基本不改。它在领域返回之后本来就有一次模型调用（就是现在空转的那 4 圈），只是这次喂给它的料够了。

**改哪里会变红：** `backend/app/agents/prompts.py:13-42`；`backend/app/agents/supervisor.py:830-841`；新增 `backend/tests/test_domain_turn.py`（断言 `facts` 出现在主 agent 收到的提示词里）。

## 6. S-4 领域说「在等用户补充」时，下一句还路由给它

**现状与证据。** 基座没有跨轮记忆字段（`backend/app/agents/state.py:80-95`）。用户答一句「随时赎回」，主 agent 只能凭转录猜这句属于谁。理财自己的 `pending_prompt` 能识别（`backend/app/modules/wealth/workflow/nodes.py:207-209`），但前提是这句得先被路由到理财。

**目标行为。**

1. `AgentState` 增加通道 `awaiting_agent: str | None`（`backend/app/agents/state.py:80-95`）。
2. `goal_reset_state()`（`:114-134`）把它清成 `None`；`new_agent_state`（`:137-171`）**不**写它——跨轮记忆正是靠「没被写过的通道保留 checkpoint 里的旧值」这个机制留下来的。
3. `supervisor_user_turn(request, menu, awaiting=None)`（`backend/app/agents/supervisor.py:792-803`）在 `awaiting` 非空时补一条规则：「上一轮 `{awaiting}` 正在等用户补充信息；如果用户这句话是在回答它，请仍然派给 `{awaiting}`。」`supervisor_node.decide` 里从 `state.get("awaiting_agent")` 读出来传进去。
4. 通道由 `_conclude_turn` 写（§4 第 3 条），由 `_conclude` 清（§4 第 4 条）。

**边界。** 这只是**路由提示**，不是硬绑定：主 agent 仍可以判断用户换了话题，把请求派给别的领域。理财自己也会处理「用户说的是无关新事情」（`backend/app/modules/wealth/llm/prompts.py:112-116` 的 `RESUME_NOTICE`）。

**改哪里会变红：** `backend/app/agents/state.py:80-95`、`:114-134`；`backend/app/agents/supervisor.py:792-803`、`:207`；新增 `backend/tests/test_domain_turn.py`（两轮：第一轮 `WAITING_INPUT`，第二轮断言路由回同一个 agent）。

## 7. S-5 收尾文案不再把内部结论说成失败

**现状与证据。** `_closing_reply`（`backend/app/agents/supervisor.py:844-861`）在**有结论**时也套上「抱歉，这个请求暂时没能完成：」前缀，把写给 Supervisor 看的内部文案（计数、工具名、ISO 时间戳）当成给用户的话。截图里那句「抱歉…中风险（当前有效，有效期至 2026-10-27T14:49:54.628613+08:00）。；共查到 2 只产品…」就是这么来的。

**目标行为。**

```python
def _closing_reply(results: list[dict[str, Any]]) -> str:
    """领域已经有结论时把结论原样交给用户，一条都没有才说没完成。

    「抱歉，这个请求暂时没能完成：<结论>」把写给 Supervisor 看的内部文案当成了给用户的
    话。结论本身已经是给用户看的（领域的 summary、或能力边界的如实说明），前缀只会让一句
    好话读起来像一次失败。
    """
    conclusions = [str(item["summary"]) for item in results if item.get("summary")]
    if not conclusions:
        return "抱歉，这个请求暂时没能完成，请换一种说法再试一次。"
    return "；".join(dict.fromkeys(conclusions))
```

**会跟着变红的既有断言**（它们是这条改动的验收，不是要绕开的障碍）：
- `backend/tests/test_domain_agents.py:336`：`assert finals and finals[-1] == "抱歉，这个请求暂时没能完成：处理过了"` → 期望值变成 `"处理过了"`。
- `backend/tests/test_domain_agents.py:360`：`assert events[-1].data["text"] == "抱歉，这个请求暂时没能完成：" + NO_TOOLS_SUMMARY` → 期望值变成 `NO_TOOLS_SUMMARY`。
- `backend/tests/test_domain_agents.py:643` 只断言 `summaries == [NO_TOOLS_SUMMARY]`，不受影响。

**改哪里会变红：** `backend/app/agents/supervisor.py:844-861`；`backend/tests/test_domain_agents.py:336`、`:360`。

## 8. S-6 理财侧的整轮适配器

**目标行为。**

1. `WealthAgent` 增加 `set_model(self, model: ModelClient) -> None`（`backend/app/modules/wealth/application/runtime.py`，紧邻 `:331` 的构造）。理由：底座运行期会换模型（`backend/app/integration/llm_config.py:121`），而理财 agent 是进程级的长寿对象，模型不能在构造期钉死。docstring 写清这一点。

2. 新增 `backend/app/modules/wealth/application/turn.py`：

   ```python
   UNSUPPORTED_WRITE_SUMMARY = (
       "理财的申购、赎回和验证码确认还没有接入主助手，本次不能办理。"
       "产品、持仓和风险测评可以继续查询。"
   )
   NO_SUMMARY = "本次没有可以回复的内容。"

   _STATUS = {
       WealthStatus.COMPLETED: TurnStatus.COMPLETED,
       WealthStatus.WAITING_INPUT: TurnStatus.WAITING_INPUT,
       # 用户自己撤掉的那一步也是办完了，它的 summary 就是「已取消」。
       WealthStatus.CANCELLED: TurnStatus.COMPLETED,
       WealthStatus.FAILED: TurnStatus.FAILED,
   }
   # WealthStatus 还有 UNKNOWN，它落进 .get 的默认值 FAILED。

   class WealthTurn:
       """一个进程一个：会话槽位就存在它里面的 WealthAgent 上（D-4）。"""

       def __init__(self, client: BankClient, *, clock: Clock | None = None) -> None:
           self._agent = build_wealth_agent(
               UnconfiguredModelClient(), HttpWealthBank(client), clock or SystemClock()
           )
           # (user_id, session_id) -> 上一轮 WAITING_INPUT 的 pending_id。clock 可注入只是
           # 为了让测试驱动待办过期，不必真等五分钟。
           self._pending_ids: dict[tuple[str, str], str] = {}

       def handle(self, context, model, message) -> TurnResult:
           self._agent.set_model(model)
           key = (context.user_id, context.session_id)
           pending_id = self._pending_ids.get(key)
           if pending_id is None:
               response = self._agent.handle(
                   context, WealthRequest(kind=WealthRequestKind.MESSAGE, text=message)
               )
           else:
               response = self._agent.handle(
                   context,
                   WealthRequest(kind=WealthRequestKind.ANSWER, text=message, pending_id=pending_id),
               )
               if response.status == WealthStatus.FAILED \
                       and response.error_code == ErrorCode.INVALID_STATE:
                   # 待办失效（过期 / 换人 / 换意图）：这句其实不是答案，按新话题重发。
                   # ANSWER 在模型调用之前就返回了，重发不花钱。
                   response = self._agent.handle(
                       context, WealthRequest(kind=WealthRequestKind.MESSAGE, text=message)
                   )
           self._remember_pending(key, response)
           return self._to_result(context, response)

       def _remember_pending(self, key: tuple[str, str], response: WealthResponse) -> None:
           # 写预览（waiting_confirmation / waiting_verification）会被 _to_result 撤下，
           # 它的 pending 也不能记。
           pending = response.pending
           if response.status == WealthStatus.WAITING_INPUT and pending is not None:
               self._pending_ids[key] = pending.pending_id
           else:
               self._pending_ids.pop(key, None)

   def build_turn(client: BankClient) -> TurnHandler:
       return WealthTurn(client).handle
   ```

3. `_to_result(context, response)`：

   ```python
   if response.status in (WealthStatus.WAITING_CONFIRMATION, WealthStatus.WAITING_VERIFICATION):
       # 写路径没接（D-2）。先把模块里那个等着确认的 pending 撤掉，免得它过期、
       # 或者用户下一句「确认」被当成别的东西。
       self._agent.handle(context, WealthRequest(kind=WealthRequestKind.CANCEL))
       return TurnResult(status=TurnStatus.UNSUPPORTED, summary=UNSUPPORTED_WRITE_SUMMARY)
   status = _STATUS.get(response.status, TurnStatus.FAILED)
   summary = response.summary or (response.pending.prompt if response.pending else "") or NO_SUMMARY
   return TurnResult(status=status, summary=summary,
                     facts=dict(response.facts), business_ids=dict(response.business_ids))
   ```

   关键点：
   - **理财的 `WealthTurn` 只在「上一轮是 `WAITING_INPUT`」时向自己的模块发 `ANSWER`（带 `pending_id`），其余一律发 `MESSAGE`；基座只知道把用户的原话交给这个领域，不区分 `ANSWER` 与 `MESSAGE`。** 只用 `MESSAGE` + `RESUME_NOTICE` 不够：那条提示只说「有一个问题没回答」（`backend/app/modules/wealth/llm/prompts.py:112-116`），模型得靠自然语言猜用户这句是不是答案——整句能懂，裸账户号（`a_wealth_main`）不懂，于是重新追问，用户永远学不到结论。`WealthTurn` 按 `(user_id, session_id)` 记住上一轮 `pending.pending_id`，下一轮发 `WealthRequestKind.ANSWER`；`_task_text`（`backend/app/modules/wealth/workflow/nodes.py:1281-1288`）随即把「（用户回答了你上一次的提问）问题：…／用户回答：…」渲染进提示词。
   - **失效回退是免费的。** `ANSWER` 在模型调用之前就返回（`backend/app/modules/wealth/application/runtime.py:400-403`），`_valid_pending` 返回 None 时（不是本人/本会话、`pending_id` 不匹配、超过 `PENDING_TTL = 5 分钟`、`intent_version` 变了）得到 `FAILED + INVALID_STATE`，此时用**同一条消息**重发 `MESSAGE`，用户的新话题不会被「没有可以回答的待办」吞掉。D-1 仍然成立：槽位与待办都在理财自己的会话里，`pending_id` 也记在理财自己的 `WealthTurn._pending_ids`（`backend/app/modules/wealth/application/turn.py:61`，进程内存，见 §12）；基座新增的只有 `AgentState.awaiting_agent`。
   - **刚问过「要哪一个」的资源要进 `shown`。** `ask_user` 只写 `pending_question` + `missing_slots`（`backend/app/modules/wealth/workflow/nodes.py:803-813`），而 `shown` 此前只由 `finish_node` 写（`:831-832`）；槽位校验 `if value not in known.get(resource, frozenset()) and value not in (shown.get(resource) or ()): raise SlotError(f"{name} 只能来自已经查到的记录")`（`backend/app/modules/wealth/workflow/state.py:242-249`）里的 `known` 只来自**本轮** facts，于是用户照着上一轮列出的账户编号回答时会被判成没查到过。`_commit`（`backend/app/modules/wealth/application/runtime.py:514-537`）在写 `shown` 之后补一次 `_asked_candidates(result)`：从 `missing_slots` 反查 `SLOT_RESOURCES`，按 `facts[resource]` 的**行顺序**取 `RESOURCE_ID_FIELDS[resource]` 字段去重（`resource_ids()` 返回 frozenset，拿不到顺序）。放在 `_commit` 而不是 `ask_user_node`：`ask_user_node`、`_ask_which_holding`、问卷、推荐、申购预览五条路径都写 `missing_slots`，而 `_commit` 本来就拥有 `session.shown` 的持久化。
   - `WAITING_CONFIRMATION` / `WAITING_VERIFICATION` 一律转 `UNSUPPORTED` 并撤回 pending：不发写请求、不留一个会过期的半截状态。
   - `summary` 在任何状态下都是可以直接给用户看的话：`COMPLETED` 是 `answer`（`:1354-1360`），`WAITING_INPUT` 是那句追问（`:1325-1337`），`FAILED` 是失败说明（`:1245-1254`）。
   - `facts` / `business_ids` 原样带上（`:1354-1360` 已经填好）。

**改哪里会变红：** `backend/app/modules/wealth/application/runtime.py`（新增 `set_model`）；新增 `backend/app/modules/wealth/application/turn.py`；新增 `backend/tests/wealth/test_base_turn.py`。

## 9. S-7 生产注册表回到空目录

**目标行为。**

1. `backend/app/modules/wealth/__init__.py` 改成：

   ```python
   MODULE = ModuleSpec(name="wealth", declaration=WEALTH, turn=build_turn)
   ```

   删掉 `tools=` 与 `handlers=`（D-3）。docstring 重写：工具不再声明在模块上，它们在理财 agent 的整轮内部被调用；底座因此不再替理财做单次工具调用。

2. `backend/tests/test_module_registry.py:226-244` 的 `test_the_production_catalog_is_the_wealth_read_tools_and_nothing_else` 必须重写（D-3 直接推翻它的断言）。新的断言：
   - `registry.catalog.names() == ()`；
   - `registry.handlers == {}`；
   - `[module.name for module in ALL_MODULES if module.tools] == []`；
   - `[module.name for module in ALL_MODULES if module.turn is not None] == ["wealth"]`。

3. `backend/tests/test_interrupt_reset_integration.py:63-69` 的 docstring 现为 "The production gateway grants `bill` no tools (only `wealth` declares any)"，改成「生产注册表一个工具都没有（理财走整轮，不走工具）」这层意思。

4. **不影响**的测试：`backend/tests/wealth/test_read_flow.py:532-536` 与 `backend/tests/wealth/test_common_layer_wiring.py:199` 都是自建本地 registry（`READ_TOOLS` / 本地 `PROBE`），与生产 `MODULE` 解耦。

**改哪里会变红：** `backend/app/modules/wealth/__init__.py`；`backend/tests/test_module_registry.py:226-244`；`backend/tests/test_interrupt_reset_integration.py:63-69`。

## 10. 组合根：整轮处理器只造一次

**目标行为。**

1. 新增 `build_turns(registry, client) -> dict[str, TurnHandler]`（`backend/app/runtime.py`）：

   ```python
   def build_turns(registry: Registry, client: BankClient) -> dict[str, TurnHandler]:
       """把模块声明的整轮处理器造出来，一个模块一个，进程生命周期内只造一次。

       必须只造一次：理财 agent 的会话状态（上一次问了什么、槽位攒到哪）就在它自己里面，
       每轮新建一个等于每轮失忆。
       """
       return {module.name: module.turn(client) for module in registry.modules if module.turn is not None}
   ```

2. `agent_graph_factory(gateway, turn_handlers=None)`（`backend/app/runtime.py:50-66`）把 `turn_handlers` 透传给 `build_agent_graph`。

3. `Runtime.__init__`（`:72-102`）：`self.gateway = build_tool_gateway(registry=registry, ...)` 与 `self.turn_handlers = build_turns(registry, self.bank_client)` 共用同一个 `build_registry()` 结果（现在 `build_tool_gateway` 内部自己调了一次，别让它调两次）。**容器不能叫 `self.turns`**——`:102` 的 `ConversationTurns` 已经占了那个名字，覆盖它会把整个会话服务打掉。构造期没有 I/O：`HttpWealthBank(client)` 只是存引用，`build_wealth_agent` 只做类型检查。

**改哪里会变红：** `backend/app/runtime.py:36-66`、`:72-102`；新增 `backend/tests/test_turn_wiring.py`（断言同一个 handler 实例跨轮复用）。

## 11. 明确不做

- 不改理财模块内部的工作流、提示词、槽位、门禁、推荐（`backend/app/modules/wealth/workflow/`、`llm/`）。这一版是接线，不是改业务。
- 不给理财会话加落库（D-4）。
- 不接申购/赎回/验证码（D-2）。
- 不让底座替理财发 `CONFIRM` / `VERIFY`。`ANSWER` 是个例外（见 §8 关键点 1）：上一轮是 `WAITING_INPUT` 时 `WealthTurn` 发 `ANSWER` + `pending_id`，失效则回退 `MESSAGE`；`CONFIRM` / `VERIFY` 永远不发。
- 不给整轮模块做流式中间事件（见 §12）。
- 不删 `_run_tool` / `_call_tool` / `_with_tools_available` 这条工具通道：它仍是模块声明工具的通用路径，只是生产上暂时没有模块走它。

## 12. 已知后果（诚实清单）

1. **底座的运行账本如实记录理财的每一次银行调用。**（本条按 2026-10-13 提交 `03fd141` 改写；该提交把整轮改为领域图 + 原生工具。）现在前端能看到 `tool_call wealth.holdings`，**之后仍然能看到**：只读工具经基座网关以原生 `tool_call`／`tool_result` 帧回传，账本记的就是这些调用；整轮的骨架是 `agent_start(wealth)` → 若干 `tool_call`／`tool_result` → `subtask_end` → `final`。以 interrupt（提问卡）结束的追问轮**也会**发出 `subtask_end`：它的 `turn_status` 是 `waiting_input`、摘要就是那句追问（事件层的 `status` 仍是冻结词表里的 `SUCCEEDED`）；承载查询结果的那条 `turn_status=completed` 子任务行属于用户答完追问后的完成轮。理财自己的审计仍在模块内（`WealthAgent._audit`）。
2. **理财的模型调用仍然进底座的 `llm_call_audit`**：整轮用的 model 就是底座运行期注入的那个（`AgentRuntime.model`，配置面板换过模型之后是那个新模型），审计包装在它外面。
3. **前端「补充信息卡片」不再出现**（D-1 的代价）：理财的追问会作为一条普通助手消息出现，用户的回答也作为一条普通消息发出去。
4. **理财会话是进程内存**（D-4）：后端重启后，上一句追问过的槽位丢失，但余额/持仓/订单不受影响。
5. **主 agent 的模型现在会转述理财的结论**，措辞每次可能不同；明细来自 `facts`，不是模型编的（提示词里禁止增删事实）。

## 13. 实施顺序建议

1. S-1（`app/agents/turn.py` + `ModuleSpec.turn` + `DomainSpec.turn` + 重导出）——纯新增，跑 `backend/tests/test_module_registry.py`、`test_import_cycle.py`。
2. S-2 + S-4（supervisor 分流 + `awaiting_agent`）——先写 `backend/tests/test_domain_turn.py` 的失败用例（假 handler），再实现。
3. S-3 + S-5（提示词 + `_already_concluded` + `_closing_reply`）——同一批，改 `backend/tests/test_domain_agents.py:336`、`:360`。
4. S-6（`set_model` + `application/turn.py`）——写 `backend/tests/wealth/test_base_turn.py`。
5. S-7 + 组合根（`wealth/__init__.py` + `build_turns` + `Runtime`）——最后接线，跑注册表测试与全量理财测试。
6. 验收（§14）。

## 14. 验收

**pytest（接缝）**

- `backend/tests/test_domain_turn.py`（新）：
  - 整轮模块被委派时，handler 被调用一次，参数是 `(TrustedContext, ModelClient, 用户原话)`；
  - 领域的 `summary` 与 `facts` 出现在主 agent 收到的提示词里（`facts` 里的产品名可在提示词原文里找到）；
  - `COMPLETED` 之后主 agent 只委派一次就收尾（断言 handler 调用次数 == 1，且最终回复来自主 agent 的模型）；
  - handler 抛异常 → 事件流仍以一条给用户看的失败说明收尾，图不炸；
  - 主 agent 的模型不可用 → 回复是领域的 `summary` 原文，不是「抱歉…」；
  - 第一轮 `WAITING_INPUT`、第二轮同一领域仍被委派（`awaiting_agent`）；
  - 整轮结束的 `subtask_end.status` 落在既有的 `SUCCEEDED` / `FAILED` / `DENIED` 值域里，不是 `TurnStatus.value`。
- `backend/tests/test_turn_contract.py`（新）：`TurnResult` 的字段校验；`TERMINAL_TURN_STATUSES` 是**字符串**的 frozenset。
- `backend/tests/test_turn_wiring.py`（新）：同一个 handler 实例跨轮复用（`build_turns` 只造一次）；组合根两跳各有钉子——`Runtime.__init__` 必须把同一个 handler 字典交给 `agent_graph_factory`（不传就红），且 `build_registry()` 只被调用一次（第二个注册表＝第二个理财 agent，下一轮 pending 查不到）。
- `backend/tests/test_module_registry.py`：`turn` + `tools` 同声明被拒；无 `declaration` 的 `turn` 被拒；生产目录为空且只有 wealth 有 `turn`。
- `backend/tests/wealth/test_base_turn.py`（新）：`WAITING_CONFIRMATION` → `UNSUPPORTED` 且 pending 被撤回（第二次 `MESSAGE` 不再看到那个 pending）；`COMPLETED` → `facts` 原样带出；两次 `handle` 用同一个 `TrustedContext` 时槽位延续。
- `backend/tests/wealth/` 全绿（`backend/tests/wealth/conftest.py:36-37` 的 `BankDouble` 可直接复用）。

**真实端到端（D-6 第二条）**

基座 8000 + `bank_backend` 8001 + 前端 5173，演示用户已改绑 `u_wealth`（`backend/app/storage/seed.py` 的 `DEMO_BANK_CUSTOMER`，2026-10-11 单独提交，见 §0.3；不带它跑就是银行的默认客户 `u_zhang`），重跑截图里的三问：

1. 「你好，我要理财」→ 补「资金规模：20万；投资期限：2年；流动性要求：随时赎回」→ 应给出**具体产品明细**（名称、期限、预期收益），而不是「共查到 N 只」；
2. 「有什么产品？」→ 应列出产品明细；
3. 「产品是什么？」→ 应列出产品明细，且不再出现「抱歉，这个请求暂时没能完成：」；
4. 追问一句「买 2000 元的 p_demo_cash」→ 理财会先问「从哪个账户扣款」（这是模块自己的申购流程，不是写路径）；回一句账户编号后 → 应如实回答写路径尚未接入，且 `bank_db` 里**没有**新增订单（实测 `bank_db.orders` 前后都是 4 行、最近 6 小时 0 行）。这一条第一次跑时暴露出「2000 元被用成会话里的 20 万计划金额」，见上一条修订。

**回归**

- `backend/tests/test_domain_agents.py`、`test_cross_domain_orchestration.py`、`test_error_codes.py`、`test_envelope_projection.py`、`test_business_ids.py`、`test_write_safety.py`、`test_interrupt_reset_integration.py` 除 3 条**基线预存在**失败外全绿（实测 3 failed, 119 passed；3 条都在 `backend/tests/test_interrupt_reset_integration.py` 的 `:218`、`:270`、`:295`；在分支基线 `f5cd7a0` 上跑同一文件同样是这 3 条，2 passed）。**不是本分支引入，本分支也不修**：`_with_tools_available` 把空工具菜单领域的结论换成 `NO_TOOLS_SUMMARY`，而该测试的 `pausable_gateway()` 只给 `bill` 授 echo、脚本却先路由 `transfer`（空菜单）。
