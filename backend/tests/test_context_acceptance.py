"""Spec §13, one test per acceptance clause.

These are the clauses a reviewer would otherwise have to take on trust: that compression
never touches the ledger, that a summary call is auditable and invisible, that the current
message is never summarized away, and that the two failure reasons are told apart.
"""

from sqlalchemy import select

from app.agents.prompts import DOMAIN_PROMPT, SUMMARY_PROMPT, render
from app.audit.model_client import AuditedModelClient
from app.context.budget import chunk_for_summary
from app.context.estimator import estimate_message_tokens, estimate_tokens
from app.context.ledger import read_ledger
from app.context.summary import (
    NO_PREVIOUS_SUMMARY,
    SUMMARY_USER_TEMPLATE,
    _describe_ledger,
)
from app.core.errors import (
    CONTEXT_LIMIT_REASON_OVER_BUDGET,
    CONTEXT_LIMIT_REASON_SUMMARY_FAILED,
    context_limit_error,
)
from app.integration.model_client import ModelClientError
from app.storage.models import AgentRunEvent, ContextSummary, ConversationMessage, LlmCallAudit
from tests.support.scripted_model import RoutingModel
from tests.test_context_integration import DECISION, TIGHT, wired_agent
from tests.test_conversation_api import create, final_text, messages, submit
from tests.test_multi_agent_api import client_for

# How many turns the helpers below are willing to spend looking for a trigger. The trigger is
# an estimate over the supervisor prompt's own size, so no test hardcodes *when* it fires --
# they drive until the database says it did, and assert on what they found.
SIZE = 600
LIMIT = 14


def message_count(rt, conversation_id):
    with rt.database.sessions() as db:
        return len(
            db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation_id
                )
            ).all()
        )


def row_fingerprint(rt, conversation_id):
    """Every ledger row this conversation has, by identity and content.

    `message_count` counts rows; this is the other half of spec §13/INV-4, which also says the
    rows that were already there must survive a summarizing turn unchanged. A summarizer that
    rewrote a row in place would keep the count and still change this.
    """

    with rt.database.sessions() as db:
        return sorted(
            (row.sequence, row.role, row.message_kind, row.content)
            for row in db.scalars(
                select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation_id
                )
            ).all()
        )


def newest_message(rt, conversation_id):
    with rt.database.sessions() as db:
        return db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence.desc())
        ).first()


def newest_summary(rt, conversation_id):
    with rt.database.sessions() as db:
        return db.scalars(
            select(ContextSummary)
            .where(ContextSummary.conversation_id == conversation_id)
            .order_by(ContextSummary.version.desc())
        ).first()


def versions(rt, conversation_id):
    with rt.database.sessions() as db:
        return list(
            db.scalars(
                select(ContextSummary)
                .where(ContextSummary.conversation_id == conversation_id)
                .order_by(ContextSummary.version.asc())
            )
        )


def assemblies(rt, conversation_id):
    """Every `CONTEXT_ASSEMBLED` row this conversation wrote, oldest first."""

    with rt.database.sessions() as db:
        return list(
            db.scalars(
                select(AgentRunEvent)
                .where(
                    AgentRunEvent.conversation_id == conversation_id,
                    AgentRunEvent.event_type == "CONTEXT_ASSEMBLED",
                )
                .order_by(AgentRunEvent.created_at)
            )
        )


def a_wired_agent(rt, *, decisions=60, script=None, audited=False, grants=None):
    """A wired agent whose model the test can inspect.

    `wired_agent` wraps its own model in the auditing decorator, and that model is not
    returned. Replacing it here is deliberate: these tests assert on what the model *saw*.
    `audited=True` keeps the decorator around the routing model instead of dropping it, for
    the one test that reads `llm_call_audit`: without the decorator there is no row at all,
    and with the stock `ScriptedModel` underneath, every summary call would be answered with
    a supervisor decision -- no version could ever be committed, so the driver below would
    never return. `script` is for the one test that has to dispatch a domain agent; the
    default script only ever answers as the supervisor. `grants` reaches `agent_for` for that
    same dispatching test: the domain it hands off to must own a tool, or it runs the tool-less
    shell template and never reaches the model.
    """

    replies = [DECISION] * decisions if script is None else list(script)
    agent = wired_agent(rt, decisions=len(replies), grants=grants)
    model = RoutingModel(replies)
    agent.set_model(AuditedModelClient(model, rt.audit) if audited else model)
    return agent, model


