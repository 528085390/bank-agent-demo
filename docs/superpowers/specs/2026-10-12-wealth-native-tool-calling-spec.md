# Spec 2 — 理财改造成与 card 对齐的领域图（B2）

- 状态：**待执行**（§19 前三条已裁定、第 4 条按用户 m02008 裁定为「补自由文本字段」；§2.1 的形态按用户 2026-10-12 二次裁定改为 **B2**）
- 日期：2026-10-12
- 基线：`D:\ai_project\agent` @ `bc70879`（dev）
- 上位文档：`docs/superpowers/plans/2026-10-12-unify-tool-calling-plan.md`
- 兄弟 spec：`docs/superpowers/specs/2026-10-12-abolish-json-text-protocol-spec.md`（spec1，基座）、`docs/superpowers/specs/2026-10-12-wealth-write-verification-spec.md`（spec3，写流程验收）

## 0. 结论

理财从「`turn=` 整轮模块」改成「`graph_builder` 领域图模块」，与 card 同形：声明 7 个只读工具给基座
（`tools={name: tool.spec for name, tool in READ_TOOLS.items()}, handlers=public_handlers()` —— **不能直接写 `tools=READ_TOOLS`**，值是 `ReadTool` 不是 `ToolSpec`，见 §8）、自己的图里做决策与写流程、状态放进图 state 由基座
checkpointer 持久化。决策协议同时从「渲染工具表 + 读 JSON 文本」换成原生 tool calling。
`WealthTurn` / `build_turn` 删除，`build_wealth_agent` 保留（CLI 与测试仍用）。

模块自己的门禁（动作词表、参数 schema、越门禁计数、引用有据、预算）与写逻辑（预览、幂等、对账）**一条都不减**，
只换住处。三处用户可见的行为变化（§2.7、§15）：追问变成检查点中断（前端必须发 resume 信封）、答案变成
`{字段: 值}` 结构、读路径新增 `tool_call`/`tool_result` 事件。

## 1. 需求

| # | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 形态与 card 对齐：无 `turn=`，声明 `tools`/`handlers` + `graph_builder` | `ModuleSpec(name="wealth", declaration=WEALTH, tools={name: tool.spec for name, tool in READ_TOOLS.items()}, handlers=public_handlers())` —— **不能写 `tools=READ_TOOLS`**（值是 `ReadTool` 不是 `ToolSpec`，import 期 `AttributeError`，见 §8）；`rg -n "turn=build_turn" backend/app` 为空 |
| R-2 | 跨轮状态进图 state，重启不失忆 | 换掉 `rt.agent`（重建图与 checkpointer 之外的运行时）后，待办与槽位仍在（照 `test_pending_action_api.py:123` 的写法） |
| R-3 | 理财决策只走 `stream(tools=...)` | 仓库根 `rg -n "json.dumps|extract_json_object" backend/app/modules/wealth` 只剩非决策用途（落点：理财计划 Task 2 Step 11 与 Task 4 Step 10 的 grep 行） |
| R-4 | 工具表是唯一词表来源 | 送出去的 schema 与 `_read_decision` 校验用的是同一个 `ToolEntry.parameters` |
| R-5 | 门禁不减 | `GateViolation` 的三种越门禁尝试（动作不在词表／工具不在白名单／业务输入里塞授权字段）仍有对应用例；`_read_decision` 的三段校验一行不改 |
| R-6 | 提示词里不再有工具表文本 | `read_messages` 不再收 `tools` 参数；`{tools}` 槽位删除 |
| R-7 | 写流程仍然端到端可用 | 预览 → 确认卡 → 验证码 → 幂等提交 → 对账；spec3 的端到端用例全绿 |
| R-8 | 验证码不进图 state | 仓库根 `rg -n "verification_code" backend/app/modules/wealth/workflow` 为空（落点：理财计划 Task 4 Step 10）；它只经 `graph_builder` 的闭包进入节点 |
| R-9 | 不新增资金写工具 | `tool_table()` 里仍然没有 purchase/redeem 执行工具（`test_redeem_income.py:592` 的既有断言保持） |
| R-10 | 隔离键与图 state 对齐 | `scope_id`（conversation_id 或 session_id）与图 state 的 `thread_id` 一致；两个会话的槽位与待办不串（落点：理财计划 Task 4 Step 1 的 `test_wealth_graph.py` thread_id 用例） |

## 2. 既有事实（改造前）

### 2.1 形态裁定：wealth 改成 B2（与 card 对齐）

用户 2026-10-12 二次裁定（m01371）：「还是做成 b2 吧，和 card 对齐」。

| | card（今天） | wealth（今天） | wealth（B2 后） |
| --- | --- | --- | --- |
| 声明 | `tools=TOOLS, handlers=HANDLERS` + `graph_builder=build_card_graph`（`backend/app/modules/card/__init__.py:25-28`） | `turn=build_turn`（`backend/app/modules/wealth/__init__.py:28`） | 同 card：`tools=READ_TOOLS, handlers=public_handlers()` + `graph_builder=build_wealth_graph` |
| 决策 | 自己图里的 `decide` 节点，仍是 JSON 文本（`backend/app/modules/card/workflow.py:184-199`） | `WealthTurn` → `WealthAgent` → 自己的读图，JSON 文本 | 自己图里的 `decide` 节点，原生 tool calling |
| 跨轮状态 | 图 state（`CardState`，`backend/app/modules/card/workflow.py:29-32`），基座 checkpointer 持久化 | Python 对象（`WealthTurn._pending_ids` + `WealthAgent._sessions`），每进程一次 | 图 state，基座 checkpointer 持久化 |
| 追问 | 检查点中断 | **没有中断**（§2.7），靠 `_pending_ids` 记，下一条消息当答案 | 检查点中断（`wait_for_user`） |
| 确认 | 检查点中断 + 自己图里的 `process` 节点 | 检查点中断（`_confirmation_pause`）+ `WealthTurn.act` | 检查点中断 + 自己图里的 `process` 节点 |

**为什么不是 B1**：B1 保留 `turn=` 也能把协议换成 tool calling，但 wealth 与 card 仍是两种形态，
基座的等待通道与工具事件对 wealth 永远不可见，跨轮状态仍然在进程内存里、重启即丢。用户要的是形态对齐。

**B2 的收益**：重启不失忆；理财的工具调用对基座可见（`tool_call`/`tool_result` 事件）；形态与 card 一致。
**B2 的代价**：§2.5（状态搬家）、§2.6（基座接缝缺口）、§2.7（追问通道与前端）。

### 2.2 决策链现状

```
wealth/application/turn.py:343 build_turn(client) -> WealthTurn
  → turn.py:112-149 handle() / :151-188 act()
  → runtime.py:366 WealthAgent.handle()      （持有 _Session、pending、幂等、对账）
  → workflow/graph.py:102 build_read_graph(model, bank, context, tools=READ_TOOLS)
  → workflow/graph.py:116 graph.add_node("decide", decide_node(model, tools=tools))
  → workflow/nodes.py:171 decide_node(model, *, tools)
      :182 table = tool_table(tools)                    ← 词表与 schema 的唯一来源
      :183 menu = tool_table_text(table)                ← 渲染成文字（本 spec 要删）
      :205 messages = read_messages(..., tools=menu)    ← 摆进提示词 {tools} 槽位
      :949 _read_decision(reply, table=, tools=, state=)
          :967 decision = parse_decision(reply, table=table)   ← json.loads
      :1438 _model_text(model, messages)
          :1441 call_with_one_retry(lambda: model.complete(messages))   ← 无 tools
```

```python
# backend/app/modules/wealth/llm/decisions.py:167
def parse_decision(text: str | None, *, table: Mapping[str, ToolEntry]) -> WealthDecision:
```

### 2.3 工具表的真实内容

`wealth/llm/toolcalls.py`：

- `:42 CALL_SHAPE = '{"tool":"<工具名>","arguments":{...}}'`
- `:44 TOOL_CALL_NOTICE = "请只输出一个 JSON 对象，形如 " + CALL_SHAPE + "，不要输出其他文字。"`
- `:47-73 ToolEntry(name, description, parameters={}, side_effect=SideEffect.READ, output="")`，属性 `required`/`properties`/`additional_properties`
- `:76-82 _parameters(properties, required=())` → `{type:object, properties, required, additionalProperties:False}`
- `:89-238 CONTROL_TOOLS` 8 项：`set_slots`、`ask_user`、`submit_risk`(WRITE)、`recommend`、`compare`、`prepare_purchase`(WRITE)、`prepare_redeem`(WRITE)、`finish`
- `:243 tool_table(tools=READ_TOOLS) -> dict[str, ToolEntry]`：只读 7 项复用 `tool.spec` 的 name/description/parameters/side_effect，再 update 控制类
- `:281 tool_table_text(entries)`：渲染成 `### {name}\n说明：…\n参数 schema：{json.dumps(...)}\n输出：…\n副作用：写|只读`
- `__all__` 在 `:32`/`:34`/`:38` 声明了 `CALL_SHAPE`/`TOOL_CALL_NOTICE`/`tool_table_text`

