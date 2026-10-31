"""离线理财测试专用：HTTP 传输替身、脚本模型、可控时钟与银行响应样本。

正式实现 `HttpWealthBank` 只接受一个 `BankClient`，所以这里替换的是**传输层**
（`httpx.MockTransport`），不是适配器本身：请求的 method／path／query／body／headers
仍然由真实的 `BankClient` 组装，断言才有意义。

本模块只存在于 `tests/wealth/`，正式代码不导入它。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.domain import build_domain_specs
from app.agents.runtime import AgentRuntime, TurnContext
from app.agents.turn import CONFIRMATION_KIND, TurnStatus
from app.integration.bank_client import BankClient
from app.integration.model_client import ModelReply, ToolCall
from app.modules.registry import build_registry
from app.modules.wealth.contracts.bank import WriteAuthorization
from app.modules.wealth.contracts.requests import WealthRequest
from app.modules.wealth.contracts.responses import WealthResponse
from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.modules.wealth.workflow.graph import build_wealth_graph
from app.modules.wealth.workflow.state import PendingData, SessionData, WealthState
from app.runtime import build_tool_gateway
from app.tools.protocol import TrustedContext
from tests.support.scripted_model import chunks

# 银行服务把整个路由挂在 /api 下，BankClient 的 base_url 已经带上这个前缀。
BANK_BASE_URL = "http://bank.test/api"

# M1 演示数据里的客户（bank_backend/app/storage/wealth_seed.py 的 WEALTH_USER）。
DEMO_CUSTOMER = "u_wealth"
DEMO_OTHER_CUSTOMER = "u_wealth_other"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """一次由真实 `BankClient` 发出的请求。"""

    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any] | None
    headers: dict[str, str]

    @property
    def idempotency_key(self) -> str | None:
        return self.headers.get("idempotency-key")


Responder = Callable[[RecordedCall], httpx.Response]


class BankDouble:
    """bank_backend 的传输替身：按 method + path 预设响应，并记录每次请求。

    没有预设过的调用会直接抛 `AssertionError`，而不是回一个通用错误：测试里出现
    计划外的请求时，要看到的是“谁多发了请求”，不是被误判成一次业务失败。
    """

    def __init__(self, base_url: str = BANK_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._prefix = urlsplit(self._base_url).path.rstrip("/")
        self.calls: list[RecordedCall] = []
        self._answers: dict[tuple[str, str], Responder] = {}

    # ------------------------------------------------------------------ 预设响应

    def on(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        code: int = 0,
        status: int = 200,
        message: str | None = None,
    ) -> BankDouble:
        """按银行 {code, message, data} 信封回一个成功或业务失败的响应。

        失败响应的默认 message 就是银行真实的类名默认值（`resource_not_found`），
        这样“银行的英文文案不会漏给用户”才是一条有效断言。
        """

        text = (
            message if message is not None else ("success" if code == 0 else "resource_not_found")
        )
        payload = {"code": code, "message": text, "data": data}
        return self.on_response(method, path, httpx.Response(status, json=payload))

    def on_response(self, method: str, path: str, response: httpx.Response) -> BankDouble:
        self._answers[(method.upper(), path)] = lambda call: response
        return self

    def on_sequence(
        self,
        method: str,
        path: str,
        *datas: Any,
        code: int = 0,
        status: int = 200,
    ) -> BankDouble:
        """同一个端点按调用顺序返回不同的 `data`；最后一个会被之后每次调用重复使用。

        每个 `datas` 元素就是那一次响应的完整 `data`（列表接口要传列表），用来断言跨调用的真实
        事实，例如"预览时余额 20000 元、写完之后余额 18000 元"。
        """

        queue = list(datas)

        def responder(call: RecordedCall) -> httpx.Response:
            del call
            current = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else None)
            text = "success" if code == 0 else "resource_not_found"
            return httpx.Response(status, json={"code": code, "message": text, "data": current})

        self._answers[(method.upper(), path)] = responder
        return self

    def on_failure(self, method: str, path: str, error: Exception) -> BankDouble:
        """让这次调用在传输层失败：连接被拒、超时、TLS 错误等。"""

        def fail(call: RecordedCall) -> httpx.Response:
            del call
            raise error

        self._answers[(method.upper(), path)] = fail
        return self

    def on_sequence_any(
        self, method: str, path: str, *items: Any, code: int = 0, status: int = 200
    ) -> BankDouble:
        """同一个端点按调用顺序返回：`Exception` 在传输层抛出，其余作为那次响应的 `data`。

        最后一个元素会被之后每次调用重复使用。`on_sequence` 只能表达成功响应、`on_failure` 只能
        表达一直失败，而"第一次瞬时失败、第二次成功"这类序列要用这个（例如见证只读重试）。
        """

        queue = list(items)

        def responder(call: RecordedCall) -> httpx.Response:
            del call
            current = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else None)
            if isinstance(current, Exception):
                raise current
            text = "success" if code == 0 else "resource_not_found"
            return httpx.Response(status, json={"code": code, "message": text, "data": current})

        self._answers[(method.upper(), path)] = responder
        return self

    # ------------------------------------------------------------------ 使用

    def client(self) -> BankClient:
        return BankClient(self._base_url, transport=httpx.MockTransport(self._handle))

    def only_call(self) -> RecordedCall:
        assert len(self.calls) == 1, f"expected exactly one bank call, got {self.calls}"
        return self.calls[0]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        call = self._record(request)
        self.calls.append(call)
        responder = self._answers.get((call.method, call.path))
        if responder is None:
            raise AssertionError(f"unexpected bank call: {call.method} {call.path}")
        return responder(call)

    def _record(self, request: httpx.Request) -> RecordedCall:
        # 记录原始路径而不是 httpx 规范化后的解码路径：断言“真正发到线上的路径”才有意义，
        # 例如带 %2F 的 ID 不能被规范化成另一个资源路径。
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        if self._prefix and path.startswith(self._prefix):
            path = path[len(self._prefix) :] or "/"
        content = request.content
        return RecordedCall(
            method=request.method,
            path=path,
            query=dict(request.url.params),
            body=json.loads(content) if content else None,
            headers=dict(request.headers),
        )


# ---------------------------------------------------------------------- 银行响应样本
# 字段名与 bank_backend 的 view 函数一一对应（investment_service.product_view /
# holding_view / order_view、account_service.account_view、wealth_risk_service.
# assessment_view、wealth_order_service.wealth_order_view），样本漂移会在映射测试里暴露。


def product_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": "p_demo_cash",
        "name": "演示灵活理财",
        "product_type": "money_fund",
        "min_amount_minor": 10000,
        "max_amount_minor": 100000000,
        "expected_return": 0.02,
        "risk_level": "low",
        "duration_months": 0,
        "redeem_rule": "flexible",
        "status": "available",
    }
    payload.update(overrides)
    return payload


def account_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account_id": "a_wealth_main",
        "user_id": DEMO_CUSTOMER,
        "account_type": "current",
        "balance_minor": 2000000,
        "available_minor": 2000000,
        "currency": "CNY",
        "interest_rate": 0.001,
        "status": "active",
    }
    payload.update(overrides)
    return payload


def holding_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "holding_id": "h_wealth_sample",
        "user_id": DEMO_CUSTOMER,
        "product_id": "p_demo_cash",
        "product_name": "演示灵活理财",
        "source_account_id": "a_wealth_main",
        "principal_minor": 100000,
        "current_value_minor": 100500,
        "accrued_interest_minor": 500,
        "realized_income_minor": 0,
        "redeemable_minor": 100500,
        "expected_return": 0.02,
        "status": "holding",
        "purchased_at": "2026-09-06T10:00:00+08:00",
        "maturity_date": None,
    }
    payload.update(overrides)
    return payload


def order_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_id": "order_wealth_sample",
        "order_type": "product_purchase",
        "user_id": DEMO_CUSTOMER,
        "product_id": "p_demo_cash",
        "product_name": "演示灵活理财",
        "holding_id": "h_wealth_sample",
        "from_account_id": "a_wealth_main",
        "to_account_id": None,
        "amount_minor": 100000,
        "status": "completed",
        "transaction_id": "txn_wealth_sample_buy",
        "details": {"redeem_rule": "flexible", "sample": True},
        "created_at": "2026-09-06T10:00:00+08:00",
        "updated_at": "2026-09-06T10:00:00+08:00",
    }
    payload.update(overrides)
    return payload


def risk_profile_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "assessment_id": "wealth_assessment_1",
        "user_id": DEMO_CUSTOMER,
        "answers": {"q1": 0, "q2": 1, "q3": 1, "q4": 0, "q5": 0},
        "score": 2,
        "risk_level": "low",
        "rule_version": "wealth-risk-v1",
        "assessed_at": "2026-10-06T10:00:00+08:00",
        "expires_at": "2026-11-05T10:00:00+08:00",
        "valid": True,
    }
    payload.update(overrides)
    return payload


def purchase_receipt_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_id": "order_purchase_1",
        "holding_id": "h_purchase_1",
        "transaction_id": "txn_purchase_1",
        "product_id": "p_demo_cash",
        "amount_minor": 200000,
        "status": "completed",
        "principal_minor": 200000,
        "maturity_date": None,
        "redeem_rule": "flexible",
        "risk_level": "low",
        "assessment_id": "wealth_assessment_1",
        "replayed": False,
    }
    payload.update(overrides)
    return payload


def redemption_receipt_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_id": "order_redeem_1",
        "holding_id": "h_purchase_1",
        "transaction_id": "txn_redeem_1",
        "amount_minor": 50000,
        "principal_redeemed_minor": 49751,
        "interest_minor": 249,
        "holding_status": "holding",
        "remaining_principal_minor": 150249,
        "remaining_value_minor": 150749,
        "status": "completed",
        "replayed": False,
    }
    payload.update(overrides)
    return payload


def idempotency_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "found": True,
        "user_id": DEMO_CUSTOMER,
        "operation_type": "purchase_product",
        "idempotent_key": "idem-1",
        "result": {
            "order_id": "order_purchase_1",
            "holding_id": "h_purchase_1",
            "transaction_id": "txn_purchase_1",
            "status": "completed",
        },
        "order": order_payload(order_id="order_purchase_1", transaction_id="txn_purchase_1"),
        "created_at": "2026-10-06T10:00:00+08:00",
    }
    payload.update(overrides)
    return payload


def lookup_miss(
    operation_type: str = "purchase_product", key: str = "idem-unknown"
) -> dict[str, Any]:
    """按幂等键核实"这个键上没有记录"：不编造订单，也不说"没有扣款"（任务11）。

    银行只说明"查不到"，判断留给调用方：一笔结果未知的写查不到仍然是未知。
    """

    return idempotency_payload(
        found=False,
        operation_type=operation_type,
        idempotent_key=key,
        result=None,
        order=None,
        created_at=None,
    )


def lookup_hit(
    operation_type: str,
    key: str,
    receipt: Mapping[str, Any],
    *,
    order: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按幂等键核实"查到了"：`result` 就是银行当时保存的那份原回执。

    回执的字段与银行 `investment_service` 保存进幂等记录的 payload 一致，所以它既能被适配器
    还原成回执对象，也能和用户确认过的快照比对（任务11 的核实路径）。
    """

    return idempotency_payload(
        operation_type=operation_type,
        idempotent_key=key,
        result=dict(receipt),
        order=None if order is None else dict(order),
    )


