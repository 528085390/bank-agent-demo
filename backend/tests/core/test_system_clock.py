import logging
import time
from datetime import date, datetime, timedelta

import httpx
import pytest

import app.core.clock as clock_module
from app.core.clock import _CLOCK_CACHE_TTL_SECONDS, CST, SystemClock


def _reset_clock_module_state() -> None:
    """把时钟模块的全部进程级全局恢复成刚导入时的状态。

    app.core.clock 的缓存是模块级全局：前一个用例成功读到的
    _last_bank_datetime 若泄漏进来，从未成功过的降级用例会假绿，
    反过来 stale 用例也会被上一个用例的残留状态污染。
    """
    clock_module._cached_datetime = None
    clock_module._cached_at = 0.0
    clock_module._current_source = "local-fallback"
    clock_module._last_bank_datetime = None
    client, clock_module._default_client = clock_module._default_client, None
    if client is not None:
        client.close()


@pytest.fixture(autouse=True)
def _reset_clock_state():
    """Ensure each test runs with a clean module-level clock state."""
    _reset_clock_module_state()
    yield
    _reset_clock_module_state()


def _bank_time_client(iso_str: str) -> httpx.Client:
    """Mock client that always answers GET /clock with iso_str."""
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"code": 200, "data": {"current_time": iso_str}},
                request=req,
            )
        ),
        base_url="http://127.0.0.1:8001/api",
    )


def _broken_bank_client(kind: str, counter: list[int] | None = None) -> httpx.Client:
    """Mock client that always fails; counter counts real HTTP attempts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter[0] += 1
        if kind == "connect-error":
            raise httpx.ConnectError("Connection refused to bank service")
        if kind == "http-500":
            return httpx.Response(500, text="Internal Server Error", request=request)
        if kind == "missing-current-time":
            return httpx.Response(200, json={"code": 200, "data": {}}, request=request)
        raise AssertionError(f"unknown failure kind: {kind}")

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8001/api",
    )


def _freeze_monotonic(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Freeze time.monotonic so a test can step over the 3.0 s TTL exactly."""
    current = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: current[0])
    return current


def test_now_returns_datetime_with_cst_timezone():
    dt = SystemClock.now()
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.tzinfo == CST


def test_today_returns_date():
    today = SystemClock.today()
    assert isinstance(today, date)
    assert not isinstance(today, datetime)
    assert today == SystemClock.now().date()


def test_instance_methods():
    clock = SystemClock()
    assert clock.now().tzinfo == CST
    assert isinstance(clock.today(), date)
    clock.invalidate()


def test_mock_bank_200_returns_exact_cst_time():
    mock_response = httpx.Response(
        200,
        json={"code": 200, "data": {"current_time": "2026-11-08T08:00:00+08:00"}},
        request=httpx.Request("GET", "http://127.0.0.1:8001/api/clock"),
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: mock_response),
        base_url="http://127.0.0.1:8001/api",
    )

    now = SystemClock.now(client=client)
    assert now == datetime(2026, 11, 8, 8, 0, 0, tzinfo=CST)
    assert now.tzinfo == CST
    assert SystemClock.today(client=client) == date(2026, 11, 8)
    assert SystemClock.source() == "bank"
    assert SystemClock.is_fallback() is False


def test_mock_bank_200_with_naive_and_utc_iso_string():
    # Naive ISO string should assume CST
    mock_response_naive = httpx.Response(
        200,
        json={"code": 200, "data": {"current_time": "2026-10-25T14:30:00"}},
        request=httpx.Request("GET", "http://127.0.0.1:8001/api/clock"),
    )
    client_naive = httpx.Client(
        transport=httpx.MockTransport(lambda req: mock_response_naive),
        base_url="http://127.0.0.1:8001/api",
    )
    SystemClock.invalidate()
    res_naive = SystemClock.now(client=client_naive)
    assert res_naive == datetime(2026, 10, 25, 14, 30, 0, tzinfo=CST)
    assert res_naive.tzinfo == CST
    assert SystemClock.source() == "bank"

    # UTC ISO string ("Z") should be converted to CST
    mock_response_utc = httpx.Response(
        200,
        json={"code": 200, "data": {"current_time": "2026-10-25T06:30:00Z"}},
        request=httpx.Request("GET", "http://127.0.0.1:8001/api/clock"),
    )
    client_utc = httpx.Client(
        transport=httpx.MockTransport(lambda req: mock_response_utc),
        base_url="http://127.0.0.1:8001/api",
    )
    SystemClock.invalidate()
    res_utc = SystemClock.now(client=client_utc)
    assert res_utc == datetime(2026, 10, 25, 14, 30, 0, tzinfo=CST)
    assert res_utc.tzinfo == CST
    assert SystemClock.source() == "bank"


