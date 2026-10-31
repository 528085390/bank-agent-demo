"""L1 端到端验收：日累计把第二笔 600 元推成红标，红标决策与真实写各自留下审计行。

本文件只验证「策略 + 日累计 + 审计」这一条链，不驱动 Supervisor、不造 LLM 调用（模型分派属于 L2）。
四件事被钉住：

1. 当日空账 + 600 元 -> 黄标、免验证码，且不留 policy_decision 行：留痕只跟着「需要验证码」走；
2. 当日已累计 600 元，再来 600 元 -> 1200 元 > 日限额 1000 元 -> 红标、要验证码。
   策略引擎是严格大于（`app/policy/evaluator.py:116` 的 `projected > DAILY_RED_THRESHOLD_MINOR`），
   所以 60000 分单独一笔仍是黄标，两笔相加才越线；
3. 红标决策在业务审计里落一行 `transfer_prepare`（resource_type=policy_decision），reason 带
   「超过日限额」。读取器按 created_at 升序（`app/audit/service.py:589-591`），断言用 `any`；
4. 资金写成功之后才记日累计：一行 `transfer_submit` 的 ok 审计 + 日累计累加到 120000 分
   （预置 60000 + 真实写 60000）。记账在银行 2xx 之后，失败与重试不得重复累加。

两把钥匙不能混用：审计与日累计都按应用用户 id（`user_0001`）记，银行的客户号（`u_zhang`）
只上发银行。读者按前者过滤，写错钥匙的行等于没写。

`Runtime` 在构造时就把 `bank_client` 捕获进了 gateway 与 graph factory（`app/runtime.py:117`、
`:122`），所以这里一律**显式传入**假银行，而不是 `monkeypatch.setattr(rt, "bank_client", ...)`：
换不掉被捕获的那一个，只会让测试读起来像换掉了。写侧同理，自己建 `WriteFacade`，不改 `rt.writes`。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.audit.fund_writes import build_fund_write_audit_hook
from app.core.clock import SystemClock
from app.integration.write_facade import WriteFacade
from app.modules import production_modules
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

#: 银行的客户号：只上发银行，绝不当审计 / 日累计的 user_id。
CUSTOMER = "u_zhang"
#: 本文件唯一的金额：600 元 = 60000 分，两笔正好越过 1000 元的日限额。
SIX_HUNDRED_YUAN_MINOR = 60_000
PAYER = "a_zhang_current"
PAYEE = "a_partner_current"
CONTACT = "老王"


def _bank() -> FakeBankClient:
    """按 path 登记的假银行：未登记的路径直接 AssertionError，不会静默返回空。

    `/accounts` 与 `/contacts` 是 `transfer_prepare` 解析意图要读的两张表（`prepare` 要求
    `data` 是 list）；`/transfers` 是写侧的响应体。账户不能是 fixed、状态必须是 active，
    收款人要能被 `to_name` 精确命中，否则 prepare 会改问选择卡而不是给结论。
    """

    bank = FakeBankClient()
    bank.register(
        "/accounts",
        [
            {
                "account_id": PAYER,
                "user_id": CUSTOMER,
                "account_type": "current",
                "balance_minor": 832_000,
                "available_minor": 800_000,
                "currency": "CNY",
                "interest_rate": 0.0,
                "status": "active",
            }
        ],
    )
    bank.register(
        "/contacts",
        [
            {
                "contact_id": "c_1",
                "user_id": CUSTOMER,
                "name": CONTACT,
                "phone": "13800138000",
                "remark": None,
                "recipient_user_id": "u_partner",
                "recipient_account_id": PAYEE,
                "birthday": None,
            }
        ],
    )
    bank.register("/transfers", {"transaction_id": "TX_E2E_1"})
    return bank


def _prepare_handler(rt):
    """生产装配下的 transfer_prepare：组合根已把运行时数据库绑进 partial。"""

    modules = {
        module.name: module
        for module in production_modules(
            notification_service=rt.notification, database=rt.database, writes=rt.writes
        )
    }
    return modules["transfer"].handlers["transfer_prepare"]


def _invocation(session, *, idempotency_key: str = "idem_prepare_1") -> ToolInvocation:
    return ToolInvocation(
        agent_id="transfer",
        call=ToolCall(
            tool_name="transfer_prepare",
            arguments={"amount_yuan": "600", "to_name": CONTACT},
            idempotency_key=idempotency_key,
        ),
        context=TrustedContext(
            user_id=session["user_id"],
            session_id=session["session_id"],
            bank_customer_id=CUSTOMER,
        ),
    )


def _context(session) -> TrustedContext:
    return TrustedContext(
        user_id=session["user_id"],
        session_id=session["session_id"],
        bank_customer_id=CUSTOMER,
    )


def _record(rt, user_id: str, amount_minor: int) -> date:
    """预置当日累计：与写侧同一个 `DailyTotalStore`，业务日来自 `SystemClock`。"""

    business_date = SystemClock.today()
    with rt.database.sessions.begin() as db:
        DailyTotalStore.record_operation_amount(db, user_id, business_date, amount_minor)
    return business_date


def _business_events(rt, user_id: str) -> list[dict[str, Any]]:
    return rt.audit.reader.business_events(user_id)["items"]


def test_an_empty_day_keeps_six_hundred_yuan_at_yellow(rt, session):
    """空账 + 600 元：60000 分没越过 1000 元的日限额，确认卡只要求点一下。"""

    data = _prepare_handler(rt)(_invocation(session), _bank())

    facts = data.facts
    assert facts["intent"]["amount_minor"] == SIX_HUNDRED_YUAN_MINOR
    assert facts["requires_code"] is False
    assert facts["risk_level"] == "yellow"
    assert "超过日限额" not in facts["reason"]
    # 黄标不留痕：policy_decision 那一行只在需要验证码时写，空账这一笔不该凭空多一行账。
    assert _business_events(rt, session["user_id"]) == []


def test_a_second_six_hundred_yuan_escalates_to_red(rt, session):
    """当日已累计 600 元再来 600 元：120000 分 > 100000 分，升红标并要验证码。"""

    _record(rt, session["user_id"], SIX_HUNDRED_YUAN_MINOR)

    data = _prepare_handler(rt)(_invocation(session), _bank())

    facts = data.facts
    assert facts["requires_code"] is True
    assert facts["risk_level"] == "red"
    assert "超过日限额" in facts["reason"]
    # 用户看到的确认卡必须把「为什么要验证码」说出来，而不是只多一个输入框。
    assert "需要验证码" in data.summary


def test_the_red_decision_leaves_a_policy_row_in_the_audit_trail(rt, session):
    """红标决策落一行 policy_decision：谁、在哪个身份下、因为什么被要求验证码。"""

    _record(rt, session["user_id"], SIX_HUNDRED_YUAN_MINOR)

    _prepare_handler(rt)(_invocation(session), _bank())

    # 读取器按 created_at 升序返回（app/audit/service.py:589-591），所以不能取 [0]：
    # 一次运行里可能还有别的行，问「有没有这样一行」只能用 any。
    items = _business_events(rt, session["user_id"])
    assert any(
        event["action"] == "transfer_prepare" and "超过日限额" in (event.get("reason") or "")
        for event in items
    )

    rows = [event for event in items if event["action"] == "transfer_prepare"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["resource_type"], row["result"], row["resource_id"]) == (
        "policy_decision",
        "ok",
        "red",
    )
    # 记在应用身份下：银行的客户号当审计作者，读者永远查不到这一行。
    assert row["user_id"] == session["user_id"]
    assert _business_events(rt, CUSTOMER) == []


def test_a_successful_write_lands_an_ok_row_and_completes_the_days_total(rt, session):
    """写侧闭环：银行 2xx 之后才记账，一行 ok 审计 + 日累计累加到 120000 分。

    这笔写在真实链路里是红标：预置 600 元 + 600 元 > 1000 元，确认卡上必然带码，所以这里也
    带着码点确认。不带码的同一笔会被门面的日限额预检挡回 `CONFIRMATION_REQUIRED`
    （`tests/transfer/test_daily_limit_guard.py` 钉的就是那条路）。
    """

    _record(rt, session["user_id"], SIX_HUNDRED_YUAN_MINOR)
    bank = _bank()
    # 用自己建的门面（同一个审计钩子、同一个库），不改 rt.writes：它的客户端在构造时就被捕获了。
    facade = WriteFacade(
        bank,
        audit_hook=build_fund_write_audit_hook(rt.audit),
        database=rt.database,
    )

    response = facade.send(
        "POST",
        "/transfers",
        context=_context(session),
        json={
            "from_account_id": PAYER,
            "to_account_id": PAYEE,
            "amount_minor": SIX_HUNDRED_YUAN_MINOR,
            "remark": None,
        },
        idempotency_key="idem_write_1",
        operation="transfer_submit",
        amount_minor=SIX_HUNDRED_YUAN_MINOR,
        # 红标卡片上用户当场输入的码：门面靠它判断「当日限额已经回答过了」。
        verification_code="123456",
    )

    assert response.code == 0
    assert response.data == {"transaction_id": "TX_E2E_1"}

    items = _business_events(rt, session["user_id"])
    assert any(event["action"] == "transfer_submit" and event["result"] == "ok" for event in items)

    # get_accumulated_minor 收的是 Session，不是 Database（传 rt.database 会 AttributeError）。
    with rt.database.sessions() as db:
        assert (
            DailyTotalStore.get_accumulated_minor(db, session["user_id"], SystemClock.today())
            == 2 * SIX_HUNDRED_YUAN_MINOR
        )