def drive_until_summarized(rt, session, agent, model, *, size=SIZE, limit=LIMIT):
    """Drive until a version exists; return the client, the conversation and the turn count."""

    client = client_for(rt, session, agent)
    created = create(client, "长会话")
    for turn in range(1, limit + 1):
        assert final_text(submit(client, created["conversation_id"], "问" * size)) == "好"
        if newest_summary(rt, created["conversation_id"]) is not None:
            # A committed version is the model's own evidence that a summary call happened.
            # Asserting it here keeps every `for prompt in model.summary_prompts` below from
            # passing vacuously on an empty list.
            assert model.summary_prompts, "a committed version means a summary call"
            return client, created, turn
    raise AssertionError("the conversation never grew past the summary trigger")


def drive_until_a_new_version(rt, session, agent, client, created, *, size=SIZE, limit=LIMIT):
    """Drive until the newest version number grows; return that version and the turns taken."""

    before = newest_summary(rt, created["conversation_id"]).version
    for turn in range(1, limit + 1):
        assert final_text(submit(client, created["conversation_id"], "问" * size)) == "好"
        after = newest_summary(rt, created["conversation_id"])
        if after.version > before:
            return after, turn
    raise AssertionError("no further turn ever summarized again")


def drive_turns(rt, session, agent, *, turns, size=300):
    """Drive a fixed number of turns and return the client and the conversation.

    For the tests whose point is that something *never* happens: they cannot wait for a
    version to appear, so they drive a fixed count and then assert that the call really was
    attempted. `size=300` keeps the uncompressed view under `TIGHT.input_budget` for six
    turns -- a failing summary never shrinks anything, so the count has to stay small enough
    that the turn does not become an `over_budget` failure instead.
    """

    client = client_for(rt, session, agent)
    created = create(client, "长会话")
    for _ in range(turns):
        assert final_text(submit(client, created["conversation_id"], "问" * size)) == "好"
    return client, created


def test_inv1_a_message_added_after_a_summary_is_in_the_view(rt, session):
    """The user's newest message must reach the model in full, never through a summary."""

    agent, model = a_wired_agent(rt)
    client, created, _turns = drive_until_summarized(rt, session, agent, model)
    assert newest_summary(rt, created["conversation_id"]) is not None, "a summary was written"

    submit(client, created["conversation_id"], "独一无二的新消息")

    assert "独一无二的新消息" in model.decision_prompts[-1]
    summary = newest_summary(rt, created["conversation_id"])
    assert summary.covered_to_sequence < newest_message(rt, created["conversation_id"]).sequence


def test_inv2_summaries_chain_without_gaps_or_overlap(rt, session):
    agent, model = a_wired_agent(rt)
    client, created, _turns = drive_until_summarized(rt, session, agent, model)
    for _ in range(2):
        drive_until_a_new_version(rt, session, agent, client, created)

    rows = versions(rt, created["conversation_id"])

    assert len(rows) >= 3, "the conversation was long enough for three versions"
    assert rows[0].version == 1
    assert rows[0].covered_from_sequence == 1
    assert rows[0].base_summary_id is None, "the first version is the root of the chain"
    for previous, current in zip(rows, rows[1:]):
        assert current.version == previous.version + 1
        assert current.covered_from_sequence == previous.covered_to_sequence + 1
        assert current.base_summary_id == previous.summary_id


def test_inv3_the_retained_range_is_contiguous_and_covers_the_current_message(rt, session):
    agent, model = a_wired_agent(rt)
    _client, created, _turns = drive_until_summarized(rt, session, agent, model)
    summary = newest_summary(rt, created["conversation_id"])
    newest = newest_message(rt, created["conversation_id"])

    retained = read_ledger(
        rt.database,
        created["conversation_id"],
        after_sequence=summary.covered_to_sequence,
        user_id=session["user_id"],
    )

    assert [row.sequence for row in retained] == list(
        range(summary.covered_to_sequence + 1, newest.sequence + 1)
    )