**strict 可表达性问题**：`_parameters` 里用了 `minLength`/`minItems`/`maxItems`/`minProperties`/`pattern`/`enum`。
其中 `minLength`/`minItems`/`maxItems`/`minProperties` **不在 OpenAI strict 子集内**，直接送出去可能被 provider 拒绝。
而 spec1 已删除 strict 降级，所以必须在送出去之前处理掉（见 W-1）。

### 2.4 测试面现状

`backend/tests/wealth/support.py:455-545` 有 **10 个**把决策**造成为 JSON 字符串**的助手（旧稿写 9 个）：
`call`(:461)、`call_read`(:455)、`set_slots`(:467)、`ask_user`(:476)、`finish`(:487)、`submit_risk`(:506)、
`recommend`(:515)、`compare`(:521)、`prepare_purchase`(:527)、`prepare_redeem`(:537)。
`call`(:464) 的实现是 `json.dumps({"tool": tool, "arguments": ...})`。

`parse_decision(reply, table=...)` 的测试调用点：
`test_read_flow.py:675`、`test_risk_recommend.py:681`、`:693`、`:708`。

`tool_table(...)` 的测试调用点：`test_tool_selection.py:356`、`test_redeem_income.py:580`/`:585`/`:592`、
`test_dialogue.py:75`、`test_safety.py:737`、`test_read_flow.py:621`、`test_purchase.py:60`、
`test_confirmation_events.py:325`、`test_risk_recommend.py:630`。

`read_overhead` 的测试调用点：`test_context_handoff.py:39`(import)、`:195`。

### 2.5 状态住处：card 与 wealth 的差别就是「搬什么」

| 状态 | card 的住处 | wealth 今天的住处 | B2 后的住处 |
| --- | --- | --- | --- |
| 槽位 `slots` / `shown` / `clarifications` / `intent_version` / `last_holding` | 图 state | `_Session`（`wealth/application/runtime.py:337`，表在 `:383 self._sessions`） | 图 state |
| 待回答的追问 | `pending_question`（图 state） | `WealthTurn._pending_ids`（`application/turn.py:100`）+ `_Session.pending_id` | 基座 `pending_question`（图 state） |
| 写预览与幂等键 | 无（card 只有 `card_action_id`） | `_WriteDraft`(:241) / `_WriteIntent`(:255) / `_Pending`(:298)，表在 `:384 self._pending` | 图 state（纯数据） |
| 对账引用 | 无 | `_Unresolved`(:318) | 图 state（纯数据） |
| 跨线程安全计数 | 无 | `_locked_users` / `_technical_failures` / `_bypass_events`（`:385-392`） | **留在进程内**（不是会话状态） |

**硬约束**：搬进图 state 的必须是可序列化纯数据。生产 checkpointer 是 Postgres（`backend/app/main.py:45-46 postgres_checkpointer`），图 state 每节点落盘。所以 `_Session` / `_Pending` / `_WriteIntent` / `_Unresolved` 要以 dict/TypedDict 形态进 state，`Clock` 与 `WealthBank` 不进 state（它们从图构建闭包拿）。

**隔离键**：`scope_id(context, conversation)`（`wealth/application/runtime.py:1797`）今天是 `_sessions` / `_pending_ids` 的键；B2 后它必须与图 state 的 `thread_id` 对齐（`backend/app/agents/runtime.py:86-108 TurnContext.thread_id`），否则两个会话会串。**注意 `thread_id` 是带前缀的**：`thread_id_for_session`（`app/agents/runtime.py:57-60`）返回 `f"{THREAD_PREFIX}{session_id}"`，`THREAD_PREFIX = "chat:"`（`:34`）；conversation 侧是 `conv:`（`app/conversation/service.py:61`/`:70-73`）。要么直接用 `thread_id` 当唯一键、要么自己拼前缀，**不要**拿裸 `session_id` 当键。

### 2.6 基座接缝的四个缺口（W-0，见 §7）

| # | 缺口 | 现状 | 影响 |
| --- | --- | --- | --- |
| 1 | `graph_builder` 只拿到 `(model, spec)` | `backend/app/agents/supervisor.py:2063-2064` | 拿不到 `turn`（验证码）与 `assembler`（会话历史/线索） |
| 2 | handoff 只有 `turn=` 模块能进 | `conversation_scope` 唯一进入点 `backend/app/agents/supervisor.py:607`（在 `_run_module` 内） | 理财丢 `_history_text` / `_clue_slots` / `_clue_shown` 与结构化记忆回写 |
| 3 | 工具门只在通用路径 | `_with_tools_available` 定义在 `backend/app/agents/supervisor.py:1173`，唯一调用点 `:1328` | 无影响（理财 7 个只读工具非空） |
| 4 | 等待/回答通道只服务通用路径与 `turn=` | `wait_for_user`(:805) / `process_answer`(:846) / `_answer_confirmation`(:926) | 理财的图自己实现 decide/wait/process，`wait` 直接用 `wait_for_user` |

**验证码为什么必须从闭包进**：`TurnContext.verification_code`（`backend/app/agents/runtime.py:86-108`）的 docstring（`:94-97`）明确它故意不进 `AgentState`——图状态每节点 checkpoint，放进去就持久化了；`backend/app/modules/wealth/README.md:50` 也写「验证码不进检查点」。所以它只能经 `graph_builder(model, spec, turn=...)` 的闭包进入 `process` 节点。

**不要用「包一层函数再 `subgraph.invoke()`」绕过**：会破坏 LangGraph 子图的中断/流式/检查点语义，card 的确认卡依赖它（`backend/tests/test_pending_action_api.py`）。

### 2.7 追问通道：`turn=` 模块今天没有检查点中断

`backend/app/agents/supervisor.py:667-721 _conclude_turn` 对 `TurnStatus.WAITING_INPUT` **只写 `awaiting_agent`，走 `idle_work_state()`，不写 `pending_question` / `turn_phase`**（只有 `WAITING_CONFIRMATION` 才走 `_confirmation_pause` 写真正的 checkpoint interrupt，`:724-756`）。所以今天理财的「追问」靠 `WealthTurn._pending_ids` 记住，下一条聊天消息被当成答案。真机四轮实证吻合：第 1/3 轮（`waiting_input`）没有 `event: interrupt`，只有第 4 轮（`waiting_confirmation`）有。

**B2 后**：追问走基座的 `_awaiting`（`:1022-1081`）+ `wait_for_user` → 真中断。三处连带（**第 1 条是核对，不是改动**）：

1. **前端：通道已经存在，本 spec 不含任何前端代码改动**（逐行核对见 §10.1）。`frontend/src/api.ts:24-25 resumeFor(question, value)` 是唯一的 resume 构造器；`frontend/src/App.vue:592` 的**通用输入卡片**已经在用它发送答案（发送「补充信息：…」时把 `resumeFor(card, value)` 作为客户端上下文），`:957-970` 按 `field.type` 渲染 `select`/`number`/`date`/`text`，`:642 interruptComplete(card)` 卡住必填项，`:108`/`:430` 在 `pending` 存在时禁止再发普通消息。**card 模块的 `ask_user` 今天就跑在这条通道上**，理财追问变成中断后走的是同一条路。若前端不发 resume，`backend/app/agents/runtime.py:303-365 admission(...)` 会在有 open interrupt 时抛 409 `INVALID_INTERRUPT_ANSWER`（`resume_value` 在 `:63-74`：只有 `resume` 是 Mapping 且含 `"value"` 键才算）——这正是既有通道已经处理掉的场景。
2. **答案形状**：`backend/app/agents/supervisor.py:1134 _read_answer` 要求 `set(value.keys()) == {field["name"] for field in question["fields"]}`，且必填字段非空——所以答案不是自由文本，而是与问题字段一一对应的 `{字段: 值}`。
3. **理财槽位声明**：`_awaiting` 用 `resolve_fields(spec.declaration, names)`（`:1049-1051`），所以 `WEALTH` 必须声明 `fields=`（可追问槽位的 `FieldMeta`），否则问题渲染不出字段。

## 3. W-1 `llm/toolcalls.py`：工具表变成 JSON Schema 来源

删：

- `:42 CALL_SHAPE`、`:44 TOOL_CALL_NOTICE`、`:281 tool_table_text` 与 `__all__` 里对应的 `:32`/`:34`/`:38`。

加：

```python
# 这些关键字不在 OpenAI strict 子集里：留在 Python 侧的校验器里（parse_decision 用
# table[name].parameters 校验），但从送出去的 schema 里拿掉，并把约束写进 description，
# 模型仍然知道规则，provider 不会因为看不懂关键字而拒绝整张表。
_NON_STRICT_KEYWORDS = ("minLength", "maxLength", "minItems", "maxItems",
                        "minProperties", "maxProperties", "pattern", "format")


def _drop_keywords(schema: dict[str, Any], keywords: Sequence[str]) -> list[str]:
    """就地删掉命中的关键字，返回可读行（例如 "product_ids 至少 2 项"）；返回 [] 表示没删。

    只扫**顶层** properties，与 normalize_parameters 同层级；嵌套对象/数组项里的同名
    关键字不动（今天 CONTROL_TOOLS 的 parameters 没有嵌套对象）。
    """
    ...


def _wire_parameters(entry: ToolEntry, *, strict: bool) -> tuple[dict[str, Any], str, bool]:
    """→ (送出去的 schema, 补救后的 description, 能不能声明 strict)"""
    schema, expressible = normalize_parameters(dict(entry.parameters))
    description = entry.description
    if strict:
        dropped = _drop_keywords(schema, _NON_STRICT_KEYWORDS)
        if dropped:
            description = description + "\n" + "；".join(dropped)
    return schema, description, bool(strict and expressible)


def function_schema(entry: ToolEntry, *, strict: bool = True) -> dict[str, Any]:
    """One tool, in the shape `ModelClient.stream(tools=...)` takes."""
    parameters, description, can_be_strict = _wire_parameters(entry, strict=strict)
    return {
        "type": "function",
        "function": {
            "name": entry.name,
            "description": description,
            "parameters": parameters,
            "strict": can_be_strict,
        },
    }


def function_schemas(table: Mapping[str, ToolEntry], *, strict: bool = True) -> tuple[dict[str, Any], ...]:
    return tuple(function_schema(entry, strict=strict) for entry in table.values())
```

