"""The pending-action decision state machine, with no HTTP in the way.

`POST /api/conversations/{id}/pending-actions/{pending_id}/decide` is one way into a
confirmation box; the Feishu channel will be another. Both have to reach the same binding
checks -- the click names the pause the server recorded *and* the operation that pause was
built from -- so those checks live in `app.api.pending_actions` and the route is only the
HTTP adapter.

What this file pins is the part a second caller cannot restate for itself: the *order* of the
refusals, and the fact that a refusal happens before the turn lifecycle writes anything.

1. a conversation that is not the caller's is refused first (404), even when the request
   carries no idempotency key -- the key is read where it has always been read, after the
   writability check, so the two refusals cannot swap places;
2. a request with no key at all is refused as soon as the key is read, before the click is
   checked and before the bank is asked to authorize anything -- an authorization at the bank
   is a write, so it cannot precede a refusal that says the request is unwritable;
3. a click that matches no open pause is refused as `INVALID_INTERRUPT_ANSWER`, and it names
   the question the client should be looking at instead;
4. nothing is written on any of these refusals: no message, no submission, no consumed
   sequence, and no authorization at the bank;
5. the receipt path -- a key that was already spent -- arrives with the conversation lock
   already released, so the same conversation can be taken again immediately.
"""

from types import SimpleNamespace

import pytest

from app.api.pending_actions import (
    PendingActionPreparation,
    execute_pending_action_decision,
    submit_prepared_turn,
)
from app.conversation.service import Receipt
from app.core.errors import AppError, ConversationErrorCode, conversation_error
from tests.test_conversation_api import second_session


def who_from(session) -> dict:
    """The identity dict the HTTP layer hands the state machine.

    真实账号之后 `identity(request)` 解的是浏览器那张 token（`Identity.authenticate`），
    内部 `session_id` 不再对外可查。测试手里只有签发时那本字典，就按同一个形状拼出来。
    """

    return {
        "session_id": session["session_id"],
        "user_id": session["user_id"],
        "login_name": session.get("login_name"),
        "role": session.get("role"),
    }


@pytest.fixture
def who(session):
    """The identity dict the HTTP layer hands the state machine."""

    return who_from(session)


async def test_pending_action_decision_invalid_click(rt, who):
    conv = rt.conversations.create(who["user_id"])
    with pytest.raises(AppError) as exc_info:
        # 键是新的，只是点错了卡：无键的拒绝排在点击检查之前，所以这里必须带键才能
        # 走到「这张卡不是当前待确认的那一笔」。
        await execute_pending_action_decision(
            rt=rt,
            who=who,
            conversation_id=conv.conversation_id,
            pending_id="non-existent-pid",
            question_id="non-existent-qid",
            decision="confirm",
            idempotency_key="k-test",
        )
    assert exc_info.value.code == "INVALID_INTERRUPT_ANSWER"


async def test_the_refusal_names_the_question_the_client_should_answer(rt, who):
    """「当前待确认的是哪一笔」跟着拒绝一起回去，客户端才有东西可指向。"""

    conv = rt.conversations.create(who["user_id"])
    with pytest.raises(AppError) as exc_info:
        await execute_pending_action_decision(
            rt=rt,
            who=who,
            conversation_id=conv.conversation_id,
            pending_id="wp_gone",
            question_id="q_gone",
            decision="cancel",
            idempotency_key="k-test",
        )
    error = exc_info.value
    assert error.status == 409
    # Nothing is open, so the answer is "nothing": the placeholder is present all the same,
    # which is what lets the client distinguish "no pause" from "this field is ignored".
    assert error.details == {"question_id": None}


async def test_a_refused_click_writes_nothing(rt, who):
    conv = rt.conversations.create(who["user_id"])
    before = rt.conversations.messages(who["user_id"], conv.conversation_id)["items"]

    with pytest.raises(AppError):
        await execute_pending_action_decision(
            rt=rt,
            who=who,
            conversation_id=conv.conversation_id,
            pending_id="non-existent-pid",
            question_id="non-existent-qid",
            decision="confirm",
            idempotency_key="k-test",
        )

    assert rt.conversations.messages(who["user_id"], conv.conversation_id)["items"] == before


