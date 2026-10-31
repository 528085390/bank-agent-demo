"""The generic domain loop, kept as test support.

Production no longer runs this loop. A domain either declares its own `graph_builder` or
is a tool-less shell (`app.agents.subgraph_templates`); a domain with business tools and no
builder is a build-time error. This module reproduces the loop the base used to run, so a
test that means "drive the base's generic tool loop" still can.

It orchestrates; it does not encode. The fund-safety mechanics -- minting the idempotency
key once before the retry, retrying once, collecting business ids, publishing
`subtask_end` -- live in `app.agents.sdk` and are called from here. Nothing in this module
is production code.

The shape is the old decide / wait / process split: `decide` and `process` call the model
and never interrupt; `wait` interrupts and never calls the model. That split is the whole
fix for resume -- LangGraph replays the interrupting node, so only `wait` is replayed, and
it reads its question back from the checkpoint instead of asking the model for a new one.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import parse_domain_decision
from app.agents.declaration import DomainSpec
from app.agents.prompts import (
    DOMAIN_PROMPT,
    describe_results,
    describe_tools,
    render,
    user_turn,
)
from app.agents.sdk import (
    MAX_TOOL_ROUNDS,
    DomainOutcome,
    awaiting,
    conclude_domain,
    domain_outcome,
    next_attempt,
    overflow_outcome,
    pending_context,
    read_answer,
    run_tool,
    tool_envelope,
    wait_for_user,
)
from app.agents.state import AgentState
from app.agents.subgraph_templates import NO_TOOLS_SUMMARY, minimal_shell_graph
from app.agents.toolcalls import collect_stream
from app.agents.upstream import UPSTREAM_RETRY_NOTICE, UpstreamUnavailable
from app.integration.model_client import ModelClient
from app.tools.protocol import ToolStatus
from app.tools.schema import ASK_USER_TOOL_NAME

NO_FIELDS_SUMMARY = "请补充更多信息。"


def _with_tools_available(spec: DomainSpec, outcome: DomainOutcome) -> DomainOutcome:
    """With an empty tool menu, every outcome becomes the same conclusion.

    The model's own prose is replaced too: a summary is written for the Supervisor, so it
    talks about handing the request to another domain, and the Supervisor would read that as
    work still to do.
    """

    if not spec.tool_menu():
        return DomainOutcome(summary=NO_TOOLS_SUMMARY)
    return _with_answerable_fields(outcome)


def _with_answerable_fields(outcome: DomainOutcome) -> DomainOutcome:
    """A question that names no fields is not a question."""

    if outcome.ask is None or outcome.ask.get("fields"):
        return outcome
    return DomainOutcome(summary=str(outcome.ask.get("display") or NO_FIELDS_SUMMARY))


def _conclude(
    spec: DomainSpec,
    state: AgentState,
    outcome: DomainOutcome,
    writer: Any,
    *,
    business_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Publish the domain's ending, then hand its result back to the Supervisor."""

    if outcome.wants_tool:
        tool = run_tool(spec, outcome, writer)
        summary, status, ids = tool.summary, tool.status, tool.business_ids
    else:
        summary, status, ids = outcome.summary, ToolStatus.SUCCEEDED, {}
    merged = {**ids, **(business_ids or {})}
    return conclude_domain(spec, state, summary, status, writer, business_ids=merged)


def domain_route(state: AgentState) -> str:
    """Where a domain subgraph goes next: back to the user, or finished."""

    return "wait" if state.get("turn_phase") == "waiting_for_user" else END


def domain_user_turn(ask: str, tools: list[tuple[str, str]]) -> str:
    """The domain agent's user turn, with the closed set of tool names it may request."""

    names = "、".join(name for name, _ in tools)
    rules = ["你是本领域 Agent，只处理本领域事务。"]
    if names:
        rules.append("tool 只能从这几个里选：" + names + "。")
    else:
        rules.append("本领域当前没有可用工具，不要输出 tool。")
    return user_turn(ask, rule="\n".join(rules), label="本次指令")


