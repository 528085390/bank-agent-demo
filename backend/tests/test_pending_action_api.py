"""The authenticated button event, over HTTP.

A confirmation box is answered on its own route -- `POST /api/conversations/{id}/pending-actions/
{pending_id}/decide` -- and nowhere else. What this file pins is the part that makes that route
an authorization rather than a message:

1. the click must name the pause the server itself recorded **and** the pending operation that
   pause was built from, so a click on a preview that has since been replaced is refused;
2. it is the only way in: a chat message that says 确认 travels the ordinary route, where the
   pause refuses it before anything is written;
3. a double click is one submission, not two;
4. another user cannot click someone else's card.
"""

import json
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.turn import TurnAction, TurnConfirmation, TurnResult, TurnStatus
from app.main import create_app
from app.modules.registry import build_registry
from app.tools.gateway import ToolGateway
from app.tools.recording import RecordingAdapter
from tests.support.handler_graph import handler_module
from tests.support.scripted_model import ScriptedModel
from tests.test_conversation_api import create, messages, second_session, submit
from tests.test_multi_agent_api import frames

PENDING_ID = "wp_api_1"
PREVIEW = TurnConfirmation(
    pending_id=PENDING_ID,
    operation="purchase_product",
    title="确认申购",
    amount_minor=200000,
    product_name="演示灵活理财",
    account_display="a_wealth_main",
    expires_at="2030-01-01T00:00:00+00:00",
)
# A card the bank will only authorise with a code: it asks for one, and the click must carry it.
PREVIEW_WITH_CODE = TurnConfirmation(
    pending_id=PENDING_ID,
    operation="purchase_product",
    title="确认申购",
    amount_minor=200000,
    product_name="演示灵活理财",
    account_display="a_wealth_main",
    expires_at="2030-01-01T00:00:00+00:00",
    requires_code=True,
    code_label="模拟验证码",
)
PREVIEW_SUMMARY = "请确认这笔模拟申购：2,000.00 元，从 a_wealth_main 扣款。"
# 这个文件里的卡片来自脚本，不是理财自己的图：用一个同名模块、脚本化的图接回来。
CARD_DECLARATION = DomainDeclaration(agent_id="wealth", display="理财 Agent", purpose="理财")


class ConfirmingSpy:
    """A scripted module that shows one preview and answers button events from a script."""

    def __init__(self, action: TurnResult, preview: TurnConfirmation = PREVIEW) -> None:
        self._action = action
        self._preview = preview
        self.actions: list[TurnAction] = []

    def __call__(self, context, model, message) -> TurnResult:
        return TurnResult(
            status=TurnStatus.WAITING_CONFIRMATION,
            summary=PREVIEW_SUMMARY,
            confirmation=self._preview,
        )

    def act(self, context, model, action: TurnAction) -> TurnResult:
        self.actions.append(action)
        return self._action


def paused(rt, session, *, action: TurnResult | None = None, preview: TurnConfirmation = PREVIEW):
    """One conversation paused on a confirmation card, over the real HTTP lifecycle."""

    model = ScriptedModel(
        [
            ("call", "handoff", '{"agent_id":"wealth","instruction":"买一笔"}'),
        ]
    )
    spy = ConfirmingSpy(
        action or TurnResult(status=TurnStatus.COMPLETED, summary="模拟申购已完成。"), preview
    )
    # 这个文件钉的是按钮事件那条 HTTP 路由，不是理财自己的图。理财通过 graph_builder
    # 回答，所以这里换一个同名模块、脚本化的图，让卡片仍然来自上面的脚本。
    registry = build_registry([handler_module("wealth", CARD_DECLARATION, spy)])
    gateway = ToolGateway(registry.catalog, RecordingAdapter({}), audit=rt.audit)
    checkpoint = InMemorySaver()

    def rebuild():
        return AgentRuntime(
            model,
            graph_factory=lambda model, context, assembler=None, turn=None: build_agent_graph(
                model,
                gateway,
                context,
                assembler=assembler,
                turn=turn,
                roster=registry.roster,
            ),
            checkpointer=checkpoint,
            customer_resolver=rt.identity.bank_customer_id,
        )

    rt.agent = rebuild()
    client = TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )
    conversation = create(client)
    response = submit(client, conversation["conversation_id"], "买 2000 元的演示灵活理财")
    question = next(data for name, data in frames(response) if name == "interrupt")
    return client, conversation, question, spy, rebuild


