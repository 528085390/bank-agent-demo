"""Analysis reports ride with the assistant message.

Another device has no copy of the browser-local report, so the turn writes the full
`bill_analyze_comprehensive` display into the assistant message's JSONB metadata. These tests
pin the narrow part of that seam: only successful analysis results are stored, duplicates from a
retried tool call collapse to one, and an oversized report is dropped rather than bloating the
ledger.
"""

import asyncio

from app.agents.runtime import Admission, AgentEvent
from app.conversation.service import Receipt
from app.conversation.turns import (
    MAX_ANALYSIS_REPORT_BYTES,
    ConversationTurns,
    TurnPlan,
)


def report(generated_at: str = "2026-09-01T10:00:00Z") -> dict:
    return {
        "report_meta": {
            "date_range": {"start": "2026-08-01", "end": "2026-08-31"},
            "total_transactions": 128,
            "accounts": ["****1234"],
            "generated_at": generated_at,
        },
        "modules": [{"module_id": "hourly_activity", "title": "时段", "data": {}, "insights": []}],
        "text_report": "2026 年 8 月流水分析报告",
    }


def tool_result(
    display: dict | None,
    *,
    tool_name: str = "bill_analyze_comprehensive",
    error: str | None = None,
) -> AgentEvent:
    return AgentEvent(
        event="tool_result",
        data={
            "agent_id": "bill",
            "tool_name": tool_name,
            "call_id": "call_1",
            "status": "ok" if error is None else "error",
            "data": {"summary": "分析完成", "display": display, "business_ids": None},
            "error": error,
            "error_code": None,
        },
    )


FINAL = AgentEvent(event="final", data={"text": "已经完成", "business_ids": []})


class FakeService:
    """The two writes `stream` makes, recorded instead of hitting a database."""

    def __init__(self) -> None:
        self.reply: dict | None = None

    def attach_run(self, user_id, receipt, run_id) -> None:
        pass

    def append_assistant_message(
        self, user_id, conversation_id, *, run_id, content, metadata=None
    ) -> None:
        self.reply = {"content": content, "metadata": metadata}


class FakeRuntime:
    """Yields the scripted events the graph would have emitted."""

    audit = None

    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = events

    async def invoke(self, *args, **kwargs):
        for event in self.events:
            yield event


def plan() -> TurnPlan:
    return TurnPlan(
        conversation_id="conv",
        thread_id="thread",
        receipt=Receipt("sub_1", "msg_1", None, "conv", 1),
        message="分析八月流水",
        client_context=None,
        admission=Admission(resuming=False),
    )


def run(events: list[AgentEvent]) -> FakeService:
    service = FakeService()
    turns = ConversationTurns(service, FakeRuntime(events))

    async def consume() -> None:
        async for _event in turns.stream("user_1", "session_1", plan()):
            pass

    asyncio.run(consume())
    assert service.reply is not None
    return service


def test_a_successful_analysis_report_is_stored_with_the_reply():
    service = run([tool_result(report()), FINAL])

    stored = service.reply["metadata"]["analysis_reports"]
    assert stored == [report()]


def test_other_tools_and_failed_analysis_are_not_stored():
    service = run(
        [
            tool_result(report(), tool_name="bill_query_transactions"),
            tool_result(report(), error="分析失败"),
            FINAL,
        ]
    )

    assert "analysis_reports" not in service.reply["metadata"]


def test_a_retried_analysis_collapses_to_one_stored_report():
    service = run([tool_result(report()), tool_result(report()), FINAL])

    assert len(service.reply["metadata"]["analysis_reports"]) == 1


def test_an_oversized_report_is_dropped():
    oversized = report()
    oversized["text_report"] = "x" * (MAX_ANALYSIS_REPORT_BYTES + 100)

    service = run([tool_result(oversized), FINAL])

    assert "analysis_reports" not in service.reply["metadata"]


def test_the_reply_text_is_kept_even_when_the_report_is_too_large():
    oversized = report()
    oversized["text_report"] = "x" * (MAX_ANALYSIS_REPORT_BYTES + 100)

    service = run([tool_result(oversized), FINAL])

    assert service.reply["content"] == "已经完成"
    # The reply still carries its ordinary metadata; only the report was left out.
    assert service.reply["metadata"] == {"business_ids": []}


def query_display(count: int = 25) -> dict:
    return {
        "total": count,
        "page": 1,
        "page_size": 20,
        "period_start": "2026-05-05",
        "period_end": "2026-05-06",
        "items": [
            {
                "booked_date": "2026-05-05",
                "amount_minor": 3250,
                "direction": "debit",
                "transaction_type": "消费-餐饮",
                "counterparty": "美团点评-肠粉王店",
                "category": "餐饮",
            }
            for _ in range(count)
        ],
    }


def test_a_query_result_keeps_only_the_current_page_of_at_most_20_rows():
    service = run(
        [
            tool_result(query_display(), tool_name="bill_query_transactions"),
            FINAL,
        ]
    )

    stored = service.reply["metadata"]["query_results"]
    assert len(stored) == 1
    assert stored[0]["period_start"] == "2026-05-05"
    assert stored[0]["period_end"] == "2026-05-06"
    assert stored[0]["total"] == 25
    assert len(stored[0]["items"]) == 20


def test_a_query_result_remembers_the_task_it_was_asked_for():
    """按文件查的明细把任务号一起存下来：恢复后的表格靠它直接翻页。"""

    display = {**query_display(3), "task_id": "task_abc123"}
    service = run(
        [
            tool_result(display, tool_name="bill_query_transactions"),
            FINAL,
        ]
    )

    stored = service.reply["metadata"]["query_results"]
    assert stored[0]["task_id"] == "task_abc123"


def test_two_tasks_with_the_same_page_and_total_are_not_merged():
    """指纹要带上任务号，否则不同账单的同一页会被当成重复结果丢掉。"""

    first = {**query_display(3), "task_id": "task_one"}
    second = {**query_display(3), "task_id": "task_two"}
    service = run(
        [
            tool_result(first, tool_name="bill_query_transactions"),
            tool_result(second, tool_name="bill_query_transactions"),
            FINAL,
        ]
    )

    stored = service.reply["metadata"]["query_results"]
    assert [item["task_id"] for item in stored] == ["task_one", "task_two"]
