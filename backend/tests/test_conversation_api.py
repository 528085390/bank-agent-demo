"""The conversation and message API, over HTTP.

What this file pins is the contract a client actually meets: the two response shapes of one
POST and how to tell them apart, the refusal that must happen *before* anything is written,
and the ordering guarantee that a reply the user was shown is a reply they can read back.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.conversation import thread_id_for_conversation
from app.main import create_app
from app.storage.models import ConversationSubmission
from tests.support.domain_harness import field_names, without_fields
from tests.support.scripted_model import ScriptedModel
from tests.test_multi_agent_api import agent_for, error_frame, frames


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def new_key() -> str:
    return f"test-{uuid4().hex}"


def create(client, title=None) -> dict:
    body = {} if title is None else {"title": title}
    response = client.post("/api/conversations", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def submit(client, conversation_id, message, *, key=None, context=None):
    body = {"message": message}
    if context is not None:
        body["client_context"] = context
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=body,
        headers={"Idempotency-Key": key or new_key()},
    )


def messages(client, conversation_id, **params):
    response = client.get(f"/api/conversations/{conversation_id}/messages", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def final_text(response) -> str:
    return next(data["text"] for name, data in frames(response) if name == "final")


# --- lifecycle ---------------------------------------------------------------------------


def test_a_conversation_can_be_created_listed_read_renamed_and_archived(rt, session):
    client = client_for(rt, session)

    created = create(client, "转账")
    assert created["title"] == "转账"
    assert created["message_count"] == 0
    assert "thread_id" not in created

    assert [
        item["conversation_id"] for item in client.get("/api/conversations").json()["data"]["items"]
    ] == [created["conversation_id"]]
    assert (
        client.get(f"/api/conversations/{created['conversation_id']}").json()["data"][
            "conversation"
        ]["title"]
        == "转账"
    )

    renamed = client.patch(
        f"/api/conversations/{created['conversation_id']}", json={"title": "改过的标题"}
    )
    assert renamed.json()["data"]["title"] == "改过的标题"

    archived = client.patch(
        f"/api/conversations/{created['conversation_id']}", json={"archived": True}
    )
    assert archived.json()["data"]["archived"] is True
    # Hidden from the default list, and not writable any more.
    assert client.get("/api/conversations").json()["data"]["items"] == []
    assert submit(client, created["conversation_id"], "还在吗").status_code == 409


def test_the_serial_number_is_returned_and_read_only(rt, session):
    """A client reads the number; it never sends one.

    The number is the service's to assign. Accepting one from a client would let a caller
    pick a number that is already taken, or take a deleted conversation's number -- so the
    request model forbids the field, and an attempt is a 422 rather than a quietly ignored key.
    """

    client = client_for(rt, session)

    first = create(client)
    second = create(client)

    assert first["serial_no"] == 1
    assert second["serial_no"] == 2

    items = client.get("/api/conversations").json()["data"]["items"]
    assert {item["conversation_id"]: item["serial_no"] for item in items} == {
        first["conversation_id"]: 1,
        second["conversation_id"]: 2,
    }

    refused = client.patch(f"/api/conversations/{first['conversation_id']}", json={"serial_no": 9})
    assert refused.status_code == 422

    renamed = client.patch(f"/api/conversations/{first['conversation_id']}", json={"title": "改名"})
    assert renamed.status_code == 200
    assert renamed.json()["data"]["title"] == "改名"
    assert renamed.json()["data"]["serial_no"] == 1

    items = client.get("/api/conversations").json()["data"]["items"]
    assert (
        next(
            item["serial_no"]
            for item in items
            if item["conversation_id"] == first["conversation_id"]
        )
        == 1
    )


def test_a_conversation_can_be_soft_deleted_and_restored(rt, session):
    client = client_for(rt, session)
    created = create(client)

    deleted = client.delete(f"/api/conversations/{created['conversation_id']}")
    assert deleted.json()["data"]["deleted"] is True
    assert client.get("/api/conversations").json()["data"]["items"] == []

    restored = client.post(f"/api/conversations/{created['conversation_id']}/restore")
    assert restored.json()["data"]["deleted"] is False
    assert len(client.get("/api/conversations").json()["data"]["items"]) == 1


def test_bulk_delete_only_touches_the_callers_live_conversations(rt, session):
    """A batch delete is still owner-scoped: unknown, already deleted and other users' ids are skipped."""

    client = client_for(rt, session)
    first = create(client, "第一")
    second = create(client, "第二")
    third = create(client, "留下")
    already = create(client, "已删")
    assert client.delete(f"/api/conversations/{already['conversation_id']}").status_code == 200

    other = client_for(rt, second_session(rt))
    foreign = create(other, "别人的")

    response = client.post(
        "/api/conversations/bulk-delete",
        json={
            "ids": [
                first["conversation_id"],
                second["conversation_id"],
                already["conversation_id"],
                foreign["conversation_id"],
                "conv_missing",
            ]
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["deleted_count"] == 2
    assert set(result["conversation_ids"]) == {
        first["conversation_id"],
        second["conversation_id"],
    }

    live = {
        item["conversation_id"]
        for item in client.get("/api/conversations").json()["data"]["items"]
    }
    assert live == {third["conversation_id"]}
    assert other.get(f"/api/conversations/{foreign['conversation_id']}").status_code == 200

    # A batch delete is the same soft delete as the single-row action, so restore still works.
    restored = client.post(f"/api/conversations/{first['conversation_id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["deleted"] is False


def test_bulk_delete_all_wipes_only_my_live_conversations(rt, session):
    client = client_for(rt, session)
    first = create(client, "一")
    second = create(client, "二")
    already = create(client, "已删")
    assert client.delete(f"/api/conversations/{already['conversation_id']}").status_code == 200

    other = client_for(rt, second_session(rt))
    foreign = create(other, "别人的")

    empty = client.post("/api/conversations/bulk-delete", json={})
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == {"deleted_count": 0, "conversation_ids": []}

    response = client.post("/api/conversations/bulk-delete", json={"all": True})
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["deleted_count"] == 2
    assert set(result["conversation_ids"]) == {
        first["conversation_id"],
        second["conversation_id"],
    }

    assert client.get("/api/conversations").json()["data"]["items"] == []
    assert other.get(f"/api/conversations/{foreign['conversation_id']}").status_code == 200

    # Wiping again is a no-op, not an error.
    again = client.post("/api/conversations/bulk-delete", json={"all": True})
    assert again.status_code == 200, again.text
    assert again.json()["data"] == {"deleted_count": 0, "conversation_ids": []}


def test_the_archived_and_deleted_filters_are_independent(rt, session):
    """Two flags, two states: asking for archived rows must not also reveal deleted ones."""

    client = client_for(rt, session)
    live = create(client, "在用")
    archived = create(client, "归档")
    deleted = create(client, "删除")
    client.patch(f"/api/conversations/{archived['conversation_id']}", json={"archived": True})
    client.delete(f"/api/conversations/{deleted['conversation_id']}")

    def ids(**params):
        return {
            item["conversation_id"]
            for item in client.get("/api/conversations", params=params).json()["data"]["items"]
        }

    assert ids() == {live["conversation_id"]}
    assert ids(archived=True) == {live["conversation_id"], archived["conversation_id"]}
    assert ids(deleted=True) == {live["conversation_id"], deleted["conversation_id"]}
    assert ids(archived=True, deleted=True) == {
        live["conversation_id"],
        archived["conversation_id"],
        deleted["conversation_id"],
    }


def test_another_users_conversation_reads_as_missing(rt, session):
    """Owner isolation over HTTP: a second logged-in user cannot see the first user's row."""

    client = client_for(rt, session)
    created = create(client, "我的会话")
    other = client_for(rt, second_session(rt))

    for response in (
        other.get(f"/api/conversations/{created['conversation_id']}"),
        other.patch(f"/api/conversations/{created['conversation_id']}", json={"title": "偷看"}),
        other.delete(f"/api/conversations/{created['conversation_id']}"),
        other.post(f"/api/conversations/{created['conversation_id']}/restore"),
        other.get(f"/api/conversations/{created['conversation_id']}/messages"),
        submit(other, created["conversation_id"], "你好"),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "NOT_FOUND"

    # Nothing was changed by any of it.
    assert (
        client.get(f"/api/conversations/{created['conversation_id']}").json()["data"][
            "conversation"
        ]["title"]
        == "我的会话"
    )


def second_session(rt) -> dict:
    """A real login session for a second user, so ownership is exercised over HTTP.

    Since P1 the browser credential is the token, not `session_id`: the row has to carry
    `token_hash` and `absolute_expires_at` or `authenticate()` refuses it. Minting it here
    (rather than through `/api/auth/register`) keeps the test to the ownership question —
    registering would also claim a bank customer, which this file is not about.
    """

    from datetime import datetime, timedelta, timezone

    from app.identity.tokens import digest, new_session_token
    from app.storage.models import AppUser, LoginSession

    now = datetime.now(timezone.utc)
    token = new_session_token()
    with rt.database.sessions.begin() as db:
        if not db.scalar(select(AppUser).where(AppUser.user_id == "user_0002")):
            db.add(AppUser(user_id="user_0002"))
            db.flush()
        row = LoginSession(
            session_id=f"sess_other_{uuid4().hex}",
            user_id="user_0002",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=1),
            token_hash=digest(token),
            last_seen_at=now,
        )
        db.add(row)
    return {"session_id": row.session_id, "token": token, "user_id": "user_0002"}


def test_an_unknown_conversation_is_a_404(rt, session):
    client = client_for(rt, session)

    for response in (
        client.get("/api/conversations/conv_missing"),
        client.patch("/api/conversations/conv_missing", json={"title": "x"}),
        client.delete("/api/conversations/conv_missing"),
        client.post("/api/conversations/conv_missing/restore"),
        client.get("/api/conversations/conv_missing/messages"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


# --- the client never names its thread ---------------------------------------------------


def test_creating_a_conversation_with_a_client_supplied_thread_id_is_refused(rt, session):
    """The thread is the server's business: a client that names one is refused, not ignored."""

    client = client_for(rt, session)

    response = client.post("/api/conversations", json={"title": "x", "thread_id": "conv_attacker"})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_PARAMETER"
    assert any(field.endswith("thread_id") for field in error["details"]["fields"])
    # A refused body must not have created anything.
    assert client.get("/api/conversations").json()["data"]["items"] == []


def test_submitting_a_message_with_a_client_supplied_thread_id_is_refused(rt, session):
    """The same guarantee on the message route: refused before anything is written or run."""

    agent, model, _recorder = agent_for(["不该出现"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client, "我的会话")

    response = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"message": "你好", "thread_id": "conv_attacker"},
        headers={"Idempotency-Key": new_key()},
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_PARAMETER"
    assert any(field.endswith("thread_id") for field in error["details"]["fields"])
    assert messages(client, created["conversation_id"])["items"] == []
    with rt.database.sessions() as db:
        submissions = list(
            db.scalars(
                select(ConversationSubmission).where(
                    ConversationSubmission.conversation_id == created["conversation_id"]
                )
            )
        )
    assert submissions == []
    # Unlike the stub-graph case, this runtime's model is the production one, so a turn that
    # actually ran would have recorded a prompt here; an empty script is evidence the refusal
    # happened before the graph was reached.
    assert model.prompts == []


# --- the two response shapes of one POST -------------------------------------------------


def test_a_first_submission_streams_the_six_events(rt, session):
    agent, _model, _recorder = agent_for(["你好呀"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    response = submit(client, created["conversation_id"], "你好")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    assert [name for name, _ in frames(response)] == ["reply_delta", "final"]
    assert final_text(response) == "你好呀"


def test_a_replay_is_a_json_receipt_and_never_runs_the_graph_again(rt, session):
    """The one contract a client cannot guess: read `Content-Type` before choosing a parser."""

    agent, model, _recorder = agent_for(["只回答一次"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    key = new_key()

    first = submit(client, created["conversation_id"], "你好", key=key)
    calls_after_first = len(model.prompts)
    second = submit(client, created["conversation_id"], "你好", key=key)

    assert "text/event-stream" in first.headers["Content-Type"]
    assert second.status_code == 200
    assert "application/json" in second.headers["Content-Type"]
    receipt = second.json()["data"]
    assert set(receipt) == {"submission_id", "message_id", "run_id", "duplicate"}
    assert receipt["duplicate"] is True
    assert receipt["run_id"] is not None, "the first submission did dispatch"
    assert len(model.prompts) == calls_after_first, "a replay must not call the graph again"
    # And the ledger holds exactly one user message and one reply.
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "你好",
        "只回答一次",
    ]


def test_the_same_key_with_a_different_body_is_a_conflict(rt, session):
    agent, _model, _recorder = agent_for(["一次"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    key = new_key()
    submit(client, created["conversation_id"], "第一句", key=key)

    conflict = submit(client, created["conversation_id"], "第二句", key=key)

    assert conflict.status_code == 409
    assert "application/json" in conflict.headers["Content-Type"]
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "第一句",
        "一次",
    ]


@pytest.mark.parametrize("path", ["/api/conversations/{id}/messages", "/api/chat/messages"])
def test_a_submission_without_a_key_is_refused_on_both_routes(rt, session, path):
    """No degraded mode: a server that cannot recognise a retry must refuse the first write."""

    agent, model, _recorder = agent_for(["不该执行"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    if path.startswith("/api/chat"):
        url, body = path, {"session_id": session["session_id"], "message": "你好"}
    else:
        url, body = path.format(id=created["conversation_id"]), {"message": "你好"}

    refused = client.post(url, json=body)

    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert model.prompts == []
    if not path.startswith("/api/chat"):
        assert messages(client, created["conversation_id"])["items"] == []


def test_the_receipt_is_the_same_four_keys_on_the_legacy_route(rt, session):
    agent, _model, _recorder = agent_for(["一次"])
    client = client_for(rt, session)
    rt.agent = agent
    key = new_key()
    body = {"session_id": session["session_id"], "message": "你好"}
    headers = {"Idempotency-Key": key}

    first = client.post("/api/chat/messages", json=body, headers=headers)
    second = client.post("/api/chat/messages", json=body, headers=headers)

    assert "text/event-stream" in first.headers["Content-Type"]
    assert "application/json" in second.headers["Content-Type"]
    assert set(second.json()["data"]) == {"submission_id", "message_id", "run_id", "duplicate"}


# --- the ledger over HTTP ----------------------------------------------------------------


def test_the_ledger_pages_by_sequence(rt, session):
    agent, _model, _recorder = agent_for(
        [
            "回答一",
            # The first turn is named once it has answered, and the naming call takes the next
            # reply from the same script -- the graph's second reply is the one after it.
            "第一轮的概括",
            "回答二",
            "回答三",
        ]
    )
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    for text in ("问题一", "问题二", "问题三"):
        submit(client, created["conversation_id"], text)

    first = messages(client, created["conversation_id"], limit=2)
    assert [item["sequence"] for item in first["items"]] == [1, 2]
    assert [item["role"] for item in first["items"]] == ["user", "assistant"]
    second = messages(client, created["conversation_id"], limit=2, after=first["next_cursor"])
    assert [item["sequence"] for item in second["items"]] == [3, 4]
    assert second["next_cursor"] is not None
    rest = messages(client, created["conversation_id"], limit=2, after=second["next_cursor"])
    assert [item["sequence"] for item in rest["items"]] == [5, 6]
    assert rest["next_cursor"] is None


def test_the_reply_is_stored_with_its_business_ids_and_run(rt, session):
    agent, _model, _recorder = agent_for(["已经完成"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    key = new_key()

    submit(client, created["conversation_id"], "帮我处理", key=key)

    ledger = messages(client, created["conversation_id"])["items"]
    user_message, reply = ledger
    assert reply["role"] == "assistant"
    assert reply["content"] == "已经完成"
    assert reply["run_id"] is not None
    # Both messages of one turn carry the same correlation id.
    assert user_message["run_id"] == reply["run_id"]
    receipt = submit(client, created["conversation_id"], "帮我处理", key=key).json()["data"]
    assert receipt["message_id"] == user_message["message_id"]
    assert receipt["run_id"] == user_message["run_id"]


def test_the_reply_is_in_the_ledger_before_the_final_frame_is_sent(rt, session):
    """A reply the user saw must be a reply they can read back, so the write comes first."""

    agent, _model, _recorder = agent_for(["已经完成"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    response = submit(client, created["conversation_id"], "帮我处理")

    assert final_text(response) == "已经完成"
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "帮我处理",
        "已经完成",
    ]


# --- interrupt resume --------------------------------------------------------------------


def paused(rt, session, replies=None):
    """Park one conversation on a question.

    Five replies, in the order the graph spends them: the Supervisor delegates, the domain
    asks, the resumed domain calls its tool with the answer in hand, the tool round is
    followed by the domain's report (spec §4.8.3), and the Supervisor answers.
    """

    agent, model, _recorder = agent_for(
        replies
        or [
            ("call", "handoff", '{"agent_id":"transfer","instruction":"需要补充信息"}'),
            ("call", "ask_user", '{"fields":["value"],"display":"请提供 value"}'),
            ("call", "echo", '{"value":"from-user"}'),
            "已补充 value",
            "已经完成",
        ],
        grants={"transfer": ["echo"]},
        replies_for={"echo": {"value": "from-user"}},
    )
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    response = submit(client, created["conversation_id"], "帮我处理")
    question = frames(response)[-1][1]
    return client, model, created, question


def test_the_conversation_detail_reports_the_open_question(rt, session):
    client, _model, created, question = paused(rt, session)

    detail = client.get(f"/api/conversations/{created['conversation_id']}").json()["data"]

    assert detail["recovery_required"] is False
    assert detail["pending_interrupt"]["question_id"] == question["question_id"]
    assert field_names(detail["pending_interrupt"]) == ["value"]
    assert without_fields(detail["pending_interrupt"]) == {
        "kind": "input",
        "question_id": question["question_id"],
        "agent_id": "transfer",
        "display": "请提供 value",
        "attempt": 1,
    }


def test_a_resume_answer_is_ledgered_as_an_interrupt_answer(rt, session):
    client, _model, created, question = paused(rt, session)

    resumed = submit(
        client,
        created["conversation_id"],
        "补充信息：value：from-user",
        context={
            "resume": {"question_id": question["question_id"], "value": {"value": "from-user"}}
        },
    )

    assert [name for name, _ in frames(resumed)][-1] == "final"
    ledger = messages(client, created["conversation_id"])["items"]
    answer = ledger[1]
    assert answer["role"] == "user"
    assert answer["message_kind"] == "interrupt_answer"
    assert answer["question_id"] == question["question_id"]
    assert answer["content"] == "补充信息：value：from-user"
    # The answer and the reply that consumed it share one correlation id, and it is not the
    # first turn's: a resume is a new graph call.
    assert ledger[0]["run_id"] != answer["run_id"]
    assert ledger[2]["run_id"] == answer["run_id"]


def test_a_plain_message_while_paused_is_refused_before_anything_is_written(rt, session):
    client, model, created, question = paused(rt, session)
    calls = len(model.prompts)

    refused = submit(client, created["conversation_id"], "在吗")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert refused.json()["error"]["details"]["question_id"] == question["question_id"]
    assert len(model.prompts) == calls
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "帮我处理"
    ]


def test_a_resume_naming_another_question_is_refused_before_anything_is_written(rt, session):
    client, _model, created, _question = paused(rt, session)

    refused = submit(
        client,
        created["conversation_id"],
        "补充",
        context={"resume": {"question_id": "q_from_another_turn", "value": {"value": "x"}}},
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert len(messages(client, created["conversation_id"])["items"]) == 1


def test_a_resume_with_nothing_open_is_refused(rt, session):
    agent, _model, _recorder = agent_for(["没有等待点"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    submit(client, created["conversation_id"], "你好")

    refused = submit(
        client,
        created["conversation_id"],
        "补充",
        context={"resume": {"question_id": "q_1", "value": {"value": "v"}}},
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "INVALID_INTERRUPT_ANSWER"
    assert len(messages(client, created["conversation_id"])["items"]) == 2


def test_the_legacy_resume_shape_is_bound_to_the_open_question(rt, session):
    client, _model, created, question = paused(rt, session)

    resumed = submit(
        client,
        created["conversation_id"],
        "补充",
        context={"resume": {"value": {"value": "from-user"}}},
    )

    assert [name for name, _ in frames(resumed)][-1] == "final"
    answer = messages(client, created["conversation_id"])["items"][1]
    assert answer["question_id"] == question["question_id"]


def test_a_new_goal_is_refused_and_asks_for_a_new_conversation(rt, session):
    agent, model, _recorder = agent_for(["不该执行"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    refused = submit(client, created["conversation_id"], "重新开始", context={"mode": "new_goal"})

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "NEW_CONVERSATION_REQUIRED"
    assert model.prompts == []
    assert messages(client, created["conversation_id"])["items"] == []


def test_an_unnameable_pause_reports_that_recovery_is_required(rt, session):
    """A pause nobody can name is not a question, so nothing may answer it.

    The checkpoint is the only thing that can say this, and saying it is the whole point:
    without it the client would be offered a card with no question id and no way to tell
    whether answering is safe.
    """

    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from app.agents.runtime import AgentRuntime
    from app.agents.state import AgentState

    def nameless(state):
        del state
        # An interrupt with no `question_id`: a pause that cannot be bound to an answer.
        return {"pending_answer": interrupt({"kind": "input", "display": "没有编号的问题"})}

    def graph_factory(model, context, assembler=None, turn=None):
        del model, context
        graph = StateGraph(AgentState)
        graph.add_node("nameless", nameless)
        graph.add_edge(START, "nameless")
        graph.add_edge("nameless", END)
        return graph

    client = client_for(rt, session)
    rt.agent = AgentRuntime(ScriptedModel([]), graph_factory=graph_factory)
    created = create(client)
    submit(client, created["conversation_id"], "你好")

    detail = client.get(f"/api/conversations/{created['conversation_id']}").json()["data"]

    assert detail["pending_interrupt"] is None
    assert detail["recovery_required"] is True
    # And a submission is refused rather than guessed at.
    refused = submit(client, created["conversation_id"], "补充")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EXECUTION_RECOVERY_REQUIRED"
    assert len(messages(client, created["conversation_id"])["items"]) == 1


def test_a_checkpoint_with_work_left_but_no_question_reports_that_recovery_is_required(rt, session):
    """`state.next` non-empty and `state.interrupts` empty is its own branch.

    The unnameable-pause case above is *not* this one: there an interrupt exists and only
    its name is missing. Here there is no pause at all -- the checkpoint says work is
    queued -- so the only safe answer is to refuse and ask for a new conversation.
    """

    from langgraph.graph import END, START, StateGraph

    from app.agents.state import AgentState

    def first(state):
        del state
        return {}

    def second(state):
        del state
        return {}

    def graph_factory(model, context, assembler=None, turn=None):
        del model, context, assembler, turn
        graph = StateGraph(AgentState)
        graph.add_node("first", first)
        graph.add_node("second", second)
        graph.add_edge(START, "first")
        graph.add_edge("first", "second")
        graph.add_edge("second", END)
        return graph

    saver = InMemorySaver()
    model = ScriptedModel([])
    client = client_for(rt, session)
    rt.agent = AgentRuntime(model, graph_factory=graph_factory, checkpointer=saver)
    created = create(client)

    # The runtime does not expose its compiled graph, so the checkpoint is written by hand
    # over the same saver and the same thread the runtime will read.
    graph = graph_factory(None, None).compile(checkpointer=saver)
    config = {
        "configurable": {"thread_id": thread_id_for_conversation(created["conversation_id"])},
        "recursion_limit": 50,
    }
    asyncio.run(graph.aupdate_state(config, {}, as_node="first"))

    detail = client.get(f"/api/conversations/{created['conversation_id']}").json()["data"]

    assert detail["pending_interrupt"] is None
    assert detail["recovery_required"] is True
    # And a submission is refused rather than replayed: nothing ran, so nothing is written.
    refused = submit(client, created["conversation_id"], "补充")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EXECUTION_RECOVERY_REQUIRED"
    assert messages(client, created["conversation_id"])["items"] == []


def test_a_failed_persistence_does_not_publish_a_final(rt, session, monkeypatch):
    """The reply frame is gated on the ledger write, so a storage failure cannot show it."""

    agent, _model, _recorder = agent_for(["不该出现"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    def explode(*args, **kwargs):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("select 1", {}, Exception("down"))

    monkeypatch.setattr(rt.conversations, "append_assistant_message", explode)
    response = submit(client, created["conversation_id"], "帮我处理")

    assert "event: final" not in response.text
    assert "PERSISTENCE_FAILED" in response.text
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "帮我处理"
    ]


def test_a_crash_between_the_write_and_the_dispatch_never_replays(rt, session, monkeypatch):
    """The delivery limit this phase accepts, pinned rather than assumed.

    The database commit and the graph call are not one atomic step. If the process dies in
    between, the user's message survives, no call was made, and the same key returns the
    receipt -- it does not quietly run the turn. The user reads the ledger, sees no reply,
    and continues in a new conversation. A hidden retry here would be the exact thing the
    spec forbids, so the absence of one is what this asserts.
    """

    agent, model, _recorder = agent_for(["不该出现"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)
    key = new_key()

    from sqlalchemy.exc import OperationalError

    def crash(*args, **kwargs):
        raise OperationalError("update", {}, Exception("the process died here"))

    monkeypatch.setattr(rt.conversations, "attach_run", crash)
    first = submit(client, created["conversation_id"], "转账 100 元", key=key)

    # The stream had already started by the time the attach failed, so this is an error frame
    # rather than a JSON error -- the same shape every other in-turn failure takes. The client
    # reads the code from it either way.
    assert first.status_code == 200
    assert error_frame(first)["code"] == "PERSISTENCE_FAILED"
    assert error_frame(first)["retryable"] is True
    assert model.prompts == [], "no graph call may happen without a run id"
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "转账 100 元"
    ]

    # The same key again, with the storage working. It answers with the receipt and stops:
    # the turn is not re-dispatched, because a spent key is not a queue.
    replay = submit(client, created["conversation_id"], "转账 100 元", key=key)

    assert "application/json" in replay.headers["Content-Type"]
    receipt = replay.json()["data"]
    assert receipt["duplicate"] is True
    assert receipt["run_id"] is None, "the run id was never attached, and is not invented now"
    assert model.prompts == [], "a replay must not run the turn"
    assert [item["content"] for item in messages(client, created["conversation_id"])["items"]] == [
        "转账 100 元"
    ]


def test_two_conversations_do_not_share_a_checkpoint(rt, session):
    agent, model, _recorder = agent_for(["甲的回答", "乙的回答"])
    client = client_for(rt, session)
    rt.agent = agent
    first = create(client, "甲")
    second = create(client, "乙")

    assert final_text(submit(client, first["conversation_id"], "甲的问题")) == "甲的回答"
    assert final_text(submit(client, second["conversation_id"], "乙的问题")) == "乙的回答"

    # The second conversation's prompt must not carry the first one's transcript. That holds
    # because each conversation has its own `thread_id`, so the two checkpoints -- and their
    # `messages` channels -- are separate namespaces; the prompt's transcript is rendered from
    # that checkpoint state (`describe_transcript(message_dicts(state.get("messages")))`,
    # `app/agents/supervisor/graph.py:113`).
    #
    # This file does not exercise the ledger read path. `agent_for`
    # (`tests/test_multi_agent_api.py:57`) installs no `assembler_factory`, so
    # `app/agents/runtime.py:177-181` computes `assembler = None` and
    # `app/agents/supervisor/graph.py:112` leaves `view is None` -- `prompt_for` then falls back to the
    # checkpoint above. Do not read this assertion as evidence about `read_ledger`
    # (`app/context/ledger.py`): dropping its `conversation_id` filter does not turn this red.
    # The ledger path is covered by `tests/test_conversation_audit.py` and the
    # `tests/test_context_*` series instead.
    assert "甲的问题" not in model.prompts[1]
    assert "甲的回答" not in model.prompts[1]


def test_one_conversation_reuses_its_own_thread(rt, session):
    agent, model, _recorder = agent_for(["第一轮回答", "第二轮回答"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    submit(client, created["conversation_id"], "第一轮")
    submit(client, created["conversation_id"], "第二轮")

    # The same mechanism seen from the other side: both turns go to one conversation, so both use
    # the same `thread_id` and therefore the same checkpoint. The second turn's prompt carries the
    # first round's history because `new_agent_state` (`app/agents/state.py:164`) seeds
    # `{"messages": [turn] if message else []}` and LangGraph's message reducer merges that user
    # message into the messages the checkpoint already holds. As in the test above, this is the
    # checkpoint and not the ledger: this file's runtime has no assembler, so `read_ledger`
    # (`app/context/ledger.py`) is never consulted here.
    #
    # Naming a conversation no longer costs a model call (`app/conversation/titling.py` derives
    # the title from the user's own words), so every prompt in `prompts` is a graph prompt and
    # index 1 is the second turn.
    assert "第一轮" in model.prompts[1] and "第一轮回答" in model.prompts[1]


def test_a_resume_answer_appears_once_in_the_transcript(rt, session):
    """The ledger id is what makes this true: the reducer merges by id, so a replay is one row."""

    client, model, created, question = paused(rt, session)
    submit(
        client,
        created["conversation_id"],
        "补充信息：value：from-user",
        context={
            "resume": {"question_id": question["question_id"], "value": {"value": "from-user"}}
        },
    )

    prompts = "\n".join(model.prompts)
    assert prompts.count("补充信息：value：from-user") == 1


def test_the_stream_never_names_a_thread_or_the_caller(rt, session):
    agent, _model, _recorder = agent_for(["好的"])
    client = client_for(rt, session)
    rt.agent = agent
    created = create(client)

    text = submit(client, created["conversation_id"], "你好").text

    assert "thread_id" not in text
    assert "conv:" not in text
    assert session["session_id"] not in text
    assert session["user_id"] not in text
    assert created["conversation_id"] not in text
