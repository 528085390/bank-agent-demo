"""Grounding checks: a model summary may only repeat what the tools showed it."""

import json

from app.agents.grounding import guard_summary


def thread_with(result: str) -> list[dict]:
    """A domain thread as the model sees it: one tool envelope carrying the tool summary."""

    return [
        {
            "role": "user",
            "content": json.dumps(
                {"tool": "card_cards", "status": "SUCCEEDED", "result": result},
                ensure_ascii=False,
            ),
        }
    ]


def test_a_summary_that_repeats_tool_amounts_and_ids_is_kept():
    summary = "您名下有一张借记卡，可用额度 5,000.00 元，卡号尾号 8801。"
    result = guard_summary(
        summary,
        thread=thread_with("借记卡：额度 5,000.00 元；卡号 6222 **** **** 8801。"),
    )

    assert result.blocked is False
    assert result.summary == summary
    assert result.reason is None


def test_an_amount_absent_from_the_tool_result_is_blocked():
    result = guard_summary(
        "您的卡里还有 99,999.00 元。",
        thread=thread_with("借记卡：可用额度 5,000.00 元。"),
    )

    assert result.blocked is True
    assert "99,999" not in result.summary
    assert "无法与工具返回核对" in result.summary
    assert result.reason


def test_an_id_absent_from_the_tool_result_is_blocked():
    result = guard_summary(
        "转账已受理，交易号 txn_deadbeef99。",
        thread=thread_with("转账已受理，交易号 txn_abc12345。"),
    )

    assert result.blocked is True
    assert "txn_deadbeef99" not in result.summary
    assert "无法与工具返回核对" in result.summary


def test_a_rule_answer_without_any_tool_evidence_is_not_treated_as_hallucination():
    summary = "境内转账单笔超过 1,000.00 元需要验证码。"
    result = guard_summary(summary)

    assert result.blocked is False
    assert result.summary == summary


def test_the_users_own_amount_counts_as_evidence():
    result = guard_summary(
        "已按您说的 1,500.00 元办理。",
        thread=thread_with("转账已受理。"),
        user_text="给李四转账 1500 元",
    )

    assert result.blocked is False


def test_the_user_message_already_in_the_thread_counts_as_evidence():
    thread = [
        {"role": "user", "content": "指令：给李四转账 1500 元"},
        {
            "role": "user",
            "content": json.dumps(
                {"tool": "transfer_prepare", "status": "SUCCEEDED", "result": "转账已受理。"},
                ensure_ascii=False,
            ),
        },
    ]

    result = guard_summary("已按您说的 1,500.00 元办理。", thread=thread)

    assert result.blocked is False


def test_business_ids_and_fact_amounts_count_as_evidence():
    by_id = guard_summary(
        "取消任务 task_abc12345 已受理。",
        thread=thread_with("取消申请已提交。"),
        business_ids={"task_id": "task_abc12345"},
    )
    by_fact = guard_summary(
        "本次转账金额 1,500.00 元。",
        thread=thread_with("转账已受理。"),
        facts={"amount_minor": 150000},
    )

    assert by_id.blocked is False
    assert by_fact.blocked is False
