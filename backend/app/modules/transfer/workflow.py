"""转账的自有图：一整轮、原生工具调用、平台确认卡。

三节点 `decide → wait → process`，与 card 同构：

- `decide` 只读与解析（`transfer_prepare` 从不写银行），把结果放进 facts；
- `wait` 就是平台的 `wait_for_user`，它把确认卡/选择题写进检查点并 interrupt；
- `process` 消费点击：确认才走 `writes.submit_transfer` 发那一次 `POST /transfers`，
  验证码只来自本次调用的 `turn.verification_code`（闭包进来，从不进 state）。

图在每次请求重建（基座 `_graph_for`），不带 checkpointer。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agents.decisions import DecisionError, parse_domain_decision
from app.agents.sdk import (
    CONFIRMATION_KIND,
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
    read_answer,
    wait_for_user,
)
from app.agents.state import AgentState, idle_work_state
from app.agents.toolcalls import StreamTruncated, collect_stream
from app.agents.upstream import UpstreamUnavailable
from app.modules.base import ToolFailure
from app.modules.money import format_minor
from app.modules.transfer.writes import create_aa, create_scheduled, pay_aa, submit_transfer
from app.tools.normalize import normalize_arguments
from app.tools.protocol import ErrorCode, ToolStatus
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# transfer_cancel / transfer_aa_cancel 对模型可见，但 run_call 一律先把它拦成确认卡：
# 只有用户点击才经网关执行。
CONFIRMATION_TOOLS = ("transfer_cancel", "transfer_aa_cancel")

_RESULT_TURN_STATUS = {
    ToolStatus.SUCCEEDED: TurnStatus.COMPLETED,
    ToolStatus.FAILED: TurnStatus.FAILED,
    ToolStatus.DENIED: TurnStatus.UNSUPPORTED,
}

PROMPT = """你是转账 Agent，只理解需求和补全信息。工具名和参数以工具定义为准。
**办理即时转账时直接调用 transfer_prepare**：它会自己解析付款账户和收款人，不要先调 transfer_accounts 或 transfer_contacts。
**每轮只调用一个工具**：平台每轮只接受一个工具调用，两个一起调会被整轮拒绝。
transfer_accounts / transfer_contacts / transfer_scheduled 只在用户单纯查询时使用。
用户只是查询账户、余额或收款人时，直接调用对应工具并如实回答，不要反问、不要进入转账准备。
一次只办理一笔转账；是否成功只根据工具结果判断，不要自行确认、验证或声称转账成功。
金额用人民币元字符串（例如 5000.00），不要换算为分或做金额计算。
缺少信息时只用 ask_user 追问真正缺少的 amount_yuan / to_name；from_hint、remark 都是可选项，不要追问。用户原话里的称呼（例如「家人」「妈妈」「张三」）就是 to_name，不得要求真实姓名、账号或备注；用户已经给出「给X转N元」时，必须直接调用 transfer_prepare，amount_yuan=N、to_name=X，不要先追问。
**办理定时转账时用 transfer_schedule**：用户说「明天／后天／每月／定时／预约」时，调用它并给出 amount_yuan、to_name、execute_date；execute_date 可原样传「明天」「后天」或 2026-11-01，execute_time 默认 09:00。绝不要把这句当成即时转账执行。
取消定时转账用 transfer_cancel（代码会要求用户确认，不要自己声称已取消）。
**AA 收款（AA／平摊／均摊／人均／凑钱）用 transfer_aa**：amount_yuan 是要收取的总额；participants 传参与人姓名列表，用户只说人数（「我们 4 个人」）时改传 participant_count=4（含发起人自己）；发起人自己也占一份时 include_me=true。AA 收款是生成收款请求，绝不要当成转账直接转出。查 AA 进度用 transfer_aa_status（AA 编号在发起成功的回复里）；支付我那一份用 transfer_aa_pay；取消 AA 用 transfer_aa_cancel（代码会要求用户确认）。缺少信息时只用 ask_user 追问 amount_yuan / participants（或 participant_count）。
转账的确认与验证码由代码和页面处理，你不需要询问，也不要输出确认卡的内容。"""


# 转账意图已经明确时，只把 transfer_prepare 交给模型。
# 账户/联系人查询工具会把模型带向「先查一遍再办」的路径；平台每轮只接受一个
# 工具调用，两个查询一起返回会让整轮被拒，用户看到的只是「没有可用的转账信息」。
_QUERY_MARKERS = ("查", "看看", "哪些", "列表", "余额", "明细", "取消", "定时")


def _last_user_text(state: dict[str, Any]) -> str:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, dict):
            role = message.get("role") or message.get("type")
            content = message.get("content")
        else:
            role = getattr(message, "type", None)
            content = getattr(message, "content", None)
        if role in ("user", "human") and isinstance(content, str):
            return content
    return state.get("goal") or ""


def _transfer_only(state: dict[str, Any]) -> bool:
    text = _last_user_text(state)
    if not text:
        return False
    if any(marker in text for marker in _QUERY_MARKERS):
        return False
    return re.search(r"(?:转账|转给|转\s*[0-9]|给.{0,20}转)", text) is not None


# 真实模型（尤其是把「家人」当成需要补全真实姓名的模型）会在「给家人转 100 元」
# 这种已经完整的句子上反问 to_name。这里只抓最明确、无歧义的「给X转N元」和
# 「转给X N元」两种说法，直接进入 transfer_prepare；其余情况仍然交给模型。
# 这样既不让模型把已给出的称呼当成空槽，也不改变工具的确认/风控语义。
_SCHEDULE_MARKERS = ("明天", "明日", "后天", "后日", "定时", "预约", "每月", "每周", "下个月", "下周")

# AA 收款的措辞。注意**不能**拿「收款」当标记 ——「收款人」是转账语境的高频词。
_AA_MARKERS = ("AA", "aa", "平摊", "均摊", "分摊", "人均", "凑钱", "各付各")

_DIRECT_TRANSFER_PATTERNS = (
    re.compile(
        r"给\s*(?P<name>[^\s，,。；;：:]+?)\s*(?:转账|转)\s*(?P<amount>[0-9]+(?:\.[0-9]{1,2})?)\s*元"
    ),
    re.compile(
        r"转给\s*(?P<name>[^\s，,。；;：:]+?)\s*[，,]?\s*(?P<amount>[0-9]+(?:\.[0-9]{1,2})?)\s*元"
    ),
)


def _direct_transfer(state: dict[str, Any]) -> tuple[str, str] | None:
    text = _last_user_text(state)
    if not text:
        return None
    if any(marker in text for marker in _SCHEDULE_MARKERS):
        # 「明天给老王转 100 元」是定时转账，不能被即时直通吞掉。
        return None
    if any(marker in text for marker in _AA_MARKERS):
        # 「给同事转 AA 收款 100 元」是收款请求，也不是即时转账。
        return None
    for pattern in _DIRECT_TRANSFER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("name"), match.group("amount")
    return None


class TransferState(AgentState, total=False):
    transfer_intent: dict[str, Any] | None
    transfer_pending: dict[str, Any] | None
    transfer_call: dict[str, Any] | None
    transfer_choices: dict[str, str]
    transfer_field: str | None


def _selection_label(call: dict[str, Any] | None, field: str | None) -> str:
    """选择题的字段名按工具语境换词：AA 的 from_account 是收款账户，aa_participant 是我的一份。"""

    if field == "aa_participant":
        return "我的一份"
    if field == "from_account" and (call or {}).get("tool") == "transfer_aa":
        return "收款账户"
    return "付款账户" if field == "from_account" else "收款人"


def _card_confirmation(operation, intent: dict[str, Any], facts: dict[str, Any]) -> TurnConfirmation:
    """把解析结果冻结成一张确认卡。操作不同，卡上的措辞与金额来源不同。"""

    if operation == "aa_collect":
        names = "、".join(str(item.get("name") or "") for item in facts.get("split_preview") or [])
        if len(names) > 60:
            names = names[:60] + "…"
        return TurnConfirmation(
            pending_id=f"taa_{uuid4().hex}",
            operation="aa_collect",
            title="确认发起 AA 收款",
            amount_minor=intent["total_amount_minor"],
            product_name=f"{facts.get('participant_count')} 人分摊 · {names}",
            account_display=str(facts["from_label"]),
            requires_code=False,
        )
    if operation == "aa_payment":
        aa = facts.get("aa") or {}
        return TurnConfirmation(
            pending_id=f"tap_{uuid4().hex}",
            operation="aa_payment",
            title="确认支付 AA 分摊",
            amount_minor=intent["amount_minor"],
            product_name=str(aa.get("remark") or intent["aa_request_id"]),
            account_display=str(facts["from_label"]),
            requires_code=bool(facts["requires_code"]),
        )
    scheduled = operation == "schedule_transfer"
    return TurnConfirmation(
        pending_id=f"tp_{uuid4().hex}",
        operation="schedule_transfer" if scheduled else "transfer",
        title="确认定时转账" if scheduled else "确认转账",
        amount_minor=intent["amount_minor"],
        product_name=(
            f"{facts['to_label']} · {facts['execute_at_label']}"
            if scheduled
            else str(facts["to_label"])
        ),
        account_display=str(facts["from_label"]),
        requires_code=bool(facts["requires_code"]),
    )


def build_transfer_graph(model, spec, *, assembler=None, turn=None, client=None, writes=None):
    """转账的领域图。基座把 model／spec／assembler／turn／client／writes 交进来。"""

    # 注意：**不在建图时**要求 client。基座为每个域、每次请求都重建一次图，一个模块不能因为
    # 「这次没有银行客户端」就让整张图建不起来 —— 那会让一次与转账无关的请求也 502。读工具走
    # 网关自己的客户端；只有那一次 POST /transfers 需要这个 client，所以失败推迟到 process。
    # `writes` 是组合根注入的那一把门面（`transfer/__init__.py` 的 `bind_transfer` 用 partial
    # 绑进来的，装配由组合根触发、由模块自己完成）：
    # 有它就走门面策略与审计，没有它 `submit_transfer` 才退回自己造一把 —— 生产路径永远有。

    def invoke(name, arguments):
        writer = get_stream_writer()
        outcome = DomainOutcome(tool_name=name, arguments=dict(arguments))
        key = f"transfer:{name}:{uuid4().hex}"
        result = call_tool(spec, outcome, writer, idempotency_key=key)
        if may_retry(spec, result):
            result = call_tool(spec, outcome, writer, idempotency_key=key)
            if may_retry(spec, result):
                raise UpstreamUnavailable("TOOL_UNAVAILABLE")
        return result

    def conclude(state, summary, status=TurnStatus.COMPLETED, facts=None, business_ids=None):
        """A terminal ending. Never used for a pause: that goes through conclude_turn itself."""

        result = TurnResult(
            status=status,
            summary=summary,
            facts=facts or {},
            business_ids=business_ids or {},
        )
        conclusion = conclude_turn(spec, result, get_stream_writer(), state, ())
        conclusion.update(
            {
                "transfer_intent": None,
                "transfer_pending": None,
                "transfer_call": None,
                "transfer_choices": {},
                "transfer_field": None,
                **idle_work_state(),
            }
        )
        return conclusion

    def from_result(state, result):
        if not result.succeeded:
            return conclude(
                state,
                f"转账操作未成功：{result.error}",
                _RESULT_TURN_STATUS[result.status],
            )
        data = result.data if isinstance(result.data, dict) else {}
        return conclude(state, data.get("summary") or "已完成。")

    def prepare(state, call, result):
        data = result.data if isinstance(result.data, dict) else {}
        facts = data.get("facts") or {}
        status = facts.get("status")

        if status == "select":
            choices: dict[str, Any] = {}
            for item in facts.get("candidates") or []:
                label = item.get("label") if isinstance(item, dict) else None
                value = item.get("value") if isinstance(item, dict) else None
                if not isinstance(label, str) or not label or value in (None, "") or label in choices:
                    return conclude(state, "候选账户或收款人无法安全选择，请重新说明。", TurnStatus.FAILED)
                choices[label] = value
            if not choices:
                return conclude(state, "没有可选的候选账户或收款人，请重新说明。", TurnStatus.FAILED)
            field = facts.get("field")
            question = {
                "kind": "transfer_selection",
                "question_id": f"q_{uuid4().hex}",
                "agent_id": spec.agent_id,
                "attempt": 1,
                "display": data.get("summary") or "请选择。",
                "fields": [
                    {
                        "name": "selection",
                        "label": _selection_label(call, field),
                        "type": "select",
                        "required": True,
                        "options": list(choices),
                    }
                ],
            }
            return {
                "transfer_call": call,
                "transfer_choices": choices,
                "transfer_field": field,
                "pending_question": question,
                "pending_answer": None,
                "turn_phase": "waiting_for_user",
            }

        if status != "ready":
            return conclude(state, "转账意图解析结果不正确，请重新说明。", TurnStatus.FAILED)

        intent = dict(facts["intent"])
        operation = facts.get("operation")
        confirmation = _card_confirmation(operation, intent, facts)
        fallback = {
            "schedule_transfer": "请确认这笔定时转账。",
            "aa_collect": "请确认发起这笔 AA 收款。",
            "aa_payment": "请确认支付这笔 AA 分摊。",
        }.get(operation, "请确认这笔转账。")
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=data.get("summary") or fallback,
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update(
            {
                "transfer_intent": intent,
                "transfer_pending": {
                    "pending_id": confirmation.pending_id,
                    "idempotency_key": f"transfer-{uuid4().hex}",
                },
                "transfer_call": None,
                "transfer_choices": {},
                "transfer_field": None,
            }
        )
        return conclusion

    def hang_cancel(state, args):
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return conclude(state, "请提供要取消的定时转账编号。", TurnStatus.FAILED)
        listing = invoke("transfer_scheduled", {})
        rows = []
        if listing.succeeded and isinstance(listing.data, dict):
            rows = (listing.data.get("facts") or {}).get("scheduled") or []
        task = next(
            (row for row in rows if isinstance(row, dict) and row.get("task_id") == task_id), None
        )
        amount = task.get("amount_minor") if isinstance(task, dict) else None
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return conclude(state, f"没有找到编号为 {task_id} 的定时转账。", TurnStatus.FAILED)
        confirmation = TurnConfirmation(
            pending_id=f"tc_{uuid4().hex}",
            operation="cancel_scheduled_transfer",
            title="确认取消定时转账",
            amount_minor=amount,
            product_name="定时转账",
            account_display=task_id,
            requires_code=False,
        )
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=f"请确认取消定时转账 {task_id}（{format_minor(amount)}）。",
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update(
            {
                "transfer_intent": None,
                "transfer_call": {"tool": "transfer_cancel", "arguments": {"task_id": task_id}},
                "transfer_pending": {
                    "pending_id": confirmation.pending_id,
                    "idempotency_key": f"transfer-{uuid4().hex}",
                },
                "transfer_choices": {},
                "transfer_field": None,
            }
        )
        return conclusion

    def hang_aa_cancel(state, args):
        aa_request_id = str(args.get("aa_request_id") or "").strip()
        if not aa_request_id:
            return conclude(state, "请提供要取消的 AA 收款编号。", TurnStatus.FAILED)
        detail = invoke("transfer_aa_status", {"aa_request_id": aa_request_id})
        aa = {}
        if detail.succeeded and isinstance(detail.data, dict):
            facts = detail.data.get("facts") or {}
            if isinstance(facts.get("aa"), dict):
                aa = facts["aa"]
        total = aa.get("total_amount_minor")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            return conclude(state, f"没有找到编号为 {aa_request_id} 的 AA 收款。", TurnStatus.FAILED)
        confirmation = TurnConfirmation(
            pending_id=f"tac_{uuid4().hex}",
            operation="aa_cancel",
            title="确认取消 AA 收款",
            amount_minor=total,
            product_name=str(aa.get("remark") or "AA 收款"),
            account_display=aa_request_id,
            requires_code=False,
        )
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=f"请确认取消 AA 收款 {aa_request_id}（{format_minor(total)}）。",
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update(
            {
                "transfer_intent": None,
                "transfer_call": {
                    "tool": "transfer_aa_cancel",
                    "arguments": {"aa_request_id": aa_request_id},
                },
                "transfer_pending": {
                    "pending_id": confirmation.pending_id,
                    "idempotency_key": f"transfer-{uuid4().hex}",
                },
                "transfer_choices": {},
                "transfer_field": None,
            }
        )
        return conclusion

    def run_call(state, call):
        name = call["tool"]
        args = normalize_arguments(call.get("arguments"))
        if name in CONFIRMATION_TOOLS:
            # 写工具对模型可见，但绝不直接执行：先挂卡，用户点击后才经网关写。
            if name == "transfer_aa_cancel":
                return hang_aa_cancel(state, args)
            return hang_cancel(state, args)
        declared = spec.handle.spec(name)
        if declared is None:
            return conclude(state, "该转账操作不被允许。", TurnStatus.UNSUPPORTED)
        # 校验用**原始声明**、但先去掉 null：模型对可选参数有两种写法（送 null 或整个省略），
        # 归一化成「省略」再校验，两种写法都能过，而真正的约束照旧生效。
        validator = Draft202012Validator(declared.parameters)
        if not validator.is_valid(args):
            # 参数被拒时把「哪个工具、错在哪、收到的参数」写进日志：只给用户一句通用中文
            # 的话，线上没人能看出模型到底送了什么。
            logger.warning(
                "transfer tool %s arguments rejected: %s | args=%r",
                name,
                next(iter(validator.iter_errors(args)), None),
                args,
            )
            return conclude(state, "转账参数不完整或格式不正确，请重新说明。", TurnStatus.FAILED)
        result = invoke(name, args)
        if not result.succeeded or name not in (
            "transfer_prepare",
            "transfer_schedule",
            "transfer_aa",
            "transfer_aa_pay",
        ):
            return from_result(state, result)
        return prepare(state, call, result)

    def choose(state, question, answers):
        choices = state.get("transfer_choices") or {}
        field = state.get("transfer_field")
        selection = answers.get("selection")
        if selection not in choices:
            return conclude(state, "所选账户或收款人不在当前候选列表中。", TurnStatus.UNSUPPORTED)
        call = dict(state.get("transfer_call") or {})
        arguments = dict(call.get("arguments") or {})
        if field == "from_account":
            arguments["from_account_id"] = choices[selection]
        elif field == "aa_participant":
            arguments["participant_index"] = choices[selection]
        else:
            arguments["to_account_id"] = choices[selection]
        return run_call(state, {"tool": call.get("tool") or "transfer_prepare", "arguments": arguments})

    def upgrade_card(state, question, intent, pending, failure):
        """银行要码而卡片没长码框时：把同一笔的卡升级为要码，再用同一把幂等键重试。"""

        old = question.get("confirmation") or {}
        noun = "这笔 AA 分摊支付" if str(old.get("operation")) == "aa_payment" else "这笔转账"
        confirmation = TurnConfirmation(
            pending_id=str(old.get("pending_id") or f"tp_{uuid4().hex}"),
            operation=str(old.get("operation") or "transfer"),
            title=str(old.get("title") or "确认转账"),
            amount_minor=intent.get("amount_minor", intent.get("total_amount_minor")),
            product_name=str(old.get("product_name") or "转账"),
            account_display=str(old.get("account_display") or "付款账户"),
            confirm_label=str(old.get("confirm_label") or "确认"),
            cancel_label=str(old.get("cancel_label") or "取消"),
            requires_code=True,
        )
        conclusion = conclude_turn(
            spec,
            TurnResult(
                status=TurnStatus.WAITING_CONFIRMATION,
                summary=f"{failure.message}\n{noun}需要验证码，请填写后再次确认。",
                confirmation=confirmation,
            ),
            get_stream_writer(),
            state,
            (),
        )
        conclusion.update({"transfer_intent": intent, "transfer_pending": pending})
        return conclusion

    def decide(state, *, answers=None):
        get_stream_writer()({"event": "agent_start", "agent_id": spec.agent_id})
        direct = _direct_transfer(state)
        if direct is not None and not answers:
            logger.info("transfer_direct_prepare to_name=%s amount_yuan=%s", direct[0], direct[1])
            return run_call(
                state,
                {
                    "tool": "transfer_prepare",
                    "arguments": {"amount_yuan": direct[1], "to_name": direct[0]},
                },
            )
        all_tools = list(spec.handle.tools)
        tools = (
            [
                item
                for item in all_tools
                if item.name not in ("transfer_accounts", "transfer_contacts")
            ]
            if _transfer_only(state)
            else all_tools
        )
        schemas = tuple(function_schema(item, strict=False) for item in tools) + (
            function_schema(ask_user_spec(), strict=False),
        )
        messages = (
            {"role": "system", "content": PROMPT},
            {
                "role": "user",
                "content": (
                    f"指令：{state.get('goal') or '按用户请求办理转账'}\n"
                    f"已补充信息：{json.dumps(answers or {}, ensure_ascii=False)}"
                ),
            },
        )
        try:
            text, calls = collect_stream(model, messages, schemas)
        except StreamTruncated:
            return conclude(state, "模型输出中断，本次没有发起任何转账。", TurnStatus.FAILED)
        known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}
        try:
            outcome = domain_outcome(parse_domain_decision(text, calls, known))
        except DecisionError as error:
            # 决策不可用时，日志里留下是哪个调用被拒 —— 没有这一行，线上只能看到一句通用中文。
            logger.warning(
                "transfer decide rejected: %s | text=%r calls=%r",
                error,
                text[:200],
                [(call.name, dict(call.arguments)) for call in calls],
            )
            return conclude(state, "模型没有给出可用的转账信息，请重新说明。", TurnStatus.UNSUPPORTED)
        if outcome.ask is not None:
            fields = outcome.ask.get("fields") or []
            if not fields or any(name not in spec.declaration.fields for name in fields):
                return conclude(state, "请说明转账金额、收款人和付款账户。", TurnStatus.UNSUPPORTED)
            attempt = int((state.get("pending_question") or {}).get("attempt", 0)) + 1
            return awaiting(
                spec, outcome, get_stream_writer(), collected=answers or {}, attempt=attempt
            )
        if outcome.wants_tool:
            return run_call(state, {"tool": outcome.tool_name, "arguments": outcome.arguments})
        # Ruling: 模型只回一句话时，用它的原话收尾（例如「当前不支持预约」），而不是换成通用追问
        # —— 否则用户永远看不到那句解释。状态仍是 UNSUPPORTED：事件层是 DENIED，不是成功；
        # 提示词已明确禁止声称转账成功，且这条路径没有任何写调用。
        prose = (outcome.summary or "").strip()
        return conclude(
            state,
            prose or "请说明要转账的金额、收款人与付款账户；当前尚未发起任何转账。",
            TurnStatus.UNSUPPORTED,
        )

    def process(state):
        question = state.get("pending_question") or {}
        answers, problem = read_answer(state.get("pending_answer"), question)
        if problem:
            attempt = int(question.get("attempt", 1)) + 1
            if attempt > MAX_ATTEMPTS:
                return conclude(state, "未获得有效输入，本次转账已结束。", TurnStatus.FAILED)
            return {
                "pending_question": {
                    **question,
                    "attempt": attempt,
                    "display": f"{problem}\n{question.get('display') or ''}",
                },
                "pending_answer": None,
                "turn_phase": "waiting_for_user",
            }
        if question.get("kind") == "transfer_selection":
            return choose(state, question, answers)
        if question.get("kind") == CONFIRMATION_KIND:
            kind = action_kind(question, answers)
            if kind is None:
                return conclude(state, "请明确确认或取消。", TurnStatus.UNSUPPORTED)
            if kind is TurnAction.CANCEL:
                # 转账在 POST /transfers 之前不存在，所以取消是本地收尾：银行零写调用。
                operation = (question.get("confirmation") or {}).get("operation")
                if operation in ("aa_collect", "aa_payment", "aa_cancel"):
                    return conclude(state, "已取消，本次 AA 操作没有发起。")
                return conclude(state, "已取消，本次转账没有发起。")
            call = state.get("transfer_call")
            if isinstance(call, dict) and call.get("tool") == "transfer_cancel":
                return from_result(
                    state, invoke("transfer_cancel", dict(call.get("arguments") or {}))
                )
            if isinstance(call, dict) and call.get("tool") == "transfer_aa_cancel":
                return from_result(
                    state, invoke("transfer_aa_cancel", dict(call.get("arguments") or {}))
                )
            intent = state.get("transfer_intent")
            pending = state.get("transfer_pending")
            if not intent or not pending:
                return conclude(state, "这笔转账的确认信息已失效，请重新说明转账需求。", TurnStatus.FAILED)
            if client is None:
                # 没有银行客户端就写不出去；如实收尾，绝不让「查不到」看起来像「没有」。
                return conclude(state, "转账服务暂时不可用，请稍后重试。", TurnStatus.FAILED)
            intent_operation = intent.get("operation")
            scheduled = bool(intent.get("execute_at"))
            try:
                if intent_operation == "aa_collect":
                    response = create_aa(
                        client,
                        spec.handle.trusted_context,
                        intent,
                        pending,
                        verification_code=(turn.verification_code if turn is not None else None),
                        writes=writes,
                    )
                elif intent_operation == "aa_payment":
                    response = pay_aa(
                        client,
                        spec.handle.trusted_context,
                        intent,
                        pending,
                        verification_code=(turn.verification_code if turn is not None else None),
                        writes=writes,
                    )
                elif scheduled:
                    response = create_scheduled(
                        client,
                        spec.handle.trusted_context,
                        intent,
                        pending,
                        verification_code=(turn.verification_code if turn is not None else None),
                        writes=writes,
                    )
                else:
                    response = submit_transfer(
                        client,
                        spec.handle.trusted_context,
                        intent,
                        pending,
                        verification_code=(turn.verification_code if turn is not None else None),
                        writes=writes,
                    )
            except ToolFailure as failure:
                if failure.code == ErrorCode.CONFIRMATION_REQUIRED:
                    return upgrade_card(state, question, intent, pending, failure)
                return conclude(state, failure.message, TurnStatus.FAILED)
            data = response.data if isinstance(response.data, dict) else None
            if intent_operation == "aa_collect":
                aa_request_id = data.get("aa_request_id") if isinstance(data, dict) else None
                if not aa_request_id:
                    return conclude(
                        state, "银行没有返回 AA 收款编号，请稍后重新发起。", TurnStatus.FAILED
                    )
                return conclude(
                    state,
                    f"AA 收款已发起：总额 {format_minor(intent['total_amount_minor'])}，"
                    f"{len(intent.get('participants') or [])} 人分摊。"
                    f"编号 {aa_request_id}，7 天内有效。",
                    TurnStatus.COMPLETED,
                    facts={
                        "aa_request_id": aa_request_id,
                        "total_amount_minor": intent["total_amount_minor"],
                    },
                    business_ids={"aa_request_id": aa_request_id},
                )
            if intent_operation == "aa_payment":
                transaction_id = data.get("transaction_id") if isinstance(data, dict) else None
                if not transaction_id:
                    return conclude(
                        state, "银行没有返回支付回执，请稍后在流水中核实。", TurnStatus.FAILED
                    )
                return conclude(
                    state,
                    f"AA 分摊已支付：{format_minor(intent['amount_minor'])}。交易号 {transaction_id}。",
                    TurnStatus.COMPLETED,
                    facts={
                        "transaction_id": transaction_id,
                        "amount_minor": intent["amount_minor"],
                        "aa_request_id": intent["aa_request_id"],
                    },
                    business_ids={
                        "transaction_id": transaction_id,
                        "aa_request_id": intent["aa_request_id"],
                    },
                )
            if scheduled:
                task_id = data.get("task_id") if isinstance(data, dict) else None
                if not task_id:
                    return conclude(state, "银行没有返回定时转账编号，请稍后在定时转账列表中核实。", TurnStatus.FAILED)
                return conclude(
                    state,
                    f"定时转账已创建：{format_minor(intent['amount_minor'])}，将于 {intent.get('execute_at_label') or intent['execute_at']} 执行。",
                    TurnStatus.COMPLETED,
                    facts={
                        "task_id": task_id,
                        "execute_at": intent["execute_at"],
                        "amount_minor": intent["amount_minor"],
                    },
                    business_ids={"task_id": task_id},
                )
            transaction_id = data.get("transaction_id") if isinstance(data, dict) else None
            if not transaction_id:
                return conclude(state, "银行没有返回转账回执，请稍后在流水中核实。", TurnStatus.FAILED)
            return conclude(
                state,
                f"转账已完成：{format_minor(intent['amount_minor'])}。交易号 {transaction_id}。",
                TurnStatus.COMPLETED,
                facts={"transaction_id": transaction_id, "amount_minor": intent["amount_minor"]},
                business_ids={"transaction_id": transaction_id},
            )
        collected = {**(state.get("collected_inputs") or {}), **answers}
        return decide(state, answers=collected)

    def route(state):
        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(TransferState)
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
