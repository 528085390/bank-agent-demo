"""账单模块自己的图：一个模型循环 + 确认协议（冻结 → 点击确认 → 执行）。

为什么账单需要一个图：`agents/supervisor/graph.py:427-434` 拒绝「声明了业务工具却没有
graph_builder」的域。账单有四个业务工具，所以它必须自己说清「一轮怎么走」。

图与订阅同形（spec §6.3）：decide → route → wait(wait_for_user) → process → route → END。
整段模型循环只住在 activate 里，而且**它自己从不 interrupt** —— 要问用户时它把问题写进
pending_question 然后返回，由 wait 节点去 interrupt。这样一条停下来的 activation 会被原样
重放，而不会在重放时再问模型一次。

发报告分两段：模型只调 `bill_send_report`（prepare）—— 它校验收件人、把这次操作**冻结**成
bill_action，一次都不发信；用户点击确认后 process 走 consume_confirmation，用**快照里的**期间
与收件人、以及**同一个 idempotency_key** 调隐藏的 `bill_send_report_execute`。冻结之后再换绑
邮箱、或另说一段期间，都改不了已经发出去的那一封。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.agents.decisions import DecisionError, parse_domain_decision
from app.agents.grounding import guard_summary
from app.agents.sdk import (
    MAX_TOOL_ROUNDS,
    DomainOutcome,
    TurnConfirmation,
    TurnStatus,
    awaiting,
    call_tool,
    current_request,
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
from app.storage.models import AgentRunEvent
from app.tools.protocol import SideEffect, ToolStatus
from app.tools.schema import ASK_USER_TOOL_NAME, ask_user_spec, function_schema

logger = logging.getLogger(__name__)

#: ToolStatus → 领域回合状态。DENIED 说成 unsupported：那是「这个操作不给做」，不是「做失败了」。
_BILL_TURN_STATUS = {
    ToolStatus.SUCCEEDED: TurnStatus.COMPLETED.value,
    ToolStatus.FAILED: TurnStatus.FAILED.value,
    ToolStatus.DENIED: TurnStatus.UNSUPPORTED.value,
}

_READ_ROUND = "read"
_PARKED = "parked"
_DONE = "done"

#: 确认卡的寿命。过期就 fail-closed 地拒绝，而不是拿一张旧卡去发信。
PENDING_EXPIRY_MINUTES = 5

#: 模型菜单里**没有**的执行工具：它只能由确认动作直接调用（纵深防御，见 _HIDDEN 的两处用法）。
_HIDDEN = frozenset({"bill_send_report_execute"})

#: 模型爱把「没填」写成这些字面量。它们不是值。
_PLACEHOLDERS = frozenset({"", "null", "none", "undefined"})

_OPERATION = "send_report"
_OPERATION_LABEL = "发送账单分析报告"

_BILL_PROMPT = """你是账单 Agent，只理解用户的需求。工具名和参数以工具定义为准。

一次只办理一个操作；操作是否成功只根据工具结果判断，不要自行确认或声称已经发送 —— 确认与执行由代码处理。

工具选择必须严格按用户的话来分：

