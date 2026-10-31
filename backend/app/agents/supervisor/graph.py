"""Graph assembly and Supervisor routing.

`build_supervisor` compiles the Supervisor with each domain as a subgraph node, and
`build_domain_graph` decides how one domain's graph is obtained: its own builder, the
tool-less shell, or a refusal. `supervisor_node` and `supervisor_route` are the routing half.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import SupervisorDecision
from app.agents.declaration import DomainSpec
from app.agents.prompts import (
    SUPERVISOR_PROMPT,
    describe_agents,
    describe_results,
    describe_transcript,
    render,
    risk_sentence,
    visible_results,
)
from app.agents.runtime import TurnContext
from app.agents.sdk import _current_request
from app.agents.state import AgentState, message_dicts
from app.agents.subgraph_templates import NO_TOOLS_SUMMARY, minimal_shell_graph
from app.agents.supervisor.handoff import (
    SupervisorOutcome,
    _DeltaBatch,
    _outcome_from,
    _stream_decision,
)
from app.agents.task_dag import TaskNode, TaskPlan, label_for_domain_task
from app.agents.toolcalls import StreamTruncated
from app.agents.turn import TERMINAL_TURN_STATUSES, TurnStatus
from app.agents.upstream import UpstreamUnavailable, upstream_app_error
from app.context.view import REASON_OVER_BUDGET, AssembledView, ContextAssembler
from app.core.errors import AppError, context_limit_error
from app.core.types import new_id
from app.integration.model_client import ModelClient, ModelClientError
from app.tools.protocol import TrustedContext

# The log channel keeps the name it had before the split (`app.agents.supervisor`), so a
# record emitted from here is not renamed just because the module became a package.
logger = logging.getLogger("app.agents.supervisor")

DONE = "done"

# How many domain delegations one request may chain before the Supervisor must answer.
# Without a bound, a model that keeps naming agents would run until LangGraph's own
# recursion limit aborted the request instead of answering it.
MAX_DOMAIN_TURNS = 4

# A queued handoff is written before the previous domain has run, so it cannot name that
# domain's result yet. The Supervisor writes this placeholder instead; dispatch replaces it
# with the latest conclusion. Without that, the second domain would be told to act on a
# template string and would have to guess the first domain's answer.
PREVIOUS_SUMMARY_PLACEHOLDER = "{{previous_summary}}"
PREVIOUS_SUMMARY_MISSING = "（上一步没有返回可用结论，请按当前已知信息继续，不要猜测。）"


def _resolve_previous_summary(instruction: str, results: list[dict[str, Any]]) -> str:
    """Fill a queued handoff's placeholder with the conclusion it depends on."""

    if PREVIOUS_SUMMARY_PLACEHOLDER not in instruction:
        return instruction
    previous = next(
        (
            str(item.get("summary") or "").strip()
            for item in reversed(results)
            if str(item.get("summary") or "").strip()
        ),
        "",
    )
    return instruction.replace(
        PREVIOUS_SUMMARY_PLACEHOLDER, previous or PREVIOUS_SUMMARY_MISSING
    )


def _task_label(agent_id: str, instruction: str) -> str:
    """A plan label is shown before the dependency runs, so it must not leak the placeholder."""

    return label_for_domain_task(
        agent_id, instruction.replace(PREVIOUS_SUMMARY_PLACEHOLDER, "待上一步结论")
    )


# `NO_TOOLS_SUMMARY` -- what a tool-less domain says -- now lives with the template that
# publishes it (`app.agents.subgraph_templates`) and is imported above.


# Why a turn ended (spec §4.3). "completed" is the default and is not sent, so an ordinary
# reply keeps the payload shape it had before this phase.
TERMINAL_COMPLETED = "completed"
TERMINAL_API_ERROR = "api_error"
TERMINAL_MAX_TURNS = "max_turns"
TERMINAL_ABORTED_STREAMING = "aborted_streaming"


# The provider's own "this prompt is too long" code, as the audited adapter reports it back
# in `AppError.details["upstream_code"]` (see `classify_provider_error`).
OVERFLOW_UPSTREAM_CODE = "LLM_CONTEXT_OVERFLOW"


