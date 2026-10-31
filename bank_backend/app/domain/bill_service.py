"""账单与异常交易分析。

月/年账单按自然月、自然年切分；自定义分析支持日期范围、分类、关键词和金额过滤。

异常规则（每条命中都返回 rule_id、transaction_ids、涉及金额和说明）：
- large_amount：单笔支出达到大额阈值。
- frequent_small：短窗口内出现多笔小额支出。
- repeated_transaction：短窗口内出现多笔金额与分类完全相同的支出。

规则阈值来自 Settings，窗口长度是本模块的固定演示参数。
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.core.timeutil import month_bounds, year_bounds
from app.domain.account_service import require_user
from app.storage.database import Database
from app.storage.models import Transaction

# 计入"消费"的支出类型；转账与理财申赎不计入消费，但仍计入支出。
CONSUMPTION_TYPES = ("consumption", "subscription", "aa")

RULE_LARGE_AMOUNT = "large_amount"
RULE_FREQUENT_SMALL = "frequent_small"
RULE_REPEATED = "repeated_transaction"

# 聚合窗口：相邻两笔间隔不超过该值即视为同一簇。
FREQUENT_WINDOW = timedelta(minutes=10)
REPEATED_WINDOW = timedelta(minutes=30)
MIN_CLUSTER_SIZE = 3

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _bill_view(period: str, entries: list[Transaction]) -> dict:
    income = 0
    expense = 0
    consumption = 0
    categories: dict[str, int] = defaultdict(int)

    for entry in entries:
        if entry.status != "completed":
            continue
        if entry.direction == "credit":
            income += entry.amount_minor
            continue
        expense += entry.amount_minor
        if entry.transaction_type in CONSUMPTION_TYPES:
            consumption += entry.amount_minor
        categories[entry.category or "uncategorized"] += entry.amount_minor

    return {
        "period": period,
        "income_minor": income,
        "expense_minor": expense,
        "consumption_minor": consumption,
        "categories": dict(sorted(categories.items(), key=lambda item: (-item[1], item[0]))),
    }


def _clusters(entries: list[Transaction], window: timedelta) -> list[list[Transaction]]:
    """把时间上相邻（间隔不超过 window）的流水聚成一簇。"""
    ordered = sorted(entries, key=lambda entry: (entry.created_at, entry.id))
    clusters: list[list[Transaction]] = []
    current: list[Transaction] = []
    for entry in ordered:
        if current and entry.created_at - current[-1].created_at > window:
            clusters.append(current)
            current = []
        current.append(entry)
    if current:
        clusters.append(current)
    return clusters


def _finding(rule_id: str, entries: list[Transaction], explanation: str) -> dict:
    return {
        "rule_id": rule_id,
        "transaction_ids": [entry.id for entry in entries],
        "amount_minor": sum(entry.amount_minor for entry in entries),
        "explanation": explanation,
    }


def find_large_amounts(entries: list[Transaction], threshold_minor: int) -> list[dict]:
    return [
        _finding(
            RULE_LARGE_AMOUNT,
            [entry],
            f"单笔支出 {entry.amount_minor} 分，达到大额阈值 {threshold_minor} 分",
        )
        for entry in sorted(entries, key=lambda item: item.created_at)
        if entry.amount_minor >= threshold_minor
    ]


def find_frequent_small(
    entries: list[Transaction], threshold_minor: int, window: timedelta = FREQUENT_WINDOW
) -> list[dict]:
    small = [entry for entry in entries if entry.amount_minor < threshold_minor]
    findings = []
    for cluster in _clusters(small, window):
        if len(cluster) >= MIN_CLUSTER_SIZE:
            findings.append(
                _finding(
                    RULE_FREQUENT_SMALL,
                    cluster,
                    f"{_span(cluster)}内出现 {len(cluster)} 笔低于 {threshold_minor} 分的小额支出",
                )
            )
    return findings


def find_repeated(entries: list[Transaction], window: timedelta = REPEATED_WINDOW) -> list[dict]:
    findings = []
    for cluster in _clusters(entries, window):
        buckets: dict[tuple[int, str | None], list[Transaction]] = defaultdict(list)
        for entry in cluster:
            buckets[(entry.amount_minor, entry.category)].append(entry)
        for (amount, category), group in buckets.items():
            if len(group) >= MIN_CLUSTER_SIZE:
                findings.append(
                    _finding(
                        RULE_REPEATED,
                        sorted(group, key=lambda item: item.created_at),
                        f"{_span(group)}内出现 {len(group)} 笔金额 {amount} 分、"
                        f"分类 {category or 'uncategorized'} 完全相同的支出",
                    )
                )
    return findings


def _span(entries: list[Transaction]) -> str:
    stamps = [entry.created_at for entry in entries]
    seconds = int((max(stamps) - min(stamps)).total_seconds())
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分钟"


class BillService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or get_settings()

    def monthly(self, user_id: str, month: str) -> dict:
        if not MONTH_PATTERN.match(month or ""):
            raise ValidationError("month 必须形如 YYYY-MM")
        year, month_number = int(month[:4]), int(month[5:])
        start, end = month_bounds(year, month_number)
        return self._period(user_id, month, start, end)

    def yearly(self, user_id: str, year: int) -> dict:
        if not 2000 <= year <= 2100:
            raise ValidationError("year 必须在 2000..2100 之间")
        start, end = year_bounds(year)
        return self._period(user_id, str(year), start, end)

    def categories(
        self, user_id: str, start_date: date | None = None, end_date: date | None = None
    ) -> dict:
        period = _period_label(start_date, end_date)
        return self._period(user_id, period, *_as_bounds(start_date, end_date))

    def analyze(
        self,
        *,
        user_id: str,
        start_date: date,
        end_date: date,
        category: str | None = None,
        min_amount_minor: int | None = None,
        keywords: list[str] | None = None,
    ) -> dict:
        if end_date < start_date:
            raise ValidationError("end_date 不能早于 start_date")

        with self.database.sessions() as session:
            require_user(session, user_id)
            entries = self._load(session, user_id, *_as_bounds(start_date, end_date))
            filtered = _apply_filters(entries, category, min_amount_minor, keywords)
            debits = [entry for entry in filtered if entry.direction == "debit"]

            findings = [
                *find_large_amounts(debits, self.settings.large_amount_threshold_minor),
                *find_frequent_small(debits, self.settings.frequent_small_amount_minor),
                *find_repeated(debits),
            ]
            findings.sort(key=lambda item: (item["rule_id"], item["transaction_ids"]))

            return {
                "period": _period_label(start_date, end_date),
                "analyzed_count": len(filtered),
                "findings": findings,
                "rules": {
                    RULE_LARGE_AMOUNT: (
                        f"单笔支出 >= {self.settings.large_amount_threshold_minor} 分"
                    ),
                    RULE_FREQUENT_SMALL: (
                        f"{int(FREQUENT_WINDOW.total_seconds() // 60)} 分钟内出现 "
                        f">= {MIN_CLUSTER_SIZE} 笔低于 "
                        f"{self.settings.frequent_small_amount_minor} 分的支出"
                    ),
                    RULE_REPEATED: (
                        f"{int(REPEATED_WINDOW.total_seconds() // 60)} 分钟内出现 "
                        f">= {MIN_CLUSTER_SIZE} 笔金额与分类相同的支出"
                    ),
                },
            }

    def _period(self, user_id: str, period: str, start: datetime, end: datetime) -> dict:
        with self.database.sessions() as session:
            require_user(session, user_id)
            return _bill_view(period, self._load(session, user_id, start, end))

    @staticmethod
    def _load(
        session: Session, user_id: str, lower: datetime, upper: datetime
    ) -> list[Transaction]:
        return list(
            session.execute(
                select(Transaction)
                .where(
                    Transaction.user_id == user_id,
                    Transaction.created_at >= lower,
                    Transaction.created_at < upper,
                )
                .order_by(Transaction.created_at, Transaction.id)
            ).scalars()
        )


def _apply_filters(
    entries: list[Transaction],
    category: str | None,
    min_amount_minor: int | None,
    keywords: list[str] | None,
) -> list[Transaction]:
    result = entries
    if category:
        result = [entry for entry in result if entry.category == category]
    if min_amount_minor is not None:
        result = [entry for entry in result if entry.amount_minor >= min_amount_minor]
    if keywords:
        lowered = [keyword.lower() for keyword in keywords if keyword]
        if lowered:
            result = [
                entry
                for entry in result
                if entry.remark and any(word in entry.remark.lower() for word in lowered)
            ]
    return result


def _as_bounds(start_date: date | None, end_date: date | None) -> tuple[datetime, datetime]:
    from app.core.timeutil import day_end_exclusive, day_start

    if start_date is None or end_date is None:
        raise ValidationError("start_date 与 end_date 必须同时提供")
    if end_date < start_date:
        raise ValidationError("end_date 不能早于 start_date")
    return day_start(start_date), day_end_exclusive(end_date)


def _period_label(start_date: date | None, end_date: date | None) -> str:
    if start_date and end_date:
        return f"{start_date.isoformat()}..{end_date.isoformat()}"
    return "all"