- 用户说「分析 / 统计 / 报告 / 趋势 / 分类汇总」→ 只调用 bill_analyze_comprehensive。它内部已经读取了流水，**绝对不要同时调用 bill_query_transactions**。
- 用户在新的一轮里再次要求分析时，**必须重新调用分析工具**，不要复述上一轮的结论——期间可能已经导入了新的流水。只有用户只是要求「看看已有结果」时才复述。
- **只有当前这一条用户消息**里带**任务号**（形如「任务 task_…」）时，才把它原样放进 `task_id`，此时不要追问日期。查询用 bill_query_transactions；分析用 bill_analyze_comprehensive；发送用 bill_send_report。任务号只抄一次，不要改写、不要省略。
- **历史消息里出现过的任务号、日期、期间一律不算。** 用户换了个说法（例如只说「我要分析流水」「再分析一下」）时，就当这次没有任务号、也没有日期，按下面的日期规则处理：没有明确日期就先问，**不要拿上一次成功分析的期间或任务号再跑一遍**——那只会把同一份报告原样再发一次。
- 用户要**年报 / 年度账单**（「出 2025 年年报」「去年的年度报告」）→ 分析工具的两个日期都传**整年写法**：2025年、去年、前年（解析器会取 1 月 1 日到 12 月 31 日）。不要自己拆成两个具体日期，也不要按月或按日。
- **绝不要向用户索要任务号。** 任务号只由页面在用户点按钮时自动带上；用户记不住它，也不该被要求记住。当前消息里没有任务号，就当它不存在，按下面的日期规则处理。
- **唯一的例外：重试失败的那一次。** 用户说「重新 / 重来 / 重做 / 再试一次」，**并且上一轮分析没有成功**（工具报错，或报告没生成）时，可以沿用上一轮的 `task_id` 或日期再试一次，不必重复追问。**上一轮已经成功时不适用**：那仍算新的一次请求，按上面的规则来。
- 用户明确说「查流水 / 列明细 / 看某笔交易」→ 才用 bill_query_transactions；它只用于查看明细，不用于分析、统计或出报告。
- 用户**只要求发送**（「把账单分析报告发到我邮箱」「发到邮箱」，或句子里带「只发送 / 无需再展示」）→ **只调用 bill_send_report**。句子里出现「分析报告 / 账单分析报告」是**报告的名字**，不代表现在要分析；**不要先调用 bill_analyze_comprehensive**，发送时服务端会按这段时间自己生成报告内容。不要询问收件邮箱，也不要问「要不要发送」；recipient 留空，工具会自动使用用户已绑定的邮箱；未绑定会返回明确提示，再让用户去设置里绑定。
- 用户**明确要求先看分析、再发送**（「分析一下并发送到邮箱」「先分析再发」）且期间已经明确 → 先调用 bill_analyze_comprehensive，让分析结果在前端显示；拿到结果后，再调用 bill_send_report。不要在分析之前直接发信，也不要在同一次模型输出里同时请求这两个工具。

**每次模型输出只能请求一个工具；严禁在一次输出里同时请求查询和分析。工具结果返回后，可以再决定下一步（例如分析完成后接着发信）。** 代码会弹确认卡，不要在回复里自己问确认，也不要替用户确认；真正的发送由代码在用户点击确认后调用 bill_send_report_execute，那个工具不在你的菜单里。

期间（period_start / period_end）与任务号（task_id）二选一：**当前这条消息**里带任务号时用 task_id、不问日期；没有任务号时期间必填，而追问的是日期——用 ask_user 问 `period_start` / `period_end`（「要分析哪段时间？」），只给日期字段，绝不问「任务号是什么」。不要编造日期，**也不要用历史对话里的日期或期间**（除非用户在当前消息里明确提到，或属于上面「重试失败的那一次」）；日期可以直接用用户的原话（9月13日、今年5月1日、五月一日、今天、昨天、前天），解析交给 app/modules/dates.py 的 parse_tool_date。**用户说「今年」时把「今年」原样传下来，不要自己换算成年份，也不要把它改成其他年份。**

查明细一次只取一页（每页固定 20 条）；不要传 page_size=100/200，也不要为了凑行数重复查询同一区间。翻页由前端表格处理。

只把你确实打算用的可选参数（category / direction / page / page_size / modules / recipient）放进参数，不要写成 null 或 "null"。

