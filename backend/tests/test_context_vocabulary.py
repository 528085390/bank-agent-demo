import pytest

from app.audit.context import (
    AGENT_ID_SUPERVISOR,
    SOURCE_CONTEXT_SUMMARY,
    SOURCE_CONVERSATION,
    AuditContext,
    audit_context,
    current_context,
    summary_context,
)
from app.audit.service import RunEventType
from app.core.errors import (
    CONTEXT_LIMIT_REASON_OVER_BUDGET,
    CONTEXT_LIMIT_REASON_SUMMARY_FAILED,
    ConversationErrorCode,
    context_limit_error,
    conversation_error,
)
from app.integration.model_client import classify_provider_error


def test_the_code_is_part_of_the_frozen_vocabulary():
    assert ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED == "CONTEXT_LIMIT_EXCEEDED"


def test_a_transient_summary_failure_is_retryable():
    error = context_limit_error(CONTEXT_LIMIT_REASON_SUMMARY_FAILED)

    assert (error.code, error.status, error.retryable) == ("CONTEXT_LIMIT_EXCEEDED", 409, True)
    assert error.details == {"reason": "summary_failed"}


def test_a_deterministic_over_budget_failure_is_not_retryable():
    """Retrying the same ledger with the same message yields the same error."""

    error = context_limit_error(CONTEXT_LIMIT_REASON_OVER_BUDGET)

    assert (error.status, error.retryable) == (409, False)
    assert error.details == {"reason": "over_budget"}


def test_the_fixed_table_refuses_this_code_so_the_reason_cannot_be_dropped():
    """`conversation_error` hard-codes retryable, which is wrong for this one code."""

    with pytest.raises(ValueError, match="context_limit_error"):
        conversation_error(ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED)


def test_supplying_a_reason_does_not_let_the_code_bypass_the_refusal():
    """A caller who brings the reason the fixed table lacks still gains nothing: the refusal is
    on the code, so this raises instead of building an error from the tables."""

    with pytest.raises(ValueError, match="context_limit_error"):
        conversation_error(
            ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED, details={"reason": "over_budget"}
        )


def test_an_unknown_reason_is_refused_rather_than_defaulted():
    """A typo must not silently become 'not retryable' and strand a transient failure."""

    with pytest.raises(ValueError, match="reason"):
        context_limit_error("summry_failed")


def test_the_run_event_vocabulary_gains_the_assembly_fact():
    assert RunEventType.CONTEXT_ASSEMBLED == "CONTEXT_ASSEMBLED"


def test_the_summary_source_names_itself():
    assert SOURCE_CONTEXT_SUMMARY == "context_summary"


def test_the_summary_scope_names_the_call_without_losing_the_turn():
    """The turn's conversation and run must survive, or the call is unattributable."""

    with audit_context(AuditContext(conversation_id="c1", run_id="r1", source=SOURCE_CONVERSATION)):
        with summary_context():
            inside = current_context()

    assert (inside.conversation_id, inside.run_id) == ("c1", "r1")
    assert inside.source == SOURCE_CONTEXT_SUMMARY
    assert current_context() is None, "the previous value is restored"


def test_the_summary_scope_works_with_no_turn_to_inherit_from():
    """A summary can be asked for outside a turn, where there is nothing to carry over: that
    must scope to an empty context naming the summary, not crash and not bind `None`."""

    assert current_context() is None, "the test starts outside any turn"

    with summary_context():
        inside = current_context()

    assert inside is not None, "the scope binds a context even with none to inherit"
    assert inside.source == SOURCE_CONTEXT_SUMMARY
    assert (inside.conversation_id, inside.run_id, inside.user_id) == (None, None, None)
    assert current_context() is None, "the previous value is restored"


def test_the_dispatcher_is_a_named_agent():
    """`source` says what kind of call it was; `agent_id` says which agent asked."""

    assert AGENT_ID_SUPERVISOR == "supervisor"


def test_a_context_carries_the_agent_that_made_the_call():
    """Naming the agent must not cost the call the turn it belongs to."""

    with audit_context(AuditContext(conversation_id="c1", run_id="r1", source=SOURCE_CONVERSATION)):
        with audit_context(current_context().merged(agent_id="card")):
            inside = current_context()

    assert inside.agent_id == "card"
    assert (inside.conversation_id, inside.run_id, inside.source) == (
        "c1",
        "r1",
        SOURCE_CONVERSATION,
    )


def test_an_unknown_agent_does_not_clear_a_known_one():
    """An override of `None` means "this caller does not know", not "clear the field"."""

    assert AuditContext(agent_id="card").merged(agent_id=None).agent_id == "card"
    assert AuditContext().agent_id is None, "a call that belongs to no agent records none"


def test_a_context_length_error_is_classified_as_an_overflow():
    class ProviderError(Exception):
        status_code = 400

        def __str__(self):
            return "This model's maximum context length is 128000 tokens"

    classified = classify_provider_error(ProviderError())

    assert classified.code == "LLM_CONTEXT_OVERFLOW"
    assert classified.retryable is False, "the same request cannot fit next time either"


def test_an_ordinary_bad_request_is_still_a_bad_request():
    class ProviderError(Exception):
        status_code = 400

        def __str__(self):
            return "invalid role"

    assert classify_provider_error(ProviderError()).code == "LLM_BAD_REQUEST"
