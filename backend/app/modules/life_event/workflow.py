"""生活事件域的自有图：只读循环 + 确认卡 + 点击消费。

一轮对话最多跑三轮只读工具（查生日、挑礼物），拿到事实后直接回一段话；要动钱就挂确认卡
（``life_prepare_order`` 冻结订单快照，``life_cancel_order`` 冻结撤销动作），点"确认"后才
真正提交。验证码只走 ``TurnContext.verification_code``，既不进工具参数也不进网关适配器：
``ToolHandler`` 的签名是 ``(invocation, client)``，走适配器会把 ``mfa_code`` 静默丢掉。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import DecisionError, parse_domain_decision
from app.agents.grounding import guard_summary
from app.agents.sdk import (
    CONFIRMATION_KIND,
    MAX_ASK_ATTEMPTS,
    DomainOutcome,
    TurnAction,
    TurnConfirmation,
    TurnResult,
    TurnStatus,
    action_kind,
    awaiting,
    call_tool,
    conclude_turn,
    domain_node,
    domain_outcome,
    may_retry,
    next_attempt,
    overflow_outcome,
    read_answer,
    tool_envelope,
    tool_outcome,
    wait_for_user,
)
from app.agents.state import AgentState, idle_work_state
from app.agents.toolcalls import StreamTruncated, collect_stream
from app.agents.upstream import UpstreamUnavailable
from app.integration.bank_errors import BankCallError, BankErrorKind
from app.modules.base import ToolFailure
from app.modules.life_event.tools import cancel as cancel_order
from app.modules.life_event.tools import execute_order
from app.modules.money import format_minor
from app.tools.normalize import normalize_arguments
from app.tools.protocol import (
    ErrorCode,
    SideEffect,
    ToolCall,
    ToolInvocation,
    ToolStatus,
)
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema

logger = logging.getLogger(__name__)

#: 一轮里最多让模型连跑几轮只读工具（查生日 + 挑礼物 + 兜底一次）。
MAX_READ_ROUNDS = 3

#: 确认卡的寿命。过期就 fail-closed 地拒绝，而不是拿一张隔天的旧卡去提交生日订单。
PENDING_EXPIRY_MINUTES = 5

#: 需要点击确认、不能直接执行的工具。撤销必须挂卡，别让模型一句话就把订单撤了。
CONFIRMATION_TOOLS = ("life_cancel_order",)

#: 银行订单状态的中文说法，只用于拒绝撤销时那句人话。
ORDER_STATUS_LABELS = {
    "pending": "待执行",
    "completed": "已完成",
    "cancelled": "已取消",
    "failed": "已失败",
}

#: 只在确认后由本图直连调用的工具：既不进模型菜单，也不许模型自己声明调用。
_HIDDEN = frozenset({"life_execute_order"})

#: 冻结给 ``life_execute_order`` 的五个键。``facts["intent"]`` 有八个（还带 contact_name /
#: product_name / execute_at），交出去之前收窄到工具自己声明的那五个。
_EXECUTE_KEYS = ("contact_id", "product_code", "from_account_id", "birthday_date", "amount_minor")

#: 冻结快照里必须是非空字符串的那几个键。
_TEXT_KEYS = ("contact_id", "product_code", "from_account_id", "birthday_date")

_RESULT_TURN_STATUS = {
    ToolStatus.SUCCEEDED: TurnStatus.COMPLETED,
    ToolStatus.FAILED: TurnStatus.FAILED,
    ToolStatus.DENIED: TurnStatus.UNSUPPORTED,
}

_READ = "read"
_DONE = "done"

SYSTEM_PROMPT = """你是银行的生活事件助手，负责生日、节日这类生活安排。

可用工具：
- life_birthdays：读通讯录里的生日联系人（含 next_birthday / days_until）。
- life_gift_plan：按预算挑礼物，返回 product_code 与价格。
- life_prepare_order：把一份生日订单冻结下来，提交前会弹确认卡，由用户点"确认"。
- life_cancel_order：撤销一笔已经下单的生日订单（会先弹确认卡）。用户说「刚才那单」「把刚才预订的取消掉」时，不要追问订单号，直接调用本工具，系统会用本对话最近提交的订单。

