# Spec — `langgraph-supervisor` 引入 spike（A6-b，独立立项）

- 状态：**未启动（deferred）** —— 本文件只登记工作项与门禁，不含实施计划
- 日期：2026-10-14
- 来源：`docs/superpowers/specs/2026-10-13-architecture-optimization-roadmap-spec.md` §4「A6-b」与 §8 裁决点 2
- 原位置：`docs/superpowers/plans/2026-10-13-architecture-optimization-plan.md` Task 8 —— **已从该计划移出**，落地于本文件
- 相关：
  - `docs/superpowers/specs/2026-10-13-architecture-optimization-roadmap-spec.md`（需求 R-7）
  - `docs/ARCHITECTURE.md`
  - `backend/app/agents/supervisor/`、`backend/app/agents/hitl.py`、`backend/app/agents/subgraph_templates.py`
  - 外部参照：[langgraph-supervisor](https://github.com/langchain-ai/langgraph-supervisor)

---

## 0. 为什么单独立项

**裁决（2026-10-14，用户决定）：A6-b 先不做；从架构优化计划移出，单独立项，就是本文件。**

理由：

1. A6-b 是路线图里**可选**的工作包。§8 裁决点 2 的结论是「A6-a 完成后再开；默认不阻塞比赛演示」。
2. 架构优化计划 Task 1–7 已收口，A6-a 已落地（见 §1）。把一个**未启动的可选 spike** 留在计划里，会让「该计划是否完成」永远悬着，也让它的两个未勾步骤看起来像欠债。
3. spike 的产物是**一份评审报告 + 一次裁决**，不是可交付代码；「通过才允许换依赖」这条门禁需要独立记录，不能挂在别的计划的验收里。

本文件在 spike 启动前是**登记**，启动后就是**该 spike 的 spec**。

---

## 1. 已具备的前提（A6-a 交付，2026-10-14 核实）

A6-a（supervisor 拆分）已经完成，这既是 spike 的输入，也是它的对照组：

| 事实 | 位置 |
| --- | --- |
| supervisor 已是包：surface 再导出 + 35 名 `__all__` | `backend/app/agents/supervisor/__init__.py`（99 行 / 非空 92） |
| 路由 + 构图，**非空 369 行 < 400**（A6-a 的行数目标） | `backend/app/agents/supervisor/graph.py`（436 / 369）；`build_supervisor` :359、`build_domain_graph` :400 |
| handoff 环：用户轮次规则、delta 合并器、流式决策轮、决策翻译 | `backend/app/agents/supervisor/handoff.py`（139 / 110） |
| interrupt payload ↔ HTTP `/decide` 适配 | `backend/app/agents/hitl.py`（101 / 84） |
| 无工具空壳领域的极简模板（单 `conclude` 节点、不调模型、不 interrupt） | `backend/app/agents/subgraph_templates.py`（59 / 46） |
| handoff 工具定义 | `backend/app/agents/prompts.py:14 HANDOFF_TOOL_NAME = "handoff"`、`:132 def handoff_tool(agents: list[tuple[str, str]]) -> dict[str, Any]`（`strict: True`，properties `agent_id` + `instruction`，`required` 二者） |
| 决策解析 | `backend/app/agents/decisions.py` `parse_decision(text, tool_calls, known)` |
| 唯一的流式 tool call 收集器 | `backend/app/agents/toolcalls.py` `collect_stream` |
| 领域挂载：有业务工具必须声明 `graph_builder`，否则 `raise ValueError` | `backend/app/agents/supervisor/graph.py` 的 `build_domain_graph`；card / transfer / wealth 挂自有图，bill / life_event / subscription 挂 `minimal_shell_graph` |

spike 必须跨过的三条现有约束（不是可选项）：

- **冻结的 SSE 事件词汇表**：`backend/app/agents/runtime.py:40-54` 的 `EVENT_FIELDS`（`agent_start` / `tool_call` / `tool_result` / `interrupt` / `subtask_end` / `reply_delta` / `narration` / `final`）。注释写明：**不在这张表里的事件会在运行时抛错**，表外的字段会被丢弃。
- **checkpointer 与 thread**：`backend/app/storage/checkpointer.py:26 LockedAsyncPostgresSaver(AsyncPostgresSaver)`、`:70 from_conn_string(checkpointer_url(...))`；生产由 `backend/app/main.py` lifespan 装配（`require_checkpoint_tables` → `postgres_checkpointer` → `rt.agent.use_checkpointer(saver)`）。一个 conversation 一个 thread：`backend/app/conversation/service.py:70-73 thread_id_for_conversation -> f"conv:{conversation_id}"`，旧会话 `chat:{session_id}`。
- **上下文与工具注入**：`app.tools.protocol.TrustedContext`；工具经 `backend/app/tools/gateway.py` 的 `ToolGateway.handle(agent_id, ...)` 按 catalog 的 per-agent grants 下发（`backend/app/tools/catalog.py:50-67 build`、`:69 for_agent`）。

---

## 2. spike 必答

继承路线图 §4「A6-b」与计划 Task 8，逐条给结论与证据：

1. **Postgres checkpointer 是否可用？** 官方 supervisor 作父图时，我们的 `graph_builder` 编译产物仍作为子图挂载，且共用同一个 saver 与 `thread_id`。
2. **自定义 handoff 指令字段能否保留？** 现在 handoff 工具带 `agent_id` + `instruction`；官方预置的 handoff 工具是否允许等价字段（或需要自定义工具）。
3. **interrupt 跨子图 resume 能否保留？** 现有确认卡：`kind=confirmation` + `POST /conversations/{conversation_id}/pending-actions/{pending_id}/decide`，靠 `Command(resume=...)` 恢复；官方 supervisor 是否仍把 `interrupt` 冒泡到父图。
4. **现有 SSE 事件模型能否适配？** `EVENT_FIELDS` 是冻结词汇表；官方 supervisor 若直接往 custom stream 写自己的事件，需要适配层，否则运行时抛错。
5. **`TrustedContext` / Tool Gateway 如何注入？** 现在由 runtime 构造并随每请求下发；官方 supervisor 的节点是否有等价缝。
6. **合入成本 vs 留在 A6-a？** 明确比较：改动面、回归面、行数收益（A6-a 已把 `graph.py` 收到非空 369 行）。

---

## 3. 门禁与产物

- **分支**：spike 分支，**限时 1–2 天**。不合并、不双轨。
- **产物**：`docs/superpowers/reviews/YYYY-MM-DD-langgraph-supervisor-spike.md`（`docs/superpowers/reviews/` 已存在，沿用其日期命名）。
- **通过才允许换依赖**；不通过则**关闭分支**，保留 A6-a，不保留任何半成品接线。
- 通过后**另立 spec**「引入官方 supervisor」，不在本文件里直接大爆炸合并。

---

## 4. 非目标

- 本文件不改任何生产代码。
- spike 期间不改 `backend/app/modules/**` 的业务逻辑（领域图作为被调 agent 传入，而不是再包一层）。
- 不把 A6-b 的成功当作架构优化计划的收口条件。

---

## 5. 启动条件

- A6-a 已完成：`graph.py` 非空 < 400、模块零引用 `app.agents.supervisor`、唯一确认入口、唯一写门面 —— **均已达成（2026-10-14）**。
- 启动前需人工确认，并把本文件状态从「未启动」改为「进行中」。

---

## 6. 裁决记录

| 日期 | 裁决 | 依据 |
| --- | --- | --- |
| 2026-10-13 | A6-b 默认不阻塞比赛演示；A6-a 完成后再开 | 路线图 §8 裁决点 2 |
| 2026-10-14 | **不启动**；从架构优化计划 Task 8 移出，单独立项（本文件） | 用户决定 |

---