def decide(client, conversation_id, pending_id, body, *, key=None):
    """One click. The key is the client's, as on the message route: a double click repeats one
    request and a retry after a rejected code is a different one."""

    return client.post(
        f"/api/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json=body,
        headers={"Idempotency-Key": key or f"click-{uuid4().hex}"},
    )


def test_the_card_is_restored_from_the_checkpoint_with_its_pending_id(rt, session):
    """刷新页面后确认框还在，而且显示的还是用户看到的那一笔。"""

    client, conversation, question, _spy, rebuild = paused(rt, session)
    # The graph is rebuilt every turn in production; the pause has to survive that.
    rt.agent = rebuild()

    detail = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]

    card = detail["pending_interrupt"]
    assert card["kind"] == "confirmation"
    assert card["question_id"] == question["question_id"]
    assert card["confirmation"]["pending_id"] == PENDING_ID
    assert card["confirmation"]["amount_minor"] == 200000
    assert card["confirmation"]["product_name"] == "演示灵活理财"
    assert card["confirmation"]["account_display"] == "a_wealth_main"
    assert detail["recovery_required"] is False


def test_the_click_reaches_the_module_and_streams_the_result(rt, session):
    client, conversation, question, spy, _rebuild = paused(rt, session)
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {"question_id": question["question_id"], "decision": "confirm"},
    )

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["Content-Type"]
    parsed = frames(response)
    # 点击是恢复，不是新的一轮：计划属于用户已经不在看的那一轮，所以基座层先从检查点
    # 把计划和正在跑的节点重发一遍，再续上这个领域的结论。
    assert [name for name, _ in parsed] == [
        "plan",
        "task_update",
        "agent_start",
        "subtask_end",
        "task_update",
        "final",
    ]
    # 恢复时重发的是"正在跑"，点击之后的那一轮才把它标成"完成"。
    assert [data["status"] for name, data in parsed if name == "task_update"] == [
        "running",
        "done",
    ]
    assert next(data["text"] for name, data in parsed if name == "final") == "模拟申购已完成。"
    # The module received a structured action, not a sentence: only confirm + the pending id.
    assert [(item.kind, item.pending_id) for item in spy.actions] == [("confirm", PENDING_ID)]

    # The click is in the ledger as an answer, and the reply is readable back after a reload.
    ledger = messages(client, conversation["conversation_id"])["items"]
    assert len(ledger) == before + 2
    assert ledger[-2]["message_kind"] == "interrupt_answer"
    assert ledger[-2]["question_id"] == question["question_id"]
    assert ledger[-2]["content"] == "确认本次操作"
    assert ledger[-1]["role"] == "assistant"
    # The card is consumed: nothing is left waiting.
    detail = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]
    assert detail["pending_interrupt"] is None


def test_cancel_is_a_decision_of_its_own(rt, session):
    client, conversation, question, spy, _rebuild = paused(
        rt,
        session,
        action=TurnResult(status=TurnStatus.COMPLETED, summary="已停止当前未提交的理财待办。"),
    )

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {"question_id": question["question_id"], "decision": "cancel"},
    )

    assert response.status_code == 200, response.text
    assert [(item.kind, item.pending_id) for item in spy.actions] == [("cancel", PENDING_ID)]
    # The module's own conclusion is the turn's reply: the click path ends without a
    # second model call, so what the user reads is exactly what the module says.
    parsed = frames(response)
    assert next(data["summary"] for name, data in parsed if name == "subtask_end") == (
        "已停止当前未提交的理财待办。"
    )
    assert next(data["text"] for name, data in parsed if name == "final") == "已停止当前未提交的理财待办。"


