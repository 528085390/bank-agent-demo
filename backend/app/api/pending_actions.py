"""The pending-action decision state machine, shared by every way in.

A confirmation box is answered from the SPA today (`POST /conversations/{id}/pending-actions/
{pending_id}/decide`) and from an IM channel next. The two must not grow two implementations
of "is this click the pause that is open right now": that binding *is* the authorization, and a
second copy of it would be a second policy that drifts.

So the whole decision lives here -- writability, the normalized intent, the idempotency gate,
the click binding, the verification-code rules, the bank authorization, and the turn lifecycle
-- and `app.api.routes` keeps only the HTTP adaptation: read the header, call this, then choose
a `StreamingResponse` or a JSON receipt from what came back. A channel layer calls the same
function and does its own transport.

The order of the refusals is part of the contract, not an accident of the code:

1. the conversation must be the caller's and writable (404/409) -- checked before the
   idempotency key is even read, so a stranger is never told "missing key" about a
   conversation that is not theirs;
2. a request with no key is refused the moment the key is read
   (`IDEMPOTENCY_KEY_REQUIRED`, 400), before the click is checked and before the bank is
   called: an authorization at the bank is a write, and a submission nobody can recognise
   on a repeat must not cause one;
3. a click that is *not* a repeat of something already settled must match the open pause
   (`INVALID_INTERRUPT_ANSWER`, 409). A repeat skips this: the card it answered is by
   definition gone, and the lifecycle answers a replay with its receipt;
4. a card that asks for a verification code will not take a click without one (`VERIFICATION_
   CODE_REQUIRED`, 422) -- refused before anything is written;
5. the bank authorizes the card action in this same request, so a wrong code is refused
   (`VERIFICATION_FAILED`, 422) while the pause stays open and nothing is ledgered.

What is deliberately *not* here: the verification code. It rides `TurnPlan.verification_code`
and stops at the module; it is never folded into the resume value, the request fingerprint, the
ledger or an audit row -- which is why a preparation carries it *beside* the context rather
than inside it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from starlette.concurrency import run_in_threadpool

from app.agents.hitl import _authorize_card_action_if_needed, _click_matches, _requires_code
from app.conversation.service import Receipt, request_hash
from app.conversation.turns import DeferredNaming, HeldLock, TurnPlan
from app.core.errors import AppError, ConversationErrorCode, conversation_error

# A client's idempotency key, or a zero-argument reader for one. The reader form exists for
# the HTTP route: it has to read `Idempotency-Key` at exactly the point this module reads the
# key -- after the writability check -- or the two refusals swap order and a conversation that
# is not the caller's starts answering "missing key" instead of 404.
KeySource = str | Callable[[], str]


@dataclass(frozen=True, slots=True)
class PendingActionPreparation:
    """One imminent turn: what will be sent, under which key, with which secrets.

    `context` is the resume envelope the graph will read -- the machine decision, never the
    button's wording, and never the code. `key` is what this submission is deduplicated
    against, and `auto_cancel` is the server's own statement that the bank voided the
    operation, which no client may ever supply.

    The ordinary message route builds the same shape, which is why the submission step below
    is shared rather than copied: one lock protocol, one place where a receipt is recognised.
    """

    conversation_id: str
    message: str
    context: Mapping[str, Any] | None
    key: str
    verification_code: str | None = None
    auto_cancel: bool = False


@dataclass(frozen=True, slots=True)
class PendingActionResult:
    """What one submission turned into, before any transport has been chosen.

    `status` says which shape the answer has -- a stream whose lock is still held, or a
    receipt for a key that was already spent. The caller owns the lock from here: the streaming
    path must release it when its body ends (and the routes' generator does so in a
    `finally`), and the receipt path arrives already released.
    """

    status: Literal["streaming", "receipt"]
    outcome: TurnPlan | Receipt
    held_lock: HeldLock | None = None
    naming: DeferredNaming | None = None


def _key_of(source: KeySource | None) -> str | None:
    """The caller's key, read here when it was handed over as a reader (see `KeySource`)."""

    if source is None:
        return None
    return source() if callable(source) else source


async def execute_pending_action_decision(
    rt,
    who: dict,
    conversation_id: str,
    pending_id: str,
    question_id: str,
    decision: str,
    verification_code: str | None = None,
    transaction_password: Any | None = None,
    idempotency_key: KeySource | None = None,
) -> PendingActionResult:
    """Answer one confirmation box: check the click, authorize it, then run the turn.

    `pending_id` and `question_id` are both required because they bind different things: the
    question is the pause the server itself recorded, the pending id is the operation that pause
    was built from. A click on a preview that has since been replaced matches neither and is
    refused, instead of being applied to whatever happens to be open now.

    `idempotency_key` is the client's key: a double click repeats one request and is answered
    with the first one's receipt, while a *retry* after a rejected code is a genuinely different
    request and must not be deduplicated against it. A caller with no key at all is refused
    (`IDEMPOTENCY_KEY_REQUIRED`, 400) exactly where the key would have been read -- before the
    click is checked and before the bank authorizes anything: without a key the same click has
    no way to be recognised as the same click, so nothing may be written on its behalf.
    """

    preparation = await _prepare(
        rt,
        who,
        conversation_id,
        pending_id,
        question_id,
        decision,
        verification_code,
        transaction_password,
        idempotency_key,
    )
    return await submit_prepared_turn(rt, who, preparation)


