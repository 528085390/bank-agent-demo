"""bill 模块的形态：声明里不带工具，装配发生在组合根。

三条钉子：
1. import 期的 `MODULE` 不声明任何工具 —— `app/agents/domain.py` 在 import 期就
   跑 `build_registry().roster`，模块自己带工具会把服务依赖拖进 import 链。
2. 装配后的 bill 模块拥有四个 `bill_` 前缀工具，且工具键集与 handler 键集相等。
3. 工具只在组合根（`production_modules`）装配：默认 `build_registry()` 看不到它们。
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.modules import production_modules
from app.modules.base import ToolData, ToolFailure
from app.modules.bill import MODULE, build_bill_module
from app.modules.email_handlers import create_email_handlers
from app.modules.registry import build_registry
from app.tools.protocol import ErrorCode, ToolCall, ToolInvocation, TrustedContext


class FakeDb:
    """数据库句柄的替身：handler 只在被调用时才碰它，装配时只要非空。"""


class FakeNotifier:
    """NotificationService 的替身：装配只要求它非空，发信要能证明没发生。

    `sent` 记录每一次 `send_email` 的关键参数 —— 冻结（prepare）阶段的全部意义就是
    这个列表保持为空。
    """

    def __init__(self, *, bound_email: str | None = "user@example.com") -> None:
        self.database = FakeDb()
        self._bound_email = bound_email
        self.sent: list[dict] = []
        self.result: dict = {"notification_id": "email_fake", "status": "SENT", "queued": False}

    def get_user_email(self, user_id: str) -> str | None:
        return self._bound_email

    def send_email(self, **kwargs) -> dict:
        self.sent.append(kwargs)
        return self.result


def invocation(period_start="2026-09-01", period_end="2026-09-30", recipient=None):
    """一个最小可用的 ToolInvocation：handler 只读 `call.arguments` 与 `context.user_id`。"""

    return ToolInvocation(
        agent_id="bill",
        call=ToolCall(
            tool_name="bill_send_report",
            arguments={
                "period_start": period_start,
                "period_end": period_end,
                "recipient": recipient,
            },
            call_id=f"call_{uuid4().hex}",
        ),
        context=TrustedContext(user_id="user_0001", session_id="sess_bill"),
    )


def test_the_import_time_module_declares_no_tools():
    # 用 `== {}` 而不是 `== ()`：`ModuleSpec.tools` 的默认值是
    # `field(default_factory=dict)`（modules/base.py:108-109），空映射是 `{}`，
    # 跟空元组永远不相等。
    assert MODULE.tools == {} and MODULE.handlers == {}


def test_the_production_module_owns_four_prefixed_tools_with_matching_handlers():
    # 传 `database=FakeDb()`：handler 是「依赖非空才登记」的，`database=None` 时
    # 只有邮件工具，明细查询与深度分析两个 handler 根本不会出现。
    module = build_bill_module(notification_service=FakeNotifier(), database=FakeDb())
    # Task 9 把发信拆成 prepare / execute：第 4 个是隐藏的 `bill_send_report_execute`，
    # 它同样要登记 handler —— Gateway 的绑定校验要它（tools.py 的 `build_handlers`）。
    assert set(module.tools) == set(module.handlers) == {
        "bill_query_transactions",
        "bill_analyze_comprehensive",
        "bill_send_report",
        "bill_send_report_execute",
    }
    assert all(re.fullmatch(r"bill_[a-z_]+", name) for name in module.tools)


def test_the_production_registry_reaches_the_bill_tools_but_the_bare_one_does_not():
    # 这条钉住「装配放组合根」这个修复：放回 build_registry 内部它就红。
    # `catalog.for_agent()` 在该 agent 没有任何授权工具时返回 None（tools/catalog.py:69-73），
    # 所以声明版 bill 这里拿到的是 None，不能直接取 .names。
    bare = build_registry().catalog.for_agent("bill")
    assert bare is None or bare.names == ()
    wired = build_registry(
        production_modules(
            notification_service=FakeNotifier(), database=FakeDb(), writes=MagicMock()
        )
    )
    assert set(wired.catalog.for_agent("bill").names) == {
        "bill_query_transactions",
        "bill_analyze_comprehensive",
        "bill_send_report",
        "bill_send_report_execute",
    }


def test_analysis_summary_carries_the_external_cashflow(monkeypatch):
    """两跳编排里，后一个领域只能看到前一步的 summary。

    账单分析若只报「几笔交易、几个模块」，生日预算就无从谈起；这里钉住对外收支
    与净结余确实进了 summary。
    """

    from app.modules.bill import analysis_handlers as analysis_handlers_module

    report = {
        "report_meta": {
            "date_range": {"start": "2026-09-01", "end": "2026-09-30"},
            "total_transactions": 3,
            "cashflow": {
                "all": {"income_minor": 900000, "expense_minor": 100000, "net_minor": 800000},
                "external": {
                    "income_minor": 832000,
                    "expense_minor": 120000,
                    "net_minor": 712000,
                },
            },
            "generated_at": "2026-10-14 10:00:00",
        },
        "modules": [],
        "text_report": "报告正文",
        "audit": {},
    }

    class FakeAnalyzer:
        def __init__(self, database):
            self.database = database

        def analyze(self, user_id, period_start, period_end, modules=None, *, document_id=None):
            return report

    monkeypatch.setattr(analysis_handlers_module, "BillAnalyzer", FakeAnalyzer)
    module = build_bill_module(notification_service=FakeNotifier(), database=FakeDb())

    result = module.handlers["bill_analyze_comprehensive"](invocation(), None)

    assert isinstance(result, ToolData)
    assert "对外收入 8,320.00 元" in result.summary
    assert "对外支出 1,200.00 元" in result.summary
    assert "净结余 7,120.00 元" in result.summary


def test_the_runtime_hop_exposes_the_four_bill_tools(rt):
    # 钉住 runtime.py:98-100 那一跳：改回裸 build_registry() 这条就红 —— 上一条测试
    # 自己调 build_registry(production_modules(...))，永远走不到组合根。
    handle = rt.gateway.handle("bill", TrustedContext(user_id="user_0001", session_id="sess_bill"))

    assert {item.name for item in handle.tools} == {
        "bill_query_transactions",
        "bill_analyze_comprehensive",
        "bill_send_report",
        "bill_send_report_execute",
    }


# ---- 发信拆成 prepare / execute：冻结阶段一次都不发，点击才执行 -------------------


def test_prepare_refuses_to_freeze_a_report_when_no_email_is_bound():
    # handler 是「抛」ToolFailure，不是「返回」失败结果：modules/base.py:81 的
    # class ToolFailure(Exception)，由 tools/bank_adapter.py 的 except ToolFailure
    # 转成 ToolResult(status=FAILED, error_code=failure.code)。
    notifier = FakeNotifier(bound_email=None)
    handlers = create_email_handlers(notifier)

    with pytest.raises(ToolFailure) as failure:
        handlers["bill_send_report"](invocation(), None)

    assert failure.value.code == ErrorCode.VALIDATION  # StrEnum，也等于 "VALIDATION"
    assert notifier.sent == []  # 冻结阶段一次都不发


def test_prepare_freezes_the_operation_without_sending():
    notifier = FakeNotifier()
    handlers = create_email_handlers(notifier)

    result = handlers["bill_send_report"](invocation(), None)

    assert isinstance(result, ToolData)
    # facts 恰为五个键：多一个少一个都会被 bank_adapter._facts_violation 判
    # OUTPUT_SCHEMA_VIOLATION，所以这里用「相等」而不是「包含」。
    assert set(result.facts) == {
        "action_id", "period_start", "period_end", "recipient", "task_id"
    }
    # 按日期冻结的那一类没有任务号；按文件冻结时这里会是它。
    assert result.facts["task_id"] is None
    assert result.facts["action_id"].startswith("bill-report-")
    assert result.facts["period_start"] == "2026-09-01"
    assert result.facts["period_end"] == "2026-09-30"
    assert result.facts["recipient"] == "user@example.com"
    assert result.display == result.facts
    assert "user@example.com" in result.summary
    # 冻结阶段不碰 send_email，也不碰 PDF 生成 —— 这就是 prepare / execute 的分界。
    assert notifier.sent == []


def test_execute_sends_the_report_and_treats_queued_as_success(monkeypatch):
    # 打桩要打在 handler 真正取名字的地方：execute 是在**函数体内**
    # `from app.services.bill_analysis import BillAnalyzer`，取的是包上的属性；
    # `build_analysis_report_pdf` 则是在 email_handlers 模块顶层就绑好的，得打在
    # `app.modules.email_handlers` 上 —— 打在 analyzer / analysis_pdf 子模块上都不起作用。
    from app.modules import email_handlers as email_handlers_module
    from app.services import bill_analysis as bill_analysis_module

    report = {
        "report_meta": {
            "date_range": {"start": "2026-09-01", "end": "2026-09-30"},
            "total_transactions": 3,
            "generated_at": "2026-10-14 10:00:00",
        },
        "modules": [],
        "text_report": "报告正文",
        "audit": {},
    }

    class FakeAnalyzer:
        def __init__(self, database):
            self.database = database

        def analyze(self, user_id, period_start, period_end, modules=None, *, document_id=None):
            return report

    monkeypatch.setattr(bill_analysis_module, "BillAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(
        email_handlers_module, "build_analysis_report_pdf", lambda _report: b"%PDF-1.4"
    )
    notifier = FakeNotifier()
    notifier.result = {"status": "QUEUED", "queued": True}
    handlers = create_email_handlers(notifier)

    result = handlers["bill_send_report_execute"](invocation(), None)

    # QUEUED 也是成功：队列模式下真正发信的是另一个进程，status 就是判据。
    assert isinstance(result, ToolData)
    assert result.facts["recipient"] == "user@example.com"
    assert result.facts["format"] == "pdf"
    assert result.facts["queued"] is True
    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent["recipient"] == "user@example.com"
    attachment = sent["attachments"][0]
    assert attachment.filename == "流水深度分析结果_20260901_20260930.pdf"
    assert attachment.content == b"%PDF-1.4"
