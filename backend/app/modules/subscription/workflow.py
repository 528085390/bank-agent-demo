"""A subscription-only workflow. A subscription can be queried, or changed after confirmation.

The module owns its own graph so its turns, its conversation slots and its pending question
live in the graph state the base checkpoints -- the same shape card, wealth and transfer use.
It declares no `turn`: one request must not be answered twice.

A write tool never changes anything here: it freezes a snapshot and parks on a confirmation
card. Only the confirmation click executes it, through the hidden `subscription_execute` tool.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import DecisionError, parse_domain_decision
from app.agents.grounding import guard_summary
from app.agents.sdk import (
    MAX_TOOL_ROUNDS,
    DomainOutcome,
    TurnConfirmation,
    TurnStatus,
    awaiting,
    call_tool,
    domain_node,
    domain_outcome,
    may_retry,
    overflow_outcome,
    read_answer,
    tool_envelope,
    tool_outcome,
    wait_for_user,
)
from app.agents.state import AgentState, idle_work_state
from app.agents.toolcalls import StreamTruncated, collect_stream
from app.agents.upstream import UpstreamUnavailable
from app.modules.money import format_minor
from app.modules.subscription.tools import (
    FREQUENCY_LABELS,
    matched_subscriptions,
    subscription_label,
)
from app.tools.protocol import SideEffect, ToolStatus
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema

logger = logging.getLogger(__name__)

# How a subscription turn's ending is named in `domain_results` and `subtask_end`. The
# Supervisor reads `turn_status` back to decide whether an agent has already concluded, so a
# turn without one looks unfinished forever. A denied operation is "unsupported", not a failure.
_SUBSCRIPTION_TURN_STATUS = {
    ToolStatus.SUCCEEDED: TurnStatus.COMPLETED.value,
    ToolStatus.FAILED: TurnStatus.FAILED.value,
    ToolStatus.DENIED: TurnStatus.UNSUPPORTED.value,
}

# What one tool call means for this activation: concluding is a return value, only a read
# round goes back to the model, and a write freezes a snapshot and parks on a confirmation.
_READ_ROUND = "read"
_PARKED = "parked"
_DONE = "done"

# A frozen snapshot is valid for this long, judged on the wall clock. The bank's clock is
# simulated and may stand still, so expiry cannot be read from it.
PENDING_EXPIRY_MINUTES = 5

# How each frozen operation is named on the confirmation card.
OPERATION_LABELS = {
    "cancel": "取消订阅",
    "pause": "暂停订阅",
    "resume": "恢复订阅",
    "charge": "立即扣款",
}

# How the model spells "leave this parameter alone". The schema forbids extra properties, so a
# placeholder has to be dropped before validation rather than read as a malformed call.
_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})

# The tool the confirmation click calls. It stays in the bound catalog the Gateway checks, but
# it must never travel to the model -- neither in the tool menu nor as a name it can call.
_HIDDEN = frozenset({"subscription_execute"})


def authorization_question(action):
    """The confirmation card for one frozen snapshot.

    `fields` must stay non-empty: the front end drops an interrupt that carries no field to
    answer with, so an empty list would leave the click nothing to submit.
    """

    operation = action.get("operation") or ""
    label = OPERATION_LABELS.get(operation, operation or "订阅操作")
    product = action.get("product_name") or "该订阅"
    merchant = action.get("merchant_name")
    target = f"{product}（{merchant}）" if merchant else product
    account = action.get("account_display") or "未指定账户"
    if operation == "charge":
        amount_line = f"本次立即扣款 {format_minor(action['amount_minor'])}，扣款账户 {account}。"
    else:
        frequency = (
            FREQUENCY_LABELS.get(action.get("frequency")) or action.get("frequency") or "每期"
        )
        due = action.get("next_due_date") or "未安排"
        amount_line = (
            f"{frequency}扣款 {format_minor(action['amount_minor'])}，下次扣款 {due}，"
            f"扣款账户 {account}。"
        )
    lines = [f"请确认：{label} · {target}。", amount_line]
    reason = action.get("reason")
    if reason:
        # The engine's own wording, present when `prepare` froze a verdict onto the snapshot.
        # It is information only: a subscription has no code channel, so this line never
        # gates the click (R7).
        lines.append(f"风险提示：{reason}")
    lines.append("确认后由代码执行；选择取消不会修改订阅。")
    return {
        "kind": "confirmation",
        "question_id": f"q_{uuid4().hex}",
        "agent_id": "subscription",
        "attempt": 1,
        "display": "\n".join(lines),
        "fields": [
            {
                "name": "decision",
                "label": "操作决定",
                "type": "select",
                "required": True,
                "options": ["确认", "取消"],
            }
        ],
        "confirmation": TurnConfirmation(
            pending_id=action["pending_id"],
            operation=operation,
            title=label,
            amount_minor=action["amount_minor"],
            product_name=product,
            account_display=account,
            expires_at=action.get("expires_at"),
        ).as_payload(),
    }


def expired(action, now=None):
    """True once a frozen snapshot's wall-clock window has closed.

    Judged on the wall clock, not the bank's: the bank clock is simulated and may stand still,
    so a snapshot could otherwise never expire. A missing or unparseable deadline counts as
    expired -- a malformed timestamp must not license an execution.
    """

    value = (action or {}).get("expires_at")
    if not isinstance(value, str) or not value:
        return True
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return True
    moment = now or datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= deadline


def declared_arguments(arguments):
    """Drop the values that mean "not provided" before the schema sees them."""

    return {
        key: value
        for key, value in arguments.items()
        if value is not None
        and not (isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS)
    }


def _needs_selection(arguments):
    """Whether a write call needs the graph to resolve its target before freezing.

    A `subscription_id` is the bank's primary key, so the write tool can read it directly and
    `_select_subscription` still checks any other selector against that same row. Without an id
    the graph must list the caller's rows and decide 0 / 1 / many itself.
    """

    return not arguments.get("subscription_id")


_SUBSCRIPTION_PROMPT = """你是订阅管理 Agent，只理解需求。工具名和参数以工具定义为准。
一次只办理一个操作；操作是否成功只根据工具结果判断，不要自行确认或声称已经办理成功，确认与执行由代码处理。
先查询再决定是允许的：只读工具的结果会作为工具结果返回给你，你可以据此再决定下一步；一次仍只办理一个操作。
查订阅用 subscription_list；要看某一笔的详情用 subscription_detail，用查询结果里的订阅名称或商户名定位（原样照抄）。
用订阅名称或商户名定位某一笔时，文字必须与 subscription_list 返回的完全一致；只是听起来像的名字不算，拿不准就说明没找到，不要近似匹配。
要取消、暂停、恢复或立即扣款时调用对应的写工具；代码会弹出确认卡，不要在回复里自己问确认，也不要替用户确认。
动词拿不准时落到可逆的一侧：单独说「扣款」先当查询（subscription_detail），只有明确说「现在就扣」「立即扣款」才用 subscription_charge；说「停掉」「先停一停」用 subscription_pause（可恢复），只有明确说「取消」「退订」才用 subscription_cancel。
只把你确实打算修改的字段放进参数；没打算改的字段不要放，也不要写成 null 或 "null"。
用户没说清是哪一笔订阅时，省略 subscription_id，代码会列出候选让用户选择；不要编造订阅 ID 或金额。
缺少信息先用 ask_user 追问；不需要询问验证码，代码会处理。
"""


class SubscriptionState(AgentState, total=False):
    """Subscription's graph state: the base slots, the frozen snapshot, and an open selection."""

    pending_action: dict
    subscription_call: dict
    subscription_choices: dict[str, str]


