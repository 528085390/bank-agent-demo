"""系统统一时钟访问器。

提供统一的当前时间与日期访问能力，时区固定为东八区（CST = UTC+8）。
优先同步 bank_backend 的模拟时钟（GET /clock），以便测试与演示拨钟场景。
时钟源共有三种状态，由 source() 暴露给前端面板与调用方：

  - "bank"           最近一次读取银行 /clock 成功；
  - "bank-stale"     本次读取失败，沿用最后一次成功的银行读数（_last_bank_datetime）；
  - "local-fallback" 从未成功读取过银行时钟，只能降级到本机系统时间。

演示态银行模拟日（2031）与本机墙钟（2026）相差数年，所以银行读取失败时不得
切换到本机时间：那会让 SystemClock.today() 跨年跳变，日累计落到另一个分区、
1000 元限额被静默重置、WriteFacade.send 的执行期预检失效。只有从未成功读到过
银行时钟才允许本机降级，那是唯一一种会偏离银行模拟日的情况。
具备 3.0 秒本地 TTL 缓存，避免频繁轮询（stale 时同样刷新 TTL：死掉的银行按 TTL
重试，而不是被打爆）；支持外部在拨钟后显式调用 invalidate() 清除缓存，
invalidate() 会保留最后银行读数。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
_CLOCK_CACHE_TTL_SECONDS: float = 3.0

_cached_datetime: datetime | None = None
_cached_at: float = 0.0
_current_source: str = "local-fallback"
# 最后一次成功解析出的银行时间。银行读取失败时用它兜底，保证业务日期不跳到墙钟。
_last_bank_datetime: datetime | None = None
_default_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """获取或初始化持久化 module-level httpx.Client。"""
    global _default_client
    if _default_client is None or _default_client.is_closed:
        settings = get_settings()
        base = settings.bank_backend_base_url.rstrip("/")
        host = httpx.URL(base).host
        trust_env = host not in ("127.0.0.1", "localhost", "::1")
        # 银行所有非豁免接口都要求 X-Service-Token，/clock 也不例外；
        # 缺这个头时 401 会被时钟降级逻辑吞掉，表现为业务日期悄悄落到本机墙钟。
        headers = {"X-Service-Token": settings.bank_service_token} if settings.bank_service_token else {}
        _default_client = httpx.Client(
            base_url=base,
            timeout=httpx.Timeout(1.0, connect=0.5),
            trust_env=trust_env,
            headers=headers,
        )
    return _default_client


class SystemClock:
    """系统统一时钟。

    支持类方法与实例方法直接调用：
    - SystemClock.now() / SystemClock().now()
    - SystemClock.today() / SystemClock().today()
    - SystemClock.source() -> 'bank' | 'bank-stale' | 'local-fallback'
    - SystemClock.is_fallback() / SystemClock.is_stale()
    - SystemClock.invalidate() / SystemClock().invalidate()

    'bank' 表示最近一次读取银行 /clock 成功；'bank-stale' 表示本次读取失败、
    正在沿用最后一次成功的银行读数（业务日期不跳变）；'local-fallback' 表示
    从未成功读到过银行时钟，只能使用本机系统时间。
    """

    @classmethod
    def now(cls, client: httpx.Client | None = None) -> datetime:
        """获取当前系统时间（东八区 CST）。

        优先从 3.0s 缓存中获取；缓存失效时向银行接口请求。
        读取失败时若曾经成功读到过银行时钟，则沿用最后一次成功的读数
        （source() == 'bank-stale'，业务日期不跳变，只在进入 stale 的那一次告警）；
        只有从未成功读到过银行时钟才降级到本机时间（source() == 'local-fallback'）。
        """
        global _cached_datetime, _cached_at, _current_source, _last_bank_datetime

        now_monotonic = time.monotonic()
        if _cached_datetime is not None and (now_monotonic - _cached_at) < _CLOCK_CACHE_TTL_SECONDS:
            return _cached_datetime

        active_client = client or _get_client()
        failure_reason: str | None = None
        try:
            resp = active_client.get("/clock")
            if resp.status_code == 200:
                payload: dict[str, Any] = resp.json()
                data = payload.get("data") or {}
                iso_str = data.get("current_time")
                if iso_str:
                    dt = datetime.fromisoformat(iso_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=CST)
                    else:
                        dt = dt.astimezone(CST)
                    _cached_datetime = dt
                    _cached_at = time.monotonic()
                    _last_bank_datetime = dt
                    _current_source = "bank"
                    return dt
                failure_reason = "银行端 /clock 响应缺少 data.current_time 字段"
            else:
                failure_reason = f"银行端 /clock 返回非预期状态码 {resp.status_code}: {resp.text}"
        except Exception as exc:  # noqa: BLE001 - 时钟降级绝不允许影响主请求
            failure_reason = f"读取银行模拟时钟失败（{type(exc).__name__}: {exc}）"

        if _last_bank_datetime is not None:
            # 曾经读到过银行时钟：沿用最后已知真相，业务日期绝不跳到墙钟。
            stale_dt = _last_bank_datetime
            _cached_datetime = stale_dt
            # 刷新 TTL：死掉的银行按 3.0s 重试，而不是每次调用都去打它。
            _cached_at = time.monotonic()
            if _current_source != "bank-stale":
                # 只在"进入 stale"的那一次告警，银行长时间不可用时不刷屏。
                logger.warning(
                    "%s，沿用最后一次银行时钟读数 %s（bank-stale）",
                    failure_reason,
                    stale_dt.isoformat(),
                )
            _current_source = "bank-stale"
            return stale_dt

        logger.warning("%s，降级为本机时间", failure_reason)
        fallback = datetime.now(CST)
        _cached_datetime = fallback
        _cached_at = now_monotonic
        _current_source = "local-fallback"
        return fallback

    @classmethod
    def today(cls, client: httpx.Client | None = None) -> date:
        """获取当前系统日期（东八区 CST）。"""
        return cls.now(client).date()

    @classmethod
    def source(cls) -> str:
        """获取当前时钟时间源：'bank'、'bank-stale' 或 'local-fallback'。"""
        return _current_source

    @classmethod
    def is_fallback(cls) -> bool:
        """当前是否处于本机时间降级状态（仅指从未成功读到过银行时钟）。"""
        return _current_source == "local-fallback"

    @classmethod
    def is_stale(cls) -> bool:
        """当前是否在沿用最后一次成功的银行读数（银行读取失败但业务日期未跳变）。"""
        return _current_source == "bank-stale"

    @classmethod
    def invalidate(cls) -> None:
        """清空时钟缓存，强制下一次调用重新从银行或系统获取。

        只清缓存，不清 _last_bank_datetime：那是最后已知真相，必须活过一次
        显式重读，否则拨钟后的第一次失败仍会跳回墙钟。
        """
        global _cached_datetime, _cached_at
        _cached_datetime = None
        _cached_at = 0.0


invalidate = SystemClock.invalidate

__all__ = ["CST", "SystemClock", "_CLOCK_CACHE_TTL_SECONDS", "invalidate"]
