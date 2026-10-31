"""The server-only `auto_cancel` signal's carrier.

`auto_cancel` is the server's own statement that the bank voided the operation this turn is
resuming. It rides the turn the way `verification_code` does -- on TurnPlan / TurnContext,
never in `message` and never in `client_context` -- so it stays out of the request
fingerprint and out of the checkpoint. A client can never supply it: the escalation surface
is pinned in tests/test_pending_action_api.py.
"""

from app.agents.runtime import Admission, TurnContext
from app.conversation.service import Receipt
from app.conversation.turns import TurnPlan


def test_the_signal_defaults_to_off_on_the_carrier():
    turn = TurnContext(conversation_id="c", thread_id="t", message_id="m", content="x", sequence=1)

    assert turn.auto_cancel is False


def test_the_plan_defaults_the_signal_to_off():
    plan = TurnPlan(
        conversation_id="c",
        thread_id="t",
        receipt=Receipt(
            submission_id="s", message_id="m", run_id=None, conversation_id="c", sequence=1
        ),
        message="x",
        client_context=None,
        admission=Admission(resuming=False),
    )

    assert plan.auto_cancel is False