# ---------------------------------------------------------------------- 可信上下文


def bound_context(
    bank_customer_id: str = DEMO_CUSTOMER,
    *,
    user_id: str = "user_0001",
    session_id: str = "sess_wealth_1",
) -> TrustedContext:
    return TrustedContext(user_id=user_id, session_id=session_id, bank_customer_id=bank_customer_id)


class ClickConfirmationBank(HttpWealthBank):
    """一个**声明"点击即授权"**的银行适配器替身。

    它继承真实的 `HttpWealthBank`，所以请求的 method／path／query／body／headers 仍然由真实的
    `BankClient` 组装，`BankDouble` 的预设与断言照常有效。它只改一件事：能力声明。

    `HttpWealthBank._authorization` 按这个声明决定要不要带 `mfa_code`，所以这里没有第二套写实现：
    改写能力声明，`purchase`／`redeem` 两条路径的行为就一起变成"不带验证码"。

    **它不证明当前银行兼容。** 本仓库的 `HttpWealthBank` 回答 `CLICK_AND_CODE`（银行的写门禁
    要求验证码），所以这个替身只用来验证"接入方提供无验证码通道之后"那条路径上的运行时逻辑：
    提交一次、幂等、回执比对、失败分类与核实。当前银行那条（要验证码）必须用真正的
    `HttpWealthBank` 断言，两者不能互相替代。
    """

    def write_authorization(self, operation: str, amount_minor: int) -> WriteAuthorization:
        return WriteAuthorization.CLICK