- `normalize_parameters` 来自 `app.tools.schema`（spec1 §2.2 提到的 `app/tools/schema.py`，签名 `(parameters) -> (schema, can_be_strict)`）：把不在 `required` 里的属性变成 `[X, "null"]` 并补进 `required`，这是 strict 的硬要求。
- `_drop_keywords(schema, keywords)` 返回"被拿掉了哪些约束"的可读行（例如 `"product_ids 至少 2 项"`），拼进**返回的** `description`（不是 `entry.description`）；**只处理顶层 `properties`**，与 `normalize_parameters` 的层级一致。命名统一为 `_NON_STRICT_KEYWORDS` / `_drop_keywords`（旧稿两处名字不一致）。
- **`entry.parameters` 本身不改**：`_read_decision` 仍用它做 `Draft202012Validator` 校验，所以 W-1 不放松任何一条既有约束。
- 保留 `ToolEntry`、`tool_table`、`read_arguments`、`_parameters`、`_CONTROL_BY_NAME`、`CONTROL_TOOLS`。

## 4. W-2 `llm/decisions.py`：解析器读 `ToolCall`

```python
def parse_decision(
    text: str | None, tool_calls: Sequence[ToolCall], *, table: Mapping[str, ToolEntry]
) -> WealthDecision:
```

新规则（顺序即优先级）：

| 输入 | 结果 |
| --- | --- |
| `len(tool_calls) > 1` | `DecisionError("一次只能调用一个工具，不能一次给多个")` |
| 恰好 1 个，名字在 `table` 里 | `_to_decision(call.name, call.arguments)`（既有映射全部保留） |
| 恰好 1 个，名字不在 `table` 里 | `GateViolation("工具不在词表里：{name}")`（越门禁尝试） |
| 0 个，`text.strip()` 非空 | `WealthDecision(action=ACTION_BY_TOOL["finish"], answer=text.strip())` |
| 0 个，文本为空 | `DecisionError("模型没有返回内容")` |

**行为变化（有意为之）**：今天"只有散文、没有 JSON"是一次格式错误，纠正一次后仍不行就以失败收尾；
改后它等价于一次不带 `cited_ids`/`shown` 的 `finish`。这与 spec §4.6「答案是流式文本」一致，
风险与今天模型主动调 `finish` 时相同（`_ungrounded_shown` 只在 `shown` 非空时生效）。

删：`:39` 与 `:170` 的 `extract_json_object` 导入与调用、`:78 _CALL_KEYS`、`:167-174` 的顶层多余键检查
（`"顶层只允许 tool 与 arguments"` 这条 GateViolation 随之消失——协议层已经不存在"顶层"）。

保留不动：`WealthDecision`(`:100`)、`GateViolation`(`:83`)、`_to_decision`(`:237`)、
`_undeclared_in_closed_objects`(`:209`)、`_schema_problem`(`:301`)、`ungrounded_citations`(`:275`)、
`ACTION_BY_TOOL`(`:52`)、`ACTIONS`(`:65`)。

加：

```python
def render_call(name: str, arguments: Mapping[str, Any]) -> str:
    """一次失败的工具调用，写成给纠正提示词看的文字（取代原先的"上一条回复原文"）。"""
    return f"{name}({json.dumps(dict(arguments), ensure_ascii=False)})"
```

## 5. W-3 `llm/prompts.py`：提示词不再带工具表

- `:154 def read_messages(..., tools: str, ...)` → 删 `tools` 形参；删 `:66` 的 `{tools}` 槽位与 `:192`/`:221` 的 `tools=tools`。
- `:198 def read_overhead(tools: str, questionnaire: str = "") -> str` → `def read_overhead(questionnaire: str = "") -> str`。
- 模块 docstring `:8`（「工具表由 workflow 层用 toolcalls.tool_table_text 渲染好传进来」）与 `read_messages` docstring `:171` 一并改写为「工具表由 `stream(tools=)` 提供，不经过提示词」。
- **生产侧还有一个调用点（审查 C-7，旧稿漏了）**：`backend/app/modules/wealth/application/turn.py:24` 的 `from ...infrastructure.tools import tool_menu` 与 `:25` 的 `read_overhead`，`:309` 写的是 `view = handoff.view(prompt_overhead=read_overhead(tool_menu(), questionnaire_lines()))` → 改成 `read_overhead(questionnaire_lines())` 并删 `:24` 的 import（否则 F401）。只改测试侧（`test_context_handoff.py:39`/`:195`）会在这一步直接红。
- `:139 correction_notice(reason, previous)` 签名不变，调用方改传 `render_call(...)`（W-4）。

## 6. W-4 `workflow/nodes.py`：模型调用改流式带 tools

`decide_node`(`:171`)：

```python
    table = tool_table(tools)
    schemas = function_schemas(table)          # 取代 menu = tool_table_text(table)
```

`run` 里 `:205` 的 `read_messages(..., tools=menu)` 去掉 `tools=`。

`:1438 _model_text` → 换成：

```python
def _model_decision(
    model: ModelClient, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> tuple[str, list[ToolCall]]:
    """一次模型调用，拿回文字与结构化工具调用。"""
    return collect_stream(model, tuple(dict(m) for m in messages), tuple(tools))
```

- `collect_stream` 来自 spec1 §10 的 `app.agents.toolcalls`（`start_stream` 已内含"只重试一次"的边界），所以 `call_with_one_retry`(`:42` 的 import) 在本文件不再需要。
- 调用点的 `except` 扩成 `except (AppError, ModelClientError, StreamTruncated)` → 既有的 `failure: "model"` 分支（`StreamTruncated` 是 `DecisionError` 子类，不捕会被 `:968` 的 `except DecisionError` 吞成"格式错误"并白纠正一次）。

`:949 _read_decision` 签名与调用：

```python
def _read_decision(
    reply: str,
    tool_calls: Sequence[ToolCall],
    *,
    table: Mapping[str, ToolEntry],
    tools: Mapping[str, ReadTool],
    state: WealthWorkflowState,
) -> tuple[WealthDecision | None, DecisionError | None]:
    ...
    decision = parse_decision(reply, tool_calls, table=table)
```

`:949-1077` 的三段结构（`call_read` 的 `extra_arguments`/`check_arguments`、`set_slots`/`prepare_*` 的 `normalize_slots` 与越门禁计数、
`ask_user` 的 `missing` 规则、`finish` 的 `ungrounded_citations`/`_ungrounded_shown`）**一行不改**——
它们校验的是 `WealthDecision`，与决策怎么来的无关。

纠正提示词的 `previous` 参数：从"上一条回复原文"改成 `render_call(call.name, call.arguments)`（0 个工具调用时用 `reply`）。

## 7. W-0 基座配套（前置；文件落在 spec1 的范围里）

四处接缝扩展，都不改变既有行为（card 忽略新参数），但不做理财接不进来。

### 7.1 `graph_builder` 调用点扩参

`backend/app/agents/supervisor.py:2063-2064`：

```python
if spec.declaration.graph_builder is not None and spec.handle.tools:
    return spec.declaration.graph_builder(model, spec)
```

改成：

```python
if spec.declaration.graph_builder is not None and spec.handle.tools:
    return spec.declaration.graph_builder(
        model, spec, assembler=assembler, turn=turn, client=client
    )
```

- `backend/app/agents/declaration.py:56` 的 `graph_builder: Callable[[Any, Any], Any] | None` 类型注解同步放宽为 `Callable[..., Any] | None`（旧稿写 `:62`，那是 display name 的 ValueError）。
- **`client` 的整条透传链（审查 C-3，旧稿只有形参没有接线）**：基线 `backend/app/runtime.py:122 self.bank_client = client or BankClient.from_settings()`、`:130 self.turn_handlers = build_turns(registry, self.bank_client)`、`:69-74 agent_graph_factory(gateway, turn_handlers=None, *, protocol=...)`、内层 `:86-91 factory(model, context, assembler=None, turn=None)` 都不收银行；`backend/app/agents/supervisor.py:2029 build_domain_graph` 与 `:2063-2064` 调用点也要跟着收。W-0 必须把 `client` 一路带下去：`Runtime.bank_client` → `agent_graph_factory(..., client=...)` → 内层 `factory(model, context, assembler=..., turn=..., client=...)` → `build_agent_graph(..., client=...)` → `build_domain_graph(..., client=...)` → `spec.declaration.graph_builder(model, spec, assembler=..., turn=..., client=...)`。**不做这一步，B2 之后 wealth 拿不到 `WealthBank`**（`turn=build_turn` 已删），spec3 的 V-1/V-2 用 `BankDouble` 的端到端用例全部落空（R-7）。
- `backend/app/modules/card/workflow.py:83 build_card_graph(model, spec)` 加同名的三个关键字参数 `*, assembler=None, turn=None, client=None`（忽略即可），否则 TypeError。

