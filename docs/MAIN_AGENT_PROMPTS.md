# 主 Agent（Supervisor）提示词清单

采集时间：2026-10-10。目的：把当前所有会进入**主 Agent**（总调度 Supervisor）模型调用的提示词文本集中到一处，供审查。

> ⚠️ **这是 2026-10-10 的快照，部分内容已被后续改造取代。** 2026-10-12 起 Supervisor 与领域 Agent 改用**原生 tool calling**，JSON 文本协议已废除（见 [SPEC_SUPERVISOR_TOOL_CALLING.md](SPEC_SUPERVISOR_TOOL_CALLING.md)）。因此文中出现的 `SUPERVISOR_SHAPE` 常量、`{"agent": ...}` 输出形状**在当前代码中已不存在**；现在以 `backend/app/agents/prompts.py` 为准（交接走 `handoff_tool`，`backend/app/agents/prompts.py:132-168`）。

来源文件：
- `backend/app/agents/prompts.py` —— 提示词文本（数据层）
- `backend/app/agents/supervisor/graph.py` —— system 提示词渲染与每轮规则
- `backend/app/agents/supervisor/handoff.py` —— user turn 规则与决策流式拼装
- `backend/app/context/summary.py` —— 主 Agent 上下文压缩（摘要）用的提示词

---

## 0. 主 Agent 每次模型调用实际发出的两条消息

```
{"role":"system","content": <SUPERVISOR_PROMPT 渲染后>}
{"role":"user",  "content": <supervisor_user_turn 渲染后>}
```

- system 渲染点：`backend/app/agents/supervisor/graph.py:107-118`（`prompt_for`），固定开销版本在 `backend/app/agents/supervisor/graph.py:96-105`（`overhead_prompt`）
- user 渲染点：`backend/app/agents/supervisor/handoff.py:124` 调用 `supervisor_user_turn`
- 实际发消息：`backend/app/agents/supervisor/handoff.py:126`（`collect_stream`）
- 调用次数：每轮最多 1 次主 Agent 调用（`backend/app/agents/supervisor/graph.py:137-249` 的 `decide`）；超过 `MAX_DOMAIN_TURNS = 4`（`backend/app/agents/supervisor/graph.py:53`）时不再调模型，直接收尾

---

## 1. `SUPERVISOR_PROMPT` —— 主 Agent 的 system 提示词

定义：`backend/app/agents/prompts.py:13-44`

```text
你是银行智能体的总调度（Supervisor）。你不办理任何具体业务，也不调用任何工具。

历史摘要（覆盖较早的对话，可能为空）：
{summary}

对话记录：
{transcript}

已经获得的结构化结果：
{results}

可用领域 Agent：
{agents}

你的职责：理解用户目标，判断应该交给哪个领域 Agent；每次只选择一个，并给它一句明确的执行指令。
收到领域 Agent 的结果后，再决定是继续交给下一个领域 Agent，还是汇总回复用户。

只输出一个 JSON 对象，不要输出其他文字。
需要某个领域 Agent 继续工作时：
{"agent":"<上表中的 agent_id>","instruction":"给该 Agent 的执行指令"}
需要把结果汇总给用户时：
{"agent":null,"reply":"给用户看的完整回复"}
规则：
- agent 必须是上表中出现的 agent_id，不要编造。
- 一次只选一个 Agent；需要多个领域协作时，先选一个，拿到结果后再决定下一个。
- 已经拿到的结果不要重复索取；信息足够时立即用 agent=null 汇总回复。
- 已经给出结论的领域（结果里有 `turn_status`）不要再派给它；直接用 agent=null 把它的 `summary` 和 `facts` 转述给用户。
- `facts` 里的明细（产品名称、期限、预期收益、风险等级等）要讲给用户，但不得增删事实。
- 某个 Agent 的结果是「该能力当前尚未接入」时，它已经给出了结论：不要再把同一个请求交给同一个 Agent，直接用 agent=null 向用户说明该能力尚未接入。
- 汇总回复只能基于领域 Agent 返回的结构化结果，不要编造其中没有的数据。
- 用户只是打招呼、闲聊或表达感谢时，同样用 agent=null 给出一句友好的 reply；任何情况下都不要输出空对象。
```

四个槽位的填充方式：

