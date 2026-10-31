"""The assembler is wired in, and the degraded paths still behave."""

from sqlalchemy import select

from app.audit.model_client import AuditedModelClient
from app.context.budget import Budget
from app.storage.models import AgentRunEvent, ContextSummary, LlmCallAudit
from tests.support.scripted_model import RoutingModel
from tests.test_conversation_api import create, final_text, submit
from tests.test_multi_agent_api import agent_for, client_for

TIGHT = Budget(
    model_context_limit=4000,
    reserved_output_tokens=0,
    safety_margin=0,
    summary_trigger_ratio=0.5,
    recent_message_budget=500,
    summary_max_tokens=100,
)
DECISION = "好"


def wired_agent(rt, *, decisions=8, budget=TIGHT, grants=None):
    """A runtime whose assembler is the production one, on a budget a test can reach.

    `agent_for` builds a bare `AgentRuntime` -- no `AuditedModelClient` -- so the model is
    wrapped here: without the decorator there is no `llm_call_audit` row at all, and the
    "summary calls are tellable apart" test would pass vacuously on an empty table.

    `grants` is passed straight through to `agent_for`: a test that dispatches a domain needs
    the domain to own a tool, or it runs the tool-less shell template and never calls the model.
    """

    agent, model, _recorder = agent_for([DECISION] * decisions, grants=grants)
    agent.set_model(AuditedModelClient(model, rt.audit))
    agent._assembler_factory = lambda user_id, turn, model: rt.build_context_assembler(
        user_id, turn, model, budget=budget
    )
    return agent


def drive(rt, session, agent, *, turns, size=300, client=None):
    client = client or client_for(rt, session, agent)
    created = create(client, "长会话")
    for _ in range(turns):
        assert final_text(submit(client, created["conversation_id"], "问" * size)) == "好"
    return client, created


def test_a_short_turn_never_writes_a_summary(rt, session):
    agent = wired_agent(rt)
    _client, created = drive(rt, session, agent, turns=1, size=10)

    with rt.database.sessions() as db:
        assert (
            db.scalars(
                select(ContextSummary).where(
                    ContextSummary.conversation_id == created["conversation_id"]
                )
            ).all()
            == []
        )


def test_the_turn_context_carries_the_ledger_sequence(rt, session):
    """INV-1 needs the current message's sequence, and only the receipt knows it."""

    seen = []
    agent = wired_agent(rt)
    inner = agent._assembler_factory
    agent._assembler_factory = (
        lambda user_id, turn, model: seen.append(turn) or inner(user_id, turn, model)
    )

    _client, created = drive(rt, session, agent, turns=1, size=10)

    # `admission()` reads the checkpoint before the turn exists, so it builds an assembler
    # with no turn too (`turn=None` is the session shape). The turn that matters is the one
    # the actual invocation carried.
    turns_seen = [item for item in seen if item is not None]
    assert turns_seen, "the assembler factory was called for the conversation turn"
    assert turns_seen[0].sequence == 1
    assert turns_seen[0].conversation_id == created["conversation_id"]


def test_every_assembly_is_audited_as_a_run_event(rt, session):
    agent = wired_agent(rt)
    _client, created = drive(rt, session, agent, turns=1, size=10)

    with rt.database.sessions() as db:
        events = db.scalars(
            select(AgentRunEvent).where(AgentRunEvent.event_type == "CONTEXT_ASSEMBLED")
        ).all()

    assert events, "the assembly is a run fact"
    assert events[0].conversation_id == created["conversation_id"]
    assert events[0].payload_json["reason"] == "within_budget"
    assert events[0].agent_id is None


def test_a_summary_call_is_audited_under_its_own_source(rt, session):
    """A summary call and a decision call must be tellable apart in llm_call_audit.

    Both queries are scoped to this conversation. `llm_call_audit` is shared across tests, so an
    unscoped `select(LlmCallAudit.source)` can be answered by a row some earlier test wrote --
    the test would then pass without this conversation ever having called the model.
    """

    agent = wired_agent(rt, decisions=20)
    _client, created = drive(rt, session, agent, turns=6, size=300)

    with rt.database.sessions() as db:
        sources = set(
            db.scalars(
                select(LlmCallAudit.source).where(
                    LlmCallAudit.conversation_id == created["conversation_id"]
                )
            ).all()
        )

    assert "context_summary" in sources
    assert "conversation" in sources


async def test_the_session_path_without_a_conversation_summarizes_nothing(rt, session):
    """`turn=None` has no ledger; it must fall back to the transcript, not crash."""

    agent = wired_agent(rt)
    with rt.database.sessions() as db:
        before = db.scalars(select(ContextSummary)).all()

    events = [
        event async for event in agent.invoke(session["session_id"], session["user_id"], "你好", {})
    ]

    assert [event.event for event in events] == ["reply_delta", "final"]
    with rt.database.sessions() as db:
        assert db.scalars(select(ContextSummary)).all() == before


def test_the_supervisor_prompt_carries_the_summary_and_the_recent_rounds(rt, session):
    """The two halves of the view both reach the model, in the order the spec fixes."""

    agent = wired_agent(rt, decisions=20)
    model = RoutingModel([DECISION] * 20, summaries=("这是历史摘要",) * 20)
    agent.set_model(model)
    _client, _created = drive(rt, session, agent, turns=6, size=300)

    last = model.decision_prompts[-1]
    assert "这是历史摘要" in last
    # The slot label the prompt template gives the summary half of the view.
    assert "当前对话摘要：" in last
    assert "问" in last, "the recent rounds are still there"
