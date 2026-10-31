"""Bill date parsing: relative year and Chinese month/day names, without hardcoding a date."""

from datetime import date

import pytest

from app.modules.base import ToolFailure
from app.modules.dates import parse_tool_date

TODAY = date(2026, 10, 21)


def parse(value: str, field: str = "period_start"):
    return parse_tool_date(value, field, today=TODAY)


def test_existing_formats_are_unchanged():
    assert parse("2026-05-01") == date(2026, 5, 1)
    assert parse("2026年5月1日") == date(2026, 5, 1)
    assert parse("2026/5/1") == date(2026, 5, 1)
    assert parse("5月1日") == date(2026, 5, 1)
    assert parse("今天") == TODAY
    assert parse("昨天") == date(2026, 10, 20)


def test_chinese_month_and_day_names():
    assert parse("五月一日") == date(2026, 5, 1)
    assert parse("一月一日") == date(2026, 1, 1)
    assert parse("十二月三十一日") == date(2026, 12, 31)
    assert parse("十月二十日") == date(2026, 10, 20)


def test_this_year_prefix_is_resolved_by_code():
    assert parse("今年5月1日") == date(2026, 5, 1)
    assert parse("今年五月一日") == date(2026, 5, 1)
    assert parse("今年12月31日") == date(2026, 12, 31)


def test_month_only_uses_the_start_or_end_of_that_month():
    assert parse("今年一月", "period_start") == date(2026, 1, 1)
    assert parse("今年一月", "period_end") == date(2026, 1, 31)
    assert parse("今年二月", "period_end") == date(2026, 2, 28)
    assert parse("今年十二月", "period_start") == date(2026, 12, 1)
    assert parse("今年十二月", "period_end") == date(2026, 12, 31)
    assert parse("五月", "period_start") == date(2026, 5, 1)
    assert parse("五月", "period_end") == date(2026, 5, 31)


def test_a_bare_year_is_the_whole_year():
    """年报：只给年份就是整年区间 —— 开始 1 月 1 日，结束 12 月 31 日。

    以前「今年」单独出现会被判成缺日期；现在它本身就是完整区间，年报靠它解析。
    """

    assert parse("2025年", "period_start") == date(2025, 1, 1)
    assert parse("2025年", "period_end") == date(2025, 12, 31)
    assert parse("今年", "period_start") == date(2026, 1, 1)
    assert parse("今年", "period_end") == date(2026, 12, 31)
    assert parse("去年", "period_start") == date(2025, 1, 1)
    assert parse("前年", "period_end") == date(2024, 12, 31)


def test_a_bare_year_is_not_a_point_in_time():
    """只给年份时没有"哪一天"可言：不是 period_start/end 的字段必须拒绝。"""

    with pytest.raises(ToolFailure) as failure:
        parse("2025年", "some_field")
    assert failure.value.code == "VALIDATION"


def test_invalid_chinese_dates_are_rejected():
    with pytest.raises(ToolFailure):
        parse("今年十三月")
    with pytest.raises(ToolFailure):
        parse("今年二月三十日")


def test_parse_chinese_date_defaults_to_system_clock_today():
    from app.core.clock import SystemClock
    assert parse_tool_date("今天", "period_start") == SystemClock.today()
    assert parse_tool_date("今日", "period_start") == SystemClock.today()