async def _prepare(
    rt,
    who: dict,
    conversation_id: str,
    pending_id: str,
    question_id: str,
    decision: str,
    verification_code: str | None,
    transaction_password: Any | None,
    idempotency_key: KeySource | None,
) -> PendingActionPreparation:
    """The five checks, in their fixed order, ending in something safe to submit."""

    conversation = await run_in_threadpool(
        rt.conversations.require_writable, who["user_id"], conversation_id
    )
    # Domain-agnostic ledger wording: wealth and card both use this route.
    message = "确认本次操作" if decision == "confirm" else "取消本次操作"
    # The value is the machine decision, not the button's wording, and it deliberately does
    # not carry the code: this dict is hashed into the request fingerprint and echoed back in
    # a replay, and a secret belongs in neither.
    context = {"resume": {"question_id": question_id, "value": {"decision": decision}}}
    key = _key_of(idempotency_key)
    if key is None:
        # Refused on the line the key is read, before a single check can cause work: the bank
        # authorization below is a write, and a submission nobody can recognise on a repeat
        # must not reach it. Nothing is looked up, nothing is clicked and nothing is authorized.
        raise conversation_error(ConversationErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    code = (verification_code or "").strip()
    # Only the server can set this: the client's body cannot carry it, and a replay path never
    # reaches the bank at all.
    auto_cancel = False
    # A repeat of a click that already went through is the lifecycle's to answer, and it
    # answers it before anything else happens. Only a click that is *not* a repeat has to
    # match the pause that is open right now.
    settled = await run_in_threadpool(
        rt.conversations.find_submission,
        who["user_id"],
        key,
        request_hash(conversation_id, message, context),
    )
    if settled is None:
        hint = await rt.agent.recovery_hint(
            who["user_id"], who["session_id"], conversation.thread_id
        )
        if not _click_matches(hint.pending, question_id, pending_id):
            # Nothing about the click matches what is open: it is a stale card, a card from
            # another conversation, or a hand-made request. All three are the same refusal,
            # and it names the question the client should be looking at instead.
            raise AppError(
                "INVALID_INTERRUPT_ANSWER",
                "确认没有指向当前待确认的操作",
                409,
                details={"question_id": (hint.pending or {}).get("question_id")},
            )
        if decision == "confirm" and _requires_code(hint.pending) and not code:
            # The card asked for a code and this click carries none. Refused here, before the
            # turn lifecycle writes anything: a click that could not have worked must not
            # consume a sequence or leave a ledger row behind. The module checks again.
            raise AppError(
                "VERIFICATION_CODE_REQUIRED",
                "这笔操作需要验证码，请填写后重试",
                422,
                details={"question_id": question_id},
            )
        # Card actions: authorize at the bank in this same request, then resume execute.
        # A wrong code is refused here so the pause stays open and nothing is ledgered.
        outcome = await _authorize_card_action_if_needed(
            rt,
            who,
            hint.pending,
            decision,
            code or None,
            transaction_password=transaction_password,
        )
        auto_cancel = outcome.status == "cancelled"
    return PendingActionPreparation(
        conversation_id=conversation_id,
        message=message,
        context=context,
        key=key,
        verification_code=code or None,
        auto_cancel=auto_cancel,
    )


async def submit_prepared_turn(rt, who: dict, preparation: PendingActionPreparation):
    """Take the conversation lock, admit the submission, and say which answer it gets.

    The lock is held across the idempotency lookup, the checkpoint admission, the ledger write,
    the graph call and the final write -- a turn spans all of them, and releasing it earlier
    would let two turns of one conversation interleave on one checkpoint. It is therefore
    handed back on the streaming result and released by whoever ends the body; the receipt path
    releases it here, because its answer is complete.

    The one naming attempt the turn is allowed is not run here either: the stream registers it
    on the handle, and the response runs it as background work -- after the body is done, and so
    after the lock is released (design spec S-2.1).
    """

    turns = rt.turns
    held = await turns.acquire(preparation.conversation_id)
    try:
        outcome = await turns.prepare(
            who["user_id"],
            who["session_id"],
            preparation.conversation_id,
            idempotency_key=preparation.key,
            message=preparation.message,
            client_context=preparation.context,
            verification_code=preparation.verification_code,
            auto_cancel=preparation.auto_cancel,
        )
    except BaseException:
        held.release()
        raise
    if isinstance(outcome, Receipt):
        # The key is spent, so this is a replay: the user message is stored and no graph call
        # is made. Answering with JSON rather than an empty stream is what lets the client
        # tell "nothing new happened" from "the assistant said nothing".
        held.release()
        return PendingActionResult(status="receipt", outcome=outcome)
    return PendingActionResult(
        status="streaming",
        outcome=outcome,
        held_lock=held,
        naming=DeferredNaming(turns),
    )
