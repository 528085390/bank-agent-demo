"""The trusted context carries the bank's customer id, and defaults to absent."""

from dataclasses import FrozenInstanceError

import pytest

from app.tools.protocol import TrustedContext


def test_a_context_without_a_bank_binding_is_legal_and_explicit():
    context = TrustedContext(user_id="user_0001", session_id="sess_1")

    assert context.bank_customer_id is None


def test_a_context_carries_the_bank_customer_id_when_it_has_one():
    context = TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id="u_zhang")

    assert context.bank_customer_id == "u_zhang"


def test_the_context_stays_frozen():
    context = TrustedContext(user_id="user_0001", session_id="sess_1")

    with pytest.raises(FrozenInstanceError):
        context.bank_customer_id = "u_zhang"