def unbound_context(
    *, user_id: str = "user_0001", session_id: str = "sess_wealth_1"
) -> TrustedContext:
    """没有银行客户绑定的上下文：任何一次银行调用都必须在此之前被拒绝。"""

    return TrustedContext(user_id=user_id, session_id=session_id)


# ---------------------------------------------------------------------- 脚本模型与时钟


class ScriptModel:
    """按脚本逐条回复的模型替身（离线测试里的"模型"）。

    它做两件事：把每次的提示词记下来（测试要断言"工具事实进了下一轮提示词""验证码没进提示词"），
    按顺序返回脚本里的下一条。脚本用完还被调用就是测试自己写错了——直接断言失败，而不是回一个
    通用答案，让"模型只被调用了 N 次"这类断言变得没有意义。
    """

    def __init__(self, *replies: str | ToolCall | Exception) -> None:
        self._replies: list[str | ToolCall | Exception] = list(replies)
        self.prompts: list[str] = []
        self.tools: list[tuple[dict[str, Any], ...]] = []
        self.calls = 0
        self.streams = 0

    def _record(self, messages: Sequence[Mapping[str, Any]]) -> None:
        self.prompts.append("\n".join(str(message.get("content", "")) for message in messages))
        self.calls += 1

    def _next(self) -> str | ToolCall:
        assert self._replies, f"模型被调用了第 {self.calls} 次，但脚本已经用完"
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def complete(self, messages: Sequence[Mapping[str, Any]]) -> ModelReply:
        self._record(messages)
        return ModelReply(text=self._next())

    def stream(self, messages, *, tools=()):
        """决策轮的流式答复：脚本里的下一条，按原生工具调用的分片发出去。

        工具调用走 provider 的 tool-call 分片，纯文本走 text 分片——两条路都必须在测试里
        走一遍，才能证明图真的走的是流式协议，而不是"把 JSON 拼进 prompt 再解析出来"。
        """

        self._record(messages)
        self.tools.append(tuple(tools))
        self.streams += 1
        reply = self._next()
        if isinstance(reply, ToolCall):
            # 分片里的 arguments 是 provider 逐段发来的 JSON 文本，不是 Python 对象。
            yield from chunks(
                [("call", reply.name, json.dumps(reply.arguments, ensure_ascii=False))]
            )
        else:
            yield from chunks([reply])

    def close(self) -> None:
        return None


