# Spec 1 — 废除 JSON 文本决策协议

- 状态：**待执行**（§16 两项已裁定，见 §16）
- 日期：2026-10-12
- 基线：`D:\ai_project\agent` @ `bc70879`（dev）
- 上位文档：`docs/superpowers/plans/2026-10-12-unify-tool-calling-plan.md`
- 兄弟 spec：`docs/superpowers/specs/2026-10-12-wealth-native-tool-calling-spec.md`（spec2，理财决策层）、`docs/superpowers/specs/2026-10-12-wealth-write-verification-spec.md`（spec3，理财写流程验收）

## 0. 结论

主 Agent 与领域 Agent 的模型决策只保留一条路径：`stream(messages, tools=[...])` 拿结构化 tool_calls。
`json_text` 协议、它的形状常量、它的解析器、它的降级缓存全部删除；上游不支持 `tools` 时**直接报错，不做任何回退**。
本 spec 覆盖基座（`app/agents/`、`app/runtime.py`、`app/core/config.py`）与 card 模块；理财模块的决策层在 spec2，理财写流程验收在 spec3。

## 1. 需求

| # | 需求 | 验收 |
| --- | --- | --- |
| R-1 | 决策只走 `stream(tools=...)` | 全仓 grep 不到 `json_text`/`PROTOCOL_JSON_TEXT` |
| R-2 | 没有"降级"概念 | `app/agents/degradation.py` 不存在；grep 不到 `mark_unavailable`/`FEATURE_TOOLS not in` |
| R-3 | 上游拒绝 `tools` 时报可读错误，不是 500 | 用例断言 `AppError` 且 `details["upstream_code"] == "LLM_TOOLS_UNSUPPORTED"` |
| R-4 | 上下文溢出重试在工具路径上生效 | 用例：第一次溢出 → `assemble(aggressive=True)` 重试 → 成功；两次都溢出 → `context_limit_error` |
| R-5 | 流式收集器只有一份实现 | `app/agents/toolcalls.py` 是唯一实现；`supervisor.py` 与 `card/workflow.py` 都从它导入 |
| R-6 | card 的决策改走工具调用，行为不变 | card 既有 32 个用例全绿；card 的提示词里不再有 JSON 形状 |
| R-7 | 删除协议开关后配置面不残留 | `Settings` 无 `llm_agent_protocol`/`llm_supervisor_protocol`；`.env` 里写了旧键不报错也不生效 |
| R-8 | 三条红线不破 | reasoning 不外发；循环内旁白不进事件流；`wait` 不调模型、循环体不 interrupt |

## 2. 既有事实（改造前，行号基于 `bc70879`）

### 2.1 协议开关的传播路径

```
Settings.llm_agent_protocol            app/core/config.py:44
  → Runtime.__init__                   app/runtime.py:137
  → agent_graph_factory(protocol=)     app/runtime.py:69-102
  → build_agent_graph(protocol=)       app/agents/domain.py:54-82
  → build_supervisor(protocol=)        app/agents/supervisor.py:1988
  → supervisor_node(protocol=)         app/agents/supervisor.py:283
  → use_tools = protocol != PROTOCOL_JSON_TEXT and _speaks_streaming(model)   :303
  → build_domain_graph(protocol=)      app/agents/supervisor.py:2036
  → domain_decide / process_answer / _domain_loop / _domain_turn           :503 / :853 / :1264 / :1367
```

形状常量：`SUPERVISOR_SHAPE`（`prompts.py:136`）、`DOMAIN_SHAPE`（`prompts.py:182`），由 `user_turn(..., shape=)`（`prompts.py:189`）拼进提示词。

解析器：`parse_supervisor`（`supervisor.py:217`）、`parse_domain`（`supervisor.py:235`）、`_model_text`（`supervisor.py:1862`），底层是 `decisions.extract_json_object`（`decisions.py:93`）。

### 2.2 `ModelClient` 的能力边界

`backend/app/integration/model_client.py:208-218`：

```python
class ModelClient(Protocol):
    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply: ...
    def stream(self, messages, *, tools: tuple[dict[str, Any], ...] = ()) -> Iterator[StreamChunk]: ...
    def close(self) -> None: ...
```

`ModelReply`（:163-175）只有 `text` 与 `raw`，**没有 tool_calls**。
**结论：原生 tool calling 只能走 `stream(tools=)`，`complete` 做不到。**

`_stream_parameters`（:341-351）在 `tools` 非空时只发 `{"tools": ...}` 并丢掉 `response_format`，所以 `LLM_RESPONSE_FORMAT=json_schema` 与 tools 不冲突。

### 2.3 `decide` 的两个独立 try（★本 spec 要修的 bug）

`supervisor.py:376` 的 `try:`（内层，:377-444）与 `supervisor.py:446` 的 `try:`（外层，:446-482）是**两条平级语句**，不是嵌套关系。

因此 `supervisor.py:428`/`:429` 在 `except ModelClientError` 处理器里 `raise UpstreamUnavailable(...)` / `raise upstream_app_error(error)` 时，异常直接冒出 `decide`，**永远进不了 :463 的 `except AppError` 溢出分支**。
后果：生产（工具路径）下 `assemble(aggressive=True)` 的上下文溢出重试是死代码，上游溢出直接变成 502。