def test_cache_hit_and_invalidate_behavior():
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"code": 200, "data": {"current_time": f"2026-10-25T14:30:0{call_count}+08:00"}},
            request=request,
        )

    client = httpx.Client(
        transport=httpx.MockTransport(mock_handler),
        base_url="http://127.0.0.1:8001/api",
    )

    t1 = SystemClock.now(client=client)
    assert call_count == 1
    assert t1 == datetime(2026, 10, 25, 14, 30, 1, tzinfo=CST)

    t2 = SystemClock.now(client=client)
    assert call_count == 1
    assert t2 == t1

    SystemClock.invalidate()
    t3 = SystemClock.now(client=client)
    assert call_count == 2
    assert t3 == datetime(2026, 10, 25, 14, 30, 2, tzinfo=CST)


def test_cache_ttl_expiry(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"code": 200, "data": {"current_time": f"2026-10-25T14:30:0{call_count}+08:00"}},
            request=request,
        )

    client = httpx.Client(
        transport=httpx.MockTransport(mock_handler),
        base_url="http://127.0.0.1:8001/api",
    )

    current_time = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: current_time[0])

    t1 = SystemClock.now(client=client)
    assert call_count == 1

    current_time[0] += 2.0
    t2 = SystemClock.now(client=client)
    assert call_count == 1
    assert t2 == t1

    current_time[0] += 1.5
    t3 = SystemClock.now(client=client)
    assert call_count == 2
    assert t3 != t1


def test_mock_bank_network_failure_fallback_with_warning(caplog):
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused to bank service")

    client = httpx.Client(
        transport=httpx.MockTransport(failing_handler),
        base_url="http://127.0.0.1:8001/api",
    )

    with caplog.at_level(logging.WARNING):
        dt = SystemClock.now(client=client)
    assert isinstance(dt, datetime)
    assert dt.tzinfo == CST
    assert SystemClock.source() == "local-fallback"
    assert SystemClock.is_fallback() is True
    assert any("读取银行模拟时钟失败" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "malformed_response_factory",
    [
        # 1. 畸形 JSON (not json)
        lambda: httpx.Response(200, text="not json at all"),
        # 2. 空响应体 (empty body)
        lambda: httpx.Response(200, text=""),
        # 3. 缺少 data 键 (data missing)
        lambda: httpx.Response(200, json={"code": 200}),
        # 4. data 是列表而非字典 (data is list -> 曾引发 AttributeError)
        lambda: httpx.Response(200, json={"code": 200, "data": ["item1", "item2"]}),
        # 5. current_time 不是字符串 (current_time not str -> 曾引发 TypeError)
        lambda: httpx.Response(200, json={"code": 200, "data": {"current_time": 123456}}),
        # 6. data 为 None (data null)
        lambda: httpx.Response(200, json={"code": 200, "data": None}),
        # 7. HTTP 500 内部服务错误
        lambda: httpx.Response(500, text="Internal Server Error"),
    ],
)
def test_all_seven_malformed_bank_responses_fallback_safely_without_raising(
    malformed_response_factory, caplog
):
    """N1 关键验收：验证 7 种畸形响应全部安全降级为 local-fallback，绝不抛出任何异常打崩主请求。"""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: malformed_response_factory()),
        base_url="http://127.0.0.1:8001/api",
    )
    SystemClock.invalidate()
    with caplog.at_level(logging.WARNING):
        dt = SystemClock.now(client=client)

    assert isinstance(dt, datetime)
    assert dt.tzinfo == CST
    assert SystemClock.source() == "local-fallback"
    assert SystemClock.is_fallback() is True


# ---------------------------------------------------------------------------
# Fix C1: 银行 /clock 失败时不得跳到墙钟时间
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_kind", ["connect-error", "http-500", "missing-current-time"])
def test_success_then_failure_serves_the_last_bank_reading(failure_kind, monkeypatch, caplog):
    """银行失败时系统时钟必须停在上一次银行读数，不得跳到墙钟。"""
    clock = _freeze_monotonic(monkeypatch)
    healthy = _bank_time_client("2031-03-15T09:30:00+08:00")
    first = SystemClock.now(client=healthy)
    assert first == datetime(2031, 3, 15, 9, 30, tzinfo=CST)
    assert SystemClock.source() == "bank"
    assert SystemClock.is_stale() is False

    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    with caplog.at_level(logging.WARNING):
        stale = SystemClock.now(client=_broken_bank_client(failure_kind))

    assert stale == first
    assert stale.date() == date(2031, 3, 15)
    assert SystemClock.source() == "bank-stale"
    assert SystemClock.is_stale() is True
    assert SystemClock.is_fallback() is False


