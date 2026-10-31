"""Task 2 of L1.A: the hidden `life_execute_order` write tool.

确认卡点击之后才会走到这里：参数是 `life_prepare_order` 冻结好的订单快照（含金额），
执行侧不重新定价、不重新选账户。下单经资金写门面（`WriteFacade`），当日累计由门面在
银行 2xx 之后自己记账，模块不写 `fund_write_*` 审计。

验证码走关键字参数，不是工具参数：工具参数会进 `tool_call` SSE 帧与 run-event 审计，
所以 schema 里根本不该有它（与 `transfer/workflow.py` 把 `turn.verification_code`
当闭包参数传给 `submit_transfer` 同一个路子）。

时钟固定两个地方：读侧（`life_tools.SystemClock`）与写侧（`write_facade.SystemClock`），
否则门面会用真实日期记账，和测试读的不是同一天。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app.audit.fund_writes import build_fund_write_audit_hook
from app.core.clock import CST
from app.integration import write_facade as write_facade_module
from app.integration.bank_client import BankResponse
from app.integration.write_facade import WriteFacade
from app.modules.base import ToolFailure
from app.modules.life_event import tools as life_tools
from app.modules.life_event import writes as life_writes
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

APP_USER = "user_0001"
CUSTOMER = "u_zhang"
BUSINESS_DAY = date(2031, 3, 4)
NOW = datetime(2031, 3, 4, 9, 0, tzinfo=CST)

PARTNER = {
    "contact_id": "c_partner",
    "user_id": CUSTOMER,
    "name": "爱人",
    "phone": "13800138002",
    "remark": "家人",
    "recipient_user_id": "u_partner",
    "recipient_account_id": "a_partner_current",
    "birthday": "1995-09-25",
}
PRODUCTS = [
    {"product_code": "flowers", "name": "模拟鲜花", "price_minor": 19900, "currency": "CNY"},
    {"product_code": "cake", "name": "模拟蛋糕", "price_minor": 12900, "currency": "CNY"},
]
CURRENT_ACCOUNT = {
    "account_id": "a_zhang_current",
    "user_id": CUSTOMER,
    "account_type": "current",
    "balance_minor": 2465950,
    "available_minor": 2465950,
    "currency": "CNY",
    "interest_rate": "0.0025",
    "status": "active",
}
ORDER = {
    "order_id": "o_birthday_1",
    "order_type": "birthday",
    "user_id": CUSTOMER,
    "amount_minor": 19900,
    "status": "pending",
    "task_id": "t_birthday_1",
    "transaction_id": None,
    "details": {
        "contact_id": "c_partner",
        "contact_name": "爱人",
        "product_code": "flowers",
        "product_name": "模拟鲜花",
        "birthday_date": "2031-09-25",
        "execute_at": "2031-09-23T08:00:00+08:00",
        "task_id": "t_birthday_1",
    },
}


class _FixedClock:
    @staticmethod
    def today(client=None) -> date:
        return BUSINESS_DAY

    @staticmethod
    def now(client=None) -> datetime:
        return NOW


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(life_tools, "SystemClock", _FixedClock)
    monkeypatch.setattr(write_facade_module, "SystemClock", _FixedClock)
    return _FixedClock


def bank() -> FakeBankClient:
    client = FakeBankClient()
    client.register("/birthdays", [PARTNER])
    client.register("/mock-products", PRODUCTS)
    client.register("/accounts", [CURRENT_ACCOUNT])
    client.register("/birthdays/orders", ORDER)
    return client


def _facade(client: FakeBankClient, rt) -> WriteFacade:
    return WriteFacade(
        client,
        audit_hook=build_fund_write_audit_hook(rt.audit),
        database=rt.database,
    )


def _invocation(tool: str, arguments: dict | None = None, *, key: str = "idem-life-1"):
    return ToolInvocation(
        "life_event",
        ToolCall(
            tool_name=tool,
            arguments=arguments or {},
            call_id="call_life_1",
            idempotency_key=key,
        ),
        TrustedContext(user_id=APP_USER, session_id="sess_life", bank_customer_id=CUSTOMER),
    )


def _frozen_intent(client: FakeBankClient) -> dict:
    """The intent `life_prepare_order` freezes -- what Task 3 stores in the checkpoint."""

    data = life_tools.prepare_order(
        _invocation("life_prepare_order", {"contact_id": "c_partner", "product_code": "flowers"}),
        client,
        database=None,
    )
    return dict(data.facts["intent"])


def _frozen_args(intent: dict) -> dict:
    """The frozen facts the confirmation-card callback hands to the hidden tool."""

    return {
        "contact_id": intent["contact_id"],
        "product_code": intent["product_code"],
        "from_account_id": intent["from_account_id"],
        "birthday_date": intent["birthday_date"],
        "amount_minor": intent["amount_minor"],
    }


def _accumulated(rt, user_id: str = APP_USER) -> int:
    with rt.database.sessions() as session:
        return DailyTotalStore.get_accumulated_minor(session, user_id, BUSINESS_DAY)


def _fund_rows(rt) -> list[dict]:
    return [
        row
        for row in rt.audit.reader.business_events(APP_USER)["items"]
        if row["resource_type"] == "fund_write"
    ]


def _posts(client: FakeBankClient) -> list[dict]:
    return [request for request in client.requests if request["method"] == "POST"]


# ------------------------------------------------------------------ 正常下单


def test_a_click_posts_the_frozen_order_and_lands_the_day_total_and_one_audit_row(rt, frozen_clock):
    client = bank()
    facade = _facade(client, rt)
    intent = _frozen_intent(client)

    data = life_tools.execute_order(
        _invocation("life_execute_order", _frozen_args(intent)), client, writes=facade
    )

    posts = _posts(client)
    assert [post["path"] for post in posts] == ["/birthdays/orders"]
    # 请求体就是银行要的那几项：金额只在门面里记账，不重复发给银行。
    assert posts[0]["json"] == {
        "contact_id": "c_partner",
        "product_code": "flowers",
        "from_account_id": "a_zhang_current",
        "birthday_date": "2031-09-25",
        "confirm": True,
    }
    assert posts[0]["idempotency_key"] == "idem-life-1"
    assert posts[0]["user_id"] == CUSTOMER

    assert _accumulated(rt) == 19900

    rows = _fund_rows(rt)
    assert [(row["action"], row["result"]) for row in rows] == [("birthday_order", "ok")]
    assert rows[0]["reason"] == "资金操作成功: birthday_order"

    assert data.facts["order"]["order_id"] == "o_birthday_1"
    assert data.business_ids == {"order_id": "o_birthday_1"}
    assert "o_birthday_1" in data.summary


# ------------------------------------------------------------------ 红卡带验证码


def test_the_tool_schema_never_declares_the_verification_code():
    """参数会进 SSE 帧与 run-event 审计；验证码只能是关键字参数。"""

    parameters = life_tools.TOOLS["life_execute_order"].parameters
    assert set(parameters["properties"]) == {
        "contact_id",
        "product_code",
        "from_account_id",
        "birthday_date",
        "amount_minor",
    }
    assert "verification_code" not in json.dumps(parameters, ensure_ascii=False)
    assert set(parameters["required"]) == set(parameters["properties"])
    assert parameters["additionalProperties"] is False


def test_a_verification_code_keyword_rides_in_the_body_as_mfa_code_and_never_lands_in_the_audit(
    rt, frozen_clock
):
    client = bank()
    facade = _facade(client, rt)
    intent = _frozen_intent(client)

    life_tools.execute_order(
        _invocation("life_execute_order", _frozen_args(intent)),
        client,
        writes=facade,
        verification_code="123456",
    )

    posts = _posts(client)
    assert posts[0]["json"]["mfa_code"] == "123456"

    # 验证码只走这一趟请求体；审计里必须是脱敏后的样子。
    assert "123456" not in json.dumps(
        rt.audit.reader.business_events(APP_USER), ensure_ascii=False, default=str
    )


# ------------------------------------------------------------------ 幂等重试


class _ReplayingBank(FakeBankClient):
    """A bank that dedupes birthday orders on the idempotency key, like `execute_once`."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: list[dict] = []
        self._by_key: dict[str, dict] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        user_id: str,
        params=None,
        json=None,
        idempotency_key=None,
    ) -> BankResponse:
        response = super().request(
            method,
            path,
            user_id=user_id,
            params=params,
            json=json,
            idempotency_key=idempotency_key,
        )
        if method != "POST" or path != "/birthdays/orders":
            return response
        stored = self._by_key.get(idempotency_key)
        if stored is not None:
            return BankResponse(code=0, message="success", data={**stored, "replayed": True})
        order = {**response.data, "replayed": False}
        self._by_key[idempotency_key] = order
        self.orders.append(order)
        return BankResponse(code=0, message="success", data=order)