同一处还有两个次要缺陷：
- `:439` 的 `else:` 只在 :377 的 try 体未抛异常时执行，所以走 :392-405 strict 重试的那一轮，`:441-444` 的 `narration` 事件被跳过。
- `:392-405` 的重试没有再包 `except StreamTruncated`/`except ModelClientError`，那条路径上的异常裸穿。

（本 spec 删除 strict 重试后，后两条自动消失；第一条由 S-7 修。）

### 2.4 两个同名 `ToolCall`

| 类 | 位置 | 字段 | 谁在用 |
| --- | --- | --- | --- |
| `app.tools.protocol.ToolCall` | `app/tools/protocol.py:116` | `tool_name, arguments, call_id, idempotency_key` | `app/tools/__init__.py`、`app/tools/gateway.py` |
| `app.integration.model_client.ToolCall` | `app/integration/model_client.py:178` | `name, arguments` | `app/agents/supervisor.py:79-87`（`ToolCall` 在 `:86`）、`app/agents/decisions.py:16` |

两者不重名冲突（不同模块），但读代码时容易混。本 spec 不改名（改名要动网关与 12 个 card 工具），只在 S-8 的共享模块里统一从 `model_client` 导入。

## 3. S-1 删除协议常量与形状

`backend/app/agents/prompts.py`：

- 删 `:16 PROTOCOL_TOOL_CALLING`、`:17 PROTOCOL_JSON_TEXT`。
- 删 `:136 SUPERVISOR_SHAPE`、`:182 DOMAIN_SHAPE`。
- `user_turn(request, rule, shape=None, *, label="本轮用户消息")`（`:189`）→ `user_turn(request, rule, *, label="本轮用户消息")`，删掉拼 shape 的那两行。
- `SUPERVISOR_PROMPT`（:19-47）保留，**不改提示词**。注意 `:43` 的「结果里能看到 turn_status」在 T-10 落地前是不成立的：`card/workflow.py:95-113 finish()` 只写 `agent_id/status/summary`，从不写 `turn_status`，`_already_concluded` 因此永远判不出 card 已收尾（真机复现：一次「查余额」打了 4 次相同的 `card.cards`，`terminal_reason: max_turns`）。由 §12 **T-10** 把 card 补上，补完这句话自动成立。
- `DOMAIN_PROMPT`（:49-66）保留；`:62` 关于「提问工具」的措辞保留（`ask_user` 仍是协议工具）。

## 4. S-2 主 Agent：工具路径无条件

`backend/app/agents/supervisor.py`：

- `supervisor_node(model, domains, assembler=None, turn=None, *, protocol=PROTOCOL_JSON_TEXT)`（`:283`）→ 删 `protocol` 参数。
- 删 `:303 use_tools = protocol != PROTOCOL_JSON_TEXT and _speaks_streaming(model)`。
- 删 `:257 _speaks_streaming`（不再需要判断模型有没有流式半边：`ModelClient` 协议要求 `stream` 存在，缺了就该炸）。
- `decide` 里 `:376 if use_tools and FEATURE_TOOLS not in unavailable(key):` 的条件删掉，工具路径变成无条件执行（细节见 S-7 的新结构）。
- `_stream_decision`（`:1824`）删 `strict` 参数（没有 strict 重试了）。

## 5. S-3 主 Agent：删掉 JSON 文本回退

`backend/app/agents/supervisor.py`：

- 删 `:217 parse_supervisor`、`:235 parse_domain`、`:1862 _model_text`。
- `decide` 里 `:447-455` 的 `if decision is None: outcome = parse_supervisor(_model_text(...))` 整块删除；`decision` 只可能来自 `_stream_decision`。
- `supervisor_user_turn(request, menu, awaiting=None, *, protocol=PROTOCOL_TOOL_CALLING)`（`:1610`）→ `supervisor_user_turn(request, menu, awaiting=None)`；删 `:1621` 的 `shape` 分支。
- `app/agents/decisions.py`：删 `:111 as_text`、`:119 as_fields`、`:127 as_arguments`；**保留 `:93 extract_json_object`**——`backend/app/context/summary.py:41`(import)/`:258`(调用) 还在用它（对话压缩器走 `llm_response_format`，§14 明确不动），它不是协议开关的一部分。

**连带（2026-10-12 更正，审查 I-6）**：`extract_json_object` **保留**（见上一行），所以 `backend/app/modules/wealth/llm/decisions.py:39`(import) 与 `:170`(调用) 在 spec1 单独落地时**不会失败** —— 它随 spec2 的 W-2 自己消失（wealth 改用 `ToolCall`）。因此 **spec1 与 spec2 之间没有编译级耦合，可以分开落地**；`backend/app/context/summary.py:41`/`:258` 是保留路径，不受影响。

## 6. S-4 领域 Agent：删掉 `protocol` 参数与回退

`backend/app/agents/supervisor.py`：