def test_inv4_a_summarizing_turn_adds_exactly_its_own_two_rows(rt, session):
    """The summary action writes no ledger row; the turn writes its user and assistant rows.

    Spec §13/INV-4 has two halves and this asserts both: the row arithmetic, and the untouched
    existing rows. The second half is the fingerprint comparison -- `message_count` alone cannot
    see a row that was rewritten in place, and the count assertion is what the original version
    of this test had.
    """

    agent, model = a_wired_agent(rt)
    client, created, _turns = drive_until_summarized(rt, session, agent, model)
    before_rows = message_count(rt, created["conversation_id"])
    before_fingerprint = row_fingerprint(rt, created["conversation_id"])

    after, turns = drive_until_a_new_version(rt, session, agent, client, created)
    after_fingerprint = row_fingerprint(rt, created["conversation_id"])

    assert after.version > 0, "this turn really did summarize"
    assert message_count(rt, created["conversation_id"]) == before_rows + 2 * turns
    assert before_fingerprint == [row for row in after_fingerprint if row in before_fingerprint], (
        "every row that was already there is still there, with the same content"
    )


def test_inv6_a_summary_call_is_audited_and_adds_no_visible_message(rt, session):
    """The audit row exists, is scoped to this conversation, and is not a visible message.

    The query is scoped by `conversation_id` on purpose: unscoped, it would also match a row an
    earlier test left in the shared `llm_call_audit` table, so the test could pass without this
    conversation ever having summarized.
    """

    agent, model = a_wired_agent(rt, audited=True)
    client, created, _turns = drive_until_summarized(rt, session, agent, model)

    with rt.database.sessions() as db:
        summary_calls = db.scalars(
            select(LlmCallAudit).where(
                LlmCallAudit.source == "context_summary",
                LlmCallAudit.conversation_id == created["conversation_id"],
            )
        ).all()

    assert summary_calls, "the summary call is auditable under its own conversation"
    ledger = messages(client, created["conversation_id"])["items"]
    assert all(row["role"] in {"user", "assistant"} for row in ledger)
    assert all("历史摘要" not in row["content"] for row in ledger), "the summary is not a message"


async def test_inv7_the_turn_without_a_conversation_writes_no_summary(rt, session):
    agent = wired_agent(rt)

    [event async for event in agent.invoke(session["session_id"], session["user_id"], "你好", {})]

    with rt.database.sessions() as db:
        assert db.scalars(select(ContextSummary)).all() == []


def test_inv9_an_oversized_summary_writes_no_version(rt, session):
    """A summary that blows its own budget would break the invariant it exists to serve.

    What this pins is that the turn wrote *no* version: this conversation never had one, so
    there is nothing here for an oversized answer to have overwritten. The other half of the
    clause -- that a failure leaves the version that was already there untouched -- needs a
    conversation that has a version first, and is pinned at unit level by
    `backend/tests/test_context_summary.py:243 test_a_failure_with_a_base_keeps_the_base`.
    """

    agent = wired_agent(rt, decisions=60)
    model = RoutingModel([DECISION] * 60, summaries=("长" * 5000,) * 60)
    agent.set_model(model)

    _client, created = drive_turns(rt, session, agent, turns=6)

    assert model.summary_prompts, "the turn really did try to summarize"
    assert versions(rt, created["conversation_id"]) == []


def test_inv11_a_view_compressed_to_the_limit_does_not_compress_again(rt, session):
    """spec §13/INV-11 and §6.1(a): the end state may still sit above the trigger.

    The last round is never stripped, so one huge message keeps the view above the trigger no
    matter how much history is summarized away. What this test asserts is the assembly's outcome:
    it reports `summarized`, stays within `input_budget`, and commits a version. The stronger
    property -- that the assembler does not loop into a second compression round -- is proven
    positively by `test_compressed_to_the_limit_but_still_above_the_trigger_does_not_compress_again`
    in `backend/tests/test_context_view.py:174`, which assembles twice and asserts the second pass
    neither calls the model nor writes a version.
    """

    agent, model = a_wired_agent(rt)
    client, created, _turns = drive_until_summarized(rt, session, agent, model)
    before = len(versions(rt, created["conversation_id"]))

    # 2,400 CJK chars is the largest single message that still fits TIGHT's 4,000-token
    # budget once the current supervisor prompt overhead is added; the point is that the
    # huge last round is never stripped, not the exact character count.
    assert final_text(submit(client, created["conversation_id"], "问" * 2400)) == "好"

    payload = assemblies(rt, created["conversation_id"])[-1].payload_json
    assert payload["reason"] == "summarized"
    assert payload["estimated_tokens"] > payload["summary_trigger"], "still above the trigger"
    assert payload["estimated_tokens"] <= payload["input_budget"], "and still sendable"
    assert len(versions(rt, created["conversation_id"])) > before, "the strip really committed"