def build_subscription_graph(model, spec, *, assembler=None, turn=None, client=None, writes=None):
    """Subscription's own graph. It ignores the base's `assembler`/`turn`/`client` seam.

    The base always passes the three keywords to a module that builds its own graph; this
    module reads none of them, but it has to accept them or the call site raises `TypeError`.
    `writes` arrives the same way once `bind_subscription` has run: the click carries the
    frozen amount into the facade, so the graph never needs the facade itself.
    """

    def invoke(name, arguments, idempotency_key=None):
        writer = get_stream_writer()
        outcome = DomainOutcome(tool_name=name, arguments=arguments)
        # A fresh key per activation, unless a frozen snapshot supplies the key it was prepared
        # with: a click that is retried must replay, not repeat. The retry below reuses `key`.
        key = idempotency_key or f"subscription:{name}:{uuid4().hex}"
        result = call_tool(spec, outcome, writer, idempotency_key=key)
        if may_retry(spec, result):
            result = call_tool(spec, outcome, writer, idempotency_key=key)
            if may_retry(spec, result):
                raise UpstreamUnavailable("TOOL_UNAVAILABLE")
        return result

    def finish(summary, status=ToolStatus.SUCCEEDED, ids=None, *, thread=None):
        # Only a summary the model wrote has to be grounded; a tool result is the evidence
        # itself. Callers that omit thread pass trusted tool text or a constant safe sentence.
        guarded = guard_summary(summary, thread=thread, business_ids=ids) if thread is not None else None
        blocked_reason = guarded.reason if guarded is not None and guarded.blocked else None
        if guarded is not None and guarded.blocked:
            summary = guarded.summary
            status = ToolStatus.FAILED
        turn_status = _SUBSCRIPTION_TURN_STATUS[status]
        event = {
            "event": "subtask_end",
            "agent_id": "subscription",
            "status": status.value,
            "summary": summary,
            "turn_status": turn_status,
        }
        result = {
            "agent_id": "subscription",
            "summary": summary,
            "instruction": "",
            "business_ids": ids or {},
            "turn_status": turn_status,
        }
        if blocked_reason:
            event["grounding_blocked"] = blocked_reason
            result["grounding_blocked"] = blocked_reason
        get_stream_writer()(event)
        return {
            "active_agent": None,
            "domain_results": [result],
            # idle_work_state() does not clear a module-specific slot, so reset them here or a
            # concluded activation leaves its snapshot or its open selection behind.
            "pending_action": None,
            "subscription_call": None,
            "subscription_choices": {},
            **idle_work_state(),
        }

    def from_result(result):
        if not result.succeeded:
            return finish(f"订阅操作未成功：{result.error}", result.status)
        return finish(result.data["summary"], ids=result.data.get("business_ids"))

    def run_call(call):
        """Run one decided call and say what it means for the activation.

        A declared read goes back to the model. A declared write never reaches the bank: it
        freezes the operation into a snapshot and returns a park, which `activate` turns into
        the graph's own slots. The hidden execute tool is denied here even though it is declared,
        so only the confirmation click can reach it.
        """

        name, args = call["tool"], declared_arguments(call["arguments"])
        declared = spec.handle.spec(name)
        # Defence in depth: `parse_domain_decision` already rejects a name the model was not
        # offered, but a hidden tool must stay denied however a call reaches this node.
        if declared is None or name in _HIDDEN:
            return _DONE, finish("该订阅操作不被允许。", ToolStatus.DENIED)
        if not Draft202012Validator(declared.parameters).is_valid(args):
            return _DONE, finish("订阅操作参数不完整或格式不正确，请重新说明。", ToolStatus.FAILED)
        if declared.side_effect == SideEffect.WRITE and _needs_selection(args):
            # Resolve the target against the caller's own rows before freezing anything. Without
            # this the model could never name a merchant, so the selection park would be dead code.
            listing = invoke("subscription_list", {})
            if not listing.succeeded:
                return _DONE, from_result(listing)
            matches = matched_subscriptions(listing.data["facts"]["subscriptions"], args)
            if not matches:
                return _DONE, finish("没有找到符合条件的本人订阅。", ToolStatus.FAILED)
            if len(matches) > 1:
                # Labels are built from bank data; the model cannot choose another customer.
                choices = {subscription_label(row): row["subscription_id"] for row in matches}
                if len(choices) != len(matches):
                    return _DONE, finish(
                        "订阅名称或商户存在重复，无法安全选择，请联系人工处理。",
                        ToolStatus.FAILED,
                    )
                return _PARKED, {
                    "subscription_call": call,
                    "subscription_choices": choices,
                    "turn_phase": "waiting_for_user",
                    "pending_question": {
                        "kind": "subscription_selection",
                        "question_id": f"q_{uuid4().hex}",
                        "agent_id": "subscription",
                        "attempt": 1,
                        "display": "有多笔订阅符合条件，请选择要办理的订阅。",
                        "fields": [
                            {
                                "name": "subscription_choice",
                                "label": "订阅",
                                "type": "select",
                                "required": True,
                                "options": list(choices),
                            }
                        ],
                    },
                    "pending_answer": None,
                }
            args["subscription_id"] = matches[0]["subscription_id"]
        result = invoke(name, args)
        if not result.succeeded:
            return _DONE, from_result(result)
        if declared.side_effect == SideEffect.READ:
            # A read round is not an ending: the result goes back to the model so it can decide
            # again with the facts in hand.
            return _READ_ROUND, result
        action = result.data["facts"]["action"]
        return _PARKED, {
            "pending_action": action,
            "pending_question": authorization_question(action),
            "pending_answer": None,
            "turn_phase": "waiting_for_user",
        }

    # What the model may call travels as tool definitions, not as JSON printed into the prompt.
    # The execute tool is filtered out here and only here: it stays in the bound catalog, but the
    # model is never offered it and never learns its name.
    tools = [item for item in spec.handle.tools if item.name not in _HIDDEN]
    schemas = tuple(function_schema(item) for item in tools) + (function_schema(ask_user_spec()),)
    known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}

    def seed_thread(state, answers):
        """一次激活的第一条用户消息：指令 + 已经拿到的补充信息。"""

        return [
            {
                "role": "user",
                "content": (
                    f"指令：{state.get('goal') or '按用户请求查询订阅'}\n"
                    f"已补充信息：{json.dumps(answers or {}, ensure_ascii=False)}"
                ),
            }
        ]

    def assistant_turn(text, calls):
        """模型这一轮在线程里的样子：有叙述存叙述，没有就存它要调的那个调用。"""

        if text.strip():
            return {"role": "assistant", "content": text}
        return {
            "role": "assistant",
            "content": json.dumps(
                {"tool": calls[0].name, "arguments": calls[0].arguments}, ensure_ascii=False
            ),
        }

    def activate(state, thread, rounds, answers, pending=None):
        """一次激活的循环：模型 → 工具 → 回喂 → 模型，直到收尾、提问或到界。

        整个循环活在**一次节点访问**里，它自己不中断：问用户是循环的返回值，中断由 `wait`
        节点做 —— 这条分工是「停在问题上的激活能被原样重放」的唯一原因。`pending` 是代码
        已经决定好、必须先执行的那一次调用（用户选完订阅之后的写操作），它走同一条执行路径，
        所以只读轮次的回喂逻辑只有一份。
        """

        summaries: list[str] = []
        while True:
            if pending is None:
                messages = ({"role": "system", "content": _SUBSCRIPTION_PROMPT}, *thread)
                try:
                    text, calls = collect_stream(model, messages, schemas)
                except StreamTruncated:
                    return finish("模型输出中断，本次没有办理任何操作。", ToolStatus.FAILED)
                try:
                    decision = parse_domain_decision(text, calls, known)
                except DecisionError as error:
                    # 决策不可用时把被拒的调用留在日志里，否则线上只能看到一句通用中文。
                    logger.warning(
                        "subscription decide rejected: %s | text=%r calls=%r",
                        error,
                        text[:200],
                        [(call.name, dict(call.arguments)) for call in calls],
                    )
                    if len(calls) == 1 and calls[0].name not in known:
                        # 模型点名了它没被授予的工具（含隐藏的 subscription_execute）：
                        # 这是「不被允许」，不是模型把话说坏了。
                        return finish("该订阅操作不被允许。", ToolStatus.DENIED)
                    return finish("模型没有给出可用的订阅信息，请重新说明。", ToolStatus.FAILED)
                outcome = domain_outcome(decision)
                if outcome.ask is not None:
                    attempt = int((state.get("pending_question") or {}).get("attempt", 0)) + 1
                    return awaiting(
                        spec,
                        outcome,
                        get_stream_writer(),
                        collected=answers or {},
                        attempt=attempt,
                        domain_thread=thread,
                        tool_rounds=rounds,
                    )
                if not outcome.wants_tool:
                    return finish(
                        outcome.summary or "请说明要查询哪笔订阅；当前尚未办理任何操作。",
                        thread=thread,
                    )
                if rounds >= MAX_TOOL_ROUNDS:
                    # 与基座同序：到界的那一次模型调用仍然发生，所以模型总有一次用叙述收尾的机会。
                    return finish(overflow_outcome(spec, summaries).summary)
                thread = [*thread, assistant_turn(text, calls)]
                name, args = outcome.tool_name, outcome.arguments
            else:
                name, args = pending
                pending = None
            kind, payload = run_call({"tool": name, "arguments": args})
            if kind == _PARKED:
                # A park is a return, not an ending. Carrying the live thread and the round count
                # is what lets a resumed activation continue this round instead of restarting it.
                payload["domain_thread"] = thread
                payload["tool_rounds"] = rounds
                return payload
            if kind == _DONE:
                return payload
            tool = tool_outcome(payload)
            thread = [*thread, tool_envelope(DomainOutcome(tool_name=name), tool)]
            summaries.append(tool.summary)
            rounds += 1

    def decide(state, *, answers=None):
        get_stream_writer()({"event": "agent_start", "agent_id": "subscription"})
        thread = list(state.get("domain_thread") or [])
        if thread:
            # 恢复：线程已经在线（可能已经跑过只读轮次），只把这一轮的补充接在后面。
            thread.append(
                {
                    "role": "user",
                    "content": json.dumps({"user_answer": answers or {}}, ensure_ascii=False),
                }
            )
        else:
            thread = seed_thread(state, answers)
        return activate(state, thread, int(state.get("tool_rounds") or 0), answers or {})

    def confirmation_answer(answers):
        """The click as a machine value: the button sends `confirm`/`cancel`, a card sends 确认/取消."""

        decision = answers.get("decision")
        if decision in ("confirm", "确认"):
            return "confirm"
        if decision in ("cancel", "取消"):
            return "cancel"
        return None

    def consume_confirmation(state, answers):
        """Turn one confirmation click into an execution, a cancellation, or a denial.

        Validated in order: a frozen snapshot must exist and carry both fields the write path
        needs, it must not have expired, and if the answer carries a `pending_id` it must be the
        one frozen. Any failure denies without a bank call -- a click is not by itself a licence
        to write.
        """

        action = state.get("pending_action")
        if not isinstance(action, dict):
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        subscription_id = action.get("subscription_id")
        operation = action.get("operation")
        # Fail closed on a snapshot missing either field the write path needs: a KeyError here
        # would surface as an internal error instead of the denial this check exists to give.
        if not subscription_id or not operation:
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        if expired(action):
            return finish("这笔操作的确认信息已过期，请重新说明。", ToolStatus.DENIED)
        pending_id = answers.get("pending_id")
        if pending_id is not None and pending_id != action.get("pending_id"):
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        decision = confirmation_answer(answers)
        if decision == "cancel":
            # There is no abort tool: the snapshot lives only in the checkpoint, so cancelling is
            # clearing the state, not releasing anything on the bank's side.
            return finish("本次操作已取消，未修改订阅。")
        if decision != "confirm":
            return finish("请明确确认或取消。", ToolStatus.DENIED)
        result = invoke(
            "subscription_execute",
            {
                "subscription_id": subscription_id,
                "operation": operation,
                # The frozen amount travels with the click: the facade judges the day's cap on
                # it, and a charge must never re-read an amount the user did not confirm.
                "amount_minor": action.get("amount_minor"),
            },
            idempotency_key=action.get("idempotency_key"),
        )
        return from_result(result)

    def process(state):
        question = state.get("pending_question") or {}
        answers, problem = read_answer(state.get("pending_answer"), question)
        if problem:
            attempt = int(question.get("attempt", 1)) + 1
            if attempt > 3:
                return finish("未获得有效输入，本次办理已结束。", ToolStatus.FAILED)
            return {
                "pending_question": {
                    **question,
                    "attempt": attempt,
                    "display": f"{problem}\n{question['display']}",
                },
                "pending_answer": None,
                "turn_phase": "waiting_for_user",
            }
        if question.get("kind") == "confirmation":
            return consume_confirmation(state, answers)
        if question.get("kind") == "subscription_selection":
            selected = answers.get("subscription_choice")
            choices = state.get("subscription_choices") or {}
            if selected not in choices:
                return finish("所选订阅不在当前候选列表中。", ToolStatus.DENIED)
            call = state.get("subscription_call")
            if not isinstance(call, dict):
                return finish("这笔订阅选择已失效，请重新说明。", ToolStatus.DENIED)
            # The chosen row's id is bank data, so the model never supplies it. The write tool
            # runs on the live thread, so a read round before the selection is not lost.
            args = {**call["arguments"], "subscription_id": choices[selected]}
            thread = list(state.get("domain_thread") or [])
            if thread:
                thread.append(
                    {
                        "role": "user",
                        "content": json.dumps({"user_answer": answers}, ensure_ascii=False),
                    }
                )
            else:
                thread = seed_thread(state, {**(state.get("collected_inputs") or {}), **answers})
            return activate(
                state,
                thread,
                int(state.get("tool_rounds") or 0),
                {**(state.get("collected_inputs") or {}), **answers},
                pending=(call["tool"], args),
            )
        collected = {**(state.get("collected_inputs") or {}), **answers}
        return decide(state, answers=collected)

    def route(state):
        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(SubscriptionState)
    # 归属：模型调用记在哪个 agent 名下，取决于调用发生在谁的作用域里。模块自己建的图，
    # 节点就得自己开 —— 基座不能替它开（见 sdk.domain_node 的说明）。
    graph.add_node("decide", domain_node(spec, assembler, turn, decide))
    graph.add_node("wait", wait_for_user)
    graph.add_node("process", domain_node(spec, assembler, turn, process))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
    return graph.compile()