def _seed_thread(
    spec: DomainSpec,
    state: AgentState,
    *,
    question: Mapping[str, Any] | None = None,
    collected: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The first message of an activation: the instruction, plus a parked question if any."""

    ask = state.get("goal") or "按用户请求处理本领域事务。"
    content = domain_user_turn(ask, spec.tool_menu())
    if question is not None and collected is not None:
        content = f"{content}\n\n{pending_context(question, collected)}"
    return [{"role": "user", "content": content}]


def _domain_turn(
    model: ModelClient,
    spec: DomainSpec,
    state: AgentState,
    *,
    thread: list[dict[str, Any]],
) -> DomainOutcome:
    """Ask the domain's model what to do, on the activation's thread."""

    ask = state.get("goal") or "按用户请求处理本领域事务。"
    tools = spec.tool_menu()
    prompt = render(
        DOMAIN_PROMPT,
        display=spec.display,
        agent_id=spec.agent_id,
        instruction=ask,
        results=describe_results(list(state.get("domain_results") or [])),
        tools=describe_tools(tools),
    )
    messages = ({"role": "system", "content": prompt}, *thread)
    text, calls = collect_stream(model, messages, tuple(spec.tool_schemas()))
    if text.strip():
        thread.append({"role": "assistant", "content": text})
    elif calls:
        # The flattened seam has no tool_calls field, so the call rides in the content, in
        # the same JSON vocabulary the tool envelope that follows it speaks.
        thread.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"tool": calls[0].name, "arguments": calls[0].arguments},
                    ensure_ascii=False,
                ),
            }
        )
    known = {name for name, _ in tools} | {ASK_USER_TOOL_NAME}
    return domain_outcome(parse_domain_decision(text, calls, known))


def _domain_loop(
    spec: DomainSpec,
    model: ModelClient,
    state: AgentState,
    thread: list[dict[str, Any]],
    *,
    rounds: int,
    writer: Callable[[dict[str, Any]], None],
    collected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One activation, run to its end: report, question, or out of rounds."""

    summaries: list[str] = []
    collected_ids: dict[str, str] = {}
    while True:
        outcome = _with_tools_available(
            spec,
            _domain_turn(model, spec, state, thread=thread),
        )
        if outcome.ask is not None:
            parked = state.get("pending_question")
            return awaiting(
                spec,
                outcome,
                writer,
                collected=collected,
                attempt=next_attempt(parked) if parked else 1,
                domain_thread=thread,
                tool_rounds=rounds,
            )
        if not outcome.wants_tool:
            return _conclude(spec, state, outcome, writer, business_ids=collected_ids)
        if rounds >= MAX_TOOL_ROUNDS:
            return _conclude(
                spec, state, overflow_outcome(spec, summaries), writer, business_ids=collected_ids
            )
        tool = run_tool(spec, outcome, writer)
        thread = [*thread, tool_envelope(outcome, tool)]
        summaries.append(tool.summary)
        collected_ids.update(tool.business_ids)
        rounds += 1


def domain_decide(model: ModelClient, spec: DomainSpec):
    """The decide node: run a fresh activation to an ending."""

    def run(state: AgentState) -> dict[str, Any]:
        writer = get_stream_writer()
        writer({"event": "agent_start", "agent_id": spec.agent_id})
        thread = list(state.get("domain_thread") or [])
        if not thread:
            thread = _seed_thread(spec, state)
        return _domain_loop(
            spec,
            model,
            state,
            thread,
            rounds=int(state.get("tool_rounds") or 0),
            writer=writer,
        )

    return run


def process_answer(model: ModelClient, spec: DomainSpec):
    """The process node: consume the answer and resume the activation."""

    def run(state: AgentState) -> dict[str, Any]:
        writer = get_stream_writer()
        question = state.get("pending_question") or {}
        answer, problem = read_answer(state.get("pending_answer"), question)
        if problem is not None:
            return awaiting(
                spec,
                DomainOutcome(ask=question),
                writer,
                question=question,
                reask=f"{problem}请重新提供。",
                attempt=next_attempt(question),
                question_id=question.get("question_id"),
            )

        writer({"event": "agent_start", "agent_id": spec.agent_id})
        collected = {**(state.get("collected_inputs") or {}), **answer}
        thread = list(state.get("domain_thread") or [])
        if not thread:
            thread = _seed_thread(spec, state, question=question, collected=collected)
        thread.append(
            {"role": "user", "content": json.dumps({"user_answer": answer}, ensure_ascii=False)}
        )
        try:
            return _domain_loop(
                spec,
                model,
                state,
                thread,
                rounds=int(state.get("tool_rounds") or 0),
                writer=writer,
                collected=collected,
            )
        except UpstreamUnavailable:
            return awaiting(
                spec,
                DomainOutcome(ask=question),
                writer,
                question=question,
                collected=collected,
                reask=UPSTREAM_RETRY_NOTICE,
                attempt=question.get("attempt"),
                question_id=question.get("question_id"),
            )

    return run


def tool_domain_graph_builder(
    model: ModelClient,
    spec: DomainSpec,
    *,
    assembler: Any = None,
    turn: Any = None,
    client: Any = None,
) -> Any:
    """A `graph_builder`-compatible callable that rebuilds the old decide/wait/process graph.

    A tool-less domain has no loop to drive: its production behaviour is the model-free
    `minimal_shell_graph`, so the builder defers to it rather than reproducing an empty
    tool menu the model would only be asked to guess at.
    """

    if not spec.tool_menu():
        return minimal_shell_graph(model, spec, assembler=assembler, turn=turn, client=client)

    graph = StateGraph(AgentState)
    graph.add_node("decide", domain_decide(model, spec))
    graph.add_node("wait", wait_for_user)
    graph.add_node("process", process_answer(model, spec))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", domain_route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", domain_route, {"wait": "wait", END: END})
    return graph.compile()