规则：
1. 用户问"谁的生日快到了""推荐什么礼物"时，先查事实再回答，一轮里可以连续调用多个只读工具。
2. 只有用户明确要求下单时才调用 life_prepare_order；下单前把联系人、礼物、付款账户说清楚。
3. 不要假装订单已经提交：确认卡由系统弹出，用户点确认后才真正下单。
4. contact_id 可以传 life_birthdays 返回的 ID，也可以直接传该联系人的姓名（必须与通讯录完全一致）；product_code 只能从 life_gift_plan 的候选里选；付款账户不传时，工具会自动选唯一的活期账户。只有缺失的信息才问用户；用户当前消息已经说清联系人、礼物和账户时，直接调用 life_prepare_order 弹确认卡，不要再用追问表单重复确认。
5. 回答用中文，简短，带上联系人姓名、礼物名称和金额。"""


class LifeState(AgentState, total=False):
    """生活事件图私有的三个槽位。"""

    life_intent: dict[str, Any] | None
    life_pending: dict[str, Any] | None
    life_action: dict[str, Any] | None


def expired(action: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """这张确认卡是不是已经过期。

    读不到 expires_at 就算过期（fail closed）。用挂钟时间而不是银行时钟 —— 银行时钟在测试里是
    模拟的，可能停着不走；裸时间戳按 UTC 解释。
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


def order_to_cancel(client: Any, customer_id: str, order_id: str) -> tuple[int | None, str | None]:
    """凭订单号向银行核验一笔「本对话不记得」的生日订单。

    换会话、丢检查点、或者用户直接报了订单号时，state["life_action"] 里没有这一单；
    但银行那边还挂着 pending。核验通过才返回金额，挂撤销卡时才有东西可写。

    银行侧的 GET /birthdays/orders/{order_id} 收了 user_id 却不传给 service
    （bank_backend/app/api/routes.py:1126-1131），所以归属只能在这里按返回体的
    user_id 自己比：不是本人的单子一律当成不存在，别让模型借一个已知单号去撤别人的单。
    """

    try:
        data = client.request("GET", f"/birthdays/orders/{order_id}", user_id=customer_id).data
    except BankCallError as failure:
        if failure.kind is BankErrorKind.NOT_FOUND:
            return None, f"没有找到编号为 {order_id} 的生日订单。"
        logger.warning("life order lookup failed: %s | order=%s", failure, order_id)
        return None, "暂时无法核验这张生日订单，请稍后再试。"
    order = data if isinstance(data, dict) else {}
    if str(order.get("user_id") or "") != customer_id:
        return None, f"没有找到编号为 {order_id} 的生日订单。"
    status = str(order.get("status") or "")
    if status != "pending":
        label = ORDER_STATUS_LABELS.get(status, status or "未知")
        return None, f"订单 {order_id} 当前状态是「{label}」，不能再撤销。"
    amount = order.get("amount_minor")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        logger.warning(
            "life order lookup returned no usable amount: %r | order=%s", amount, order_id
        )
        return None, "暂时无法核验这张生日订单，请稍后再试。"
    return amount, None