- `domain_decide(model, spec, context=None, assembler=None, turn=None, *, protocol=PROTOCOL_JSON_TEXT)`（`:496`）→ 删 `protocol`。
- `process_answer(...)`（`:846`）→ 删 `protocol`。
- `_domain_loop(spec, model, state, thread, *, rounds, writer, protocol)`（def 在 `:1304`，不是 `:1264`）→ 删 `protocol`。
- **`_seed_thread(...)`（def 在 `:1258`；`:1264 protocol: str = PROTOCOL_JSON_TEXT`；`:1274` 传给 `domain_user_turn(..., protocol=protocol)`；调用点 `:534`、`:890-891`）→ 删形参与两处实参**。漏掉它，`domain_user_turn` 去掉 `protocol` 后会 TypeError，且 `:1264` 的字面量会让 T-11 的 grep 不空。
- `_domain_turn(model, spec, state, *, thread, protocol=PROTOCOL_JSON_TEXT)`（`:1361`）→ `_domain_turn(model, spec, state, *, thread)`，删 `:1396` 的 `if protocol == PROTOCOL_JSON_TEXT or not _speaks_streaming(model):` 分支与 `:1400`、`:1408` 两处 `model.complete(messages)` 回退。
- `domain_user_turn(ask, tools, *, protocol=PROTOCOL_JSON_TEXT)`（`:1629`）→ `domain_user_turn(ask, tools)`；删 `:1643` 的 `shape` 分支。
- `build_supervisor(..., protocol=)`（`:1988`）、`build_domain_graph(..., protocol=)`（`:2036`）→ 删 `protocol`。

**注意**：删掉 `:1396` 的分支后，`_domain_turn` 只剩 `_stream_calls(...)` 一条路；`_stream_calls` 本身在 S-8 被 `toolcalls.collect_stream` 取代。`:1394-1395` 的注释里也写着 `json_text`，**连注释一起删**，否则 T-11 的 grep 不空。另：`:452`/`:474` 有两处 `supervisor_user_turn(..., protocol=PROTOCOL_JSON_TEXT)` 实参，也要删。

## 7. S-5 card 图改用原生 tool calling

`backend/app/modules/card/workflow.py`：

- `:19 import _model_text`、`:21 import parse_domain` 删掉；改为从 `app.agents.toolcalls` 导入 `collect_stream`，从 `app.agents.decisions` 导入 `parse_domain_decision`，从 `app.agents.supervisor` 导入 `_domain_outcome`，从 `app.tools.schema` 导入 `ASK_USER_TOOL_NAME`、`ask_user_spec`、`function_schema`。
- `decide(state, *, answers=None)`（`:177`）里 `:184-194` 的提示词删掉「只输出一个 JSON 对象」与三形状示例，改成自然语言规则（工具名与参数由 schema 提供，不再塞 `json.dumps(menu)`）。
- `:199 outcome = parse_domain(_model_text(model, prompt, user_content))` 换成：

```python
    tools = [item for item in spec.handle.tools if item.name not in ("card.execute", "card.cancel")]
    schemas = tuple(function_schema(item) for item in tools) + (function_schema(ask_user_spec()),)
    messages = ({"role": "system", "content": prompt}, {"role": "user", "content": user_content})
    try:
        text, calls = collect_stream(model, messages, schemas)
    except StreamTruncated:
        return finish("模型输出中断，本次没有办理任何操作。", ToolStatus.FAILED)
    known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}
    outcome = _domain_outcome(parse_domain_decision(text, calls, known))
```

- `:200-210` 之后的三分支（`outcome.ask` / `outcome.wants_tool` / 兜底 `finish`）**不改**：`DomainOutcome` 是既有契约，`_domain_outcome`（`supervisor.py:1431`）负责把 `DomainDecision` 投影成它。
- `json` 若不再被本文件使用则删 `:5 import json`（`:186`、`:197` 的 `json.dumps` 一并删）。
- `card.execute`/`card.cancel` 仍然刻意不进 schema（`:182` 的过滤条件保留），只由 `:247`/`:252` 的代码路径调用。

**行为等价性**：`run_call`（`:120`）、`process`（`:212`）、`finish`（`:95`）不动，所以卡片选择、授权确认、幂等重试、权限等级全不变。变的只是"模型怎么把意图说清楚"。

### 7.1 S-5 附带：`ask_user` 的空字段兜底（2026-10-12 二次审查裁定）

**问题**：`ask_user` 的 `fields` 允许空数组——`app/tools/schema.py:103` 的 `ASK_USER_FIELDS` 只有 `{"type": "array", "items": {"type": "string"}}`，没有 `minItems`；`resolve_fields`（`app/agents/declaration.py:113-136`）对空列表与空白名字都返回 `[]`（`:124` 跳过空白名字）；`_awaiting`（`app/agents/supervisor.py:1022-1081`，`fields` 在 `:1049-1051` 解析；旧稿写的 `:883-942`/`:911` 是错的 —— `:911` 是 `except UpstreamUnavailable:` 里的注释行）于是把 `"fields": []` 写进 `pending_question`。前端按字段渲染输入框（`frontend/src/App.vue:947-970`），而 `interruptComplete`（`:642`）对空数组恒真 → 卡片一个输入框都没有、「提交」却可点，只能提交 `{}`；同时 `App.vue:108`/`:430` 在 pending 期间禁止发普通消息，用户被卡住。`_read_answer`（`supervisor.py:1134`）要求 `set(value.keys()) == {field["name"] ...}`，空集与 `{}` 相等，所以这次追问实际上什么也没问。

