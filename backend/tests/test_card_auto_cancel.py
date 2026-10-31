"""C08-c: the card graph consumes the server's auto-cancel and cancels at the bank.

The server decides this, never the client: `/decide` authorizes at the bank first, and the
third wrong code makes the bank void the action itself. That verdict rides the resume turn as
`TurnContext.auto_cancel`; what this file pins is what the card graph does with it -- go
through `card_cancel`, never read the answers and never execute -- and that a turn without the
signal behaves exactly as it did before.

The final assistant text is the model's own and is never asserted (spec §8 R-2b). What is
asserted is the module's own wording, the bank's traffic, and the absence of a new pause.
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime, TurnContext, thread_id_for_session
from app.agents.supervisor import build_domain_graph
from app.modules.card import MODULE
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from tests.support.scripted_model import ScriptedModel
from tests.test_card_authorization_api import ACTION_ID, AuthorizationBank, decide, paused_card
from tests.test_card_module import CONTEXT, CardBank

# The one cancellation wording: `app/modules/card/tools.py`'s `cancel`. The graph must not
# grow a second copy of it.
CANCEL_SUMMARY = "本次银行卡操作已取消，未修改银行卡。"
SESSION = "sess_card"


def turn_context(*, auto_cancel: bool) -> TurnContext:
    """The resume turn the decide route hands the runtime."""

    return TurnContext(
        conversation_id="conv_card",
        thread_id=thread_id_for_session(SESSION),
        message_id="msg_card",
        content="确认本次操作",
        sequence=1,
        auto_cancel=auto_cancel,
    )


def runtime_for(bank, replies, *, turn=None):
    """`test_card_module.runtime_for`, with the turn the graph is assembled for."""

    registry = build_registry([MODULE])
    gateway = ToolGateway(registry.catalog, BankToolAdapter(registry, bank))
    model = ScriptedModel(replies)
    spec = build_domain_specs(gateway, CONTEXT, roster=registry.roster)[0]
    graph = build_domain_graph(model, spec, turn=turn).copy(
        update={"checkpointer": InMemorySaver()}
    )
    return AgentRuntime(graph=graph), model, graph


def invoke(runtime, context=None):
    async def collect():
        return [
            event
            async for event in runtime.invoke(SESSION, "user_0001", "冻结我的卡", context or {})
        ]

    return asyncio.run(collect())


def resume(events, value):
    question = next(event.data for event in reversed(events) if event.event == "interrupt")
    return {"resume": {"question_id": question["question_id"], "value": value}}


def subtask_of(events):
    endings = [event.data for event in events if event.event == "subtask_end"]
    assert endings, f"the turn ended without a subtask_end: {[event.event for event in events]}"
    return endings[-1]


def domain_results(graph):
    """What the module wrote into its own state channel, read back from the checkpoint."""

    async def read():
        return await graph.aget_state(
            {"configurable": {"thread_id": thread_id_for_session(SESSION)}}
        )

    return list(asyncio.run(read()).values.get("domain_results") or [])


def cancels(bank):
    """Every bank authorize that asked for a cancellation."""

    return [
        request
        for request in bank.requests
        if request["path"].endswith("/authorize")
        and (request["json"] or {}).get("decision") == "cancel"
    ]


def test_auto_cancel_resume_cancels_at_the_bank_and_never_executes():
    bank = CardBank()
    runtime, model, graph = runtime_for(
        bank, [("call", "card_freeze", "{}")], turn=turn_context(auto_cancel=True)
    )
    first = invoke(runtime)
    question = next(event.data for event in first if event.event == "interrupt")
    assert question["kind"] == "confirmation"
    assert question["confirmation"]["pending_id"] == ACTION_ID

    events = invoke(runtime, resume(first, {"decision": "确认"}))

    # The summary is the tool's own wording, and it is what both projections carry.
    assert subtask_of(events)["summary"] == CANCEL_SUMMARY
    assert [item["summary"] for item in domain_results(graph)] == [CANCEL_SUMMARY]
    # Cancelling is the whole turn: the bank is told to cancel, and execute is never reached.
    assert len(cancels(bank)) == 1
    assert not any(request["path"].endswith("/execute") for request in bank.requests)
    # The pause is consumed: no second question, and no second model round to plan one.
    assert not any(event.event == "interrupt" for event in events)
    assert len(model.prompts) == 1


def test_without_the_server_signal_the_resume_still_executes():
    """The regression pin: a field on the turn changes nothing until the server sets it."""

    bank = CardBank()
    runtime, _, _ = runtime_for(bank, [("call", "card_freeze", "{}")], turn=None)
    first = invoke(runtime)
    forged = invoke(runtime, resume(first, {"decision": "确认"}))
    assert any(event.event == "interrupt" for event in forged)
    assert cancels(bank) == []

    bank.authorized = True
    final = invoke(runtime, resume(forged, {"decision": "确认"}))
    ending = subtask_of(final)
    assert ending["status"] == "SUCCEEDED"
    assert "银行卡操作已完成" in ending["summary"]


class ExhaustingBank(AuthorizationBank):
    """The bank's own counter: two wrong codes stay pending, the third voids the action."""

    def __init__(self):
        super().__init__()
        self.confirm_attempts = 0

    def request(self, method, path, **kwargs):
        decision = (kwargs.get("json") or {}).get("decision")
        if path.endswith("/authorize") and decision == "confirm":
            self.confirm_attempts += 1
            attempt = self.confirm_attempts
            self.authorized = False
            self.register(
                path,
                {
                    "status": "cancelled" if attempt >= 3 else "pending",
                    "verification_passed": False,
                    "verification_failures": attempt if attempt < 3 else 3,
                },
            )
            self.requests.append({"method": method, "path": path, **kwargs})
            return self._responses[path]
        return super().request(method, path, **kwargs)