### 7.2 handoff 入口公开

`conversation_scope` 与 `_handoff_for`（`backend/app/agents/supervisor.py:607`、`:616-642`）今天只在 `_run_module` 内使用。新增一个公开入口供 graph_builder 模块的节点使用：

```python
@contextmanager
def domain_scope(spec, state, assembler, turn, message) -> Iterator[Handoff]:
    handoff = _handoff_for(spec, state, assembler, turn, message)
    with conversation_scope(handoff):
        yield handoff
```

节点内的用法：

```python
with domain_scope(spec, state, assembler, turn, message) as handoff:
    view = handoff.view(prompt_overhead=read_overhead(questionnaire_lines()))
    ...   # 决策
    handoff.publish(update)
```

**不要用「包一层函数再 `subgraph.invoke()`」绕过**：会破坏 LangGraph 子图的中断/流式/检查点语义，card 的确认卡依赖它（`backend/tests/test_pending_action_api.py`）。

### 7.3 工具门与等待通道

- 工具门：无改动。`_with_tools_available`（`backend/app/agents/supervisor.py:1173`）只在通用路径 `_domain_loop` 里用；graph_builder 模块不过这道门，空菜单的判空由模块自己保证（理财的 7 个只读工具非空）。
- 等待通道：理财的图自己实现 decide/wait/process；`wait` 直接用基座的 `wait_for_user`（`backend/app/agents/supervisor.py:805-826`），不新增基座节点。

### 7.4 验收

- card 的既有用例全绿（`backend/tests/test_pending_action_api.py`、`backend/tests/test_multi_agent_api.py`）。
- 新增一条：`graph_builder` 收到 `assembler`/`turn`/`client` 三个关键字参数（合成模块，断言 `assembler`/`turn` 不是 None；`client` 允许 None）。

## 8. W-5 声明与注册（`wealth/__init__.py`）

```python
WEALTH = DomainDeclaration(
    agent_id="wealth",
    display="理财 Agent",
    purpose="处理理财产品、持仓、收益、赎回相关的请求",
    fields=WEALTH_FIELDS,               # 新增：可追问槽位的 FieldMeta（§2.7 第 3 条）
    graph_builder=build_wealth_graph,   # 新增
)

MODULE = ModuleSpec(
    name="wealth",
    declaration=WEALTH,
    # ⚠ 不能写 tools=READ_TOOLS：READ_TOOLS 的值是 ReadTool(spec, method, run)（infrastructure/tools.py:75-86/:359），
    #   不是 ToolSpec；ModuleSpec.tools 必须是 Mapping[str, ToolSpec]（modules/base.py:109，:121 构造期就访问
    #   spec.name，registry.py:87 访问 spec.side_effect）→ 直接写会在 import 期 AttributeError。
    tools={name: tool.spec for name, tool in READ_TOOLS.items()},   # 7 个只读 ToolSpec
    handlers=public_handlers(),    # 新增：infrastructure/tools.py:368-381
)
```

- 删 `turn=build_turn`（`backend/app/modules/wealth/__init__.py:28`）。
- `WEALTH_FIELDS` 由 `backend/app/modules/wealth/workflow/state.py` 的 `ASKABLE_SLOTS`（`:121`）与 `SLOT_LABELS`（`:123-132`）生成：每个可追问槽位一个 `FieldMeta(label=SLOT_LABELS[name], type="text")`。`SLOT_UNITS`（`:151-155`）今天没有任何使用者，**不要写进生成式**（要么拿它的单位补 `label`，要么别动它）。
- `MODULE.tools` 非空 ⇒ `DomainSpec.tool_menu()`（`backend/app/agents/declaration.py:85-92`）非空 ⇒ `spec.handle.tools` 非空 ⇒ `build_domain_graph` 走 `graph_builder`（§7.1）。这条链必须成立，否则图不会被调用。
- `ModuleSpec` 的硬互斥（`backend/app/modules/base.py:138-142`）要求 `turn` 与 `tools` 不同时出现；删 `turn=` 是这条改动的必要条件。

## 9. W-6 状态搬家（本 spec 最大的一块）

### 9.1 目标形状

```python
class WealthState(AgentState, total=False):
    # 会话（原 _Session 的字段，wealth/application/runtime.py:337）
    wealth_slots: dict[str, Any]
    wealth_shown: dict[str, Any]
    wealth_intent_version: int
    wealth_clarifications: int
    wealth_pending_id: str | None
    wealth_last_holding: dict[str, str] | None   # 原 _Session.last_holding（runtime.py:361）是 dict，不是 str
    wealth_unresolved: dict[str, Any] | None    # 原 _Unresolved(:318)
    # 写意图（原 _Pending(:298) + _WriteIntent(:255) + _WriteDraft(:241)）
    wealth_pending: dict[str, Any] | None
    # ⚠ _WriteDraft.snapshot 是 PurchaseSnapshot | RedeemSnapshot（runtime.py:250），_WriteIntent.receipt
    #   是 PurchaseReceipt | RedemptionReceipt（:267），四个都是 frozen+slots dataclass（contracts/bank.py:389/:577 等）。
    #   langgraph 1.2.11 的 msgpack 对未注册类型告警，LANGGRAPH_STRICT_MSGPACK=true 直接失败；
    #   snapshot 已有 as_state/from_state，receipt 没有，要新写。搬家必须同时补这两处的 dict 转换。
    # 读流程工作区（原 WealthWorkflowState 的字段，workflow/state.py:445-499）
    wealth_work: dict[str, Any]
    wealth_asked_question: str | None           # 原 pending_question，改名
```

- **改名**：`WealthWorkflowState.pending_question: str | None`（`wealth/workflow/state.py:496`）与基座 `AgentState.pending_question`（dict）同名冲突，改成 `wealth_asked_question`。
- **形态**：`_Session`/`_Pending`/`_WriteIntent`/`_WriteDraft`/`_Unresolved` 从 dataclass 换成 TypedDict（纯数据）；`_WriteIntent.preview_facts()`（`:291`）这类方法变成模块级函数。
- **代价要算清**：换成 TypedDict 后 `runtime.py` 里 `session.`/`record.`/`write.`/`.snapshot`/`.receipt` 等属性访问**约 140 处**要改下标，`_WriteIntent` 的 3 个 property（`:276`/`:282`/`:288`）与 `preview_facts`（`:291`）要改成模块级函数。这不是「逻辑一行不改」的小改，是搬家本身的最大工作量。
- **留在进程内**：`_locked_users`/`_technical_failures`/`_bypass_events`（`:385-392`）——它们是跨线程安全计数，不是会话状态。
- **不进 state**：`Clock`、`WealthBank`、`ModelClient`（从图构建闭包拿）。

### 9.2 `WealthAgent` 变成无会话状态的服务对象

今天 `WealthAgent.handle` 自己 `self._session(...)`（`:586`）取会话、`self._pending`（`:384`）取待办。改法：会话与待办由调用方传入传出。

**这是一次破坏性签名变更，调用点比想象的多**：`tests/wealth/` 里 `agent.handle(` **实测 87 处**、`tests/wealth_integration/` **60 处**、`cli.py:4`/`:260` 各一处。§15 里「不动 CLI 就自动受益」是错的——CLI 与独立入口都要跟着改（§12.4 T-12/T-14、§17）。

**两个搬运类型必须先定义出来（审查 C-10）**：`SessionData`/`PendingData` 在基线里**不存在**（今天只有 `:298 class _Pending`、`:337 class _Session`）。它们定义在 `wealth/workflow/state.py`，与图 state 的扁平 `wealth_*` 字段**一一对应**：

```python
class SessionData(TypedDict, total=False):
    slots: dict[str, Any]
    shown: dict[str, Any]
    intent_version: int
    clarifications: int
    pending_id: str | None
    last_holding: dict[str, str] | None
    unresolved: dict[str, Any] | None


class PendingData(TypedDict, total=False):
    # 原 _Pending(:298) + _WriteIntent(:255) + _WriteDraft(:241) 的纯数据字段
    ...
```

图 state 保持 §9.1 的扁平 `wealth_*` 命名（为了不与基座同名字段冲突），**节点负责映射**，映射函数也放在 `state.py`：

```python
def session_of(state: Mapping[str, Any]) -> SessionData: ...
def merge_session(state: dict[str, Any], session: SessionData) -> None: ...
```

于是 §9.1 的扁平清单与本节的两参数签名**不冲突**：`handle` 收/返的是两个搬运类型，图 state 存的是 `wealth_*` 扁平字段，`wealth_decide`/`wealth_process` 各做一次映射。