**裁定**（用户 2026-10-12，m02008「按你推荐的」）：**给这类提问补一个自由文本字段**。另两个方案不取：要求 `missing` 非空会否掉「问问卷某一题／自由提问」这一既有语义；前端补 textarea 要把「有没有字段」的判断搬进前端，而后端仍会写出一个无法回答的问题。

**实现**（`_awaiting` 是唯一创建问题的地方 —— 它自己的注释里就这么写着 —— 所以兜底放在这里，card 与 wealth 一起受益）：

```python
# backend/app/agents/declaration.py（紧挨 :42 的 FALLBACK_FIELD）
FREE_TEXT_FIELD: PendingField = {
    "name": "answer",
    "label": "补充说明",
    "type": "text",
    "required": True,
    "options": [],
}

# backend/app/agents/supervisor.py:1049-1051（_awaiting 内）
fields = resolve_fields(spec.declaration, names) or [dict(FREE_TEXT_FIELD)]
```

- 名字取 `answer`：它不在任何领域的可追问字段里（wealth 是 `workflow/state.py:121-132` 的 `amount_minor`/`plan_amount_minor`/`product_id`/`account_id`/`holding_id`/`horizon_months`/`liquidity`），也不参与槽位归一化——它只作为自由文本出现在下一轮提示词的「已补充信息」里（card 的 `decide(state, answers=...)`、wealth B2 的 decide 同理）。
- `type: "text"` 落在前端认识的四个值里（`frontend/src/App.vue:966`），**前端零改动**。
- 空白名字（`"  "`）走的是同一条路（`resolve_fields:124` 跳过），同样被兜底覆盖。
- `reask` 分支（紧邻 `fields` 解析处）取的是已有问题的 `fields`（已是对象），不受影响。
- 答案信封的键集合 `{"answer"}` 与字段集合相等，`_read_answer` 放行。

## 8. S-6 上游不支持 tools 时硬失败

删 `backend/app/agents/degradation.py`（`:16 DEGRADED`、`:19 provider_key`、`:43 unavailable`、`:51 mark_unavailable`、`:59 reset_unavailable`）。

**同时必须删干净它的引用**，否则 `ImportError` 或 ruff F401 让 §13 验收失败：
- `backend/app/agents/supervisor.py:39 from app.agents.degradation import mark_unavailable, provider_key, unavailable`；
- 使用点 `:299 key = provider_key(model)`（连它上面 4 行注释）、`:376` 的 `unavailable(key)` 条件、`:385`/`:1813`/`:1820` 的 `mark_unavailable`、`:1405 provider_key(model)`；
- `backend/app/agents/supervisor.py:31-34` 的 import 头里的 `as_text`/`as_fields`/`as_arguments` 要删、`extract_json_object` 要留（使用点 `:223`/`:232`/`:244`/`:245`/`:252`）；
- `backend/tests/test_supervisor_streaming.py:15 from app.agents import degradation` 与 `:36-39` 的 autouse fixture `clean_degradation()`——不删整文件 collection error，15 个用例全废。

`backend/app/agents/upstream.py` 新增：

```python
def tools_unsupported_error(error: FeatureUnsupported) -> AppError:
    """The provider said it cannot do tool calls. There is no second protocol to fall back to."""
    return AppError(
        UPSTREAM_ERROR_CODE,
        f"当前模型接口不支持工具调用（{error.feature}），无法继续办理。请更换模型或接口。",
        502,
        retryable=False,
        details={"upstream_code": "LLM_TOOLS_UNSUPPORTED", "feature": error.feature},
    )
```

在 S-8 的共享收集器里捕获 `FeatureUnsupported` 并抛它。删除 `supervisor.py` 中：
- `:381-407 except FeatureUnsupported` 整块；
- `:1785 _unstrict`、`:1798 _stream_calls`（含 `:1812`、`:1819` 两个 `except FeatureUnsupported`）。

**后果（须知悉）**：provider 支持 tools 但不支持 `strict` 时也会失败。当前生产 provider（`deepseek-v4.1-flash` @ `https://llm.example.com/v1`）实测接受 `strict`，不受影响。若将来遇到不支持 strict 的 provider，需要用户显式裁定加回一个"只放宽 strict、不换协议"的开关。

## 9. S-7 上下文溢出重试回到工具路径

`backend/app/agents/supervisor.py` 的 `decide` 重排为：

