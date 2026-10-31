"""账单与异常分析的领域测试。

覆盖计划要求：固定种子命中、空时间范围和关键词/金额过滤。
"""

from datetime import date

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.domain.bill_service import (
    RULE_FREQUENT_SMALL,
    RULE_LARGE_AMOUNT,
    RULE_REPEATED,
    BillService,
)

SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture
def bills(seeded_database):
    return BillService(seeded_database)


def _findings(result) -> dict[str, dict]:
    return {item["rule_id"]: item for item in result["findings"]}


# ---------------------------------------------------------------- 账单


def test_monthly_bill_totals_match_the_seeded_history(bills):
    bill = bills.monthly("u_zhang", "2026-09")
    assert bill["period"] == "2026-09"
    # 种子：大额消费 1200000 + 餐饮 5×2000 + 转账 5000 + 订阅 5900；理财收益 5000 入账。
    assert bill["income_minor"] == 5000
    assert bill["expense_minor"] == 1220900
    # 消费口径为 consumption/subscription/aa，不含转账。
    assert bill["consumption_minor"] == 1215900
    assert bill["categories"] == {
        "shopping": 1200000,
        "dining": 10000,
        "subscription": 5900,
        "transfer": 5000,
    }


def test_monthly_bill_rejects_a_malformed_month(bills):
    for month in ("2026-13", "202609", "2026-9", ""):
        with pytest.raises(ValidationError):
            bills.monthly("u_zhang", month)


def test_yearly_bill_aggregates_the_same_seed(bills):
    bill = bills.yearly("u_zhang", 2026)
    assert bill["period"] == "2026"
    assert bill["expense_minor"] == 1220900
    assert bills.yearly("u_zhang", 2025)["expense_minor"] == 0


def test_yearly_bill_rejects_an_out_of_range_year(bills):
    with pytest.raises(ValidationError):
        bills.yearly("u_zhang", 1999)


def test_categories_route_respects_the_date_range(bills):
    bill = bills.categories("u_zhang", *SEPTEMBER)
    assert bill["categories"]["dining"] == 10000
    empty = bills.categories("u_zhang", date(2020, 1, 1), date(2020, 1, 31))
    assert empty["expense_minor"] == 0
    assert empty["categories"] == {}


def test_bill_for_an_unknown_user_raises_not_found(bills):
    with pytest.raises(NotFoundError):
        bills.monthly("u_nobody", "2026-09")


# ---------------------------------------------------------------- 异常分析


def test_analysis_hits_all_three_rules_on_the_fixed_seed(bills):
    result = bills.analyze(user_id="u_zhang", start_date=SEPTEMBER[0], end_date=SEPTEMBER[1])
    findings = _findings(result)

    assert set(findings) == {RULE_LARGE_AMOUNT, RULE_FREQUENT_SMALL, RULE_REPEATED}

    large = findings[RULE_LARGE_AMOUNT]
    assert large["amount_minor"] == 1200000
    assert len(large["transaction_ids"]) == 1

    frequent = findings[RULE_FREQUENT_SMALL]
    assert len(frequent["transaction_ids"]) == 5
    assert frequent["amount_minor"] == 10000

    repeated = findings[RULE_REPEATED]
    assert len(repeated["transaction_ids"]) == 5
    assert "dining" in repeated["explanation"]


def test_analysis_returns_the_rule_catalogue(bills):
    result = bills.analyze(user_id="u_zhang", start_date=SEPTEMBER[0], end_date=SEPTEMBER[1])
    assert set(result["rules"]) == {RULE_LARGE_AMOUNT, RULE_FREQUENT_SMALL, RULE_REPEATED}
    assert result["analyzed_count"] == 9


def test_empty_time_range_returns_no_findings(bills):
    result = bills.analyze(
        user_id="u_zhang", start_date=date(2020, 1, 1), end_date=date(2020, 1, 31)
    )
    assert result["findings"] == []
    assert result["analyzed_count"] == 0


def test_category_filter_narrows_to_the_dining_transactions(bills):
    result = bills.analyze(
        user_id="u_zhang",
        start_date=SEPTEMBER[0],
        end_date=SEPTEMBER[1],
        category="dining",
    )
    findings = _findings(result)
    assert set(findings) == {RULE_FREQUENT_SMALL, RULE_REPEATED}
    assert result["analyzed_count"] == 5


def test_min_amount_filter_leaves_only_the_large_consumption(bills):
    result = bills.analyze(
        user_id="u_zhang",
        start_date=SEPTEMBER[0],
        end_date=SEPTEMBER[1],
        min_amount_minor=1000000,
    )
    findings = _findings(result)
    assert set(findings) == {RULE_LARGE_AMOUNT}
    assert result["analyzed_count"] == 1


def test_keyword_filter_matches_remarks(bills):
    matched = bills.analyze(
        user_id="u_zhang",
        start_date=SEPTEMBER[0],
        end_date=SEPTEMBER[1],
        keywords=["午餐"],
    )
    assert matched["analyzed_count"] == 5
    assert set(_findings(matched)) == {RULE_FREQUENT_SMALL, RULE_REPEATED}

    unmatched = bills.analyze(
        user_id="u_zhang",
        start_date=SEPTEMBER[0],
        end_date=SEPTEMBER[1],
        keywords=["完全不存在的关键词"],
    )
    assert unmatched["findings"] == []
    assert unmatched["analyzed_count"] == 0


def test_analysis_rejects_an_inverted_range(bills):
    with pytest.raises(ValidationError):
        bills.analyze(
            user_id="u_zhang", start_date=date(2026, 9, 30), end_date=date(2026, 9, 1)
        )


def test_analysis_can_be_scoped_to_a_single_day(bills):
    result = bills.analyze(
        user_id="u_zhang", start_date=date(2026, 9, 19), end_date=date(2026, 9, 19)
    )
    assert result["analyzed_count"] == 5
    # 只剩 5 笔餐饮，大额规则不再命中。
    assert set(_findings(result)) == {RULE_FREQUENT_SMALL, RULE_REPEATED}