def test_failure_without_prior_success_still_falls_back_to_the_wall_clock():
    """从未成功读到银行时钟时，保留旧的本机降级行为。"""
    before = datetime.now(CST)
    dt = SystemClock.now(client=_broken_bank_client("connect-error"))
    after = datetime.now(CST)

    assert before <= dt <= after
    assert clock_module._last_bank_datetime is None
    assert SystemClock.source() == "local-fallback"
    assert SystemClock.is_fallback() is True
    assert SystemClock.is_stale() is False


def test_invalidate_then_failure_stays_stale_on_the_bank_date():
    """invalidate() 只清缓存，最后已知的银行读数必须活下来。"""
    healthy = _bank_time_client("2031-03-15T09:30:00+08:00")
    first = SystemClock.now(client=healthy)
    assert first == datetime(2031, 3, 15, 9, 30, tzinfo=CST)

    SystemClock.invalidate()
    broken = _broken_bank_client("connect-error")
    stale = SystemClock.now(client=broken)

    assert stale == first
    assert SystemClock.source() == "bank-stale"
    assert SystemClock.today(client=broken) == date(2031, 3, 15)
    assert SystemClock.is_fallback() is False


def test_today_is_identical_across_a_stale_failure(monkeypatch):
    """一次网络抖动不得让业务日期跨年跳变。"""
    clock = _freeze_monotonic(monkeypatch)
    healthy = _bank_time_client("2031-03-15T09:30:00+08:00")
    before = SystemClock.today(client=healthy)
    assert before == date(2031, 3, 15)

    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    after = SystemClock.today(client=_broken_bank_client("connect-error"))

    assert after == before
    assert SystemClock.source() == "bank-stale"


def test_stale_failure_refreshes_the_ttl_so_a_dead_bank_is_retried(monkeypatch):
    """stale 时仍按 3.0s TTL 重试，而不是每次调用都去打已经死掉的银行。"""
    clock = _freeze_monotonic(monkeypatch)
    SystemClock.now(client=_bank_time_client("2031-03-15T09:30:00+08:00"))

    attempts = [0]
    broken = _broken_bank_client("connect-error", counter=attempts)
    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    SystemClock.now(client=broken)
    assert attempts[0] == 1

    SystemClock.now(client=broken)
    SystemClock.today(client=broken)
    assert attempts[0] == 1

    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    SystemClock.now(client=broken)
    assert attempts[0] == 2


def test_stale_warning_fires_once_per_transition(monkeypatch, caplog):
    """进入 stale 只 warning 一次，恢复后再次进入再 warning 一次。"""
    clock = _freeze_monotonic(monkeypatch)
    healthy = _bank_time_client("2031-03-15T09:30:00+08:00")
    broken = _broken_bank_client("connect-error")
    SystemClock.now(client=healthy)

    with caplog.at_level(logging.WARNING, logger="app.core.clock"):
        for _ in range(3):
            clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
            SystemClock.now(client=broken)
        assert len(caplog.records) == 1

        clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
        SystemClock.now(client=healthy)
        assert SystemClock.source() == "bank"

        clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
        SystemClock.now(client=broken)
        assert len(caplog.records) == 2


def test_each_successful_reading_updates_the_kept_bank_time(monkeypatch):
    """每次成功解析都刷新最后银行读数，而不是只记住第一次。"""
    clock = _freeze_monotonic(monkeypatch)
    SystemClock.now(client=_bank_time_client("2031-03-15T09:30:00+08:00"))

    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    second = SystemClock.now(client=_bank_time_client("2031-03-16T10:00:00+08:00"))
    assert second == datetime(2031, 3, 16, 10, 0, tzinfo=CST)
    assert clock_module._last_bank_datetime == second

    clock[0] += _CLOCK_CACHE_TTL_SECONDS + 1.0
    stale = SystemClock.now(client=_broken_bank_client("connect-error"))
    assert stale == second
    assert stale.date() == date(2031, 3, 16)


def test_default_client_carries_the_bank_service_token(monkeypatch):
    """默认 HTTP 客户端必须带银行服务令牌，否则 /clock 会 401 并悄悄降级到本机时间。"""

    class _Settings:
        bank_backend_base_url = "http://127.0.0.1:8001/api"
        bank_service_token = "svc-token-for-clock"

    monkeypatch.setattr(clock_module, "get_settings", lambda: _Settings())

    client = clock_module._get_client()

    assert client.headers["X-Service-Token"] == "svc-token-for-clock"