```python
    def run(prompt_text: str, batch: _DeltaBatch) -> tuple[SupervisorDecision | None, dict[str, Any] | None]:
        """One decision attempt. Returns (decision, terminal-state); exactly one is not None."""
        try:
            decision = _stream_decision(
                model, prompt_text, request, menu, known, awaiting=awaiting, emit=batch.add
            )
        except StreamTruncated:
            if not batch.started:
                raise
            batch.flush()
            return None, _final_from(results, writer, turns, text=batch.text,
                                     terminal_reason=TERMINAL_ABORTED_STREAMING)
        except ModelClientError as error:
            if not batch.started:
                if error.retryable:
                    raise UpstreamUnavailable(error.code) from error
                raise upstream_app_error(error) from error
            batch.flush()
            return None, _final_from(results, writer, turns, text=batch.text,
                                     terminal_reason=TERMINAL_API_ERROR, error_status=error.status)
        batch.flush()
        if decision.narration:
            writer({"event": "narration", "text": decision.narration})
        return decision, None

    batch = _DeltaBatch(writer)
    try:
        decision, terminal = run(prompt, batch)
    except AppError as error:
        if assembler is None or error.details.get("upstream_code") != OVERFLOW_UPSTREAM_CODE:
            if any(item.get("turn_status") for item in results):
                return _final_from(results, writer, turns)
            raise
        view = assemble(state, results, aggressive=True)
        batch = _DeltaBatch(writer)
        try:
            decision, terminal = run(prompt_for(state, results, view), batch)
        except AppError as again:
            if again.details.get("upstream_code") != OVERFLOW_UPSTREAM_CODE:
                raise
            raise context_limit_error(REASON_OVER_BUDGET) from None
    if terminal is not None:
        return terminal
```

要点：
1. `run` 的 `except AppError` 现在是**外层 try 的直接子句**，`:428/:429` 抛的 `AppError` 会被它接住 → 溢出重试在工具路径上生效（R-4）。
2. `run` 返回二元组而不是"有时返回 dict 有时返回 dataclass"，避免 `isinstance` 判别。
3. `narration` 的写入移到 `run` 内部、异常处理之外，因此不再被 strict 重试跳过（strict 重试已删，这一条是防御性写法）。
4. `batch` 在重试前重建：第一轮的半截文本已经 flush 给前端，不能和第二轮混在一个批次里。
5. **`queued` 的处置不能丢（审查 I-5）**：伪码只画到 `terminal`/`decision`，`if terminal is not None: return terminal` 之后必须照基线 `supervisor.py:456`/`:459-462`/`:482`/`:489-491` 原样接上：`outcome = _outcome_from(decision)` → 若 `outcome.agent_id is not None and _already_concluded(results, outcome.agent_id)` 则 `outcome = SupervisorOutcome(agent_id=None, reply=_closing_reply(results)); queued = []` → `agent_id is None` 时写 final 并返回，否则 `_dispatch(outcome, queued, turns)`。`queued` 仍取自 `state["pending_handoffs"]`（`:317`），**溢出重试不重置它**（它属于本轮，不属于某一次尝试）；重试只重置 `batch`。

`_outcome_from(decision)`、`_already_concluded`、`_dispatch`、`_final_from` 的调用顺序不变。

## 10. S-8 抽出共享的流式工具调用收集器

新建 `backend/app/agents/toolcalls.py`，从 `supervisor.py` 搬入（保留实现，只改可见性）：

| 新名字 | 来源 | 说明 |
| --- | --- | --- |
| `StreamTruncated(DecisionError)` | `supervisor.py:208` | 流没有 finish 就结束 |
| `start_stream(model, messages, tools)` | `:1647 _start_stream` | 只对 `next(stream)` 的失败重试一次 |
| `absorb(chunk, text_parts, names, arguments, emit=None)` | `:1717 _absorb` | 按 `tool_index` 分流 |
| `arguments_or_error(parts)` | `:1741 _arguments_or_error` | `json.loads` 失败 → `DecisionError` |
| `collect_stream(model, messages, tools, *, emit=None)` | `:1756 _collect_stream` | 返回 `(text, list[ToolCall])`；**first 与 rest 都检查 `truncated`** |

**不搬**：`_unstrict`（`:1785`，删）、`_stream_calls`（`:1798`，删——它比 `collect_stream` 只多一层降级）、`_DeltaBatch`（`:1673`，留在 `supervisor.py`：回复增量是主 Agent 的关切）。

`collect_stream` 里新增 `except FeatureUnsupported` → `raise tools_unsupported_error(error) from error`（S-6）。

调用方：
- `supervisor.py:_stream_decision`（`:1824`）→ `collect_stream(model, messages, (handoff_tool(menu),), emit=emit)`。
- `card/workflow.py:decide`（S-5）。
- spec2 的 wealth `decide_node`。

**搬走之后的 import 来源（审查 C-5）**：`StreamTruncated` / `start_stream` / `absorb` / `arguments_or_error` / `collect_stream` 一律从 `app.agents.toolcalls` 取。`supervisor.py`（`_stream_decision`）、`backend/app/modules/card/workflow.py`、`backend/app/modules/wealth/workflow/nodes.py` 三处都**不得**再 `from app.agents.supervisor import StreamTruncated` —— 本 spec 落地后那里已经没有这个名字（ImportError）。

`handoff_tool(agents, *, strict=True)`（`prompts.py:143`）保留 `strict` 形参（纯构造器，被测试直接调用），但生产代码不再传 `strict=False`。

