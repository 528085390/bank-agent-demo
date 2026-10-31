"""The bank transport: which failures are transient, and what the user is told.

The classification half is asserted as data, not by calling the bank: the two tables below
are the specification. Retryability is a property of the *kind*, never of the bank's `code`,
and the wording a user sees comes from this side -- the bank's own `message` is the class
default (`resource_not_found`), so it is both English and a non-reason.
"""

import json

import httpx
import pytest

from app.core.config import get_settings
from app.integration.bank_client import CODE_KINDS, BankClient
from app.integration.bank_errors import (
    BANK_ERROR_MESSAGES,
    RETRYABLE_KINDS,
    BankCallError,
    BankErrorKind,
    message_for,
)


def test_only_transient_failures_are_retryable():
    assert RETRYABLE_KINDS == frozenset(
        {
            BankErrorKind.CONNECT,
            BankErrorKind.TIMEOUT,
            BankErrorKind.RATE_LIMITED,
            BankErrorKind.SERVER,
        }
    )


def test_every_kind_has_a_chinese_message_and_none_are_english():
    assert set(BANK_ERROR_MESSAGES) == set(BankErrorKind)
    for message in BANK_ERROR_MESSAGES.values():
        assert message.strip()
        assert any("\u4e00" <= character <= "\u9fff" for character in message)


def test_a_business_failure_is_not_retryable_by_default():
    error = BankCallError(BankErrorKind.INSUFFICIENT_BALANCE, "余额不足")

    assert error.kind is BankErrorKind.INSUFFICIENT_BALANCE
    assert error.retryable is False


def test_a_transient_failure_has_to_say_so():
    error = BankCallError(BankErrorKind.TIMEOUT, "超时", retryable=True)

    assert error.retryable is True


@pytest.mark.parametrize("kind", list(BankErrorKind))
def test_every_kind_is_translated_by_kind_not_by_the_banks_text(kind: BankErrorKind):
    """`message_for` is the only route to a user-visible sentence, and it must be total.

    Asserting `message_for(kind) == BANK_ERROR_MESSAGES[kind]` would be a tautology -- it
    re-reads the same table. What is worth pinning is that the sentence is this side's
    wording, in Chinese, and that the bank's own text is nowhere in it.
    """

    message = message_for(kind)

    assert message.strip()
    assert any("\u4e00" <= character <= "\u9fff" for character in message)
    # bank_backend answers with a class-level default such as "resource_not_found".
    assert "resource_not_found" not in message
    assert not message.isascii()


def test_no_two_kinds_share_a_message():
    """A copy-pasted entry tells the user the wrong thing while every lookup test passes."""

    messages = list(BANK_ERROR_MESSAGES.values())

    assert len(messages) == len(set(messages))


# The transport half: what a real HTTP answer -- or a real transport failure -- becomes.
# bank_backend is not running: `httpx.MockTransport` stands in for it, so these tests are
# about the mapping and nothing else.


def client_returning(handler) -> BankClient:
    return BankClient("http://bank.test", transport=httpx.MockTransport(handler))


def envelope(code=0, message="success", data=None):
    return httpx.Response(200, json={"code": code, "message": message, "data": data})


def test_a_successful_call_returns_the_envelope():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return envelope(data={"accounts": []})

    response = client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert response.code == 0
    assert response.data == {"accounts": []}
    assert "user_id=u_zhang" in seen["url"]


def test_the_customer_id_is_always_sent_never_left_to_the_bank_default():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return envelope(data={})

    client_returning(handler).request("GET", "/accounts/balance", user_id="u_zhang")

    assert "user_id=u_zhang" in seen["url"]


def test_a_write_carries_the_customer_id_in_the_body_the_bank_actually_reads():
    """The GET routes read a query parameter, but the ten POST routes read `body.user_id`
    and ignore the query string -- so a body-only-less write ran as the bank's own default
    customer, moving the named account's money under someone else's name."""

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return envelope(data={})

    client_returning(handler).request(
        "POST", "/transfers", user_id="u_zhang", json={"from_account_id": "a"}
    )

    assert seen["body"]["user_id"] == "u_zhang"
    assert seen["body"]["from_account_id"] == "a"