| 槽位 | 来源 | 代码位置 |
|---|---|---|
| `{summary}` | 上下文摘要；无视图时为空串，有视图无摘要时为「（暂无）」 | `backend/app/agents/supervisor/graph.py:115` |
| `{transcript}` | 逐行 `role: content`，或 `ContextAssembler` 的裁剪视图 | `backend/app/agents/prompts.py:104-113`；`backend/app/agents/supervisor/graph.py:110-114` |
| `{results}` | `domain_results` 的 `json.dumps(..., ensure_ascii=False, indent=2)` | `backend/app/agents/prompts.py:124-129` |
| `{agents}` | `- id: purpose` 行；空菜单时「（没有可用的领域 Agent）」 | `backend/app/agents/prompts.py:116-121` |

---

## 2. 每轮 user turn 的规则拼接

定义：`backend/app/agents/supervisor/handoff.py:44-58`（`supervisor_user_turn`）

```python
def supervisor_user_turn(
    request: str, menu: list[tuple[str, str]], awaiting: str | None = None
) -> str:
    ids = "、".join(agent_id for agent_id, _ in menu)
    rules = [
        "你是总调度，不直接回答业务问题，必须判断该请求属于哪个领域。"
        "agent 只能从这几个里选：" + ids + "（只有闲聊、打招呼或表达感谢才用 null）。"
    ]
    if awaiting:
        rules.append(
            f"上一轮 {awaiting} 正在等用户补充信息；"
            f"如果用户这句话是在回答它，请仍然派给 {awaiting}。"
        )
    return user_turn(request, rule="\n".join(rules), shape=SUPERVISOR_SHAPE)
```

外层包装：`backend/app/agents/prompts.py:154-167`（`user_turn`），标签默认「本轮用户消息」。

实际渲染出来的 user 消息（以六个领域都在、无 awaiting 为例）：

```text
本轮用户消息：<用户原话，空则「（用户没有提供文字）」>

你是总调度，不直接回答业务问题，必须判断该请求属于哪个领域。agent 只能从这几个里选：transfer、bill、wealth、card、subscription、life_event（只有闲聊、打招呼或表达感谢才用 null）。
请只输出一个符合此结构的 JSON 对象：
{"agent":"<上表中的 agent_id 或 null>","instruction":"给该 Agent 的执行指令","reply":"给用户看的完整回复"}
```

有 `awaiting` 时中间多一行：

```text
上一轮 <agent_id> 正在等用户补充信息；如果用户这句话是在回答它，请仍然派给 <agent_id>。
```

输出形状常量：`SUPERVISOR_SHAPE`（**已于 2026-10-12 废除，当前代码中无此常量**；现为交接工具 `handoff_tool`，`backend/app/agents/prompts.py:132-168`）

```python
SUPERVISOR_SHAPE = (
    '{"agent":"<上表中的 agent_id 或 null>",'
    '"instruction":"给该 Agent 的执行指令",'
    '"reply":"给用户看的完整回复"}'
)
```

---

## 3. `{agents}` 菜单里的领域说明文本（属于主 Agent 提示词的一部分）

`backend/app/agents/prompts.py:108-113` 把每个模块的 `purpose` 渲染成 `- id: purpose`：

| agent_id | purpose 原文 | 定义位置 |
|---|---|---|
| `transfer` | 处理转账、汇款、收款人相关的请求；转账由代码要求确认或验证码 | `backend/app/modules/transfer/__init__.py:26` |
| `bill` | 处理账单查询、账单分析、消费分类相关的请求 | `backend/app/modules/bill/__init__.py:22` |
| `wealth` | 处理理财产品、持仓、收益、赎回、订单查询、账户余额与风险测评相关的请求 | `backend/app/modules/wealth/__init__.py:26` |
| `card` | 处理银行卡查询、申请、日/月交易限额、冻结、解冻、商户黑名单、挂失和解挂请求；写操作由代码要求确认或验证 | `backend/app/modules/card/__init__.py:13` |
| `subscription` | 处理订阅查询、识别、续费提醒与取消/暂停/恢复/代扣相关的请求 | `backend/app/modules/subscription/__init__.py:22` |
| `life_event` | 处理生日、节日、生活提醒等事件相关的请求 | `backend/app/modules/life_event/__init__.py:17` |

---

## 4. 不经过模型的主 Agent 收尾/回退文案

这些不是提示词，但决定了主 Agent 对用户说的话，审查提示词时应一并看：