## 11. S-9 配置面与文档

`backend/app/core/config.py`：
- 删 `:40-44` 的注释与 `llm_agent_protocol`。
- 删 `:45-46 llm_supervisor_protocol`（弃用别名）与 `:48-52 @model_validator adopt_legacy_protocol`。
- **保留** `:39 llm_response_format`：`complete` 仍被对话压缩器（`app/context/summary.py:255-257`）、设置页探测（`app/integration/llm_config.py:212`）、审计装饰器（`app/audit/model_client.py:84`）使用。

`backend/app/api/schemas.py:94-103`：删 `LlmConfigRequest.llm_supervisor_protocol`（`:99-102` 的注释一并删）。

`backend/app/integration/llm_config.py:154`：删 `candidate.pop("llm_supervisor_protocol", None)` 与其上 4 行注释。

`backend/app/runtime.py`：删 `:22 import PROTOCOL_JSON_TEXT`、`:73` 的 `protocol` 形参默认值、`:99 protocol=protocol`、`:137 protocol=self.settings.llm_agent_protocol`。

`backend/app/agents/domain.py`：删 `:14 import`、`:63` 的 `protocol` 形参、`:81 protocol=protocol`。

`backend/.env.example:23-25`：删 `LLM_AGENT_PROTOCOL` 相关三行。

**向后兼容**：`backend/.env` 里若残留 `LLM_AGENT_PROTOCOL`/`LLM_SUPERVISOR_PROTOCOL`，pydantic-settings 默认 `extra="ignore"` 会静默忽略。落地前请实跑一次 `uv run python -c "from app.core.config import Settings; Settings()"` 确认不报 `extra_forbidden`（若 `model_config` 设了 `extra="forbid"` 则需保留一个 `**extra: Any` 吞掉，或删掉 `.env` 里那两行）。

## 12. 测试面

### 删除（随协议一起消失）

| 文件 | 内容 |
| --- | --- |
| `backend/tests/test_supervisor_decision.py` | 整文件测 `parse_supervisor`（`prompts.py:189 user_turn` 的 shape 分支） |
| `backend/tests/test_supervisor_protocol.py` | **不是整文件删**：12 个 def 里只删 7 个降级/开关用例——`:67 test_the_settings_default_is_the_new_protocol`、`:73 test_the_graph_defaults_to_the_old_protocol`、`:138 test_a_provider_without_tools_falls_back_to_json_text`、`:152 test_the_degradation_is_remembered_for_the_rest_of_the_process`、`:162 test_a_provider_without_strict_retries_without_strict`、`:176 test_the_static_switch_skips_the_streamed_path_entirely`、`:185 test_a_complete_only_model_degrades_to_json_text_under_the_tool_protocol`。**保留**（去掉 protocol 实参后仍有效）：`:79` 流式带 handoff、`:96` 直接回答、`:105` 第二个 handoff、`:134` 新请求无队列、`:206` 首块失败只重试一次 |
| `backend/tests/test_supervisor_handoff.py` | 只删 `:143-148 test_the_json_text_turn_still_carries_the_shape`。`:135-139 test_the_tool_calling_turn_carries_no_shape_paragraph` **保留并改写**（`:136` 去掉 `protocol=PROTOCOL_TOOL_CALLING`；断言 `HANDOFF_TOOL_NAME in turn` / `"请只输出一个符合此结构的 JSON 对象" not in turn` / `"JSON" not in turn` 仍成立）。**断言必须换字面量**：`:91` 已删 `prompts.py:136 SUPERVISOR_SHAPE`，测试里再引用这个名字就是 ruff F821 + NameError；同时清 `:15`/`:16`/`:18` 的协议常量与 `SUPERVISOR_SHAPE` import |
| `backend/tests/test_domain_protocol.py` | `:231`/`:258`/`:269`/`:286`/`:319-333` 中的 `json_text` 分支；`:319 test_the_json_text_prompt_still_carries_the_shape` 删；`:328 test_the_tool_calling_prompt_carries_no_shape` **保留并改写**成「提示词不含任何 JSON 形状」（原稿把它写成 `..._drops_the_shape`，该符号不存在） |
| `backend/tests/test_llm_settings.py:112` | 整函数 `test_the_panel_request_exposes_the_supervisor_protocol_switch`（def 在 `:112`，docstring `:113-120`）——面板的 `llm_supervisor_protocol` 字段 |
| `backend/tests/support/domain_harness.py:23,128` | `protocol` 透传 |

### 改写

