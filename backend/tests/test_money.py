"""Money is formatted here and nowhere else, with integer arithmetic only."""

import pytest

from app.modules.money import format_minor


def test_yuan_and_fen_are_split_without_floating_point():
    assert format_minor(832_000) == "8,320.00 元"


def test_a_small_amount_keeps_its_leading_zero():
    assert format_minor(5) == "0.05 元"


def test_zero_is_formatted_not_treated_as_missing():
    assert format_minor(0) == "0.00 元"


def test_a_negative_amount_keeps_its_sign_outside_the_grouping():
    assert format_minor(-123_456) == "-1,234.56 元"


def test_an_unknown_currency_falls_back_to_its_code():
    assert format_minor(100, "USD") == "1.00 USD"


@pytest.mark.parametrize("bad", [8.32, True, "8320", None])
def test_a_non_integer_amount_is_a_programming_error(bad):
    with pytest.raises(TypeError):
        format_minor(bad)


def test_a_negative_amount_below_one_yuan_keeps_both_sign_and_leading_zero():
    assert format_minor(-5) == "-0.05 元"


def test_a_large_amount_survives_the_float_precision_a_division_would_lose():
    # 90,071,992,547,409.93 元. Dividing by 100 as a float lands on ...409.94.
    assert format_minor(9_007_199_254_740_993) == "90,071,992,547,409.93 元"
