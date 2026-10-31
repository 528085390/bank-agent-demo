"""Run one domain's subgraph, without the Supervisor.

A module team needs to prove its tools work. Making it stand up the whole orchestrator --
and understand the Supervisor's routing -- would mean six teams each inventing their own
idea of what "works" means. This is the shared answer: build the domain subgraph from the
module's own declaration, drive it with a scripted model, and hand back what happened.

The default adapter is the production one, so the module's real handler runs. What is
replaced is the bank: `FakeBankClient` records every request and answers from a fixed
table, which is what keeps a module's contract test free of a running bank.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.domain import DOMAIN_ROSTER, build_domain_specs
from app.agents.runtime import AgentEvent, AgentRuntime
from app.agents.supervisor import build_domain_graph
from app.integration.bank_client import CODE_KINDS, BankClient, BankResponse
from app.integration.bank_errors import RETRYABLE_KINDS, BankCallError, BankErrorKind, message_for
from app.modules.base import ModuleSpec
from app.modules.registry import build_registry
from app.tools.bank_adapter import BankToolAdapter
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolAdapter, TrustedContext
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

SESSION = "sess_harness"
USER = "user_0001"
CUSTOMER = "u_zhang"


def generic_roster(roster=None):
    """The production roster with every module graph replaced by the test-support loop.

    A test that drives the generic tool loop must not be answered by a module's own graph:
    wealth, card and transfer all declare a `graph_builder`, and the tool-less shells
    declare the minimal template. Both are replaced with `tool_domain_graph_builder`, the
    test-support copy of the loop production no longer runs, so the test drives
    decide/wait/process.
    """

    return tuple(
        replace(declaration, graph_builder=tool_domain_graph_builder)
        for declaration in (roster or DOMAIN_ROSTER)
    )


class FakeBankClient:
    """A bank that records what it was asked and answers from a fixed table.

    An unregistered path raises rather than returning an empty envelope: a test that
    forgot to register a response should fail, not silently succeed on nothing. A
    registered non-zero `code` raises too, the way the real client classifies it.
    """

    def __init__(self, responses: Mapping[str, BankResponse] | None = None) -> None:
        self._responses = dict(responses or {})
        self.requests: list[dict[str, Any]] = []

    def register(self, path: str, data: Any, *, code: int = 0, message: str = "success") -> None:
        self._responses[path] = BankResponse(code=code, message=message, data=data)

    def request(
        self,
        method: str,
        path: str,
        *,
        user_id: str,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> BankResponse:
        if not user_id:
            # The real client refuses before the socket, and the bank would answer with its
            # own default customer. A fake that accepted this would let a module's test pass
            # while the production path fails closed.
            raise AssertionError("a bank call without a customer id must never be made")
        self.requests.append(
            {
                "method": method,
                "path": path,
                "user_id": user_id,
                "params": dict(params or {}),
                "json": json,
                "idempotency_key": idempotency_key,
            }
        )
        if path not in self._responses:
            raise AssertionError(f"no canned bank response registered for {path}")
        response = self._responses[path]
        if response.code != 0:
            # The production client classifies a non-zero envelope into a `BankCallError`
            # before a handler can read `.data` (bank_client.py:170). A fake that returned it
            # as a success would let a module mistake a bank failure for an empty payload.
            # There is no HTTP status here, so an unknown code is a server failure.
            kind = CODE_KINDS.get(response.code, BankErrorKind.SERVER)
            raise BankCallError(kind, message_for(kind), retryable=kind in RETRYABLE_KINDS)
        return response

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DomainRun:
    events: list[AgentEvent] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    bank_requests: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    final_text: str | None = None

    def events_named(self, name: str) -> list[dict[str, Any]]:
        """Every event of one kind, in order. Most assertions want this."""

        return [event.data for event in self.events if event.event == name]


def field_names(question: Mapping[str, Any]) -> list[str]:
    """The field names a question asked for.

    The metadata (label/type/required) is a separate concern with its own tests; an
    assertion about *which* fields were asked for should not restate all of it.
    """

    return [
        item["name"] if isinstance(item, Mapping) else item for item in question.get("fields") or []
    ]


def without_fields(question: Mapping[str, Any]) -> dict[str, Any]:
    """The question minus its field metadata.

    Its companion: a test that asserts the *rest* of the question still has to compare those
    keys, and an exact dict comparison cannot ignore one key on its own. Dropping the key here
    is what lets `field_names` be the single assertion about the fields.
    """

    return {key: value for key, value in question.items() if key != "fields"}


def run_domain(
    module: ModuleSpec,
    *,
    replies: list[str] | None = None,
    model: Any | None = None,
    adapter: ToolAdapter | None = None,
    bank: BankClient | None = None,
    message: str = "帮我",
    answer: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    customer: str | None = CUSTOMER,
    again: bool = False,
) -> DomainRun:
    """Run one module's domain subgraph and collect what it published.

    `model` replaces the default scripted one, which is how a scripted decision is driven.
    Existing callers that pass only `replies` are unchanged.

    `adapter` exists for the rare test that wants canned tool results instead of the real
    handler. The default is the production adapter, so a module's own code is what runs.

    `answer` drives the second half of a pause: the domain is run, and if it asks a question
    the answer is supplied and the run continues on the *same* checkpointer. That is what
    makes the answer-to-tool leg reachable -- supplying a resume to a first, unanswered run
    would have no open question to bind to, and the graph refuses it. A single mapping
    answers the one question a run is expected to ask; a sequence answers each pause in
    turn, which is how a test walks a domain through its full allowance of questions.

    `again` sends one more ordinary message on the same checkpointer once the first run has
    finished. That is how a test proves a concluded activation left nothing behind: the next
    activation on this conversation must start from a fresh thread, not the last one's tool
    transcript.
    """

    registry = build_registry([module])
    resolved_bank = bank if bank is not None else FakeBankClient()
    resolved_adapter = adapter if adapter is not None else BankToolAdapter(registry, resolved_bank)
    gateway = ToolGateway(registry.catalog, resolved_adapter)
    context = TrustedContext(user_id=USER, session_id=SESSION, bank_customer_id=customer)
    specs = build_domain_specs(gateway, context, roster=registry.roster)
    if not specs:
        # A module with `declaration=None` provides shared tools and owns no domain, so it
        # has no subgraph to drive. Say that, rather than letting `specs[0]` raise IndexError.
        raise AssertionError(
            f"module {module.name!r} declares no domain (declaration=None), so there is no "
            f"subgraph to run; drive it through the module that owns the tools instead"
        )
    model = model if model is not None else ScriptedModel(replies or [])
    # `build_domain_graph` already returns a compiled graph, and deliberately one without a
    # checkpointer: as a Supervisor subgraph, LangGraph hands the parent's down to it. Driven
    # standalone there is no parent to hand one down, and a pause has to be checkpointed to be
    # readable at all, so the harness attaches one here rather than rebuilding the graph.
    # One checkpointer for both phases, so the answer can bind to the question just asked.
    graph = build_domain_graph(model, specs[0]).copy(update={"checkpointer": InMemorySaver()})
    runtime = AgentRuntime(graph=graph)

    async def invoke(context_payload: dict[str, Any]) -> list[AgentEvent]:
        return [event async for event in runtime.invoke(SESSION, USER, message, context_payload)]

    events = asyncio.run(invoke({}))
    if answer is not None:
        answers = [answer] if isinstance(answer, Mapping) else list(answer)
        consumed = 0  # events already accounted for by the resumes before this one
        for value in answers:
            # The first answer must have a pause to bind to -- a run that never asked is a
            # test bug, and `_open_question_id` says so. Later answers stop the walk when
            # the domain stopped asking: there is no open question left to bind to.
            if consumed and not any(event.event == "interrupt" for event in events[consumed:]):
                break
            question_id = _open_question_id(events)
            more = asyncio.run(
                invoke({"resume": {"question_id": question_id, "value": dict(value)}})
            )
            consumed = len(events)
            events = events + more
    if again:
        events += asyncio.run(invoke({}))

    status: str | None = None
    final_text: str | None = None
    for event in events:
        if event.event == "subtask_end":
            status = event.data.get("status")
            # A domain-only run never emits `final` -- that event is the Supervisor's reply.
            # The domain's own report is the conclusion its ending carried, and that is what
            # an assertion about "what did the domain end up saying" wants.
            final_text = event.data.get("summary")
        elif event.event == "interrupt":
            # A run that paused on a question did not end. Name the pause, so a test can
            # tell "waiting for the user" from a run that never reached any ending; a later
            # subtask_end (the resumed half) overwrites it.
            status = "WAITING_INPUT"
        elif event.event == "final":
            final_text = event.data.get("text")
    return DomainRun(
        events=events,
        prompts=list(model.prompts),
        bank_requests=list(resolved_bank.requests),
        status=status,
        final_text=final_text,
    )


def _open_question_id(events: list[AgentEvent]) -> str:
    """The question the run paused on, named explicitly so the answer is bound to it."""

    for event in reversed(events):
        if event.event == "interrupt":
            return str(event.data["question_id"])
    raise AssertionError("answer was given but the domain never asked a question")
