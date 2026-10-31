"""One turn, one automatic title: where the name comes from, when it is written, and what it costs.

A conversation is named from the user's own first words, and only then. The name is derived
locally from that text (`titling.derive_title`) -- no model call, no timeout, nothing to be
exempted from the audit rule -- so the interesting cases are the ones about *the row*: the reply
is final first, the title is still the default, exactly one user request exists, and naming is
attempted once. The last four cases drive the service directly, because a rename or a second
message that lands between the candidate read and the conditional write is a race no HTTP request
can be made to lose on demand.
"""

import asyncio
from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit.context import AuditContext, audit_context
from app.audit.model_client import AuditedModelClient
from app.audit.service import RunEventType
from app.conversation.service import (
    ACTION_RENAME,
    DEFAULT_TITLE,
    MAX_TITLE_LENGTH,
    ConversationService,
    request_hash,
)
from app.conversation.titling import derive_title, is_usable_title
from app.conversation.turns import DeferredNaming
from app.core.types import new_id, utcnow
from app.main import create_app
from app.storage.models import (
    AgentRunEvent,
    BusinessAuditEvent,
    ConversationMessage,
    LlmCallAudit,
)
from tests.support.scripted_model import ScriptedModel
from tests.test_multi_agent_api import agent_for, frames

REPLY = "好的"
FIRST_MESSAGE = "我要转账给张三"
# Short enough that the derivation keeps it whole -- see the first section for the cut rules.
FIRST_TITLE = FIRST_MESSAGE


class RecordingModel(ScriptedModel):
    """A scripted model that keeps every call, so "naming spent no call" is observable.

    The script is deliberately one reply long: a second call would find it empty and raise, which
    is exactly how a naming call that went back to the model would show up here.
    """

    def __init__(self, replies: list[str]) -> None:
        super().__init__(replies)
        self.calls: list[tuple[dict, ...]] = []
        self.on_call: Callable[[int], None] | None = None

    def complete(self, messages: tuple[dict, ...]):
        self.calls.append(tuple(messages))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        return super().complete(messages)

    def stream(self, messages: tuple[dict, ...], *, tools=()):
        self.calls.append(tuple(messages))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        yield from super().stream(messages, tools=tools)


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def new_key() -> str:
    return f"auto-title-{uuid4().hex}"


def use_recording_model(rt, replies) -> RecordingModel:
    """Install a recording model behind the audit boundary, as the settings panel would."""

    model = RecordingModel(list(replies))
    rt.agent.set_model(AuditedModelClient(model, rt.audit))
    return model


def create(client, title=None) -> dict:
    body = {} if title is None else {"title": title}
    response = client.post("/api/conversations", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def submit(client, conversation_id, message, *, key=None):
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"message": message},
        headers={"Idempotency-Key": key or new_key()},
    )


def conversation(client, conversation_id) -> dict:
    """The conversation itself, not the wrapper the detail endpoint returns it in."""

    response = client.get(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]["conversation"]


def final_text(response) -> str:
    return next(data["text"] for name, data in frames(response) if name == "final")


def event_types(rt) -> list[str]:
    with rt.database.sessions() as db:
        rows = db.scalars(select(AgentRunEvent).order_by(AgentRunEvent.created_at))
        return [row.event_type for row in rows]


def llm_audits(rt, conversation_id) -> list[LlmCallAudit]:
    with rt.database.sessions() as db:
        rows = db.scalars(
            select(LlmCallAudit)
            .where(LlmCallAudit.conversation_id == conversation_id)
            .order_by(LlmCallAudit.created_at)
        )
        return list(rows)


def business_actions(rt, conversation_id) -> list[str]:
    with rt.database.sessions() as db:
        rows = db.scalars(
            select(BusinessAuditEvent)
            .where(BusinessAuditEvent.conversation_id == conversation_id)
            .order_by(BusinessAuditEvent.created_at)
        )
        return [row.action for row in rows]


def service_for(rt) -> ConversationService:
    return ConversationService(rt.database, audit=rt.audit)


def as_user(user_id: str, conversation_id: str):
    """The context the route establishes, which a business audit row insists on."""

    return audit_context(AuditContext(user_id=user_id, conversation_id=conversation_id))


def append_request(service, user_id, conversation_id, content):
    return service.append_user_message(
        user_id,
        conversation_id,
        idempotency_key=new_key(),
        expected_hash=request_hash(conversation_id, content, None),
        content=content,
    )


def raw_user_message(rt, conversation_id, *, sequence, content, message_kind, question_id=None):
    with rt.database.sessions.begin() as db:
        db.add(
            ConversationMessage(
                message_id=new_id("msg"),
                conversation_id=conversation_id,
                sequence=sequence,
                role="user",
                content=content,
                message_kind=message_kind,
                question_id=question_id,
                created_at=utcnow(),
                meta={},
            )
        )