class TestClock:
    """可控时钟：待办的有效期由它推进，测试不用等真实时间。"""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SessionCarrier:
    """替端到端用例拿着会话与待办：运行时不再替调用方记状态（任务 3）。

    单元测试直接写 `session=...`／`pending=...`；端到端用例的调用点太多，所以在这里按
    `(agent, 身份, 会话)` 记一份，调用点仍然只写"发一次请求"。表里同时存着 agent 本身，
    避免对象被回收之后编号被下一次装配复用；同一身份换一份空会话仍然从零开始。
    """

    def __init__(self) -> None:
        self._states: dict[tuple[int, str, str], tuple[Any, SessionData, PendingData | None]] = {}

    def _key(self, agent: Any, context: TrustedContext) -> tuple[int, str, str]:
        return (id(agent), context.user_id, context.session_id)

    def _state(self, agent: Any, context: TrustedContext) -> tuple[SessionData, PendingData | None]:
        stored = self._states.get(self._key(agent, context))
        return (SessionData(), None) if stored is None else (stored[1], stored[2])

    def handle(self, agent: Any, context: TrustedContext, request: WealthRequest) -> WealthResponse:
        """一次 `handle`：把这份会话与待办递进去，再把返回的那份存回来。"""

        session, pending = self._state(agent, context)
        response, session, pending = agent.handle(
            context, request, session=session, pending=pending
        )
        self._states[self._key(agent, context)] = (agent, session, pending)
        return response

    def session(self, agent: Any, context: TrustedContext) -> SessionData:
        return self._state(agent, context)[0]

    def pending(self, agent: Any, context: TrustedContext) -> PendingData | None:
        return self._state(agent, context)[1]

    def pending_id(self, agent: Any, context: TrustedContext) -> str | None:
        pending = self.pending(agent, context)
        return None if pending is None else str(pending["pending_id"])

    def write(self, agent: Any, context: TrustedContext) -> SimpleNamespace | None:
        """待办里的写意图，按属性读（与单元测试的 `_write_record` 同一形状）。"""

        write = (self.pending(agent, context) or {}).get("write")
        return SimpleNamespace(**write) if write else None


