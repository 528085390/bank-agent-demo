"""Reading a tool-calling turn: where the request goes, and what to say about it.

The two things this file exists to pin down are both easy to get subtly wrong: text next to a
handoff is narration rather than the answer (so it must not be published as the reply), and a
turn with two handoffs must not silently drop the second one.
"""

from __future__ import annotations

import pytest

from app.agents.decisions import DecisionError, Handoff, parse_decision
from app.agents.prompts import HANDOFF_TOOL_NAME, SUPERVISOR_PROMPT, describe_agents, handoff_tool
from app.agents.supervisor import supervisor_user_turn
from app.agents.supervisor.graph import _build_task_plan_from_decision
from app.integration.model_client import ToolCall
from app.modules.registry import build_registry
from app.modules.wealth import MODULE as WEALTH_MODULE

KNOWN = {"transfer", "bill", "card", "subscription", "life_event", "wealth"}
MENU = [("transfer", "转账"), ("wealth", "理财持仓与推荐")]


def call(agent_id: str | None = "wealth", instruction: str = "查一下持仓") -> ToolCall:
    arguments = {"instruction": instruction}
    if agent_id is not None:
        arguments["agent_id"] = agent_id
    return ToolCall(name=HANDOFF_TOOL_NAME, arguments=arguments)


def test_a_single_handoff_becomes_the_dispatch_target():
    decision = parse_decision("", [call()], KNOWN)

    assert decision.agent_id == "wealth"
    assert decision.instruction == "查一下持仓"
    assert decision.reply == ""
    assert decision.narration == ""
    assert decision.queued == ()


def test_text_alongside_a_handoff_is_narration_not_the_answer():
    """The documented shape: a text block first, the tool call next."""

    decision = parse_decision("我先帮你看看持仓。", [call()], KNOWN)

    assert decision.agent_id == "wealth"
    assert decision.narration == "我先帮你看看持仓。"
    assert decision.reply == ""


def test_two_handoffs_dispatch_the_first_and_queue_the_rest():
    decision = parse_decision(
        "", [call("transfer", "转 100 元给张三"), call("wealth", "顺便看看持仓")], KNOWN
    )

    assert decision.agent_id == "transfer"
    assert decision.instruction == "转 100 元给张三"
    assert decision.queued == (Handoff(agent_id="wealth", instruction="顺便看看持仓"),)


def test_a_handoff_without_an_instruction_is_still_a_handoff():
    decision = parse_decision(
        "", [ToolCall(name=HANDOFF_TOOL_NAME, arguments={"agent_id": "bill"})], KNOWN
    )

    assert decision.agent_id == "bill"
    assert decision.instruction == ""


def test_an_unknown_agent_is_refused():
    with pytest.raises(DecisionError) as raised:
        parse_decision("", [call("nope")], KNOWN)

    assert "未知 Agent" in str(raised.value)


def test_a_handoff_without_an_agent_is_refused():
    with pytest.raises(DecisionError) as raised:
        parse_decision("", [call(None)], KNOWN)

    assert "没有指明" in str(raised.value)


def test_an_unknown_tool_is_a_protocol_error():
    """Silently ignoring it would turn a provider bug into a mystery."""

    with pytest.raises(DecisionError) as raised:
        parse_decision("", [ToolCall(name="run_sql", arguments={})], KNOWN)

    assert "未知工具" in str(raised.value)


def test_text_without_a_handoff_is_the_reply():
    decision = parse_decision("  你好，有什么可以帮你的？  ", [], KNOWN)

    assert decision.agent_id is None
    assert decision.reply == "你好，有什么可以帮你的？"
    assert decision.narration == ""


def test_empty_text_without_a_handoff_leaves_the_fallback_to_the_caller():
    """decisions.py must not own the fallback: supervisor.py imports it, not the other way."""

    decision = parse_decision("   ", [], KNOWN)

    assert decision.agent_id is None
    assert decision.reply == ""


def test_the_handoff_schema_is_strict_and_lists_only_known_agents():
    tool = handoff_tool(MENU)

    function = tool["function"]
    assert function["name"] == HANDOFF_TOOL_NAME
    assert function["strict"] is True
    assert function["parameters"]["properties"]["agent_id"]["enum"] == ["transfer", "wealth"]
    assert function["parameters"]["required"] == ["agent_id", "instruction"]
    assert function["parameters"]["additionalProperties"] is False
    assert "理财" in function["description"]


def test_the_supervisor_prompt_no_longer_asks_for_json():
    """The prompt must not describe a shape the protocol does not have any more."""

    assert "JSON" not in SUPERVISOR_PROMPT
    assert "空对象" not in SUPERVISOR_PROMPT
    assert HANDOFF_TOOL_NAME in SUPERVISOR_PROMPT
    assert "打招呼" in SUPERVISOR_PROMPT


def test_the_turn_carries_no_shape_paragraph():
    """The tools are the only place the shape is stated, so the prompt must not restate it."""

    turn = supervisor_user_turn("查一下持仓", MENU)

    assert HANDOFF_TOOL_NAME in turn
    assert "JSON" not in turn


def test_the_supervisor_menu_tells_wealth_that_it_handles_orders():
    """supervisor 只看得到 purpose，菜单里没有「订单」这个词，订单请求就永远到不了 wealth。"""

    registry = build_registry([WEALTH_MODULE])
    menu = describe_agents([(item.agent_id, item.purpose) for item in registry.roster])

    assert "- wealth:" in menu
    assert "订单" in menu


def test_the_wealth_menu_names_the_balance_and_risk_tools_it_carries():
    """wealth_accounts / wealth_risk_profile 是真实工具；菜单里不写，supervisor 就不会派这类请求过来。"""

    registry = build_registry([WEALTH_MODULE])
    menu = describe_agents([(item.agent_id, item.purpose) for item in registry.roster])

    assert "余额" in menu
    assert "风险测评" in menu


def test_multi_handoff_plan_has_independent_labels_and_linear_dependencies():
    """断言一次派发两个 handoff 时，TaskPlan 的两个节点有清晰独立 label 且依赖线性。"""
    decision = parse_decision(
        "",
        [
            call("bill", "查询最近一周支出账单"),
            call("transfer", "向张三转账 200 元"),
        ],
        KNOWN,
    )
    plan = _build_task_plan_from_decision(decision)
    assert plan is not None
    assert len(plan.nodes) == 2
    assert plan.nodes[0].owner == "bill"
    assert "账单查询与分析 · 查询最近一周支出账单" in plan.nodes[0].label
    assert plan.nodes[1].owner == "transfer"
    assert "资金转账办理 · 向张三转账 200 元" in plan.nodes[1].label
    assert plan.nodes[1].depends_on == ("node_1",)
