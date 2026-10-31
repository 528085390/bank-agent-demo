"""订阅扣款并入资金写路径（Fix C2b）：当日累计、业务审计行、门面预检。

扣款过去直接打银行：不记当日累计、不写业务审计行、也不过门面的日限额预检。现在：

1. `prepare` 对 charge 先读当日累计（与转账同一读法），越限就**拒绝**——订阅没有验证码通道，
   发一张永远点不过的卡比当场拒绝更糟；
2. `execute` 把冻结金额送进 `WriteFacade`，由门面在银行调用之前按当日累计重判一次，
   成功后才记当日累计并写业务审计行；
3. 取消 / 暂停 / 恢复仍直接打银行，也绝不进当日累计（它们不是资金写）。

用例钉住这几件事，外加组合根真的把 database / writes 绑进了订阅模块。
"""

from __future__ import annotations

import asyncio
from datetime import date
from functools import partial

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import build_domain_graph
from app.audit.fund_writes import build_fund_write_audit_hook
from app.integration import write_facade as write_facade_module
from app.integration.write_facade import WriteFacade
from app.modules import production_modules
from app.modules.base import ModuleDeps, ToolFailure
from app.modules.registry import build_registry
from app.modules.subscription import MODULE, bind_subscription
from app.modules.subscription import tools as subscription_tools
from app.modules.subscription.tools import TOOLS, execute, prepare
from app.modules.subscription.workflow import authorization_question
from app.storage.daily_total import DailyTotalStore
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ErrorCode, ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient
from tests.support.scripted_model import ScriptedModel

APP_USER = "user_0001"
#: 银行的客户号：只上发银行，绝不当审计 / 日累计的 user_id。
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
#: 600 元 = 60000 分：单笔是黄标，两笔 120000 分才越过 1000 元的日限额。
SIX_HUNDRED_YUAN_MINOR = 60_000
THREE_HUNDRED_YUAN_MINOR = 30_000
CAP_MESSAGE = "这笔订阅扣款会使当日累计超过 ¥1,000.00 的限额，今日无法完成，请明天再试。"

SUB_600 = {
    "subscription_id": "sub_600",
    "user_id": CUSTOMER,
    "from_account_id": "acc_1",
    "name": "视频会员",
    "merchant_name": "某视频",
    "amount_minor": SIX_HUNDRED_YUAN_MINOR,
    "frequency": "monthly",
    "start_date": "2026-01-01",
    "next_due_date": "2026-11-01",
    "status": "active",
    "auto_renew": True,
    "last_charge_id": None,
}
SUB_300 = {
    **SUB_600,
    "subscription_id": "sub_300",
    "name": "音乐会员",
    "merchant_name": "某音乐",
    "amount_minor": THREE_HUNDRED_YUAN_MINOR,
}


class _FixedClock:
    """固定业务日：真实的 `SystemClock.today()` 会去问银行，测试不该有网络。"""

    @staticmethod
    def today(client=None):
        return BUSINESS_DAY


@pytest.fixture
def business_day(monkeypatch):
    # 读侧（prepare）和写侧（门面预检、记账）必须看到同一个业务日。
    monkeypatch.setattr(subscription_tools, "SystemClock", _FixedClock)
    monkeypatch.setattr(write_facade_module, "SystemClock", _FixedClock)


def _bank() -> FakeBankClient:
    bank = FakeBankClient()
    bank.register("/subscriptions", [SUB_600, SUB_300])
    bank.register("/subscriptions/sub_600/charge", {**SUB_600, "last_charge_id": "ch_600"})
    bank.register("/subscriptions/sub_300/charge", {**SUB_300, "last_charge_id": "ch_300"})
    bank.register("/subscriptions/sub_600/cancel", {**SUB_600, "status": "cancelled"})
    return bank


def _context() -> TrustedContext:
    return TrustedContext(user_id=APP_USER, session_id="sess_c2b", bank_customer_id=CUSTOMER)


def _invocation(tool, arguments, key="idem-sub-1") -> ToolInvocation:
    return ToolInvocation(
        "subscription",
        ToolCall(tool_name=tool, arguments=arguments, idempotency_key=key),
        _context(),
    )


def _facade(bank, rt) -> WriteFacade:
    """与组合根同一个形状：审计钩子 + 运行时数据库。"""

    return WriteFacade(
        bank,
        audit_hook=build_fund_write_audit_hook(rt.audit),
        database=rt.database,
    )