| 文件 | 改法 |
| --- | --- |
| `backend/tests/test_domain_loop.py` | import `:14` 与 9 处实参（`:71`/`:88`/`:103`/`:122`/`:133`/`:149`/`:162`/`:194`/`:214`）里的 `PROTOCOL_TOOL_CALLING` 全部删掉（`_domain_loop` 不再收 `protocol`） |
| `backend/tests/test_supervisor_streaming.py:15,17,35-39,42` | 删 `:15 from app.agents import degradation`、`:17 PROTOCOL_TOOL_CALLING`、`:36-39` 的 `clean_degradation` fixture、`:42` 的 `protocol=` 形参 |
| `backend/tests/test_supervisor_protocol.py:17`、`:45`、`:47` | `:17` 的 `from app.agents.prompts import PROTOCOL_JSON_TEXT, PROTOCOL_TOOL_CALLING` 整行删（F401）；`:45 def compiled(model, *, protocol=PROTOCOL_TOOL_CALLING, roster=DOMAIN_ROSTER)` 去掉 `protocol` 形参、`:47 build_agent_graph(..., protocol=protocol)` 去掉实参。**漏掉这两处，删除表里「保留」的 5 个用例（`:79`/`:96`/`:105`/`:134`/`:206`）全部 TypeError** |
| `backend/tests/test_domain_protocol.py:17-19` | import 清单去掉协议常量 |
| `backend/tests/test_write_safety.py:39` | 注释里的协议名改掉 |
| `backend/tests/support/scripted_model.py` | **所有充当"已配置模型"的替身必须实现 `stream(messages, *, tools=())`**；刻意缺 `stream` 的替身（用于测降级）删除。工作量别低估：`ScriptedModel:16` 今天只有 `complete`，被 20+ 文件使用（`test_agent_runtime.py`/`test_card_module.py`/`test_domain_*.py`/`test_write_safety.py`/`test_error_codes.py` 等），这些文件的脚本都要从「回 JSON 文本」改成「回 tool call」。另 `StreamingModel:78` 只记录 `self.tools`（`:97`），**没有 `self.streams`**，T-1/T-3/T-7 要断言调用次数得先给它加计数 |

### 新增

| # | 用例 | 断言 |
| --- | --- | --- |
| T-1 | 主 Agent 的模型调用永远带 tools | 替身记录每次 `stream` 的 `tools`，断言每次都非空且含 `handoff` |
| T-2 | 上游拒绝 tools → 502 且可读 | `AppError`，`details["upstream_code"] == "LLM_TOOLS_UNSUPPORTED"`，`retryable is False`，消息含「不支持工具调用」 |
| T-3 | 上游拒绝 strict 也报错，不重试 | 替身只拒 strict → 同样 502；调用次数用替身自己的计数器（`StreamingModel` 今天没有 `self.streams`，见改写表） |
| T-4 | 溢出重试在工具路径生效 | 第一次 `stream` 抛 `LLM_CONTEXT_OVERFLOW`，第二次成功 → 正常 final；断言 `assemble` 收到 `aggressive=True` |
| T-5 | 两次都溢出 → `context_limit_error` | `AppError` 且 code 为 `REASON_OVER_BUDGET` 对应值 |
| T-6 | 溢出但已有结论 → 用已有结论收尾 | 与 `:464-466` 的既有语义一致 |
| T-7 | 首 delta 之前失败 → 502 | 用**非 retryable** 的上游错误（`_start_stream` 对 retryable 错误本来就重试一次，见 `:1647-1670`），断言调用 1 次；若用 retryable 错误则应断言 2 次 |
| T-8 | 首 delta 之后失败 → `terminal_reason="api_error"` + 已见文本 | 既有用例保持 |
| T-9 | card 的提示词不含 JSON 形状 | `"只输出一个 JSON 对象" not in prompt`；`"{\"tool\"" not in prompt` |
| T-10 | card 的 `subtask_end` 带 `turn_status` | 修 §12 的错配：`card/workflow.py:95-113 finish()` 增加 `turn_status` 字段并透传（对齐 `supervisor.py:667 _conclude_turn`） |
| T-11 | 全仓无 `json_text` | 在仓库根跑 `rg -nE "json_text|PROTOCOL_JSON_TEXT" backend/app` 为空（**含 `supervisor.py:1394-1395` 的注释**）。本机 PATH 上只有 `rg.exe`、**没有 `grep`**；`rg` 默认扩展正则，`|` 直接生效 |
| T-12 | `app.agents.toolcalls` 是唯一收集器 | `rg -n "_collect_stream|_stream_calls" backend/app` 为空 |
| T-13 | `ask_user` 的空字段兜底 | `_awaiting(spec, DomainOutcome(ask={"fields": [], "display": "请补充说明"}), writer)` 产出的 `pending_question["fields"]` 恰一项且等于 `{"name": "answer", "label": "补充说明", "type": "text", "required": True, "options": []}`；空白名字 `["  "]` 同样兜底；随后 `_read_answer({"question_id": <同一个>, "value": {"answer": "随便"}}, question)` 不报错 |

**T-10 的背景**：`SUPERVISOR_PROMPT:43` 承诺「结果里能看到 turn_status」，但 `card/workflow.py:95-113` 的 `finish()` 只写 `agent_id/summary/instruction/business_ids`，从不写 `turn_status`。真机复现：一次「查余额」打了 4 次完全相同的 `card.cards`，`terminal_reason: "max_turns"`。这是合并后才暴露的错配，属于本 spec 的收尾项（否则 R-1 落地后该错配仍在）。

## 13. 验收

