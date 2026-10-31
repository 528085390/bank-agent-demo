"""Unit coverage for decide-route card authorize (no Postgres)."""

import asyncio
from types import SimpleNamespace

import pytest

from app.api.routes import _authorize_card_action_if_needed, _click_matches, _requires_code
from app.core.errors import AppError
from tests.test_card_module import ACTION_ID, CardBank


class _Identity:
    def bank_customer_id(self, user_id):
        assert user_id == "user_1"
        return "u_zhang"


def _rt(bank):
    return SimpleNamespace(bank_client=bank, identity=_Identity())


def _pending(*, requires_code=False, pending_id=ACTION_ID, question_id="q_1"):
    return {
        "kind": "confirmation",
        "question_id": question_id,
        "confirmation": {
            "pending_id": pending_id,
            "operation": "freeze",
            "title": "冻结银行卡",
            "amount_minor": 0,
            "product_name": "借记卡",
            "account_display": "借记卡",
            "confirm_label": "确认",
            "cancel_label": "取消",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "requires_code": requires_code,
            "code_label": "模拟验证码",
        },
    }


def test_click_matches_and_requires_code_for_card_confirmation():
    pending = _pending(requires_code=True)
    assert _click_matches(pending, "q_1", ACTION_ID)
    assert _requires_code(pending) is True
    assert not _click_matches(pending, "q_other", ACTION_ID)
    assert not _click_matches(pending, "q_1", "wp_1")


def test_authorize_skipped_for_non_card_pending_id():
    bank = CardBank()
    before = len(bank.requests)
    asyncio.run(
        _authorize_card_action_if_needed(
            _rt(bank),
            {"user_id": "user_1"},
            _pending(pending_id="wp_wealth_1"),
            "confirm",
            "123456",
        )
    )
    assert bank.requests == []
    assert len(bank.requests) == before


def test_authorize_skipped_on_cancel():
    bank = CardBank()
    asyncio.run(
        _authorize_card_action_if_needed(
            _rt(bank), {"user_id": "user_1"}, _pending(), "cancel", None
        )
    )
    assert bank.requests == []


def test_authorize_confirm_marks_bank_authorized():
    bank = CardBank()
    asyncio.run(
        _authorize_card_action_if_needed(
            _rt(bank), {"user_id": "user_1"}, _pending(), "confirm", None
        )
    )
    assert bank.authorized
    assert any(item["path"].endswith("/authorize") for item in bank.requests)
    assert bank.requests[-1]["json"] == {"decision": "confirm", "verification_code": None}


def test_authorize_wrong_code_raises_without_authorizing():
    bank = CardBank()

    def request(method, path, **kwargs):
        if path.endswith("/authorize"):
            bank.requests.append({"method": method, "path": path, **kwargs})
            bank.authorized = False
            from app.integration.bank_client import BankResponse

            return BankResponse(
                code=0,
                message="success",
                data={
                    "status": "pending",
                    "verification_passed": False,
                    "verification_failures": 1,
                },
            )
        return CardBank.request(bank, method, path, **kwargs)

    bank.request = request  # type: ignore[method-assign]
    with pytest.raises(AppError) as raised:
        asyncio.run(
            _authorize_card_action_if_needed(
                _rt(bank),
                {"user_id": "user_1"},
                _pending(requires_code=True),
                "confirm",
                "000000",
            )
        )
    assert raised.value.code == "VERIFICATION_FAILED"
    assert raised.value.status == 422
    assert not bank.authorized


def _bank_answering(data):
    """A CardBank whose /authorize answers `data` (same monkeypatch style as :89-109)."""

    bank = CardBank()

    def request(method, path, **kwargs):
        if path.endswith("/authorize"):
            bank.requests.append({"method": method, "path": path, **kwargs})
            bank.authorized = False
            from app.integration.bank_client import BankResponse

            return BankResponse(code=0, message="success", data=data)
        return CardBank.request(bank, method, path, **kwargs)

    bank.request = request  # type: ignore[method-assign]
    return bank


def _cancelled_bank():
    return _bank_answering(
        {"status": "cancelled", "verification_passed": False, "verification_failures": 3}
    )


def test_exhausted_attempts_come_back_as_cancelled_not_as_a_raise():
    bank = _cancelled_bank()
    outcome = asyncio.run(
        _authorize_card_action_if_needed(
            _rt(bank), {"user_id": "user_1"}, _pending(requires_code=True), "confirm", "000000"
        )
    )
    assert outcome.status == "cancelled"
    assert outcome.remaining == 0
    assert not bank.authorized


def test_a_wrong_code_still_raises_with_the_status_in_details():
    bank = _bank_answering(
        {"status": "pending", "verification_passed": False, "verification_failures": 1}
    )
    with pytest.raises(AppError) as raised:
        asyncio.run(
            _authorize_card_action_if_needed(
                _rt(bank), {"user_id": "user_1"}, _pending(requires_code=True), "confirm", "000000"
            )
        )
    assert raised.value.code == "VERIFICATION_FAILED"
    assert raised.value.details["status"] == "pending"
    assert not bank.authorized


def test_a_cancel_click_authorizes_nothing():
    bank = CardBank()
    outcome = asyncio.run(
        _authorize_card_action_if_needed(
            _rt(bank), {"user_id": "user_1"}, _pending(requires_code=True), "cancel", None
        )
    )
    assert outcome.status == "none"
    assert bank.requests == []