def _prepared(bank, rt, tool="subscription_charge", subscription_id="sub_600"):
    data = prepare(
        _invocation(tool, {"subscription_id": subscription_id}), bank, database=rt.database
    )
    return data.facts["action"]


def _click(bank, action, facade):
    """按冻结快照点击：金额随 payload 走，键用快照冻结的那一把。"""

    return execute(
        _invocation(
            "subscription_execute",
            {
                "subscription_id": action["subscription_id"],
                "operation": action["operation"],
                "amount_minor": action["amount_minor"],
            },
            key=action["idempotency_key"],
        ),
        bank,
        writes=facade,
    )


def _posts(bank) -> list[dict]:
    return [request for request in bank.requests if request["method"] == "POST"]


def _record(rt, amount_minor):
    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(session, APP_USER, BUSINESS_DAY, amount_minor)


def _accumulated(rt, user_id: str = APP_USER) -> int:
    # get_accumulated_minor 收的是 Session，不是 Database（传 rt.database 会 AttributeError）。
    with rt.database.sessions() as db:
        return DailyTotalStore.get_accumulated_minor(db, user_id, BUSINESS_DAY)


def _fund_rows(rt) -> list[dict]:
    rows = rt.audit.reader.business_events(APP_USER)["items"]
    return [row for row in rows if row["resource_type"] == "fund_write"]


def test_a_charge_on_an_empty_day_parks_on_a_yellow_card(rt, business_day):
    """当日累计为 0 的 600 元扣款：黄标、不要码、只读一次银行。"""

    bank = _bank()
    action = _prepared(bank, rt)

    assert action["operation"] == "charge"
    assert action["amount_minor"] == SIX_HUNDRED_YUAN_MINOR
    assert action["risk_level"] == "yellow"
    question = authorization_question(action)
    assert question["confirmation"]["requires_code"] is False
    assert [request["method"] for request in bank.requests] == ["GET"]
    assert _accumulated(rt) == 0


def test_a_charge_that_would_cross_the_cap_is_refused_at_the_card(rt, business_day):
    """当日累计已有 600 元：这张 600 元的卡永远点不过，所以当场拒绝，不冻结快照。"""

    bank = _bank()
    _record(rt, SIX_HUNDRED_YUAN_MINOR)

    with pytest.raises(ToolFailure) as caught:
        prepare(
            _invocation("subscription_charge", {"subscription_id": "sub_600"}),
            bank,
            database=rt.database,
        )

    assert caught.value.code == ErrorCode.CONFIRMATION_REQUIRED
    assert caught.value.message == CAP_MESSAGE
    assert [request["method"] for request in bank.requests] == ["GET"]


def test_a_charge_goes_through_the_facade_and_lands_one_audit_row(rt, business_day):
    """点击后：一次银行写、一条业务审计行、当日累计 60000。"""

    bank = _bank()
    facade = _facade(bank, rt)
    action = _prepared(bank, rt)

    _click(bank, action, facade)

    posts = _posts(bank)
    assert [request["path"] for request in posts] == ["/subscriptions/sub_600/charge"]
    assert posts[0]["idempotency_key"] == action["idempotency_key"]
    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR
    rows = _fund_rows(rt)
    assert [(row["action"], row["result"]) for row in rows] == [("subscription_charge", "ok")]
    # 扣款请求体是空的（金额由订阅自己决定），所以这一行只报动作名、不带金额：
    # 钩子宁可少写金额，也不编一个 0 分（`app/audit/fund_writes.py:99-103`）。
    assert rows[0]["reason"] == "资金操作成功: subscription_charge"


def test_the_days_total_accumulates_across_two_charges(rt, business_day):
    """两笔都放行：600 + 300 = 90000，仍未越限，当日累计要真的加上去。"""

    bank = _bank()
    facade = _facade(bank, rt)

    first = _prepared(bank, rt, subscription_id="sub_600")
    _click(bank, first, facade)
    second = _prepared(bank, rt, subscription_id="sub_300")
    _click(bank, second, facade)

    assert [request["path"] for request in _posts(bank)] == [
        "/subscriptions/sub_600/charge",
        "/subscriptions/sub_300/charge",
    ]
    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR + THREE_HUNDRED_YUAN_MINOR
    assert [row["action"] for row in _fund_rows(rt)] == [
        "subscription_charge",
        "subscription_charge",
    ]


