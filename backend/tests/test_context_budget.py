import pytest
from pydantic import ValidationError

from app.context.budget import (
    MAX_MODEL_CONTEXT_LIMIT,
    MAX_RECENT_MESSAGE_BUDGET,
    MAX_SUMMARY_MAX_TOKENS,
    Budget,
    chunk_for_summary,
    iter_rounds,
    leading_strip,
)
from app.context.ledger import LedgerMessage
from app.core.config import Settings

DEFAULTS = dict(
    model_context_limit=128000,
    reserved_output_tokens=4096,
    safety_margin=2048,
    summary_trigger_ratio=0.7,
    recent_message_budget=32768,
    summary_max_tokens=1024,
)


def budget(**overrides):
    return Budget(**{**DEFAULTS, **overrides})


def message(sequence, role, content, kind="message", question_id=None):
    return LedgerMessage(sequence, role, content, kind, question_id)


def round_of(index, user="问题", assistant="回答"):
    return [message(index, "user", user), message(index + 1, "assistant", assistant)]


def test_the_derived_budgets_match_the_spec():
    assert budget().input_budget == 121856
    assert budget().summary_trigger == 85299
    assert budget().chunk_budget == 32768


def test_a_retained_budget_at_or_above_the_trigger_is_rejected():
    with pytest.raises(ValueError, match="recent_message_budget"):
        budget(recent_message_budget=90000).validate()


def test_a_summary_that_cannot_fit_the_input_budget_is_rejected():
    with pytest.raises(ValueError, match="summary_max_tokens"):
        budget(summary_max_tokens=200000).validate()


def test_the_defaults_validate():
    budget().validate()


def test_settings_reject_a_self_contradicting_budget_at_construction():
    """Startup fails, not the first long conversation."""

    with pytest.raises(ValidationError):
        Settings(recent_message_budget=90000)


def test_settings_reject_a_trigger_ratio_outside_the_open_unit_interval():
    with pytest.raises(ValidationError):
        Settings(summary_trigger_ratio=1.0)


def test_settings_carry_the_spec_defaults():
    assert Budget.from_settings(Settings()) == budget()


def test_rounds_are_a_user_message_plus_the_assistant_message_that_closes_it():
    rounds = iter_rounds([*round_of(1), *round_of(3)])

    assert [(r.messages[0].sequence, r.messages[-1].sequence, r.closed) for r in rounds] == [
        (1, 2, True),
        (3, 4, True),
    ]


def test_a_trailing_user_message_is_an_unclosed_round():
    """The current message has no reply yet, so it can never be summarized away."""

    rounds = iter_rounds([*round_of(1), message(3, "user", "刚说的")])

    assert [(r.messages[0].sequence, r.closed) for r in rounds] == [(1, True), (3, False)]


def test_nothing_is_stripped_when_the_tail_already_fits():
    assert leading_strip(round_of(1), budget_target=100000) == []


def test_the_strip_is_the_minimal_number_of_whole_rounds():
    messages = [*round_of(1, user="甲" * 200), *round_of(3, user="乙" * 200)]

    stripped = leading_strip(messages, budget_target=200)

    assert [row.sequence for row in stripped] == [1, 2], "only the first round is stripped"


def test_the_last_round_is_never_stripped_even_when_it_alone_exceeds_the_target():
    """Keeping the current round is the hard constraint; the budget target is soft."""

    messages = [*round_of(1), *round_of(3, user="乙" * 500)]

    assert [row.sequence for row in leading_strip(messages, budget_target=10)] == [1, 2]


def test_an_unclosed_round_is_never_stripped():
    messages = [*round_of(1), message(3, "user", "甲" * 500)]

    assert [row.sequence for row in leading_strip(messages, budget_target=1)] == [1, 2]


def test_chunks_are_consecutive_whole_rounds_within_the_chunk_budget():
    messages = [*round_of(1, user="甲" * 200), *round_of(3, user="乙" * 200)]

    chunks = chunk_for_summary(messages, chunk_budget=100)

    assert [[row.sequence for row in chunk] for chunk in chunks] == [[1, 2], [3, 4]]


def test_a_round_larger_than_the_chunk_budget_becomes_its_own_chunk():
    """Splitting inside a round would put covered_to on a user message, which is forbidden."""

    messages = [*round_of(1, user="甲" * 900), *round_of(3)]

    chunks = chunk_for_summary(messages, chunk_budget=100)

    assert [[row.sequence for row in chunk] for chunk in chunks] == [[1, 2], [3, 4]]


def test_chunking_an_empty_range_yields_no_chunks():
    assert chunk_for_summary([], chunk_budget=100) == []


def test_a_context_limit_beyond_the_absolute_ceiling_is_rejected():
    with pytest.raises(ValueError, match="model_context_limit"):
        budget(model_context_limit=MAX_MODEL_CONTEXT_LIMIT + 1).validate()


def test_a_recent_message_budget_beyond_the_absolute_ceiling_is_rejected():
    # `model_context_limit` is raised as well on purpose. With the default 128000 the derived
    # `summary_trigger` is 85299, so the *pre-existing* relational check
    # (`recent_message_budget >= summary_trigger`) fires first -- and its message already
    # contains "recent_message_budget", so `match=` would pass even with the new ceiling
    # deleted. Raising the limit to the ceiling makes `summary_trigger` 1395699, the relational
    # check stays quiet, and only the new ceiling can make this test fail.
    with pytest.raises(ValueError, match="recent_message_budget"):
        budget(
            model_context_limit=MAX_MODEL_CONTEXT_LIMIT,
            recent_message_budget=MAX_RECENT_MESSAGE_BUDGET + 1,
        ).validate()


def test_a_summary_budget_beyond_the_absolute_ceiling_is_rejected():
    with pytest.raises(ValueError, match="summary_max_tokens"):
        budget(summary_max_tokens=MAX_SUMMARY_MAX_TOKENS + 1).validate()


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_context_limit": MAX_MODEL_CONTEXT_LIMIT},
        # Same reason as above: at the default limit the relational check would reject this.
        {
            "model_context_limit": MAX_MODEL_CONTEXT_LIMIT,
            "recent_message_budget": MAX_RECENT_MESSAGE_BUDGET,
        },
        {"summary_max_tokens": MAX_SUMMARY_MAX_TOKENS},
    ],
)
def test_the_ceiling_itself_is_still_allowed(overrides):
    """A guardrail, not a freeze: the value at the ceiling must still validate."""

    budget(**overrides).validate()


def test_settings_reject_a_context_limit_beyond_the_ceiling_at_construction():
    with pytest.raises(ValidationError, match="absolute ceiling"):
        Settings(model_context_limit=MAX_MODEL_CONTEXT_LIMIT + 1)


@pytest.mark.parametrize(
    ("ceiling", "field"),
    [
        (MAX_MODEL_CONTEXT_LIMIT, "model_context_limit"),
        (MAX_RECENT_MESSAGE_BUDGET, "recent_message_budget"),
        (MAX_SUMMARY_MAX_TOKENS, "summary_max_tokens"),
    ],
)
def test_the_ceiling_is_far_above_the_default_so_it_is_a_guardrail_not_a_freeze(ceiling, field):
    """If the ceiling ever drops near the default, tuning the default would break."""

    assert ceiling >= 10 * getattr(Settings(), field)
