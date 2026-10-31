"""A card-only workflow. User authorization is verified by the bank, never the LLM."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import parse_domain_decision
from app.agents.grounding import guard_summary
from app.agents.sdk import (
    CONFIRMATION_KIND,
    MAX_TOOL_ROUNDS,
    DomainOutcome,
    TurnAction,
    TurnStatus,
    action_kind,
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
from app.modules.card.tools import card_label, matched_cards
from app.policy.evaluator import classify_operation
from app.tools.protocol import SideEffect, ToolStatus
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema

logger = logging.getLogger(__name__)

# How a card turn's ending is named in `domain_results` and `subtask_end`. The Supervisor
# reads `turn_status` back to decide whether an agent has already concluded, so a card turn
# without one looks unfinished forever and is dispatched again. This is the inverse of the
# Supervisor's own TurnStatus-to-ToolStatus table, written here because it describes this
# module's endings: a denied operation is the "unsupported" one, not a failure.
_CARD_TURN_STATUS = {
    ToolStatus.SUCCEEDED: TurnStatus.COMPLETED.value,
    ToolStatus.FAILED: TurnStatus.FAILED.value,
    ToolStatus.DENIED: TurnStatus.UNSUPPORTED.value,
}

# What one tool call means for this activation. The loop only reads these three: concluding
# and parking on a question are return values, and only a read round is "go back to the model
# and decide again".
_READ_ROUND = "read"
_PARKED = "parked"
_DONE = "done"

# How the model spells "leave this parameter alone". A field it does not intend to change
# comes back as JSON null or as the *string* "null" often enough that the schema -- which
# types every limit as a patterned string and forbids extra properties -- would reject an
# otherwise valid call. Dropping the placeholder is what keeps "don't change the monthly
# limit" from reading as "the arguments are malformed".
_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})


def declared_arguments(arguments):
    """Drop the values that mean "not provided" before the schema sees them."""

    return {
        key: value
        for key, value in arguments.items()
        if value is not None
        and not (isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS)
    }


_CARD_PROMPT = """你是银行卡管理 Agent，只理解需求和补全信息。工具名和参数以工具定义为准。
一次只办理一个操作；操作成功只根据工具结果判断。不要自行确认、验证或声称办理成功。
先查询再决定是允许的：只读工具的结果会作为工具结果返回给你，你可以据此再决定下一步；一次仍只办理一个操作。
查卡可直接调用 card_cards；多张卡由代码追问，用户不需要填写内部 ID。
挂失用 report_loss，找回挂失卡用 release_loss，临时冻结用 freeze，解冻用 unfreeze。
限额是交易限额，金额参数用人民币元字符串；不要换算为分或做金额计算。
申请缺少 card_type 时用 ask_user 追问它（debit/credit/virtual）。缺少限额时追问 daily_limit_yuan 或 monthly_limit_yuan。
商户增删分别用 add/remove，明确要求覆盖时才用 replace。
缺少信息先用 ask_user 追问，不得编造金额或商户。不需要询问确认、验证码，代码会处理。
修改银行卡交易密码用 card_change_password。密码只能在独立表单填写，禁止询问、接收或输出旧密码、新密码、确认密码及其哈希；工具只传卡片选择信息。
申请银行卡用 card_apply，只传卡类型。初始交易密码由用户在独立确认表单设置，禁止通过聊天或工具索要、接收密码。
本轮能力以实际提供的工具定义为准，旧聊天中的拒绝记录不是当前能力清单。card_change_password 已接入交易密码修改；遇到改密请求先调用该工具核实卡片并准备确认表单，不得沿用旧的“不做密码”结论，也不得在工具执行前声称修改成功。
用户没指明卡号末四位时，直接调用工具并省略 card_suffix，代码会列出候选卡让用户选择；不要自己在回复里询问要哪张卡。
没打算修改的字段不要放进参数里，也不要写成 null 或 "null"。
"""


class CardState(AgentState, total=False):
    card_call: dict[str, Any] | None
    card_choices: dict[str, str]
    card_action_id: str | None


def authorization_question(action):
    """Build the platform confirmation pause for one prepared card action.

    Uses `kind=confirmation` and the shared 11-key confirmation payload so the only user
    entry is `/pending-actions/{id}/decide`. `pending_id` is the bank action_id; red ops set
    `requires_code`. Permission level stays as a confirmation extension for diagnostics.
    """

    before, params = action["before"], action["parameters"]
    target = (
        card_label(before)
        if before
        else {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}[params["card_type"]]
    )
    lines = [f"请确认：{action['label']} · {target}。"]
    if action["operation"] == "change_password":
        lines.append(
            "请在独立密码表单填写旧密码、新密码与确认新密码，交易密码为六位数字；不要发送到聊天中。"
        )
    elif action["operation"] == "apply":
        lines.append("请在独立表单设置并确认六位数字交易密码，不需要旧密码；不要发送到聊天中。")
    from app.modules.money import format_minor

    # `None` 才是「这次操作没有金额」（冻结、限制商户…）。以前这里用 0 兼作这个意思，
    # 于是前端只能靠「金额 > 0」去藏它 —— 那同时也是「真的花 0 元」的样子。卡片操作
    # 不是资金操作，卡面本来就不该有金额行，所以直说没有。
    amount_minor = None
    if action["operation"] == "update_limit":
        amount_minor = int(
            params.get("daily_limit_minor") or params.get("monthly_limit_minor") or 0
        )
        lines += [
            f"日交易限额：{format_minor(before['daily_limit_minor'])} → {format_minor(params['daily_limit_minor'])}",
            f"月交易限额：{format_minor(before['monthly_limit_minor'])} → {format_minor(params['monthly_limit_minor'])}",
        ]
    if action["operation"] == "update_restrictions":
        lines += [
            f"原受限商户：{'、'.join(before['merchant_blacklist']) or '无'}",
            f"新受限商户：{'、'.join(params['merchant_blacklist']) or '无'}",
            "仅修改卡片配置，模拟转账暂未执行商户限制。",
        ]
    # One-way tightening: the bank's own tier can only ever be raised here, never lowered.
    # The operation travels as its tool name, because `TOOL_TO_OPERATION` is keyed by tool
    # name and a bare operation would fall through to the Fail-Closed branch and turn every
    # card write red. Card operations are not fund writes, so the amount is not a factor.
    local = classify_operation(f"card_{action['operation']}", side_effect=SideEffect.WRITE)
    requires_code = action["permission_level"] == "red" or local.mfa_required
    if requires_code:
        lines.append("红色操作：需要通过模拟验证码验证。验证码只随确认提交，不进入聊天。")
    else:
        lines.append("黄色操作：明确确认后执行；取消不会修改银行卡。")
    if local.mfa_required:
        lines.append(f"风险提示：{local.reason}")
    return {
        "kind": CONFIRMATION_KIND,
        "question_id": f"q_{uuid4().hex}",
        "agent_id": "card",
        "display": "\n".join(lines),
        "attempt": 1,
        "fields": [
            {
                "name": "decision",
                "label": "操作决定",
                "type": "select",
                "required": True,
                "options": ["确认", "取消"],
            }
        ],
        "confirmation": {
            "pending_id": action["action_id"],
            "operation": action["operation"],
            "title": action["label"],
            "amount_minor": amount_minor,
            "product_name": target,
            "account_display": target,
            "confirm_label": "确认",
            "cancel_label": "取消",
            "expires_at": action["expires_at"],
            "requires_code": requires_code,
            "code_label": "模拟验证码",
            # Extension: bank risk tier; UI uses requires_code, not this field.
            "permission_level": action["permission_level"],
        },
    }


def build_card_graph(model, spec, *, assembler=None, turn=None, client=None):
    """Card's own graph. Accepts the base `assembler`/`turn`/`client` seam keywords.

    Authorization of a prepared action is done on `POST .../pending-actions/{id}/decide`
    before this graph resumes; `turn` carries what only the server knows about that click --
    the verification code for that route, or the bank's own cancellation (`auto_cancel`) --
    and is never written into graph state. `client` is unused here (tools go through the
    gateway).
    """

    # `assembler` is not read here, but the scope below still needs it: it is what a node
    # that does assemble conversation memory would be handed. `client` is genuinely unused.
    del client

    def invoke(name, arguments):
        writer = get_stream_writer()
        outcome = DomainOutcome(tool_name=name, arguments=arguments)
        key = f"card:{name}:{uuid4().hex}"
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
        turn_status = _CARD_TURN_STATUS[status]
        event = {
            "event": "subtask_end",
            "agent_id": "card",
            "status": status.value,
            "summary": summary,
            "turn_status": turn_status,
        }
        result = {
            "agent_id": "card",
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
            "card_call": None,
            "card_choices": {},
            "card_action_id": None,
            **idle_work_state(),
        }

    def from_result(result):
        if not result.succeeded:
            return finish(f"银行卡操作未成功：{result.error}", result.status)
        return finish(result.data["summary"], ids=result.data.get("business_ids"))

    def run_call(state, call):
        """Run one decided call and say what it means for the activation.

        Returns `(kind, payload)`: `_DONE` carries a finished activation's state, `_PARKED`
        carries one that stopped on a question, and `_READ_ROUND` carries the read tool's own
        `ToolResult` -- the caller writes that into the thread and asks the model again.
        """

        name, args = call["tool"], declared_arguments(call["arguments"])
        # 今天写参数从不外发，C13 的「猜错卡型」与「猜错尾号」无法区分。
        logger.info("card tool requested: %s", name)
        declared = spec.handle.spec(name)
        if declared is None or name in ("card_execute", "card_cancel"):
            return _DONE, finish("该银行卡操作不被允许。", ToolStatus.DENIED)
        if not Draft202012Validator(declared.parameters).is_valid(args):
            return _DONE, finish("卡片操作参数不完整或格式不正确，请重新说明。", ToolStatus.FAILED)
        if name not in ("card_cards", "card_apply"):
            listing = invoke("card_cards", {})
            if not listing.succeeded:
                return _DONE, from_result(listing)
            matches = matched_cards(listing.data["facts"]["cards"], args)
            if not matches:
                return _DONE, finish("没有找到符合条件的本人银行卡。", ToolStatus.FAILED)
            if len(matches) > 1:
                # Labels are built from bank data; the model cannot choose another customer.
                choices = {card_label(card): card["card_number"][-4:] for card in matches}
                if len(choices) != len(matches):
                    return _DONE, finish(
                        "卡片尾号和类型存在重复，无法安全选卡，请联系人工处理。", ToolStatus.FAILED
                    )
                return _PARKED, {
                    "card_call": call,
                    "card_choices": choices,
                    "turn_phase": "waiting_for_user",
                    "pending_question": {
                        "kind": "card_selection",
                        "question_id": f"q_{uuid4().hex}",
                        "agent_id": "card",
                        "attempt": 1,
                        "display": "有多张银行卡符合条件，请选择要办理的卡片。",
                        "fields": [
                            {
                                "name": "card_choice",
                                "label": "银行卡",
                                "type": "select",
                                "required": True,
                                "options": list(choices),
                            }
                        ],
                    },
                    "pending_answer": None,
                }
            args["card_suffix"] = matches[0]["card_number"][-4:]
            args["card_type"] = matches[0]["card_type"]
        result = invoke(name, args)
        if not result.succeeded:
            return _DONE, from_result(result)
        if declared.side_effect == SideEffect.READ:
            # A read round is not an ending: the result goes back to the model so it can
            # decide again with the facts in hand.
            return _READ_ROUND, result
        action = result.data["facts"]["action"]
        return _PARKED, {
            "card_action_id": action["action_id"],
            "card_call": None,
            "pending_question": authorization_question(action),
            "pending_answer": None,
            "turn_phase": "waiting_for_user",
        }

    # What the model may call travels as tool definitions, not as JSON printed into the
    # prompt. The two tools that authorize and execute a write are withheld from the schema
    # as well as from the prompt, so the model cannot name them at all.
    tools = [item for item in spec.handle.tools if item.name not in ("card_execute", "card_cancel")]
    schemas = tuple(function_schema(item) for item in tools) + (function_schema(ask_user_spec()),)
    known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}

    def seed_thread(state, answers):
        """一次激活的第一条用户消息：指令 + 已经拿到的补充信息。"""

        return [
            {
                "role": "user",
                "content": (
                    f"指令：{state.get('goal') or '按用户请求管理银行卡'}\n"
                    f"已补充信息：{json.dumps(answers or {}, ensure_ascii=False)}"
                ),
            }
        ]

    def assistant_turn(text, calls):
        """模型这一轮在线程里的样子。

        与基座记录领域回合的规则一致：有叙述就存叙述，没有就存它要调的那个调用。扁平化的
        接缝没有 tool_calls 字段，所以调用以 JSON 随内容走，和它后面那条工具信封同一种词汇。
        """

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
        已经决定好、必须先执行的那一次调用（用户选完卡之后的写操作），它走同一条执行路径，
        所以只读轮次的回喂逻辑只有一份。
        """

        summaries: list[str] = []
        while True:
            if pending is None:
                messages = ({"role": "system", "content": _CARD_PROMPT}, *thread)
                try:
                    text, calls = collect_stream(model, messages, schemas)
                except StreamTruncated:
                    return finish("模型输出中断，本次没有办理任何操作。", ToolStatus.FAILED)
                outcome = domain_outcome(parse_domain_decision(text, calls, known))
                if outcome.ask is not None:
                    if any(
                        "password" in name.lower() or "pin" in name.lower() or "密码" in name
                        for name in outcome.ask.get("fields", [])
                    ):
                        return finish(
                            "交易密码只在独立密码表单中填写，请先选择要修改的银行卡。",
                            ToolStatus.DENIED,
                        )
                    # 问出去的字段不必都在声明里：`awaiting` 会用 `resolve_fields` 兜底，
                    # 未声明的字段退回自由文本。以前这里把整个问题换成一句收尾文案，用户
                    # 真正问的事就消失了（真机 C10 的挂失请求就是这样丢的）。
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
                    # 循环建立之后，模型自己的汇报就是收尾文案：它刚刚看过工具结果。
                    return finish(
                        outcome.summary
                        or "请说明要查询、申请或修改哪项银行卡信息；当前尚未办理任何操作。",
                        thread=thread,
                    )
                if rounds >= MAX_TOOL_ROUNDS:
                    # 与基座同序：到界的**那一次模型调用仍然发生**，所以模型总有一次用叙述
                    # 收尾的机会；它若仍然只要工具，就换成可读的到界收尾，不静默丢弃。
                    return finish(overflow_outcome(spec, summaries).summary)
                if any(
                    "password" in key.lower() or "pin" in key.lower() for key in outcome.arguments
                ):
                    return finish("工具不接受密码，请通过独立密码表单办理。", ToolStatus.DENIED)
                thread = [*thread, assistant_turn(text, calls)]
                name, args = outcome.tool_name, outcome.arguments
            else:
                name, args = pending
                pending = None
            kind, payload = run_call(state, {"tool": name, "arguments": args})
            if kind == _PARKED:
                # 停下来的激活要把线程一起存进检查点：用户选完卡接着跑的是同一次激活，
                # 它得看得见停下来之前查到了什么。收尾相反 —— idle_work_state() 会清掉。
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
        get_stream_writer()({"event": "agent_start", "agent_id": "card"})
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
        if question["kind"] == "card_selection":
            selected = answers.get("card_choice")
            if selected not in state.get("card_choices", {}):
                return finish("所选卡片不在当前候选列表中。", ToolStatus.DENIED)
            call = state["card_call"]
            # Include type as well as suffix; two types may share the same last four digits.
            args = {**call["arguments"], "card_suffix": state["card_choices"][selected]}
            args["card_type"] = next(
                key
                for key, label in {
                    "debit": "借记卡",
                    "credit": "信用卡",
                    "virtual": "虚拟卡",
                }.items()
                if selected.startswith(label)
            )
            # 选完卡仍然走那条循环：选卡只是补上了选择器，只读工具的答案还要回喂给模型。
            thread = list(state.get("domain_thread") or [])
            if thread:
                thread.append(
                    {
                        "role": "user",
                        "content": json.dumps({"user_answer": answers}, ensure_ascii=False),
                    }
                )
            else:
                # 线程本该由停在选卡上的那次激活存进检查点；旧检查点里没有时兜底播种。
                thread = seed_thread(state, {**(state.get("collected_inputs") or {}), **answers})
            return activate(
                state,
                thread,
                int(state.get("tool_rounds") or 0),
                {**(state.get("collected_inputs") or {}), **answers},
                pending=(call["tool"], args),
            )
        if question["kind"] == CONFIRMATION_KIND:
            # The bank can void the operation before this resume: /decide authorizes first, and
            # a third wrong code ends the action there. The server says so on the turn, and then
            # the answers are not read and execute is not attempted -- the module cancels, which
            # is the same branch the user's own cancel takes, wording and all.
            if turn is not None and turn.auto_cancel:
                result = invoke("card_cancel", {"action_id": state["card_action_id"]})
                return from_result(result)
            # Machine confirm/cancel from /decide, or the labels this card showed.
            decision = action_kind(question, answers)
            if decision is TurnAction.CANCEL:
                result = invoke("card_cancel", {"action_id": state["card_action_id"]})
                return from_result(result)
            if decision is not TurnAction.CONFIRM:
                return finish("请明确确认或取消。", ToolStatus.DENIED)
            # Authorize happens on the decide route in the same HTTP request. A forged resume
            # that skipped it cannot execute: the bank checks its own authorization record.
            result = invoke("card_execute", {"action_id": state["card_action_id"]})
            if result.error_code == "CONFIRMATION_REQUIRED":
                return {
                    "pending_question": question,
                    "pending_answer": None,
                    "turn_phase": "waiting_for_user",
                }
            return from_result(result)
        collected = {**(state.get("collected_inputs") or {}), **answers}
        return decide(state, answers=collected)

    def route(state):
        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(CardState)
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