def test_inv11b_each_summary_call_writes_exactly_one_version(rt, session):
    """A chunk and a version are one-to-one: no call without a row, no row without a call."""

    agent, model = a_wired_agent(rt)
    _client, created, _turns = drive_until_summarized(rt, session, agent, model)

    assert model.summary_prompts, "the conversation really did summarize"
    assert len(versions(rt, created["conversation_id"])) == len(model.summary_prompts)


def test_inv12_every_summary_call_stays_within_its_own_budget(rt, session):
    """The whole summary call -- both messages -- fits the budget the chunking is built around.

    The bound is spelled out term by term, and every term is measured here rather than restated
    as a constant:

    - the system message is the fixed `SUMMARY_PROMPT`;
    - the user template's own text, rendered with both slots empty;
    - the `previous` slot: `backend/app/context/summary.py:262` refuses a stored summary whose
      `estimate_tokens` exceeds `max_tokens`, so `TIGHT.summary_max_tokens` bounds it -- every
      version in this conversation was written under TIGHT as well -- and the placeholder
      constant is measured instead when it is the larger of the two;
    - the `conversation` slot: the largest chunk `chunk_for_summary` can build out of this
      conversation's rows under `TIGHT.chunk_budget`, rendered by `_describe_ledger`, the same
      function production renders it with. Using the whole ledger's largest chunk is sound, not
      merely convenient: packing is greedy from the head
      (`backend/app/context/budget.py:192-205`), so packing any prefix -- and every strip is a
      prefix -- can only end on a chunk the ledger's own packing already contains.

    What this bound does not claim: it is looser than the path production actually takes, so it
    is a regression guard, not evidence that a summary call ever came near the model's window.
    On the TIGHT fixture the four terms measured 269 + 35 + 100 + 607 = 1011 tokens against a
    worst observed call of 921, and `TIGHT.input_budget` is 4000; with the shipped defaults the
    same arithmetic lands around 34k against 121856.

    What it catches is the chunking being turned off or bypassed -- replacing
    `for chunk in chunk_for_summary(strip, chunk_budget)` in `backend/app/context/summary.py`
    with `for chunk in [list(strip)]` (Task 10's mutation table, row 4): the `conversation` slot
    then carries the whole strip, and the call blows past the bound (measured: 1528 > 1011).

    What it cannot catch, by construction, is growth of the constants it is itself computed
    from. Its first two terms are `estimate_message_tokens(SUMMARY_PROMPT)` and the rendered
    user template measured at run time, and its third carries `TIGHT.summary_max_tokens`; growing
    any of them grows the bound by the same amount, so this assertion can never go red on it.
    That job belongs to the two fixed-bound assertions below -- they compare against numbers that
    do not move with the constants -- and the budgets' own self-consistency is checked at process
    start by `validate_context_budget` (`backend/app/core/config.py:71-80`, whose two relational
    checks in `backend/app/context/budget.py:85-107` do not bound their absolute size either). A
    bound computed from a constant cannot detect that constant's growth.
    """

    agent, model = a_wired_agent(rt)
    _client, created, _turns = drive_until_summarized(rt, session, agent, model)

    assert model.summary_messages, "the conversation really did summarize"

    # Fixed ceilings on the prompt text itself, so that "someone grew the prompt" is a failure
    # this test can actually produce. The bound below is computed from these same constants, so
    # it cannot see them grow (see the docstring); these two numbers are deliberately above the
    # measured 269 and 35 -- an equality would freeze the current wording, not guard it.
    assert estimate_message_tokens(SUMMARY_PROMPT) <= 300, "the summarizer's system prompt grew"
    assert (
        estimate_message_tokens(render(SUMMARY_USER_TEMPLATE, previous="", conversation="")) <= 60
    ), "the summarizer's user template grew"

    rows = read_ledger(
        rt.database, created["conversation_id"], after_sequence=0, user_id=session["user_id"]
    )
    chunk_allowance = max(
        estimate_tokens(_describe_ledger(chunk))
        for chunk in chunk_for_summary(rows, TIGHT.chunk_budget)
    )
    bound = (
        estimate_message_tokens(SUMMARY_PROMPT)
        + estimate_message_tokens(render(SUMMARY_USER_TEMPLATE, previous="", conversation=""))
        + max(TIGHT.summary_max_tokens, estimate_tokens(NO_PREVIOUS_SUMMARY))
        + chunk_allowance
    )
    for call in model.summary_messages:
        total = sum(estimate_message_tokens(row["content"]) for row in call)
        assert total <= bound, f"a summary call grew past its own budget: {total} > {bound}"