def test_a_retry_with_the_same_key_reaches_the_bank_once_and_replays(rt, frozen_clock):
    client = _ReplayingBank()
    client.register("/birthdays", [PARTNER])
    client.register("/mock-products", PRODUCTS)
    client.register("/accounts", [CURRENT_ACCOUNT])
    client.register("/birthdays/orders", ORDER)
    facade = _facade(client, rt)
    intent = _frozen_intent(client)
    arguments = _frozen_args(intent)

    first = life_tools.execute_order(
        _invocation("life_execute_order", arguments), client, writes=facade
    )
    second = life_tools.execute_order(
        _invocation("life_execute_order", arguments), client, writes=facade
    )

    posts = _posts(client)
    assert [post["idempotency_key"] for post in posts] == ["idem-life-1", "idem-life-1"]
    assert posts[0]["json"] == posts[1]["json"]
    assert len(client.orders) == 1
    assert first.facts["order"]["replayed"] is False
    assert second.facts["order"]["replayed"] is True


# ------------------------------------------------------------------ 冻结快照缺金额


def test_a_frozen_snapshot_without_a_positive_amount_is_refused_before_the_bank(rt, frozen_clock):
    client = bank()
    facade = _facade(client, rt)
    intent = _frozen_intent(client)

    with pytest.raises(ToolFailure) as caught:
        life_tools.execute_order(
            _invocation("life_execute_order", {**_frozen_args(intent), "amount_minor": 0}),
            client,
            writes=facade,
        )

    assert caught.value.code == ErrorCode.VALIDATION
    # 没有金额就无从判当日限额；这单不许出门。
    assert _posts(client) == []


