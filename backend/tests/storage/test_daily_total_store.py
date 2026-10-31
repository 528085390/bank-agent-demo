"""日累计 store 的行为契约：空表读 0、同一 (用户, 日期) 累加写、记不记由调用方决定。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import UniqueConstraint, select

from app.storage.daily_total import DailyTotalStore
from app.storage.models import DailyOperationTotal

DAY = date(2026, 10, 22)


def rows_for(rt, user_id: str) -> list[DailyOperationTotal]:
    with rt.database.sessions() as db:
        return list(
            db.scalars(
                select(DailyOperationTotal).where(DailyOperationTotal.user_id == user_id)
            ).all()
        )


def test_an_empty_table_reads_as_zero(rt):
    """没有当天记录时是 0：不是 None，也不抛异常。"""

    with rt.database.sessions() as db:
        assert DailyTotalStore.get_accumulated_minor(db, "user-1", DAY) == 0


def test_recording_twice_accumulates_into_one_row(rt):
    """upsert：第二次写同一 (用户, 日期) 累加到同一行，不新增行。"""

    with rt.database.sessions.begin() as db:
        first = DailyTotalStore.record_operation_amount(db, "user-1", DAY, 60_000)
    with rt.database.sessions.begin() as db:
        second = DailyTotalStore.record_operation_amount(db, "user-1", DAY, 50_000)

    assert (first, second) == (60_000, 110_000)

    rows = rows_for(rt, "user-1")
    assert len(rows) == 1
    assert (rows[0].total_minor, rows[0].business_date) == (110_000, DAY)

    with rt.database.sessions() as db:
        assert DailyTotalStore.get_accumulated_minor(db, "user-1", DAY) == 110_000


def test_another_user_or_day_is_a_separate_row(rt):
    """唯一键是 (用户, 日期)：换人、换天各占一行，互不相加。"""

    next_day = DAY.replace(day=DAY.day + 1)
    with rt.database.sessions.begin() as db:
        DailyTotalStore.record_operation_amount(db, "user-1", DAY, 60_000)
        DailyTotalStore.record_operation_amount(db, "user-2", DAY, 900_000)
        DailyTotalStore.record_operation_amount(db, "user-1", next_day, 5_000)
        assert DailyTotalStore.get_accumulated_minor(db, "user-1", DAY) == 60_000
        assert DailyTotalStore.get_accumulated_minor(db, "user-2", DAY) == 900_000
        assert DailyTotalStore.get_accumulated_minor(db, "user-1", next_day) == 5_000

    assert len(rows_for(rt, "user-1")) == 2


def test_the_table_carries_one_row_per_day_and_no_kind_column():
    """0017 之后表里没有类型列，唯一约束就是 (user_id, business_date)。"""

    table = DailyOperationTotal.__table__
    assert "operation_kind" not in table.columns
    unique = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert unique == {"uq_daily_op_user_date"}