def _build_task_plan_from_decision(decision: SupervisorDecision) -> TaskPlan | None:
    """The graph a decision describes, or None when it delegates to nobody.

    Node 1 is the handoff this round dispatches; the rest of `queued` follow in the order the
    model asked for them, each waiting on the one before, because a queue is a sequence rather
    than a set. A reply-only decision has no work to order, so it gets no plan -- an empty graph
    would claim the round is running something.
    """

    if decision.agent_id is None:
        return None
    nodes = [
        TaskNode(
            id="node_1",
            label=_task_label(decision.agent_id, decision.instruction),
            owner=decision.agent_id,
        )
    ]
    for item in decision.queued:
        nodes.append(
            TaskNode(
                id=f"node_{len(nodes) + 1}",
                label=_task_label(item.agent_id, item.instruction),
                owner=item.agent_id,
                depends_on=(nodes[-1].id,),
            )
        )
    return TaskPlan(plan_id=new_id("plan"), nodes=tuple(nodes))


def supervisor_node(
    model: ModelClient,
    domains: list[DomainSpec],
    assembler: ContextAssembler | None = None,
    turn: TurnContext | None = None,
):
    """Ask the model which agent runs next, or for the reply that ends the turn.

    When an assembler was injected, the transcript half of the prompt is the assembled
    view -- the summary covering the earlier rounds plus the rounds kept verbatim -- rather
    than the whole checkpoint transcript. A directly built graph (tests) gets no assembler:
    its transcript is the raw one and the summary slot is empty.

    Only the `turn` knows which conversation and which ledger sequence this prompt belongs
    to, and `ContextAssembler` deliberately does not import `app.agents`, so those two
    values travel here through the graph factory rather than through `TrustedContext`.
    """

    known = {spec.agent_id for spec in domains}
    menu = [(spec.agent_id, spec.purpose) for spec in domains]
    conversation_id = turn.conversation_id if turn is not None else None
    current_sequence = turn.sequence if turn is not None else 0

    def overhead_prompt(results: list[Any]) -> str:
        """The fixed part of the prompt: everything except the transcript and the summary."""

        return render(
            SUPERVISOR_PROMPT,
            transcript="",
            summary="",
            results=describe_results(results),
            agents=describe_agents(menu),
        )

    def prompt_for(state: AgentState, results: list[Any], view: AssembledView | None) -> str:
        return render(
            SUPERVISOR_PROMPT,
            transcript=(
                view.transcript
                if view is not None
                else describe_transcript(message_dicts(state.get("messages")))
            ),
            summary=(view.summary_text or "（暂无）") if view is not None else "",
            results=describe_results(results),
            agents=describe_agents(menu),
        )

    def assemble(state: AgentState, results: list[Any], *, aggressive: bool) -> AssembledView:
        return assembler.assemble(
            conversation_id=conversation_id,
            current_sequence=current_sequence,
            prompt_overhead=overhead_prompt(results),
            transcript_fallback=describe_transcript(message_dicts(state.get("messages"))),
            aggressive=aggressive,
        )

    def _dispatch(
        handoff: Mapping[str, Any],
        rest: list[Any],
        turns: int,
        plan_id: str | None,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Hand one node to its domain, and say that the node is running.

        Both dispatch paths go through here -- the plan just drawn and the queue an earlier
        round left -- so this is the one place that knows which node started. The node id is
        written into the checkpoint for the same reason it is broadcast: the round that
        finishes the node has nothing else to name it by. A handoff that names no node, or a
        plan with no id, has nothing to report and nothing to record.
        """

        update: dict[str, Any] = {
            "active_agent": handoff["agent_id"],
            "goal": _resolve_previous_summary(
                str(handoff.get("instruction") or ""), results
            ),
            "turns": turns + 1,
            "pending_handoffs": list(rest),
        }
        node_id = handoff.get("node_id")
        if node_id is None or plan_id is None:
            return update
        get_stream_writer()(
            {
                "event": "task_update",
                "plan_id": plan_id,
                "node_id": node_id,
                "status": "running",
            }
        )
        update["active_task_node_id"] = node_id
        return update

    def decide(state: AgentState) -> dict[str, Any]:
        writer = get_stream_writer()
        turns = int(state.get("turns") or 0)
        results = list(state.get("domain_results") or [])
        queued = list(state.get("pending_handoffs") or [])
        # The round after a domain is the first one that can see how it ended -- and it is also
        # the round that would dispatch the next queued node. A failure clears the queue here,
        # so the fast-path below cannot start a node that was just broadcast as skipped.
        if _finish_active_node(state, results, writer):
            queued = []

        # A queue means the model already said what it wanted on an earlier round. Asking
        # again would be a second opinion nobody asked for, and it would cost a call to get
        # the same answer back.
        if queued:
            if turns >= MAX_DOMAIN_TURNS:
                logger.warning(
                    "handoff 队列还有 %d 个未派发，已达本轮上限 %d",
                    len(queued),
                    MAX_DOMAIN_TURNS,
                )
                return _final_from(results, writer, turns, terminal_reason=TERMINAL_MAX_TURNS)
            plan_id = (state.get("task_plan") or {}).get("plan_id")
            return _dispatch(queued[0], queued[1:], turns, plan_id, results)
        if turns >= MAX_DOMAIN_TURNS:
            return _final_from(results, writer, turns, terminal_reason=TERMINAL_MAX_TURNS)

        # 确认卡上的按钮不是一条新的用户请求：模块已经把结果追加进 domain_results，
        # 只是这条消息的文本是固定的「确认本次操作」。若再交给模型，它会把这句话当成
        # 一个含义不明的新请求，反问「你要确认哪笔操作」。有 settled 结果时直接收尾。
        if _current_request(state) in {"确认本次操作", "取消本次操作"} and any(
            item.get("turn_status") in {"completed", "failed", "unsupported", "unknown"}
            for item in visible_results(results)
        ):
            return _final_from(results, writer, turns)

        request = _current_request(state)
        view = assemble(state, results, aggressive=False) if assembler is not None else None
        prompt = prompt_for(state, results, view)
        awaiting = state.get("awaiting_agent")

        def run(
            prompt_text: str, batch: _DeltaBatch
        ) -> tuple[SupervisorDecision | None, dict[str, Any] | None]:
            """One decision attempt. Returns (decision, terminal-state); exactly one is not None."""

            try:
                decision = _stream_decision(
                    model, prompt_text, request, menu, known, awaiting=awaiting, emit=batch.add
                )
            except StreamTruncated:
                # The stream stopped without a finish signal. Whatever arrived is what the
                # user has; the rest of the decision does not exist.
                if not batch.started:
                    raise
                batch.flush()
                return None, _final_from(
                    results,
                    writer,
                    turns,
                    text=batch.text,
                    terminal_reason=TERMINAL_ABORTED_STREAMING,
                )
            except ModelClientError as error:
                # A failure after the first chunk: starting over would restart an answer the
                # user is already reading, so the round ends with what is on the screen.
                # Nothing shown yet means it is an upstream failure, and the single allowed
                # retry was already spent trying to start the stream (spec §4.6).
                if not batch.started:
                    if error.retryable:
                        raise UpstreamUnavailable(error.code) from error
                    raise upstream_app_error(error) from error
                batch.flush()
                return None, _final_from(
                    results,
                    writer,
                    turns,
                    text=batch.text,
                    terminal_reason=TERMINAL_API_ERROR,
                    error_status=error.status,
                )
            batch.flush()
            if decision.narration:
                # The text was not an answer: it was the model saying what it is about
                # to do. It stays on the screen as activity, not as the turn's reply.
                writer({"event": "narration", "text": decision.narration})
            return decision, None

        batch = _DeltaBatch(writer)
        try:
            decision, terminal = run(prompt, batch)
        except AppError as error:
            # A refusal that never reached the user as text. When the domain already has an
            # answer for them, the round ends with that answer instead of the error: the
            # phrasing failed, not the work.
            if assembler is None or error.details.get("upstream_code") != OVERFLOW_UPSTREAM_CODE:
                if any(item.get("turn_status") for item in results):
                    return _final_from(results, writer, turns)
                raise
            # The transcript was too long for the model. The same round is worth one more
            # attempt against the aggressive view; the queue belongs to the round rather
            # than to the attempt, so only the delta batch is rebuilt (spec §9 note 5).
            view = assemble(state, results, aggressive=True)
            batch = _DeltaBatch(writer)
            try:
                decision, terminal = run(prompt_for(state, results, view), batch)
            except AppError as again:
                if again.details.get("upstream_code") != OVERFLOW_UPSTREAM_CODE:
                    raise
                raise context_limit_error(REASON_OVER_BUDGET) from None
        if terminal is not None:
            return terminal

        outcome = _outcome_from(decision)

        if outcome.agent_id is not None and _already_concluded(results, outcome.agent_id):
            outcome = SupervisorOutcome(agent_id=None, reply=_closing_reply(results))
        if outcome.agent_id is None:
            return _final_from(results, writer, turns, text=outcome.reply)

        # From here the round really delegates, so this is the only place a plan is drawn: a
        # reply-only turn has nothing to order, and the queue fast-path above must not redraw a
        # graph the checkpoint already holds. The plan comes from `decision` because the branch
        # just above -- the only one that can shorten the queue -- also ends the turn.
        plan = _build_task_plan_from_decision(decision)
        # The outcome above is derived from this same decision, so a dispatch always has a plan.
        assert plan is not None
        payload = plan.as_dict()
        writer({"event": "plan", **payload})
        # Each queued handoff runs as the node drawn for it, so the rounds after this one can
        # name the node their task_update finishes.
        queued = [
            {"node_id": node.id, "agent_id": item.agent_id, "instruction": item.instruction}
            for node, item in zip(plan.nodes[1:], decision.queued, strict=True)
        ]
        update = _dispatch(
            {
                "agent_id": outcome.agent_id,
                "instruction": outcome.instruction,
                # The node this handoff is drawn as: dispatching it is what marks it running.
                "node_id": plan.nodes[0].id,
            },
            queued,
            turns,
            plan.plan_id,
            results,
        )
        update["task_plan"] = payload
        return update

    return decide


def _final_from(
    results: list[dict[str, Any]],
    writer: Callable[[dict[str, Any]], Any],
    turns: int,
    *,
    text: str | None = None,
    terminal_reason: str = TERMINAL_COMPLETED,
    error_status: int | None = None,
) -> dict[str, Any]:
    """End the turn with one final event.

    `text` defaults to the closing reply assembled from what the domains collected. It is
    passed explicitly when the text is already on the user's screen: a stream that broke
    halfway has nothing to gain from a second, different ending (spec §4.6).

    The task graph is cleared here too: the turn is over, so a checkpoint that kept the plan
    would tell the next reader that work is still in progress.
    """

    reply = _closing_reply(results) if text is None else text
    payload: dict[str, Any] = {
        "event": "final",
        "text": reply,
        "business_ids": _collect_business_ids(results),
    }
    if terminal_reason != TERMINAL_COMPLETED:
        payload["terminal_reason"] = terminal_reason
    if error_status is not None:
        payload["error_status"] = error_status
    writer(payload)
    return {
        "messages": [{"role": "assistant", "content": reply}],
        "active_agent": None,
        "turns": turns,
        "pending_handoffs": [],
        "task_plan": None,
        "active_task_node_id": None,
    }


# The turn statuses that end a plan. `unsupported` and `unknown` are failures at this
# layer: the graph has one word for "this node did not deliver", and the module's own honest
# wording travels in the frame's `error` -- which is the same summary the user is shown.
_FAILED_NODE_STATUSES = frozenset({TurnStatus.FAILED, TurnStatus.UNSUPPORTED, TurnStatus.UNKNOWN})


def _finish_active_node(
    state: AgentState, results: list[dict[str, Any]], writer: Callable[[dict[str, Any]], Any]
) -> dict[str, Any]:
    """Close the node the previous round ran, and skip the rest of the plan when it failed.

    Called at the top of `decide`, before the queue fast-path: this is the first round that can
    see how the last one ended, and it is also the round that would dispatch the next queued
    node -- which must not start once this one failed.

    The result has to belong to the node's own owner. A round can end on a different domain's
    answer, and closing a node on someone else's result would report work that never happened.
    A pause is not an ending either: the node is parked, not finished.

    The node id is deliberately left in place after the frame, so a second close of the same
    node would be a duplicate. That duplicate is unreachable for two reasons, and a change to
    either one would make it reachable: each domain appends exactly one `domain_results`
    entry per call, and `_already_concluded` refuses to hand a domain the turn again once
    its result carries a terminal status. A domain that appended two results for one call
    would close its node twice.

    The return value is only ever the queue a failure has to clear -- the caller applies nothing
    else from it. Returning nothing keeps the caller from merging a `None` node id into an
    update whose whole job is to name the *next* node.
    """

    node_id = state.get("active_task_node_id")
    plan = state.get("task_plan") or {}
    plan_id = plan.get("plan_id")
    node = next((item for item in plan.get("nodes") or [] if item.get("id") == node_id), None)
    if node is None or plan_id is None or not results:
        return {}

    last = results[-1]
    if node.get("owner") != last.get("agent_id"):
        return {}
    status = last.get("turn_status")
    summary = str(last.get("summary") or "")

    if status == TurnStatus.COMPLETED:
        writer(
            {
                "event": "task_update",
                "plan_id": plan_id,
                "node_id": node_id,
                "status": "done",
                "artifact": {"summary": summary},
            }
        )
        return {}
    if status not in _FAILED_NODE_STATUSES:
        return {}

    writer(
        {
            "event": "task_update",
            "plan_id": plan_id,
            "node_id": node_id,
            "status": "failed",
            "error": summary,
        }
    )
    # The plan ended with this node: what it queued will never run, and a node left pending on
    # the page would be a promise this turn is not keeping.
    for item in state.get("pending_handoffs") or []:
        skipped = item.get("node_id")
        if skipped is None:
            continue
        writer(
            {
                "event": "task_update",
                "plan_id": plan_id,
                "node_id": skipped,
                "status": "skipped",
            }
        )
    return {"pending_handoffs": []}


def _already_concluded(results: list[dict[str, Any]], agent_id: str) -> bool:
    """Has this agent already said everything it is going to say?

    Two shapes count as a conclusion: a module that declares no tools (its answer is the
    boundary notice) and a module that ran its own whole turn (its answer carries a
    terminal turn_status). Re-sending the request would reach the same conclusion.
    """

    for item in results:
        if item.get("agent_id") != agent_id:
            continue
        if item.get("summary") == NO_TOOLS_SUMMARY:
            return True
        if item.get("turn_status") in TERMINAL_TURN_STATUSES:
            return True
    return False


LOCKED_GUIDANCE = "查询功能仍然可用，可在演示面板中解除锁定"
LOCKED_EXPLANATION = "资金工具已锁定，查询仍可用，可在演示面板中解除锁定。"


def _join_conclusions(conclusions: list[str]) -> str:
    """用分号连接领域结论，并先去掉前一条末尾的句号，避免出现「。；」。"""

    if len(conclusions) == 1:
        return conclusions[0]
    ending = conclusions[-1][-1] if conclusions[-1][-1] in "。！？!?" else ""
    body = "；".join(item.rstrip("。；;！？!? ") for item in conclusions)
    return body + ending


def _closing_reply(results: list[dict[str, Any]]) -> str:
    """将各领域的结论汇总收尾，优先使用提炼的风险说明与锁定态引导。

    设计与架构协同说明：
    1. 视觉卡片与文本分工：
       在资金写操作的准备阶段（如转账准备、生日下单准备），底层已通过 SSE 发送带有完整金额、收款人等
       结构化表单的 `TurnConfirmation` 确认卡。若文本气泡再次原样复读确认卡三行文本（包含“点击确认后才会发起”
       等交互废话），会造成视觉冗余。此时优先通过 risk_sentence() 提炼风险核验原由，让对话文本聚焦解释“为什么
       需要核验/黄标”。
    2. 普通业务与查询兜底：
       对于查询操作或常规完成结论，risk_sentence() 返回 None，完整保留各领域输出的业务原话。
    3. 资金工具锁定态处理：
       检测到包含「已锁定」或「资金工具已锁定」时，丰富提示用户“查询功能仍然可用，可在演示面板中解除锁定”，
       并防范“资金工具已锁定...资金工具已锁定”口吃式重复拼接。
    4. 结论去重与多领域连接：
       所有有效结论用「；」连接，前一条末尾的句号先去掉，避免出现「。；」；全空时返回标准未完成兜底。
    """
    conclusions: list[str] = []

    # 同一领域在确认后既有旧的 waiting_confirmation，又有新的 completed；收尾只读后者，
    # 否则用户会看到“请确认这笔转账”和“转账已完成”同时出现。checkpoint 的 append-only
    # 历史不动，这里只过滤展示层。
    for item in visible_results(results):
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue

        # 1. 资金工具锁定态处理（防口吃重复，自然引导）
        if "已锁定" in summary or "资金工具已锁定" in summary:
            if "解除锁定" in summary or "演示面板" in summary:
                lock_sentence = summary
            else:
                lock_sentence = f"{summary}（{LOCKED_GUIDANCE}）"
            if lock_sentence not in conclusions:
                conclusions.append(lock_sentence)
            continue

        # 2. 风险分级与核验原因优先提取
        risk = risk_sentence(summary)
        chosen = risk if risk is not None else summary
        if chosen not in conclusions:
            conclusions.append(chosen)

    if not conclusions:
        return "抱歉，这个请求暂时没能完成，请换一种说法再试一次。"
    return _join_conclusions(conclusions)


def reset_goal_node(state: AgentState) -> dict[str, Any]:
    """The anchor a goal reset is attributed to.

    It has no side effects and is never entered from `START`: its only job is to give the
    runtime a legal node to pass as `as_node`, so `aupdate_state` replaces the pending task
    of an abandoned goal instead of leaving it to swallow the next ordinary message.
    """

    del state
    return {}


def _collect_business_ids(results: list[dict[str, Any]]) -> list[str]:
    """The business ids this request produced or read, deduped, in first-seen order.

    Only the values are published. The keys (`account_id`, `transfer_id`) are internal
    names, and putting them in front of a user is the same mistake as leaking an agent id.
    """

    seen: list[str] = []
    for item in results:
        ids = item.get("business_ids")
        if not isinstance(ids, dict):
            continue
        for value in ids.values():
            if isinstance(value, str) and value and value not in seen:
                seen.append(value)
    return seen


def supervisor_route(state: AgentState) -> str:
    return state.get("active_agent") or DONE


def build_supervisor(
    model: ModelClient,
    domains: list[DomainSpec],
    *,
    assembler: ContextAssembler | None = None,
    turn: TurnContext | None = None,
    context: TrustedContext | None = None,
    client: Any | None = None,
) -> StateGraph:
    """Compile the Supervisor with each domain as a subgraph node.

    LangGraph owns the subgraph checkpoint namespace: no `checkpoint_ns`, no
    `subtask_id` and no second resume protocol is built here.
    """

    if not domains:
        raise ValueError("Supervisor needs at least one domain agent")

    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node(model, domains, assembler, turn))
    for spec in domains:
        graph.add_node(
            spec.agent_id,
            build_domain_graph(model, spec, context, assembler, turn, client),
        )
    # Reachable only as the target of `aupdate_state(..., as_node="reset_goal")`. It is
    # deliberately not wired from START and not added to the Supervisor's route, so an
    # ordinary turn can never wander into it.
    graph.add_node("reset_goal", reset_goal_node)
    graph.add_edge(START, "supervisor")
    graph.add_edge("reset_goal", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        supervisor_route,
        {**{spec.agent_id: spec.agent_id for spec in domains}, DONE: END},
    )
    for spec in domains:
        graph.add_edge(spec.agent_id, "supervisor")
    return graph


def build_domain_graph(
    model: ModelClient,
    spec: DomainSpec,
    context: TrustedContext | None = None,
    assembler: ContextAssembler | None = None,
    turn: TurnContext | None = None,
    client: Any | None = None,
) -> Any:
    """One domain's graph, compiled as a subgraph node.

    The rule is one line: a domain that carries a `graph_builder` owns its graph. The
    builder is called here with `assembler`, `turn` and `client`, so a module can open
    the conversation scope itself and bind its own bank adapter; a module that does not need
    them ignores them. Declaring a builder means owning the graph, tools or no tools -- the
    builder decides what to do with its own tool menu.

    A domain that carries business tools but no builder is refused, not served: A5 §1 allows
    one orchestration path per domain, and a silent fallback would be a second one.

    A domain with neither tools nor a builder still compiles -- `minimal_shell_graph`, the
    model-free conclusion a tool-less shell runs -- so a synthetic spec in a test can be built
    without declaring anything.

    It is compiled without its own checkpointer: LangGraph hands the Supervisor's checkpointer
    down, which is what keeps the subgraph's checkpoint namespace out of this module's hands.
    """

    if spec.declaration.graph_builder is not None:
        return spec.declaration.graph_builder(
            model, spec, assembler=assembler, turn=turn, client=client
        )
    if spec.handle.tools:
        raise ValueError(
            f"domain {spec.agent_id!r} declares business tools but no graph_builder; a domain "
            "with business tools must declare a graph_builder (roadmap A5 §1)"
        )
    return minimal_shell_graph(model, spec, assembler=assembler, turn=turn, client=client)
