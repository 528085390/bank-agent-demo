"""账单查询服务：供 bill agent 工具处理器查询已有入库数据。

修正方案把职责分离：上传/解析/入库走后台服务（不经 agent），bill agent 只负责
查询和分析。这里提供按时间段查询摘要、交易明细与消费分类统计的只读接口，
供 ``app.modules.bill.handlers`` 调用。

**查询的语义是「某个用户在某段时间里的账单」，不是「某一份文档的内容」。** 所以三个
查询都在该用户的**全部**已入库交易上按 ``booked_date`` 过滤：

- 查 9 月 → 只有 9 月的数
- 查 1 月 → 空（不会拿 7–10 月那份糊弄）
- 导入过多份账单 → 各查各的

曾经按「最近一份文档」查，结果是：传过一份账单之后，查任何日期都只看到那一份——
因为 `_latest_summary` 找的是「与区间**有交集**的摘要」并把整份返回，而文档又只取
最新那一份。两个过滤都不在你给的那个区间上。

ORM 列名跟随 001 契约（``amount`` / ``credit_total`` / ``generated_at``）；返回的 dict 键
仍用 ``*_minor``（对外契约，明示单位是分），映射在这一层做一次。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, select

from app.core.bill_errors import VALIDATION, raise_bill_error
from app.storage.bill_models import BillTask, Transaction


def _of_user(user_id: str) -> Select:
    """该用户的交易查询。跨他导入过的所有账单——按账户，不按文档。"""

    return select(Transaction).join(BillTask, BillTask.id == Transaction.task_id).where(
        BillTask.user_id == user_id
    )


def _spending_breakdown(db, user_id: str, start: date, end: date) -> list[dict]:
    """支出按分类聚合（消费口径，只算 debit）。

    这是 `get_summary` 的内部辅助，**不是**被深度分析取代的那套 `analyze_spending`
    —— 摘要里的分类聚合仍然要它。前端 `BillResult.vue` 的画法就是拿它的结果画条形图。
    """

    rows = db.execute(
        _of_user(user_id)
        .with_only_columns(
            Transaction.category,
            func.sum(Transaction.amount),
            func.count(Transaction.id),
        )
        .where(
            Transaction.booked_date >= start,
            Transaction.booked_date <= end,
            Transaction.direction == "debit",
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    ).all()
    return [
        {
            "category": row[0] or "未分类",
            "amount_minor": int(row[1] or 0),
            "count": int(row[2] or 0),
        }
        for row in rows
    ]


class BillQueryService:
    def __init__(self, database):
        self.database = database

    def get_summary(self, user_id: str, period_start: date, period_end: date) -> dict:
        start, end = _validate_period(period_start, period_end)
        with self.database.sessions() as db:
            totals = db.execute(
                _of_user(user_id)
                .with_only_columns(
                    Transaction.direction,
                    func.sum(Transaction.amount),
                    func.count(Transaction.id),
                )
                .where(
                    Transaction.booked_date >= start,
                    Transaction.booked_date <= end,
                )
                .group_by(Transaction.direction)
            ).all()
            # 修改：空数据返回零值而非抛异常，避免查询不稳定且符合统计查询语义
            if not totals:
                return {
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "currency": "CNY",
                    "credit_minor": 0,
                    "debit_minor": 0,
                    "net_minor": 0,
                    "record_count": 0,
                    "categories": [],
                    "quality_issues": [],
                }

            credit = sum(int(row[1] or 0) for row in totals if row[0] == "credit")
            debit = sum(int(row[1] or 0) for row in totals if row[0] == "debit")
            count = sum(int(row[2] or 0) for row in totals)
            currency = db.scalar(
                _of_user(user_id)
                .with_only_columns(Transaction.currency)
                .where(
                    Transaction.booked_date >= start,
                    Transaction.booked_date <= end,
                )
                .limit(1)
            )
            return {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "currency": currency or "CNY",
                "credit_minor": credit,
                "debit_minor": debit,
                "net_minor": credit - debit,
                "record_count": count,
                # 费用口径的分类聚合。深度分析有它自己的一套模块，但「查账单摘要」这条
                # 路径仍然要靠它 —— 前端拿它画分类条形图，留空的话图永远是空的。
                "categories": [
                    {"category": item["category"], "amount_minor": item["amount_minor"]}
                    for item in _spending_breakdown(db, user_id, start, end)
                ],
                # 导入期的解析提示（"前 15 行是说明"之类）挂在文档上，不在交易上；
                # 按区间聚合的交易查不到它们，如实留空而不是编一个。
                "quality_issues": [],
            }

    def get_transactions(
        self,
        user_id: str,
        period_start: date | None = None,
        period_end: date | None = None,
        category: str | None = None,
        direction: str | None = None,
        page: int = 1,
        page_size: int = 20,
        *,
        document_id=None,
    ) -> dict:
        """分页查询某时间段内的交易明细，可选按分类 / 方向筛选。

        给了 ``document_id`` 就只查这一份账单文档（「查看刚导入的流水」那条路），
        日期不再参与过滤；否则按起止日期跨文档查询（既有行为）。
        """

        page = max(1, page)
        page_size = min(max(1, page_size), 200)

        with self.database.sessions() as db:
            if document_id is not None:
                filters = [Transaction.statement_document_id == document_id]
            else:
                start, end = _validate_period(period_start, period_end)
                filters = [
                    Transaction.booked_date >= start,
                    Transaction.booked_date <= end,
                ]
            if category:
                filters.append(Transaction.category == category)
            if direction in {"debit", "credit"}:
                filters.append(Transaction.direction == direction)

            scoped = _of_user(user_id).where(*filters)
            total = db.scalar(
                _of_user(user_id)
                .with_only_columns(func.count(Transaction.id))
                .where(*filters)
            )
            rows = db.scalars(
                scoped.order_by(
                    Transaction.booked_date.desc(), Transaction.source_row_index
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "total": total or 0,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "booked_date": tx.booked_date.isoformat(),
                        "amount_minor": tx.amount,
                        "direction": tx.direction,
                        "transaction_type": tx.transaction_type,
                        "counterparty": tx.counterparty,
                        "category": tx.category,
                    }
                    for tx in rows
                ],
            }

    # analyze_spending 已废弃，被 analyze_bill_comprehensive 取代
    # def analyze_spending(
    #     self,
    #     user_id: str,
    #     period_start: date,
    #     period_end: date,
    #     analysis_type: str = "category",
    # ) -> dict:
    #     """消费分析：按分类聚合支出金额、笔数与占比。"""
    #
    #     start, end = _validate_period(period_start, period_end)
    #     empty = {
    #         "period_start": start.isoformat(),
    #         "period_end": end.isoformat(),
    #         "currency": "CNY",
    #         "total_spending_minor": 0,
    #         "categories": [],
    #         "analysis_type": analysis_type,
    #     }
    #     with self.database.sessions() as db:
    #         categories = _spending_breakdown(db, user_id, start, end)
    #         if not categories:
    #             return empty
    #
    #         total = sum(item["amount_minor"] for item in categories)
    #         currency = db.scalar(
    #             _of_user(user_id)
    #             .with_only_columns(Transaction.currency)
    #             .where(
    #                 Transaction.booked_date >= start,
    #                 Transaction.booked_date <= end,
    #             )
    #             .limit(1)
    #         )
    #         for item in categories:
    #             item["share"] = (
    #                 format(item["amount_minor"] / total * 100, ".1f") if total else "0.0"
    #             )
    #         return {
    #             "period_start": start.isoformat(),
    #             "period_end": end.isoformat(),
    #             "currency": currency or "CNY",
    #             "total_spending_minor": int(total),
    #             "categories": categories,
    #             "analysis_type": analysis_type,
    #         }


def _validate_period(start: date, end: date) -> tuple[date, date]:
    if end < start:
        raise_bill_error(VALIDATION, "结束日期不能早于开始日期")
    return start, end