def test_the_bound_customer_wins_over_one_a_caller_put_in_the_body():
    """Identity is the client's to state, so a module cannot name a different customer."""

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return envelope(data={})

    client_returning(handler).request(
        "POST", "/transfers", user_id="u_zhang", json={"user_id": "u_li", "amount_minor": 1}
    )

    assert seen["body"]["user_id"] == "u_zhang"


def test_a_read_without_a_body_does_not_grow_one():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return envelope(data={})

    client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert seen["body"] == b""


@pytest.mark.parametrize("unbound", [None, ""])
def test_a_caller_without_a_binding_never_reaches_the_bank(unbound):
    """The direction that matters: no binding means no request, not the bank's default.

    The bank treats `user_id` as optional and answers with its own default customer, so a
    call that goes out without one reads someone else's money.
    """

    sent = []

    def handler(request):
        sent.append(request)
        return envelope(data={})

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id=unbound)

    assert sent == []
    assert caught.value.retryable is False


def test_a_connection_failure_is_transient():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.CONNECT
    assert caught.value.retryable is True


def test_a_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.TIMEOUT
    assert caught.value.retryable is True


def test_a_transient_failure_is_not_retried_here():
    """One retry layer only: the supervisor owns it, so the client sends exactly one call."""

    sent = []

    def handler(request):
        sent.append(request)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.retryable is True
    assert len(sent) == 1


def test_too_many_requests_is_transient():
    # 9999 is outside the bank's own table (`bank_backend/app/core/errors.py`): a response
    # that carries no code we know is classified by its status.
    def handler(request):
        return httpx.Response(429, json={"code": 9999, "message": "busy", "data": None})

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.RATE_LIMITED
    assert caught.value.retryable is True


def test_a_server_fault_is_transient():
    def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.SERVER
    assert caught.value.retryable is True


def test_the_banks_wrong_code_is_its_own_kind():
    """1008 是银行对"验证码不对"的独立结论，不是 1007 的"需要确认"。

    对话侧必须能把两者分开：把一次验证失败当成"还没有确认"，运行时就会重新走一遍确认
    流程（而不是把原因如实告诉用户）。这条断言钉的是映射表本身。
    """

    assert CODE_KINDS[1008] is BankErrorKind.VERIFICATION_FAILED


def test_the_fund_tool_lock_is_its_own_kind():
    """1009 是银行对"资金工具被锁"的独立结论，不再复用 1005 的通用「状态不允许」。

    锁会同时命中 require_not_locked 的全部三个调用点（转账、申购、赎回），所以对话侧
    必须能把"被锁"和"状态不对"分开，否则用户只会看到一句无用的通用文案。
    """

    assert CODE_KINDS[1009] is BankErrorKind.LOCKED


def test_the_fund_tool_lock_tells_the_user_it_is_locked():
    """1009 的对话侧文案逐字固定，且只能来自生产代码的 `message_for`（不是通用状态文案）。"""

    assert message_for(BankErrorKind.LOCKED) == "该资金工具已锁定，请稍后重试。"


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        (1004, 404, BankErrorKind.NOT_FOUND),
        (1005, 409, BankErrorKind.INVALID_STATE),
        (1006, 409, BankErrorKind.INSUFFICIENT_BALANCE),
        (1007, 409, BankErrorKind.CONFIRMATION_REQUIRED),
        (1008, 409, BankErrorKind.VERIFICATION_FAILED),
        (1009, 409, BankErrorKind.LOCKED),
        (1012, 422, BankErrorKind.VALIDATION),
    ],
)
def test_a_business_error_code_wins_over_the_http_status(code, status, expected):
    def handler(request):
        return httpx.Response(status, json={"code": code, "message": "english", "data": None})

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is expected
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        (1004, 503, BankErrorKind.NOT_FOUND),
        (1005, 429, BankErrorKind.INVALID_STATE),
    ],
)
def test_a_known_code_wins_over_a_status_that_suggests_otherwise(code, status, expected):
    """The status is a coarse echo; the code is the bank's own statement of what failed.

    Both of these would be retryable if the status decided, so the code has to take that
    away -- otherwise a business rejection is retried as if it were an outage.
    """

    def handler(request):
        return httpx.Response(status, json={"code": code, "message": "english", "data": None})

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is expected
    assert caught.value.retryable is False