def test_a_click_that_does_not_match_the_open_card_is_refused(rt, session):
    """错编号、错问题、错会话都指向同一件事：这不是当前待确认的那一笔。"""

    client, conversation, question, spy, _rebuild = paused(rt, session)
    before = len(messages(client, conversation["conversation_id"])["items"])

    other_pending = decide(
        client,
        conversation["conversation_id"],
        "wp_someone_elses",
        {"question_id": question["question_id"], "decision": "confirm"},
    )
    assert other_pending.status_code == 409
    assert other_pending.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"

    other_question = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {"question_id": "q_forged", "decision": "confirm"},
    )
    assert other_question.status_code == 409
    assert other_question.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"

    other_conversation = create(client)
    foreign = decide(
        client,
        other_conversation["conversation_id"],
        PENDING_ID,
        {"question_id": question["question_id"], "decision": "confirm"},
    )
    assert foreign.status_code == 409

    assert spy.actions == []
    assert len(messages(client, conversation["conversation_id"])["items"]) == before


def test_a_chat_message_that_says_confirm_is_not_an_authorization(rt, session):
    """聊天里的"确认"两个字走普通消息通道：暂停中的问题直接拒绝它，模块拿不到任何动作。"""

    client, conversation, _question, spy, _rebuild = paused(rt, session)
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = submit(client, conversation["conversation_id"], "确认")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert spy.actions == []
    assert len(messages(client, conversation["conversation_id"])["items"]) == before


def test_a_repeat_click_is_one_submission(rt, session):
    """重复点击不能重复提交：带着同一个键的第二次是同一次提交的重放，模块只被调用一次。"""

    client, conversation, question, spy, _rebuild = paused(rt, session)
    body = {"question_id": question["question_id"], "decision": "confirm"}
    key = f"click-{uuid4().hex}"

    first = decide(client, conversation["conversation_id"], PENDING_ID, body, key=key)
    second = decide(client, conversation["conversation_id"], PENDING_ID, body, key=key)

    assert first.status_code == 200
    assert "text/event-stream" in first.headers["Content-Type"]
    # A byte-identical repeat reuses the key, so it is answered with the receipt rather than
    # resuming the graph a second time.
    assert second.status_code == 200
    assert "application/json" in second.headers["Content-Type"]
    assert second.json()["data"]["duplicate"] is True
    assert [item.pending_id for item in spy.actions] == [PENDING_ID]

    ledger = messages(client, conversation["conversation_id"])["items"]
    answers = [item for item in ledger if item["message_kind"] == "interrupt_answer"]
    assert len(answers) == 1