# --- where the name comes from -------------------------------------------------------------


def test_a_short_first_message_is_the_title_unchanged():
    assert derive_title("我要转账给张三") == "我要转账给张三"


def test_the_title_is_one_line_even_when_the_message_is_not():
    assert derive_title("帮我\n看看\t这张账单") == "帮我 看看 这张账单"


def test_a_long_first_message_is_kept_whole():
    """The name is the sentence, not a shorter version of it."""

    text = "我想知道我这个月到底花在了什么地方，能不能按类别给我排个序"

    assert derive_title(text) == text


def test_a_comma_nowhere_near_the_limit_does_not_shorten_the_name():
    text = "你好，我想问一下我这个月的信用卡账单在哪里能看到明细，最好能按类别分开"

    assert derive_title(text) == text


def test_only_the_row_limit_cuts_a_message():
    text = "好" * 500
    title = derive_title(text)

    assert title is not None
    assert len(title) == MAX_TITLE_LENGTH
    assert title.endswith("…")


def test_nothing_to_name_is_not_a_name():
    assert derive_title(None) is None
    assert derive_title("") is None
    assert derive_title("   \n\t ") is None


def test_the_default_title_is_not_written_back():
    assert is_usable_title(DEFAULT_TITLE) is False
    assert is_usable_title(None) is False
    assert is_usable_title("我要转账") is True


# --- the first turn ------------------------------------------------------------------------


def test_the_first_turn_names_the_conversation(rt, session):
    client = client_for(rt, session)
    created = create(client)
    assert created["title"] == DEFAULT_TITLE
    model = use_recording_model(rt, [REPLY])

    response = submit(client, created["conversation_id"], FIRST_MESSAGE)

    assert response.status_code == 200, response.text
    assert final_text(response) == "好的"
    assert conversation(client, created["conversation_id"])["title"] == FIRST_TITLE
    # One call, the graph's own: naming costs the provider nothing now. A second call would have
    # found the script empty and raised, so the title above would still be the default.
    assert len(model.calls) == 1


def test_a_renamed_conversation_is_left_alone(rt, session):
    client = client_for(rt, session)
    created = create(client, "我自己的名字")
    model = use_recording_model(rt, [REPLY])

    response = submit(client, created["conversation_id"], FIRST_MESSAGE)

    assert final_text(response) == "好的"
    assert conversation(client, created["conversation_id"])["title"] == "我自己的名字"
    assert len(model.calls) == 1


def test_a_second_turn_does_not_rename_the_conversation(rt, session):
    client = client_for(rt, session)
    created = create(client)
    use_recording_model(rt, [REPLY])
    submit(client, created["conversation_id"], FIRST_MESSAGE)
    assert conversation(client, created["conversation_id"])["title"] == FIRST_TITLE

    model = use_recording_model(rt, [REPLY])
    response = submit(client, created["conversation_id"], "再转一笔")

    assert final_text(response) == "好的"
    assert conversation(client, created["conversation_id"])["title"] == FIRST_TITLE
    assert len(model.calls) == 1


def test_a_first_turn_that_never_answered_is_not_named(rt, session):
    """Naming is bound to the answer, not to the request.

    A turn that paused for input has a user message and no reply, so the turn never reached
    `final` and never registers a naming attempt. The title is what proves it: it is still the
    default even though the first message alone would have been a perfectly good name.
    """

    agent, model, _recorder = agent_for(
        [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
        ],
        grants={"transfer": ["echo"]},
    )
    rt.agent = agent
    client = client_for(rt, session)
    created = create(client)

    response = submit(client, created["conversation_id"], FIRST_MESSAGE)

    assert [name for name, _ in frames(response)] == [
        "plan",
        "task_update",
        "agent_start",
        "task_update",
        "interrupt",
    ]
    assert conversation(client, created["conversation_id"])["title"] == DEFAULT_TITLE
    # Both scripted replies were spent by the graph; nothing was asked on top of them.
    # `ScriptedModel` records prompts, not call objects: two prompts is two graph turns.
    assert len(model.prompts) == 2


# --- what naming costs ---------------------------------------------------------------------


def test_a_naming_failure_leaves_the_turn_successful(rt, session, monkeypatch):
    """Naming is an extra, never a dependency (design spec S-2.6).

    The derivation is made to raise, which is the shape of any failure in that step -- a bad
    string, a bug in the cut rules. The turn has already stored and published its answer, so the
    only acceptable outcome is a warning and an unnamed conversation.
    """

    def boom(_text):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr("app.conversation.turns.derive_title", boom)
    client = client_for(rt, session)
    created = create(client)
    use_recording_model(rt, [REPLY])

    response = submit(client, created["conversation_id"], FIRST_MESSAGE)

    assert response.status_code == 200, response.text
    assert final_text(response) == "好的"
    assert conversation(client, created["conversation_id"])["title"] == DEFAULT_TITLE
    recorded = event_types(rt)
    assert RunEventType.RUN_SUCCEEDED.value in recorded
    assert RunEventType.RUN_FAILED.value not in recorded