def test_a_card_frozen_before_the_cap_was_crossed_is_stopped_by_the_facade(rt, business_day):
    """卡在空账上判黄，点之前别的写把当日累计推过限额：门面必须挡回，且不到银行。"""

    bank = _bank()
    facade = _facade(bank, rt)
    action = _prepared(bank, rt)
    _record(rt, SIX_HUNDRED_YUAN_MINOR)

    with pytest.raises(ToolFailure) as caught:
        _click(bank, action, facade)

    assert caught.value.code == ErrorCode.CONFIRMATION_REQUIRED
    assert caught.value.message == CAP_MESSAGE
    assert _posts(bank) == []
    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR


def test_cancel_still_calls_the_bank_directly_and_never_enters_the_daily_total(rt, business_day):
    """取消不是资金写：直接打银行，不记当日累计，也不留资金写审计行。"""

    bank = _bank()
    facade = _facade(bank, rt)
    action = _prepared(bank, rt, tool="subscription_cancel")

    _click(bank, action, facade)

    assert [request["path"] for request in _posts(bank)] == ["/subscriptions/sub_600/cancel"]
    assert _accumulated(rt) == 0
    assert _fund_rows(rt) == []


def test_the_execute_tool_carries_the_frozen_amount_as_an_optional_argument():
    parameters = TOOLS["subscription_execute"].parameters
    assert parameters["properties"]["amount_minor"]["type"] == "integer"
    assert "amount_minor" not in parameters["required"]


def test_the_composition_root_binds_the_database_and_the_facade(rt):
    """组合根真的把 database / writes 绑进订阅模块，而不只是留在声明里。"""

    modules = production_modules(
        notification_service=rt.notification, database=rt.database, writes=rt.writes
    )
    module = next(item for item in modules if item.name == "subscription")

    charge = module.handlers["subscription_charge"]
    assert isinstance(charge, partial)
    assert charge.keywords["database"] is rt.database
    assert module.handlers["subscription_execute"].keywords["writes"] is rt.writes
    assert module.declaration.graph_builder.keywords["writes"] is rt.writes

    # 取消 / 暂停 / 恢复不进当日累计，也就没有理由拿到数据库或门面。
    for name in ("subscription_cancel", "subscription_pause", "subscription_resume"):
        assert module.handlers[name] is subscription_tools.prepare
    assert module.handlers["subscription_list"] is subscription_tools.subscriptions
    assert module.handlers["subscription_detail"] is subscription_tools.detail


def _parked(module, bank):
    """在订阅图上挂一张扣款确认卡，返回图与问题 id。"""

    registry = build_registry([module])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    spec = build_domain_specs(gateway, _context(), roster=registry.roster)[0]
    model = ScriptedModel([("call", "subscription_charge", '{"subscription_id":"sub_600"}')])
    graph = build_domain_graph(model, spec).copy(update={"checkpointer": InMemorySaver()})
    question_id = _park_and_question(graph, "sess_c2b", "现在就扣这期视频会员")
    return graph, question_id


def _park_and_question(graph, session, message):
    runtime = AgentRuntime(graph=graph)

    async def run():
        events = [event async for event in runtime.invoke(session, APP_USER, message, {})]
        return next(event.data["question_id"] for event in events if event.event == "interrupt")

    return asyncio.run(run())


def _resume(graph, session, question_id, value):
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [
            event
            async for event in runtime.invoke(
                session,
                APP_USER,
                "确认",
                {"resume": {"question_id": question_id, "value": value}},
            )
        ]

    return asyncio.run(run())


def test_the_click_carries_the_frozen_amount_into_the_facade(rt, business_day):
    """整轮图：卡冻结后当日累计被推过限额，点击必须被门面挡回（金额确实随 payload 走了）。"""

    bank = _bank()
    facade = _facade(bank, rt)
    module = bind_subscription(
        MODULE,
        ModuleDeps(notification_service=rt.notification, database=rt.database, writes=facade),
    )
    graph, question_id = _parked(module, bank)
    _record(rt, SIX_HUNDRED_YUAN_MINOR)

    events = _resume(graph, "sess_c2b", question_id, {"decision": "confirm"})

    ending = next(event for event in reversed(events) if event.event == "subtask_end")
    assert ending.data["status"] == "FAILED"
    assert CAP_MESSAGE in ending.data["summary"]
    assert _posts(bank) == []
    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR
