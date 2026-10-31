"""转账模块自己的受控写路径：一次点击只写一次，且只写银行那一把 POST /transfers。"""

from __future__ import annotations

import pytest

from app.integration.bank_client import BankResponse
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.integration.write_facade import WriteFacade
from app.modules.base import ToolFailure
from app.modules.transfer.writes import cancel_aa, cancel_scheduled, create_aa, pay_aa, submit_transfer
from app.tools.protocol import ErrorCode, TrustedContext

CONTEXT = TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id="u_zhang")
INTENT = {
    "from_account_id": "a_zhang_current",
    "to_account_id": "a_partner_current",
    "amount_minor": 500000,
    "remark": "测试",
}
PENDING = {"pending_id": "tp_1", "idempotency_key": "transfer-key-1"}


class RecordingBank:
    """Records every call; raises whatever error the test handed it."""

    def __init__(self, error=None):
        self.requests = []
        self.responses = {}
        self.error = error

    def register(self, path, data, *, code=0):
        self.responses[path] = BankResponse(code=code, message="success", data=data)

    def request(self, method, path, *, user_id, params=None, json=None, idempotency_key=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "user_id": user_id,
                "params": dict(params or {}),
                "json": json,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses[path]


def test_submit_transfer_sends_one_confirming_write_with_the_frozen_key():
    bank = RecordingBank()
    bank.register("/transfers", {"transaction_id": "t_1"})

    response = submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code="123456")

    assert response.data == {"transaction_id": "t_1"}
    assert len(bank.requests) == 1
    request = bank.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/transfers"
    assert request["user_id"] == "u_zhang"
    assert request["idempotency_key"] == "transfer-key-1"
    assert request["json"] == {
        "from_account_id": "a_zhang_current",
        "to_account_id": "a_partner_current",
        "amount_minor": 500000,
        "remark": "测试",
        "confirm": True,
        "mfa_code": "123456",
        "idempotent_key": "transfer-key-1",
    }


def test_submit_transfer_sends_no_code_when_the_card_had_none():
    bank = RecordingBank()
    bank.register("/transfers", {"transaction_id": "t_1"})

    submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code=None)

    assert bank.requests[0]["json"]["mfa_code"] is None


def test_submit_transfer_strips_a_padded_code():
    bank = RecordingBank()
    bank.register("/transfers", {"transaction_id": "t_1"})

    submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code=" 123456 ")

    assert bank.requests[0]["json"]["mfa_code"] == "123456"


def test_submit_transfer_refuses_a_non_numeric_code_before_the_bank():
    bank = RecordingBank()

    with pytest.raises(ToolFailure) as failure:
        submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code="abc")

    assert failure.value.code == "CONFIRMATION_REQUIRED"
    assert bank.requests == []


def test_submit_transfer_maps_the_bank_confirmation_refusal():
    bank = RecordingBank(error=BankCallError(BankErrorKind.CONFIRMATION_REQUIRED, "需要确认"))

    with pytest.raises(ToolFailure) as failure:
        submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code=None)

    assert failure.value.code == "CONFIRMATION_REQUIRED"
    assert failure.value.message == "该操作需要额外确认。"
    assert len(bank.requests) == 1


def test_submit_transfer_maps_insufficient_balance():
    bank = RecordingBank(error=BankCallError(BankErrorKind.INSUFFICIENT_BALANCE, "余额不足"))

    with pytest.raises(ToolFailure) as failure:
        submit_transfer(bank, CONTEXT, INTENT, PENDING, verification_code=None)

    assert failure.value.code == "INSUFFICIENT_BALANCE"


def test_submit_transfer_refuses_without_a_bank_customer():
    bank = RecordingBank()
    anonymous = TrustedContext(user_id="user_0001", session_id="sess_1")

    with pytest.raises(ToolFailure) as failure:
        submit_transfer(bank, anonymous, INTENT, PENDING, verification_code=None)

    assert failure.value.code == "NO_CUSTOMER_BINDING"
    assert bank.requests == []


def test_cancel_scheduled_sends_the_key_and_returns_the_bank_payload():
    bank = RecordingBank()
    bank.register("/transfers/scheduled/task_1/cancel", {"status": "cancelled"})

    response = cancel_scheduled(bank, CONTEXT, "task_1", idempotency_key="k1")

    assert response.data == {"status": "cancelled"}
    request = bank.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/transfers/scheduled/task_1/cancel"
    assert request["idempotency_key"] == "k1"
    assert request["user_id"] == "u_zhang"
    assert request["json"] == {}


def test_cancel_scheduled_maps_a_missing_task():
    bank = RecordingBank(error=BankCallError(BankErrorKind.NOT_FOUND, "没有这个任务"))

    with pytest.raises(ToolFailure) as failure:
        cancel_scheduled(bank, CONTEXT, "task_9", idempotency_key="k1")

    assert failure.value.code == "NOT_FOUND"


# ------------------------------------------------------------------ 经 WriteFacade 的门面策略


def test_submit_transfer_routes_through_the_write_facade_and_redacts_the_code():
    bank = RecordingBank()
    bank.register("/transfers", {"transaction_id": "t_1"})
    events: list[tuple[str, dict]] = []
    writes = WriteFacade(
        bank, audit_hook=lambda name, payload: events.append((name, dict(payload)))
    )

    response = submit_transfer(
        bank, CONTEXT, INTENT, PENDING, verification_code="123456", writes=writes
    )

    assert response.data == {"transaction_id": "t_1"}
    assert [name for name, _ in events] == ["fund_write_attempt", "fund_write_success"]
    for _name, payload in events:
        assert payload["body"]["mfa_code"] == "***"
        assert "123456" not in str(payload)
    # The bank still receives the raw code; only the audit copy is redacted.
    assert bank.requests[0]["json"]["mfa_code"] == "123456"
    assert bank.requests[0]["idempotency_key"] == "transfer-key-1"


def test_submit_transfer_without_an_idempotency_key_fails_the_contract_code():
    bank = RecordingBank()
    bank.register("/transfers", {"transaction_id": "t_1"})

    for pending in ({}, {"pending_id": "tp_1"}, {"pending_id": "tp_1", "idempotency_key": ""}):
        with pytest.raises(ToolFailure) as failure:
            submit_transfer(bank, CONTEXT, INTENT, pending, verification_code=None)
        assert failure.value.code == ErrorCode.IDEMPOTENCY_KEY_REQUIRED

    assert bank.requests == []


def test_cancel_scheduled_routes_through_the_write_facade():
    bank = RecordingBank()
    bank.register("/transfers/scheduled/task_1/cancel", {"status": "cancelled"})
    events: list[tuple[str, dict]] = []
    writes = WriteFacade(
        bank, audit_hook=lambda name, payload: events.append((name, dict(payload)))
    )

    response = cancel_scheduled(bank, CONTEXT, "task_1", idempotency_key="k1", writes=writes)

    assert response.data == {"status": "cancelled"}
    assert [name for name, _ in events] == ["fund_write_attempt", "fund_write_success"]
    for _name, payload in events:
        assert payload["idempotency_key"] == "k1"
        assert payload["operation"] == "cancel_scheduled_transfer"
    assert bank.requests[0]["idempotency_key"] == "k1"


# ------------------------------------------------------------------ AA 收款：发起 / 付份额 / 取消


AA_INTENT = {
    "operation": "aa_collect",
    "from_account_id": "a_zhang_current",
    "total_amount_minor": 30000,
    "participants": [
        {"name": "老王", "phone": None, "account_id": "a_partner_current"},
        {"name": "小李", "phone": None, "account_id": None},
    ],
    "remark": "聚餐",
}
AA_PAY_INTENT = {
    "operation": "aa_payment",
    "aa_request_id": "aa_1",
    "participant_index": 0,
    "amount_minor": 15000,
    "from_account_id": "a_zhang_current",
}


class SpyFacade(WriteFacade):
    """捕获 send() 的关键字参数，断言 amount_minor / operation 这些门面契约。"""

    def __init__(self, bank):
        super().__init__(bank)
        self.sends = []

    def send(self, *args, **kwargs):
        self.sends.append(kwargs)
        return super().send(*args, **kwargs)


def test_create_aa_sends_one_request_that_moves_no_money():
    bank = RecordingBank()
    bank.register("/transfers/aa", {"aa_request_id": "aa_1", "status": "open"})
    writes = SpyFacade(bank)

    response = create_aa(bank, CONTEXT, AA_INTENT, PENDING, verification_code=None, writes=writes)

    assert response.data == {"aa_request_id": "aa_1", "status": "open"}
    assert len(bank.requests) == 1
    request = bank.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/transfers/aa"
    assert request["user_id"] == "u_zhang"
    assert request["idempotency_key"] == "transfer-key-1"
    assert request["json"] == {
        "from_account_id": "a_zhang_current",
        "total_amount_minor": 30000,
        "participants": AA_INTENT["participants"],
        "remark": "聚餐",
        "confirm": True,
        "mfa_code": None,
        "idempotent_key": "transfer-key-1",
    }
    # 发起 AA 只是收款请求：门面不带 amount_minor —— 不占当日累计、不触发限额复检。
    assert len(writes.sends) == 1
    assert writes.sends[0]["amount_minor"] is None
    assert writes.sends[0]["operation"] == "aa_collect"


def test_pay_aa_carries_the_share_as_a_fund_write():
    bank = RecordingBank()
    bank.register("/transfers/aa/aa_1/pay", {"transaction_id": "t_aa_1"})
    writes = SpyFacade(bank)

    response = pay_aa(
        bank, CONTEXT, AA_PAY_INTENT, PENDING, verification_code="123456", writes=writes
    )

    assert response.data == {"transaction_id": "t_aa_1"}
    request = bank.requests[0]
    assert request["path"] == "/transfers/aa/aa_1/pay"
    assert request["json"] == {
        "participant_index": 0,
        "confirm": True,
        "mfa_code": "123456",
        "idempotent_key": "transfer-key-1",
    }
    # 付份额是真资金写：份额作为 amount_minor 到门面，点击时限额复检、成功后记日累计。
    assert writes.sends[0]["amount_minor"] == 15000
    assert writes.sends[0]["operation"] == "aa_payment"
    assert writes.sends[0]["verification_code"] == "123456"


def test_pay_aa_refuses_a_missing_participant_index_before_the_bank():
    bank = RecordingBank()

    with pytest.raises(ToolFailure) as failure:
        pay_aa(
            bank,
            CONTEXT,
            {**AA_PAY_INTENT, "participant_index": None},
            PENDING,
            verification_code=None,
        )

    assert failure.value.code == "VALIDATION"
    assert bank.requests == []


def test_cancel_aa_sends_one_write_with_the_key():
    bank = RecordingBank()
    bank.register("/transfers/aa/aa_1/cancel", {"aa_request_id": "aa_1", "status": "cancelled"})

    response = cancel_aa(bank, CONTEXT, "aa_1", idempotency_key="k1")

    assert response.data == {"aa_request_id": "aa_1", "status": "cancelled"}
    request = bank.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/transfers/aa/aa_1/cancel"
    assert request["json"] == {}
    assert request["idempotency_key"] == "k1"


def test_create_aa_maps_a_bank_refusal():
    bank = RecordingBank(error=BankCallError(BankErrorKind.INVALID_STATE, "已关闭"))

    with pytest.raises(ToolFailure) as failure:
        create_aa(bank, CONTEXT, AA_INTENT, PENDING, verification_code=None)

    assert failure.value.code == "INVALID_STATE"