```python
def handle(
    self,
    context: TrustedContext,
    request: WealthRequest,
    *,
    session: SessionData,          # 由节点从图 state 映射出来
    pending: PendingData | None,   # 同上
    conversation=None,
) -> tuple[WealthResponse, SessionData, PendingData | None]: ...
```

（`*` 是**保留**基线的 keyword-only 约定：`runtime.py:404-410` 今天是 `def handle(self, context, request, *, conversation=None)`，旧稿把它丢了。）

- `:404 handle`、`:432 _dispatch`、`:603 _commit`、`:745 _valid_pending`、`:769 _take_pending`、`:780 _drop_pending`、`:785 _prune` 改成接收/返回会话数据。
- 写路径 `:815 _confirm`、`:881 _submit_confirmed`、`:999 _reconcile_event`、`:1043 _refresh`、`:1152 _reconcile`、`:1247 _unknown_response`：**逻辑一行不改**，只把 `session` 的来源从 `self` 换成参数。
- `:394 set_model` 保留（模型由基座每轮注入）。

### 9.3 验收

- 既有 `backend/tests/wealth/` 全绿（装配路径见 §12.4）。
- 新增：同一 `thread_id` 连续两次调用，第二次能看到第一次的槽位与待办；换 `thread_id` 看不到。
- **`_prune` 的语义变化必须写进实现（审查 I-21）**：今天 `_prune`（`runtime.py:785-794`）遍历进程内的 `_sessions`/`_pending` 表做过期清理（`test_base_turn.py:106` 的「过期 pending 不吞下一条消息」依赖它）。搬进图 state 后**没有全局表可遍历**，清理只能变成「进入节点时对传入的 `session`/`pending` 判过期，过期即丢弃并回退到 MESSAGE」，并在 §12.3 加一条断言。**不要**为了保住 `_prune` 再维护一个跨 thread 的清理表 —— 那等于把会话状态搬回进程内存，直接违背 R-2。

## 10. W-7 图接线（`wealth/workflow/graph.py` 新增 `build_wealth_graph`）

照 card 的形状（`backend/app/modules/card/workflow.py:263-274`）：

```python
def build_wealth_graph(model, spec, *, assembler=None, turn=None, client=None):
    graph = StateGraph(WealthState)
    graph.add_node("decide", wealth_decide(model, spec, assembler=assembler, turn=turn))
    graph.add_node("wait", wait_for_user)                      # 基座的
    graph.add_node("process", wealth_process(model, spec, assembler=assembler, turn=turn))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
    return graph.compile()
```

- `route(state) -> "wait" if state.get("turn_phase") == "waiting_for_user" else END`（与 card 的 `:263-265` 同形）。
- **decide 节点**：进 `domain_scope`（§7.2）→ 调 `WealthAgent.handle`（§9.2）→ 用基座的 `_conclude_turn(spec, result, writer, state, updates)`（`backend/app/agents/supervisor.py:667-721`）落盘。
  - `TurnResult(status=COMPLETED, ...)` → `idle_work_state()`，图结束。
  - `TurnResult(status=WAITING_INPUT, ...)` → **不能只调 `_conclude_turn`**：它对 `WAITING_INPUT` 只写 `awaiting_agent` + `idle_work_state()`（`supervisor.py:709-717`），真正的追问要靠 `_awaiting(spec, outcome, writer, ...)`（`:1022-1081`），而它读的是 `outcome.ask`。`TurnResult`（`app/agents/turn.py:149`）只有 `status/summary/facts/business_ids/confirmation`，**没有槽位名**（槽位名在 `_Pending.missing` 里）。所以 decide 节点必须自己拼一个 `DomainOutcome(ask={"fields": [...], "display": summary})` 再交给 `_awaiting`，否则 route 返回 END、追问不产生中断。
  - `TurnResult(status=WAITING_CONFIRMATION, summary=..., confirmation=TurnConfirmation(...))` → `_conclude_turn` 自动走 `_confirmation_pause`（`:724-756`）写真中断。
- **wait 节点**：基座 `wait_for_user`，`interrupt(dict(question))`。
- **process 节点**：`answer, problem = _read_answer(state.get("pending_answer"), question)`（`backend/app/agents/supervisor.py:1134`）→ `problem` 非空则用 `_awaiting(reask=...)` 重挂；`question["kind"] == CONFIRMATION_KIND` 时按 `_action_kind`（`:974-991`）分确认/取消，用闭包里的 `turn.verification_code`（仅当 `turn is not None and kind == TurnAction.CONFIRM`）提交；否则把 `{字段: 值}` 作为答案续跑 `WealthAgent`。
- **只读工具走基座**：`wealth.*` 的调用改成 `_call_tool(spec, DomainOutcome(tool_name=..., arguments=...), writer, idempotency_key=None)`（`backend/app/agents/supervisor.py:1489-1545`），于是自动获得 `tool_call`/`tool_result` 事件与 `_may_retry` 的瞬时失败重试。**两个实参从哪来（审查 I-20）**：`spec` 就是 `graph_builder(model, spec, ...)` 的第二实参（闭包捕获）；`writer` 是节点内 `get_stream_writer()`（card 的 `run_call`/`finish` 今天就是这么拿的，`modules/card/workflow.py:84-93`/`:95-113`）。`_call_tool` 内部还会用 `spec.handle` 找到工具并写 `agent_id`，所以**不要**把 `spec` 换成裸的 `READ_TOOLS`。理财自己的 `read_with_one_retry`（`wealth/infrastructure/tools.py:96-115`）在这一层不再需要（基座 `_run_tool` 自带）。
- **确认卡片**：`TurnConfirmation`（`backend/app/agents/turn.py:57-70`）的构造逻辑从 `WealthTurn._confirmation`（`wealth/application/turn.py:259`）搬进 decide 节点；`_preview_text`（`:249`）同理。
- **`_to_result` 的五个分支必须搬到 decide 节点（审查 C-11b）**：`WealthTurn._to_result`（`wealth/application/turn.py:205-245`）今天负责把 `WealthResponse` 映射成 `TurnResult`，五条分支各有语义：① `status == UNKNOWN` **不得**落成 `FAILED`（未知结果不算失败，`test_confirmation_events.py` 有专门用例）；② `WAITING_CONFIRMATION` → 构造 `TurnConfirmation`（`_confirmation`，`:259`）；③ `WAITING_VERIFICATION` → 取消补偿 + `UNSUPPORTED`；④ 其余走 `_STATUS.get(response.status, FAILED)` 的默认映射；⑤ `facts` 与 `business_ids` 透传。基座的 `DomainOutcome`（`supervisor.py:186-196`）**只有 `tool_name`/`arguments`/`ask`/`summary`，没有 `status`**，所以这段映射不能交给 `_conclude_turn` —— 落成一个模块级 `def _turn_result(response: WealthResponse) -> TurnResult`（放 `wealth/workflow/graph.py`），再按本节上面的三条落盘方式分派。
- **删**：`backend/app/modules/wealth/application/turn.py` 的 `WealthTurn`（`:69`）与 `build_turn`（`:343`）。`build_wealth_agent`（`application/factory.py:26-46`）保留。**拆除面比 `turn.py` 大（审查 C-11）**：`backend/tests/wealth/test_confirmation_events.py` 有**六处**直接构造 `WealthTurn`（`:829`+`:832`、`:848`+`:851`、`:871`+`:874`、`:894`+`:897`、`:921`+`:925`、`:943`+`:948`，其中 `:927` 调 `turn._to_result`）→ 改成直接调 `_turn_result(response)`；`test_base_turn.py:24`(import) 与 `:110`(构造) 一并改；`test_context_handoff.py` 有 14 处 `drive(`（`:183`/`:205`/`:232`/`:247`/`:267`/`:289`/`:306`/`:321`/`:330`/`:353`/`:362`/`:402`/`:433`/`:451`，定义 `:149`）+ `:30`(import) + `:155-158`(fixture) + `:437 restarted = WealthTurn(...)` 全部改走图。

### 10.1 前端连带：既有输入卡片通道直接复用（无需改代码）

`turn=` 模块的追问今天是「下一条聊天消息」，B2 之后变成检查点中断；这一步的**前端工作量为零**，
因为 card 模块的 `ask_user` 已经把这条通道建好并测过：

| 环节 | 位置 | 事实 |
| --- | --- | --- |
| 渲染 | `frontend/src/App.vue:947-970` | `pending` 存在即渲染 `.interrupt-card`；`pendingFields`(`:135`) 按 `field.type` 渲染 `select`/`number`/`date`/`text` |
| 必填 | `frontend/src/App.vue:642 interruptComplete(card)` | 必填字段为空时「提交」按钮禁用 |
| 发送 | `frontend/src/App.vue:571-593 submitInterrupt()` | `:592` 发送「补充信息：…」并把 `resumeFor(card, value)` 作为客户端上下文；`value` 的键**逐个来自 `card.fields`** |
| 构造器 | `frontend/src/api.ts:24-25` | `resumeFor(question, value) -> {resume: {question_id, value}}` |
| 解析 | `frontend/src/conversations.ts:130-133` | 账本里的 `client_context.resume` 反解回卡片 |
| 阻塞 | `frontend/src/App.vue:108`、`:430` | 有 open interrupt 时不允许再发普通消息（与后端 409 一致） |
| 用例 | `frontend/src/App.test.ts:491-503` | 已有：输入卡片提交后 `client_context.resume.value` 等于字段值 |