# ------------------------------------------------------------------ 模块 API 自己守门


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"amount_minor": None},
        {"amount_minor": 0},
        {"amount_minor": -19900},
        {"amount_minor": "19900"},
        {"amount_minor": True},
    ],
    ids=["missing", "null", "zero", "negative", "text", "bool"],
)
def test_submit_order_refuses_a_snapshot_without_a_positive_integer_amount(
    rt, frozen_clock, overrides
):
    """门面把 0 当「不动钱」：`submit_order` 是导出的模块 API，缺金额不许兜成 0 出门。"""

    client = bank()
    facade = _facade(client, rt)
    intent = {**_frozen_intent(client), **overrides}
    if not overrides:
        intent.pop("amount_minor")

    with pytest.raises(ToolFailure) as caught:
        life_writes.submit_order(
            client,
            _invocation("life_execute_order").context,
            intent,
            idempotency_key="idem-life-1",
            writes=facade,
        )

    assert caught.value.code == ErrorCode.VALIDATION
    assert _posts(client) == []
    assert _accumulated(rt) == 0


def test_submit_order_without_an_idempotency_key_never_reaches_the_bank(rt, frozen_clock):
    client = bank()
    facade = _facade(client, rt)
    intent = _frozen_intent(client)

    with pytest.raises(ToolFailure) as caught:
        life_writes.submit_order(
            client,
            _invocation("life_execute_order").context,
            intent,
            idempotency_key="   ",
            writes=facade,
        )

    assert caught.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    assert _posts(client) == []
    assert _accumulated(rt) == 0