def test_a_click_without_a_key_is_refused(rt, session):
    """按钮事件和一条消息一样必须带键：没有键就无法区分"重试"和"重复提交"。"""

    client, conversation, question, _spy, _rebuild = paused(rt, session)

    response = client.post(
        f"/api/conversations/{conversation['conversation_id']}/pending-actions/{PENDING_ID}/decide",
        json={"question_id": question["question_id"], "decision": "confirm"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_second_click_after_the_card_is_consumed_is_refused(rt, session):
    """卡片被消费之后，换一个键再点就是"没有待确认的操作"，而不是再提交一次。"""

    client, conversation, question, spy, _rebuild = paused(rt, session)
    body = {"question_id": question["question_id"], "decision": "confirm"}

    assert decide(client, conversation["conversation_id"], PENDING_ID, body).status_code == 200
    stale = decide(client, conversation["conversation_id"], PENDING_ID, body)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert [item.pending_id for item in spy.actions] == [PENDING_ID]


def test_a_card_that_asks_for_a_code_will_not_take_a_click_without_one(rt, session):
    """银行要验证码时，没带的点击在**写任何东西之前**被拒绝，模块也拿不到动作。"""

    client, conversation, question, spy, _rebuild = paused(rt, session, preview=PREVIEW_WITH_CODE)
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {"question_id": question["question_id"], "decision": "confirm"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VERIFICATION_CODE_REQUIRED"
    assert spy.actions == []
    # Nothing was written: no ledger row, and the card is still the one that was open.
    assert len(messages(client, conversation["conversation_id"])["items"]) == before
    detail = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]
    assert detail["pending_interrupt"]["confirmation"]["requires_code"] is True


def test_the_typed_code_reaches_the_module_and_never_the_ledger(rt, session):
    """用户输入的验证码交到模块手上，但它不进账本、不进事件、不进检查点。"""

    client, conversation, question, spy, _rebuild = paused(rt, session, preview=PREVIEW_WITH_CODE)

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
        },
    )

    assert response.status_code == 200, response.text
    assert [(item.kind, item.pending_id, item.verification_code) for item in spy.actions] == [
        ("confirm", PENDING_ID, "123456")
    ]
    # The secret travelled to the module and stops there.
    assert "123456" not in response.text
    assert "123456" not in json.dumps(messages(client, conversation["conversation_id"]))
    assert "123456" not in client.get("/api/audit/run-events").text
    # The answer recorded in the ledger is the decision, not the code.
    ledger = messages(client, conversation["conversation_id"])["items"]
    answer = next(item for item in ledger if item["message_kind"] == "interrupt_answer")
    assert answer["content"] == "确认本次操作"


def test_a_cancel_never_carries_a_code(rt, session):
    """取消不需要验证码：即使卡片要求它，取消事件里也不会带上一个。"""

    client, conversation, question, spy, _rebuild = paused(rt, session, preview=PREVIEW_WITH_CODE)

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {
            "question_id": question["question_id"],
            "decision": "cancel",
            "verification_code": "123456",
        },
    )

    assert response.status_code == 200, response.text
    assert [(item.kind, item.verification_code) for item in spy.actions] == [("cancel", None)]


def test_another_user_cannot_click_the_card(rt, session):
    client, conversation, question, spy, _rebuild = paused(rt, session)
    stranger = second_session(rt)
    before = len(messages(client, conversation["conversation_id"])["items"])

    response = client.post(
        f"/api/conversations/{conversation['conversation_id']}/pending-actions/{PENDING_ID}/decide",
        headers={"Authorization": "Bearer " + stranger["token"]},
        json={"question_id": question["question_id"], "decision": "confirm"},
    )

    assert response.status_code == 404
    assert spy.actions == []
    assert len(messages(client, conversation["conversation_id"])["items"]) == before


def test_the_decision_is_a_machine_value_and_nothing_else(rt, session):
    """按钮文案不是协议：客户端只能发 confirm/cancel，多一个字段就是 422。"""

    client, conversation, question, _spy, _rebuild = paused(rt, session)

    for body in (
        {"question_id": question["question_id"], "decision": "确认"},
        {"question_id": question["question_id"], "decision": "confirm", "amount_minor": 1},
        {"question_id": question["question_id"]},
    ):
        response = decide(client, conversation["conversation_id"], PENDING_ID, body)
        assert response.status_code == 422, json.dumps(body)


def test_an_unknown_result_is_never_reported_as_a_failure(rt, session):
    """结果未知：按钮事件回来的那句话保留"待核实"含义，页面拿到的 turn_status 也是 unknown。"""

    client, conversation, question, _spy, _rebuild = paused(
        rt,
        session,
        action=TurnResult(status=TurnStatus.UNKNOWN, summary="这笔交易的结果暂时未知。"),
    )

    response = decide(
        client,
        conversation["conversation_id"],
        PENDING_ID,
        {"question_id": question["question_id"], "decision": "confirm"},
    )

    parsed = frames(response)
    subtask: Any = next(data for name, data in parsed if name == "subtask_end")
    assert subtask["turn_status"] == "unknown"
    assert "结果暂时未知" in subtask["summary"]
    # The frozen event vocabulary has no word for "neither", so the event status stays on a
    # non-success value; the honest wording travels in the summary and in `turn_status`.
    assert subtask["status"] == "FAILED"