**因此**：wealth 侧唯一要做的是把可追问槽位声明进 `WEALTH_FIELDS`（§8），
并且每个字段的 `type` 落在前端认识的四个值里（`select`/`number`/`date`/`text`），否则会退化成文本框。

**回归（唯一一条可新增的前端用例，可选）**：在 `frontend/src/App.test.ts` 加一条 wealth 形状的输入卡片用例——
两个字段（一个 `select`、一个 `text`）且都必填；断言未填满时提交按钮禁用、填满后 `resume.value` 的键集合**恰好等于**字段集合。
它钉住的是「理财追问字段多于一个时不会少键」：后端 `_read_answer` 要求键集合**完全相等**（§2.7 第 2 条）。


**空 `missing` 的兜底（2026-10-12 二次审查裁定，m02008）**：wealth 侧还有一种提问不带字段——
`ask_user` 的 `missing` 允许空数组（`llm/toolcalls.py:110-118`），`resolve_fields(decl, [])` 返回 `[]`，
今天会渲染成一张没有输入框、却能点提交（只能提交 `{}`）的卡片。裁定为**补一个自由文本字段**，
改动落在基座 `_awaiting`（`app/agents/supervisor.py:1049-1051`），wealth 与 card 一起受益，细节见 spec1 §7.1。
wealth 侧的验收用例是 §12.3 的 T-17：`missing=[]` 的追问必须变成一张有一个 `text` 字段的卡片，
且 `{"answer": "…"}` 能被 `_read_answer` 接受。

## 11. W-8 `application/factory.py`：能力检查换成 `stream`

（原 §7 的内容，一字不改地保留在这里。）

`:42` 现在只认 `complete`：

```python
    if not callable(getattr(model, "complete", None)):
        raise TypeError("model 必须是 ModelClient，需要一个 complete(messages) 方法")
```

改为：

```python
    if not callable(getattr(model, "stream", None)):
        raise TypeError("model 必须是 ModelClient，需要一个 stream(messages, *, tools=...) 方法")
```

（`complete` 仍被 `app/context/summary.py` 等使用，但理财决策不再需要它。）

## 12. W-9 测试面

### 12.1 `backend/tests/wealth/support.py`

`:455-537` 的 **10 个**决策助手从"返回 JSON 字符串"改成"返回 `ToolCall`"：

```python
def call(tool: str, arguments: Mapping[str, Any] | None = None) -> ToolCall:
    """模型的一次工具调用。"""
    return ToolCall(name=tool, arguments=dict(arguments or {}))
```

- `call_read`/`set_slots`/`ask_user`/`finish`/`submit_risk`/`recommend`/`compare`/`prepare_purchase`/`prepare_redeem` 的返回类型注解与 docstring 一并改（`finish` 的 `cited_ids`/`shown` 语义不变，只是不再 `json.dumps`）。
- 新增 `text_reply(text: str) -> tuple[str, list[ToolCall]]`：返回 `(text, [])`，用于测"只有散文"的新路径。
- 新增 `decision_input(text_or_call) -> tuple[str, list[ToolCall]]`：把单个 `ToolCall` 包成 `("", [call])`，让调用点保持一行。

### 12.2 调用点改写

| 文件:行 | 改法 |
| --- | --- |
| `parse_decision` 的**全部**调用点（实测 **21 处** = 20 个测试行 + 1 个生产点，不是 4 处；旧稿写 22 处是把 `support.py` 的助手定义也算了进去） | 测试侧一律改成 `parse_decision(*decision_input(reply), table=tool_table(READ_TOOLS))`：`test_purchase.py:578`/`:587`/`:594`、`test_redeem_income.py:578`/`:583`、`test_read_flow.py:636`/`:637`/`:640`/`:643`/`:651`/`:675`、`test_dialogue.py:216`/`:601`/`:607`/`:614`、`test_risk_recommend.py:681`/`:693`/`:708`、`test_safety.py:793`/`:796` |
| `backend/app/modules/wealth/workflow/nodes.py:967`（**生产调用点**，旧稿的表漏了它） | `decision = parse_decision(reply, table=table)` → `decision = parse_decision(*_model_decision(...), table=table)`（W-4 的 `_model_decision` 返回 `(text, tool_calls)`） |
| `backend/app/modules/wealth/application/turn.py:24`/`:25`/`:309`（**生产调用点**，C-7） | 删 `from ...infrastructure.tools import tool_menu`；`read_overhead(tool_menu(), questionnaire_lines())` → `read_overhead(questionnaire_lines())` |
| `test_context_handoff.py:39`/`:195` | `read_overhead(tool_menu(), questionnaire_lines())` → `read_overhead(questionnaire_lines())`；**同时删 `:38` 的 `from ...infrastructure.tools import tool_menu`**（改完就没用了；`pyproject.toml:50` 的 ruff `select=["E","F","I"]` 含 F401，不删 §14.1 直接失败） |
| 所有 `model.complete` 断言（若有） | 换成断言 `model.tools` 非空 |

### 12.3 新增用例

| # | 用例 | 断言 |
| --- | --- | --- |
| T-1 | 决策走 `stream` 且带 tools | 替身记录 `stream` 调用，`tools` 含 `set_slots`/`ask_user`/`finish` 与 7 个 `wealth.*`；`complete` 一次都没被调用 |
| T-2 | 送出去的 schema 是 strict 可表达的 | 每个 `function.parameters` 的 `additionalProperties is False`；每个 `properties` 键都在 `required` 里；无 `minLength`/`minItems`/`maxItems`/`minProperties`/`pattern`。**`strict` 必须等于 `strict and expressible`**（`app/tools/schema.py:85` 就是这么绑的）。控制工具里有 strict 不可表达的嵌套对象——`set_slots.slots`、`submit_risk.answers`、`prepare_purchase`/`prepare_redeem` 的内层对象都没有 `additionalProperties`/全 `required`——无条件发 `strict: true` 可能每次被 provider 拒表 |
| T-3 | 被拿掉的约束进了 description | `compare` 的 description 含"至少 2"类字样。**注意 W-1 现在写错了**：`_wire_parameters` 里 `entry = replace(entry, ...)` 改的是局部变量，函数返回的是 `schema`，`function_schema` 用的还是原 `entry.description` → description 补救从未生效。要么让它返回 `(schema, description)`，要么把 description 一起改在 `function_schema` 里 |
| T-4 | 参数 schema 校验没有放松 | `compare` 只给一个 `product_id` → 仍是 `DecisionError`（用 `entry.parameters` 的 `minItems` 挡住） |
| T-5 | 一次两个工具调用被拒 | `DecisionError`，消息含"一次只能调用一个工具" |
| T-6 | 未知工具仍是越门禁 | `GateViolation`（`gate_violations` 计数 +1） |
| T-7 | 只有散文等价于一次 `finish` | `decision.action == "finish"`，`answer` 是原文 strip 结果 |
| T-8 | 工具表里仍然没有资金写 | `"redeem" not in tool_table()` 与 `"purchase" not in tool_table()`（保持 `test_redeem_income.py:592` 的口径） |
| T-9 | 提示词不含工具表文本 | `read_messages` 渲染结果里不含 `"### set_slots"`、不含 `CALL_SHAPE` 的字面 `{"tool"` |
| T-10 | 模型缺 `stream` 时构造失败 | `build_wealth_agent` 抛 `TypeError`，消息含 `stream` |
| T-11 | 越门禁计数不变 | `prepare_purchase` 塞 `user_id` → `GateViolation` 且 `gate_events` 计 1 |
| T-16 | wire schema 与校验器对同一参数意见一致 | `finish` 的 `cited_ids`/`shown` 在 wire schema 里是「必填可空」（`app/tools/schema.py:53-61` 把非 required 属性变成 `[X,"null"]` 并补进 required），strict 下的模型会显式回 `cited_ids: null`，而 `_read_decision` 用的原始 `entry.parameters`（`toolcalls.py:231-234`，required 只有 `["text"]`）只允许 array → `argument cited_ids: failed type` 被误判成格式错并白纠正一次。断言：回 `null` 时**不**报 DecisionError（修法：校验前按 wire 归一化，或 `_schema_problem` 容忍 null，或只发可严格表达的 schema） |
| T-17 | 空 `missing` 的追问也可答 | `ask_user(question="请问您想咨询什么？", missing=[])` → 中断卡片 `fields` 恰一项 `{"name": "answer", "type": "text", "required": True}`（基座兜底，见 spec1 §7.1）；提交 `{"answer": "…"}` 后 `_read_answer` 放行并回到 decide，不报 `DecisionError` |

### 12.4 B2 带来的装配路径问题

今天 `backend/tests/wealth/` 的写流程四个文件共 103 个测试函数（`test_base_turn.py` 6、`test_confirmation_events.py` 35、`test_purchase.py` 30、`test_redeem_income.py` 32；`pytest --collect-only` 收集到 125 个用例，差额来自 parametrize）经 `build_wealth_agent` + `WealthTurn` 驱动；`backend/tests/wealth_integration/` 的 A15「无前端无 Supervisor 无平台数据库」也走同一条路。B2 之后 `WealthTurn` 不存在了。