def test_inv13_the_summary_prompt_carries_no_identifiers(rt, session):
    """The summarizer sees the conversation, not who is having it (spec §13)."""

    agent, model = a_wired_agent(rt)
    _client, created, _turns = drive_until_summarized(rt, session, agent, model)

    # Bound to a local and checked for `None` first: `bank_customer_id` is `str | None`, so a
    # user with no binding would make `not in` raise `TypeError` instead of failing here.
    customer = rt.identity.bank_customer_id(session["user_id"])
    assert customer is not None, "this fixture's user really does have a bank customer id"

    assert model.summary_prompts, "the conversation really did summarize"
    for prompt in model.summary_prompts:
        assert created["conversation_id"] not in prompt
        assert session["user_id"] not in prompt
        assert customer not in prompt


def test_inv14_the_model_never_sees_a_superseded_answer(rt, session):
    """A re-answered question must reach the model once, with the newest answer."""

    agent, model = a_wired_agent(rt)
    client = client_for(rt, session, agent)
    created = create(client, "补充信息")
    submit(client, created["conversation_id"], "我要转账", context=None)

    # Two answers to one question: the first is superseded by the second.
    rt.conversations.append_user_message(
        session["user_id"],
        created["conversation_id"],
        idempotency_key="stale",
        expected_hash="stale-hash",
        content="陈旧的补充",
        message_kind="interrupt_answer",
        question_id="q_shared",
    )
    rt.conversations.append_user_message(
        session["user_id"],
        created["conversation_id"],
        idempotency_key="fresh",
        expected_hash="fresh-hash",
        content="最新的补充",
        message_kind="interrupt_answer",
        question_id="q_shared",
    )
    submit(client, created["conversation_id"], "继续")

    assert "最新的补充" in model.decision_prompts[-1]
    assert "陈旧的补充" not in model.decision_prompts[-1]


def test_inv15_a_summary_provider_failure_does_not_fail_the_turn(rt, session):
    """A provider failure during summarization ends the turn normally, with no version written.

    The "keep the old summary" half of this clause is not what this test can see: the driver
    below fails every summary from the first turn on, so this conversation never has an old
    version for a later one to preserve. That half is pinned at unit level by
    `backend/tests/test_context_summary.py:243 test_a_failure_with_a_base_keeps_the_base`.
    """

    agent = wired_agent(rt, decisions=60)

    class Exploding(RoutingModel):
        """A provider that fails only on the summary call, the way a real timeout would."""

        def complete(self, messages):
            if self.SUMMARY_MARKER in messages[0]["content"]:
                # `RoutingModel.complete` is never reached on this path, so the attempt is
                # recorded here: without it the test could not tell "the call was attempted
                # and failed" from "the call was never made at all".
                self.summary_prompts.append(messages[-1]["content"])
                raise ModelClientError("LLM_TIMEOUT", retryable=True, detail="timeout")
            return super().complete(messages)

    model = Exploding([DECISION] * 60)
    agent.set_model(model)
    client, created = drive_turns(rt, session, agent, turns=6)

    assert model.summary_prompts, "the turn really did try to summarize"
    assert final_text(submit(client, created["conversation_id"], "还要继续")) == "好"
    assert versions(rt, created["conversation_id"]) == []