def build_life_event_graph(
    model: Any,
    spec: Any,
    *,
    assembler: Any = None,
    turn: Any = None,
    client: Any = None,
    writes: Any = None,
) -> Any:
    """Build the life event domain graph.

    读侧只调网关；写侧（提交订单、撤销订单）在确认之后直连工具函数，好让验证码从
    ``turn`` 直接进 ``execute_order``，不经过工具参数。
    """

    def invoke(name: str, arguments: dict[str, Any]) -> Any:
        """Run one gateway call, retrying once when the failure is retryable."""

        writer = get_stream_writer()
        outcome = DomainOutcome(tool_name=name, arguments=dict(arguments))
        key = f"life:{name}:{uuid4().hex}"
        result = call_tool(spec, outcome, writer, idempotency_key=key)
        if may_retry(spec, result):
            result = call_tool(spec, outcome, writer, idempotency_key=key)
            if may_retry(spec, result):
                raise UpstreamUnavailable("TOOL_UNAVAILABLE")
        return result

    def conclude(
        state: dict[str, Any],
        summary: str,
        status: TurnStatus = TurnStatus.COMPLETED,
        facts: dict[str, Any] | None = None,
        business_ids: dict[str, Any] | None = None,
        *,
        thread: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """End the turn with a final answer; clears the frozen snapshot."""

        guarded = (
            guard_summary(summary, thread=thread, facts=facts, business_ids=business_ids)
            if thread is not None
            else None
        )
        blocked_reason = guarded.reason if guarded is not None and guarded.blocked else None
        if guarded is not None and guarded.blocked:
            summary = guarded.summary
            status = TurnStatus.FAILED
        result = TurnResult(
            status=status,
            summary=summary,
            facts=facts or {},
            business_ids=business_ids or {},
        )
        conclusion = conclude_turn(
            spec,
            result,
            get_stream_writer(),
            state,
            (),
            grounding_blocked=blocked_reason,
        )
        conclusion.update({"life_intent": None, "life_pending": None, **idle_work_state()})
        return conclusion

    def from_result(state: dict[str, Any], name: str, result: Any) -> dict[str, Any]:
        status = _RESULT_TURN_STATUS[result.status]
        if not result.succeeded:
            return conclude(state, f"{_label(name)}没有成功：{result.error}", status)
        data = result.data if isinstance(result.data, dict) else {}
        return conclude(state, str(data.get("summary") or "已完成。"), status)

    def assistant_turn(text: str, calls: list[Any]) -> dict[str, Any]:
        """Serialise the model's calls back into the thread (calls arrive as ToolCall objects)."""

        if text and text.strip():
            return {"role": "assistant", "content": text}
        serialized = []
        for item in calls:
            name = getattr(item, "name", None)
            arguments = getattr(item, "arguments", None)
            if isinstance(item, dict):
                name = item.get("tool") or item.get("name")
                arguments = item.get("arguments")
            serialized.append({"tool": name, "arguments": arguments or {}})
        payload: Any = serialized[0] if len(serialized) == 1 else serialized
        return {
            "role": "assistant",
            "content": json.dumps(payload, ensure_ascii=False),
        }

    def read_batch(calls: list[Any], known: set[str]) -> list[DomainOutcome] | None:
        """Turn several read-only calls in one assistant turn into individual decisions.

        Providers commonly batch life_birthdays and life_gift_plan; the shared single-call
        parser refuses that shape, so the life graph runs each read itself. A batch containing
        a write or ask_user is refused: those need their own turn.
        """

        outcomes: list[DomainOutcome] = []
        for item in calls:
            name = str(getattr(item, "name", "") or "")
            declared = spec.handle.spec(name)
            if (
                name not in known
                or declared is None
                or name in _HIDDEN
                or declared.side_effect is not SideEffect.READ
            ):
                return None
            outcomes.append(domain_outcome(parse_domain_decision("", [item], known)))
        return outcomes

    def seed_thread(state: dict[str, Any], answers: dict[str, Any]) -> list[dict[str, Any]]:
        request = state.get("goal") or "（没有新的指令）"
        # 用户说「把刚才那单取消掉」时，模型不该再向用户要订单号：本对话已经记着
        # 最近提交的那一单，把号码放进提示里，撤销卡才能直接挂出来。
        recent = state.get("life_action")
        order_hint = ""
        if isinstance(recent, dict) and recent.get("kind") == "placed_order":
            order_id = str(recent.get("order_id") or "").strip()
            amount = recent.get("amount_minor")
            if (
                order_id
                and isinstance(amount, int)
                and not isinstance(amount, bool)
                and amount > 0
            ):
                order_hint = (
                    f"\n本对话最近提交的生日订单号：{order_id}（{format_minor(amount)}）。"
                    "用户说「刚才的订单」「把刚才预订的取消掉」时，"
                    "直接用这个订单号调用 life_cancel_order。"
                )
        return [
            {
                "role": "user",
                "content": (
                    f"指令：{request}\n已补充信息：{json.dumps(answers, ensure_ascii=False)}"
                    f"{order_hint}"
                ),
            }
        ]

    def prepare(state: dict[str, Any], result: Any) -> dict[str, Any]:
        """Freeze the snapshot and hang the confirmation card."""

        data = result.data if isinstance(result.data, dict) else {}
        facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
        if facts.get("status") != "ready":
            return conclude(
                state, "生日订单还没有准备好，请补充联系人、礼物或付款账户。", TurnStatus.FAILED
            )
        raw = facts.get("intent") if isinstance(facts.get("intent"), dict) else {}
        frozen = {key: raw.get(key) for key in _EXECUTE_KEYS}
        amount = frozen.get("amount_minor")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return conclude(state, "生日订单金额无效，请重新发起办理。", TurnStatus.FAILED)
        for key in _TEXT_KEYS:
            value = frozen.get(key)
            if not isinstance(value, str) or not value.strip():
                return conclude(state, "生日订单信息不完整，请重新发起办理。", TurnStatus.FAILED)
            frozen[key] = value.strip()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_EXPIRY_MINUTES)
        ).isoformat()
        confirmation = TurnConfirmation(
            pending_id=f"tp_{uuid4().hex}",
            operation="birthday_order",
            title="确认生日订单",
            amount_minor=amount,
            product_name=str(raw.get("product_name") or frozen["product_code"]),
            account_display=str(raw.get("from_account_label") or frozen["from_account_id"]),
            requires_code=bool(facts.get("requires_code")),
            expires_at=expires_at,
        )
        summary = str(data.get("summary") or f"请确认这份生日订单（{format_minor(amount)}）。")
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=summary,
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update(
            {
                "life_intent": frozen,
                "life_pending": {
                    "pending_id": confirmation.pending_id,
                    "requires_code": confirmation.requires_code,
                    "idempotency_key": f"life-{uuid4().hex}",
                    "expires_at": expires_at,
                },
                "life_action": None,
                "domain_thread": [],
                "tool_rounds": 0,
            }
        )
        return conclusion

    def recoverable_amount(order_id: str) -> tuple[int | None, str | None]:
        """本对话之外的一单：没有银行客户端就当它不存在，有就去问银行。"""

        if client is None:
            return None, f"没有找到编号为 {order_id} 的生日订单。"
        customer_id = spec.handle.trusted_context.bank_customer_id
        if not customer_id:
            return None, "暂时无法核验这张生日订单，请稍后再试。"
        return order_to_cancel(client, customer_id, order_id)

    def hang_cancel(state: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        """Freeze a cancel action and hang its own card.

        金额优先用本对话下单时冻结的那份；没记着这一单就去银行核验（见 recoverable_amount）。
        """

        order_id = str(arguments.get("order_id") or "").strip()
        placed = state.get("life_action")
        if not order_id and isinstance(placed, dict) and placed.get("kind") == "placed_order":
            # 用户说「刚才那单」时模型可以不传订单号；只认本对话刚刚提交成功的那一单，
            # 不能拿别的状态兜底，否则会把不相关的订单挂上撤销卡。
            order_id = str(placed.get("order_id") or "").strip()
        if not order_id:
            return conclude(state, "请提供要撤销的生日订单编号。", TurnStatus.FAILED)
        amount = placed.get("amount_minor") if isinstance(placed, dict) else None
        remembered = (
            isinstance(placed, dict)
            and placed.get("kind") == "placed_order"
            and placed.get("order_id") == order_id
            and isinstance(amount, int)
            and not isinstance(amount, bool)
            and amount > 0
        )
        if not remembered:
            # 本对话里没记着这一单（换了会话、丢了检查点，或者用户直接报了单号）：向银行
            # 核验它还在、还是本人的、还挂着待执行，拿到金额才挂撤销卡。
            amount, problem = recoverable_amount(order_id)
            if problem is not None:
                return conclude(state, problem, TurnStatus.FAILED)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_EXPIRY_MINUTES)
        ).isoformat()
        confirmation = TurnConfirmation(
            pending_id=f"tc_{uuid4().hex}",
            operation="cancel_birthday_order",
            title="确认撤销生日订单",
            amount_minor=amount,
            product_name="生日订单",
            account_display=order_id,
            requires_code=False,
            expires_at=expires_at,
        )
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=f"请确认撤销生日订单 {order_id}（{format_minor(amount)}）。",
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update(
            {
                "life_action": {
                    "kind": "cancel_pending",
                    "pending_id": confirmation.pending_id,
                    "order_id": order_id,
                    "amount_minor": amount,
                    "idempotency_key": f"life-cancel-{uuid4().hex}",
                    "expires_at": expires_at,
                },
                "domain_thread": [],
                "tool_rounds": 0,
            }
        )
        return conclusion

    def run_call(
        state: dict[str, Any],
        call: dict[str, Any],
        thread: list[dict[str, Any]],
    ) -> tuple[str, Any]:
        """Dispatch one model tool call: read tools loop, writes hang a card."""

        name = str(call.get("tool") or "")
        arguments = normalize_arguments(call.get("arguments"))
        if name in CONFIRMATION_TOOLS:
            return _DONE, hang_cancel(state, arguments)
        declared = spec.handle.spec(name)
        if declared is None or name in _HIDDEN:
            return _DONE, conclude(state, "该生日操作不被允许。", TurnStatus.UNSUPPORTED)
        validator = Draft202012Validator(declared.parameters)
        if not validator.is_valid(arguments):
            logger.warning(
                "life tool %s arguments rejected: %s | args=%r",
                name,
                next(iter(validator.iter_errors(arguments)), None),
                arguments,
            )
            return _DONE, conclude(
                state, "生日安排参数不完整或格式不正确，请重新说明。", TurnStatus.FAILED
            )
        result = invoke(name, arguments)
        if name == "life_prepare_order":
            if not result.succeeded:
                return _DONE, from_result(state, name, result)
            return _DONE, prepare(state, result)
        if not result.succeeded:
            return _DONE, from_result(state, name, result)
        return _READ, result

    def decide(state: dict[str, Any], *, answers: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read loop: at most MAX_READ_ROUNDS tool rounds, then answer in prose."""

        writer = get_stream_writer()
        writer({"event": "agent_start", "agent_id": spec.agent_id})
        collected = dict(answers or {})
        tools = [item for item in spec.handle.tools if item.name not in _HIDDEN]
        schemas = tuple(function_schema(item) for item in tools) + (
            function_schema(ask_user_spec()),
        )
        known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}
        thread = seed_thread(state, collected)
        summaries: list[str] = []
        rounds = 0
        while True:
            try:
                text, calls = collect_stream(
                    model, ({"role": "system", "content": SYSTEM_PROMPT}, *thread), schemas
                )
            except StreamTruncated:
                return conclude(
                    state, "模型输出中断，本次没有发起任何生日订单。", TurnStatus.FAILED
                )
            try:
                if len(calls) > 1:
                    outcomes = read_batch(calls, known)
                    if outcomes is None:
                        raise DecisionError("批量工具调用里混入了非只读工具")
                else:
                    outcomes = [domain_outcome(parse_domain_decision(text, calls, known))]
            except DecisionError:
                logger.warning("life decision rejected: text=%r calls=%r", text, calls)
                return conclude(
                    state, "模型没有给出可用的生日安排，请重新说明。", TurnStatus.UNSUPPORTED
                )
            outcome = outcomes[0]
            if outcome.ask is not None:
                question = state.get("pending_question") or {}
                attempt = next_attempt(question) if question else 1
                if attempt > MAX_ASK_ATTEMPTS:
                    return conclude(
                        state, "没有收集到足够的信息，本次生日安排已结束。", TurnStatus.FAILED
                    )
                return awaiting(spec, outcome, writer, collected=collected, attempt=attempt)
            if not any(item.wants_tool for item in outcomes):
                prose = (outcome.summary or "").strip()
                return conclude(
                    state,
                    prose or "没有找到合适的生日安排，请再说一次。",
                    thread=thread,
                )
            if rounds + len(outcomes) > MAX_READ_ROUNDS:
                return conclude(state, overflow_outcome(spec, summaries).summary)
            thread = [*thread, assistant_turn(text, calls)]
            for item in outcomes:
                name = str(item.tool_name)
                kind, payload = run_call(state, {"tool": name, "arguments": item.arguments}, thread)
                if kind == _DONE:
                    return payload
                tool = tool_outcome(payload)
                thread = [*thread, tool_envelope(DomainOutcome(tool_name=name), tool)]
                summaries.append(tool.summary)
                rounds += 1

    def consume_confirmation(
        state: dict[str, Any],
        question: dict[str, Any],
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        """Seven fail-closed checks, then execute the frozen snapshot."""

        action = state.get("life_action")
        if isinstance(action, dict) and action.get("kind") == "cancel_pending":
            return consume_cancel(state, action, question, answers)
        pending = state.get("life_pending")
        intent = state.get("life_intent")
        if not isinstance(pending, dict) or not isinstance(intent, dict):
            return conclude(
                state, "这笔生日订单的确认信息已失效，请重新发起办理。", TurnStatus.FAILED
            )
        frozen = {key: intent.get(key) for key in _EXECUTE_KEYS}
        amount = frozen.get("amount_minor")
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or any(not isinstance(frozen[key], str) or not frozen[key] for key in _TEXT_KEYS)
        ):
            return conclude(
                state, "这笔生日订单的确认信息已失效，请重新发起办理。", TurnStatus.FAILED
            )
        if expired(pending):
            return conclude(
                state, "这笔生日订单的确认信息已过期，请重新发起办理。", TurnStatus.FAILED
            )
        answered = str(answers.get("pending_id") or "")
        if answered and answered != str(pending.get("pending_id") or ""):
            return conclude(
                state, "这笔生日订单的确认信息已失效，请重新发起办理。", TurnStatus.FAILED
            )
        kind = action_kind(question, answers)
        if kind is TurnAction.CANCEL:
            return conclude(state, "已取消，本次生日订单没有提交。")
        if kind is not TurnAction.CONFIRM:
            return conclude(state, "请明确确认或取消。", TurnStatus.UNSUPPORTED)
        code = (turn.verification_code or "").strip() if turn is not None else ""
        if pending.get("requires_code"):
            if not code:
                return conclude(
                    state,
                    "这笔生日订单超过了免密额度，需要短信验证码；请在确认卡里填入验证码后再提交。",
                    TurnStatus.FAILED,
                )
        elif code:
            # 黄标卡本来就不该带码：多出来的验证码说明这次点击不可信，本地拒掉，别发请求。
            return conclude(
                state,
                "这笔生日订单不需要验证码，本次没有提交，请重新确认。",
                TurnStatus.FAILED,
            )
        if client is None:
            return conclude(state, "生日服务暂时不可用，请稍后重试。", TurnStatus.FAILED)
        invocation = ToolInvocation(
            agent_id=spec.agent_id,
            call=ToolCall(
                tool_name="life_execute_order",
                arguments=dict(frozen),
                idempotency_key=str(pending.get("idempotency_key") or ""),
            ),
            context=spec.handle.trusted_context,
        )
        try:
            data = execute_order(invocation, client, writes=writes, verification_code=code or None)
        except ToolFailure as failure:
            if failure.code == ErrorCode.CONFIRMATION_REQUIRED:
                return conclude(
                    state,
                    f"{failure.message}请重新发起办理。",
                    TurnStatus.FAILED,
                )
            return conclude(state, failure.message, TurnStatus.FAILED)
        except Exception:  # noqa: BLE001 - 图不能因为银行侧异常整条崩掉
            logger.exception("life order submission failed")
            return conclude(state, "生日订单没有提交成功，请稍后重试。", TurnStatus.FAILED)
        order = (data.facts or {}).get("order") if isinstance(data.facts, dict) else None
        order = order if isinstance(order, dict) else {}
        order_id = str(order.get("order_id") or "")
        conclusion = conclude(
            state,
            str(data.summary or "生日订单已提交。"),
            TurnStatus.COMPLETED,
            facts={"order": order} if order else {},
            business_ids=dict(data.business_ids or {}),
        )
        conclusion["life_pending"] = None
        # 记住这一单：用户改主意时 life_cancel_order 直接拿它挂撤销卡，省一次银行核验；
        # 换了会话也还能撤，那时才走 order_to_cancel 的回查。
        conclusion["life_action"] = (
            {"kind": "placed_order", "order_id": order_id, "amount_minor": amount}
            if order_id
            else None
        )
        return conclusion

    def consume_cancel(
        state: dict[str, Any],
        action: dict[str, Any],
        question: dict[str, Any],
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        """Consume the cancel card: local cancel first, then the frozen order id."""

        answered = str(answers.get("pending_id") or "")
        if answered and answered != str(action.get("pending_id") or ""):
            return conclude(state, "这次撤销的确认信息已失效，请重新发起。", TurnStatus.FAILED)
        if expired(action):
            conclusion = conclude(
                state, "这次撤销的确认信息已过期，请重新发起。", TurnStatus.FAILED
            )
            # 挂撤销卡时 `life_action` 被 `cancel_pending` 顶掉了，而「已下单」这条记录是
            # ADR-3 撤销兜底的唯一凭据。不还回去，用户再说一次「撤销」就会被 `hang_cancel`
            # 判成「没有找到编号为 X 的生日订单」——他明明刚下过这一单。还回去的前提是
            # 订单号与金额都还完整；缺一个就清空，宁可让重试失败也不伪造一张卡。
            order_id = action.get("order_id")
            amount = action.get("amount_minor")
            restorable = (
                isinstance(order_id, str)
                and bool(order_id)
                and isinstance(amount, int)
                and not isinstance(amount, bool)
                and amount > 0
            )
            conclusion["life_action"] = (
                {"kind": "placed_order", "order_id": order_id, "amount_minor": amount}
                if restorable
                else None
            )
            return conclusion
        kind = action_kind(question, answers)
        if kind is TurnAction.CANCEL:
            return conclude(state, "已取消，这次生日订单没有撤销。")
        if kind is not TurnAction.CONFIRM:
            return conclude(state, "请明确确认或取消。", TurnStatus.UNSUPPORTED)
        if client is None:
            return conclude(state, "生日服务暂时不可用，请稍后重试。", TurnStatus.FAILED)
        invocation = ToolInvocation(
            agent_id=spec.agent_id,
            call=ToolCall(
                tool_name="life_cancel_order",
                arguments={"order_id": str(action.get("order_id") or "")},
                idempotency_key=str(action.get("idempotency_key") or ""),
            ),
            context=spec.handle.trusted_context,
        )
        try:
            data = cancel_order(invocation, client, writes=writes)
        except ToolFailure as failure:
            return conclude(state, failure.message, TurnStatus.FAILED)
        except Exception:  # noqa: BLE001 - 同上：撤销失败也不许把图带崩
            logger.exception("life order cancellation failed")
            return conclude(state, "生日订单没有撤销成功，请稍后重试。", TurnStatus.FAILED)
        conclusion = conclude(
            state,
            str(data.summary or "生日订单已撤销。"),
            TurnStatus.COMPLETED,
            facts={"order": data.facts.get("order")} if isinstance(data.facts, dict) else {},
            business_ids=dict(data.business_ids or {}),
        )
        conclusion["life_action"] = None
        return conclusion

    def process(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("pending_question") or {}
        answers, problem = read_answer(state.get("pending_answer"), question)
        if problem:
            attempt = next_attempt(question)
            if attempt > MAX_ASK_ATTEMPTS:
                return conclude(state, "未获得有效输入，本次生日安排已结束。", TurnStatus.FAILED)
            return {
                "pending_question": {
                    **question,
                    "attempt": attempt,
                    "display": f"{problem}\n{question.get('display') or ''}",
                },
                "pending_answer": None,
                "turn_phase": "waiting_for_user",
            }
        if question.get("kind") == CONFIRMATION_KIND:
            return consume_confirmation(state, question, answers or {})
        return decide(state, answers=answers or {})

    def route(state: dict[str, Any]) -> str:
        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(LifeState)
    graph.add_node("decide", domain_node(spec, assembler, turn, decide))
    graph.add_node("wait", wait_for_user)
    graph.add_node("process", domain_node(spec, assembler, turn, process))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", route, {"wait": "wait", END: END})
    graph.add_edge("wait", "process")
    graph.add_conditional_edges("process", route, {"wait": "wait", END: END})
    return graph.compile()


def _label(name: str) -> str:
    return {
        "life_birthdays": "查询生日",
        "life_gift_plan": "挑选礼物",
        "life_prepare_order": "准备生日订单",
        "life_cancel_order": "撤销生日订单",
    }.get(name, "生日操作")
