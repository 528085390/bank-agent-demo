"""理财内部的对话工作流（LangGraph）。

节点十一个，回边四条，所以"多次查询在理财内部完成""补信息也在理财内部完成""推荐与对比基于银行
事实""申购／赎回先预览再确认"都是图的形状，而不是提示词里的一句请求：

    START → understand → decide ─┬→ call_read  → decide   （循环，受 MAX_STEPS 约束）
                                 ├→ set_slots  → decide   （循环，同上）
                                 ├→ submit_risk ─┬→ decide （问卷保存成功，继续决策）
                                 │               └→ END    （保存失败，诚实收尾）
                                 ├→ recommend ──┬→ decide （有候选／落选原因，继续决策）
                                 │              └→ END    （要先问问卷或补齐条件）
                                 ├→ compare ────┬→ decide （对比结论，继续决策）
                                 │              └→ END    （银行失败）
                                 ├→ prepare_purchase ─┬→ END    （待确认快照／缺槽位／失败）
                                 │                    └→ decide （预检没过或银行失败，如实解释）
                                 ├→ prepare_redeem ───┬→ END    （待确认快照／缺槽位／失败）
                                 │                    └→ decide （预检没过或银行失败，如实解释）
                                 ├→ ask_user   → END
                                 └→ finish     → END
                                   （failure 直接 END）

申购与赎回的**写**都不在这张图里：图只到"待确认"为止，确认、验证码与资金写由运行时的受控节点
完成（`application/runtime.py`）。所以模型无论怎么被诱导，都拿不到一个能直接写钱的路径。

读流程在每次 `handle` 时按调用方重建（`build_read_graph` 收下 `context`），身份因此只存在于闭包里，
不进入 state。基座 `AgentRuntime` 的立场相同：句柄带着某个调用方的身份，图就不能跨会话共享。

领域图（`build_wealth_graph`）是基座交给这个模块的入口：基座只把 model／spec／assembler／turn／
client 交进来，图自己跑完一轮（`decide → wait → process`）。跨轮的槽位、待办和待确认的写入全部
落在图 state 的 `wealth_*` 字段上，由基座的检查点保存——所以进程里没有会话表，两个进程之间也没有：
记忆属于 thread。

依赖方向：`workflow → infrastructure/llm/domain/contracts`。`application` 只走函数内导入：它在
模块级导入本模块。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.declaration import DomainSpec
from app.agents.sdk import (
    CONFIRMATION_KIND,
    INVALID_DECISION_SUMMARY,
    DomainOutcome,
    TurnAction,
    TurnConfirmation,
    TurnResult,
    TurnStatus,
    action_kind,
    awaiting,
    call_tool,
    conclude_turn,
    conclude_without_tool,
    current_request,
    domain_scope,
    next_attempt,
    pending_context,
    read_answer,
    wait_for_user,
)
from app.context.handoff import Handoff
from app.integration.model_client import ModelClient
from app.modules.base import ERROR_MESSAGES, ToolData, ToolFailure
from app.modules.wealth.contracts.bank import WealthBank
from app.modules.wealth.contracts.context import ConversationContext, ConversationMessage
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import WealthResponse, WealthStatus
from app.modules.wealth.domain.rules import questionnaire_lines
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.modules.wealth.infrastructure.tools import READ_TOOLS, ReadTool
from app.modules.wealth.llm.prompts import read_overhead
from app.modules.wealth.workflow.nodes import (
    ask_user_node,
    call_read_node,
    compare_node,
    decide_node,
    finish_node,
    prepare_purchase_node,
    prepare_redeem_node,
    recommend_node,
    route_after_decide,
    route_after_evidence,
    route_after_prepare,
    set_slots_node,
    submit_risk_node,
    understand_node,
)
from app.modules.wealth.workflow.state import (
    MAX_STEPS,
    PendingData,
    SessionData,
    WealthState,
    WealthWorkflowState,
    merge_session,
    session_of,
)
from app.tools.protocol import ErrorCode, ToolStatus, TrustedContext

__all__ = [
    "build_read_graph",
    "build_wealth_graph",
    "invoke_read_workflow",
    "recursion_limit",
]

logger = logging.getLogger(__name__)

# 决策之后的边。"end" 是 `route_after_decide` 在失败或没有决策时给的出口。
ROUTES: Mapping[str, str] = {
    "call_read": "call_read",
    "set_slots": "set_slots",
    "submit_risk": "submit_risk",
    "recommend": "recommend",
    "compare": "compare",
    "prepare_purchase": "prepare_purchase",
    "prepare_redeem": "prepare_redeem",
    "ask_user": "ask_user",
    "finish": "finish",
    "end": END,
}

# 银行节点之后的边：已经有结论（追问或失败）就结束，否则回到决策节点。
#
# `needs_evidence` 也回到决策节点：推荐／对比／预览缺证据时节点只报告缺什么，由模型决定下一步
# 查哪个工具或问什么（计划任务3 §3.2 第 4 条），而不是在这里结束整个处理。
EVIDENCE_ROUTES: Mapping[str, str] = {"decide": "decide", "end": END}

# 申购预览之后的边：有快照（等确认）、有追问或失败就结束，否则回决策节点让模型如实解释。
PREPARE_ROUTES: Mapping[str, str] = {"decide": "decide", "end": END}


def recursion_limit() -> int:
    """LangGraph 自己的递归上限，只是兜底。

    真正的边界在节点里（超限时节点自己收尾，不会走到 LangGraph 的中止），这里给节点预算加 3
    的余量：LangGraph 按超步计数，一次"understand + decide + call_read 循环到收尾"的最长路径
    是 12 次工作推进加上一次收尾，实测需要 15。数值太小会在预算生效之前就抛
    `GraphRecursionError`，`test_the_step_budget_bounds_the_loop` 走的就是这条最长路径。
    """

    return MAX_STEPS + 3


def build_read_graph(
    model: ModelClient,
    bank: WealthBank,
    context: TrustedContext,
    *,
    tools: Mapping[str, ReadTool] = READ_TOOLS,
) -> Any:
    """编译一个绑定到某个调用方的对话工作流。

    `context` 只进 `call_read` 的闭包：它带银行客户号，属于一次调用，不属于可保存的状态。
    """

    graph = StateGraph(WealthWorkflowState)
    graph.add_node("understand", understand_node())
    graph.add_node("decide", decide_node(model, tools=tools))
    graph.add_node("call_read", call_read_node(bank, context, tools=tools))
    graph.add_node("set_slots", set_slots_node())
    graph.add_node("submit_risk", submit_risk_node(bank, context))
    graph.add_node("recommend", recommend_node(bank, context))
    graph.add_node("compare", compare_node(bank, context))
    graph.add_node("prepare_purchase", prepare_purchase_node(bank, context))
    graph.add_node("prepare_redeem", prepare_redeem_node(bank, context))
    graph.add_node("ask_user", ask_user_node())
    graph.add_node("finish", finish_node())
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "decide")
    graph.add_conditional_edges("decide", route_after_decide, dict(ROUTES))
    graph.add_edge("call_read", "decide")
    graph.add_edge("set_slots", "decide")
    for node in ("submit_risk", "recommend", "compare"):
        graph.add_conditional_edges(node, route_after_evidence, dict(EVIDENCE_ROUTES))
    graph.add_conditional_edges("prepare_purchase", route_after_prepare, dict(PREPARE_ROUTES))
    graph.add_conditional_edges("prepare_redeem", route_after_prepare, dict(PREPARE_ROUTES))
    graph.add_edge("ask_user", END)
    graph.add_edge("finish", END)
    return graph.compile()


def invoke_read_workflow(graph: Any, state: WealthWorkflowState) -> dict[str, Any]:
    """跑一次工作流，返回最终状态。

    没有检查点，所以 `invoke` 的返回值就是这一次处理的全部结果。
    """

    return graph.invoke(state, {"recursion_limit": recursion_limit()})


# ---------------------------------------------------------------------------
# 领域图：整轮处理、原生工具调用、跨轮记忆都在图里（spec 2 §10）
# ---------------------------------------------------------------------------

# 主助手这条通道上，写流程只走到"待确认"：状态、金额、产品、账户与待办编号都经
# `TurnResult.confirmation` 送到前端，用户点击后带着 `pending_id` 回到图里。旧验证码入口已经
# 停用（点击确认取代了"确认 + 输入验证码"），运行时不再产生 `WAITING_VERIFICATION`；真出现了
# 就如实说这条流程没接，而不是让用户对着一个不会前进的待办继续回答。
UNSUPPORTED_WRITE_SUMMARY = (
    "这条理财写流程还没有接入主助手，本次不能办理。产品、持仓和风险测评可以继续查询。"
)
NO_SUMMARY = "本次没有可以回复的内容。"
NO_CONFIRMATION_SUMMARY = "这笔待确认操作的展示信息不完整，没有生成确认框，本次没有办理任何操作。"

# 理财状态到整轮状态的映射。`UNKNOWN` 有自己的一条分支（见 `_turn_result`）：它绝不能落到
# `get` 的默认值 FAILED——把一笔结果未知的交易说成"没办成"，用户会去重新下单，那是真正的资金
# 风险。
_STATUS = {
    WealthStatus.COMPLETED: TurnStatus.COMPLETED,
    WealthStatus.WAITING_INPUT: TurnStatus.WAITING_INPUT,
    WealthStatus.WAITING_CONFIRMATION: TurnStatus.WAITING_CONFIRMATION,
    WealthStatus.UNKNOWN: TurnStatus.UNKNOWN,
    WealthStatus.CANCELLED: TurnStatus.COMPLETED,
    WealthStatus.FAILED: TurnStatus.FAILED,
}

UNKNOWN_TURN_SUMMARY = (
    "这笔理财交易的结果暂时未知：银行没有返回明确结果，程序不会重复下单。请稍后按原单核实。"
)


def _scoped_request(state: WealthState) -> str:
    """本轮交给理财的请求：优先用总调度划定的任务，独立运行时才退回用户原话。

    总调度可能把一句跨领域的话拆成多个节点。领域只应回答自己的节点；把完整用户原话交给
    模型时，模型会顺手评论“我没有查询收款人的功能”，而同一个最终答复里另一领域已经查到了
    收款人，于是自相矛盾。goal 就是这次 handoff 的范围；没有 goal 的独立入口（CLI、
    领域单测）仍按原话处理。
    """

    goal = str(state.get("goal") or "").strip()
    return goal or current_request(state)


def build_wealth_graph(
    model: ModelClient,
    spec: DomainSpec,
    *,
    assembler: Any = None,
    turn: Any = None,
    client: Any = None,
    clock: Any = None,
    writes: Any = None,
) -> Any:
    """编译理财的领域图：一整轮处理、原生工具调用、跨轮记忆都在图里。

    基座（`build_domain_graph`）在模块声明了 `graph_builder` 且带着工具时走这条路：它把
    model／spec／assembler／turn／client 交进来，剩下的由这张图自己决定。

    `client` 是**裸的银行客户端**（`Runtime.bank_client`），不是 `WealthBank`：包一层
    `HttpWealthBank` 才是理财自己的银行契约。`client=None` 必须直接报错——没有银行客户端就没有
    可查的产品、持仓或待办，静默退回一个默认银行会让"查不到"看起来像"没有"。

    `clock` 不进 state（spec 2 §2.5）：它属于图构建的闭包。默认是墙钟；测试注入 `TestClock`
    就能让一个记住的待办过期，而不用等五分钟。

    `writes` 是组合根注入的那一把写门面（`wealth/__init__.py` 的 `bind_wealth` 经
    `base.bind_writes` 用 `functools.partial` 绑进来，装配由组合根触发、由模块自己完成），
    交给 `HttpWealthBank` 之后，申购／赎回的资金写与转账共用同一套门面策略与审计钩子。
    """

    if client is None:
        raise ValueError(
            "build_wealth_graph 需要一个银行客户端：没有 client 就没有可查的产品、持仓或待办。"
        )

    # 函数内导入：`application/runtime.py` 在模块级导入本模块，顶层再导入 `application` 就会成环。
    from app.modules.wealth.application.factory import build_wealth_agent
    from app.modules.wealth.application.runtime import ProcessClock

    agent = build_wealth_agent(
        model,
        HttpWealthBank(client, writes=writes),
        clock or ProcessClock(),
        tools=_gateway_tools(spec),
    )

    def lap(
        state: WealthState, request: WealthRequest
    ) -> tuple[WealthResponse, SessionData, PendingData | None, tuple[dict[str, Any], ...]]:
        """一轮：进 `domain_scope`，把这一轮的状态交给 `WealthAgent.handle`。"""

        context = spec.handle.trusted_context
        message = current_request(state)
        session = session_of(state)
        pending = state.get("wealth_pending") or None
        with domain_scope(spec, state, assembler, turn, message) as handoff:
            conversation = _conversation(handoff)
            response, session, pending = agent.handle(
                context,
                request,
                session=session,
                pending=pending,
                conversation=conversation,
            )
            if response.status is WealthStatus.WAITING_VERIFICATION:
                # 旧验证码交互已经停用，运行时不会再产生这个状态。真出现了就把它撤下（见
                # `_turn_result` 的 UNSUPPORTED），而不是留一个永远不会前进的待办。
                _, session, pending = agent.handle(
                    context,
                    WealthRequest(kind=WealthRequestKind.CANCEL),
                    session=session,
                    pending=pending,
                    conversation=conversation,
                )
            if handoff is not None:
                # 结构化记忆经作用域回传：它按封闭白名单校验，并带上来源账本消息。没有记忆的响应
                # 什么都不发布。
                handoff.publish(response.context_update)
            updates = handoff.updates() if handoff is not None else ()
        return response, session, pending, updates

    def land(
        state: WealthState,
        response: WealthResponse,
        session: SessionData,
        pending: PendingData | None,
        updates: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        """把一轮的结果落到 state 上：结论、会话、待办，必要时还有那个问题。"""

        writer = get_stream_writer()
        result = _turn_result(response)
        conclusion = conclude_turn(spec, result, writer, state, updates)
        merge_session(conclusion, session)
        conclusion["wealth_pending"] = pending
        if result.status is TurnStatus.WAITING_INPUT:
            # `conclude_turn` 对 WAITING_INPUT 只写"在等谁"：它不写问题。问题必须自己交给
            # `awaiting`，否则 `route` 读到的 turn_phase 还是 decide，直接 END，用户永远等不到
            # 那张输入卡。字段名只能来自待办自己记的 missing：`TurnResult` 不带槽位名。
            conclusion.update(
                awaiting(
                    spec,
                    DomainOutcome(
                        ask={
                            "fields": list((pending or {}).get("missing") or ()),
                            "display": result.summary,
                        }
                    ),
                    writer,
                )
            )
        return conclusion

    def decide(state: WealthState) -> dict[str, Any]:
        """图的第一站：把这一轮当成一条新消息交给理财。"""

        get_stream_writer()({"event": "agent_start", "agent_id": spec.agent_id})
        response, session, pending, updates = lap(
            state, WealthRequest(kind=WealthRequestKind.MESSAGE, text=_scoped_request(state))
        )
        return land(state, response, session, pending, updates)

    def process(state: WealthState) -> dict[str, Any]:
        """回答一站：检查点上那次点击或那句补充，落到同一轮处理上。"""

        writer = get_stream_writer()
        question = state.get("pending_question") or {}
        answer, problem = read_answer(state.get("pending_answer"), question)
        if problem:
            # 没读懂就再问一次，attempt 加一：和基座重问的规则一致。
            return awaiting(
                spec,
                DomainOutcome(ask=question),
                writer,
                question=question,
                reask=f"{problem}请重新提供。",
                attempt=next_attempt(question),
                question_id=question.get("question_id"),
            )
        if question.get("kind") == CONFIRMATION_KIND:
            decision = action_kind(question, answer)
            if decision is None:
                # 没指向预览上的选项：不猜、不提交，如实说明。
                return conclude_without_tool(
                    spec, INVALID_DECISION_SUMMARY, ToolStatus.DENIED, writer
                )
            confirm = decision is TurnAction.CONFIRM
            response, session, pending, updates = lap(
                state,
                WealthRequest(
                    kind=WealthRequestKind.CONFIRM if confirm else WealthRequestKind.CANCEL,
                    pending_id=str((question.get("confirmation") or {}).get("pending_id") or ""),
                    # 验证码只跟着确认走，而且只能来自基座从这次调用里读到的那个值：它随
                    # TurnContext 闭包进来，从不进图 state，也不进检查点。
                    verification_code=(
                        turn.verification_code if turn is not None and confirm else None
                    ),
                ),
            )
            return land(state, response, session, pending, updates)
        session = session_of(state)
        response, session, pending, updates = lap(
            state,
            WealthRequest(
                kind=WealthRequestKind.ANSWER,
                text=pending_context(question, answer),
                pending_id=session.get("pending_id"),
            ),
        )
        if (
            response.status is WealthStatus.FAILED
            and response.error_code == ErrorCode.INVALID_STATE
        ):
            # 记住的待办已经不在了（过期，或被槽位变化作废）。这句话不是回答，就当成新消息说：
            # 用户的下一条消息不该被一个已经死掉的待办吞掉。
            response, session, pending, updates = lap(
                state,
                WealthRequest(kind=WealthRequestKind.MESSAGE, text=_answer_text(state, answer)),
            )
        return land(state, response, session, pending, updates)

    def route(state: WealthState) -> str:
        """等用户就停，否则这一轮就结束了。"""

        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(WealthState)
    graph.add_node("decide", decide)
    graph.add_node("wait", wait_for_user)
    graph.add_node("process", process)
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
    return graph.compile()


def _gateway_tools(spec: DomainSpec) -> dict[str, ReadTool]:
    """把理财的读工具换成走基座网关的同一批工具。

    工具名、描述与入参 schema 都不变，变的只是执行：真正调银行的是基座网关里的理财 handler
    （`public_handlers` 绑的那个），这里只负责把结果翻回 `ToolData`。于是模型看到的每一次查询都
    带上 `tool_call`／`tool_result` 事件，重试与闸门也留在基座里。

    这里**不重试**：`workflow/nodes.py` 的 `call_read_node` 已经用 `read_with_one_retry` 包了一层，
    再包一层会让一次瞬时失败打四次银行。
    """

    def bind(tool: ReadTool) -> ReadTool:
        def run(bank: Any, context: Any, arguments: Mapping[str, Any]) -> ToolData:
            del bank, context
            writer = get_stream_writer()
            outcome = DomainOutcome(tool_name=tool.spec.name, arguments=dict(arguments))
            result = call_tool(spec, outcome, writer, idempotency_key=None)
            if result.status is not ToolStatus.SUCCEEDED or not isinstance(result.data, dict):
                raise ToolFailure(
                    result.error_code or ErrorCode.INTERNAL,
                    result.error or ERROR_MESSAGES[ErrorCode.INTERNAL],
                    retryable=result.retryable,
                )
            return ToolData(**result.data)

        return ReadTool(spec=tool.spec, method=tool.method, run=run)

    return {name: bind(tool) for name, tool in READ_TOOLS.items()}


def _turn_result(response: WealthResponse) -> TurnResult:
    """把一次理财响应翻成整轮结果。纯映射，没有 I/O——补偿动作在节点里。

    五条分支的顺序就是优先级：`UNKNOWN` 最先（结果未知绝不能变成"没办成"），然后是确认卡片、
    旧验证码状态，最后才查 `_STATUS`。
    """

    facts = dict(response.facts)
    business_ids = dict(response.business_ids)
    if response.status is WealthStatus.UNKNOWN:
        return TurnResult(
            status=TurnStatus.UNKNOWN,
            summary=f"{UNKNOWN_TURN_SUMMARY}（{response.summary}）",
            facts=facts,
            business_ids=business_ids,
        )
    if response.status is WealthStatus.WAITING_CONFIRMATION:
        confirmation = _confirmation(response)
        if confirmation is None:
            logger.error("wealth turn: the preview carries no usable confirmation projection")
            return TurnResult(status=TurnStatus.FAILED, summary=NO_CONFIRMATION_SUMMARY)
        return TurnResult(
            status=TurnStatus.WAITING_CONFIRMATION,
            summary=_preview_text(response),
            facts=facts,
            business_ids=business_ids,
            confirmation=confirmation,
        )
    if response.status is WealthStatus.WAITING_VERIFICATION:
        return TurnResult(status=TurnStatus.UNSUPPORTED, summary=UNSUPPORTED_WRITE_SUMMARY)
    return TurnResult(
        status=_STATUS.get(response.status, TurnStatus.FAILED),
        summary=response.summary
        or (response.pending.prompt if response.pending else "")
        or NO_SUMMARY,
        facts=facts,
        business_ids=business_ids,
    )


def _preview_text(response: WealthResponse) -> str:
    """确认卡片的正文：模型的话优先，其次是待办自己的提示，最后是诚实的一句。"""

    return (
        response.summary
        or (response.pending.prompt if response.pending is not None else "")
        or NO_SUMMARY
    )


def _confirmation(response: WealthResponse) -> TurnConfirmation | None:
    """把响应的确认投影翻成整轮的确认卡片；投影不完整就返回 None。

    金额、产品、账户和按钮文案都由银行事实推导，缺一项就是一张会误导用户的卡片：宁可收尾，
    也不放一张点下去不知道会提交什么的卡片。
    """

    projection = response.confirmation
    pending = response.pending
    if projection is None or pending is None:
        return None
    try:
        return TurnConfirmation(
            pending_id=pending.pending_id,
            operation=projection.operation,
            title=projection.title,
            amount_minor=projection.amount_minor,
            product_name=projection.product_name,
            account_display=projection.account_display,
            confirm_label=projection.confirm_label,
            cancel_label=projection.cancel_label,
            expires_at=projection.expires_at,
            requires_code=projection.requires_code,
            code_label=projection.code_label,
        )
    except ValueError:
        logger.error("wealth turn: the confirmation projection is unusable")
        return None


def _conversation(handoff: Handoff | None) -> ConversationContext | None:
    """从作用域里取这一段对话；没有对话就没有可带的上下文。"""

    if handoff is None or handoff.conversation_id is None:
        return None
    view = handoff.view(prompt_overhead=read_overhead(questionnaire_lines()))
    return ConversationContext(
        # 会话编号取自作用域，不取自视图：没有装配器时视图按"没有会话"渲染，编号会是 None，
        # 而这里的契约要求一个非空编号。
        conversation_id=handoff.conversation_id,
        current_message_id=view.current_message_id,
        current_sequence=view.current_sequence,
        current_user_text=view.current_user_text,
        summary_text=view.summary_text,
        history_text=view.history_text,
        recent_messages=_messages(view),
        domain_context=view.domain_context,
        context_sources=view.context_sources,
    )


def _messages(view: Any) -> tuple[ConversationMessage, ...]:
    """只留用户与助手的原话：系统提示与工具记录不进上下文。"""

    messages: list[ConversationMessage] = []
    for row in getattr(view, "recent_messages", ()) or ():
        role = getattr(row, "role", None)
        content = getattr(row, "content", None)
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        messages.append(
            ConversationMessage(
                role=role,
                content=content,
                message_id=getattr(row, "message_id", None),
                sequence=getattr(row, "sequence", None),
            )
        )
    return tuple(messages)


def _answer_text(state: WealthState, answer: Mapping[str, Any]) -> str:
    """用户这次回答的原话。

    走 API 的那条路把原话放在 `pending_answer["content"]` 里（`TurnContext`）；走纯
    client_context 的 resume 路只有结构化答案，就把值拼起来当作那句话。
    """

    raw = state.get("pending_answer")
    if isinstance(raw, Mapping):
        content = raw.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return " ".join(
        str(value)
        for value in answer.values()
        if isinstance(value, (str, int, float)) and str(value).strip()
    )