def test_the_two_context_limit_reasons_are_told_apart(rt, session):
    assert context_limit_error(CONTEXT_LIMIT_REASON_SUMMARY_FAILED).retryable is True
    assert context_limit_error(CONTEXT_LIMIT_REASON_OVER_BUDGET).retryable is False


def test_the_original_messages_are_still_paged_back_in_full(rt, session):
    """Compression changes the prompt, never the ledger the user can read."""

    agent, model = a_wired_agent(rt)
    client, created, turns = drive_until_summarized(rt, session, agent, model)

    page = messages(client, created["conversation_id"], limit=200)

    assert len(page["items"]) == 2 * turns, "every turn wrote its user and assistant rows"
    assert [row["sequence"] for row in page["items"]] == list(range(1, 2 * turns + 1))


def test_the_domain_prompt_never_carries_a_transcript(rt, session):
    """A domain agent needs no compression because it never receives the transcript.

    This asserts positively on the domain prompt that was really rendered, while the conversation
    around it really did summarize. The selector filters `model.prompts` by `DOMAIN_PROMPT`'s own
    literal text -- the words between its first two slots, which no other agent's prompt carries
    (the template's first slot sits so early that the text before it is shared with the
    Supervisor's and the summarizer's own preambles) -- so it follows the template instead of a
    hand-copied substring, and the non-empty guard keeps a selector that stopped matching from
    making the assertions below vacuously true.

    All four checks below can go red, and each one was reddened by a real mutation -- that is why
    they are worth having. What differs is what each is sensitive to. The `role: content` check is
    sensitive to a transcript line reaching the rendered prompt: adding a
    `user: LEAKED TRANSCRIPT LINE` line to `DOMAIN_PROMPT` reddens it. The summary-header,
    summary-body and earlier-domain-result checks are sensitive to summary or result text reaching
    the rendered prompt by either route: splicing a summary header, a summary body or earlier
    result text into `DOMAIN_PROMPT` reddens the matching check, and so does appending that same
    text at the call site that renders the domain prompt. None of the four is a mere
    template-literal guard.
    `DOMAIN_PROMPT`'s own slots are `{display}`, `{agent_id}`, `{instruction}`, `{results}` and
    `{tools}`; there is no summary slot, so summary text can only arrive as literal text -- from
    the template or from the call site -- which is what the mutations above exercise.
    A previous revision of this docstring called the last three checks guards that "do not witness
    a leak path", and cited as proof a mutation that appended `state.get("summary")` to the
    rendered prompt and stayed green. That mutation was a no-op: `AgentState` has no `summary`
    key, so the appended expression was always the empty string and nothing was measured. The
    three checks do fail once summary text really reaches the rendered domain prompt.
    The previous version of this test asserted only the summary header, so the transcript-line
    route -- the one a leak would actually take -- had no witness at all.
    `本领域已处理` is not a summary here -- the script's second reply is what the *domain* call
    returns, and it reaches the supervisor through `{results}` -- but it is asserted absent too,
    because a domain prompt does have a `{results}` slot and a leak into it would be the same bug.

    The dispatch needs `grants={"transfer": ["echo"]}`: a domain with an empty tool menu runs the
    tool-less shell template, which never calls the model (roadmap A5 §1), so there would be no
    domain prompt to inspect at all. Granting the one catalogued tool gives the domain a turn.
    """

    script = [
        ("call", "handoff", '{"agent_id":"transfer","instruction":"处理一下"}'),
        "本领域已处理",
    ] + [DECISION] * 60
    agent, model = a_wired_agent(rt, script=script, grants={"transfer": ["echo"]})
    _client, _created, _turns = drive_until_summarized(rt, session, agent, model)

    domain_marker = DOMAIN_PROMPT.split("{display}", 1)[1].split("{", 1)[0]
    domain_prompts = [p for p in model.prompts if domain_marker in p]

    assert domain_prompts, "a domain agent really did run"
    for prompt in domain_prompts:
        assert not any(
            line.startswith(("user: ", "assistant: ")) for line in prompt.splitlines()
        ), "no transcript line reaches a domain prompt"
        assert "历史摘要（覆盖较早的对话" not in prompt, "no summary header"
        assert "历史摘要" not in prompt, "no summary body"
        assert "本领域已处理" not in prompt, "no earlier domain's result"