def test_a_client_that_leaves_after_the_answer_does_not_fail_the_run(rt, session):
    """The reply was delivered, so the turn succeeded -- the label that follows is not the run.

    The generator is abandoned at the `final` frame, which is what a client disconnecting
    looks like, and the turn must not be recorded as failed (design spec S-2.6): the answer
    is stored and published. Only the naming step, which has not even started, is lost.
    """

    client = client_for(rt, session)
    created = create(client)
    use_recording_model(rt, [REPLY])
    turns = rt.turns
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            created["conversation_id"],
            idempotency_key=new_key(),
            message=FIRST_MESSAGE,
            client_context=None,
        )
    )

    async def leave_at_the_answer():
        source = turns.stream(session["user_id"], session["session_id"], plan)
        async for event in source:
            if event.event == "final":
                break
        await source.aclose()

    asyncio.run(leave_at_the_answer())

    assert conversation(client, created["conversation_id"])["title"] == DEFAULT_TITLE
    kinds = event_types(rt)
    assert RunEventType.RUN_FAILED.value not in kinds
    assert kinds.count(RunEventType.RUN_SUCCEEDED.value) == 1


def test_naming_adds_no_llm_call_to_the_audit_trail(rt, session):
    client = client_for(rt, session)
    created = create(client)
    use_recording_model(rt, [REPLY])

    submit(client, created["conversation_id"], FIRST_MESSAGE)

    # The title moved, and the only audited model call is still the graph's own: naming is local
    # arithmetic on a string, so there is nothing to audit and nothing to exempt.
    assert conversation(client, created["conversation_id"])["title"] == FIRST_TITLE
    assert len(llm_audits(rt, created["conversation_id"])) == 1


def test_a_successful_naming_is_business_audited(rt, session):
    client = client_for(rt, session)
    created = create(client)
    use_recording_model(rt, [REPLY])

    submit(client, created["conversation_id"], FIRST_MESSAGE)

    assert business_actions(rt, created["conversation_id"]) == [ACTION_RENAME]


# --- the conditional write -----------------------------------------------------------------


def test_a_rename_in_flight_wins(rt, session):
    service = service_for(rt)
    user_id = session["user_id"]
    created = service.create(user_id)
    conversation_id = created.conversation_id
    append_request(service, user_id, conversation_id, FIRST_MESSAGE)
    assert service.auto_title_candidate(user_id, conversation_id) == FIRST_MESSAGE

    with as_user(user_id, conversation_id):
        service.update(user_id, conversation_id, title="我的名字")
        assert service.apply_auto_title(user_id, conversation_id, FIRST_TITLE) is False

    assert service.get(user_id, conversation_id).title == "我的名字"


def test_a_second_message_in_flight_wins(rt, session):
    service = service_for(rt)
    user_id = session["user_id"]
    created = service.create(user_id)
    conversation_id = created.conversation_id
    append_request(service, user_id, conversation_id, FIRST_MESSAGE)
    assert service.auto_title_candidate(user_id, conversation_id) == FIRST_MESSAGE
    append_request(service, user_id, conversation_id, "再转一笔")

    with as_user(user_id, conversation_id):
        assert service.apply_auto_title(user_id, conversation_id, FIRST_TITLE) is False

    assert service.get(user_id, conversation_id).title == DEFAULT_TITLE


def test_a_deleted_conversation_can_still_be_named(rt, session):
    service = service_for(rt)
    user_id = session["user_id"]
    created = service.create(user_id)
    conversation_id = created.conversation_id
    append_request(service, user_id, conversation_id, FIRST_MESSAGE)
    assert service.auto_title_candidate(user_id, conversation_id) == FIRST_MESSAGE

    with as_user(user_id, conversation_id):
        service.soft_delete(user_id, conversation_id)
        assert service.apply_auto_title(user_id, conversation_id, FIRST_TITLE) is True

    view = service.get(user_id, conversation_id)
    assert view.title == FIRST_TITLE
    assert view.serial_no == created.serial_no


def test_an_interrupted_first_turn_is_still_named(rt, session):
    service = service_for(rt)
    user_id = session["user_id"]
    created = service.create(user_id)
    conversation_id = created.conversation_id
    raw_user_message(rt, conversation_id, sequence=1, content=FIRST_MESSAGE, message_kind="message")
    raw_user_message(
        rt,
        conversation_id,
        sequence=2,
        content="转 100",
        message_kind="interrupt_answer",
        question_id="q_0001",
    )

    assert service.auto_title_candidate(user_id, conversation_id) == FIRST_MESSAGE
    with as_user(user_id, conversation_id):
        assert service.apply_auto_title(user_id, conversation_id, FIRST_TITLE) is True

    assert service.get(user_id, conversation_id).title == FIRST_TITLE