- `_closing_reply`（`backend/app/agents/supervisor/graph.py:305-322`）：把各领域的 `summary` 去重后用「；」连接；一条都没有时输出「抱歉，这个请求暂时没能完成，请换一种说法再试一次。」
- `NO_TOOLS_SUMMARY = "该能力当前尚未接入，暂时无法处理此请求。"`（`backend/app/agents/subgraph_templates.py:30`）
- 领域已给出结论但主 Agent 模型失败时的回退：`backend/app/agents/supervisor/graph.py:217-219`
- `MAX_DOMAIN_TURNS = 4`（`backend/app/agents/supervisor/graph.py:53`）：超过后 `_final_from` 不再调模型

---

## 5. 主 Agent 上下文记忆（摘要）提示词

虽然由 `app.context` 发起，但它产出的是主 Agent `{summary}` 槽位的内容，属于主 Agent 提示词链。

```text
你是对话历史的压缩器。把下面的对话并入已有摘要，输出一段供后续对话使用的摘要。

只保留后续对话需要的事实：
- 用户的目标和已确认的偏好
- 已经完成的动作及其业务 ID
- 尚未完成的事项
- 已经回答过的问题
- 用户明确说过的约束

规则：
- 不得写入源对话中不存在的事实，不要推测，不要补充常识。
- 不要保留寒暄、重复表述，以及已被后续对话推翻的内容。
- 工具的内部细节不要写入，只保留工具给出的公开结论和必要的业务 ID。

已有摘要（可能为空，代表更早的历史）：
{previous}

本次需要并入的对话：
{conversation}

只输出一个 JSON 对象，不要输出其他文字：{"summary":"压缩后的摘要"}
```

- `SUMMARY_PROMPT`：`backend/app/agents/prompts.py:74-95`
- user turn 模板 `SUMMARY_USER_TEMPLATE`：`backend/app/context/summary.py:61-66`
- 无历史时的占位 `NO_PREVIOUS_SUMMARY = "（无，这是最早的历史）"`：`backend/app/context/summary.py:70`
- 调用点 `_summarize_chunk`：`backend/app/context/summary.py:236-264`

---

## 6. 规则化清单（把上面提示词里的硬约束逐条摘出来，便于判断"是否过死"）

system 提示词内（`backend/app/agents/prompts.py:13-44`）：

1. 只输出一个 JSON 对象，不要输出其他文字。
2. agent 必须是上表中出现的 agent_id，不要编造。
3. 一次只选一个 Agent。
4. 已经拿到的结果不要重复索取。
5. 已给出结论的领域（有 `turn_status`）不要再派给它。
6. `facts` 明细要讲给用户，但不得增删事实。
7. 结果为「该能力当前尚未接入」时不要再派同一个 Agent。
8. 汇总回复只能基于结构化结果，不要编造数据。
9. 任何情况下都不要输出空对象。

user turn 内（`backend/app/agents/supervisor/handoff.py:44-58`）：

10. 你是总调度，不直接回答业务问题，必须判断该请求属于哪个领域。
11. agent 只能从固定名单里选（只有闲聊、打招呼或表达感谢才用 null）。
12. 有 awaiting 时：如果用户是在回答它，请仍然派给它。

另有两条由代码而非提示词施加的硬边界：`MAX_DOMAIN_TURNS = 4`（`backend/app/agents/supervisor/graph.py:53`）、模型输出解析失败即 `DecisionError`（`backend/app/agents/decisions.py`）。

---

## 7. 附：相邻但**不属于**主 Agent 的提示词（备查）

| 提示词 | 位置 | 归属 |
|---|---|---|
| `DOMAIN_PROMPT`（六个领域 Agent 的 system 提示词） | `backend/app/agents/prompts.py:46-71` | 领域 Agent |
| `DOMAIN_SHAPE` | `backend/app/agents/prompts.py:147-151` | 领域 Agent |
| `domain_user_turn` | `backend/tests/support/tool_domain_graph.py:107` | 领域 Agent（生产已删：基座通用循环随 A5 移除，只有测试支持复刻它） |
| `READ_PROMPT` / `USER_TURN` / `RESUME_NOTICE` | `backend/app/modules/wealth/llm/prompts.py:24,26` | 理财整轮 Agent（结论回灌主 Agent 的 `domain_results`） |
| ~~`TITLE_SYSTEM_PROMPT`~~ | 已删除（2026-10-28） | 会话标题改为从首句本地截取：`backend/app/conversation/titling.py` 的 `derive_title()`，不再有提示词、不再调模型 |
| `PROBE_MESSAGES` | `backend/app/integration/llm_config.py:40-43` | 模型连通性探测 |