改法（按代价从小到大）：

| # | 用例组 | 改法 |
| --- | --- | --- |
| T-12 | 直接调 `WealthAgent.handle` 的用例（**实测 87 处**，不是「少数几处」） | 用新的 `handle(context, request, session, pending)` 签名，会话由用例自己持有（等价于把图 state 摊在用例里） |
| T-13 | 经 `WealthTurn` 的整轮用例 | 只有 4 处用 `build_turn(`（`test_base_turn.py:40`/`:53`/`:82`/`:150`），但 `test_context_handoff.py` 的 13 条**全部**经 `drive(turn: WealthTurn, ...)`（`:149`/`:155-158`/`:437`），也在这组。改成走图：照 `backend/tests/test_domain_turn.py:120 compiled(...)` 的写法，用 `build_agent_graph` + `InMemorySaver` 驱动 |
| T-14 | `wealth_integration` 的独立入口（A15） | **只改装配与调用签名，断言与场景一行不改**：新增一个模块级装配函数（`build_wealth_graph` + `InMemorySaver` + 内存银行），供「无基座」用例使用；60 处 `agent.handle(` 改新签名 |
| T-15 | 追问变成中断 | **改 `test_base_turn.py:50`**（`test_two_turns_on_the_same_handler_keep_the_slots`，第二轮 `ask_user` → `WAITING_INPUT`）。`:35` 那条是 `call_read`+`finish` → `COMPLETED`，B2 之后**仍然**是 completed，不要动它——按原稿改会删掉唯一的读问答覆盖 |

**这一组是本 spec 风险最高的地方**：103 个测试函数（125 个收集用例）的装配路径同时改变，必须与 §9 的搬家同批完成，不能分两次提交。

## 13. 与 spec1 的交界

- spec1 §5 **保留** `app/agents/decisions.extract_json_object`（`app/context/summary.py:41`/`:258` 也在用）；wealth/llm/decisions.py:39/:170 对它的使用随 W-2 消失。**两份 spec 没有编译级耦合，可以分开落地**（2026-10-12 更正，审查 I-6；旧稿说「必须同批」是错的 —— spec1 删的 `as_text`/`as_fields`/`as_arguments` 与协议常量 wealth 一个都没引用）。
- spec1 §10 的 `app.agents.toolcalls.collect_stream` 是本 spec W-4 的依赖。
- spec1 §12 的 `tests/support/scripted_model.py` 改造（替身实现 `stream`）是本 spec 测试的前提。
- **本 spec §7 的 W-0 要落在 spec1 的文件范围内**（`app/agents/supervisor.py`、`app/agents/declaration.py`、`modules/card/workflow.py`）。执行顺序见 §18：W-0 先于 wealth 的搬家，且不动任何既有行为。

## 14. 验收

1. `cd backend && uv run ruff check app tests scripts` → exit 0
2. `cd backend && uv run ruff format --check app tests scripts` → exit 0
3. `cd backend && uv run pytest -q tests/wealth tests/test_wealth_graph.py tests/test_graph_builder_seam.py tests/test_domain_turn.py tests/test_pending_action_api.py tests/test_turn_wiring.py` → 全绿
   （`tests/test_base_turn.py` **不存在**——真实路径是 `tests/wealth/test_base_turn.py`，已含在 `tests/wealth` 里；`tests/test_turn_wiring.py:102-118` 断言生产 registry 的 `turn_handlers` 含 `"wealth"`，删 `turn=` 后必红，必须同步改，见 §17）
   （`tests/wealth/` 是纯单元测试，用内存替身，不碰云库，可以放心全量跑。）
4. `cd backend && uv run pytest -q tests/test_domain_loop.py tests/test_domain_protocol.py tests/test_supervisor_streaming.py tests/test_multi_agent_api.py` → 全绿（spec1 的连带）
5. `cd frontend && pnpm exec vue-tsc --noEmit && pnpm test` → exit 0 / 全绿（§10.1 的前端回归）
6. 真机冒烟：`pwsh -File start.ps1 -Restart` 后，`POST /api/chat/messages`（带 `Idempotency-Key`）发「我有哪些理财产品」→ 断言 `subtask_end.turn_status == "completed"` 且 `final` 正文含真实持仓金额，且**新增** `tool_call`/`tool_result` 帧；再发「我想申购1000元理财产品」→ 断言 `event: interrupt`（`kind=input`）而不是普通的 `waiting_input` 收尾；带 `client_context.resume` 回填后继续走到 `kind=confirmation` 并点击确认。
7. 仓库根 `rg -nE "tool_table_text|TOOL_CALL_NOTICE|CALL_SHAPE" backend/app` → 空（本机只有 `rg.exe`，**没有 `grep`**；`rg` 默认扩展正则）
8. 序列化往返：把图 state 喂给 langgraph 的 serde（`JsonPlusSerializer`，1.2.11 里是 `dumps_typed`/`loads_typed`）再读回并断言等值。**tuple 会变成 list**（`_Pending.missing`、`_Session.shown`）——要么全用 list，要么在断言里归一化

## 15. 明确不做

- 不保留 `turn=`：B2 就是删掉它（§2.1）。`_domain_loop` 仍不会被生产领域使用（card 与 wealth 都走 `graph_builder`）。
- 不删 `WealthDecision`/`_read_decision` 的任何一条门禁（§12.3 T-4/T-6/T-11 钉住）。
- 不新增资金写工具（`purchase`/`redeem` 执行仍只在确认通道里）。
- **不改** `wealth/application/runtime.py` 的写逻辑：`_confirm`/`_submit_confirmed`/`_reconcile`/`_unknown_response` 的**判定**一行不改，只换会话数据的来源（§9.2）。但「一行不改」不等于「不动这个文件」：dataclass→TypedDict 会波及约 140 处属性访问（§9.1）。
- 不改 `wealth/infrastructure/tools.py` 的 7 个 `ReadTool` 定义与 `public_handlers()`（它们今天就已经是基座可用的 `ToolSpec`/`ToolHandler`）。
- 不动 wealth 的 CLI（`cli.py`）的**逻辑**——它用同一套 `WealthAgent`；但 `handle` 换签名后 `cli.py:4`/`:260` 与 `tests/wealth_integration/` 的 60 处调用要跟着改（§12.4 T-12/T-14）。
- 不引入 `tool_choice`（§19 裁定 1：不加）。
- 不动 card（spec1 §7）与四个空壳领域。
- 不改 `EVENT_FIELDS`（新增的 `tool_call`/`tool_result` 帧用的是既有事件，不新增事件名）。

## 16. 风险

| 风险 | 缓解 |
| --- | --- |
| 状态搬进图 state 后不可序列化（生产 checkpointer 是 Postgres，图 state 每节点落盘） | §9.1 的硬约束：只放 dict/list/str/int/bool/None；`Clock`/`WealthBank`/`ModelClient` 走闭包。**已知反例**：`_WriteDraft.snapshot`/`_WriteIntent.receipt` 是 frozen+slots dataclass（`contracts/bank.py:389`/`:577` 等），langgraph 1.2.11 的 msgpack 对未注册类型告警、`LANGGRAPH_STRICT_MSGPACK=true` 直接失败；receipt 没有 `as_state`/`from_state`，要新写。T-12 用 `InMemorySaver`，另加一条 serde 往返断言（注意 tuple→list） |
| 103 个测试函数（125 个收集用例）的装配路径同时改变，改到一半两套并存 | §12.4 明确同批完成；执行顺序（§18）把「搬家 + 测试改写」放在同一步 |
| 追问从「下一条聊天消息」变成「必须在卡片上提交」，前端不跟进就答不上话 | 前端通道已就绪（§10.1，card 的 `ask_user` 今天就在用）；§10.1 的可选回归 + spec3 V2-7 的裸消息 409 用例 + §14.6 真机断言 `event: interrupt` |
| `ask_user` 的 `missing=[]`（问问卷某一题／自由提问）在 B2 下答不上 | `missing` 允许空数组（`llm/toolcalls.py:110-118`），`resolve_fields(decl, [])` 返回 `[]`（`agents/declaration.py:121-136`），前端卡片因此没有输入框；而 `events.ts:192` 的 `fields.every(...)` 对空数组恒真 → 提交按钮可点但只能提交 `{}`，同时输入框被 `pending` 禁用（`App.vue:107-109`/`:430`）。**已裁定**（§19 第 4 条，m02008）：补一个自由文本字段——基座在 `_awaiting`（`app/agents/supervisor.py:1049-1051`）兜底 `{"name": "answer", "label": "补充说明", "type": "text", "required": True}`，前端零改动；wealth 侧用例见 §12.3 T-17 |
| 答案从自由文本变成 `{字段: 值}`，理财的槽位语义可能对不上 | `WEALTH_FIELDS` 由 `ASKABLE_SLOTS` 生成，字段名就是槽位名；`_read_answer` 校验后直接喂 `normalize_slots`，不需要模型再解析 |
| 模型改用散文回答，不再调 `finish` → 失去 `shown`/`cited_ids` 的编号来源，「第 2 只」解析不到 | T-7 已把散文等价成 finish；提示词保留「要回答就调 finish」的规则；§19 裁定 1 决定不加 `tool_choice` |
| strict 子集裁剪拿掉 `minItems` 等后模型更容易给坏参数 | 裁剪只影响「模型看到的提示」，Python 侧校验器仍用完整 `entry.parameters`（T-4）。**但两者已经不一致**：wire schema 把非 required 属性变成「必填可空」（`app/tools/schema.py:53-61`），strict 下的模型会显式回 `cited_ids: null`，而原始 `entry.parameters` 只允许 array → `argument cited_ids: failed type` 被误判成格式错。修法与用例见 §12.3 T-16 |
| 工具表从提示词移到 API 参数后，模型对工具语义的理解变差 | `ToolEntry.description` 是唯一说明来源，W-1 保证它完整送出；必要时把它写得更像使用说明 |
| `read_overhead` 少一个实参后提示词长度断言变化 | `test_context_handoff.py:195` 同步改；断言的是「overhead 与渲染器一致」，不是具体长度 |
| 替身缺 `stream` 导致大量用例失败 | spec1 §12 已把 `tests/support/scripted_model.py` 改造列为必做项 |
| W-0 扩参把 card 弄坏 | `build_card_graph` 只加三个被忽略的关键字参数（`assembler`/`turn`/`client`）；`test_pending_action_api.py` 与 `test_multi_agent_api.py` 全绿是 W-0 的验收条件 |

