"""账单摘要计算（C 交付物）。

读 Transaction（金额单位是分，列名 001 的 ``amount``），累加收入 / 支出 / 净额，对未分类
的交易现场分类并写回 ``category``，返回分（``credit_minor`` / ``debit_minor`` / ``net_minor``
—— 返回键沿用本模块对外契约，明示单位是分）。

分类结果写回是流程闭环的关键：A 的查询服务按 category 筛选与聚合，依赖这里把
``category`` 落回 Transaction 表。

**收与支都分类**：只给支出打标会让「按分类筛明细」对收入永远返回空，而用户并不
知道分类只覆盖支出。分类规则本身是方向无关的（「转账」「还款」对两个方向都成立），
所以每一行都打标，费用口径的聚合由消费方自己按 direction 过滤。
"""

from __future__ import annotations

from app.core.clock import SystemClock

from datetime import date

from sqlalchemy import select

from app.analysis.rules import categorize
from app.services.bill_import import latest_document_for
from app.storage.bill_models import BillTask, Transaction

#: 001 的 category_source CHECK 是大写（'RULE' / 'MANUAL' / 'DEFAULT'）。
CATEGORY_SOURCE_RULE = "RULE"


def compute_summary(task_id: str, database) -> dict:
    """计算账单摘要：读分、累加、分类写回、返回分。"""

    with database.sessions.begin() as db:
        task = db.scalar(select(BillTask).where(BillTask.task_no == task_id))
        if task is None:
            return _empty_summary(SystemClock.today(), SystemClock.today())
        # 本任务没导入过（同一文件重复上传）就回退到那份已入库的文档——写是幂等的，
        # 读不该跟着变成 0 条。
        doc = latest_document_for(db, task)
        if doc is None:
            return _empty_summary(SystemClock.today(), SystemClock.today())
        transactions = db.scalars(
            select(Transaction).where(Transaction.statement_document_id == doc.id)
        ).all()
        if not transactions:
            return _empty_summary(doc.period_start, doc.period_end)

        credit_minor = 0
        debit_minor = 0
        category_totals: dict[str, int] = {}
        for tx in transactions:
            # Every row is categorized, whichever way the money moved; only the totals below are
            # direction-specific.
            # 001 的 category 是 NOT NULL DEFAULT '未分类'，「未分类」用字符串而非 NULL 表达；
            # 是否已经分过类由 category_source 是否为空判断（它正是记录分类来源的列）。
            if tx.category_source is None:
                tx.category = categorize(tx.transaction_type, tx.counterparty, tx.product)
                tx.category_source = CATEGORY_SOURCE_RULE
            if tx.direction == "credit":
                credit_minor += tx.amount
                continue
            # The category breakdown stays a spending breakdown: income is not a "category spent".
            debit_minor += tx.amount
            category = tx.category or "未分类"
            category_totals[category] = category_totals.get(category, 0) + tx.amount

        categories = [
            {"category": name, "amount_minor": amount}
            for name, amount in sorted(category_totals.items(), key=lambda item: -item[1])
        ]
        return {
            "period_start": min(tx.booked_date for tx in transactions),
            "period_end": max(tx.booked_date for tx in transactions),
            "currency": transactions[0].currency or "CNY",
            "credit_minor": credit_minor,
            "debit_minor": debit_minor,
            "net_minor": credit_minor - debit_minor,
            "record_count": len(transactions),
            "categories": categories,
            "quality_issues": [],
        }


def _empty_summary(period_start, period_end) -> dict:
    return {
        "period_start": period_start,
        "period_end": period_end,
        "currency": "CNY",
        "credit_minor": 0,
        "debit_minor": 0,
        "net_minor": 0,
        "record_count": 0,
        "categories": [],
        "quality_issues": ["未找到交易记录"],
    }
