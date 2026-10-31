"""网关层的参数归一化：模型用占位符表示「没提供」时，工具不该收到它。

strict 模式没有「可选属性」的概念（app/tools/schema.py:1-7），所以可选参数在模型视图里变成
「必填可空」；弱模型会把空值写成字符串 "null"。归一化必须发生在网关，一次覆盖所有工具，
而不是每个模块各抄一份。
"""

from __future__ import annotations

from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, ToolStatus, TrustedContext
from app.tools.recording import RecordingAdapter

CONTEXT = TrustedContext(user_id="u_1", session_id="s_1", bank_customer_id="u_zhang")

PROBE = ToolSpec(
    name="probe",
    description="测试用：两个可选字符串、一个整数、一个布尔、一个数组、一个对象",
    parameters={
        "type": "object",
        "properties": {
            "optional_str": {"type": "string"},
            "optional_str2": {"type": "string"},
            "num": {"type": "integer"},
            "flag": {"type": "boolean"},
            "items": {"type": "array"},
            "payload": {"type": "object"},
        },
        "required": [],
        "additionalProperties": False,
    },
)


class RecordingAudit:
    """只收 record_agent_event 的载荷，用来观察审计写下的参数。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_agent_event(self, **kwargs) -> None:
        self.events.append(kwargs)


def probe_gateway(*, audit=None):
    recorder = RecordingAdapter()
    gateway = ToolGateway(
        ToolCatalog.build([PROBE], {"alpha": ["probe"]}), recorder, audit=audit
    )
    return gateway, gateway.handle("alpha", CONTEXT), recorder


def test_placeholder_strings_never_reach_the_adapter():
    """字符串占位符被丢、None 被丢，0 / False / [] / {} 一个都不能丢（spec §5 点名）。"""

    gateway, handle, recorder = probe_gateway()

    result = gateway.invoke(
        handle,
        {
            "tool": "probe",
            "arguments": {
                "optional_str": "null",
                "optional_str2": "none",
                "num": 0,
                "flag": False,
                "items": [],
                "payload": {},
            },
        },
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert recorder.invocations[0].call.arguments == {
        "num": 0,
        "flag": False,
        "items": [],
        "payload": {},
    }


def test_none_and_blank_placeholders_are_dropped_and_falsy_values_survive():
    gateway, handle, recorder = probe_gateway()

    gateway.invoke(
        handle,
        {"tool": "probe", "arguments": {"optional_str": None, "optional_str2": "  NULL  ", "num": 0}},
    )

    assert recorder.invocations[0].call.arguments == {"num": 0}


def test_a_string_that_merely_looks_like_a_placeholder_survives():
    gateway, handle, recorder = probe_gateway()

    gateway.invoke(
        handle,
        {
            "tool": "probe",
            "arguments": {"optional_str": "Null 先生", "optional_str2": "none of your business"},
        },
    )

    assert recorder.invocations[0].call.arguments == {
        "optional_str": "Null 先生",
        "optional_str2": "none of your business",
    }


def test_wealth_native_read_tools_go_through_the_same_normalization():
    """wealth 的只读工具是模块带进场的原生工具，走的是同一个网关。"""

    from app.modules.wealth.infrastructure.tools import READ_TOOLS

    specs = [tool.spec for tool in READ_TOOLS.values()]
    recorder = RecordingAdapter()
    gateway = ToolGateway(
        ToolCatalog.build(specs, {"wealth": [spec.name for spec in specs]}), recorder
    )

    result = gateway.invoke(
        gateway.handle("wealth", CONTEXT),
        {"tool": "wealth_products", "arguments": {"risk_level": "null"}},
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert recorder.invocations[0].call.arguments == {}


def test_a_refused_call_records_the_arguments_it_received():
    """被拒调用的审计留下模型真正发了什么（gateway.py:236），有意与成功调用不对称。"""

    audit = RecordingAudit()
    gateway, handle, _ = probe_gateway(audit=audit)

    result = gateway.invoke(
        handle,
        {"tool": "probe", "arguments": {"optional_str": "null", "num": "not an int"}},
    )

    assert result.status is ToolStatus.DENIED
    assert audit.events[0]["payload"]["arguments"] == {"optional_str": "null", "num": "not an int"}


def test_an_accepted_call_records_the_normalized_arguments():
    audit = RecordingAudit()
    gateway, handle, _ = probe_gateway(audit=audit)

    gateway.invoke(handle, {"tool": "probe", "arguments": {"optional_str": "null", "num": 0}})

    assert audit.events[0]["payload"]["arguments"] == {"num": 0}