def text_reply(text: str) -> tuple[str, list[ToolCall]]:
    """模型的纯文本答复：没有工具调用，决策层必须把它当成一次 finish。"""

    return text, []


def decision_input(text_or_call: str | ToolCall) -> tuple[str, list[ToolCall]]:
    """把一个工具调用包成 parse_decision 的两个入参：流式文本为空，调用列表一条。"""

    if isinstance(text_or_call, ToolCall):
        return "", [text_or_call]
    return text_or_call, []


def call_read(tool: str, arguments: Mapping[str, Any] | None = None) -> ToolCall:
    """模型的一条只读工具请求。"""

    return call(tool, dict(arguments or {}))


def call(tool: str, arguments: Mapping[str, Any] | None = None) -> ToolCall:
    """模型的一次工具调用：工具名 + 它声明的参数（原生 tool calling 的一步）。"""

    return ToolCall(name=tool, arguments=dict(arguments or {}))


def set_slots(slots: Mapping[str, Any]) -> ToolCall:
    """模型从用户这句话里读到的一组业务槽位（金额、产品、账户、持仓、用钱时间、流动性）。

    槽位值可以是具体值，也可以是最近展示顺序里的序号：`{"product_id": {"ordinal": 2}}`。
    """

    return call("set_slots", {"slots": dict(slots)})


def ask_user(question: str, missing: Sequence[str] = ()) -> ToolCall:
    """模型的一次追问；`question` 是自由文本，`missing` 是必填的"这次在补哪几个槽位"。

    问题不受槽位词表限制（问卷的某一题、含糊的金额、任何澄清都可以问），但 `missing` 必须在
    场：程序用它驱动候选展示，并机械地拒绝重复追问已知信息。不在补槽位（例如问问卷的某一题）
    就显式写空数组——这正是"必填"的意义，省略会让那两项检查都无从进行。
    """

    return call("ask_user", {"question": question, "missing": list(missing)})


def finish(
    answer: str,
    cited_ids: Sequence[str] = (),
    shown: Mapping[str, Sequence[str]] | None = None,
) -> ToolCall:
    """模型的一次回答；`cited_ids` 是它声称所依据的业务编号。

    `shown` 是它这次按展示顺序列给用户的候选（键是资源名，值是编号列表）——运行时按这份顺序
    解析"第一个／第二个"，所以它必须由模型自己声明，而不是猜数据库顺序。
    """

    arguments: dict[str, Any] = {"text": answer}
    if cited_ids:
        arguments["cited_ids"] = list(cited_ids)
    if shown:
        arguments["shown"] = {str(key): list(value) for key, value in shown.items()}
    return call("finish", arguments)


def submit_risk(answers: Mapping[str, Any]) -> ToolCall:
    """模型提交的问卷答案：题目编号 → 0／1／2 选项。

    等级不在这里：模型只能给选项，等级、分数和有效期由银行按 wealth-risk-v1 计算。
    """

    return call("submit_risk", {"answers": dict(answers)})


def recommend() -> ToolCall:
    """模型请求按会话槽位和银行测评推荐产品（筛选条件不由模型给）。"""

    return call("recommend")