def test_card_confirmation_decide_authorizes_then_executes(rt, session):
    """A3: card uses the same decide entry; one click authorizes at the bank and resumes execute."""

    from tests.test_card_authorization_api import ACTION_ID, paused_card
    from tests.test_card_authorization_api import decide as card_decide

    client, conversation, question, bank, _model, _rebuild = paused_card(rt, session)
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["pending_id"] == ACTION_ID
    assert question["confirmation"]["requires_code"] is True

    response = card_decide(
        client,
        conversation["conversation_id"],
        ACTION_ID,
        {
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
        },
    )
    assert response.status_code == 200, response.text
    assert any(name == "final" for name, _ in frames(response))
    assert bank.authorized
    assert any(request["path"].endswith("/authorize") for request in bank.requests)
    assert any(request["path"].endswith("/execute") for request in bank.requests)
    detail = client.get(f"/api/conversations/{conversation['conversation_id']}").json()["data"]
    assert detail["pending_interrupt"] is None


def test_a_client_cannot_smuggle_the_auto_cancel_signal(rt, session):
    """`auto_cancel` 是服务端专用通道：客户端把它塞进 body 只会得到 422。

    The signal says "the bank voided this operation", which only the server can know. A client
    that tries to open the channel itself must be refused at the schema boundary -- before the
    bank is touched and before anything is ledgered -- and must never produce a `card_cancel`.
    """

    from tests.test_card_authorization_api import ACTION_ID, paused_card

    client, conversation, question, bank, _model, _rebuild = paused_card(rt, session)
    before_requests = len(bank.requests)
    before_ledger = len(messages(client, conversation["conversation_id"])["items"])

    response = decide(
        client,
        conversation["conversation_id"],
        ACTION_ID,
        {
            "question_id": question["question_id"],
            "decision": "confirm",
            "verification_code": "123456",
            "auto_cancel": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMETER"
    # Nothing was written and the bank was never called.
    assert len(bank.requests) == before_requests
    assert len(messages(client, conversation["conversation_id"])["items"]) == before_ledger
    # And no cancellation tool call was invented on the client's behalf.
    events = client.get("/api/audit/run-events").json()["data"]["items"]
    tool_calls = [item for item in events if item["event_type"] == "TOOL_CALL"]
    assert all(item["payload"]["tool"] != "card_cancel" for item in tool_calls)


def test_a_replayed_click_returns_the_receipt_without_authorizing_twice(rt, session):
    """同一个键加同一个 body 的重放是同一笔提交：返回首次回执，银行只被 authorize 一次。"""

    from tests.test_card_authorization_api import ACTION_ID, paused_card
    from tests.test_card_authorization_api import decide as card_decide

    client, conversation, question, bank, _model, _rebuild = paused_card(rt, session)
    body = {
        "question_id": question["question_id"],
        "decision": "confirm",
        "verification_code": "123456",
    }
    key = f"click-{uuid4().hex}"

    first = card_decide(client, conversation["conversation_id"], ACTION_ID, body, key=key)
    assert first.status_code == 200, first.text
    authorizes = [item for item in bank.requests if item["path"].endswith("/authorize")]
    assert len(authorizes) == 1

    second = card_decide(client, conversation["conversation_id"], ACTION_ID, body, key=key)
    assert second.status_code == 200
    assert "application/json" in second.headers["Content-Type"]
    assert second.json()["data"]["duplicate"] is True
    assert len([item for item in bank.requests if item["path"].endswith("/authorize")]) == 1
