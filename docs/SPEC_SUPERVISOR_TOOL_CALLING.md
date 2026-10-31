# Spec：对齐 Claude Code 形态——Supervisor Tool Calling、直接回复流式、领域 Agent 自循环（路线图）

- 状态：**待评审**（未动笔实现）
- 版本：**v5**（v5 = 范围收窄：只交付底座，见下；v4 = 评审修复；v3 依据 2026-10-10 产品拍板立起终态形态：主 Agent 不持业务工具、领域 Agent 自循环报告，新增 §1.5 与 §4.8，P4/P5 纳入本路线图；v2 依据真实上游实测 + Anthropic/Claude Code 官方文档逐条校正，改动集中在 §1.4、§4.1-§4.6、§10；v1 基于文献推演）
- 关联文档：[ARCHITECTURE.md](ARCHITECTURE.md)、[MAIN_AGENT_PROMPTS.md](MAIN_AGENT_PROMPTS.md)
- 目标读者：底座后端 / 前端 / 合规审计
- **v4 评审修复**（对 v3 的更正，逐条附代码依据）：
  1. **§4.8 的前提不成立**：底座要改造的领域工具路径**今天对六个模块全部不可达**（五个模块不声明 `tools`，wealth 走 `spec.turn` 分支），v3 的「模块零改动 / 底座直接消费既有 `ToolSpec` 声明」必须改写（§4.8 分工边界、§4.8.1、§4.8.2、§8 P4）。
  2. **§4.8.3 的循环伪代码违反重放不变量**：节点必须按「是否碰模型」切分（[backend/app/agents/supervisor.py:295-297](backend/app/agents/supervisor.py#L295-L297)、[backend/app/agents/supervisor.py:1052-1064](backend/app/agents/supervisor.py#L1052-L1064)），循环体节点不 `interrupt`、`wait` 节点不调模型，`process_answer` 需从 `domain_thread` **续跑**（§4.8.3）。
  3. **§10.1 并入 §10 第 9 行**：多领域派发已由产品拍板（2026-10-10）；§10.1 改为真正待拍板的那条（P4 的入口）。
  4. 补齐三处未定义交互：`ask_user` 注入与工具门判定（§4.8.2）、`strict: true` 与模块 schema 的兼容（§4.8.2）、追问是否消耗工具轮次（§4.8.3）。
  5. 更正「Supervisor 纯派发」的过强表述（§1.5、G8、N6、§10 #6）：它不持有**业务**工具，但仍可直接回答与产出旁白。
  6. 修正 §8 P4 对不存在的「§9.1」的引用。
  7. §7.1 成本表述精确化：P5 消除的是**工具步之间的 Supervisor 路由**，不是领域内每步的模型调用。
  8. **审计加列已拍板**（2026-10-10）：`llm_call_audit` 增 `stream` 列，§10 增第 10 行，§9 第 3 条改写，§5 第 1 条从「无数据库迁移」更正为「仅一处迁移」。

- **v5 范围拍板（2026-10-10）**：
  1. **本 spec 只交付底座**。三条验收主线 = 正确派发子 Agent、流式、协议层调用工具与文本输出（§2.1）。
  2. **P4/P5 只交付地基**，不含任何领域侧业务改造（§10.1）；领域 Agent 的内部循环（记忆策略、终答不变量、业务语义）由领域团队按 §4.8.4 的契约自行开发（N7）。
  3. 废除的 `docs/superpowers/specs/2026-10-10-wealth-model-driven-loop-spec.md` **一项素材都不并入**，需要时从 git 提交 `b907542` 取回。
  4. 代价如实记录：P4/P5 交付后**无领域受益**（§8、§9 第 5 条）——这是范围拍板的结果，不是缺陷。

---

## 1. 背景与问题

### 1.1 现状

主 Agent（Supervisor）每一轮只做一件事：**决定「自己直接回答」还是「把活派给某个领域 Agent」**。这个决定目前编码在一段 JSON 文本里：

- 提示词 [backend/app/agents/prompts.py:13-44](backend/app/agents/prompts.py#L13-L44) 里的 `SUPERVISOR_PROMPT`，第 30 行是硬指令：
  > 只输出一个 JSON 对象，不要输出其他文字。
- 两种输出形状 [backend/app/agents/prompts.py:141-145](backend/app/agents/prompts.py#L141-L145) 的 `SUPERVISOR_SHAPE`：
  ```json
  {"agent": "<agent_id>", "instruction": "<给该 Agent 的执行指令>"}
  {"agent": "", "reply": "<直接回复用户的话>"}
  ```
- 解析靠启发式扫描：[backend/app/agents/decisions.py:18-33](backend/app/agents/decisions.py#L18-L33) 的 `extract_json_object`，再由 [backend/app/agents/supervisor.py:131-146](backend/app/agents/supervisor.py#L131-L146) 的 `parse_supervisor` 校验。
- 发消息在 [backend/app/agents/supervisor.py:908-917](backend/app/agents/supervisor.py#L908-L917) 的 `_model_text`，最终落到 [backend/app/integration/model_client.py:172-181](backend/app/integration/model_client.py#L172-L181) 的 `ChatCompletionsModelClient.complete`——**非流式、一次拿完整文本**。
- 用户可见的那句话，是在决定做完之后由 [backend/app/agents/supervisor.py:273-286](backend/app/agents/supervisor.py#L273-L286) 一次性 `writer({"event": "final", "text": outcome.reply, ...})` 推出去的。

### 1.2 由此产生的三个问题

**问题 A：拿不到流式。**
`reply` 是 JSON 对象里的一个字段。JSON 必须收完才能解析，所以逐 token 流出只会先露出半个 `{"agent": "", "reply": "您\n`。`docs/ARCHITECTURE.md:339` 当初拒绝逐 token 流式，理由正是这一条——**这个理由成立的前提，就是「文本装在 JSON 里面」**。

**问题 B：解析会出错。**
`extract_json_object` 是启发式的：它要在模型输出里找第一个平衡的 `{...}`。模型加一句解释、加个 ```json 围栏、输出两个对象、或者被 `timeout_seconds` 截断，都会让这层解析失败，而失败的表现是用户收到 [backend/app/agents/supervisor.py:92](backend/app/agents/supervisor.py#L92) 的 `NO_REPLY_FALLBACK`（"抱歉，我没能理解这个请求……"）——一个**本该由模型正确完成的任务，因为传输格式而失败**。

**问题 C：默认配置根本没启用结构约束。**
[backend/app/integration/model_client.py:196-205](backend/app/integration/model_client.py#L196-L205) 的 `_response_format` 只在配置为 `"json_object"` 时发 `{"response_format": {"type": "json_object"}}`；而默认值是 `"json_schema"`（[backend/app/integration/model_config.py:40](backend/app/integration/model_config.py#L40)、[backend/app/core/config.py:39](backend/app/core/config.py#L39)、[backend/app/api/schemas.py:78](backend/app/api/schemas.py#L78)、[backend/.env.example:22](backend/.env.example#L22)），**这个取值下不发任何字段**。已有的测试把这件事写成了契约：[backend/tests/test_llm_settings.py:97-108](backend/tests/test_llm_settings.py#L97-L108) 的用例名就是 `test_json_schema_mode_publishes_no_schema_because_there_is_none`，注释是 "The agents parse JSON out of the reply themselves, so no schema is imposed."

也就是说：**约束形状的责任完全在提示词和启发式解析上，协议层一点忙都没帮。**

### 1.3 主流做法

主流 agent 工具（OpenAI function calling、Claude Code、Codex）不在正文里要求模型「写 JSON」，而是把结构约束下沉到协议层，并**把一次回答拆成两个通道**：

| 通道 | 载体 | 给谁看 |
| --- | --- | --- |
| 自然语言 | `text_delta` / `content_block_delta` | 用户 |
| 结构化决策 | `tool_call` 的 `arguments`（OpenAI `functionCallArgumentsDelta` / Anthropic `input_json_delta`） | 程序 |

Anthropic 文档对此的原文是："the deltas are *partial JSON strings*, whereas the final `tool_use.input` is always an *object*"。**结构化参数在流式期间是半截 JSON，但它是程序通道，用户永远看不到；用户通道是纯文本，可以逐字流。** 两件事天然不冲突。

关键结论（本 spec 的立足点）：

> **JSON 与流式不互斥。真正的分界线不在「用不用 tool call」，而在「路由决策本身用不用 tool call 表达」。**
>
> 模型要派活 → 输出一个 `handoff(agent_id, instruction)` tool call（参数在内部累积，不展示）；
> 模型要直接回答 → 输出纯文本（逐字流出）。
>
> 如果只是把现有 JSON 原样塞进某个 tool 的 `parameters` 里，`reply` 仍然是参数中的一个字段，仍然要等参数收完——**体验零变化**。

### 1.4 实测：上游能力的真实边界

用 [backend/.env](backend/.env) 里的真实凭据（`LLM_BASE_URL` / `LLM_MODEL`）跑了 13 个探测用例（脚本 `_scratch/probes/backend/.probe_tools.py`、报告 `_scratch/probes/backend/.probe_report.json`，均为临时文件，已归档到 `_scratch/`）。结论直接决定本 spec 的若干取舍：

| 能力 | 结论 |
| --- | --- |
| `tools`（function calling） | ✅ 支持。派活时 `finish_reason="tool_calls"`、`content=""`、返回 `tool_calls` 数组 |
| `stream` + `tools` | ✅ 支持。参数以 **partial JSON** 增量到达（单次 34 / 61 / 143 个 chunk） |
| `strict: true` | ✅ 被接受，无 400 |
| 双通道 | ✅ 确认。派活 → `tool_calls`；闲聊 → `finish_reason="stop"` + 纯文本 + `tool_calls=null` |
| `tool_choice` | ❌ **不被遵守**。`"required"` 与具名工具都被忽略，模型仍返回纯文本 `finish_reason="stop"` |
| `strict` 的 `enum` | ⚠️ 是「替换」不是「拒绝」：把 `enum` 收窄成 `["transfer"]` 后问账单问题，模型返回 `agent_id="transfer"`（语义错误），而不是报错 |
| `response_format` + `tools` 同发 | ⚠️ HTTP 200，不报错，`tools` 生效（**不是**硬 400） |
| 第三个通道 | ⚠️ 模型是思考模型，**先流 `reasoning_content`**（first_any ≈ 1.0–1.4s，first_text ≈ 1.1–2.1s） |
| 文本 + tool call 并存 | ⚠️ **会发生**：模型先输出 15 个 `content` chunk（"我来帮您处理这两件事……"）再输出两个 `tool_call` |
| 并行 tool call | ⚠️ **会发生**：一次返回两个 `handoff` |
| SSE 杂项 | ⚠️ 流里混有 `: heartbeat` 注释行（非 `data:` 行），任何自解析 SSE 的路径必须容忍 |

四条对本 spec 有直接影响的推论：

1. **`reasoning_content` 永不外发。** 这是银行场景的合规红线，也恰好是主流默认行为——Anthropic Agent SDK 把 `ThinkingBlock` 与 `TextBlock` 分开，Opus 4.7+ 的 API 默认 `display: "omitted"`。
2. **`tool_choice` 只能当它不存在。** 既然实测被忽略，就**不发送**这个字段（§4.2）。
3. **`strict` 只保证语法，不保证语义。** `enum` 约束的是采样，不是判断力。`parse_decision` 对 `known` 的校验**不能删**。
4. **流式改善的是「感知速率」，不是首字延迟。** 首字延迟由 reasoning 阶段主导（≈1s），本 spec 不承诺降低它。合批仍然必要（chunk 粒度 2–3 字符、突发到达、`gap_median=0ms`）。

Anthropic 官方文档对「文本与 tool call 并存」给的就是这个形状——[handle-tool-calls.md](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) 的标准响应示例里，`content` 数组**第一个块就是** `{"type": "text", "text": "I'll check the current weather in San Francisco for you."}`，紧随其后才是 `tool_use`。所以 §4.2 不再丢弃这段文本。

### 1.5 终态形态：对齐 Claude Code（v3 拍板）

Codex / Claude Code 的「agent」不是一个决策步骤，而是一个**循环**：模型 → 工具 → 结果回灌**同一上下文** → 模型继续，直到给出最终文本。v3 把 base 的终态明确为这个形态，方式不是塌缩层级，而是**把循环放回它该在的位置**——下放进领域子图：

| Claude Code | base 终态 | base 现状 |
| --- | --- | --- |
| 主 agent：可直接执行，也可派 Task 子 agent | **Supervisor：不持有业务工具，但仍可直接回答**（G8 / N6，显式决定） | 不持有业务工具；直接回复路径已存在（闲聊/澄清、`_closing_reply`），与 handoff 并存 |
| Task 子 agent：领 prompt，**自带工具循环**，跑完交报告，过程文本默认不转发 | **领域 Agent：领 instruction，工具结果回灌自身上下文连续多步，以 summary 报告交回 Supervisor**（§4.8） | 单发：一次激活 = 1 次模型调用 + ≤1 个工具，即回 Supervisor |
| 循环住在每个 agent 内部 | 循环下放进领域子图；**两级结构保留**（N3） | 循环被上提到 Supervisor 层：每一步工具迭代都要重新路由一次 |

由这个形态直接推出的三条决定：

1. **主 Agent 暂不持有工具，是方向选择不是缺口**（不持有**业务**工具 ≠ 不产出文本：直接回答与 `narration` 仍在它职责内）。** Claude Code 的主 agent 持有工具，因为它要直接改代码；base 的 Supervisor 面向「办银行业务」，动作全部长在领域上。将来若出现跨领域动作（如全局资产总览），另立 spec 给 Supervisor 加工具——届时自有工具与 handoff 并存，与 Claude Code 主 agent 同构（N6）。
2. **领域 Agent 自循环。** 一次 handoff 的 `instruction` 就是子任务的 prompt（等价于 Claude Code Task 工具的 `prompt` 参数）；工具结果不再「写 state → 回 Supervisor 重问」，而是作为 tool result 回灌领域自己的上下文，直到它给出 summary——summary 就是交回 Supervisor 的报告，等价于 Claude Code 子 agent 的 final message。
3. **两级结构保留（N3 不变）。** 循环下放改变的是「一次激活内做多少步」，不改变「谁对用户说话、谁裁定结束」。§7.1 的三条约束（领域隔离、可信身份、审计粒度）因此在 P5 里继续成立——循环发生在绑定调用方身份的领域子图**内部**。

这个终态同时消掉现状最大的结构成本：**一次工具调用 = 2 次模型调用**（一次 Supervisor 路由 + 一次领域决策）。循环下放后，一次派活 = 1 次 Supervisor 决策 + N 次领域内迭代（N = 任务实际需要的工具步数），**工具步之间的路由调用消失**。要精确：消除的是路由那一半——领域内每个工具步仍是一次模型调用，总量不归零（3 步业务从 7 次降到 5 次，见 §7.1）。

---

## 2. 目标与非目标

### 2.1 目标

**本 spec 的三条验收主线**（2026-10-10 范围拍板）——超出这三条的部分不是本 spec 的工作：

1. **能否正确派发子 Agent**：Supervisor 的路由决策由 `handoff` tool call 表达；一次请求可派给多个领域，逐个派发（G1、§4.2）。
2. **能否流式**：直接回答逐字到达前端，派活前有旁白（G2、§4.3、§4.5）。
3. **能否在协议层调用工具与输出文本**：`tools` 参数承载工具调用；文本与 tool call 在同一个 assistant 消息里并存；`reasoning` 通道永不外发（G1、G6、§4.1）。

1. **G1** Supervisor 的「派活 / 直接回答」决策改用 **function calling** 表达：派活 = 调用 `handoff` 工具；直接回答 = 输出纯文本、不调工具。
2. **G2** 直接回答的那段文本走 **SSE 流式**送达前端，逐字可见。
3. **G3** 在**上游不支持** `tools` / `stream` 时，自动降级回当前的 JSON 文本协议，**用户可见行为不变**（只是不流式）。
4. **G4** 全链路审计不破：流式调用同样产生一行 `llm_call_audit`，且「一次尝试一行记录」的既有契约不变。
5. **G5** 向后兼容：不认识新事件的客户端仍然能正常工作（仍能拿到完整 `final`）。
6. **G6**（P4）领域 Agent 的工具决策同样改用 function calling：工具 schema 由底座从 `ToolSpec` 生成，`parse_domain` 的启发式解析退役为降级路径（§4.8.2）。
7. **G7**（P5）领域 Agent 一次激活内自循环：工具结果回灌其自身上下文、连续多步，直到给出 summary 报告交回 Supervisor——一次派活不再为每个工具步多付一次路由（§4.8.3）。
8. **G8** Supervisor 在本路线图内不持有业务工具。它仍可直接回答（闲聊、澄清、`_closing_reply` 收尾），只是不再新增自有业务工具。这是对齐 Claude Code orchestrator 形态的显式决定（§1.5），不是未完成项。

### 2.2 非目标（本 spec 不做）

- **N1（限 P1–P3）** P1–P3 不动领域 Agent 的决策协议——它的输出（`tool` / `ask` / `summary`）**不面向用户**，没有流式价值。P4/P5（§4.8）把它改造成 tool calling + 自循环，是本路线图的后半程，也是领域侧 tool calling 的**唯一归属**——原 `docs/superpowers/specs/2026-10-10-wealth-model-driven-loop-spec.md` 已于 2026-10-10 废除（§4.8 前言）。P4/P5 **只交付地基**：协议、schema 生成、`ask_user`、预算、降级，不含任何领域侧业务改造（§10.1）。
- **N2** 不给领域 Agent 加「旁白」（即面向用户的中间文本）。这是独立的后续工作。（Supervisor 自己的旁白不在此列，见 §4.3 的 `narration`。主流默认也不转发子 agent 文本：Anthropic Agent SDK 的 `forward_subagent_text` 默认 `False`，原文 "Without this option, Claude Code omits the text and thinking blocks of a subagent that runs in the foreground."）
- **N3** 不把两级 agent（Supervisor → 领域子图）塌缩成一级循环。对齐 Claude Code 靠的是**把循环下放进领域子图**（§4.8），不是取消层级；理由见 §7.1。
- **N4** 不修改 `response_format` 配置项的语义（`json_schema` 取值当前什么都不发这个已知缺口，见 §9 待办，单独处理）。
- **N5** 不接入 Anthropic 原生 Messages API。base 的边界是 OpenAI 兼容的 Chat Completions（[backend/app/integration/model_config.py:34-46](backend/app/integration/model_config.py#L34-L46)），只走兼容层。
- **N6** 不给 Supervisor 增加自有业务工具。「主 agent 先不持有业务工具」是产品拍板（§1.5）：业务动作全部长在领域侧。**这不等于 Supervisor 不产出文本**——直接回答、旁白（§4.3 的 `narration`）与 `_closing_reply` 都在它职责内。将来若需要跨领域动作，另立 spec——届时 Supervisor 形态与 Claude Code 主 agent（自有工具 + Task 派发并存）同构。
- **N7** 不交付任何领域侧业务改造。领域 Agent 的内部循环——会话记录即记忆、终答不变量（真实性 / 不得声称未发生 / 不得泄露身份）、业务语义与问卷设计——**由领域团队自行开发**，底座只提供契约（§4.8.4）。这是 2026-10-10 的范围拍板：本 spec 只关注底座能否正确派发子 Agent、能否流式、能否在协议层调用工具并输出文本。

---

## 3. 术语

| 术语 | 含义 |
| --- | --- |
| **协议（protocol）** | Supervisor 决策的编码方式。两个取值：`tool_calling`（新）、`json_text`（现有，降级用）。 |
| **handoff** | 新的 function 名。调用它 = 「把活派给某领域 Agent」。 |
| **delta** | 流式增量文本。 |
| **首 delta（first delta）** | 一轮回复中第一个已推送给前端的 delta 事件。它是重试与错误处理的**分水岭**。 |

---

## 4. 设计

### 4.1 模型边界：`ModelClient` 增加流式 + 工具能力

现状协议（[backend/app/integration/model_client.py:119-122](backend/app/integration/model_client.py#L119-L122)）：

```python
class ModelClient(Protocol):
    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply: ...
    def close(self) -> None: ...
```

`ModelReply`（[backend/app/integration/model_client.py:104-116](backend/app/integration/model_client.py#L104-L116)）的文档串写着 "A decision is JSON text; there is no second channel."——本 spec 要加的正是第二个通道。

**新增一个方法，而不是改 `complete`**（`complete` 仍被领域 Agent 和摘要使用，见 [backend/app/agents/supervisor.py:706](backend/app/agents/supervisor.py#L706) 与 [backend/app/context/summary.py:236-264](backend/app/context/summary.py#L236-L264)）：

```python
@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the model asked for. Arguments are already an object, never text."""
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One increment of a streamed completion.

    Exactly one of text_delta / tool_call_delta / reasoning_delta / finish is
    meaningful per chunk.
    """
    text_delta: str = ""
    tool_call_delta: str = ""     # partial JSON for the arguments, never shown to a user
    reasoning_delta: str = ""     # model's thinking channel; NEVER leaves this module
    finish: bool = False
    truncated: bool = False       # provider stopped early (length / content filter)

class ModelClient(Protocol):
    def complete(self, messages: tuple[dict[str, Any], ...]) -> ModelReply: ...
    def stream(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        tools: tuple[dict[str, Any], ...] = (),
    ) -> Iterator[StreamChunk]: ...
    def close(self) -> None: ...
```

设计要点：

1. **`stream` 是生成器**，不是「回调 + 返回值」。节点消费它，自己决定什么时候把 delta 写进 LangGraph 的 custom stream。
2. **`tool_call_delta` 只用于累积**。`ChatCompletionsModelClient` 内部拼装完整参数后 `json.loads` 一次，得到一个对象；调用方拿到的是 `ToolCall(name, arguments: dict)`，**永远不用自己解析半截 JSON**。累积逻辑封装在客户端里，节点看不到 `tool_call_delta`。
   - 因此节点侧的接口再包一层：`stream_decision(...) -> Iterator[DecisionChunk]`，其中 `DecisionChunk` 要么是 `text_delta: str`，要么是终结的 `tool_call: ToolCall | None`。**半截 JSON 不出 `integration/` 层。**
3. **`tools=()` 时行为与今天完全一致**：不发 `tools` 字段，不启用流式语义的变化。领域 Agent 在 P1–P3 继续调 `complete`（P4 起改走 `stream`，见 §4.8.2）。
4. **`response_format` 与 `tools` 互斥**：`stream(tools=...)` 非空时**不发** `response_format`（即使配置是 `json_object`）。同一请求里同时给「输出必须是 JSON」和「可以调工具」，语义自相矛盾。注意实测结果是**上游不会为此报 400**（§1.4），所以这是一条**语义纪律**，不是错误兜底——但正因为上游不报错，写错也不会有人告诉你，必须在代码里挡住。这一条要写进 `request_parameters`（[backend/app/integration/model_client.py:183-194](backend/app/integration/model_client.py#L183-L194)）并让审计记录如实反映。

5. **`reasoning_delta` 在客户端内部被吞掉。** `ChatCompletionsModelClient.stream` 收到 `delta.reasoning_content` 时**不产出 `StreamChunk`**（或产出后由 `stream_decision` 丢弃），它只用于在审计的 `raw` 里留痕。实测本上游是思考模型且 reasoning 先于正文流出（§1.4），**任何把它当 `text_delta` 转发的实现都会把模型的内部推理泄漏给用户**——银行场景的合规红线。要专门写测试锁死（§6.1）。

**审计（红线，不可省略）**
[backend/app/audit/model_client.py:50-88](backend/app/audit/model_client.py#L50-L88) 的 `AuditedModelClient` 目前只包了 `complete`。`ModelClient` 协议新增 `stream` 后，**`AuditedModelClient` 必须同步实现 `stream`**，否则流式调用会绕过审计——即「每次模型调用一行 `llm_call_audit`」这条契约在 Supervisor 这条路径上失效。

`stream` 的审计语义（沿用 [backend/app/audit/model_client.py:1-22](backend/app/audit/model_client.py#L1-L22) 的三条模块契约）：

- `call_id = new_id("call")` 在**迭代开始前**分配，整条流共用一个 `call_id`。
- **记录在流结束时写**（成功或失败都写），因为流式期间没有完整 reply。`response_format` 字段记 `REQUEST_FORMAT_CHAT_COMPLETIONS`（[backend/app/audit/model_client.py:47](backend/app/audit/model_client.py#L47)）不变，另加一个 `stream: true` 标记。
- **异常路径必须写**：调用方中途 `close()` 生成器（例如首 delta 后放弃）时，`GeneratorExit` 也要落一行，状态记为失败/中止，不能静默丢失。这条要专门写测试。

### 4.2 Supervisor 决策协议

**工具定义**（运行时按领域列表构造，`enum` 来自 `known`，即 [backend/app/agents/supervisor.py:189](backend/app/agents/supervisor.py#L189)）：

```json
{
  "type": "function",
  "function": {
    "name": "handoff",
    "description": "把一个明确的执行指令交给某个领域 Agent。只有在需要领域 Agent 办理业务时才调用；要直接回复用户时，不要调用任何工具。",
    "strict": true,
    "parameters": {
      "type": "object",
      "properties": {
        "agent_id": {
          "type": "string",
          "enum": ["transfer", "bill", "card", "subscription", "life_event", "wealth"],
          "description": "要接手的领域 Agent。"
        },
        "instruction": {
          "type": "string",
          "description": "给该 Agent 的执行指令，一句话说清要办什么。"
        }
      },
      "required": ["agent_id", "instruction"],
      "additionalProperties": false
    }
  }
}
```

- `enum` 是动态的，来自 `domains`（[backend/app/agents/supervisor.py:190](backend/app/agents/supervisor.py#L190) 的 `menu`）。`strict: true` 要求 `additionalProperties: false` 且所有 property 进 `required`——上面两条都满足。
- `tool_choice` **不发送**。实测 `"required"` 与具名工具都被上游忽略（§1.4），发它零收益、却给第三方端点增加 400 的风险面；Anthropic 的默认值本来就是 `auto`。模型要派活就派活、要直接回答就回答，不需要也不应该被强制。**注意这意味着「必须调用工具」不能靠协议保证**——如果将来需要它，只能靠提示词 + 事后校验，不能靠 `tool_choice`。
- `strict: true` **默认保留**。[strict-tool-use.md](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use) 把它定义为 agent 可靠性的基础（"Without strict mode, Claude might return incompatible types (`"2"` instead of `2`) or omit required fields, breaking your functions and causing runtime errors."），实测本上游也接受它。但它只保证**语法**（§1.4 第 3 条），所以 `parse_decision` 的校验照旧保留。若某上游拒绝 `strict`，按 §4.4 第 3 层**单独降级**，不回退整条协议。

**解析**（取代 `parse_supervisor` 的启发式路径）：

```python
def parse_decision(text: str, tool_calls: tuple[ToolCall, ...], known: set[str]) -> SupervisorOutcome:
    """Read one Supervisor decision out of a streamed reply.

    Text alongside a handoff is narration, not the answer: it is surfaced once as a
    `narration` event and does not end the turn.
    """
```

规则：

| 模型输出 | `SupervisorOutcome` | 用户看到 |
| --- | --- | --- |
| 一个 `handoff` call | `agent_id=<enum 值>, instruction=<参数>` | 无文本 |
| 纯文本，无 tool call | `agent_id=None, reply=<文本>` | 该文本（流式） |
| 文本 + `handoff` call | 同第一行，文本另存为 `narration` | 该文本进入活动流（见 §4.3） |
| 多个 `handoff` call | 第一个本轮派发，其余进 `pending_handoffs` 队列（见下） | 无文本 |
| `handoff` 的 `agent_id` 不在 `known` | `DecisionError`（沿用 [backend/app/agents/decisions.py:14-15](backend/app/agents/decisions.py#L14-L15)） | —— |
| 空文本且无 tool call | `reply=NO_REPLY_FALLBACK` | 兜底文案 |

- 最后一行「空文本且无 tool call」保留：即使 `strict` 生效，模型仍可能因为安全策略返回空内容。这是**纵深防御**，不是冗余。
- 「`agent_id` 不在 `known`」在 `strict: true` 下理论不可能（`enum` 已约束），但**保留校验**：降级路径（§4.4）与不支持 `strict` 的 provider 都会走到这里。`DecisionError` 是安全边界，不能因为「schema 保证了」就删掉。`strict` 约束的是采样而非判断力——实测把 `enum` 收窄后模型会**换一个合法但错误的值**，而不是报错（§1.4 第 3 条）。

**为什么不再丢弃「文本 + handoff」中的文本。** v1 的写法（"text alongside a handoff is dropped"）建立在「模型只会输出 JSON，正文里任何字都是噪声」这个假设上。实测推翻了它：模型会先说"我来帮您处理这两件事，分别交给账单和卡片对应的同事"，再发起两个 `handoff`——这句话是**给用户的**，不是给程序的，Anthropic 的标准响应示例正是这个形状（§1.4）。丢掉它等于让用户盯着一个不动的界面。

**多 `handoff` 必须逐个派发，不能只取第一个。** [parallel-tool-use.md](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use) 明确要求 "return one `tool_result` for each `tool_use` block"——静默丢弃一个 call 是协议违规，而用户也确实说了两件事。base 的图一轮只跑一个领域（`return {"active_agent": ..., "turns": turns + 1}`），所以：

- 本轮派发 `handoff[0]`；
- `handoff[1:]` 存入 `state["pending_handoffs"]`（新增字段，形如 `list[{"agent_id": str, "instruction": str}]`）；
- 后续 Supervisor 轮次**若 `pending_handoffs` 非空则不再调用模型**，直接取出队首派发——决策已经做过了，重问一次模型只是多花一次调用、多写一行审计；
- 队列受剩余轮次预算约束（`MAX_DOMAIN_TURNS = 4`，[backend/app/agents/supervisor.py:62](backend/app/agents/supervisor.py#L62)）。溢出时记 warning 并把 `terminal_reason` 标为 `"max_turns"`（§4.3），不静默丢弃。

这是 P2 范围内唯一动到图 state 的改动（见 §8）。**产品已拍板（2026-10-10）：允许一次请求派给多个领域 Agent，逐个派发**（§10 第 9 行）。「一次只办一件事」的备选因此不采纳——它曾是 v1 的行为，而静默丢弃第二个 `handoff` 是协议违规。

**提示词改写** [backend/app/agents/prompts.py:13-44](backend/app/agents/prompts.py#L13-L44)：

- 删除：第 30 行「只输出一个 JSON 对象，不要输出其他文字。」、第 31-34 行的两种形状说明。
- 新增（替换上述两段）：
  > 你要么把这件事交给某个领域 Agent 办理，要么自己直接回复用户。
  > 需要领域 Agent 办理时，调用 `handoff` 工具，把 agent_id 和一句清楚的执行指令填进去。
  > 需要直接回复用户时（闲聊、澄清、告知结果、说明做不到），直接用自然语言回复，不要调用任何工具。
- 保留：九条规则中与编码格式无关的部分——不重复索取用户已给的信息、不编造业务结果、`results` 里的事实要讲清楚、纯闲聊也要回复。
- 调整：原「agent 必须是上表中出现的 agent_id」改为一句说明（`enum` 已强制，但保留可读性）；原「不要输出空对象」删除（不再适用）。
- `SUPERVISOR_SHAPE`（[backend/app/agents/prompts.py:141-145](backend/app/agents/prompts.py#L141-L145)）**不删**，降级路径仍要用它。
- `user_turn`（[backend/app/agents/prompts.py:154-167](backend/app/agents/prompts.py#L154-L167)）需要按协议分支：`tool_calling` 模式下不再注入 shape 段（形状已由工具 schema 表达）。

### 4.3 事件协议：新增 `reply_delta` 与 `narration`

**现状不变量**（[backend/app/api/sse.py:7-9](backend/app/api/sse.py#L7-L9)）：

> "There is no unnamed 'message' frame carrying text deltas: the assistant's reply arrives once, complete, as `final`."

本 spec 要改的正是这一条。**新增一个具名事件**，而不是引入无名 `message` 帧——无名帧在 [backend/app/api/sse.py:76-80](backend/app/api/sse.py#L76-L80) 已被占用为**传输失败**信号，语义不能被劫持。

变更点（共 4 处后端 + 4 处前端）：

| 位置 | 变更 |
| --- | --- |
| [backend/app/agents/runtime.py:37-44](backend/app/agents/runtime.py#L37-L44) `EVENT_FIELDS` | 加 `"reply_delta": ("text",)` 与 `"narration": ("text",)` |
| [backend/app/api/sse.py:32-41](backend/app/api/sse.py#L32-L41) `EVENT_NAMES` | 加 `"reply_delta"`、`"narration"` |
| [frontend/src/api.ts:12-15](frontend/src/api.ts#L12-L15) `AGENT_EVENT_NAMES` | 加 `'reply_delta'`、`'narration'` |
| [frontend/src/types.ts:30-83](frontend/src/types.ts#L30-L83) | 加 `ReplyDeltaEvent { text: string }` 与 `NarrationEvent { text: string }`，并入 `AgentEvent` 联合与 `AgentEventName` |
| [frontend/src/events.ts:43-107](frontend/src/events.ts#L43-L107) `toEvent` | 加 `case 'reply_delta'` / `case 'narration'`：`typeof data.text !== 'string'` 则返回 `null` |
| [frontend/src/App.vue:401-408](frontend/src/App.vue#L401-L408) | 累积 delta 到草稿态；`narration` 把草稿搬进活动流（见 §4.5） |

**为什么不复用 `final`：** `final` 带 `business_ids`（[backend/app/agents/runtime.py:37-44](backend/app/agents/runtime.py#L37-L44)），且它出现意味着「本轮结束」——[frontend/src/App.vue:405](frontend/src/App.vue#L405) 把 `turn.reply` 设上后就当作终态渲染。让 `final` 承担「增量」语义会同时破坏这两点。

**`final` 的语义保持不变**：仍然是「本轮完整回复文本 + 业务编号」，在流结束后发一次。前端收到后**用 `final.text` 覆盖草稿**——这既是幂等校正（防丢帧/重复帧），也是向后兼容的关键：不认识 `reply_delta` 的老客户端忽略它，仍然只靠 `final` 正常工作。

**合批（必须做）**：一个 200 字的回复若逐 token 发帧，会产生上百个 SSE 帧，而 [frontend/src/App.vue:407](frontend/src/App.vue#L407) 每个事件都触发一次 `scrollToEnd()`。规则：

> 后端按「距上次发送 ≥ 40ms **或** 累积 ≥ 24 字符」先到者合批发送一个 `reply_delta`。流结束时**必须 flush 残余**。

40ms / 24 字符是起点值，写成模块常量便于调整。

**`narration` 事件（新增，第二个事件名）**：当本轮以 `handoff` 结束时，模型可能在 tool call 之前已经输出了一段给用户看的话（§1.4）。它不该占据 `final` 的位置（本轮还没结束），也不该被丢弃。协议：

- 这段文本**仍然先以 `reply_delta` 流式推出**。后端在流的开头无法预知后面会不会跟一个 `handoff`——先推再纠正，而不是先攒着不发，那样就没有流式可言了。
- 流结束时若确实有 `handoff`，后端再发一个**完整**的 `narration` 事件，`text` = 累积到的全部文本（完整文本让漏收过 delta 的客户端也能正确显示）。
- 前端收到 `narration` 时把 `replyDraft` **搬进** `turn.activity` 并清空草稿（§4.5）——这句话从「候选回复」变成「过程记录」。
- 不理解 `narration` 的老客户端忽略它：草稿留在气泡里，直到真正的 `final` 覆盖它。**降级可接受。**

为什么不用 `final` 承载旁白：`final` 是终态（上一节），让它在中途出现会破坏 `turn.reply` 的终态语义，也会让老客户端以为本轮结束。

**`terminal_reason` 取代布尔 `incomplete`**：`final` 新增两个可选字段，镜像 Anthropic Agent SDK 的 `ResultMessage`（`subtype` / `is_error` / `terminal_reason` / `api_error_status`）：

```json
{"event": "final", "text": "...", "business_ids": [...], "terminal_reason": "completed"}
```

`terminal_reason` 取值：`"completed"`（默认）、`"api_error"`（首 delta 之后上游失败）、`"max_turns"`（触达轮次上限）、`"aborted_streaming"`（用户中断）。可选 `error_status: int | null` 带上 HTTP 状态码。

比布尔值好在哪里：布尔只说了「不完整」，`terminal_reason` 说了**为什么**——而 Claude Code 正是这么做的，它的 `ResultMessage` 里 `is_error: bool` 旁边永远有 `subtype`（`"success"` / `"error_during_execution"` / `"error_max_turns"` / `"error_max_budget_usd"` / `"error_max_structured_output_retries"`）和 `terminal_reason`（`"completed"` / `"max_turns"` / `"api_error"` / `"aborted_streaming"` / `"aborted_tools"`）。前端据此显示不同文案（"回复未完整生成" vs "已达本轮处理上限"）。

两个字段都是**新增可选字段**，老客户端忽略即可。

### 4.4 降级：能力探测与回退

第三方 OpenAI 兼容端点对 `tools` / `stream` / `strict` 的支持参差不齐，**降级不是可选项**。

**两层控制：**

1. **静态开关**（新增配置项）：
   ```
   LLM_SUPERVISOR_PROTOCOL = "tool_calling" | "json_text"     # 默认 "tool_calling"
   ```
   落在 [backend/app/core/config.py:39](backend/app/core/config.py#L39) 一带（与 `llm_response_format` 同级），并镜像到 [backend/app/api/schemas.py:78](backend/app/api/schemas.py#L78) 的设置面板与 [backend/.env.example:22](backend/.env.example#L22)。运维可以在不改代码的情况下回退。P4 起该开关泛化为 `LLM_AGENT_PROTOCOL`（两个 agent 一把开关），旧名保留为弃用别名（§4.8.2）。

2. **运行时自动降级**（进程内，key = `(base_url, model)`）：
   - 触发条件：上游返回 **400**，且错误文本命中「不支持 tools / stream / strict」的特征词。识别方式沿用 [backend/app/integration/model_client.py:52-94](backend/app/integration/model_client.py#L52-L94) `classify_provider_error` 已有的「读 provider 原文」思路（同类先例见 [backend/app/integration/model_client.py:27-34](backend/app/integration/model_client.py#L27-L34) 的 `CONTEXT_OVERFLOW_MARKERS`）。
   - 动作：把该 `(base_url, model)` 标记为 `json_text`，记一条 warning 日志，**本次调用重试一次**走 `json_text` 路径。
   - 结果：用户看到的是正常的（非流式）回复，**不是错误**。
   - 缓存范围：进程内。重启后重新探测。不做持久化——避免把一次上游抖动固化成永久配置。

3. **`strict` 单独降级**：若 400 的原因是 `strict` 不被支持，**先只降 `strict`**（保留 tool calling 与流式），而不是整条协议回退。tool calling 带来的收益（不再解析正文 JSON）不依赖 `strict`。

**降级路径的等价性**：`json_text` 协议就是今天的行为，走 `parse_supervisor`（[backend/app/agents/supervisor.py:131-146](backend/app/agents/supervisor.py#L131-L146)），`extract_json_object` 与 `SUPERVISOR_SHAPE` 都保留。**降级路径不能是死代码**——要有测试覆盖。

### 4.5 前端渲染

现状（[frontend/src/App.vue:395](frontend/src/App.vue#L395)）：

```ts
const turn: LiveTurn = { user: userText, activity: [], reply: null, unconfirmed: false }
```

改为增加一个草稿字段：

```ts
const turn: LiveTurn = { user: userText, activity: [], reply: null, replyDraft: '', unconfirmed: false }
```

`onEvent` 分支（对应 [frontend/src/App.vue:401-408](frontend/src/App.vue#L401-L408)）：

```ts
if (event.name === 'interrupt') setPending(event.data)
else if (event.name === 'reply_delta') { turn.replyDraft += event.data.text; scrollToEnd() }
else if (event.name === 'narration') { turn.activity.push(event); turn.replyDraft = '' }
else if (event.name === 'final') { turn.reply = event.data; turn.replyDraft = '' }
else turn.activity.push(event)
```

`narration` 那一行是「把候选回复降级成过程记录」：文本已经在草稿里，这里只是把它挪个位置，不需要再用 `event.data.text` 重新赋值（完整文本用于漏收 delta 的客户端，前端可二选一，推荐以 `event.data.text` 为准以免半截）。

渲染（对应 [frontend/src/App.vue:689-690](frontend/src/App.vue#L689-L690)）：显示文本取 `live.reply?.text ?? live.replyDraft`；`business_ids` 与 `terminal_reason` 提示只在 `live.reply` 存在时渲染。条件从 `v-if="live.reply"` 放宽为「`live.reply` 或 `live.replyDraft` 非空」。

`terminal_reason` 提示文案：`"api_error"` → 「回复未完整生成」；`"max_turns"` → 「已达本轮处理上限，请分几次提出」；`"aborted_streaming"` → 「已中断」；`"completed"` → 不显示。`narration` 条目在活动流里用与 tool 卡片不同的样式（纯文本，不带图标），否则用户会以为那是一次工具调用。

**注意 `unconfirmed` 的语义不变**（[frontend/src/App.vue:690](frontend/src/App.vue#L690)）：它处理的是「连接中断、连 `final` 都没收到」。流式之后，用户可能已经看到半截文本——这是**好事**（比空着强），但 `unconfirmed` 的提示文案要相应调整，说明「以下内容可能不完整」。

### 4.6 错误与重试

首 delta 是分水岭（`_model_text` 现在用 [backend/app/agents/upstream.py:64](backend/app/agents/upstream.py#L64) 的 `call_with_one_retry` 包住整次调用）：

| 失败时机 | 行为 |
| --- | --- |
| **首 delta 之前**（含工具调用参数累积期间） | 允许重试一次，沿用 `call_with_one_retry` 的语义。用户什么都没看到，重试是安全的。 |
| **首 delta 之后** | **不重试**。已经推出去的文本不能收回，重试会让用户看到重复或矛盾的半句。改为发 `final`，`text` = 已累积文本，`terminal_reason: "api_error"`，`error_status` = 上游 HTTP 状态码。 |

**上下文溢出**（[backend/app/agents/supervisor.py:96](backend/app/agents/supervisor.py#L96) `OVERFLOW_UPSTREAM_CODE`）的处理链**不变**（[backend/app/agents/supervisor.py:256-262](backend/app/agents/supervisor.py#L256-L262)）：它是首 delta 之前的失败（模型还没产出任何 token 就已经拒绝），所以仍走既有的「截断历史后重试一次，再失败则 `context_limit_error(REASON_OVER_BUDGET)`」。

**「已结论」的短路路径保持不变**（[backend/app/agents/supervisor.py:268-272](backend/app/agents/supervisor.py#L268-L272) 的 `_closing_reply`）：当 `results` 已经足够回答时，不调用模型，直接由 [backend/app/agents/supervisor.py:950-967](backend/app/agents/supervisor.py#L950-L967) 拼接领域 summary。**这条路径没有流式，也不该有**——它是本地拼字符串，逐字流出只会假装是模型在说话。前端拿到的仍然是 `final`（无 delta），渲染路径已兼容。

同理 [backend/app/agents/supervisor.py:231-234](backend/app/agents/supervisor.py#L231-L234) 的 `_final_from`（触达 `MAX_DOMAIN_TURNS = 4`）也不流式。

### 4.7 不改动的部分（明确列出，防误伤）

- 领域子图（`domain_decide` [backend/app/agents/supervisor.py:292](backend/app/agents/supervisor.py#L292)、`DOMAIN_PROMPT`、`DOMAIN_SHAPE`、`parse_domain`）——**限 P1–P3**；P4/P5 的改造全部在 §4.8。
- 工具执行（`_run_tool` [backend/app/agents/supervisor.py:709-754](backend/app/agents/supervisor.py#L709-L754)、`_call_tool` [backend/app/agents/supervisor.py:757-779](backend/app/agents/supervisor.py#L757-L779)）与 `tool_call` / `tool_result` 事件。
- `interrupt` 事件与 `setPending` 流程。
- 摘要（`SUMMARY_PROMPT`、[backend/app/context/summary.py:236-264](backend/app/context/summary.py#L236-L264)）——继续用 `complete`。
- `llm_call_audit` 表结构与既有 `complete` 路径的审计行为。

### 4.8 P4/P5：领域 Agent 的 tool calling 与自循环（对齐 Claude Code 子 agent）

§1.5 的终态在这两期落地。P4 换编码、P5 换结构，各自独立可发布——编码与结构正交，正是拆成两期的原因。

**分工边界（2026-10-10 拍板，v4 更正）**：两期的**代码**改动位于底座（`agents/supervisor.py`、`agents/state.py`、`agents/declaration.py`、`agents/prompts.py`、integration 层）——循环、schema 生成、`ask_user` 注入、预算与降级都是底座行为。

但 v3 那句「领域模块零改动：底座直接消费其既有的 `ToolSpec` 声明」**与代码不符**，必须更正（依据见 §4.8.1）：

- 底座能看到的工具声明来自 `ModuleSpec.tools`（[backend/app/modules/base.py:109](backend/app/modules/base.py#L109)），经 registry 汇总进 `ToolCatalog`（[backend/app/tools/catalog.py:42-79](backend/app/tools/catalog.py#L42-L79)）后绑定到 `AgentToolHandle`；`DomainSpec.tool_menu()`（[backend/app/agents/declaration.py:83-90](backend/app/agents/declaration.py#L83-L90)）读的正是这个 handle。
- 而**六个模块一个都没往底座声明工具**：`transfer` / `bill` / `card` / `subscription` / `life_event` 是 `ModuleSpec(name=..., declaration=...)`（各自 `__init__.py:18`，无 `tools=`），wealth 是 `ModuleSpec(name="wealth", declaration=WEALTH, turn=build_turn)`（[backend/app/modules/wealth/__init__.py:26](backend/app/modules/wealth/__init__.py#L26)，同样无 `tools=`），其模块 docstring（[backend/app/modules/wealth/__init__.py:1-12](backend/app/modules/wealth/__init__.py#L1-L12)）原文写着 "The module declares no tools... the seven read tools are called inside that turn, by the wealth agent, never by the base"。
- 所以 P4/P5 要改造的那条路径**今天没有任何模块走**（§4.8.1）。P4 的第一步因此是**「谁向底座声明工具」**：把 `tools=` 真正填进 `ModuleSpec`（模块侧改动），或明确 P4 只交付地基、由后续 spec 决定第一个声明工具的领域。**这一条已拍板（2026-10-10）：P4/P5 只交付地基**，不含任何模块改动；领域团队按 §4.8.4 的契约自行接入。见 §10.1。

**领域侧 tool calling 的归属（2026-10-10）。** 原 `docs/superpowers/specs/2026-10-10-wealth-model-driven-loop-spec.md`（理财「模型驱动」去规则化专项 spec，曾提交于 `b907542`）**已废除**。P4/P5 因此是本仓库「领域侧 tool calling 与自循环」的**唯一归属**，不再另开 spec。该文件的原文仍可从 git 提交 `b907542` 取回。**素材处置已拍板（2026-10-10）：一项都不并入**——领域侧的记忆策略与终答不变量属于领域团队的工作（N7），底座不代为规定（§10.1）。

#### 4.8.1 现状（为什么必须动）

一次领域激活（`domain_decide` [backend/app/agents/supervisor.py:292](backend/app/agents/supervisor.py#L292)）= 一次 `_domain_turn` 模型调用（[backend/app/agents/supervisor.py:684-706](backend/app/agents/supervisor.py#L684-L706)）→ `parse_domain` → 至多一个工具同步执行（`_conclude` [backend/app/agents/supervisor.py:644-654](backend/app/agents/supervisor.py#L644-L654)）→ summary 写进 `domain_results` → END 回 Supervisor。**领域 Agent 看不见自己工具的结果**——工具摘要只进入下一轮 Supervisor 的视野；`DOMAIN_PROMPT` 的「一次只请求一个工具调用」是这条结构的事实陈述（没有循环去消费第二个调用）。多步业务因此退化为「多轮 Supervisor 路由」，每个工具步 2 次模型调用（§7.1）。

**但这条路径今天对六个模块全部不可达**（v4 补充，已逐条核对）：

1. `_with_tools_available`（[backend/app/agents/supervisor.py:610-626](backend/app/agents/supervisor.py#L610-L626)）第一句就是 `if not spec.tool_menu(): return DomainOutcome(summary=NO_TOOLS_SUMMARY)`；而 `spec.tool_menu()` 读 `self.handle.tools`（[backend/app/agents/declaration.py:83-90](backend/app/agents/declaration.py#L83-L90)），六个模块的 `ModuleSpec.tools` 全为空（§4.8 分工边界）。
2. wealth 更早就在 `domain_decide` 的 `spec.turn is not None` 分支（[backend/app/agents/supervisor.py:309-310](backend/app/agents/supervisor.py#L309-L310)）走 `_conclude_turn`，根本不进工具路径。

因此 `parse_domain`（[backend/app/agents/supervisor.py:149-168](backend/app/agents/supervisor.py#L149-L168)）、`_domain_turn`、`_run_tool` 目前是**死代码**：所有领域一律塌成 `NO_TOOLS_SUMMARY`。上面那段描述对代码成立，但「一次激活 = 1 次调用 + ≤1 个工具」今天没有任何模块实际经历——这是 P4 必须先解决的入口问题（§4.8.2 第 1 条）。

#### 4.8.2 P4：决策协议换成 tool calling（仍单发）

- **第 0 条，先解决入口：谁向底座声明工具。** 见 §4.8.1——今天没有任何模块声明底座可见的工具，因此新协议上线后没有模块会走它。P4 必须把「`ModuleSpec.tools` 填上」纳入范围，或明确只交付地基。**已拍板（2026-10-10）：只交付地基**，`tools=` 声明路径由底座做成可用接口并写接入文档（§4.8.4），填不填由领域团队决定（§10.1）。
- **工具 schema 由底座生成**：`ToolSpec.parameters`（[backend/app/tools/protocol.py:70-84](backend/app/tools/protocol.py#L70-L84)）是声明处，但**它今天不是合法的 function schema**：该字段默认值是 `field(default_factory=dict)`，即 `{}`；而唯一声明过像样 schema 的是 wealth（[backend/app/modules/wealth/infrastructure/tools.py:129-141](backend/app/modules/wealth/infrastructure/tools.py#L129-L141)），wealth 的七个读工具又由其**自己的**循环消费（[backend/app/modules/wealth/workflow/nodes.py:918](backend/app/modules/wealth/workflow/nodes.py#L918) 的 `parse_decision`），从不经过底座。底座因此必须先校验、再发。
- **`strict: true` 需要归一化才能发**：OpenAI strict 要求 `additionalProperties: false` **且所有** `properties` 都在 `required` 里。现有声明不满足——例如 [backend/app/modules/wealth/infrastructure/tools.py:324](backend/app/modules/wealth/infrastructure/tools.py#L324) 的 `_parameters({"status": {...}})` 用了默认 `required=()`（可选属性），[backend/app/modules/wealth/infrastructure/tools.py:314](backend/app/modules/wealth/infrastructure/tools.py#L314) 与 [:342](backend/app/modules/wealth/infrastructure/tools.py#L342) 是 `_parameters({})`（零属性）。所以发 `tools` 之前必须归一化：可选属性改成 `type: [X, "null"]` 并进 `required`，或对该工具显式关掉 `strict`。**§10 第 4 行的「单独降级」只覆盖上游拒绝，覆盖不了本地 schema 非法**——两条出口都要有。
- **`tool_menu()` 的姊妹方法必须同源**：它今天返回 `list[tuple[str, str]]`（[backend/app/agents/declaration.py:83-90](backend/app/agents/declaration.py#L83-L90)）且被 `_with_tools_available`（[backend/app/agents/supervisor.py:610-626](backend/app/agents/supervisor.py#L610-L626)）用来**判空**。出 schema 的方法必须与它从同一处（`self.handle.tools`）派生，否则「菜单非空」与「schema 可发」会漂移，出现「有工具却被判成 `NO_TOOLS_SUMMARY`」或反之。文本菜单保留给降级路径的提示词。
- **三种结局改由协议表达**，与 Supervisor 的双通道同一文法：

  | 模型输出 | 含义 | 取代的现状 |
  | --- | --- | --- |
  | `tool_call`（业务工具） | 执行工具。P4 仍单发；P5 起结果回灌循环 | `{"tool": ..., "arguments": ...}` |
  | `tool_call(ask_user)` | 向用户追问。`ask_user` 是**框架工具**，由底座注入每个领域的工具列表，不在业务菜单文本里。**与工具门的交互必须处理**：`_with_tools_available`（[backend/app/agents/supervisor.py:610-626](backend/app/agents/supervisor.py#L610-L626)）按 `spec.tool_menu()` 判空，若只注入 `ask_user` 而业务菜单为空，模型唯一能调的就是 `ask_user`，其结果会在 [backend/app/agents/supervisor.py:624-625](backend/app/agents/supervisor.py#L624-L625) 被塌成 `NO_TOOLS_SUMMARY`，用户拿不到提问卡片。门必须按「业务工具 ∪ 框架工具」判定 | `{"ask": {...}}` |
  | 纯文本、无 tool call | **summary：交回 Supervisor 的报告** | `{"summary": ...}` |

  纯文本 = 报告，等价于 Claude Code 子 agent 的 final message，也与 Supervisor「无 call = 直接回答」对称。`ask` 必须是工具而不能是文本约定，否则「纯文本」无法区分「报告」与「追问」。
- `ask_user(fields, display)` 的 `fields` 走现有 `resolve_fields`（[backend/app/agents/declaration.py:93-108](backend/app/agents/declaration.py#L93-L108)）渲染补充信息卡片，`MAX_ASK_ATTEMPTS = 3` 预算不变，`wait` / `process` 中断协议不动。
- **配置开关泛化**：`LLM_SUPERVISOR_PROTOCOL` 改名 `LLM_AGENT_PROTOCOL`（旧名作弃用别名），两个 agent 一把开关——分开关没有场景，先不加。`json_text` 模式下 `parse_domain` 继续服役，与新模式行为可等价对照（§4.4 的全部语义复用）。
- `DOMAIN_PROMPT` 同步改写：删「只输出一个 JSON 对象」与三种形状说明，改为描述工具用法与「完成后用自然语言交报告」。`DOMAIN_SHAPE` 不删，降级路径仍用。

#### 4.8.3 P5：自循环（结构变更）

激活内部变成一个循环，形状与 Claude Code 子 agent 一致：

```text
handoff(agent_id, instruction)
  └─ 领域激活：组装领域线程（instruction + 用户请求 + 已收集输入）
       loop（≤ MAX_TOOL_ROUNDS）:            [节点 A：调模型，永不 interrupt]
         模型（tools = 业务工具 + ask_user）
         ├─ 业务 tool_call → 执行（沿用 _run_tool 的幂等键与一次重试）
         │                   → tool_result 追加进领域线程 → 回到节点 A
         ├─ ask_user       → 写入 pending_question，**退出循环**（不 interrupt）
         │                   ↓
         │                 [节点 B：wait，只 interrupt，永不调模型]
         │                   resume → [节点 C：process_answer]
         │                   ↓ 带 answer 与 domain_thread **续跑节点 A**
         └─ 纯文本         → 报告：写入 domain_results，激活结束
```

**节点切分不是风格问题，是不变量**（v4 更正）：[backend/app/agents/supervisor.py:295-297](backend/app/agents/supervisor.py#L295-L297) 的 docstring 原文写着 "It never calls `interrupt()` itself, because a node that both asks the model and interrupts cannot survive being replayed."，[backend/app/agents/supervisor.py:1052-1064](backend/app/agents/supervisor.py#L1052-L1064) 进一步给出规则："Three nodes, split by whether they may touch the model: - `decide` and `process` call the model and never interrupt; - `wait` interrupts and never calls the model. That split is the whole fix for resume"。

所以循环体**不能**把 `ask_user` 就地 interrupt：它必须退出循环，由 `wait` 节点中断；resume 后 `process_answer`（[backend/app/agents/supervisor.py:411-466](backend/app/agents/supervisor.py#L411-L466)）要**从 `domain_thread` 续跑循环**——而它今天在 [backend/app/agents/supervisor.py:436](backend/app/agents/supervisor.py#L436) 是 `_domain_turn(...)` **全新起一轮**。这条跨节点续跑机制是 P5 的核心改动；v3 只有一句「走既有 wait/process 中断协议」，不足以实现。

- **领域线程（新 state 字段 `domain_thread`）**：本次激活私有的消息序列（assistant 的 tool_call 与 tool_result 交替）。**不进 `messages`**——用户可见 transcript 不掺子 agent 内部过程，与 Claude Code 不把子 agent 过程写回主对话同构。它必须过 checkpoint（`ask_user` 中断后循环要带着历史继续），并在激活结束时清空（并入 [backend/app/agents/state.py:103-116](backend/app/agents/state.py#L103-L116) 的 `idle_work_state()`）。
  - 清理点核对（v4）：并入 `idle_work_state()` 是对的——`_conclude`（[backend/app/agents/supervisor.py:674](backend/app/agents/supervisor.py#L674)）与 `_exhausted`（[backend/app/agents/supervisor.py:567](backend/app/agents/supervisor.py#L567)）都会带上它，而 `_awaiting`（[backend/app/agents/supervisor.py:504-518](backend/app/agents/supervisor.py#L504-L518)）**不带**，所以追问中断时线程确实能保住。
  - 两个缺口要一并处理：(a) `goal_reset_state`（[backend/app/agents/state.py:126-129](backend/app/agents/state.py#L126-L129) 的 docstring 记的正是「子图命名空间的同款陷阱」）不碰 `domain_thread`，P5 要么在这里也清，要么写明为什么不清；(b) 若循环体落在领域子图内（§7.1 的三条约束要求它落在绑定 `TrustedContext` 的子图里），`domain_thread` 实际活在**子图命名空间**，父图的 `idle_work_state()` 未必是唯一清理点。
- **回灌的是摘要信封**：沿用 `_run_tool` 的既有取舍（[backend/app/agents/supervisor.py:732-748](backend/app/agents/supervisor.py#L732-L748)）——进线程的只有 seam 定义给模型看的 `summary` 字段；`facts`、`display`、`business_ids` 各走各的通道，不因为循环而扩权。
- **预算分层，互不挤占**：

  | 预算 | 计什么 | 值 |
  | --- | --- | --- |
  | `MAX_DOMAIN_TURNS` | Supervisor 派发次数（一次请求动用几个领域） | 4（不变） |
  | `MAX_TOOL_ROUNDS`（新） | 一次激活内的工具轮次 | 6（起点值） |
  | `MAX_ASK_ATTEMPTS` | 一个问题的追问次数 | 3（不变） |

  溢出处理镜像 `MAX_DOMAIN_TURNS` 的既有哲学（[backend/app/agents/supervisor.py:231-234](backend/app/agents/supervisor.py#L231-L234)）：**从已收集的结果强制生成 summary 收尾**，不中断、不静默丢弃；`subtask_end` 带溢出标记，报告如实转述「已办成前 N 步」。
  - **「互不挤占」要落到实现上**：一次 `ask_user` **不消耗** `MAX_TOOL_ROUNDS`（否则 3 次追问会吃掉 6 个工具轮次的一半），只计 `MAX_ASK_ATTEMPTS`；`MAX_TOOL_ROUNDS` 溢出也不额外消耗 `MAX_DOMAIN_TURNS`。
- **事件与可见性**：`tool_call` / `tool_result` 逐轮照发——活动流就是循环轨迹，前端零改动。循环内模型的旁白文本**不转发**（N2；`forward_subagent_text` 默认 `False` 的同款决定），由解析层并进 summary 或丢弃。**不新增前端事件。**
- **审计**：循环每轮模型调用一行 `llm_call_audit`，「一次尝试一行」契约天然覆盖，无表结构变更。
- **Supervisor 零改动**：`domain_results` 仍是「一次激活一条」（summary + 该激活累计的 `business_ids`），`_closing_reply`、`pending_handoffs`、`_already_concluded` 全部照旧。whole-turn handler（`spec.turn`，[backend/app/agents/supervisor.py:309-310](backend/app/agents/supervisor.py#L309-L310)）不动——它本来就是「自带循环的模块」，P5 只是把同构的循环给到工具型领域。
- **降级**：`json_text` 模式下循环结构保留、编码退回逐轮 JSON 单发（`parse_domain` 继续服役）。

#### 4.8.4 交付契约：领域团队如何接入（v5 新增）

P4/P5 只交付地基（§10.1），所以本节是**唯一的接入说明书**。领域团队要让自己的领域走 tool calling + 自循环，只需三步，**不动底座任何文件**：

1. **在模块的 `__init__.py` 里声明工具**：`ModuleSpec(name=..., declaration=..., tools={...}, handlers={...})`（[backend/app/modules/base.py:109-110](backend/app/modules/base.py#L109-L110)）。`tools` 的键与 `handlers` 的键必须一一对应（registry 启动时校验，[backend/app/modules/registry.py:96-103](backend/app/modules/registry.py#L96-L103)）。模块自己的 agent 自动获得自己全部工具，无需写 `grants`（[backend/app/modules/base.py:146-161](backend/app/modules/base.py#L146-L161)）。
2. **把 `ToolSpec.parameters` 写成合法 JSON Schema**：底座会把它直接翻译成 function 定义，strict 模式下还要求 `additionalProperties: false` 且所有属性出现在 `required`（§4.8.2）。可选属性请写成 `{"type": ["string", "null"]}` 并放进 `required`。`parameters={}`（默认值）**不是合法 schema**，会被归一化或拒绝——不要依赖默认值。
3. **不要同时声明 `turn` 和 `tools`**：两者硬互斥（[backend/app/modules/base.py:138-142](backend/app/modules/base.py#L138-L142)），会抛 `ValueError("module ... declares both a turn handler and tools: one request cannot be answered twice")`。走底座循环就删掉 `turn=`；自己管循环就继续用 `turn=`，此时本节其余部分与你无关。

**底座为你做什么**（声明之后自动生效）：

- 提示词里的工具菜单 = 你的 `tools`（`DomainSpec.tool_menu()`，[backend/app/agents/declaration.py:83-90](backend/app/agents/declaration.py#L83-L90)，docstring 原文 "The menu comes from the handle, not from the declaration, so what the prompt advertises is exactly what the Gateway will accept."）
- 工具调用由 `ToolGateway` 按调用方身份授权执行（[backend/app/tools/gateway.py:138-153](backend/app/tools/gateway.py#L138-L153)）；跨领域共享要显式写 `grants`
- `ask_user` 由底座注入为框架工具，**不占用** `MAX_TOOL_ROUNDS`（§4.8.3）
- 工具结果回灌你的上下文、`MAX_TOOL_ROUNDS`（6）预算与溢出收尾、`domain_thread` 生命周期、每次模型调用一行 `llm_call_audit`（§4.8.3）

**底座不为你做什么**（N7）：会话记录怎么当记忆、终答要守哪些不变量、业务语义与问卷设计。这些留在你的领域内。

---

## 5. 兼容性与迁移

1. **数据库迁移：仅一处。** 事件协议本身是传输层，不动表结构；唯一的迁移是 `llm_call_audit` 增 `stream` 列（§9 第 3 条、§10 第 10 行，P1 交付，新迁移 `0012_llm_call_audit_stream`）。
2. **无破坏性 API 变更。** 只新增 SSE 事件名（`reply_delta`、`narration`）与 `final` 的可选字段 `terminal_reason` / `error_status`；`final` 的既有字段不变。
3. **老前端 + 新后端**：老前端不认 `reply_delta` / `narration`（[frontend/src/api.ts:12-15](frontend/src/api.ts#L12-L15) 的白名单不含它们）。需确认该白名单**只用于过滤、不抛错**——[frontend/src/events.ts:43-107](frontend/src/events.ts#L43-L107) 的 `toEvent` 对未知名字返回 `null`，[frontend/src/App.vue:402-403](frontend/src/App.vue#L402-L403) 静默 `return`。**行为安全，但必须写测试锁住**，否则将来有人把未知事件改成抛错就会打爆老客户端。
4. **新前端 + 老后端**：收不到 `reply_delta`，`replyDraft` 恒为空，行为与今天完全一致。
5. **回滚**：把 `LLM_SUPERVISOR_PROTOCOL` 设为 `json_text` 即可回到今天的行为，**无需回滚代码**。

---

## 6. 测试计划

### 6.1 后端

**决策解析**（扩充 [backend/tests/test_supervisor_decision.py](backend/tests/test_supervisor_decision.py)，现有用例在 :18/:37/:44/:51/:62）
- `handoff` call → `SupervisorOutcome(agent_id=<值>, instruction=<值>)`
- 纯文本 → `agent_id=None, reply=<文本>`
- 文本 + `handoff` → handoff 生效，文本作为 `narration` 保留（**不是**被丢弃）
- 两个 `handoff` → 第一个进 `agent_id`，第二个进 `pending_handoffs`
- `pending_handoffs` 非空时**不再调用模型**，直接派发队首（断言 model client 调用次数）
- `pending_handoffs` 溢出（超过剩余轮次）→ 记 warning 且 `terminal_reason="max_turns"`
- `agent_id` 不在 `known` → `DecisionError`（**保留**，`strict` 只保证语法，§1.4）
- 空文本且无 call → `NO_REPLY_FALLBACK`
- **回归**：`json_text` 路径的既有 5 个用例全部保持通过

**流式**（新建 `backend/tests/test_supervisor_streaming.py`）
- 假 model client 推 `["您", "好"]` → 断言事件序列 `reply_delta("您好") → final(text="您好")`
- 合批：推 100 个小 delta → 断言 `reply_delta` 帧数 **远小于 100**，且拼接结果与 `final.text` 完全一致
- 流结束 flush 残余：最后一个 delta 小于阈值也要发出去
- **`reasoning_content` 不外发（红线）**：假 client 先推 reasoning delta 再推 text delta → 断言输出的事件流里**没有任何**一个事件携带 reasoning 文本，且 `final.text` 不含它
- **文本 + handoff**：推 text delta 后给一个 `handoff` → 断言先有 `reply_delta`、再有 `narration(text=全部文本)`、**没有** `final` 携带这段文本
- 首 delta 后失败 → 不重试（断言 model client 只被调用一次）、发 `final(terminal_reason="api_error", error_status=<码>)`
- 首 delta 前失败 → 重试一次
- 首 delta 前 overflow → 仍走既有的截断重试链

**事件协议**
- `EVENT_FIELDS` 含 `reply_delta`、`narration`；`_custom_event`（[backend/app/agents/runtime.py:595-604](backend/app/agents/runtime.py#L595-L604)）接受它们
- `EVENT_NAMES` 含 `reply_delta`、`narration`；`encode_event` 能编码
- 未知事件仍然抛 `ValueError`（回归）

**审计（红线）**
- 流式调用产生**恰好一行** `llm_call_audit`，且带 `call_id`
- 流式调用中途抛异常 → 仍产生一行（失败记录）
- 生成器被提前 `close()` → 仍产生一行
- `AuditedModelClient` 实现了 `stream`：写一个测试断言协议方法集被完整覆盖（例如遍历 `ModelClient` 的成员，逐个断言 `AuditedModelClient` 有同名属性）——**这条测试的价值在于防止将来再加方法时又漏掉审计**。

**降级**
- `LLM_SUPERVISOR_PROTOCOL=json_text` → 走 `complete` + `parse_supervisor`，行为与今天一致
- 假 provider 对 `tools` 返回 400 + "does not support tools" → 自动降级、**本次调用成功返回**、该 `(base_url, model)` 被标记
- 假 provider 只拒绝 `strict` → 只降 `strict`，仍走 tool calling
- 降级后不再反复探测（断言第二次调用直接走 `json_text`，不发 `tools`）

**提示词**
- `tool_calling` 模式下渲染出的 system prompt 不含 "JSON"、不含 `SUPERVISOR_SHAPE`
- `json_text` 模式下渲染出的 system prompt 含 `SUPERVISOR_SHAPE`

### 6.2 前端

- [frontend/src/events.test.ts:157-171](frontend/src/events.test.ts#L157-L171)：`reply_delta` / `narration` 窄化——合法 `{text}` 通过；缺 `text` / `text` 非字符串返回 `null`
- [frontend/src/api.test.ts:61-93](frontend/src/api.test.ts#L61-L93)：`reply_delta` / `narration` 帧能被解析
- [frontend/src/App.test.ts:327](frontend/src/App.test.ts#L327) 一带：三段 delta 后渲染出拼接文本；`final` 到达后以 `final.text` 覆盖
- `narration` 到达 → 草稿被清空、文本出现在活动流里；随后 `final` 仍然正常渲染
- `final.terminal_reason` 为 `"api_error"` / `"max_turns"` 时出现对应提示；`"completed"` 时不出现
- **兼容测试**：喂一个未知事件名（例如 `"future_event"`）→ 断言 `toEvent` 返回 `null` 且不抛错（锁住 §5.3）

### 6.3 P4/P5（领域 tool calling 与自循环）

- 工具 schema 生成：`ToolSpec.parameters` → function 定义；`ask_user` 出现在每个领域的工具列表，业务菜单文本（降级提示词用）不含它
- 三种结局各自映射正确的 `DomainOutcome`：业务 `tool_call` / `ask_user` / 纯文本 = summary
- 循环回灌：连续两个 `tool_call` → `domain_thread` 里有两组 tool_call/tool_result，且**第二轮模型调用的输入包含第一轮的结果**（断言 prompt 内容，不是只断言调用次数）
- `MAX_TOOL_ROUNDS` 溢出 → 强制 summary 收尾、`subtask_end` 带标记、不抛错
- `ask_user` 中断后 resume → 循环带着 `domain_thread` 与 answer 继续，不从零重组上下文
- 激活结束 → `domain_thread` 清空（并入 `idle_work_state()`）
- 审计：三轮循环产生**恰好三行** `llm_call_audit`，各带独立 `call_id`
- 降级：`LLM_AGENT_PROTOCOL=json_text` → 循环保留、编码退回 `parse_domain`，与 tool_calling 路径行为等价对照
- **红线**：循环内旁白文本不出现在任何 SSE 事件里（与 §6.1 的 reasoning 不外发同组）
- 提示词：`tool_calling` 模式下 `DOMAIN_PROMPT` 渲染结果不含 "JSON"、不含 `DOMAIN_SHAPE`；`json_text` 模式反之
- **重放不变量（红线）**：循环体节点在任何情况下都不 `interrupt`；`ask_user` 到达时它写入 `pending_question` 并结束，由 `wait` 节点中断。断言「调模型的节点从未 interrupt、interrupt 的节点从未调模型」（可用一个会重放的假图）
- **跨节点续跑**：`ask_user` resume 后 `process_answer` 从 `domain_thread` 续跑循环——断言恢复后第一次模型调用的输入包含中断前已执行的 `tool_result`（不是全新起一轮）
- **工具门**：业务菜单为空但注入了 `ask_user` 的领域 → 断言不塌成 `NO_TOOLS_SUMMARY`，`ask_user` 能正常产出提问卡片
- **预算互不挤占**：连续 3 次 `ask_user` 之后仍能执行满 `MAX_TOOL_ROUNDS` 次工具调用（断言模型调用次数）
- **schema 归一化**：`parameters={}` 与含可选属性的声明 → 发出去的 function 定义满足 strict（`additionalProperties: false` 且所有 property 在 `required`），或该工具被显式标记为不 strict；两种结果都不允许出现非法 schema 直发上游
- **`domain_thread` 生命周期**：`_awaiting` 路径保住它；`_conclude` / `_exhausted` / `goal_reset_state` 路径清空它
- **接口可用性（合成模块，v5 新增）**：测试里注册一个声明了 `tools=`/`handlers=` 的假模块，断言全链路走通——「`ToolSpec.parameters` → function schema → 模型返回 tool_call → 执行 → 回灌 → 收尾」；并断言 `turn` 与 `tools` 同时声明会抛 `ValueError`（[backend/app/modules/base.py:138-142](backend/app/modules/base.py#L138-L142)）。**这是 P4/P5 唯一的端到端验收途径**：真实领域今天都不声明工具（§9 第 5 条）。

---

## 7. 备选方案与被否决项

### 7.1 否决：把两级 agent 塌缩成一级（对齐方式是「循环下放」，见 §4.8）

Codex / Claude Code 的主 agent 是**一个循环**（模型 → 工具 → 结果回灌同一上下文 → 模型）。base 是**两级、两次模型调用**：Supervisor `decide`（[backend/app/agents/supervisor.py:239-243](backend/app/agents/supervisor.py#L239-L243)）做路由 → 领域子图 `domain_decide`（[backend/app/agents/supervisor.py:706](backend/app/agents/supervisor.py#L706)）决定调哪个工具 → 工具在图节点同步执行 → 结果写回 state → 下一轮 Supervisor 重读 `results`。一次工具调用 = **2 次模型调用 + 图节点往返**，硬上限 `MAX_DOMAIN_TURNS = 4`（[backend/app/agents/supervisor.py:62](backend/app/agents/supervisor.py#L62)）。

**这笔成本的一半在 v3 中已安排消除**——P5（§4.8.3）把工具循环下放进领域子图，结果回灌领域自己的上下文，**工具步之间的 Supervisor 路由调用消失**。要精确：领域内每个工具步仍是一次模型调用，总量不归零；3 步业务从 7 次调用（4 次路由 + 3 次领域）降到 5 次（2 次路由 + 3 次领域）。消除路由这一半**不需要**塌缩层级，因为下面三条约束本来就不是在为这笔成本买单，而是在为结构本身买单：

1. **领域隔离**：[backend/app/agents/prompts.py:47](backend/app/agents/prompts.py#L47) 的 `DOMAIN_PROMPT` 明写「你只处理{display}范围内的事，不处理其他领域，也不联系其他领域 Agent」。塌缩后这条边界消失。
2. **可信身份边界**：[backend/app/agents/runtime.py:7-10](backend/app/agents/runtime.py#L7-L10)——领域子图在编译时绑定 caller 的 `TrustedContext`，**图是 caller 的函数，不能跨会话共享**。一级循环会让工具集随会话变化，与这条设计冲突。
3. **审计**：每次模型调用一行 `llm_call_audit`，两级结构让「谁做的决定」在审计里可分辨。

**结论：塌缩不做，两级结构是终态。** §1.5 对齐的本来就是 Claude Code「主 agent + Task 子 agent」的两级形态，不是它的单循环形态；循环下放见 §4.8。

### 7.2 否决：把现有 JSON 原样搬进 tool 的 `parameters`

即「用一个 `decide` 工具，参数是 `{agent, instruction, reply}`」。`reply` 仍是参数里的字段，仍要等参数收完才能展示——**流式收益为零**，只是把 `extract_json_object` 换成了 provider 的 JSON 解析。不采纳。

### 7.3 否决：用无名 `message` 帧承载 delta

[backend/app/api/sse.py:76-80](backend/app/api/sse.py#L76-L80) 已经把无名帧用作传输失败信号。劫持它会破坏现有的错误处理（[frontend/src/App.vue:690](frontend/src/App.vue#L690) 的 `unconfirmed` 逻辑依赖它）。用具名事件。

### 7.4 否决：逐 token 直接发帧

帧数与 `scrollToEnd()` 调用次数爆炸（[frontend/src/App.vue:407](frontend/src/App.vue#L407)）。用 §4.3 的合批。

### 7.5 否决：把 `response_format` 改成 `json_object` 当作「先修一半」

`json_object` 只保证**是合法 JSON**，不保证符合 `SUPERVISOR_SHAPE`——模型仍可能输出 `{"reply": ...}` 少个 `agent` 字段，或换个键名。它把问题从「可能不是 JSON」缩到「是 JSON 但形状不对」，而 `extract_json_object` 之后的形状校验（`parse_supervisor`）本来就覆盖了后者。**收益接近零，却要改默认配置。** 不采纳。

---

## 8. 分阶段实施

| 阶段 | 内容 | 独立可发布 | 用户可见变化 |
| --- | --- | --- | --- |
| **P1** | 模型边界：`ModelClient.stream` + `ToolCall` + `StreamChunk` + `AuditedModelClient.stream` + `request_parameters` 的 `tools`/`response_format` 互斥。领域 Agent 与 Supervisor 都还没用。 | 是 | 无 |
| **P2** | Supervisor 决策改用 `tool_calling`（提示词改写、`parse_decision`、`pending_handoffs` 队列、降级开关与自动降级）。`json_text` 为回退。此阶段后端**还不发** delta，`narration` 也不发（旁白在 P3 之前无处安放）。 | 是 | 无（前端还没渲染 delta；`final` 照旧） |
| **P3** | 事件协议 + 前端流式渲染（`reply_delta`、`narration`、合批、`terminal_reason`）。 | 是 | **有**：直接回复逐字出现，派活前有旁白 |
| **P4** | 领域 Agent 决策改 tool calling（§4.8.2）：**先把 `tools=` 声明路径做成可用接口并写接入文档（§4.8.4，入口）**、schema 从 `ToolSpec` 生成并归一化到 strict 兼容、`ask_user` 框架工具、`LLM_AGENT_PROTOCOL` 开关、`parse_domain` 退役为降级路径。**仍单发**（一次激活 = 1 次调用 + ≤1 个工具）。附 `response_format` 配置项语义清理（§9 第 1 条）。 | 是 | **无**（只交付地基，§10.1 拍板） |
| **P5** | 领域 Agent 自循环（§4.8.3）：`domain_thread`、结果回灌、`MAX_TOOL_ROUNDS` 预算与溢出收尾。 | 是 | **无**（只交付地基，§10.1 拍板）；一旦某领域按 §4.8.4 接入，其活动流会出现同一领域连续多轮 `tool_call`/`tool_result` |

**P2 单独发布是安全的**，因为此时 `reply_delta` 还没人发，前端零改动。这个顺序让「协议切换」和「渲染变化」这两个风险源分开验证。P4 与 P5 同理：先把编码切干净（P4 可与现状逐用例对照），再动循环结构。

**P4/P5 交付后无领域受益**（2026-10-10 拍板，如实记录）：六个模块今天都没有向底座声明工具，wealth 走 `spec.turn` 且不在本 spec 改造范围（N7）。所以 P4/P5 的验收**只能靠测试里的合成模块**（§6.3 的「接口可用性」用例），不能靠真实业务流量。选这个范围的理由是「领域 Agent 的部分留给领域团队开发」——底座先把契约做对。

---

## 9. 已知缺口与待办（不在本 spec 范围）

1. **`response_format="json_schema"` 什么都不发**（[backend/app/integration/model_client.py:196-205](backend/app/integration/model_client.py#L196-L205)、[backend/tests/test_llm_settings.py:97-108](backend/tests/test_llm_settings.py#L97-L108)）。这个取值的名字承诺了它没做的事。两个方向：(a) 真正实现它（用 tool calling 的 schema 生成路径）；(b) 改名以反映真实语义（`json_object` / `none`），并保留 `json_schema` 作为弃用别名。**本 spec 选了 (a) 的等价物但只对 Supervisor 生效**，全局配置项的语义仍未修。建议单独处理。
2. **领域 Agent 的 JSON 解析**仍是启发式的（`parse_domain` [backend/app/agents/supervisor.py:149-168](backend/app/agents/supervisor.py#L149-L168)）——**且这条路径今天对六个模块全部不可达**（§4.8.1）。P4 处理（§4.8.2），届时它退役为降级路径。
3. **`llm_call_audit` 加 `stream` 列**（v4 已拍板，P1 交付）：[backend/app/storage/models.py:254-299](backend/app/storage/models.py#L254-L299) 的 `LlmCallAudit` 增 `stream: Mapped[bool]`（SQLAlchemy `Boolean`，`nullable=False`，`server_default=sa.false()` 让既有行迁移安全——`Boolean` 目前未 import，需一并加入 [backend/app/storage/models.py:5](backend/app/storage/models.py#L5) 的 `from sqlalchemy import (...)`），新增迁移 `0012_llm_call_audit_stream`（`down_revision = "0011_conversation_serial_no"`）。**这是本 spec 唯一的数据库迁移**（§5 第 1 条）。选加列而非塞进 `detail`/`raw`，是为了让审计能按流式/非流式筛选与统计。
4. **合批参数（40ms / 24 字符）仍是起点值**，需要真实环境压测后调整。实测只提供了边界：chunk 粒度 2–3 字符、`gap_median = 0ms`（突发式到达）、`gap_max` 28–314ms。按时间合批是对的方向（纯按字符数会在突发流里把整段攒到最后才发），但具体阈值要看真实用户的阅读节奏。
5. **P4/P5 交付后无领域受益**（2026-10-10 拍板）：六个模块今天都不向底座声明工具，wealth 走 `spec.turn`（§4.8.1）。P4/P5 完成后，`_with_tools_available` 仍会对所有现有领域返回 `NO_TOOLS_SUMMARY`——这是**范围选择的结果，不是缺陷**。验收依赖 §6.3 的合成模块用例。第一个真实受益者出现的时间，取决于领域团队按 §4.8.4 接入的时间。

---

## 10. 已决问题（v2 依据实测与主流做法；v3 增补 #6–#8，依据 2026-10-10 产品拍板；v4 增补 #9–#10；v5 收窄范围并拍定 §10.1）

| # | 问题 | 决定 | 依据 |
| --- | --- | --- | --- |
| 1 | 合批阈值 | 保留 **40ms / 24 字符**，写成模块常量 | 实测 chunk 粒度 2–3 字符、`gap_median = 0ms`（突发到达）、`gap_max` 28–314ms，所以「按时间合批」是对的，「每 N 个 token」在突发流里会把一整坨攒到最后才发。Claude Code 把逐条流式事件做成**可选**（`include_partial_messages` 默认 `False`，`StreamEvent.event` 就是原始 API 事件），说明「批到什么粒度」是客户端决策、没有权威答案——取保守值 + 留可调常量。 |
| 2 | `final.incomplete` | **改为 `terminal_reason`（枚举）+ `error_status`（可选 HTTP 码）** | 镜像 Claude Code `ResultMessage` 的 `subtype` / `is_error` / `terminal_reason` / `api_error_status`。布尔说不清「为什么」，而前端需要按原因显示不同文案（§4.3）。 |
| 3 | 降级缓存范围 | **进程内**，key = `(base_url, model)` | 没有主流先例可抄，取「避免把一次上游抖动固化成永久配置」这一侧。运维要固化就用静态开关（§4.4）。 |
| 4 | 是否默认 `strict: true` | **默认 `true`**，被拒时按 §4.4 第 3 层**单独降级** | Anthropic 把 strict 定义为 agent 可靠性的基础，实测本上游也接受它。但必须记住它只保证语法（§1.4 第 3 条），`parse_decision` 的校验照旧保留。 |
| 5 | `tool_choice` 是否显式设 `auto` | **不发送该字段** | 实测 `"required"` 与具名工具都被忽略（§1.4）——发它零收益、却增加第三方端点的 400 风险面；Anthropic 的默认值本来就是 `auto`。 |
| 6 | 终态形态 | **对齐 Claude Code：主 Agent 不持有业务工具（仍可直接回答）+ 领域 Agent 自循环报告，两级结构保留** | 产品拍板（2026-10-10）。Claude Code 主 agent 的对应形态是「自有工具 + Task 派发并存」，base 的业务动作全在领域侧，故主 Agent 先不持业务工具（N6）；子 agent 自带工具循环、跑完交报告、过程文本默认不转发，正是 Agent SDK 的默认行为（`forward_subagent_text: False`）。 |
| 7 | 领域内循环预算 | 新增 `MAX_TOOL_ROUNDS = 6`（起点值），与 `MAX_DOMAIN_TURNS = 4`（派发次数）、`MAX_ASK_ATTEMPTS = 3`（追问次数）分层互不挤占；溢出从已收集结果强制 summary 收尾 | 镜像 `MAX_DOMAIN_TURNS` 的「有界但回答」哲学：不中断、不静默丢弃，`subtask_end` 带标记如实转述。 |
| 8 | 领域三结局的表达 | 业务工具 = `tool_call`；追问 = 框架工具 `ask_user`；报告 = 纯文本、无 tool call | 与 Supervisor「无 call = 直接回答」同一文法；纯文本即 Claude Code 子 agent 的 final message。`ask` 必须是工具而非文本约定，否则「纯文本」无法区分报告与追问。 |
| 9 | 一次请求是否允许派给多个领域 Agent | **允许，逐个派发**（`pending_handoffs` 队列） | 产品拍板（2026-10-10）。Anthropic 要求每个 `tool_use` 都要有对应的 `tool_result`，静默丢弃是协议违规；队列逐个派发会消耗 `MAX_DOMAIN_TURNS = 4`，产品上等价于「一次说多件事可以，但可能到不了第四件」——这是接受的取舍，不再作为未决项。 |
| 10 | `llm_call_audit` 如何表达流式调用 | **加列**（`stream: bool`，`nullable=False`，`server_default=false`），P1 交付，迁移 `0012_llm_call_audit_stream` | 用户拍板（2026-10-10）。加列可让审计按流式/非流式筛选与统计；代价是一次迁移，已在 §5 第 1 条如实记录（此前 v2/v3 写的「无数据库迁移」不再成立）。 |

### 10.1 P4/P5 的落点与废除素材（已拍板，2026-10-10）

**P4 的入口：谁向底座声明工具？**

**拍板结论（2026-10-10）**：① **P4/P5 只交付地基**，不含任何模块改动；② 废除的 wealth spec **一项素材都不并入**。

**依据（用户原话，m00473）**：「只交付地基，废除的spec 什么都不要，领域智能体的部分留给领域智能体的人开发，只关注底座部分，能否正确派发子智能体，能否流式，能否协议层调用工具和文本输出」。

**「地基」具体指什么**：协议（`ModelClient.stream` + `tools`、`ToolCall`/`StreamChunk`，§4.1）、决策（`handoff` tool call、`parse_decision`、降级，§4.2）、事件（`reply_delta`/`narration`/`terminal_reason`，§4.3）、领域侧契约（`ModuleSpec.tools` 声明路径、schema 生成与 strict 归一化、`ask_user`、`MAX_TOOL_ROUNDS`、`domain_thread`，§4.8.4）、降级（§4.4）。

**代价（如实记录，不是缺陷）**：P4/P5 交付后无领域受益（§8、§9 第 5 条）。六个模块今天都不向底座声明工具——`transfer`/`bill`/`card`/`subscription`/`life_event` 各自只有一个 `__init__.py`（537 / 532 / 544 / 578 / 565 字节），内容是 `DomainDeclaration` + 空 `ModuleSpec`，**是还没开发**；底座对它们回 `NO_TOOLS_SUMMARY`（「该能力当前尚未接入，暂时无法处理此请求。」）**是正确行为**。wealth 走 `turn=build_turn`（[backend/app/modules/wealth/__init__.py:26](backend/app/modules/wealth/__init__.py#L26)），不在本 spec 改造范围（N7）。因此 `_with_tools_available`（[backend/app/agents/supervisor.py:610-626](backend/app/agents/supervisor.py#L610-L626)）仍会对所有现有领域判空返回，P4/P5 的端到端验收只能靠 §6.3 的合成模块用例。

**机制存档**（领域团队将来接入时看这一段）——底座在启动时把每个模块声明的工具汇总成一张全局表：

1. 每个领域模块的 `__init__.py` 里有一个 `ModuleSpec`，它带 `tools: Mapping[str, ToolSpec]`（[backend/app/modules/base.py:109](backend/app/modules/base.py#L109)）与配套的 `handlers`（同处 :110）。
2. `build_registry()`（[backend/app/modules/registry.py:38](backend/app/modules/registry.py#L38)）遍历所有模块：把 `module.tools` 汇总成全局工具表、`module.effective_grants`（[backend/app/modules/base.py:146-161](backend/app/modules/base.py#L146-L161)：模块自己的 agent 自动获得自己全部工具，`grants` 只表达跨领域共享）汇总成授权表，并做校验（重名、`tools` 与 `handlers` 必须一一对应、`parameters` 不得出现底座注入的保留名 [backend/app/modules/registry.py:96-103](backend/app/modules/registry.py#L96-L103)）。
3. 这张表交给 `ToolGateway`，由它按 caller 身份铸 `AgentToolHandle`（[backend/app/tools/gateway.py:138-153](backend/app/tools/gateway.py#L138-L153)）——handle 里的 `tools` 就是该 agent 的授权清单。
4. 领域 agent 的提示词菜单 `DomainSpec.tool_menu()`（[backend/app/agents/declaration.py:83-90](backend/app/agents/declaration.py#L83-L90)）读的正是这份清单，docstring 原文："The menu comes from the handle, not from the declaration, so what the prompt advertises is exactly what the Gateway will accept."

工具**故意不放在 `DomainDeclaration` 上**（[backend/app/agents/declaration.py:1-10](backend/app/agents/declaration.py#L1-L10)）："The tools are deliberately not here. They belong to the module that implements them (`ModuleSpec.tools`), so 'which tools exist' has one source and cannot drift from the handlers behind it." 也就是说，「声明工具」这件事只有模块自己能做，底座无法代劳。

---

**以下为评估记录（已否决的选项与素材清单，保留供回看）**

**问题。** 今天六个模块的 `tools` 全是空（`transfer`/`bill`/`card`/`subscription`/`life_event` 各自 `__init__.py:18`、`wealth` `__init__.py:26`），于是全局工具表是空的，每个 agent 的 handle 里 `tools=()`（[backend/app/tools/gateway.py:147](backend/app/tools/gateway.py#L147)），`tool_menu()` 返回 `[]`，[backend/app/agents/supervisor.py:610-626](backend/app/agents/supervisor.py#L610-L626) 的 `_with_tools_available` 判空后直接返回 `NO_TOOLS_SUMMARY`。**所以「把领域 Agent 改成 tool calling 循环」不是纯底座侧改造：P4 的代码全部写完，也仍然没有任何领域会走那条路径**——除非有人去改某个模块的 `__init__.py`。

**但「空」不等于「坏」。** 那五个模块是**还没开发**：各自只有一个 `__init__.py`（537 / 532 / 544 / 578 / 565 字节），内容就是 `DomainDeclaration` + 空的 `ModuleSpec`。底座对它们回 `NO_TOOLS_SUMMARY`（「该能力当前尚未接入，暂时无法处理此请求。」）**是正确行为**——这正是「领域自己声明工具、没开发就如实说未接入」这条设计的预期结果。今天**唯一实现了业务的领域是 wealth**（完整目录树 application / contracts / domain / infrastructure / llm / workflow），而它走 `turn=build_turn`：自带循环、自带七个只读工具、**不经底座工具表**。

因此 P4/P5 的落点只有两种，区别只在「要不要连带动 wealth」：

| 选项 | 内容 | 代价 |
| --- | --- | --- |
| (a) P4/P5 含模块改动，落点只能是 wealth **（已否决）** | 删 `turn=build_turn`（[backend/app/modules/wealth/__init__.py:26](backend/app/modules/wealth/__init__.py#L26)），把七个读工具经 `ModuleSpec.tools` 声明给底座，由底座的领域子图循环驱动 | **必须先解决 `turn` 与 `tools` 硬互斥**（[backend/app/modules/base.py:138-142](backend/app/modules/base.py#L138-L142)：`if self.turn is not None and self.tools: raise ValueError("module ... declares both a turn handler and tools: one request cannot be answered twice")`）；wealth 是唯一有真实业务逻辑与既有测试的领域，迁移面最大；`ToolSpec.parameters` 要归一化到 strict 兼容（§4.8.2） |
| (b) P4/P5 只交付地基 | 协议、schema 生成、`ask_user`、预算、降级全部就位，但没有领域走新路径 | 用户可见变化确认为「无」；框架在没有消费者的情况下先落地，等第一个新领域开发时再验证 |

**已否决 (a)**：领域侧 tool calling 若始终没有第一个落点，P4/P5 确实只是纸面能力——但用户拍板「领域智能体的部分留给领域智能体的人开发」，底座先把契约做对。已按 (b) 的要求在 §8 明写「P4/P5 交付后无领域受益」，并在 §9 第 5 条重复，避免后续误以为能力已接入。

**素材清单（已拍板：一项都不并入）** 原 `docs/superpowers/specs/2026-10-10-wealth-model-driven-loop-spec.md` 已于 2026-10-10 废除（§4.8 前言），它含 wealth 专属素材，其中三项是 P4/P5 **没有覆盖**的——按拍板结果，这三项**不并入**，属于领域团队的工作（N7）：

| 素材 | 是否已在 P4/P5 内 |
| --- | --- |
| 一个模型循环（工具调用 → 结果进记录 → 再问模型） | ✅ §4.8.3 |
| 工具表（七个已有 `ToolSpec` + `ask_user` + `recommend` + `submit_risk` + `finish`） | 部分（§4.8.2 只规定 schema 来源） |
| **会话记录即记忆**（transcript 取代 `slots`/`shown`/`intent_version`/`clarifications`/`pending_id`/`last_holding`/`unresolved`） | ❌ 未覆盖 |
| **终答四条不变量**（真实性 / 不得声称未发生 / 不得泄露身份 / 预算） | ❌ 未覆盖 |
| 六个实测缺陷（对话 #59：D1 问卷无路可走、D2 主 Agent 编造入口、D3 事实不跨轮、D4 自相矛盾、D5 预算烧在重查上、D6 待办 5 分钟过期） | ❌ 未覆盖（可作 P4/P5 验收素材） |

**已选 (iii)**：都不并入，需要时从 git 提交 `b907542` 取回。（(i) 把三项 ❌ 并入 §4.8 作为验收依据、(ii) 只并六个缺陷作验收素材，均已被否决。）

**记录一条当时的约束**：「含模块改动」与「一项都不并入」不能同时选——wealth 今天的 `turn` 已经解决了一部分记忆问题，若把它的工具迁到通用循环却不带「记录即记忆」与「终答不变量」，D1–D6 会以另一种形式复现，那是回归不是改进。**本 spec 两者都不做**（既不迁 wealth，也不并素材），因此不冲突。

---

## 11. 参考资料

本 spec 的外部依据（v2 已逐条核对原文）：

- Anthropic 流式协议与 delta 类型（`text_delta` / `input_json_delta` / `thinking_delta` / `signature_delta`，以及 "new event types may be added, and your code should handle unknown event types gracefully"）：<https://platform.claude.com/docs/en/build-with-claude/streaming>
- 工具调用生命周期，含「文本块 + `tool_use` 同在一个 assistant turn」的标准示例：<https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls>
- `strict: true` 的语义（grammar-constrained sampling，"guarantees Claude's tool inputs match your JSON Schema"）：<https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use>
- 并行 tool call（"can contain several `tool_use` blocks in a single assistant turn"、"return one `tool_result` for each `tool_use` block"）：<https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use>
- Claude Code / Agent SDK 消息模型（`AssistantMessage` = `TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock` 列表；`ResultMessage.subtype` / `is_error` / `terminal_reason`；`include_partial_messages` 默认 `False`；`forward_subagent_text` 默认 `False`）：<https://code.claude.com/docs/en/agent-sdk/python>
- Claude Code 子 agent 模式（主 agent 经 Task 工具派发，子 agent 领 prompt、自带工具循环、跑完交报告、过程文本默认不转发）：出处同上 Agent SDK 文档——§1.5 终态与 §4.8 对齐的原型。

> 取证备注：`docs.claude.com` 与 `code.claude.com` 都会 308 跳到别的主机，直接抓 `platform.claude.com/.../<page>.md` 才能拿到原始 markdown；Agent SDK 的页面只在 `code.claude.com/docs/en/agent-sdk/` 下。