def compare(*product_ids: str) -> ToolCall:
    """模型请求对比两到三个已经查到的产品。"""

    return call("compare", {"product_ids": list(product_ids)})


def prepare_purchase(slots: Mapping[str, Any]) -> ToolCall:
    """模型请求发起一次申购预览：只提取产品、金额、账户三个业务槽位。

    身份、确认、验证码和幂等键都不在这里——它们由运行时的专用方法传入，模型给的多余字段
    会被决策层直接拒绝（`prepare_purchase` 只接受 `slots`）。
    """

    return call("prepare_purchase", {"slots": dict(slots)})


def prepare_redeem(slots: Mapping[str, Any]) -> ToolCall:
    """模型请求发起一次赎回预览：只提取持仓、金额、到账账户三个业务槽位。

    金额可以是具体整数分，也可以是 `{"all": true}`（全部）或 `{"half": true}`（一半）；
    持仓可以写具体编号，也可以写 `{"recent": true}`（最近成功申购的那笔新仓）。授权字段
    （身份、确认、验证码、幂等键）同样不在其中。
    """

    return call("prepare_redeem", {"slots": dict(slots)})


def wealth_demo_products() -> list[dict[str, Any]]:
    """M1 演示数据的四款产品，数值与 `bank_backend/app/storage/wealth_seed.py` 一致。

    灵活 low／0 月、稳健 medium／3 月、进取 high／12 月、停售 low（模拟年化参数 9%）。
    """

    return [
        product_payload(),
        product_payload(
            product_id="p_demo_stable",
            name="演示稳健理财",
            product_type="fixed_term",
            min_amount_minor=100000,
            expected_return=0.035,
            risk_level="medium",
            duration_months=3,
            redeem_rule="maturity_only",
        ),
        product_payload(
            product_id="p_demo_growth",
            name="演示进取理财",
            product_type="fixed_term",
            min_amount_minor=500000,
            expected_return=0.06,
            risk_level="high",
            duration_months=12,
            redeem_rule="maturity_only",
        ),
        product_payload(
            product_id="p_demo_closed",
            name="演示停售产品",
            expected_return=0.09,
            status="sold_out",
        ),
    ]


# ---------------------------------------------------------------------------
# 领域图（任务 4）：跨轮状态住在检查点上，不再住在处理器的字典里
# ---------------------------------------------------------------------------


def wealth_domain_spec(double: BankDouble, context: TrustedContext, *, registry: Any = None) -> Any:
    """理财在基座里的那份 DomainSpec：工具句柄已经接上这家银行替身。"""

    resolved = registry if registry is not None else build_registry()
    gateway = build_tool_gateway(resolved, client=double.client())
    return next(
        spec
        for spec in build_domain_specs(gateway, context, roster=resolved.roster)
        if spec.agent_id == "wealth"
    )


def wealth_domain_graph(
    model: Any,
    double: BankDouble,
    *,
    context: TrustedContext,
    assembler: Any = None,
    clock: Any = None,
    turn: Any = None,
    checkpointer: Any = None,
) -> Any:
    """一张可以直接跑的理财领域图。

    build_wealth_graph 交出来的已经是编译好的图（它以 graph.compile() 收尾），所以这里套一层
    最小的宿主图：thread 与检查点属于宿主，领域图自己不挑宿主是谁。
    """

    inner = build_wealth_graph(
        model,
        wealth_domain_spec(double, context),
        assembler=assembler,
        turn=turn,
        client=double.client(),
        clock=clock,
    )
    host = StateGraph(WealthState)
    host.add_node("wealth", inner)
    host.add_edge(START, "wealth")
    host.add_edge("wealth", END)
    return host.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


