"""A5 §3 / Ruling R11：理财只读流程不是第二条暂停/恢复通道。

`build_read_graph` 不带检查点，运行时用 `invoke_read_workflow` 直接 `invoke` 它。
这与 A5 §3「禁止 builder 对领域图做脱离父图 checkpointer 的 invoke」不冲突，因为：

1. 它 `compile()` 出来的**不是领域图**。领域图是 `build_wealth_graph`，它带 `wait`（平台的
   `wait_for_user`）并被父图挂载；只读图一个可暂停节点都没有。
2. 它**从不暂停**：追问走 `ask_user -> END`，由模块自己铸 `pending_id`（`nodes.py:846-868`
   只往 state 里写 `wealth_asked_question` / `missing_slots`），不用 `interrupt()`。
   没有第二条暂停态，也就没有第二份会被进程重启丢掉或分裂的记忆。

这组守卫把上面两条钉住：谁给只读图挂上 `wait_for_user`、或者给它一个 checkpointer，
只读流程就变成与领域图并列的第二条暂停/恢复路径，这些用例会红。

为什么不改写它：`WealthWorkflowState`（`state.py:458-512`）的 28 个通道里有本轮计数器
（`steps` / `model_calls` / `corrections` / `reads` / `gate_violations`）与累积的
`observations`，是一次性草稿；父状态里那个 `wealth_work` 通道全仓零使用。把它挂进检查点
等于把「本轮计数器」持久化——正是 spec 抱怨的「状态一半在检查点、一半在内存」的反向版本。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthStatus
from app.modules.wealth.infrastructure.tools import READ_TOOLS
from app.modules.wealth.workflow.graph import build_read_graph, build_wealth_graph
from tests.wealth import support

# 只读图的骨架：11 个工作节点 + LangGraph 的首尾。**没有 `wait`**。
# 只钉"这些必须在、且 `wait` 不在"：多加节点由 `test_the_wealth_module_never_reaches_for_interrupt`
# 与下面的行为用例兜，冻结整集只会让正常的重构无谓变红（审查 F-2）。
READ_GRAPH_NODES = frozenset(
    {
        "__start__",
        "understand",
        "decide",
        "call_read",
        "set_slots",
        "submit_risk",
        "recommend",
        "compare",
        "prepare_purchase",
        "prepare_redeem",
        "ask_user",
        "finish",
        "__end__",
    }
)

# 三个领域图共用的暂停节点名（`backend/app/modules/wealth/workflow/graph.py:408`）。
PAUSE_NODE = "wait"


def test_the_read_graph_has_no_pausable_node(bank, context):
    """只读图里没有可暂停节点，而且它确实是那张只读图（骨架节点一个不少）。"""

    graph = build_read_graph(support.ScriptModel(), bank, context, tools=READ_TOOLS)

    nodes = set(graph.get_graph().nodes)
    assert PAUSE_NODE not in nodes
    assert READ_GRAPH_NODES <= nodes


def test_the_read_graph_carries_no_checkpointer(bank, context):
    """只读图不是持久化路径：检查点只属于父图，由父图下发给领域子图。"""

    graph = build_read_graph(support.ScriptModel(), bank, context, tools=READ_TOOLS)

    assert graph.checkpointer is None


def test_the_wealth_domain_graph_is_the_one_that_pauses(bank, double, context, clock):
    """对照组：理财的**领域图**确实带暂停节点，所以上面两条断言不是恒真。"""

    inner = build_wealth_graph(
        support.ScriptModel(),
        support.wealth_domain_spec(double, context),
        client=double.client(),
        clock=clock,
    )

    assert PAUSE_NODE in set(inner.get_graph().nodes)
    # 领域图自己也不带检查点：它被父图挂载，检查点由父图下发（A5 §3）。
    assert inner.checkpointer is None


def test_the_read_flow_asks_the_user_without_interrupting(bank, double, clock):
    """走追问分支整轮跑通。

    只读图没有检查点，所以如果 `ask_user` 改用 `interrupt()`，这次 `invoke` 会直接抛错——
    用例跑得完本身就是「追问不是图级暂停」的证据。
    """

    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
        support.finish("那建议先看可灵活赎回的产品。"),
    )

    asked, session, pending = build_wealth_agent(model, bank, clock).handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session={},
        pending=None,
    )

    assert asked.status is WealthStatus.WAITING_INPUT
    assert asked.pending is not None
    assert asked.pending.prompt == "请问您计划什么时候用这笔钱？"


WEALTH_ROOT = Path(__file__).resolve().parents[2] / "app" / "modules" / "wealth"


def _interrupt_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "interrupt":
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "interrupt":
            lines.append(node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] == "interrupt":
                    lines.append(node.lineno)
    return lines


def test_the_wealth_module_never_reaches_for_interrupt():
    """静态半：`app/modules/wealth/**` 里没有 `interrupt` 这个入口（别名导入也算）。

    只读图不带检查点，所以任何一处 `interrupt()` 都会在运行时炸成 FAILED——但那只在
    **被走到** 的分支上成立。审查 F-1 正是这么绕过去的：给 `ask_user_node.run()` 加一句
    `if state.get("pending_prompt"): interrupt({...})`，只驱动第一轮的守卫 4 条全绿。
    这条扫描不看分支，只看代码里有没有这个入口。

    领域图暂停走的是平台的 `wait_for_user`（`app.agents.sdk`），不是裸 `interrupt`，
    所以这个禁令不会挡住领域图。
    """

    offenders = [
        f"{path.relative_to(WEALTH_ROOT)}:{line}"
        for path in sorted(WEALTH_ROOT.rglob("*.py"))
        for line in _interrupt_lines(path)
    ]
    assert offenders == []


def test_the_read_flow_does_not_pause_on_a_turn_that_resumes_an_old_question(bank, double, clock):
    """行为半：第二轮带着 `pending_prompt` 进图，那是 F-1 藏 interrupt 的那条分支。

    只读图没有检查点：这一轮里任何一处 `interrupt()` 都会抛错，被 `runtime.py:529` 吞成
    FAILED。所以"这一轮没有失败"就是"这一轮没有暂停"的证据。
    """

    model = support.ScriptModel(
        support.ask_user("请问您计划什么时候用这笔钱？", ["horizon_months"]),
        # 第二轮必须也落到 `ask_user`：F-1 的 interrupt 就藏在 `ask_user_node.run()` 里。
        support.ask_user("那我按哪种期限帮您筛？", ["horizon_months"]),
        support.finish("好的，先按短期看。"),
    )
    agent = build_wealth_agent(model, bank, clock)

    asked, session, pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="推荐点理财"),
        session={},
        pending=None,
    )
    assert asked.status is WealthStatus.WAITING_INPUT

    again, _session, _pending = agent.handle(
        support.bound_context(),
        WealthRequest(kind="message", text="先随便看看别的"),
        session=session,
        pending=pending,
    )

    assert again.status is not WealthStatus.FAILED