不要编造金额、商户、账户或收件人；报告里的数字只能来自工具结果。"""


# ── 范围清洗：只认「本轮用户原话」给出的任务号与日期 ─────────────────────────────
#
# 总调度会把历史里的任务号和期间"转述"进交给本领域的 instruction（实测同一句话有时转、
# 有时不转），而账单 Agent 看到的就是这条 instruction —— 于是「我要分析流水」会被当成
# 已经给了范围，原样重跑上一份报告。这里在喂给模型之前按**本轮用户原话**清一遍。

#: 本次上传的任务号。只认用户原话里出现过的。
_TASK_ID = re.compile(r"task_[0-9a-f]{32}")

#: 日期写法（检测用）：标准 / 点分 / 紧凑 / 中文 / 中文数字 / 只有月。
_DATE = (
    r"(?:\d{4}\s*[-/.年]\s*\d{1,2}(?:\s*[-/.月]\s*\d{1,2}\s*[日号]?)?"
    r"|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号]?)?"
    r"|[一二三四五六七八九十]{1,3}\s*月(?:\s*[一二三四五六七八九十]{1,3}\s*日)?"
    r"|\b20\d{6}\b)"
)
_DATE_RANGE = re.compile(rf"{_DATE}\s*(?:至|到|~|～|—|-)\s*{_DATE}")
_DATE_TOKEN = re.compile(_DATE)

#: 用户原话里"给了时间"的迹象：日期写法，或相对说法。
_DATE_HINT = re.compile(
    rf"{_DATE}|今年|去年|前年|本月|上月|上个月|下个月|本周|上周|最近|"
    r"近[一二三四五六七八九十\d]+\s*个?月|近半年|季度|年报|年度"
)

#: 用户要求重来。只有在**上一轮分析确实失败**时才允许沿用旧范围。
_RETRY = re.compile(r"重新|重来|重做|再试一次|再来一次|再分析一次|再来一遍")


def last_analysis_status(database, conversation_id: str | None) -> str | None:
    """本会话最近一次账单分析的结果（``SUCCEEDED`` / ``FAILED`` / ``None``）。

    读的是运行事件表里的结构化状态，不是助手回复的措辞 —— 后者是总调度改写的，
    措辞一变判定就失效。
    """

    if database is None or not conversation_id:
        return None
    with database.sessions() as db:
        return db.scalar(
            select(AgentRunEvent.status)
            .where(
                AgentRunEvent.conversation_id == conversation_id,
                AgentRunEvent.event_type == "TOOL_RESULT",
                AgentRunEvent.tool_name.in_(
                    ("bill_analyze_comprehensive", "analyze_bill_comprehensive")
                ),
            )
            .order_by(AgentRunEvent.created_at.desc())
            .limit(1)
        )


def sanitize_goal(goal: str, user_text: str, last_status: str | None) -> str:
    """把 instruction 里"不是本轮用户给的"范围清掉。

    - 任务号：用户原话里没有 → 删；
    - 日期/期间：用户原话里没有任何时间说法 → 删；
    - 例外：用户说「重新/重来…」且上一轮分析失败 → 原样保留，允许直接重试。
    """

    user_text = user_text or ""
    if _RETRY.search(user_text) and last_status == "FAILED":
        return goal

    text = _TASK_ID.sub(lambda match: match.group(0) if match.group(0) in user_text else "", goal)
    if not _DATE_HINT.search(user_text):
        text = _DATE_RANGE.sub(" ", text)
        text = _DATE_TOKEN.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class BillState(AgentState, total=False):
    """账单域在图里多用的两个通道。

    `bill_call` 是「冻结了哪一次调用」（工具名 + 参数），`bill_action` 是冻结出来的快照
    （pending_id / operation / period / recipient / idempotency_key / expires_at）。

    finish 必须显式清掉它们：`idle_work_state()` 只清通用通道，不清模块自己的槽 —— 留着的话
    下一轮会把上一轮的快照当成待确认的操作。
    """

    bill_call: dict[str, Any] | None
    bill_action: dict[str, Any] | None


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


def declared_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """丢掉模型爱传的「不指定」占位符。

    参数声明是可空的，模型于是常把「没填」写成字面量 "null" / "none"。那不是值，当没写处理 ——
    否则 recipient 会变成一个叫 null 的收件人。
    """

    return {
        key: value
        for key, value in arguments.items()
        if value is not None
        and not (isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS)
    }


def authorization_question(action: dict[str, Any]) -> dict[str, Any]:
    """把冻结出来的快照变成前端能渲染的确认卡。

    `confirmation` 走通用卡片协议（app/agents/turn.py），所以字段形状必须与订阅一致。
    发信本身没有金额，所以 amount_minor 传 None —— 卡片只显示期间与收件人。
    以前这个字段要求正整数分，这里只好塞 1 分，卡面于是多出一行无关的「¥0.01」。pending_id 用快照自己的 action_id（`bill-report-…`），
    故意不是 `CA-…` 形状，这样 hitl 不会去走银行验证码授权。
    """

    period_start = str(action.get("period_start") or "")
    period_end = str(action.get("period_end") or "")
    recipient = str(action.get("recipient") or "")
    display = "\n".join(
        [
            f"请确认：{_OPERATION_LABEL}",
            f"期间：{period_start} 至 {period_end}",
            f"收件人：{recipient}",
        ]
    )
    return {
        "kind": "confirmation",
        "question_id": f"q_{uuid4().hex}",
        "agent_id": "bill",
        "attempt": 1,
        "display": display,
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
            pending_id=str(action.get("pending_id") or ""),
            operation=str(action.get("operation") or _OPERATION),
            title="确认发送账单报告",
            amount_minor=None,
            product_name="账单深度分析报告",
            account_display=recipient or "-",
            expires_at=action.get("expires_at"),
        ).as_payload(),
    }


def build_bill_graph(model, spec, *, assembler=None, turn=None, client=None, database=None):
    """编译账单域的图。

    ``assembler`` / ``client`` 收下不用；``turn`` 用来取本会话号，``database`` 用来查
    上一条分析结果 —— 两者只在「范围清洗」里用（见 ``sanitize_goal``）。
    """

    def invoke(name, arguments, idempotency_key=None):
        writer = get_stream_writer()
        outcome = DomainOutcome(tool_name=name, arguments=arguments)
        key = idempotency_key or f"bill:{name}:{uuid4().hex}"
        result = call_tool(spec, outcome, writer, idempotency_key=key)
        if may_retry(spec, result):
            # 同 key 重试：键在这里的全部作用是让**下游**（银行的幂等层、审计）认出这是同一个
            # 意图 —— 我们自己没有任何一层按它去重，`email_handlers` 的执行 handler 从不读
            # `invocation.call.idempotency_key`，所以这一行就是又调了一次 handler。
            # 挡住「同一张卡点两次」的是 HTTP 层：点击不再指向当前待确认操作时
            # `app/api/routes.py:538-551` 回 409，加上会话锁。
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
        turn_status = _BILL_TURN_STATUS[status]
        event = {
            "event": "subtask_end",
            "agent_id": "bill",
            "status": status.value,
            "summary": summary,
            "turn_status": turn_status,
        }
        result = {
            "agent_id": "bill",
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
            "bill_call": None,
            "bill_action": None,
            **idle_work_state(),
        }

    def from_result(result):
        if not result.succeeded:
            return finish(f"账单操作未成功：{result.error}", result.status)
        return finish(result.data["summary"], ids=result.data.get("business_ids"))

    def freeze(name, args):
        """WRITE 的冻结步：调 prepare 拿快照，一次都不发信。"""

        key = f"bill:{uuid4().hex}"
        result = invoke(name, args, idempotency_key=key)
        if not result.succeeded:
            return _DONE, from_result(result)
        facts = dict(result.data["facts"])
        action = {
            "pending_id": str(facts.get("action_id") or ""),
            "operation": _OPERATION,
            "period_start": str(facts.get("period_start") or ""),
            "period_end": str(facts.get("period_end") or ""),
            "recipient": str(facts.get("recipient") or ""),
            # 按文件发信时冻结下来：执行阶段据此只分析这份账单，与屏幕上的报告同一口径。
            "task_id": facts.get("task_id"),
            "idempotency_key": key,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=PENDING_EXPIRY_MINUTES)
            ).isoformat(),
        }
        return _PARKED, {
            "bill_call": {"tool": name, "arguments": args},
            "bill_action": action,
            "pending_question": authorization_question(action),
            "pending_answer": None,
            "turn_phase": "waiting_for_user",
        }

    def run_call(call):
        """只认声明过的工具；读工具直接跑，写工具只冻结。"""

        name, args = call["tool"], declared_arguments(call["arguments"])
        declared = spec.handle.spec(name)
        if declared is None or name in _HIDDEN:
            return _DONE, finish("该账单操作不被允许。", ToolStatus.DENIED)
        if not Draft202012Validator(declared.parameters).is_valid(args):
            return _DONE, finish("账单操作参数不完整或格式不正确，请重新说明。", ToolStatus.FAILED)
        if declared.side_effect == SideEffect.READ:
            result = invoke(name, args)
            if not result.succeeded:
                return _DONE, from_result(result)
            return _READ_ROUND, result
        return freeze(name, args)

    tools = [item for item in spec.handle.tools if item.name not in _HIDDEN]
    schemas = tuple(function_schema(item) for item in tools) + (function_schema(ask_user_spec()),)
    known = {item.name for item in tools} | {ASK_USER_TOOL_NAME}

    def seed_thread(state, answers):
        # 总调度会把历史里的任务号/日期写进 instruction；按本轮用户原话清洗一次，
        # 免得「我要分析流水」被当成"已经给了范围"而重跑上一份报告。
        goal = state.get("goal") or "按用户请求查询或分析账单"
        goal = sanitize_goal(
            str(goal),
            current_request(state),
            last_analysis_status(
                database, getattr(turn, "conversation_id", None)
            ),
        )
        return [
            {
                "role": "user",
                "content": (
                    f"指令：{goal}\n"
                    f"已补充信息：{json.dumps(answers or {}, ensure_ascii=False)}"
                ),
            }
        ]

    def assistant_turn(text, calls):
        if text.strip():
            return {"role": "assistant", "content": text}
        return {
            "role": "assistant",
            "content": json.dumps(
                {"tool": calls[0].name, "arguments": calls[0].arguments}, ensure_ascii=False
            ),
        }

    def activate(state, thread, rounds, answers, pending=None):
        """整段模型循环。它从不 interrupt：要问用户就把问题交回去，由 wait 节点去问。"""

        summaries = []
        while True:
            if pending is None:
                messages = ({"role": "system", "content": _BILL_PROMPT}, *thread)
                try:
                    text, calls = collect_stream(model, messages, schemas)
                except StreamTruncated:
                    return finish("模型输出中断，本次没有办理任何操作。", ToolStatus.FAILED)
                try:
                    decision = parse_domain_decision(text, calls, known)
                except DecisionError as error:
                    logger.warning(
                        "bill decide rejected: %s | text=%r calls=%r",
                        error,
                        text[:200],
                        [(call.name, dict(call.arguments)) for call in calls],
                    )
                    if len(calls) == 1 and calls[0].name not in known:
                        return finish("该账单操作不被允许。", ToolStatus.DENIED)
                    return finish("模型没有给出可用的账单信息，请重新说明。", ToolStatus.FAILED)
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
                        outcome.summary or "请说明要查询哪段期间的账单；当前尚未办理任何操作。",
                        thread=thread,
                    )
                if rounds >= MAX_TOOL_ROUNDS:
                    return finish(overflow_outcome(spec, summaries).summary)
                thread = [*thread, assistant_turn(text, calls)]
                name, args = outcome.tool_name, outcome.arguments
            else:
                name, args = pending
                pending = None
            kind, payload = run_call({"tool": name, "arguments": args})
            if kind == _PARKED:
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
        get_stream_writer()({"event": "agent_start", "agent_id": "bill"})
        thread = list(state.get("domain_thread") or [])
        if thread:
            # 续问：接着已有的对话走，不要从头开始。
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
        decision = answers.get("decision")
        if decision in ("confirm", "确认"):
            return "confirm"
        if decision in ("cancel", "取消"):
            return "cancel"
        return None

    def consume_confirmation(state, answers):
        """用户点了确认卡：六步 fail-closed，然后拿快照里的参数执行。"""

        action = state.get("bill_action")
        if not isinstance(action, dict):
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        pending_id = action.get("pending_id")
        if not pending_id or not action.get("operation"):
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        if expired(action):
            return finish("这笔操作的确认信息已过期，请重新说明。", ToolStatus.DENIED)
        answer_id = answers.get("pending_id")
        if answer_id and answer_id != pending_id:
            return finish("这笔操作的确认信息已失效，请重新说明。", ToolStatus.DENIED)
        decision = confirmation_answer(answers)
        if decision == "cancel":
            return finish("本次操作已取消，未发送报告。")
        if decision != "confirm":
            return finish("请明确确认或取消。", ToolStatus.DENIED)
        # 用快照里的期间与收件人，以及同一个幂等键：冻结之后换绑邮箱也改不了这一封。
        result = invoke(
            "bill_send_report_execute",
            {
                "period_start": action.get("period_start"),
                "period_end": action.get("period_end"),
                "recipient": action.get("recipient"),
                # 快照里有任务号就按那份账单重算；没有才回落到日期区间。
                "task_id": action.get("task_id"),
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
        collected = {**(state.get("collected_inputs") or {}), **answers}
        return decide(state, answers=collected)

    def route(state):
        return "wait" if state.get("turn_phase") == "waiting_for_user" else END

    graph = StateGraph(BillState)
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