@dataclass(frozen=True, slots=True)
class Lap:
    """一轮领域图的结果：检查点里的状态加上这一轮流出来的事件。

    旧用例读的是处理器的返回值；现在整轮的结果落在两处：事件（说了什么、什么状态）和状态
    （待办、槽位、记忆、还挂着的问题）。这个类把两者收在一起，让断言读起来还是原来的样子。
    """

    state: Mapping[str, Any]
    events: tuple[Any, ...] = ()

    @property
    def result(self) -> Mapping[str, Any]:
        """这一轮交给基座的那份 domain_result。"""

        results = self.state.get("domain_results") or []
        first = results[0] if results else None
        return first if isinstance(first, Mapping) else {}

    @property
    def status(self) -> TurnStatus | None:
        """这一轮的 TurnStatus：事件里有就照它，没有就按还挂着的问题推。"""

        for event in reversed(self.events):
            if event.event == "subtask_end":
                raw = event.data.get("turn_status")
                if isinstance(raw, str):
                    return TurnStatus(raw)
        question = self.question
        if question is None:
            return TurnStatus.COMPLETED if self.result else None
        return (
            TurnStatus.WAITING_CONFIRMATION
            if question.get("kind") == CONFIRMATION_KIND
            else TurnStatus.WAITING_INPUT
        )

    @property
    def summary(self) -> str:
        """用户看到的那句话：事件的摘要，或者还挂着的问题本身。"""

        for event in reversed(self.events):
            if event.event == "subtask_end":
                return str(event.data.get("summary") or "")
        question = self.question
        return "" if question is None else str(question.get("display") or "")

    @property
    def facts(self) -> Mapping[str, Any]:
        facts = self.result.get("facts")
        return facts if isinstance(facts, Mapping) else {}

    @property
    def business_ids(self) -> Mapping[str, Any]:
        ids = self.result.get("business_ids")
        return ids if isinstance(ids, Mapping) else {}

    @property
    def question(self) -> Mapping[str, Any] | None:
        """正等着的那个问题（追问或确认框），没有就是 None。

        暂停那一轮里节点的写入还没提交（中断会让节点在恢复时重跑），所以问题先从流出来的
        中断里取；恢复过一轮之后它就在检查点的 `pending_question` 上。
        """

        question = self.state.get("pending_question")
        if isinstance(question, Mapping):
            return question
        for payload in reversed(self.interrupts):
            if isinstance(payload, Mapping):
                return payload
        return None

    @property
    def confirmation(self) -> SimpleNamespace | None:
        """确认框的展示投影，按属性读（与旧 TurnConfirmation 的属性名一致）。"""

        question = self.question or {}
        payload = question.get("confirmation")
        return SimpleNamespace(**payload) if isinstance(payload, Mapping) else None

    @property
    def pending(self) -> PendingData | None:
        """这一轮之后还留着的业务待办（旧的 _pending 表）。"""

        pending = self.state.get("wealth_pending")
        return pending if isinstance(pending, Mapping) else None

    @property
    def slots(self) -> Mapping[str, Any]:
        slots = self.state.get("wealth_slots")
        return slots if isinstance(slots, Mapping) else {}

    @property
    def memory(self) -> list[Mapping[str, Any]]:
        """这个模块写回外层的记忆记录（旧的 handoff.updates()）。"""

        stored = self.state.get("domain_context")
        own = stored.get("wealth") if isinstance(stored, Mapping) else None
        records = own.get("records") if isinstance(own, Mapping) else None
        return [record for record in records or [] if isinstance(record, Mapping)]

    @property
    def interrupts(self) -> list[Any]:
        return [event.data for event in self.events if event.event == "interrupt"]


async def domain_state(graph: Any, snapshot: Any) -> Mapping[str, Any]:
    """这一轮的领域状态。

    领域图在基座里是**子图**：中断那一轮它还没跑完，父图的检查点里没有它的字段，真正的那份
    状态在子图自己的命名空间里。父任务记着那个命名空间配置，照着它再读一次就是。
    """

    values = snapshot.values or {}
    if any(str(key).startswith("wealth_") for key in values):
        return values
    for task in getattr(snapshot, "tasks", ()) or ():
        nested = getattr(task, "state", None)
        if not isinstance(nested, Mapping) or "configurable" not in nested:
            continue
        found = await domain_state(graph, await graph.aget_state(nested))
        if any(str(key).startswith("wealth_") for key in found):
            return found
    return values


