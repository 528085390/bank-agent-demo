"""资金写的审计行：只记「钱动过」的两端，成功一行 ok，失败一行 failed。

钉住的五件事：

1. 一次成功的资金写落**恰好一行** ok：resource_id 是幂等键，action 是业务动作名
   （transfer_submit），不是 POST /transfers 这种传输层名字；
2. 银行拒绝（INSUFFICIENT_BALANCE）落**恰好一行** failed，reason 带上分类——钱没动，但
   「这笔转账试过并且失败了」正是审计时间线要回答的问题；
3. fund_write_attempt 与 daily_total_* 三个事件**一行都不落**（Ruling R3）：一次写只对应一行
   账，重放与记账的内部细节不占业务审计表；
4. 审计库自己坏掉时，send 仍把银行响应原样返回、钩子只写一行 logger.error：钱已经转出去了，
   审计写不进去不能让用户看到失败，异常也绝不能穿回 WriteFacade；
5. 超长幂等键按列宽裁剪后照常落行，缺 user_id 的畸形 payload 只记日志不落行、更不抛。

身份取 TrustedContext.user_id（应用用户 user_0001），不是银行的客户号（u_wealth）：读者按前者
过滤，客户号只在 payload["bank_customer_id"] 里留作对账。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.audit.context import AuditContext, audit_context
from app.audit.fund_writes import build_fund_write_audit_hook
from app.audit.service import AuditService
from app.integration.bank_client import BankClient
from app.integration.bank_errors import BankCallError
from app.integration.write_facade import WriteFacade
from app.tools.protocol import TrustedContext

APP_USER = "user_0001"
CUSTOMER = "u_wealth"
#: 工具层幂等键的形状（agents/sdk.py）：约 57 字符，落在列宽以内。
KEY = "transfer:transfer_submit:0123456789abcdef0123456789abcdef"


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


class BrokenDatabase:
    """读得动、写事务必炸的 Database。

    两个用途共用它：证明审计写不进去也不影响已成功的资金写，以及证明记账库坏掉时资金写
    照样成功。门面的日限额预检会先读一次当日累计（读不到就 Fail-Closed 挡下整笔写），所以
    这个假库必须让读成功、只让写事务炸 —— 否则第二个用途测到的就不是记账失败，而是预检。
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
            raise RuntimeError("audit database is down")

    def __init__(self) -> None:
        self.sessions = self._Sessions()


def _facade(rt, handler=_ok, *, audit=None, database=None):
    transport = RecordingTransport(handler)
    client = BankClient("http://bank.test", transport=transport)
    facade = WriteFacade(
        client,
        audit_hook=build_fund_write_audit_hook(rt.audit if audit is None else audit),
        database=rt.database if database is None else database,
    )
    return facade, transport


def _context() -> TrustedContext:
    return TrustedContext(user_id=APP_USER, session_id="s1", bank_customer_id=CUSTOMER)


def _items(rt, user_id: str = APP_USER) -> list[dict[str, Any]]:
    return rt.audit.reader.business_events(user_id)["items"]


def _submit(facade, *, key: str = KEY):
    return facade.send(
        "POST",
        "/transfers",
        context=_context(),
        json={"amount_minor": 60_000, "confirm": True},
        idempotency_key=key,
        operation="transfer_submit",
        amount_minor=60_000,
    )


def test_a_successful_fund_write_lands_exactly_one_ok_row(rt):
    facade, transport = _facade(rt)

    with audit_context(AuditContext(user_id=APP_USER, conversation_id="c1", run_id="r1")):
        response = _submit(facade)

    assert response.code == 0
    assert len(transport.requests) == 1

    items = _items(rt)
    assert len(items) == 1
    row = items[0]
    assert (row["result"], row["resource_type"], row["action"]) == (
        "ok",
        "fund_write",
        "transfer_submit",
    )
    assert row["resource_id"] == KEY
    assert "资金操作成功" in row["reason"]
    assert "60000" in row["reason"]
    # 记在应用身份下，并留住发起这笔写的会话：读者按 user_id 过滤、按 conversation 归堆。
    assert (row["user_id"], row["conversation_id"]) == (APP_USER, "c1")
    # 银行客户号只对账，绝不当审计的 user_id。
    assert _items(rt, CUSTOMER) == []


def test_a_refused_fund_write_lands_exactly_one_failed_row(rt):
    facade, _transport = _facade(rt, _refused)

    with pytest.raises(BankCallError):
        _submit(facade)

    items = _items(rt)
    assert len(items) == 1
    row = items[0]
    assert (row["result"], row["resource_type"], row["action"]) == (
        "failed",
        "fund_write",
        "transfer_submit",
    )
    assert row["resource_id"] == KEY
    assert "资金操作失败" in row["reason"]
    assert "INSUFFICIENT_BALANCE" in row["reason"]


@pytest.mark.parametrize(
    "make_database, fund_write",
    [
        pytest.param(lambda rt: rt.database, False, id="daily-total-skipped"),
        pytest.param(lambda _rt: BrokenDatabase(), True, id="daily-total-write-failed"),
    ],
)
def test_the_attempt_and_the_daily_total_events_land_no_row(rt, make_database, fund_write):
    facade, _transport = _facade(rt, database=make_database(rt))

    if not fund_write:
        # 撤销定时任务：没有金额，日累计只能跳过并喊一声。
        facade.send(
            "POST",
            "/transfers/scheduled/task_1/cancel",
            context=_context(),
            json={},
            idempotency_key=KEY,
            operation="cancel_scheduled_transfer",
        )
    else:
        # 记账库坏掉：日累计只能报失败。
        _submit(facade)

    items = _items(rt)
    assert len(items) == 1
    assert items[0]["result"] == "ok"
    assert items[0]["resource_id"] == KEY


def test_a_broken_audit_store_never_breaks_the_fund_write(rt, caplog):
    facade, transport = _facade(rt, audit=AuditService(BrokenDatabase()))

    with caplog.at_level(logging.ERROR, logger="app.audit.fund_writes"):
        response = _submit(facade)

    # 钱已经转出去了：审计写不进去不能让用户看到失败，也不能让异常穿回门面。
    assert response.code == 0
    assert response.data == {"transaction_id": "TX1"}
    assert len(transport.requests) == 1
    assert "fund_write_audit_failed" in caplog.text
    # 审计行没写成，但也没有留下半行。
    assert _items(rt) == []


def test_an_over_long_idempotency_key_is_clipped_to_the_column(rt):
    facade, _transport = _facade(rt)

    _submit(facade, key="k" * 100)

    items = _items(rt)
    assert len(items) == 1
    # 幂等键列宽 64：裁掉的是键的尾巴，不是身份。
    assert items[0]["resource_id"] == "k" * 64


def test_a_payload_without_a_user_only_logs_and_never_raises(rt, caplog):
    hook = build_fund_write_audit_hook(rt.audit)

    with caplog.at_level(logging.ERROR, logger="app.audit.fund_writes"):
        hook("fund_write_success", {"operation": "transfer_submit", "idempotency_key": KEY})

    assert "fund_write_audit_failed" in caplog.text
    assert _items(rt) == []
