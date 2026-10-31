"""日累计金额的唯一读写入口。

`daily_operation_total` 一行记录一个 (用户, 业务日期) 的当日累计金额，单位分。本模块是这张
表唯一的访问点：风控判定读它（`app.policy.evaluator.classify_operation` 的
`daily_accumulated_minor` 参数），银行调用成功之后写它。只有两边用同一份口径，「当日累计」
才是一个可核对的事实，而不是各自心里的估计。

两条规则：

- **只统计资金变动类操作。** 这条由「谁来调写入」保证，而不是由某一列过滤：只有知道这笔写
  动了多少钱的调用方（`WriteFacade.send` 拿到正数 `amount_minor`）才记一笔；卡、理财这类
  不报金额的写根本不进这张表。表里没有类型列，读侧也从不按类型筛选。
- **写是累加，不是覆盖。** 同一 (用户, 日期) 第二次写撞唯一约束 `uq_daily_op_user_date`，
  在原值上相加。这样重试不会把金额吃掉，也不会留下两行各算一半。

两个函数都只收 `Session`：事务边界归调用方（写入方在银行 2xx 之后的那次事务里记账），本模块
不自己开事务，也不接受 `Database` —— `app/storage/database.py` 的 `Database` 只有
`.sessions` 这个 sessionmaker，把连接工厂传进来只会让事务边界变得含糊。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.storage.models import DailyOperationTotal


class DailyTotalStore:
    """日累计表的读写入口。无状态：每个调用方传自己的 `Session`。"""

    @staticmethod
    def get_accumulated_minor(session: Session, user_id: str, business_date: date) -> int:
        """该用户该日已累计的资金变动金额（分）；没有记录时是 0。

        读失败时异常向上抛，由调用方按 Fail-Closed 判红 —— 这里不把失败吞成 0。
        """

        total = session.scalar(
            select(func.coalesce(func.sum(DailyOperationTotal.total_minor), 0)).where(
                DailyOperationTotal.user_id == user_id,
                DailyOperationTotal.business_date == business_date,
            )
        )
        return int(total or 0)

    @staticmethod
    def record_operation_amount(
        session: Session,
        user_id: str,
        business_date: date,
        amount_minor: int,
    ) -> int:
        """把一笔金额累加到该用户该日的那一行上，返回累加后的新值（分）。"""

        amount = int(amount_minor)
        statement = (
            insert(DailyOperationTotal)
            .values(
                user_id=user_id,
                business_date=business_date,
                total_minor=amount,
            )
            .on_conflict_do_update(
                constraint="uq_daily_op_user_date",
                set_={
                    "total_minor": DailyOperationTotal.total_minor + amount,
                    "updated_at": func.now(),
                },
            )
            .returning(DailyOperationTotal.total_minor)
        )
        return int(session.execute(statement).scalar_one())
