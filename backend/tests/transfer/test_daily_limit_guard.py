"""写侧的日限额预检：挂卡时判黄，不等于执行时还该放行。

确认卡只在冻结的那一刻做决策（`transfer_prepare` 读当日累计），而用户可能过一会儿才点确认。
这中间别的写已经把当日累计推过 1000 元时，`WriteFacade.send` 必须在银行调用之前重新判一次，
否则两张在空账上判黄的 600 元卡能连着把钱转出去、一分钱验证码都不要。

四条用例钉住四件事：

1. 空账上两张卡都是黄标，第一笔放行并记账，第二笔（无码）在门面处被挡回
   CONFIRMATION_REQUIRED：银行只收到一次 `POST /transfers`，当日累计停在 60000；
2. 第二笔带上用户当场输入的验证码就照常放行：预检挡的是「没码就过」，不是「不许写」；
3. 没接数据库的门面（单元级调用者）整段跳过预检：它无从知道当日累计；
4. 读当日累计失败按「已超限」判（Fail-Closed），与 `transfer_prepare` 同一套口径。

业务日固定，避免 `SystemClock.today()` 去问银行；日累计按应用用户记，银行的客户号只上发银行。
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.audit.fund_writes import build_fund_write_audit_hook
from app.integration import write_facade as write_facade_module
from app.integration.write_facade import WriteFacade
from app.modules.base import ToolFailure
from app.modules.transfer import tools as transfer_tools
from app.modules.transfer.writes import submit_transfer
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

APP_USER = "user_0001"
#: 银行的客户号：只上发银行，绝不当审计 / 日累计的 user_id。
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
#: 600 元 = 60000 分：单笔是黄标，两笔 120000 分才越过 1000 元的日限额。
SIX_HUNDRED_YUAN_MINOR = 60_000
CODE = "123456"

ACCOUNTS = [
    {
        "account_id": "a_zhang_current",
        "user_id": CUSTOMER,
        "account_type": "current",
        "balance_minor": 832000,
        "available_minor": 800000,
        "currency": "CNY",
        "interest_rate": 0.0,
        "status": "active",
    }
]

CONTACTS = [
    {
        "contact_id": "c_1",
        "user_id": CUSTOMER,
        "name": "老王",
        "phone": "13800138000",
        "remark": None,
        "recipient_user_id": "u_partner",
        "recipient_account_id": "a_partner_current",
        "birthday": None,
    }
]


class _FixedClock:
    """固定业务日：真实的 `SystemClock.today()` 会去问银行，测试不该有网络。"""

    @staticmethod
    def today(client=None):
        return BUSINESS_DAY


@pytest.fixture
def business_day(monkeypatch):
    # 读侧（prepare）和写侧（门面预检、记账）必须看到同一个业务日。
    monkeypatch.setattr(transfer_tools, "SystemClock", _FixedClock)
    monkeypatch.setattr(write_facade_module, "SystemClock", _FixedClock)


def _bank() -> FakeBankClient:
    bank = FakeBankClient()
    bank.register("/accounts", ACCOUNTS)
    bank.register("/contacts", CONTACTS)
    bank.register("/transfers", {"transaction_id": "t_1"})
    return bank


def _context() -> TrustedContext:
    return TrustedContext(user_id=APP_USER, session_id="sess_1", bank_customer_id=CUSTOMER)


def _prepare_invocation() -> ToolInvocation:
    return ToolInvocation(
        "transfer",
        ToolCall(
            tool_name="transfer_prepare",
            arguments={"amount_yuan": "600.00", "to_name": "老王", "from_hint": "活期"},
            call_id="c_1",
        ),
        _context(),
    )


def _prepared(bank, rt):
    """生产装配下的 transfer_prepare：组合根已把运行时数据库绑进 partial。"""

    data = transfer_tools.prepare(_prepare_invocation(), bank, database=rt.database)
    assert data.facts["status"] == "ready", data.facts
    return data.facts


def _facade(bank, rt, *, database=True) -> WriteFacade:
    """与组合根同一个形状：审计钩子 + 运行时数据库；`database=False` 造单元级门面。"""

    return WriteFacade(
        bank,
        audit_hook=build_fund_write_audit_hook(rt.audit),
        database=rt.database if database else None,
    )


def _submit(bank, rt, facts, *, verification_code=None, idempotency_key="idem_1", facade=None):
    return submit_transfer(
        bank,
        _context(),
        facts["intent"],
        {"pending_id": "p_1", "idempotency_key": idempotency_key},
        verification_code=verification_code,
        writes=facade if facade is not None else _facade(bank, rt),
    )


def _transfers(bank) -> list[dict]:
    return [
        request
        for request in bank.requests
        if request["method"] == "POST" and request["path"] == "/transfers"
    ]


def _accumulated(rt, user_id: str = APP_USER) -> int:
    # get_accumulated_minor 收的是 Session，不是 Database（传 rt.database 会 AttributeError）。
    with rt.database.sessions() as db:
        return DailyTotalStore.get_accumulated_minor(db, user_id, BUSINESS_DAY)


def test_the_second_click_without_a_code_is_refused_and_never_reaches_the_bank(rt, business_day):
    """两张黄标卡：第一笔放行，第二笔在门面处被挡回，银行只收到一次转账。"""

    bank = _bank()
    first = _prepared(bank, rt)
    second = _prepared(bank, rt)
    # 两张卡都是在当日累计还是 0 的时候冻结的，所以都判黄、都不问验证码。
    assert first["requires_code"] is False
    assert second["requires_code"] is False

    _submit(bank, rt, first, idempotency_key="idem_1")

    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR
    landed = rt.audit.reader.business_events(APP_USER)["items"]
    with pytest.raises(ToolFailure) as caught:
        _submit(bank, rt, second, idempotency_key="idem_2")

    assert caught.value.code == ErrorCode.CONFIRMATION_REQUIRED
    # 被挡下的那笔既没到银行，也没记账，更没留下审计行：预检跑在 fund_write_attempt 之前。
    assert len(_transfers(bank)) == 1
    assert _accumulated(rt) == SIX_HUNDRED_YUAN_MINOR
    assert [row["action"] for row in landed] == ["transfer_submit"]
    assert rt.audit.reader.business_events(APP_USER)["items"] == landed


def test_the_second_click_goes_through_when_the_user_supplies_the_code(rt, business_day):
    """同样的两张卡，第二笔带码：预检放行，两笔都到银行，当日累计 120000。"""

    bank = _bank()
    first = _prepared(bank, rt)
    second = _prepared(bank, rt)
    assert first["requires_code"] is False
    assert second["requires_code"] is False

    _submit(bank, rt, first, idempotency_key="idem_1")
    _submit(bank, rt, second, verification_code=CODE, idempotency_key="idem_2")

    transfers = _transfers(bank)
    assert len(transfers) == 2
    # 验证码要真的跟着这次写到达银行，而不是只在门面里放行。
    assert transfers[0]["json"]["mfa_code"] is None
    assert transfers[1]["json"]["mfa_code"] == CODE
    assert _accumulated(rt) == 2 * SIX_HUNDRED_YUAN_MINOR


def test_a_facade_without_a_database_still_sends(rt, business_day):
    """没接数据库的门面跳过预检：单元级调用者不受这条策略约束。"""

    with rt.database.sessions.begin() as session:
        DailyTotalStore.record_operation_amount(
            session, APP_USER, BUSINESS_DAY, SIX_HUNDRED_YUAN_MINOR
        )

    bank = _bank()
    facts = _prepared(bank, rt)
    # 当日累计已是 600 元：同样的写在有库的门面上会被要求验证码。
    assert facts["requires_code"] is True

    response = _submit(bank, rt, facts, facade=WriteFacade(bank))

    assert response.code == 0
    assert len(_transfers(bank)) == 1


def test_a_failed_daily_total_read_refuses_the_write(rt, business_day, monkeypatch, caplog):
    """读不到当日累计就按「已超限」判：Fail-Closed，与 transfer_prepare 同一口径。"""

    bank = _bank()
    facts = _prepared(bank, rt)
    assert facts["requires_code"] is False

    def boom(*_args, **_kwargs):
        raise RuntimeError("daily total store is down")

    monkeypatch.setattr(DailyTotalStore, "get_accumulated_minor", staticmethod(boom))

    with caplog.at_level(logging.WARNING, logger="app.integration.write_facade"):
        with pytest.raises(ToolFailure) as caught:
            _submit(bank, rt, facts, idempotency_key="idem_1")

    assert caught.value.code == ErrorCode.CONFIRMATION_REQUIRED
    assert _transfers(bank) == []
    assert "write_daily_total_read_failed" in caplog.text