async def test_the_key_is_read_after_the_writability_check(rt, who):
    """错人点错会话是 404，不是 400：幂等键读得比「这个会话是不是你的」晚。

    The route reads `Idempotency-Key` through a reader precisely so this stays true; without
    it a stranger would be told "missing key" instead of "not found", which is both wrong and
    a probe for conversation ids that are not theirs.
    """

    conv = rt.conversations.create(who["user_id"])
    # 一个真的属于别人的会话：演示登录每次都落回同一个用户，所以要另建一个用户。
    stranger = who_from(second_session(rt))

    def unreadable_key() -> str:
        raise conversation_error(ConversationErrorCode.IDEMPOTENCY_KEY_REQUIRED)

    with pytest.raises(AppError) as exc_info:
        await execute_pending_action_decision(
            rt=rt,
            who=stranger,
            conversation_id=conv.conversation_id,
            pending_id="non-existent-pid",
            question_id="non-existent-qid",
            decision="confirm",
            idempotency_key=unreadable_key,
        )
    assert exc_info.value.code == "NOT_FOUND"
    assert exc_info.value.status == 404


async def test_a_keyless_decision_is_refused_before_the_bank_authorizes(rt, who, monkeypatch):
    """没有键的点击连银行都不能碰：授信本身就是一次写入，拒绝必须先于它。

    The click below is otherwise a perfectly good one -- an open card, a matching question and
    pending id, and a card-action id the bank would accept -- so the *only* thing stopping it
    is the missing key. Before the refusal moved up to where the key is read, this exact
    request authorized the card at the bank and was answered 400 afterwards: a money-side
    effect left behind by a request that was refused.
    """

    conv = rt.conversations.create(who["user_id"])
    action_id = "CA-" + "a" * 32
    calls: list[tuple] = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return SimpleNamespace(data={"status": "authorized", "verification_passed": True})

    async def recovery_hint(*args, **kwargs):
        return SimpleNamespace(
            pending={
                "kind": "confirmation",
                "question_id": "q_open",
                "confirmation": {"pending_id": action_id, "requires_code": False},
            },
            recovery_required=False,
        )

    # 只换掉"当前开着哪一张卡"和"银行在哪里"，其余都是真的：真实会话、真实决策机。
    monkeypatch.setattr(rt, "agent", SimpleNamespace(recovery_hint=recovery_hint))
    monkeypatch.setattr(rt, "bank_client", SimpleNamespace(request=request))

    with pytest.raises(AppError) as exc_info:
        await execute_pending_action_decision(
            rt=rt,
            who=who,
            conversation_id=conv.conversation_id,
            pending_id=action_id,
            question_id="q_open",
            decision="confirm",
        )

    assert exc_info.value.code == "IDEMPOTENCY_KEY_REQUIRED"
    assert exc_info.value.status == 400
    assert calls == []


async def test_a_repeat_key_returns_the_receipt_with_the_lock_already_released(rt, who):
    """同键的第二次是回执，而且锁已经放开：不然这个会话在本进程余下的生命里都进不来。

    This pins the handoff rather than the graph: the first call is the ordinary submission
    step of the decide path (it persists the click and hands the lock to the response body),
    the second is that same submission replayed.
    """

    conv = rt.conversations.create(who["user_id"])
    # 这张图没有在等任何人：这里钉的是提交步骤的锁交接，不是恢复语义，所以不带 resume。
    preparation = PendingActionPreparation(
        conversation_id=conv.conversation_id,
        message="确认本次操作",
        context=None,
        key="k-receipt",
    )

    first = await submit_prepared_turn(rt, who, preparation)
    assert first.status == "streaming"
    assert first.held_lock is not None
    # 流式路径的释放归响应体（routes._stream_turn 的 finally），这里替它收尾。
    first.held_lock.release()

    second = await submit_prepared_turn(rt, who, preparation)
    assert second.status == "receipt"
    assert isinstance(second.outcome, Receipt)
    assert second.held_lock is None
    assert second.naming is None

    # 回执路径确实把锁还回来了：同一会话现在还能再拿到。
    again = await rt.turns.acquire(conv.conversation_id)
    again.release()