## 17. 迁移清单（按文件）

| 文件 | 动作 |
| --- | --- |
| `backend/app/runtime.py` | W-0（C-3）：`agent_graph_factory`/内层 `factory` 加 `client=None` 并透传 `self.bank_client`（`:122`/`:130`/`:69-74`/`:86-91`） |
| `backend/app/agents/supervisor.py` | W-0：`:2063-2064` 调用点扩参（`assembler`/`turn`/`client`）；`build_agent_graph`/`build_domain_graph` 透传 `client`；导出 `domain_scope` |
| `backend/app/agents/declaration.py` | W-0：`:56` 的 `graph_builder: Callable[[Any, Any], Any] | None` 类型注解放宽（`:62` 是 `display name` 的 ValueError，不是注解） |
| `backend/app/modules/card/workflow.py` | W-0：`:83` 加被忽略的关键字参数（`assembler`/`turn`/`client`） |
| `backend/app/modules/wealth/__init__.py` | W-5：`WEALTH` 加 `fields`/`graph_builder`；`MODULE` 加 `tools`/`handlers`、删 `turn=` |
| `backend/app/modules/wealth/llm/toolcalls.py` | 删 3 个导出 + `tool_table_text`；加 `function_schema`/`function_schemas`/`_wire_parameters`/`_drop_keywords` |
| `backend/app/modules/wealth/llm/decisions.py` | `parse_decision` 换签名与规则；删 `_CALL_KEYS` 与顶层键检查；加 `render_call` |
| `backend/app/modules/wealth/llm/prompts.py` | 删 `{tools}` 槽位与 `tools` 形参；`read_overhead` 去 `tools` |
| `backend/app/modules/wealth/application/turn.py` | W-3（C-7）：`:24` 删 `tool_menu` import、`:309` 的 `read_overhead` 去第一个实参；W-7：整个文件删除（`WealthTurn`/`build_turn`），`_confirmation`/`_preview_text`/`_to_result` 的逻辑搬进 decide 节点 |
| `backend/app/modules/wealth/workflow/nodes.py` | `decide_node` 用 `function_schemas`；`_model_text`→`_model_decision`；`:967` 的 `parse_decision` 收 `tool_calls`；`:88` 删 `tool_table_text` import；`except` 加 `StreamTruncated` |
| `backend/app/modules/wealth/workflow/graph.py` | W-7：新增 `build_wealth_graph`（decide/wait/process）+ 模块级 `_turn_result`（原 `WealthTurn._to_result` 的五分支） |
| `backend/app/modules/wealth/workflow/state.py` | W-6：`WealthState`；`pending_question` 改名；`WEALTH_FIELDS`；`SessionData`/`PendingData` 与 `session_of`/`merge_session` 映射 |
| `backend/app/modules/wealth/application/runtime.py` | W-6：`_Session`/`_Pending`/`_WriteIntent`/`_Unresolved` 换 TypedDict；`handle` 收/返会话数据；`_prune` 改成节点内判过期（I-21） |
| `backend/app/modules/wealth/application/factory.py` | W-8：`:42` 检查 `complete` → 检查 `stream` |
| `frontend/src/App.vue` | **不改**：`:592` 已对输入卡片发 `resumeFor`（§10.1）；至多加一条 wealth 形状的回归用例 |
| `backend/tests/wealth/support.py` | **10 个**决策助手（`:455`-`:537`）返回 `ToolCall`；加 `text_reply`/`decision_input` |
| `backend/tests/wealth/test_*.py` | `parse_decision` 调用点、`read_overhead` 调用点、装配路径（§12.4） |
| `backend/tests/wealth/test_tool_selection.py` | 新增 T-1..T-3、T-5..T-7、T-9..T-11、T-16、T-17 |
| `backend/tests/test_wealth_graph.py`（新建） | T-12/T-13/T-14/T-15：图驱动、重启不失忆、独立入口、追问是中断 |
| `backend/app/modules/wealth/contracts/bank.py` | W-6：`PurchaseSnapshot`/`RedeemSnapshot`/`PurchaseReceipt`/`RedemptionReceipt` 补 `as_state`/`from_state`（receipt 两个今天没有），供图 state 序列化 |
| `backend/app/modules/wealth/cli.py` | W-6：`:4`/`:260` 的 `handle` 调用改新签名 |
| `backend/tests/wealth_integration/` | W-6：**断言与场景一行不改**，只改 60 处 `agent.handle(` 的新签名与 A15 独立入口的装配函数（§12.4 T-14） |
| `backend/tests/test_turn_wiring.py` | W-5：`:102-118` 的断言（`"wealth" in turn_handlers`）随 `turn=` 删除而失效，改成断言 wealth 走 `graph_builder` |
| `backend/tests/wealth/test_context_handoff.py` | W-3/W-6：13 条经 `WealthTurn`/`drive(turn, ...)` 的用例改走图；`:38` 的 `tool_menu` import 删除 |

## 18. 执行顺序

1. spec1 的 S-8（`app.agents.toolcalls` 落地）
2. **W-0**（基座接缝，独立提交，card 用例全绿即可）
3. W-1（`function_schemas`）
4. W-2（`parse_decision` 换签名）
5. W-3 / W-4（提示词与节点接线）
   —— **3–5 步之间测试套件是红的**（21 处 `parse_decision` 调用点、4 处 `build_turn`、87 处 `agent.handle(` 都还没改）：要么接受中间红，要么把 §12 的调用点改写并进 3–5 步，只在全部改完时跑一次验收
6. **W-5 + W-6 + W-7 + W-9 同批**（声明、搬家、图、测试改写——不能分两次提交）
7. W-8（factory 检查）
8. 前端回归：确认既有输入卡片通道覆盖理财追问（**无需改代码**，§10.1）
9. §14 验收

## 19. 裁定（已定，2026-10-12）

用户 2026-10-12 经选项确认，以及随后的追加裁定：

1. **不给 `ModelClient.stream` 加 `tool_choice`**：维持现状。理财侧靠 T-7（散文等价成一次不带 `cited_ids`/`shown` 的 `finish`）与提示词里「要回答就调 finish」的规则覆盖；等真机实测出现「模型不调 `finish`」再单独裁定加回。
2. **不合并两个 `ask_user`**：理财的 `ask_user`（`{question, missing}`，`toolcalls.py:102`）与基座的 `ask_user`（`{fields, display}`，`app/tools/schema.py`）各留在自己模块的 schema 里，互不冲突，合并会改理财的追问语义与既有用例。
3. **形态取 B2**（2026-10-12 二次裁定，m01371）：「还是做成 b2 吧，和 card 对齐」。本 spec 的 §2.1、§7-§10 按 B2 写；原「保留 `turn=`」的 B1 路线作废。
4. **`ask_user` 的空 `missing` 怎么答**（2026-10-12 二次审查新增，**已裁定**）：`missing=[]` 的提问今天在前端渲染成一张没有输入框的卡片，提交按钮还可点。**补一个自由文本字段**：基座在 `_awaiting` 里对空字段兜底一个 `{"name": "answer", "label": "补充说明", "type": "text", "required": True}`（`app/agents/supervisor.py:1049-1051`，细节与理由见 spec1 §7.1），前端零改动，wealth 侧验收用例见 §12.3 T-17。不取「要求 `missing` 非空」（否掉「问问卷某一题／自由提问」的既有语义）与「前端补 textarea」（后端仍会写出一个无法回答的问题）。

**待用户确认的新增项**（不是可选项，是 B2 的必然结果）：§2.7 的三处行为变化——追问从「下一条聊天消息」变成「必须在卡片上提交」（**前端代码已就绪**，§10.1）、答案变成 `{字段: 值}`、读路径新增 `tool_call`/`tool_result` 事件。若其中任何一条不能接受，需要回到 B1。
