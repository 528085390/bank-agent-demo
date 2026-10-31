from app.context.estimator import (
    ESTIMATOR_VERSION,
    MESSAGE_OVERHEAD,
    estimate_message_tokens,
    estimate_tokens,
)


def test_cjk_counts_one_token_per_character():
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("账户余额查询") == 6


def test_non_cjk_counts_three_tenths_per_character():
    assert estimate_tokens("abcd") == 2  # ceil(4 * 0.3) = ceil(1.2)
    assert estimate_tokens("abcdefghij") == 3  # ceil(10 * 0.3) = 3


def test_the_estimate_is_deliberately_pessimistic_for_ascii():
    """0.3 is above the empirical ~0.25, so a real tokenizer is never exceeded by much."""

    assert estimate_tokens("a" * 100) == 30


def test_mixed_text_adds_both_parts_then_rounds_up():
    assert estimate_tokens("你好ab") == 3  # ceil(2 + 0.6)


def test_fullwidth_punctuation_counts_as_cjk():
    assert estimate_tokens("，。！？") == 4


def test_empty_text_is_zero():
    assert estimate_tokens("") == 0


def test_every_message_carries_a_protocol_overhead():
    assert estimate_message_tokens("你好") == 2 + MESSAGE_OVERHEAD
    assert estimate_message_tokens("") == MESSAGE_OVERHEAD


def test_the_estimator_version_is_recorded_with_every_assembly():
    assert ESTIMATOR_VERSION == "heuristic_v1"
