"""资金写成功之后才记账：日累计只在这一条路径上出现。

四件事被钉住：

1. 注入 `database` 且给了正数 `amount_minor` → `daily_operation_total` 落一行，
   业务日期来自 `SystemClock`（不是本机 `date.today()`）；
2. 没有正数 `amount_minor`（形参缺省、0、负数；请求体里带着金额也不算）→ 不落行，但审计钩子
   收到 `daily_total_skipped`：不记账这件事必须看得见，不能静默。撤销定时任务、理财申购都走
   这一支 —— 「这是资金写」由调用方明说的正数金额表达，不由请求体里恰好出现的字段推断；
3. 记账段抛异常 → `send` 仍把银行响应原样返回（钱已经转出去了，记不上账不能让用户看到失败），
   同时钩子收到 `daily_total_write_failed`；
4. 银行自己失败 → 一行都不落，也不出现任何记账事件。

日累计的 `user_id` 取 `TrustedContext.user_id`（应用侧身份，不是银行的客户号）：审计时间线
与 API 都按它过滤，风控读侧必须读同一把钥匙。银行的客户号只上发到银行那一层，事件里额外
以 `bank_customer_id` 出现，便于对账。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.integration import write_facade as write_facade_module
from app.integration.bank_client import BankClient
from app.integration.bank_errors import BankCallError
from app.integration.write_facade import WriteFacade
from app.storage.daily_total import DailyTotalStore
from app.storage.models import DailyOperationTotal
from app.tools.protocol import TrustedContext

CUSTOMER = "u_zhang"
APP_USER = "app_user"
#: 固定业务日，故意不是今天：日期若取自本机 `date.today()`，断言就会失败。
DAY = date(2031, 3, 4)


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"transaction_id": "TX1"}})


def _refused(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(409, json={"code": 1006, "message": "nope", "data": None})


def _facade(handler, *, database=None):
    events: list[tuple[str, dict[str, Any]]] = []
    transport = RecordingTransport(handler)
    client = BankClient("http://bank.test", transport=transport)
    facade = WriteFacade(
        client,
        audit_hook=lambda name, payload: events.append((name, dict(payload))),
        database=database,
    )
    return facade, transport, events


def _context() -> TrustedContext:
    return TrustedContext(user_id=APP_USER, session_id="s1", bank_customer_id=CUSTOMER)


def _rows(rt) -> list[DailyOperationTotal]:
    with rt.database.sessions() as db:
        return list(
            db.scalars(
                select(DailyOperationTotal).where(DailyOperationTotal.user_id == APP_USER)
            ).all()
        )


def _accumulated(rt) -> int:
    with rt.database.sessions() as db:
        return DailyTotalStore.get_accumulated_minor(db, APP_USER, DAY)


class _FixedClock:
    """业务日固定：证明日期来自 `SystemClock`，而不是本机 `date.today()`。"""

    @staticmethod
    def today(client=None) -> date:
        del client
        return DAY


@pytest.fixture
def business_day(monkeypatch):
    monkeypatch.setattr(write_facade_module, "SystemClock", _FixedClock)
    return DAY


class BrokenDatabase:
    """读得动、写事务必炸的 `Database`：证明记账失败不会污染已成功的资金写。

    门面的日限额预检会先读一次当日累计，读不到就按「已超限」判（Fail-Closed，与
    `transfer_prepare` 同一口径）；所以这个假库必须让读成功、只让记账那个事务炸 —— 否则
    测到的就不是「记账失败不污染已成功的写」，而是预检把写挡在门外。
    """

    class _Session:
        """空账：当日累计为 0，预检照常放行。"""

        @staticmethod
        def scalar(*_args, **_kwargs):
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Sessions:
        def __call__(self):
            return BrokenDatabase._Session()

        def begin(self):
            raise RuntimeError("database is down")

    def __init__(self) -> None:
        self.sessions = self._Sessions()


def test_a_successful_fund_write_records_the_amount_for_the_business_day(rt, business_day):
    facade, _transport, events = _facade(_ok, database=rt.database)

    response = facade.send(
        "POST",
        "/transfers",
        context=_context(),
        json={"amount_minor": 60_000, "confirm": True},
        idempotency_key="idem-daily-1",
        operation="transfer_submit",
        amount_minor=60_000,
    )

    assert response.code == 0
    # 成功路径只多出一行日累计，不多出一个事件。
    assert [name for name, _ in events] == ["fund_write_attempt", "fund_write_success"]

    rows = _rows(rt)
    assert len(rows) == 1
    assert (rows[0].total_minor, rows[0].business_date) == (60_000, business_day)
    assert _accumulated(rt) == 60_000

    # 记在应用身份下，不在银行客户号下：两把钥匙不能混用，否则风控读侧永远读回 0。
    with rt.database.sessions() as db:
        assert DailyTotalStore.get_accumulated_minor(db, CUSTOMER, DAY) == 0
    success = [payload for name, payload in events if name == "fund_write_success"][0]
    assert (success["user_id"], success["bank_customer_id"]) == (APP_USER, CUSTOMER)


@pytest.mark.parametrize("amount_minor", [None, 0, -1], ids=["missing", "zero", "negative"])
def test_a_write_without_a_positive_amount_records_nothing(rt, business_day, amount_minor):
    """请求体里带着金额也没用：资金写的信号是调用方明说的正数金额。"""

    facade, _transport, events = _facade(_ok, database=rt.database)

    facade.send(
        "POST",
        "/transfers",
        context=_context(),
        json={"amount_minor": 45_000, "confirm": True},
        idempotency_key="idem-daily-2",
        operation="transfer_submit",
        amount_minor=amount_minor,
    )

    assert _rows(rt) == []
    assert _accumulated(rt) == 0
    skipped = [payload for name, payload in events if name == "daily_total_skipped"]
    assert skipped[0]["reason"] == "no amount to count: this write moves no money"


def test_a_write_without_an_amount_records_nothing_and_says_so(rt, business_day):
    facade, _transport, events = _facade(_ok, database=rt.database)

    facade.send(
        "POST",
        "/transfers/scheduled/task_1/cancel",
        context=_context(),
        json={},
        idempotency_key="idem-daily-3",
        operation="cancel_scheduled_transfer",
    )

    assert _rows(rt) == []
    assert _accumulated(rt) == 0

    names = [name for name, _ in events]
    assert names == ["fund_write_attempt", "fund_write_success", "daily_total_skipped"]
    skipped = [payload for name, payload in events if name == "daily_total_skipped"]
    assert skipped[0]["operation"] == "cancel_scheduled_transfer"


def test_a_bookkeeping_failure_never_turns_a_successful_write_into_a_failure(business_day):
    facade, transport, events = _facade(_ok, database=BrokenDatabase())

    response = facade.send(
        "POST",
        "/transfers",
        context=_context(),
        json={"amount_minor": 60_000, "confirm": True},
        idempotency_key="idem-daily-4",
        operation="transfer_submit",
        amount_minor=60_000,
    )

    # 钱已经转出去了：记不上账不能让用户看到失败。
    assert response.code == 0
    assert response.data == {"transaction_id": "TX1"}
    assert len(transport.requests) == 1

    names = [name for name, _ in events]
    assert names == ["fund_write_attempt", "fund_write_success", "daily_total_write_failed"]
    failed = [payload for name, payload in events if name == "daily_total_write_failed"]
    assert failed[0]["operation"] == "transfer_submit"
    assert failed[0]["error_kind"] == "RuntimeError"


def test_a_failed_bank_write_records_nothing(rt, business_day):
    facade, _transport, events = _facade(_refused, database=rt.database)

    with pytest.raises(BankCallError):
        facade.send(
            "POST",
            "/transfers",
            context=_context(),
            json={"amount_minor": 60_000, "confirm": True},
            idempotency_key="idem-daily-5",
            operation="transfer_submit",
            amount_minor=60_000,
        )

    assert _rows(rt) == []
    assert [name for name, _ in events] == ["fund_write_attempt", "fund_write_failure"]
