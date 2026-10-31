"""时间辅助。

演示统一使用东八区（CST）。账单按自然月/自然年切分，日期过滤按当天 00:00 起算。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

CST = timezone(timedelta(hours=8))


def iso(value: datetime | None) -> str | None:
    """把数据库读出的时间统一按东八区渲染。

    TIMESTAMPTZ 在不同服务器/会话上会被 libpq 渲染成不同偏移（本机 UTC、远程 +08），
    直接 isoformat() 会让接口输出随环境漂移。这里显式换算到演示时区。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CST)
    return value.astimezone(CST).isoformat()


def day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=CST)


def day_end_exclusive(value: date) -> datetime:
    return day_start(value) + timedelta(days=1)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=CST)
    end = datetime(year + 1, 1, 1, tzinfo=CST) if month == 12 else datetime(
        year, month + 1, 1, tzinfo=CST
    )
    return start, end


def year_bounds(year: int) -> tuple[datetime, datetime]:
    return datetime(year, 1, 1, tzinfo=CST), datetime(year + 1, 1, 1, tzinfo=CST)


def add_months(value: date, months: int) -> date:
    """按自然月推进；月份不足时取该月最后一天。"""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def advance_due_date(value: date, frequency: str) -> date:
    if frequency == "daily":
        return value + timedelta(days=1)
    if frequency == "weekly":
        return value + timedelta(weeks=1)
    if frequency == "monthly":
        return add_months(value, 1)
    if frequency == "yearly":
        return add_months(value, 12)
    raise ValueError(f"未知的订阅频率: {frequency}")


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day