def exhausting_card(rt, session, monkeypatch):
    import tests.test_card_authorization_api as card_api

    monkeypatch.setattr(card_api, "AuthorizationBank", ExhaustingBank)
    return card_api.paused_card(rt, session)


def wrong_click(client, conversation_id, question_id, *, key=None):
    return decide(
        client,
        conversation_id,
        ACTION_ID,
        {"question_id": question_id, "decision": "confirm", "verification_code": "000000"},
        key=key,
    )


def test_the_third_wrong_code_ends_the_turn_as_a_cancellation(rt, session, monkeypatch):
    client, conversation, question, bank, model, _ = exhausting_card(rt, session, monkeypatch)
    conversation_id = conversation["conversation_id"]
    for _ in range(2):
        refused = wrong_click(client, conversation_id, question["question_id"])
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "VERIFICATION_FAILED"
    assert bank.confirm_attempts == 2

    ended = wrong_click(client, conversation_id, question["question_id"])
    assert ended.status_code == 200, ended.text
    assert "text/event-stream" in ended.headers["Content-Type"]
    assert bank.confirm_attempts == 3
    # The bank's cancellation is consumed as one: cancel at the bank, execute never.
    assert len(cancels(bank)) == 1
    assert not any(request["path"].endswith("/execute") for request in bank.requests)
    assert CANCEL_SUMMARY in ended.text
    detail = client.get(f"/api/conversations/{conversation_id}").json()["data"]
    assert detail["pending_interrupt"] is None

    before = len(bank.requests)
    stale = wrong_click(client, conversation_id, question["question_id"])
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert len(bank.requests) == before


def test_a_replayed_cancelling_click_is_answered_with_the_first_receipt(rt, session, monkeypatch):
    client, conversation, question, bank, model, _ = exhausting_card(rt, session, monkeypatch)
    conversation_id = conversation["conversation_id"]
    for _ in range(2):
        assert wrong_click(client, conversation_id, question["question_id"]).status_code == 422

    key = "click-third-wrong-code"
    ended = wrong_click(client, conversation_id, question["question_id"], key=key)
    assert ended.status_code == 200, ended.text
    assert len(cancels(bank)) == 1

    before = len(bank.requests)
    prompts = len(model.prompts)
    replay = wrong_click(client, conversation_id, question["question_id"], key=key)
    assert replay.status_code == 200, replay.text
    assert replay.headers["Content-Type"].startswith("application/json")
    assert replay.json()["data"]["duplicate"] is True
    # A replay buys nothing a second time: no bank call, no model round, no new authorize.
    assert len(bank.requests) == before
    assert len(model.prompts) == prompts
    assert bank.confirm_attempts == 3