# --- naming as the response's background work ----------------------------------------------


def test_the_stream_ends_before_the_conversation_is_named(rt, session):
    """Naming is the response's background work, not a part of the stream (spec S-2.1, m00787).

    The turn decides *whether* to name -- it reached `final` -- and the stream is over by then;
    the title has not moved when the last frame is handed over.
    """

    client = client_for(rt, session)
    created = create(client)
    conversation_id = created["conversation_id"]
    use_recording_model(rt, [REPLY])
    turns = rt.turns
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            conversation_id,
            idempotency_key=new_key(),
            message=FIRST_MESSAGE,
            client_context=None,
        )
    )
    naming = DeferredNaming(turns)

    async def drive():
        return [
            event
            async for event in turns.stream(
                session["user_id"], session["session_id"], plan, naming=naming
            )
        ]

    events = asyncio.run(drive())

    assert events[-1].event == "final"
    # Registered, not made: the title is still the default when the stream ends.
    assert conversation(client, conversation_id)["title"] == DEFAULT_TITLE

    asyncio.run(naming.run())

    assert conversation(client, conversation_id)["title"] == FIRST_TITLE


def test_the_lock_is_free_once_the_response_is_done(rt, session):
    """The route releases the lock when the stream ends, naming or not (spec S-2.1).

    Naming runs as the response's background work, so a route that held the lock until the
    background work finished would still be holding it here -- `TestClient` does not return until
    that work is done (starlette/testclient.py:349-351). A bounded acquire, so a wedged lock fails
    here instead of hanging the suite.
    """

    client = client_for(rt, session)
    created = create(client)
    conversation_id = created["conversation_id"]
    use_recording_model(rt, [REPLY])

    response = submit(client, conversation_id, FIRST_MESSAGE)

    assert response.status_code == 200, response.text
    assert conversation(client, conversation_id)["title"] == FIRST_TITLE

    async def take_it_again():
        again = await asyncio.wait_for(rt.turns.acquire(conversation_id), timeout=1.0)
        again.release()

    asyncio.run(take_it_again())


def test_running_the_deferred_naming_twice_names_once(rt, session, monkeypatch):
    """`run()` takes the pending attempt away before using it, so a second call is a no-op.

    Starlette runs the background work once, but the handle is what owns "one attempt per turn".
    Counting the attempts directly is the only honest probe now that the attempt itself always
    succeeds: after the first `run()` the conversation is named, and a second attempt would find
    no candidate either way.
    """

    client = client_for(rt, session)
    created = create(client)
    conversation_id = created["conversation_id"]
    use_recording_model(rt, [REPLY])
    turns = rt.turns
    attempts: list[str] = []
    original = turns._auto_title

    async def counting(user_id, plan):
        attempts.append(plan.conversation_id)
        await original(user_id, plan)

    monkeypatch.setattr(turns, "_auto_title", counting)
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            conversation_id,
            idempotency_key=new_key(),
            message=FIRST_MESSAGE,
            client_context=None,
        )
    )
    naming = DeferredNaming(turns)

    async def drive():
        async for _ in turns.stream(session["user_id"], session["session_id"], plan, naming=naming):
            pass
        await naming.run()
        await naming.run()

    asyncio.run(drive())

    assert attempts == [conversation_id]
    assert conversation(client, conversation_id)["title"] == FIRST_TITLE


def test_the_deferred_naming_writes_the_same_facts(rt, session):
    """The background task re-enters the turn's audit context, or the business row is lost.

    `apply_auto_title` writes `conversation.rename`, and a business row without a user id
    raises instead of being written (audit/service.py:261-263) -- so the title moving *and* the
    row existing are the two halves of the same fact (spec S-2.1, A-17).
    """

    client = client_for(rt, session)
    created = create(client)
    conversation_id = created["conversation_id"]
    use_recording_model(rt, [REPLY])
    turns = rt.turns
    plan = asyncio.run(
        turns.prepare(
            session["user_id"],
            session["session_id"],
            conversation_id,
            idempotency_key=new_key(),
            message=FIRST_MESSAGE,
            client_context=None,
        )
    )
    naming = DeferredNaming(turns)

    async def drive():
        async for _ in turns.stream(session["user_id"], session["session_id"], plan, naming=naming):
            pass
        await naming.run()

    asyncio.run(drive())

    assert conversation(client, conversation_id)["title"] == FIRST_TITLE
    assert business_actions(rt, conversation_id) == [ACTION_RENAME]
    assert len(llm_audits(rt, conversation_id)) == 1