class GraphTurn:
    """整轮处理器的图替身：跨轮状态放在检查点上，模型一轮一轮注入。

    旧 WealthTurn 把会话与待办放在自己的字典里；现在它们住在检查点上，所以重建一次处理器只是
    换一个 saver，而同一个会话就是同一个线程。lap 的 handoff 参数沿用旧用例的形状：装配器、
    会话编号、当前消息与那几轮记忆都从它来，调用点因此几乎不用改写。
    """

    def __init__(
        self,
        double: BankDouble,
        *,
        context: TrustedContext,
        clock: Any = None,
        checkpointer: Any = None,
        graph: Any = None,
    ) -> None:
        self._double = double
        self._context = context
        self._clock = clock
        # 用例自己装配好的图（A15 的独立入口就是这么用的）：给了就不再自己建。
        self._graph = graph
        self.saver = checkpointer if checkpointer is not None else InMemorySaver()

    def lap(
        self,
        model: Any,
        message: str,
        *,
        handoff: Any = None,
        context: TrustedContext | None = None,
        session: str | None = None,
        resume: Any = None,
        verification_code: str | None = None,
        assembler: Any = None,
        conversation_id: str | None = None,
        sequence: int | None = None,
        message_id: str | None = None,
    ) -> Lap:
        """跑一轮领域图：入口还是用户那句话，跨轮的东西由检查点带着。"""

        context = context if context is not None else self._context
        session_id = session or context.session_id
        records: list[Mapping[str, Any]] = []
        if handoff is not None:
            assembler = handoff.assembler if assembler is None else assembler
            if conversation_id is None:
                conversation_id = handoff.conversation_id
            if sequence is None:
                sequence = handoff.current_sequence
            if message_id is None:
                message_id = handoff.current_message_id
            records = [dict(record) for record in handoff.domain_records]
        sequence = 1 if sequence is None else int(sequence)
        message_id = message_id or "msg_%d" % sequence
        conversation = conversation_id
        thread_id = "chat:%s" % (conversation or session_id)
        # 没有会话就是旧用例的独立路径：运行时按 session 派生 thread，处理器看不到会话。
        turn = (
            None
            if conversation is None
            else TurnContext(
                conversation_id=conversation,
                thread_id=thread_id,
                message_id=message_id,
                content=message,
                sequence=sequence,
                resume=resume,
                verification_code=verification_code,
            )
        )
        graph = (
            self._graph
            if self._graph is not None
            else wealth_domain_graph(
                model,
                self._double,
                context=context,
                assembler=assembler,
                clock=self._clock,
                turn=turn,
                checkpointer=self.saver,
            )
        )
        config = {"configurable": {"thread_id": thread_id}}
        if records:
            asyncio.run(
                graph.aupdate_state(config, {"domain_context": {"wealth": {"records": records}}})
            )
        return asyncio.run(self._run(graph, context, session_id, message, turn, config, resume))

    def answer(
        self,
        model: Any,
        question: Mapping[str, Any],
        value: Mapping[str, Any],
        *,
        message: str,
        **lap: Any,
    ) -> Lap:
        """回答一个正等着的追问：信封要指名它回答的是哪一个问题。"""

        return self.lap(
            model,
            message,
            resume={"question_id": question.get("question_id"), "value": dict(value)},
            **lap,
        )

    def click(self, model: Any, held: Lap, decision: str, *, message: str, **lap: Any) -> Lap:
        """点一次确认框上的按钮：只交出决定，待办编号永远由服务端自己绑定。"""

        return self.answer(
            model, held.question or {}, {"decision": decision}, message=message, **lap
        )

    async def _run(
        self,
        graph: Any,
        context: TrustedContext,
        session_id: str,
        message: str,
        turn: TurnContext | None,
        config: Mapping[str, Any],
        resume: Any = None,
    ) -> Lap:
        runtime = AgentRuntime(graph=graph)
        if turn is None:
            # 会话路径：恢复信封走 client_context，和基座自己的 resume 用例同一条路。
            envelope = None if resume is None else {"resume": resume}
            events = [
                event
                async for event in runtime.invoke(session_id, context.user_id, message, envelope)
            ]
        else:
            events = [
                event
                async for event in runtime.invoke(
                    session_id, context.user_id, message, None, turn=turn
                )
            ]
        state = await domain_state(graph, await graph.aget_state(config))
        return Lap(state=state, events=tuple(events))