1. `cd backend && uv run ruff check app tests scripts` → exit 0
2. `cd backend && uv run ruff format --check app tests scripts` → exit 0
3. `cd backend && uv run pytest -q tests/test_domain_loop.py tests/test_domain_protocol.py tests/test_supervisor_streaming.py tests/test_domain_agents.py tests/test_domain_harness.py tests/test_cross_domain_orchestration.py tests/test_multi_agent_api.py tests/test_contracts.py tests/test_business_ids.py tests/test_domain_turn.py tests/test_turn_wiring.py tests/test_write_safety.py tests/test_card_module.py` → 全绿（`tests/test_card_module.py` 的收集条数保持 14）
   （**不要跑全量**：`tests/conftest.py:112-140` 的 `rt` fixture 每个用例都新建私有 schema + `alembic upgrade head` 打云库，约 8s/用例，1513 个用例要 3 小时以上。定向跑受影响模块即可。）
4. `cd frontend && pnpm exec vue-tsc --noEmit` → exit 0；`pnpm test` → 全绿（本 spec 不动前端，跑一次确认没连带）
5. 真机冒烟（`pwsh -File start.ps1 -Restart` 后）：`POST /api/session` 取 `session_id` → `POST /api/chat/messages` 带 `Idempotency-Key` → 断言 SSE 出现 `agent_start`/`tool_call`/`tool_result`/`subtask_end`/`reply_delta`/`final`，且 `final.terminal_reason` 不是 `max_turns`
6. `backend/app/agents/degradation.py` 文件不存在

## 14. 明确不做

- 不改 `app.tools.protocol.ToolCall` 与 `app.integration.model_client.ToolCall` 的重名（S-8 只统一导入来源）。
- 不改 `ModelClient.complete` 的签名（`complete` 无 tools，这是协议现状；要动得先让 provider 侧统一）。
- 不动 wealth 的决策层（spec2）与写流程（spec3）。
- 不动四个空壳领域（transfer / bill / subscription / life_event）。
- 不动前端（`reply_delta`/`narration`/`terminal_reason` 事件契约不变）。
- 不删 `llm_response_format`（对话压缩器与设置页探测仍用 `complete`）。
- 不把 `_domain_loop` 从死代码变成活代码（card 走 `graph_builder`、wealth 走 `turn`，通用 loop 仍只被合成模块测试覆盖）。

## 15. 风险

| 风险 | 缓解 |
| --- | --- |
| 删除 strict 放宽后，换 provider 即不可用 | 当前 provider 实测支持 strict；§8 已写清后果与回退方案 |
| card 改协议后行为漂移 | card 既有 32 个用例 + T-9/T-10；`run_call`/`process`/`finish` 不动 |
| `extract_json_object` 被 wealth 引用 | 它**不删**（§5），spec1 单独落地不会 import 失败；wealth 侧的 import/调用随 spec2 的 W-2 自己消失，两 spec 无编译级耦合 |
| `Settings` 对未知 env 键的处理未知 | §11 的向后兼容一条要求实跑确认 |
| 测试替身普遍缺 `stream` | §12 改写表已列为必做项 |

## 16. 裁定（已定，2026-10-12）

用户 2026-10-12 经选项确认：

1. **接受删除 `strict` 放宽**：上游拒绝 `strict` 时直接失败（§8 的做法），不保留"只放宽 strict、不换协议"的开关，
   也不恢复 `degradation.py` 的进程级缓存。§8 的"后果须知悉"（第 178 行）继续有效：
   若将来遇到不支持 `strict` 的 provider，再单独裁定加回开关。
2. **T-10（card 补 `turn_status`）纳入本 spec**：S-5 把 card 改成原生 tool calling 时，
   一并修掉 `app/modules/card/workflow.py:95-113` 的 `finish()` 不发 `turn_status` 导致的重复派单空转
   （`supervisor.py:1922` 的 `_already_concluded` 判据）；验收用例见 spec3 §5。
3. **`ask_user` 空字段兜底纳入本 spec**（2026-10-12 二次审查裁定，用户 m02008「按你推荐的」）：§7.1 给空字段的提问兜底一个自由文本字段；
   验收用例 T-13，wealth 侧连带用例见 spec2 §12.3 T-17。

## 17. 执行顺序

1. S-8（先抽共享收集器，`supervisor.py` 与 card 都改导入）
2. S-1 / S-3 / S-4（删协议常量与回退，此时 `json_text` 已无人使用）
3. S-2（工具路径无条件）→ S-6（硬失败）→ S-7（溢出重试）
4. S-5（card 改协议）与 §7.1 的 `ask_user` 空字段兜底
5. S-9（配置面）
6. 测试面（删/改/增）
7. §13 验收

**与 spec2 的交界**：spec1 删的是 `parse_supervisor`/`parse_domain`/`_model_text`/`as_text`/`as_fields`/`as_arguments`，**不删 `extract_json_object`**，所以两 spec 之间**没有编译级耦合**，可以分开落地（索引 §1 的顺序仍让理财段先走）。`wealth/llm/decisions.py:39`/`:170` 对 `extract_json_object` 的使用由 spec2 的 W-2 自己清掉。