def test_an_unknown_4xx_is_a_client_error():
    def handler(request):
        return httpx.Response(403, json={"code": 9999, "message": "nope", "data": None})

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.CLIENT
    assert caught.value.retryable is False


def test_a_body_that_is_not_an_envelope_is_a_protocol_error():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.kind is BankErrorKind.PROTOCOL
    assert caught.value.retryable is False


def test_the_banks_own_english_message_never_reaches_the_caller():
    def handler(request):
        return httpx.Response(
            404, json={"code": 1004, "message": "resource_not_found", "data": None}
        )

    with pytest.raises(BankCallError) as caught:
        client_returning(handler).request("GET", "/accounts", user_id="u_zhang")

    assert caught.value.message == message_for(BankErrorKind.NOT_FOUND)
    assert "resource_not_found" not in caught.value.message


def test_no_failure_path_ever_shows_the_banks_own_text():
    """The bank's `message` is a class default (`resource_not_found`), not a reason, and it
    is English. Every path that fails has to carry our own sentence instead."""

    marker = "resource_not_found"
    answers = [
        lambda request: httpx.Response(404, json={"code": 1004, "message": marker, "data": None}),
        lambda request: httpx.Response(409, json={"code": 1006, "message": marker, "data": None}),
        lambda request: httpx.Response(422, json={"code": 1012, "message": marker, "data": None}),
        lambda request: httpx.Response(403, json={"code": 9999, "message": marker, "data": None}),
        lambda request: httpx.Response(429, json={"code": 9999, "message": marker, "data": None}),
        lambda request: httpx.Response(503, json={"code": 0, "message": marker, "data": None}),
        lambda request: httpx.Response(200, text=f"<html>{marker}</html>"),
        lambda request: httpx.Response(200, json={"message": marker, "data": None}),
    ]

    for answer in answers:
        with pytest.raises(BankCallError) as caught:
            client_returning(answer).request("GET", "/accounts", user_id="u_zhang")

        assert caught.value.message == message_for(caught.value.kind)
        assert marker not in caught.value.message


def test_an_idempotency_key_travels_as_a_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("Idempotency-Key")
        return envelope(data={})

    client_returning(handler).request(
        "POST", "/transfers", user_id="u_zhang", json={"a": 1}, idempotency_key="k1"
    )

    assert seen["key"] == "k1"


def test_from_settings_points_the_client_at_the_configured_bank():
    settings = get_settings()
    client = BankClient.from_settings()
    try:
        assert str(client._client.base_url).rstrip("/") == settings.bank_backend_base_url
    finally:
        client.close()


def test_the_configured_bank_url_carries_the_api_prefix():
    """bank_backend mounts its whole router under `/api`, so a module writes the resource
    path alone ("/accounts") and the prefix has to come from the base URL. Without it every
    call 404s, and a codeless 404 is reported as PROTOCOL -- a confusing failure to debug."""

    assert get_settings().bank_backend_base_url.rstrip("/").endswith("/api")


def test_a_module_path_reaches_the_api_prefix_without_repeating_it():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return envelope(data={})

    client = BankClient(
        base_url="http://127.0.0.1:8001/api",
        connect_timeout=1.0,
        read_timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.request("GET", "/accounts", user_id="u_zhang")
    finally:
        client.close()

    assert seen["url"].startswith("http://127.0.0.1:8001/api/accounts")
