"""WealthAgent：独立理财 Agent 的对外运行时。

一次 `handle` 做六件事：

1. **校验与分流输入**：`message` 走对话工作流；`answer` 只能回答本人本会话本意图未过期的待办；
   `confirm` 只提交本人本会话本意图、且已经展示过预览的**那一笔**资金写；`reconcile` 只按原
   幂等键**读**一次银行，用来核实一笔结果未知的交易；`cancel` 只终止未提交的工作，不声称撤销
   任何已提交或结果未知的订单。旧 `verify` 入口明确停用（点击确认取代了"确认 + 输入验证码"）。
2. **装配响应**：状态、回答、事实、业务号、待办、错误码、`trace_id`。事实与业务号只来自银行
   返回和 `domain` 规则算出的结论，这一层只做搬运；成交话术由程序模板生成（`domain.rules.
   purchase_success`），不是模型写的。
3. **状态与失败一一对应**：工作流以失败收尾时如实返回失败；只读工具全部失败、没有一条成功
   结果时，即使模型给了回答也返回 failed——不能把"银行不可用"说成一次成功的查询。
4. **会话**：按 `(user_id, session_id)` 隔离，保存业务槽位（金额、产品、账户、持仓、用钱时间、
   流动性）、最近一次展示过的候选顺序、意图版本和连续澄清计数（计划任务7）。改任何槽位即换
   意图：意图版本 +1，旧待办（含旧确认与旧验证请求）立即失效。会话是临时状态，重启即丢；
   余额、测评、持仓和订单仍在银行，重启后要重新查询和确认。
5. **待办**：`waiting_input`／`waiting_confirmation` 都必须带 `pending_id`，由这里铸。待办绑定
   用户、会话与意图版本，5 分钟后过期；跨用户、跨会话、跨意图版本或已过期的输入一律拒绝，也不
   透露待办是否存在。同会话同时只有一个活动待办。
6. **点击确认状态机**（任务4）：预览（`waiting_confirmation`）→ 可信 `confirm(pending_id)` →
   归属／有效期／意图／快照校验 → 写前刷新 → 受控提交 → completed／failed／unknown。新交互路径
   **不产生 `waiting_verification`**，也不再要求用户输入验证码。三处只有运行时能做的事：

   - **快照冻结**：确认绑定的是一份在预览时冻结的快照（申购的 `PurchaseSnapshot` 或赎回的
     `RedeemSnapshot`——产品／持仓、金额、账户、预览原文），加上这一刻铸好的**唯一幂等键**。
     改任何槽位即换意图，旧快照与旧键一起作废。
   - **写前刷新**：提交前重新读产品、账户、测评（赎回则读持仓与账户，**不要求重新测评**）并重跑
     预检；条件变了就失败并作废待办，绝不用已确认的金额去凑一次写。金额与账户在确认之后不再改变。
   - **结果分类**：结果未知（超时、5xx、读不懂的响应）既不宣布成功也不宣布失败，待办与幂等键都
     留着供核实；银行明确拒绝如实转达；只有拿到银行已提交回执、并且回执与意图一致时才说
     "模拟申购／赎回已完成"。赎回的释放本金与实现收益是银行算的分摊，运行时只搬运（需求 §6.2）。

   **能力前提**：确认怎么才算授权，取决于适配器的 `write_authorization()`。本仓库的
   `HttpWealthBank` 回答 `CLICK_AND_CODE`（银行的写门禁要求 `confirm=true` 且验证码等于配置的
   演示码），所以确认框上会出现验证码输入，用户当场输入的值随确认一起提交给银行——**不代填、
   不猜、也不从配置里取一个演示码冒充**。银行要验证码而这次点击没带时，运行时在**发出任何资金
   请求之前**返回"这笔需要验证码"，并能确定地说"没有提交"。回答 `UNSUPPORTED` 的适配器才会走
   `reason_code=confirmation_transport_unsupported` 那条集成阻塞分支。

   会话另外记下最近一次成功申购的新仓：用户说"赎回刚才买的"时绑定它，而不是按数据库顺序猜第一笔。

7. **结果未知的处理**（任务11）：一次资金写没有拿到明确结果（超时、5xx、读不懂的响应）时，绝不
   宣布成功也绝不宣布失败，而是**按原 `operation_type` 与原幂等键**问银行"这个键上有没有记录"
   （`find_order_by_key`）。三条出口：查到了且与确认过的意图一致就如实报告成交（只扣一次）；
   查到了但读不出或对不上、查不到、核实本身失败，都仍是 `unknown`／`TRANSACTION_UNKNOWN`——
   **不重新生成键、不重发写请求**。核实引用（操作类型＋幂等键）交给受控调用方，模型拿不到它，
   也无法构造一个幂等键。未解决的 `unknown` 会让同一会话的新交易被拒绝、让 `cancel` 不再声称
   "没有提交任何交易"。

8. **注入与锁定**（任务11）：用户输入、产品文案和银行返回里的自由文本都是数据。工具白名单是
   不可变表，身份只从 `TrustedContext` 来，确认与验证状态只由 `confirm`／`verify` 两条输入推进，
   模型能表达的动作里根本没有资金写——所以"注入改变门禁"在结构上不可达，提示词只是最后一句
   说明。运行时按用户记两笔账：**连续技术失败**（模型不可用、模型输出不可用、只读路径的实现／
   协议错误、预算或追问用尽）到 3 次，或**累计绕过门禁事件**（模型写了资金写／授权动作、白名单
   外的工具、或往只带业务输入的决策里塞授权字段）到 3 次，就锁定该用户在本进程的写操作。业务
   结论（缺字段、余额不足、验证码错误、状态非法、结果未知）既不算攻击也不延续技术失败；任何
   非技术失败的结论都会把连续技术失败清零。这一层锁定是**本进程的内存状态**，与银行持久化的
   `SecurityState`（自己的阈值与计数）不是同一套，互不替代，本任务也不改银行的阈值。

9. **审计**：运行时每次处理记一行**脱敏动作摘要**（结论、错误码、业务编号、`trace_id`），银行
   那侧另有持久化业务审计。验证码、模型输出原文、提示词与请求文本都不进日志，也不进银行的
   审计与幂等结果。

时钟由外部注入（`build_wealth_agent(model, bank, clock)`）：待办有效期是运行时的判断，
不能用本机日期覆盖银行已经返回的 `valid`／`redeemable_minor`。

依赖方向：`application → workflow/llm/infrastructure/domain → contracts`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypedDict
from uuid import uuid4

from app.integration.model_client import ModelClient
from app.modules.wealth.contracts.bank import (
    CONFIRMATION_TRANSPORT_UNSUPPORTED,
    PURCHASE_OPERATION,
    REDEEM_OPERATION,
    IdempotencyLookup,
    PurchaseReceipt,
    PurchaseSnapshot,
    RedeemSnapshot,
    RedemptionReceipt,
    WealthBank,
    WealthBankError,
    WriteAuthorization,
)
from app.modules.wealth.contracts.context import ConversationContext
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import (
    Confirmation,
    Pending,
    WealthResponse,
    WealthStatus,
)
from app.modules.wealth.domain.rules import (
    PurchaseCheck,
    RedeemCheck,
    check_purchase,
    check_redeem,
    purchase_success,
    redeem_success,
)
from app.modules.wealth.infrastructure.tools import READ_TOOLS, ReadTool, read_with_one_retry
from app.modules.wealth.workflow.graph import build_read_graph, invoke_read_workflow
from app.modules.wealth.workflow.state import (
    RESOURCE_ID_FIELDS,
    SLOT_RESOURCES,
    SLOTS,
    PendingData,
    SessionData,
    clues_to_slots,
    initial_state,
)
from app.tools.protocol import ErrorCode, TrustedContext

__all__ = [
    "MAX_BYPASS_EVENTS",
    "MAX_TECHNICAL_FAILURES",
    "MAX_VERIFICATION_ATTEMPTS",
    "PENDING_TTL",
    "TECHNICAL_FAILURE_CODES",
    "Clock",
    "ProcessClock",
    "SystemClock",
    "WealthAgent",
    "scope_id",
]

logger = logging.getLogger(__name__)

# 待办的有效期（需求 §7.4）。由注入的时钟判断，不用本机日期。
PENDING_TTL = timedelta(minutes=5)

# 同一笔申购连续 3 次验证码错误后锁定写操作，是旧验证码交互的产物。点击确认不再向用户索要
# 验证码，所以运行时**不再**按这个阈值累计任何东西；常量留着是因为银行的持久化 `SecurityState`
# 仍然按自己的阈值（5 次 / 15 分钟）独立生效，文档与集成说明也还引用这个数字。
MAX_VERIFICATION_ATTEMPTS = 3

# 同一个用户连续 3 次技术失败，或累计 3 次绕过门禁的尝试之后，锁定本进程的写操作（计划任务11）。
# 与验证码那一条共用同一张"被锁定的用户"表，但触发条件各自独立；银行那一套阈值仍然独立生效。
MAX_TECHNICAL_FAILURES = 3
MAX_BYPASS_EVENTS = 3

# 哪些失败算"技术失败"：模型不可用、模型输出不可用、只读路径的实现／协议错误、预算或追问用尽。
# 业务结论**不**在此列——余额不足、状态非法、验证码错误、结果未知都只是业务事实，不是技术故障，
# 也就不会把用户推向锁定（需求 §7.4："正常缺字段和余额不足不算攻击"）。
TECHNICAL_FAILURE_CODES = frozenset(
    {
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.PROTOCOL,
        ErrorCode.INTERNAL,
        ErrorCode.OUTPUT_SCHEMA_VIOLATION,
    }
)

NO_ANSWER_SUMMARY = "本次没有可以回复的内容。"
CRASH_SUMMARY = "本次处理未能完成，请稍后重试。"
NO_PENDING_SUMMARY = "没有可以回答的待办，或者该待办已经失效。"
NOTHING_TO_CONFIRM_SUMMARY = "当前没有待确认的理财操作。"
NOTHING_TO_RECONCILE_SUMMARY = "当前没有需要核实的理财交易，或者该核实引用已经失效。"
ONLY_INPUT_PENDING_SUMMARY = "当前待办只需要补充信息，还没有可以确认的理财操作。"
CANCELLED_SUMMARY = "已停止当前未提交的理财待办，没有提交任何交易。"
NOTHING_RUNNING_SUMMARY = "当前没有正在进行的理财操作，已停止本次查询。"
SUBMITTING_SUMMARY = "这笔操作正在提交中，请稍候；不要重复点击。"

# 点击确认是唯一的资金提交入口；旧验证码入口明确停用，而不是继续当作正常流程。验证码本身没有
# 消失——它现在是**确认的一个参数**，只在银行要求时出现在确认框上，由用户当场输入。
VERIFY_RETIRED_SUMMARY = (
    "验证码确认这条旧入口已经停用：请重新查看预览，在确认框上点确认；"
    "如果银行要求验证码，确认框里会有输入框。"
)

# 能力不兼容：这次确认**没有**变成一笔资金请求，因为这条通道根本走不通（交接文档第 6 节 D2 的
# 形态之一）。它不是业务失败，也不是结果未知，而是一个明确的集成阻塞。
CONFIRMATION_UNSUPPORTED_SUMMARY = (
    "这笔交易还没有提交：当前银行的写通道无法完成这种确认方式，"
    "所以本次确认无法完成。待办会保留到过期，您可以取消；等接入方提供可用的确认能力后再试。"
)

# 银行要求验证码，而这次确认没有带上用户输入的验证码。能确定地说"没有提交"：资金写请求根本
# 没有发出去。它不是结果未知，也不是"银行拒绝了"——是这一次点击少了一个必填项。
CODE_REQUIRED_SUMMARY = (
    "这笔操作需要验证码：请在确认框里输入银行发来的验证码，再点确认。本次没有提交任何交易。"
)

# 申购状态机的四句话术。它们都以证据为准：没有成交回执就不说"已完成"。
NOT_CONFIRMED_SUMMARY = "请先确认这笔申购的预览。"
LOCKED_SUMMARY = (
    "本进程内该用户的理财写操作已被锁定；已经提交的交易不受影响，请核对后再联系管理员。"
)
REFRESH_BLOCKED_SUMMARY = "提交前复核发现条件已经变化，这笔操作没有提交：{reasons}"
REFRESH_FAILED_SUMMARY = "提交前复核没能完成，这笔{label}没有提交：{message}"
MISMATCH_SUMMARY = "银行回执与确认过的操作内容不一致，本次没有按成功处理。"
UNVERIFIED_NOTE = "（未能在银行重新查到这笔订单，请以订单号为准。）"

# 结果未知的四种说法（任务11）。它们都不宣布成功，也都不宣布失败——"查不到"不等于"没提交"。
UNKNOWN_TAILS: Mapping[str, str] = {
    # 已经按原幂等键核实过，银行那边暂时没有这个键的记录。
    "not_found": "已按原幂等键在银行核实，暂时没有查到记录；稍后可以用同一个待办再核实一次，程序不会重新下单。",
    # 核实这次调用本身失败了（银行暂时不可用）。
    "lookup_failed": "按原幂等键核实这次没有成功（银行暂时不可用）；请稍后用同一个待办再核实一次，程序不会重新下单。",
    # 查到了记录，但读不出可用于确认这笔业务的回执。
    "unreadable": "按原幂等键在银行查到了记录，但读不出可以确认这笔业务的回执；请人工核对订单后再处理，程序不会重新下单。",
    # 还没有核实过（例如重复确认一笔结果未知的交易）。
    "unresolved": "这笔交易的结果仍未核实；可以用同一个待办再核实一次，程序不会重新下单。",
}
RECONCILED_NOTE = "（原写请求没有返回结果，已按原幂等键在银行核实到这笔订单，没有重复提交。）"

# 结果未知的那一笔还没核实之前，同一会话不能再开一笔新交易；取消也不声称它被撤回。
UNRESOLVED_WRITE_SUMMARY = (
    "本会话还有一笔结果未知的理财交易没有核实，不能在同一会话里发起新的理财交易。"
    "请先用原来的待办核实那一笔（核实不会重新下单），再开始新的交易。"
)
CANCELLED_WITH_UNKNOWN_SUMMARY = (
    "已停止当前未提交的理财待办，没有提交新的交易。但本会话有一笔结果未知的交易尚未核实，"
    "取消不代表它没有提交：请用原来的待办按原幂等键核实那一笔。"
)

# 待办只有一个阶段了：确认即提交。常量保留是因为银行侧与文档仍用它描述"这一笔还在等用户"。
STAGE_CONFIRMATION = "confirmation"


class Clock(Protocol):
    """运行时的时钟：待办的有效期由它判断。"""

    def now(self) -> datetime: ...


class ProcessClock:
    """进程时钟，UTC。演示与测试可以注入自己的时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# 别名兼容：推荐新代码使用 ProcessClock 以免与 app.core.clock.SystemClock 混淆
SystemClock = ProcessClock


# 写意图现在是纯数据（PendingData 的 write 键）：原来挂在 _WriteIntent 上的三个属性和
# preview_facts() 变成了下面这一组模块级函数。snapshot 与 receipt 用契约自己的 as_state() 存成
# dict——图状态只放 msgpack 认识的东西。


def _write_operation(write: Mapping[str, Any]) -> str:
    """这一笔是申购还是赎回（PURCHASE_OPERATION／REDEEM_OPERATION）。"""

    return str(write["operation"])


def _write_idempotency_key(write: Mapping[str, Any]) -> str:
    """这一笔的幂等键：一个意图只有一个，确认、重复确认和核实都用它。"""

    return str(write["idempotency_key"])


def _write_snapshot(write: Mapping[str, Any]) -> PurchaseSnapshot | RedeemSnapshot:
    """写意图里被冻结的业务快照：状态里存 dict，用契约自己的 from_state 还原。"""

    return _snapshot_from_state(_write_operation(write), write.get("snapshot"))


def _write_receipt(write: Mapping[str, Any]) -> PurchaseReceipt | RedemptionReceipt | None:
    """银行已提交的回执；还没有回执时是 None。"""

    payload = write.get("receipt")
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("写意图里的回执不是对象")
    if _write_operation(write) == REDEEM_OPERATION:
        return RedemptionReceipt.from_state(payload)
    return PurchaseReceipt.from_state(payload)


def _write_preview_facts(write: Mapping[str, Any]) -> dict[str, Any]:
    """待办里冻结的业务快照：申购与赎回各用一个键，前端按它展示"确认的是这一笔"。"""

    return _preview_facts(_write_operation(write), _write_snapshot(write))


def _new_write(operation: str, snapshot: PurchaseSnapshot | RedeemSnapshot) -> dict[str, Any]:
    """铸一份写意图：被冻结的业务快照，加上这一刻的唯一幂等键。

    "一个意图只有一个幂等键"（需求 §7.4）就是这一对：确认、重复确认和重复核实都指向同一个键，
    所以银行最多只会执行一次。operation 区分申购与赎回——它决定提交时调用哪个银行写方法，以及
    回执要和哪一份快照比对。它不是业务事实：金额、账户、产品和成交状态都在银行。
    """

    return {
        "operation": operation,
        "idempotency_key": f"wealth-{uuid4().hex}",
        "snapshot": snapshot.as_state(),
        # 银行已提交的回执与那次写后核实的响应事实：重复的确认事件直接返回它，不再写第二次。
        "receipt": None,
        "confirmed_facts": {},
        # 上一次提交的结果未知：之后同一个待办上的输入只按原幂等键核实，绝不重发写请求。
        "reconcile_required": False,
        # 这一次提交正在银行里进行。并发点击只对应同一个幂等键，所以第二次进来直接如实返回，
        # 绝不发起第二次资金写（进程内是同步的，这一位是并发与重入的显式守卫）。
        "submitting": False,
    }


def _pending_expired(pending: Mapping[str, Any], now: datetime) -> bool:
    """待办过期没有：expires_at 在图状态里是 ISO 字符串，这里按时间比较。"""

    try:
        expires_at = datetime.fromisoformat(str(pending["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _as_session(session: Mapping[str, Any] | None) -> SessionData:
    """把调用方递进来的会话补齐成完整形状；缺字段用默认值。

    调用方可能给一份空字典（新会话），所以后面的下标读写不假设每个键都在。
    """

    current = dict(session or {})
    last_holding = current.get("last_holding")
    unresolved = current.get("unresolved")
    return SessionData(
        slots=dict(current.get("slots") or {}),
        shown={str(key): tuple(value) for key, value in (current.get("shown") or {}).items()},
        intent_version=int(current.get("intent_version") or 0),
        clarifications=int(current.get("clarifications") or 0),
        pending_id=current.get("pending_id"),
        last_holding=dict(last_holding) if last_holding else None,
        unresolved=dict(unresolved) if unresolved else None,
    )


class _Unresolved(TypedDict, total=False):
    """一笔结果未知、还没有核实的资金写（任务11）。

    它是核实引用，不是业务事实：操作类型、原幂等键和当时确认过的业务快照。放在会话上而不是
    只放在待办里，因为待办会过期、会被下一次追问替换掉，而一笔可能已经成交的交易不能因为
    "待办没了"就永远查不清。它不携带任何授权能力：用它只能按原键读一次银行，不能再写。

    三种用法：同会话不允许再开新交易、cancel 不许声称它被撤回、待办过期后仍能按原键核实。
    """

    pending_id: str
    operation: str
    idempotency_key: str
    # 当时冻结的业务快照（PurchaseSnapshot.as_state()／RedeemSnapshot.as_state()）：核实回来的
    # 回执要和它比对，所以它必须一起留着。
    snapshot: dict[str, Any]


class WealthAgent:
    """独立理财 Agent：一次 `handle` 处理一次输入。"""

    def __init__(
        self,
        model: ModelClient,
        bank: WealthBank,
        clock: Clock,
        *,
        tools: Mapping[str, ReadTool] = READ_TOOLS,
        graph_factory: Any = build_read_graph,
    ) -> None:
        self._model = model
        self._bank = bank
        self._clock = clock
        self._tools = tools
        self._graph_factory = graph_factory
        # 本进程内被锁定写操作的用户。三种触发条件共用这张表：验证码连续错误到上限（需求 §7.4）、
        # 连续技术失败到上限、累计绕过门禁事件到上限（计划任务11）。它不是银行的持久化安全状态，
        # 进程重启即解除；银行那一套阈值与计数仍然独立生效。
        self._locked_users: set[str] = set()
        # 每个用户连续几次技术失败（任何非技术失败的结论都会清零）。
        self._technical_failures: dict[str, int] = {}
        # 每个用户累计几次绕过门禁的尝试（只增不减：一次尝试就是一次尝试）。
        self._bypass_events: dict[str, int] = {}

    def set_model(self, model: ModelClient) -> None:
        """Replace the model the next turn's graph is built with.

        The base swaps models at runtime when the configuration panel is saved, and this
        agent is a process-lifetime object: pinning the model at construction would leave
        every later turn running on the model that happened to be configured at startup.
        """

        self._model = model

    def handle(
        self,
        context: TrustedContext,
        request: WealthRequest,
        *,
        session: SessionData,
        pending: PendingData | None,
        conversation: ConversationContext | None = None,
    ) -> tuple[WealthResponse, SessionData, PendingData | None]:
        """处理一次输入。同步的：工具协议与模型边界都是同步的。

        三件事在这里定死：handle **永远不抛异常**（实现 bug 收敛成一次诚实的 failed）；"先如实
        回答，再记一笔账"（_settle）——锁定只影响之后的写操作，不改这一次已经得出的结论，把一次
        已经查清的结果改写成失败反而是对用户说了假话；跨轮状态由调用方持有——session 与 pending
        递进来，连同响应一起递回去。

        这个对象本身只留本进程的账（写锁定、连续技术失败、越门禁计数），不再按用户／会话存任何
        槽位或待办：重启即丢的临时状态归调用方（图状态）保存。

        conversation 只由外层服务构造并注入：它不是 WealthRequest 的字段，模型的动作词表里也
        没有能表达它的动作。没有它（独立 CLI、单元测试）时，会话隔离退回 (user_id, session_id)，
        记忆为空——外部历史不可用不等于这个 Agent 不能用。
        """

        session = _as_session(session)
        trace_id = f"wealth_{uuid4().hex}"
        try:
            response, gate_events, pending = self._dispatch(
                context, request, session, pending, conversation, trace_id
            )
        except Exception as error:
            # 实现 bug，不是业务失败：只记类型，不把可能含业务数据的异常文本写给用户或日志。
            logger.error("wealth flow crashed: %s", type(error).__name__)
            response, gate_events = _failed(ErrorCode.INTERNAL, CRASH_SUMMARY, trace_id), 0
        self._settle(context.user_id, response, gate_events, trace_id)
        return response, session, pending

    def _dispatch(
        self,
        context: TrustedContext,
        request: WealthRequest,
        session: SessionData,
        pending: PendingData | None,
        conversation: ConversationContext | None,
        trace_id: str,
    ) -> tuple[WealthResponse, int, PendingData | None]:
        """按输入类型分流；返回 (响应, 这一次处理里模型绕过门禁的次数, 之后的待办)。"""

        kind = request.kind
        scope = scope_id(context, conversation)
        # 过期的待办在这里丢掉（I-21）：以前是按进程里的那张待办表统一清理，现在没有全局表了，
        # 过期只能是每个节点对**递进来的这一份**待办自己的判断——过期就不再往下传。
        pending = self._prune(session, pending)
        # 外层的语义线索先铺底，会话里的值覆盖它：本轮模型新读到的值再由工作流覆盖在上面。
        # 这就是"当前用户明确纠正 > 较新的明确用户信息 > 较早上下文"。
        base_slots = {**_clue_slots(conversation), **session["slots"]}
        base_shown = {**_clue_shown(conversation), **session["shown"]}
        history = _history_text(conversation)

        if kind is WealthRequestKind.MESSAGE:
            text = (request.text or "").strip()
            if not text:
                return (
                    _failed(ErrorCode.VALIDATION, "请先说明你想查询的理财内容。", trace_id),
                    0,
                    pending,
                )
            # 上一次的追问还没被回答：把它交给工作流，让模型自己先问一句是否放弃旧任务，
            # 而不是静默替换用户正在回答的那个问题。
            resume = self._valid_pending(
                session["pending_id"], context, session, pending, scope=scope
            )
            state = initial_state(
                request_text=text,
                slots=base_slots,
                shown=base_shown,
                clarifications=session["clarifications"],
                pending_prompt=resume["question"] if resume else None,
                pending_missing=resume["missing"] if resume else (),
                recent_holding=session["last_holding"],
                **history,
            )
        elif kind is WealthRequestKind.ANSWER:
            record = self._take_pending(request.pending_id, context, session, pending, scope=scope)
            if record is None:
                return _failed(ErrorCode.INVALID_STATE, NO_PENDING_SUMMARY, trace_id), 0, pending
            # 待办已经被这次回答吃掉了：它不属于这一轮之后的状态。
            pending = None
            text = (request.text or "").strip()
            if not text:
                return (
                    _failed(ErrorCode.VALIDATION, "回答内容为空，请重新说明。", trace_id),
                    0,
                    pending,
                )
            state = initial_state(
                request_text=record["question"],
                question=record["question"],
                answer_text=text,
                slots=base_slots,
                shown=base_shown,
                clarifications=session["clarifications"],
                recent_holding=session["last_holding"],
                **history,
            )
        elif kind is WealthRequestKind.CONFIRM:
            # 确认是唯一的资金提交入口：不调用模型，也不接受任何来自文本的"已确认"。
            # 验证码（如果银行要求）是这次确认的一个参数，由用户在确认框上当场输入。
            response, pending = self._confirm(
                request.pending_id,
                context,
                session,
                pending,
                trace_id,
                request.verification_code,
                scope=scope,
            )
            return response, 0, pending
        elif kind is WealthRequestKind.RECONCILE:
            # 核实只读银行：按原幂等键问一次"那一笔到底成交没有"，不构成任何写授权。
            response = self._reconcile_event(
                request.pending_id, context, session, pending, trace_id, scope=scope
            )
            return response, 0, pending
        elif kind is WealthRequestKind.VERIFY:
            # 旧验证码入口已停用：明确拒绝，不再把它当作正常流程，也不因此去碰银行。
            return _failed(ErrorCode.INVALID_STATE, VERIFY_RETIRED_SUMMARY, trace_id), 0, pending
        else:
            response, pending = self._cancel(
                context, session, pending, request.pending_id, trace_id, scope=scope
            )
            return response, 0, pending

        try:
            graph = self._graph_factory(self._model, self._bank, context, tools=self._tools)
            result = invoke_read_workflow(graph, state)
        except Exception as error:
            # 图本身崩了（实现 bug）：只记类型，不把可能含业务数据的异常文本写给用户或日志。
            logger.error("wealth read flow crashed: %s", type(error).__name__)
            return _failed(ErrorCode.INTERNAL, CRASH_SUMMARY, trace_id), 0, pending
        self._commit(session, result, answered=kind is WealthRequestKind.ANSWER)
        # 工作流改动了槽位就会作废旧待办（_commit 清掉会话里的指针）：手上这份跟着一起丢。
        if session["pending_id"] is None:
            pending = None
        response, pending = self._respond(session, pending, result, context, trace_id, scope=scope)
        update = _context_update(session, result)
        if update:
            # 结构化记忆随响应回到调用方，由外层校验后写进检查点。它只有语义线索和来源引用：
            # 确认、验证码与幂等键没有任何字段可以走这条路。
            response = replace(response, context_update=update)
        return response, _gate_events(result), pending

    def _settle(
        self,
        user_id: str,
        response: WealthResponse,
        gate_events: int,
        trace_id: str,
    ) -> None:
        """一次处理结束后记一笔账：脱敏审计行、技术失败与越门禁计数、以及到上限后的写锁定。

        三类结果严格分开，只有第一类算技术失败（需求 §7.4、计划任务11）：

        - **技术失败**：模型不可用、模型输出不可用、只读路径的实现／协议错误、预算或追问用尽——
          连续 3 次就锁定写操作，任何非技术失败的结论都会把它清零。
        - **绕过门禁事件**：模型试图调用资金写／授权动作、白名单外的工具，或往只带业务输入的决策里
          塞授权字段——累计 3 次就锁定，且不因成功而清零（一次尝试就是一次尝试）。
        - **业务结论**：缺字段、余额不足、验证码错误、状态非法、结果未知——都不是攻击，不累计
          越门禁事件，也不延续技术失败。

        锁定只是本进程的内存状态：它不改银行的持久化安全状态，也不影响已经提交的交易。
        """

        if gate_events > 0:
            self._bypass_events[user_id] = self._bypass_events.get(user_id, 0) + gate_events
        if (
            response.status is WealthStatus.FAILED
            and response.error_code in TECHNICAL_FAILURE_CODES
        ):
            self._technical_failures[user_id] = self._technical_failures.get(user_id, 0) + 1
        else:
            self._technical_failures[user_id] = 0
        self._audit(response, trace_id)

        if user_id in self._locked_users:
            return
        failures = self._technical_failures.get(user_id, 0)
        bypasses = self._bypass_events.get(user_id, 0)
        if failures >= MAX_TECHNICAL_FAILURES or bypasses >= MAX_BYPASS_EVENTS:
            self._locked_users.add(user_id)
            logger.warning(
                "wealth flow: writes locked for this process (technical_failures=%s bypasses=%s)",
                failures,
                bypasses,
            )

    @staticmethod
    def _audit(response: WealthResponse, trace_id: str) -> None:
        """一行脱敏的动作摘要：结论、错误码、业务编号、`trace_id`。

        只记"做了什么、结论是什么、涉及哪个业务号"。**不记**验证码、请求文本、提示词、模型输出
        原文和模型私有推理——验证码是秘密，其余是业务数据（需求 §7.4）。银行那侧另有持久化的
        业务审计，两者不是同一份记录。
        """

        logger.info(
            "wealth audit: status=%s error_code=%s business_ids=%s trace_id=%s",
            response.status.value,
            response.error_code or "-",
            ",".join(sorted(f"{key}:{value}" for key, value in response.business_ids.items()))
            or "-",
            trace_id,
        )

    # ------------------------------------------------------------ 会话状态的推进

    def _commit(
        self, session: SessionData, result: Mapping[str, Any], *, answered: bool = False
    ) -> None:
        """把一次处理的结论写回会话：槽位、展示顺序、意图版本和澄清计数。

        `answered` 表示这一轮消费的是用户对上一次提问的回答。它参与澄清计数：**回答本身就是
        进展**。没有这一条，一次正常的五题问卷会在第五题被"连续澄清到上限"截断——那正是本轮要
        修的重测失败（对话 #59）的一半成因。工具查到新事实同样算进展。

        **只把会话词表 `SLOTS` 写回会话。** 预览节点返回的 `slots` 里还带着单笔订单金额
        （`amount_minor`，它随 `prepare_purchase`／`prepare_redeem` 进来），那一个**不能**留在
        会话里：留着它，下一笔没说金额时程序就会认为"金额已经知道"，于是把上一笔的金额当成
        这一笔的金额——正是"推荐预算／上一笔金额不能自动继承"要挡住的事（计划 §3.2 第 12 条）。
        订单金额只活在它那一次预览的冻结快照里。
        """

        produced = {str(name): value for name, value in (result.get("slots") or {}).items()}
        slots = {name: produced[name] for name in SLOTS if produced.get(name) is not None}
        changed = slots != session["slots"]
        if changed:
            # 改任何槽位就是换了一个意图：旧待办（含旧确认、旧验证请求与旧幂等键）立即失效。
            session["slots"] = slots
            session["intent_version"] += 1
            self._drop_pending(session)

        shown = result.get("shown")
        if shown:
            session["shown"] = {str(key): tuple(value) for key, value in shown.items()}
        # 刚问过用户"要哪一个"的资源：这一轮查到的记录就是摆在用户眼前的候选。补进展示顺序，
        # 下一轮他直接报编号才认——编号类槽位只认"查到的"或"展示过的"，而下一轮不会重查一遍。
        for resource, identifiers in _asked_candidates(result).items():
            session["shown"][resource] = identifiers

        asked = bool(result.get("wealth_asked_question"))
        progressed = answered or any(
            observation.get("status") == "succeeded"
            for observation in result.get("observations") or []
        )
        # 连续澄清：有新信息（槽位变了）、这一轮有实际进展（回答了问题或查到了新事实），
        # 或者这一轮根本没有追问时，重新计数。
        session["clarifications"] = (
            0 if (changed or progressed or not asked) else session["clarifications"]
        ) + (1 if asked else 0)

    def _cancel(
        self,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        pending_id: str | None,
        trace_id: str,
        *,
        scope: str,
    ) -> tuple[WealthResponse, PendingData | None]:
        """取消只终止未提交的工作。

        丢掉草稿待办和草稿槽位（含待确认的申购快照与它的幂等键），并且不声称撤销任何订单：
        没有提交过的写操作本来就没有资金变动，已经提交或结果未知的订单不在取消范围内。

        **带 `pending_id` 的取消只作用于那一个待办。** 迟到的旧取消事件（用户在旧预览上按了
        取消，而那时已经生成了新预览）不得取消新待办——否则一次过期的点击会把用户当前的确认
        悄悄撤掉。只有不带 `pending_id` 的取消（"停掉当前这件事"）才作用于当前活动待办。

        归属校验交给 `_valid_pending`，所以它拿的是**真正的调用方身份**（`TrustedContext`），
        而不是从会话对象上抄一个 `user_id`：待办归属现在由调用方随会话一起递进来，这里仍然
        按 `scope_id` 复核一次，免得同一个待办编号落到另一个会话上。

        结果未知的那一笔尤其不能声称被撤回（任务11）：取消只清掉草稿，会话里的 `unresolved`
        与它的核实引用都留着，话术也明说"取消不代表它没有提交"。
        """

        if pending_id is not None and pending_id.strip():
            target = self._valid_pending(pending_id, context, session, pending, scope=scope)
            if target is None:
                # 这个待办已经不属于当前意图（被替换、过期或本就不存在）：什么也不动。
                # 尤其不能顺手取消当前那个——用户取消的是他看到的那个旧待办。
                return (
                    WealthResponse(
                        status=WealthStatus.CANCELLED,
                        summary=NOTHING_RUNNING_SUMMARY,
                        trace_id=trace_id,
                    ),
                    pending,
                )
            self._drop_pending(session)
            had_pending = True
        else:
            had_pending = session["pending_id"] is not None
            self._drop_pending(session)
        pending = None

        if session["slots"]:
            session["slots"] = {}
            session["intent_version"] += 1
        session["clarifications"] = 0
        if session["unresolved"] is not None:
            summary = CANCELLED_WITH_UNKNOWN_SUMMARY
        else:
            summary = CANCELLED_SUMMARY if had_pending else NOTHING_RUNNING_SUMMARY
        return (
            WealthResponse(
                status=WealthStatus.CANCELLED,
                summary=summary,
                trace_id=trace_id,
            ),
            pending,
        )

    def _remember(
        self,
        session: SessionData,
        context: TrustedContext,
        question: str,
        missing: Sequence[str] = (),
        *,
        scope: str,
        write: Mapping[str, Any] | None = None,
    ) -> PendingData:
        """铸一个待办（补充信息待办或资金待办）。

        同会话只有一个活动待办：新的待办替换旧的（会话里的旧指针立即失效）。资金待办的幂等键
        在铸它时生成一次，之后不再改变。待办本身随 `(session, pending)` 一起回到调用方，
        运行时不再留任何按用户／会话索引的表。
        """

        self._drop_pending(session)
        expires_at = self._clock.now() + PENDING_TTL
        pending_id = f"wp_{uuid4().hex}"
        session["pending_id"] = pending_id
        return PendingData(
            pending_id=pending_id,
            user_id=context.user_id,
            scope_id=scope,
            question=question,
            missing=tuple(str(name) for name in missing),
            intent_version=session["intent_version"],
            expires_at=expires_at.isoformat(),
            write=dict(write) if write is not None else None,
        )

    def _view(self, record: Mapping[str, Any], *, prompt: str | None = None) -> Pending:
        """待办对外可见的部分：只有编号、要用户做什么、什么时候过期。"""

        return Pending(
            pending_id=str(record["pending_id"]),
            prompt=str(record["question"]) if prompt is None else prompt,
            expires_at=str(record["expires_at"]),
        )

    def _valid_pending(
        self,
        pending_id: str | None,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        *,
        scope: str,
    ) -> PendingData | None:
        """取出一个属于本人、本会话、本意图且未过期的待办；否则 None。

        四种"不是待办"的情况（不存在、不是本人的、已经过期、属于旧意图版本）返回同一个结果：
        调用方只需要知道这个输入没有归属，不需要知道别人的待办是否存在。

        归属现在是结构性的（待办由调用方随会话一起递进来），但这里仍然按 `scope_id` 复核一次：
        调用方把同一个待办编号配到另一个会话上时，输入一律拒绝。
        """

        if not isinstance(pending_id, str) or not pending_id.strip():
            return None
        if session["pending_id"] != pending_id:
            return None
        if pending is None or pending.get("pending_id") != pending_id:
            return None
        if pending.get("user_id") != context.user_id or pending.get("scope_id") != scope:
            return None
        if _pending_expired(pending, self._clock.now()):
            return None
        if pending.get("intent_version") != session["intent_version"]:
            return None
        return pending

    def _take_pending(
        self,
        pending_id: str | None,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        *,
        scope: str,
    ) -> PendingData | None:
        """取出并消费一个有效待办。"""

        record = self._valid_pending(pending_id, context, session, pending, scope=scope)
        if record is None:
            return None
        self._drop_pending(session)
        return record

    def _drop_pending(self, session: SessionData) -> None:
        session["pending_id"] = None

    def _prune(self, session: SessionData, pending: PendingData | None) -> PendingData | None:
        """丢掉过期的待办（I-21），返回还要继续往下传的那一份。

        以前这里扫的是进程里的待办表：按时间过一遍，过期就删，顺带清掉会话上的指针。跨轮状态
        搬到图状态以后没有全局表可扫了，过期变成**每个节点对递进来的这一份待办自己的判断**：
        过期就不再往下传，会话里的指针一起清掉；没过期就原样返回。这里不另建一张清理表——那会
        把会话状态又放回进程内存（R-2）。
        """

        if pending is None:
            return None
        if not _pending_expired(pending, self._clock.now()):
            return pending
        if session["pending_id"] == pending.get("pending_id"):
            session["pending_id"] = None
        return None

    def _writes_locked(self, user_id: str) -> bool:
        """该用户在本进程内的理财写操作是否已被锁定。"""

        return user_id in self._locked_users

    def _unresolved_block(self, session: SessionData, trace_id: str) -> WealthResponse | None:
        """同会话有一笔结果未知的写没核实完时，拒绝开一笔新交易；没有就返回 `None`。

        这是"unknown 未解决时不能用新交易覆盖旧意图"（计划任务11）：新交易会铸一个新的幂等键、
        新的一份快照，等于把一笔可能已经成交的交易挤出会话；所以先要求把那一笔核实清楚。
        只读查询、追问和取消不受影响——被挡住的是"再写一笔钱"。
        """

        if session["unresolved"] is None:
            return None
        return _failed(ErrorCode.INVALID_STATE, UNRESOLVED_WRITE_SUMMARY, trace_id)

    def _confirm(
        self,
        pending_id: str | None,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        trace_id: str,
        verification_code: str | None = None,
        *,
        scope: str,
    ) -> tuple[WealthResponse, PendingData | None]:
        """用户点击确认：校验待办、写前复核，然后提交**一次**资金写。

        这是唯一的资金提交入口。四条不变量在结构上成立：

        - **不接受文本授权。** `kind=confirm` 只由可信按钮事件产生，`pending_id` 由运行时签发；
          聊天里的"确认"两个字走 `kind=message`，模型也拿不到能铸造确认的动作。
        - **以冻结快照为准。** 金额、产品、账户、期限都来自预览那一刻冻结的快照；确认输入里
          没有这些字段，改任何槽位都会换意图版本并作废这个待办。
        - **同一意图只有一个幂等键。** 重复或并发的点击指向同一个键；已经拿到回执的直接返回
          原回执，不会写第二次。
        - **结果未知只核实。** 上一次提交结果不明时，确认不再有意义：按原键读一次银行。

        `verification_code` 是**用户当场输入**的值，不是配置里的演示码。银行的能力声明
        （`write_authorization`）说不需要它时，这里必须是 `None`；说需要而没带，
        `_submit_confirmed` 会在发出任何资金请求之前拒绝。
        """

        record = self._valid_pending(pending_id, context, session, pending, scope=scope)
        if record is None:
            # 待办已经不在了：它可能过期，或者被下一次追问／新预览替换掉了。
            #
            # 如果会话上还留着一笔未核实的写、而且这次点击带的就是它的核实引用，那用户的意图
            # 显然是"那一笔到底怎么样了"——按原键核实它。
            unresolved = session["unresolved"]
            if unresolved is not None and unresolved.get("pending_id") == pending_id:
                return (
                    self._reconcile_event(
                        pending_id, context, session, pending, trace_id, scope=scope
                    ),
                    pending,
                )
            # 否则这就是一次点空了的旧按钮：如实说"没有待确认的操作"，而不是借用核实那句话术
            # ——两者对用户意味着完全不同的下一步。
            return (
                _failed(ErrorCode.INVALID_STATE, NOTHING_TO_CONFIRM_SUMMARY, trace_id),
                pending,
            )
        write = record.get("write")
        if write is None:
            return _failed(ErrorCode.INVALID_STATE, ONLY_INPUT_PENDING_SUMMARY, trace_id), pending
        snapshot = _write_snapshot(write)
        operation = _write_operation(write)
        receipt = _write_receipt(write)
        if receipt is not None:
            # 这一笔已经成交：重复或并发的点击返回同一份回执，不再写第二次。
            return (
                self._confirmed_response(
                    session,
                    operation,
                    snapshot.product_id,
                    receipt,
                    write["confirmed_facts"],
                    trace_id,
                ),
                pending,
            )
        if write.get("reconcile_required"):
            # 上一次的结果未知：只按原幂等键核实，绝不重发写请求。核实是只读的，即使该用户
            # 已经被锁定写操作也允许——不然一笔可能已经成交的交易就永远查不清了。
            return (
                self._reconcile(
                    context,
                    session,
                    trace_id,
                    operation=operation,
                    idempotency_key=_write_idempotency_key(write),
                    snapshot=snapshot,
                    write=write,
                    pending=pending,
                ),
                pending,
            )
        if self._writes_locked(context.user_id):
            return _failed(ErrorCode.TOOL_DENIED, LOCKED_SUMMARY, trace_id), pending
        return self._submit_confirmed(
            record, context, session, pending, trace_id, verification_code
        )

    def _submit_confirmed(
        self,
        record: PendingData,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        trace_id: str,
        verification_code: str | None = None,
    ) -> tuple[WealthResponse, PendingData | None]:
        """受控提交：先复核银行事实，再发一次资金写，最后核实回执。

        它取代了旧的 `_verify`：复核、回执比对、失败分类、幂等与核实逻辑都从那里原样搬过来，
        没有第二套交易实现。验证码不再是"另一条流程"，而是这次提交的一个参数：银行的能力声明
        说要，就必须带上用户输入的那个值；说不要，就不带。

        写前复核失败时能确定地说"没有提交"：那时资金写请求根本没有发出去。这不是结果未知。
        """

        write = record.get("write")
        assert write is not None  # 调用方已经确认这是资金待办
        snapshot = _write_snapshot(write)
        operation = _write_operation(write)

        authorization = self._bank.write_authorization(operation, snapshot.amount_minor)
        if authorization is WriteAuthorization.UNSUPPORTED:
            # 这条通道根本走不通：**在发出任何资金请求之前**返回。待办保留到 TTL，用户可以
            # 取消或等接入方打通——偷偷带一个码去试只会让用户以为交易已经提交过了。
            logger.warning(
                "wealth flow: the bank cannot complete this confirmation (%s)",
                CONFIRMATION_TRANSPORT_UNSUPPORTED,
            )
            return (
                _failed_with_reason(
                    ErrorCode.INVALID_STATE,
                    CONFIRMATION_UNSUPPORTED_SUMMARY,
                    trace_id,
                    CONFIRMATION_TRANSPORT_UNSUPPORTED,
                ),
                pending,
            )
        if (
            authorization is WriteAuthorization.CLICK_AND_CODE
            and not (verification_code or "").strip()
        ):
            # 银行要验证码而这次点击没带：**在发出任何资金请求之前**返回，并且能确定地说
            # "没有提交"。绝不从配置里取一个演示码顶上——那样用户以为是自己确认成交的，
            # 实际上系统替他答了门禁。
            logger.info("wealth flow: this confirmation needs a code the user did not supply")
            return _failed(ErrorCode.VALIDATION, CODE_REQUIRED_SUMMARY, trace_id), pending

        try:
            if operation == REDEEM_OPERATION:
                blocked = self._refresh_redeem(context, snapshot)
            else:
                blocked = self._refresh(context, session, snapshot)
        except WealthBankError as failure:
            logger.warning("wealth flow: the pre-write refresh failed (%s)", failure.code)
            return (
                _failed(
                    failure.code,
                    REFRESH_FAILED_SUMMARY.format(
                        label="模拟赎回" if operation == REDEEM_OPERATION else "模拟申购",
                        message=failure.message,
                    ),
                    trace_id,
                ),
                pending,
            )
        if blocked is not None:
            # 条件变了：作废这笔待办，让用户重新预览、重新确认——绝不用已确认的金额去凑一次写。
            self._drop_pending(session)
            return (
                _failed(
                    ErrorCode.INVALID_STATE,
                    REFRESH_BLOCKED_SUMMARY.format(reasons="；".join(blocked.reasons)),
                    trace_id,
                ),
                None,
            )

        if write.get("submitting"):
            # 同一待办的提交已经在进行：并发点击只对应同一幂等键，绝不发起第二次资金写。
            logger.info("wealth flow: a confirmation for this pending is already submitting")
            return _failed(ErrorCode.INVALID_STATE, SUBMITTING_SUMMARY, trace_id), pending
        write["submitting"] = True
        try:
            receipt = self._submit_at_bank(context, write, verification_code)
        except WealthBankError as failure:
            return self._write_failure(
                record, context, session, pending, failure, trace_id
            ), pending
        finally:
            write["submitting"] = False

        problem = _receipt_problem(operation, receipt, snapshot)
        if problem is not None:
            logger.error(
                "wealth flow: the receipt does not match the confirmed intent: %s", problem
            )
            return _failed(ErrorCode.PROTOCOL, MISMATCH_SUMMARY, trace_id), pending

        write["receipt"] = receipt.as_state()
        if operation == REDEEM_OPERATION:
            write["confirmed_facts"] = self._confirm_redeem_at_bank(context, receipt, snapshot)
        else:
            write["confirmed_facts"] = self._confirm_at_bank(context, receipt, snapshot)
        return (
            self._confirmed_response(
                session,
                operation,
                snapshot.product_id,
                receipt,
                write["confirmed_facts"],
                trace_id,
            ),
            pending,
        )

    def _submit_at_bank(
        self, context: TrustedContext, write: dict[str, Any], verification_code: str | None
    ) -> PurchaseReceipt | RedemptionReceipt:
        """一次资金写。身份、确认与幂等键都由运行时给；验证码是用户输入的那个（不需要时是 `None`）。

        验证码在这里只是**转发**：要不要带、带什么，由调用方按银行的能力声明决定，这一层不猜、
        也不补默认值。
        """

        operation = _write_operation(write)
        intent = _write_snapshot(write).intent()
        if operation == REDEEM_OPERATION:
            return self._bank.redeem(
                context, intent, verification_code, _write_idempotency_key(write)
            )
        return self._bank.purchase(
            context, intent, verification_code, _write_idempotency_key(write)
        )

    def _reconcile_event(
        self,
        pending_id: str | None,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        trace_id: str,
        *,
        scope: str,
    ) -> WealthResponse:
        """按原幂等键核实一笔结果未知的写（`kind=reconcile`，取代旧 verify 的核实职责）。

        它**只读银行**，不构成任何写授权，也不检查写锁定：一笔可能已经成交的交易不能因为
        待办过期、用户被锁定或进程重启就永远查不清。核实引用来自会话上那份 `_Unresolved`
        （操作类型 + 原幂等键 + 当时冻结的业务快照），不是模型能构造的东西。
        """

        record = self._valid_pending(pending_id, context, session, pending, scope=scope)
        write = record.get("write") if record is not None else None
        if write is not None and (write.get("reconcile_required") or write.get("receipt") is None):
            return self._reconcile(
                context,
                session,
                trace_id,
                operation=_write_operation(write),
                idempotency_key=_write_idempotency_key(write),
                snapshot=_write_snapshot(write),
                write=write,
                pending=pending,
            )

        unresolved = session["unresolved"]
        if unresolved is None or unresolved.get("pending_id") != pending_id:
            return _failed(ErrorCode.INVALID_STATE, NOTHING_TO_RECONCILE_SUMMARY, trace_id)
        try:
            snapshot = _snapshot_from_state(unresolved["operation"], unresolved["snapshot"])
        except ValueError as error:
            logger.error("wealth flow: unusable unresolved snapshot (%s)", type(error).__name__)
            return _failed(ErrorCode.INTERNAL, CRASH_SUMMARY, trace_id)
        return self._reconcile(
            context,
            session,
            trace_id,
            operation=unresolved["operation"],
            idempotency_key=unresolved["idempotency_key"],
            snapshot=snapshot,
            pending=pending,
        )

    def _refresh(
        self, context: TrustedContext, session: SessionData, snapshot: PurchaseSnapshot
    ) -> PurchaseCheck | None:
        """提交前重新读银行事实并重跑预检；返回 `None` 表示可以提交。

        金额与账户用快照里的（用户确认过的），不因为刷新而改变；刷新只用来判断"还允不允许提交"。
        这些读同样"最多重试一次"（计划任务11）；银行的失败原样抛出，由调用方按结果未知／明确拒绝
        分类——**这里还没有发过写请求**，所以调用方可以确定地说"没有提交"。
        """

        product = read_with_one_retry(lambda: self._bank.get_product(context, snapshot.product_id))
        accounts = read_with_one_retry(lambda: self._bank.list_accounts(context))
        profile = read_with_one_retry(lambda: self._bank.get_risk_profile(context))
        account = next(
            (item for item in accounts if item.account_id == snapshot.from_account_id), None
        )
        risk_level = profile.risk_level if (profile is not None and profile.valid) else None
        check = check_purchase(
            product,
            account=account,
            risk_level=risk_level,
            amount_minor=snapshot.amount_minor,
            liquidity=session["slots"].get("liquidity"),
            user_id=context.bank_customer_id,
        )
        return None if check.allowed else check

    def _refresh_redeem(
        self, context: TrustedContext, snapshot: RedeemSnapshot
    ) -> RedeemCheck | None:
        """赎回提交前重新读银行事实并重跑预检；返回 `None` 表示可以提交。

        金额用快照里的（用户确认过的、已经解析成具体整数分），刷新只判断"还允不允许提交"。
        **不读风险测评**：赎回是取回自己的持仓，不要求重新做问卷（计划任务10）。
        """

        holding = read_with_one_retry(lambda: self._bank.get_holding(context, snapshot.holding_id))
        accounts = read_with_one_retry(lambda: self._bank.list_accounts(context))
        account = next(
            (item for item in accounts if item.account_id == snapshot.to_account_id), None
        )
        check = check_redeem(
            holding,
            account=account,
            amount_minor=snapshot.amount_minor,
            user_id=context.bank_customer_id,
        )
        return None if check.allowed else check

    def _write_failure(
        self,
        record: PendingData,
        context: TrustedContext,
        session: SessionData,
        pending: PendingData | None,
        failure: WealthBankError,
        trace_id: str,
    ) -> WealthResponse:
        """资金写失败的三条出口：结果未知、验证码被明确判错、银行明确拒绝。

        三条都**不**声称成功，区别在于下一步是什么：

        - **结果未知**：可能已经受理，只能按原幂等键核实（绝不重发写请求）。
        - **验证码被明确判错**：银行没有受理这笔写，所以能确定地说"没有提交"；待办仍然有效
          （TTL 没到、意图没变、幂等键没换），而用户下一步要做的正是重输一个码——所以把预览
          原样交回去重新等确认，而不是把整个流程结束掉让他从头再说一遍。
        - **银行明确拒绝**（余额不足、状态非法、参数不符、复核失败…）：如实转达，待办保留，
          用户可以改条件重来。
        """

        write = record.get("write")
        assert write is not None  # 调用方已经确认这是资金待办
        if failure.outcome_unknown:
            # 可能已经受理：不宣布成功，也不宣布失败。先按原幂等键核实一次；核不出来就保持 unknown，
            # 待办与幂等键都留着供继续核实（绝不重新生成键下单）。
            return self._reconcile(
                context,
                session,
                trace_id,
                operation=_write_operation(write),
                idempotency_key=_write_idempotency_key(write),
                snapshot=_write_snapshot(write),
                write=write,
                pending=pending,
            )
        if failure.verification_failed:
            # 用户输错了验证码。银行明确没有受理，所以这不是结果未知；而待办、快照和幂等键都还
            # 有效——重新确认同一笔不会写第二次（银行按同一个幂等键去重）。所以这里重新等确认。
            logger.info("wealth flow: the bank rejected the verification code")
            return self._repreview(record, failure.message, trace_id)
        # 银行明确拒绝（余额不足、状态非法、参数不符、复核失败…）：如实转达，待办保留，
        # 用户可以改条件重来。这里能确定地说"没有提交"——银行没有受理这笔写。
        return _failed(failure.code, failure.message, trace_id)

    def _repreview(self, record: PendingData, reason: str, trace_id: str) -> WealthResponse:
        """把同一笔待确认操作**重新**交回去等确认（验证码被判错之后）。

        重新交回去的是同一份冻结快照与同一个待办编号：意图没变，所以幂等键也不换——用户重输
        一个码提交时，银行仍然只可能执行一次。摘要里带上银行给出的原因，让用户知道要改的是
        验证码，而不是金额或账户。
        """

        write = record.get("write")
        return WealthResponse(
            status=WealthStatus.WAITING_CONFIRMATION,
            summary=f"{reason}请重新输入验证码后再确认。",
            facts=_write_preview_facts(write) if write is not None else {},
            pending=self._view(record),
            confirmation=self._confirmation_projection(record),
            trace_id=trace_id,
        )

    def _reconcile(
        self,
        context: TrustedContext,
        session: SessionData,
        trace_id: str,
        *,
        operation: str,
        idempotency_key: str,
        snapshot: PurchaseSnapshot | RedeemSnapshot,
        write: dict[str, Any] | None = None,
        pending: PendingData | None = None,
    ) -> WealthResponse:
        """按原幂等键核实一笔结果未知的资金写（任务11）。

        这是"回执丢失"唯一正确的出路：**不重新生成键、不重发写请求**，只问银行"这个键上有没有
        记录"。四条出口，三条都不是成功：

        - 查到了、并且与用户确认过的意图一致：按银行已提交的回执如实报告成交（银行只扣了一次），
          并说明这是按原键核实回来的；
        - 查到了但读不出可用的回执：仍是 unknown（读不懂不等于没成交）；
        - 查不到：仍是 unknown——"查不到"只说明这个键上没有记录，不能反推"没有扣款"；
        - 核实这次调用本身失败：仍是 unknown，并说明是核实没成功。
        """

        try:
            lookup = self._bank.find_order_by_key(context, operation, idempotency_key)
        except WealthBankError as failure:
            logger.warning("wealth flow: could not reconcile a write by key (%s)", failure.code)
            return self._unknown_response(
                session,
                trace_id,
                operation=operation,
                idempotency_key=idempotency_key,
                snapshot=snapshot,
                reason="lookup_failed",
                write=write,
                pending=pending,
            )

        if not lookup.found:
            return self._unknown_response(
                session,
                trace_id,
                operation=operation,
                idempotency_key=idempotency_key,
                snapshot=snapshot,
                reason="not_found",
                found=False,
                write=write,
                pending=pending,
            )

        receipt = _receipt_from_lookup(operation, lookup)
        if receipt is None:
            logger.warning("wealth flow: the idempotency record carries no usable receipt")
            return self._unknown_response(
                session,
                trace_id,
                operation=operation,
                idempotency_key=idempotency_key,
                snapshot=snapshot,
                reason="unreadable",
                found=True,
                write=write,
                pending=pending,
            )

        problem = _receipt_problem(operation, receipt, snapshot)
        if problem is not None:
            logger.error(
                "wealth flow: the reconciled receipt does not match the confirmed intent: %s",
                problem,
            )
            return _failed(ErrorCode.PROTOCOL, MISMATCH_SUMMARY, trace_id)

        facts = (
            self._confirm_redeem_at_bank(context, receipt, snapshot)
            if operation == REDEEM_OPERATION
            else self._confirm_at_bank(context, receipt, snapshot)
        )
        facts["reconciliation"] = _reconciliation_reference(
            operation, idempotency_key, found=True, order_id=receipt.order_id
        )
        # 核实完成：这一笔不再是"未解决"，同会话可以重新开始新的交易了。
        session["unresolved"] = None
        if write is not None:
            # 待办还活着：把回执写回去，重复的验证码事件会返回同一份，不会再写第二次。
            write["receipt"] = receipt.as_state()
            write["confirmed_facts"] = facts
        return self._confirmed_response(
            session,
            operation,
            snapshot.product_id,
            receipt,
            facts,
            trace_id,
            reconciled=True,
        )

    def _unknown_response(
        self,
        session: SessionData,
        trace_id: str,
        *,
        operation: str,
        idempotency_key: str,
        snapshot: PurchaseSnapshot | RedeemSnapshot,
        reason: str,
        found: bool | None = None,
        order_id: str | None = None,
        write: dict[str, Any] | None = None,
        pending: PendingData | None = None,
    ) -> WealthResponse:
        """结果未知的响应：既不成功也不失败，并把核实引用交给受控调用方。

        三件事在这里同时发生：待办（如果还在）被标成"只能核实"，会话上记下这笔未解决的写，
        并把**同一个 `pending_id`** 交回调用方。

        最后这一件是必须的：调用方（CLI、未来的确认卡片通道）只有拿着这个 `pending_id` 才能发
        `kind=reconcile`。不返回 `pending` 就等于告诉调用方"有一笔要核实，但我不告诉你是哪一笔"，
        核实入口在实践里就永远走不到。

        `session.unresolved` 是**核实引用**而不是授权：它让同会话不能再开新交易、让取消不许声称
        撤回，也让待办过期之后仍然能按原键读一次银行。核实引用里只有操作类型、幂等键和当时冻结的
        业务快照——**没有**验证码，也没有任何能写钱的东西。
        """

        if write is not None:
            write["reconcile_required"] = True
        reference = _reconciliation_reference(
            operation, idempotency_key, found=found, order_id=order_id
        )
        unresolved = session["unresolved"]
        reference_pending = session["pending_id"] or (
            unresolved.get("pending_id") if unresolved else ""
        )
        session["unresolved"] = _Unresolved(
            pending_id=reference_pending,
            operation=operation,
            idempotency_key=idempotency_key,
            snapshot=dict(snapshot.as_state()),
        )
        return WealthResponse(
            status=WealthStatus.UNKNOWN,
            summary=_unknown_summary(operation, reason),
            facts={**_preview_facts(operation, snapshot), "reconciliation": reference},
            error_code=ErrorCode.TRANSACTION_UNKNOWN,
            # 核实引用交回调用方；没有它，`kind=reconcile` 无从发起。
            pending=(
                self._view(pending)
                if pending is not None and pending.get("pending_id") == reference_pending
                else None
            ),
            trace_id=trace_id,
        )

    def _confirmation_projection(self, record: PendingData) -> Confirmation:
        """程序生成的确认卡片投影：调用方渲染"确认这一笔"所需的最小字段集合。

        它只搬运冻结快照里的业务字段，**不含**任何授权凭据（没有幂等键、没有快照原文、没有
        验证码）：真正的授权仍然只能由 `kind=confirm` + `pending_id` 触发。没有的数据不填造值。
        """

        write = record.get("write")
        assert write is not None
        operation = _write_operation(write)
        snapshot = _write_snapshot(write)
        # 要不要验证码由**银行的能力声明**回答，所以卡片和银行的门禁永远是同一个答案：
        # 卡片问了银行不要，用户就在为一个不存在的门禁输入秘密；银行要了卡片不问，用户点了
        # 也提交不了。两者都不允许发生，所以这里不猜、也不留一个可配置的开关。
        requires_code = (
            self._bank.write_authorization(operation, snapshot.amount_minor)
            is WriteAuthorization.CLICK_AND_CODE
        )
        if operation == REDEEM_OPERATION:
            return Confirmation(
                operation=operation,
                title="确认赎回",
                amount_minor=snapshot.amount_minor,
                product_name=getattr(snapshot, "product_name", "") or "",
                account_display=snapshot.to_account_id,
                expires_at=str(record["expires_at"]),
                requires_code=requires_code,
            )
        return Confirmation(
            operation=operation,
            title="确认申购",
            amount_minor=snapshot.amount_minor,
            product_name=getattr(snapshot, "product_name", "") or "",
            account_display=snapshot.from_account_id,
            expires_at=str(record["expires_at"]),
            requires_code=requires_code,
        )

    def _confirmed_response(
        self,
        session: SessionData,
        operation: str,
        product_id: str,
        receipt: PurchaseReceipt | RedemptionReceipt,
        facts: Mapping[str, Any],
        trace_id: str,
        *,
        reconciled: bool = False,
    ) -> WealthResponse:
        """成交响应：回执、写后核实的事实与业务号；话术由程序模板生成。

        申购与赎回各有一套由回执驱动的成交话术；两者都只在拿到银行已提交回执、并且回执与意图
        一致时才用。申购成功还会把新仓记进会话，供"赎回刚才买的"绑定。

        `reconciled` 表示这份回执是**按原幂等键核实回来的**（原写请求丢了回执）：话术里要说明
        这一点，用户才知道它没有被重复提交。
        """

        facts = dict(facts)
        if operation == REDEEM_OPERATION:
            summary = redeem_success(receipt, balance_minor=facts.get("redeem_balance_minor"))
            if facts.get("redeem_verified") is not True:
                summary = f"{summary}\n{UNVERIFIED_NOTE}"
            if reconciled:
                summary = f"{summary}\n{RECONCILED_NOTE}"
            return WealthResponse(
                status=WealthStatus.COMPLETED,
                summary=summary,
                facts=facts,
                business_ids={
                    "order_id": receipt.order_id,
                    "holding_id": receipt.holding_id,
                    "transaction_id": receipt.transaction_id,
                    "product_id": product_id,
                },
                trace_id=trace_id,
            )

        summary = purchase_success(receipt, balance_minor=facts.get("purchase_balance_minor"))
        if facts.get("purchase_verified") is not True:
            summary = f"{summary}\n{UNVERIFIED_NOTE}"
        if reconciled:
            summary = f"{summary}\n{RECONCILED_NOTE}"
        # 记住这笔新仓："赎回刚才买的"绑定它（同一会话内）。它是临时提示，不是权威事实。
        session["last_holding"] = {
            "holding_id": receipt.holding_id,
            "product_id": receipt.product_id,
            "order_id": receipt.order_id,
        }
        return WealthResponse(
            status=WealthStatus.COMPLETED,
            summary=summary,
            facts=facts,
            business_ids={
                "order_id": receipt.order_id,
                "holding_id": receipt.holding_id,
                "transaction_id": receipt.transaction_id,
                "product_id": product_id,
            },
            trace_id=trace_id,
        )

    def _confirm_at_bank(
        self, context: TrustedContext, receipt: PurchaseReceipt, snapshot: PurchaseSnapshot
    ) -> dict[str, Any]:
        """写后核实：回执进事实，再从银行读回订单与账户（读不到就如实标注）。

        读回是"必要时"的复核，不是成交的前提：回执本身已经是银行的提交结果，所以读不回来不会
        把一次成交说成失败，只会在话术里说明"没能在银行重新查到这笔订单"。
        """

        facts: dict[str, Any] = {"purchase_receipt": asdict(receipt), "purchase_verified": False}
        try:
            order = self._bank.get_order(context, receipt.order_id)
            accounts = self._bank.list_accounts(context)
        except WealthBankError as failure:
            logger.warning("wealth flow: could not read the order back (%s)", failure.code)
            return facts
        if (
            order.order_id != receipt.order_id
            or order.holding_id != receipt.holding_id
            or order.product_id != snapshot.product_id
            or order.amount_minor != snapshot.amount_minor
            or order.status != "completed"
        ):
            logger.error("wealth flow: the order read back does not match the receipt")
            return facts
        facts["orders"] = [asdict(order)]
        facts["accounts"] = [asdict(account) for account in accounts]
        facts["purchase_verified"] = True
        balance = next(
            (
                account.balance_minor
                for account in accounts
                if account.account_id == snapshot.from_account_id
            ),
            None,
        )
        if balance is not None:
            facts["purchase_balance_minor"] = balance
        return facts

    def _confirm_redeem_at_bank(
        self, context: TrustedContext, receipt: RedemptionReceipt, snapshot: RedeemSnapshot
    ) -> dict[str, Any]:
        """赎回写后核实：回执进事实，再从银行读回订单、剩余持仓与到账账户（读不到就如实标注）。

        释放本金与实现收益是银行算出来的分摊（需求 §6.2：Agent 不另算权威数值），所以这里只
        核对订单与回执对得上，并把银行读回的剩余持仓和到账余额原样放进事实。
        """

        facts: dict[str, Any] = {"redeem_receipt": asdict(receipt), "redeem_verified": False}
        try:
            order = self._bank.get_order(context, receipt.order_id)
            holding = self._bank.get_holding(context, receipt.holding_id)
            accounts = self._bank.list_accounts(context)
        except WealthBankError as failure:
            logger.warning("wealth flow: could not read the redemption back (%s)", failure.code)
            return facts
        if (
            order.order_id != receipt.order_id
            or order.holding_id != receipt.holding_id
            or order.amount_minor != snapshot.amount_minor
            or order.status != "completed"
        ):
            logger.error("wealth flow: the redemption order read back does not match the receipt")
            return facts
        facts["orders"] = [asdict(order)]
        facts["holdings"] = [asdict(holding)]
        facts["accounts"] = [asdict(account) for account in accounts]
        facts["redeem_verified"] = True
        balance = next(
            (
                account.balance_minor
                for account in accounts
                if account.account_id == snapshot.to_account_id
            ),
            None,
        )
        if balance is not None:
            facts["redeem_balance_minor"] = balance
        return facts

    # ------------------------------------------------------------------ 工作流

    def _respond(
        self,
        session: SessionData,
        pending: PendingData | None,
        result: Mapping[str, Any],
        context: TrustedContext,
        trace_id: str,
        *,
        scope: str,
    ) -> tuple[WealthResponse, PendingData | None]:
        facts = dict(result.get("facts") or {})
        business_ids = dict(result.get("business_ids") or {})
        answer = str(result.get("answer") or "")
        observations = list(result.get("observations") or [])

        failure = result.get("failure")
        if failure:
            return (
                WealthResponse(
                    status=WealthStatus.FAILED,
                    summary=answer or NO_ANSWER_SUMMARY,
                    facts=facts,
                    business_ids=business_ids,
                    error_code=result.get("error_code"),
                    trace_id=trace_id,
                ),
                pending,
            )

        purchase = result.get("purchase")
        if purchase:
            # 申购预览：只到"待确认"为止。快照由工作流冻结，待办与幂等键由这里铸。
            blocked = self._unresolved_block(session, trace_id)
            if blocked is not None:
                return blocked, pending
            if self._writes_locked(context.user_id):
                return _failed(ErrorCode.TOOL_DENIED, LOCKED_SUMMARY, trace_id), pending
            try:
                snapshot = PurchaseSnapshot.from_state(purchase)
            except ValueError as error:
                # 工作流与运行时之间的契约被破坏：这是实现 bug，不带着半个意图去写钱。
                logger.error("wealth flow: unusable purchase snapshot (%s)", type(error).__name__)
                return _failed(ErrorCode.INTERNAL, CRASH_SUMMARY, trace_id), pending
            record = self._remember(
                session,
                context,
                snapshot.preview,
                scope=scope,
                write=_new_write(PURCHASE_OPERATION, snapshot),
            )
            return (
                WealthResponse(
                    status=WealthStatus.WAITING_CONFIRMATION,
                    summary=snapshot.preview,
                    facts=facts,
                    business_ids=business_ids,
                    pending=self._view(record),
                    confirmation=self._confirmation_projection(record),
                    trace_id=trace_id,
                ),
                record,
            )

        redeem = result.get("redeem")
        if redeem:
            # 赎回预览：与申购同一套门禁（确认即提交、改参失效），只是方向相反。
            blocked = self._unresolved_block(session, trace_id)
            if blocked is not None:
                return blocked, pending
            if self._writes_locked(context.user_id):
                return _failed(ErrorCode.TOOL_DENIED, LOCKED_SUMMARY, trace_id), pending
            try:
                redeem_snapshot = RedeemSnapshot.from_state(redeem)
            except ValueError as error:
                logger.error("wealth flow: unusable redeem snapshot (%s)", type(error).__name__)
                return _failed(ErrorCode.INTERNAL, CRASH_SUMMARY, trace_id), pending
            redeem_record = self._remember(
                session,
                context,
                redeem_snapshot.preview,
                scope=scope,
                write=_new_write(REDEEM_OPERATION, redeem_snapshot),
            )
            return (
                WealthResponse(
                    status=WealthStatus.WAITING_CONFIRMATION,
                    summary=redeem_snapshot.preview,
                    facts=facts,
                    business_ids=business_ids,
                    pending=self._view(redeem_record),
                    confirmation=self._confirmation_projection(redeem_record),
                    trace_id=trace_id,
                ),
                redeem_record,
            )

        question = result.get("wealth_asked_question")
        if question:
            record = self._remember(
                session,
                context,
                str(question),
                result.get("missing_slots") or (),
                scope=scope,
            )
            return (
                WealthResponse(
                    status=WealthStatus.WAITING_INPUT,
                    summary=str(question),
                    facts=facts,
                    business_ids=business_ids,
                    pending=self._view(record),
                    trace_id=trace_id,
                ),
                record,
            )

        succeeded = [item for item in observations if item.get("status") == "succeeded"]
        failed_reads = [item for item in observations if item.get("status") == "failed"]
        if failed_reads and not succeeded:
            # 一次查询都没有成功：这是失败，不是"查到 0 条"。错误码取最后一次失败的原因。
            return (
                WealthResponse(
                    status=WealthStatus.FAILED,
                    summary=answer or "暂时无法查询银行数据。",
                    facts=facts,
                    business_ids=business_ids,
                    error_code=failed_reads[-1].get("error_code"),
                    trace_id=trace_id,
                ),
                pending,
            )
        if not answer:
            return _failed(ErrorCode.INTERNAL, NO_ANSWER_SUMMARY, trace_id), pending

        return (
            WealthResponse(
                status=WealthStatus.COMPLETED,
                summary=answer,
                facts=facts,
                business_ids=business_ids,
                trace_id=trace_id,
            ),
            pending,
        )


def _receipt_problem(
    operation: str,
    receipt: PurchaseReceipt | RedemptionReceipt,
    snapshot: PurchaseSnapshot | RedeemSnapshot,
) -> str | None:
    """回执与确认过的意图是否一致；返回第一条不一致的字段名或 None。

    这是"不能把'模型说成功了'当成银行执行结果"的同一件事的另一半：**银行回执也要和用户确认的
    那一笔对上**。金额、持仓／产品、本金和编号任何一个不符，都不按成功处理。
    """

    if operation == REDEEM_OPERATION:
        assert isinstance(receipt, RedemptionReceipt)
        assert isinstance(snapshot, RedeemSnapshot)
        if receipt.status != "completed":
            return "status"
        if receipt.holding_id != snapshot.holding_id:
            return "holding_id"
        if receipt.amount_minor != snapshot.amount_minor:
            return "amount_minor"
        for name in ("order_id", "holding_id", "transaction_id"):
            if not getattr(receipt, name).strip():
                return name
        return None

    assert isinstance(receipt, PurchaseReceipt)
    assert isinstance(snapshot, PurchaseSnapshot)
    if receipt.status != "completed":
        return "status"
    if receipt.product_id != snapshot.product_id:
        return "product_id"
    if receipt.amount_minor != snapshot.amount_minor:
        return "amount_minor"
    if receipt.principal_minor != snapshot.amount_minor:
        return "principal_minor"
    for name in ("order_id", "holding_id", "transaction_id"):
        if not getattr(receipt, name).strip():
            return name
    return None


def _asked_candidates(result: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """本轮追问点名的编号资源：这一轮查到的编号，按工具返回顺序。

    `shown` 的原意是"我按这个顺序给用户列了这些候选"，由模型在 `finish` 里声明；追问不一样，
    `ask_user` 只给问题和缺的槽位。但"请说明用哪个账户付款"这种问题已经把候选摆到用户眼前了，
    所以这里按 `missing_slots` 点名的资源，把这一轮查到的记录补进展示顺序。没问到的资源不补：
    `shown` 仍然是"最近一次展示"，不是"查过什么"。
    """

    missing = result.get("missing_slots") or ()
    resources = {SLOT_RESOURCES[name] for name in missing if name in SLOT_RESOURCES}
    if not resources:
        return {}
    facts = result.get("facts") or {}
    found: dict[str, tuple[str, ...]] = {}
    for resource in sorted(resources):
        field = RESOURCE_ID_FIELDS.get(resource)
        rows = facts.get(resource) if isinstance(facts, Mapping) else None
        if field is None or not isinstance(rows, list):
            continue
        identifiers: list[str] = []
        for row in rows:
            value = row.get(field) if isinstance(row, Mapping) else None
            if isinstance(value, str) and value.strip() and value not in identifiers:
                identifiers.append(value)
        if identifiers:
            found[resource] = tuple(identifiers)
    return found


def _failed(error_code: str, summary: str, trace_id: str) -> WealthResponse:
    return WealthResponse(
        status=WealthStatus.FAILED, summary=summary, error_code=error_code, trace_id=trace_id
    )


def _failed_with_reason(
    error_code: str, summary: str, trace_id: str, reason_code: str
) -> WealthResponse:
    """一次失败，外加一个机器可读的补充原因。

    错误码回答"哪一类失败"（这里是 INVALID_STATE：当前状态不支持这次操作），`reason_code`
    回答"具体是哪一种情况"（这里是确认通道能力不兼容）。接入方据此把"集成还没打通"和
    "业务被拒绝"分开渲染，而不是都显示成一句失败。
    """

    return WealthResponse(
        status=WealthStatus.FAILED,
        summary=summary,
        error_code=error_code,
        reason_code=reason_code,
        trace_id=trace_id,
    )


def _preview_facts(operation: str, snapshot: PurchaseSnapshot | RedeemSnapshot) -> dict[str, Any]:
    """待办里冻结的业务快照：申购与赎回各用一个键。"""

    key = "redeem_preview" if operation == REDEEM_OPERATION else "purchase_preview"
    return {key: snapshot.as_state()}


def _reconciliation_reference(
    operation: str,
    idempotency_key: str,
    *,
    found: bool | None,
    order_id: str | None = None,
) -> dict[str, Any]:
    """交给受控调用方的核实引用：用哪个操作类型、哪个幂等键去核实这一笔。

    它只用于**读**银行（`find_order_by_key`），不构成任何写授权；模型也拿不到它——幂等键由运行时
    铸，从不进模型上下文，模型的输出里也没有能表达它的字段。`found=None` 表示这一次没有核实成功
    （调用失败或还没核实过），不是"没提交"。
    """

    return {
        "operation_type": operation,
        "idempotent_key": idempotency_key,
        "found": found,
        "order_id": order_id,
    }


def _unknown_summary(operation: str, reason: str) -> str:
    """结果未知的话术：申购与赎回分别措辞，且都明说"既不成功也不失败"。"""

    label = "模拟赎回" if operation == REDEEM_OPERATION else "模拟申购"
    head = f"银行没有返回明确结果，这笔{label}的结果未知：既不能按成功处理，也不能说没有提交。"
    return head + UNKNOWN_TAILS.get(reason, UNKNOWN_TAILS["unresolved"])


def _receipt_from_lookup(
    operation: str, lookup: IdempotencyLookup
) -> PurchaseReceipt | RedemptionReceipt | None:
    """把按幂等键查到的原回执还原成回执对象；形状不对返回 `None`。

    `None` 是"读不出可以确认这笔业务的回执"，不是"没有这笔交易"：调用方据此仍然按 unknown 处理，
    绝不补一个默认值让它看起来像成交。
    """

    result = lookup.result
    if not isinstance(result, Mapping):
        return None
    try:
        if operation == REDEEM_OPERATION:
            return RedemptionReceipt.from_bank(result)
        return PurchaseReceipt.from_bank(result)
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot_from_state(
    operation: str, state: Mapping[str, Any]
) -> PurchaseSnapshot | RedeemSnapshot:
    """从会话上的核实引用还原业务快照；形状不对抛 `ValueError`。"""

    if operation == REDEEM_OPERATION:
        return RedeemSnapshot.from_state(state)
    return PurchaseSnapshot.from_state(state)


def _gate_events(result: Mapping[str, Any]) -> int:
    """这一次处理里模型绕过门禁的次数（工作流报上来的，缺省 0）。"""

    value = result.get("gate_violations")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def scope_id(context: TrustedContext, conversation: ConversationContext | None = None) -> str:
    """这次调用属于哪个会话：有外层会话时是 `conversation_id`，独立调用时是 `session_id`。

    它现在只用来**校验待办归属**（`PendingData.scope_id`）：跨轮状态已经搬进图状态，运行时不再
    按用户／会话索引任何表。只有一处定义，所以"两个映射必须同时改"不再是靠记性。
    """

    return conversation.conversation_id if conversation is not None else context.session_id


# ---------------------------------------------------------------------- 外层会话视图


def _clue_slots(conversation: ConversationContext | None) -> dict[str, Any]:
    """外层记下的"已答项"读回会话槽位；没有会话时为空。

    这些是**最低优先级**的线索：调用方按 `{**线索, **会话槽位}` 合并，本轮模型新读到的值再由
    工作流覆盖在上面。读不出来的线索直接丢掉，让模型重新问，而不是猜一个值。
    """

    if conversation is None:
        return {}
    return clues_to_slots(conversation.domain_context.get("answered"))


def _clue_shown(conversation: ConversationContext | None) -> dict[str, tuple[str, ...]]:
    """外层记下的"最近展示顺序"读回展示顺序；形状不对的资源名与编号一律丢掉。

    它让"第二个"在进程重启后仍然解析得对。解析出来的编号只是**定位线索**：需要业务决策时仍然
    要重新向银行查询，编号本身不构成任何事实。
    """

    if conversation is None:
        return {}
    raw = conversation.domain_context.get("shown")
    if not isinstance(raw, Mapping):
        return {}
    shown: dict[str, tuple[str, ...]] = {}
    for resource, identifiers in raw.items():
        if resource not in RESOURCE_ID_FIELDS or not isinstance(identifiers, Sequence):
            continue
        if isinstance(identifiers, (str, bytes, bytearray)):
            continue
        values = tuple(
            entry.strip() for entry in identifiers if isinstance(entry, str) and entry.strip()
        )
        if values:
            shown[str(resource)] = values
    return shown


def _history_text(conversation: ConversationContext | None) -> dict[str, str]:
    """交给工作流起始状态的三块历史：摘要、近期轮次、外层线索。

    它们都是**历史上下文**，不是指令，也不是本轮事实：提示词里逐块标了这一点，工作流也只把它们
    渲染给模型看，不写进 `facts`。
    """

    if conversation is None:
        return {"summary_text": "", "history_text": "", "memory_text": ""}
    return {
        "summary_text": (conversation.summary_text or "").strip(),
        "history_text": (conversation.history_text or "").strip(),
        "memory_text": _memory_lines(conversation),
    }


def _memory_lines(conversation: ConversationContext) -> str:
    """把外层线索渲染成几行可读文本：目标、已答项、最近展示顺序、业务引用。"""

    lines: list[str] = []
    goal = conversation.domain_context.get("goal")
    if isinstance(goal, str) and goal.strip():
        lines.append(f"- 上一次的任务目标：{goal.strip()}")
    answered = conversation.domain_context.get("answered")
    if isinstance(answered, Mapping) and answered:
        items = "、".join(
            f"{name}={value}"
            for name, value in sorted(answered.items(), key=lambda item: str(item[0]))
        )
        lines.append(f"- 已经回答过的信息：{items}")
    shown = conversation.domain_context.get("shown")
    if isinstance(shown, Mapping) and shown:
        for resource, identifiers in sorted(shown.items(), key=lambda item: str(item[0])):
            if isinstance(identifiers, Sequence) and not isinstance(
                identifiers, (str, bytes, bytearray)
            ):
                listed = "、".join(str(item) for item in identifiers)
                if listed:
                    lines.append(f"- 上次按顺序展示过的 {resource}：{listed}")
    references = conversation.domain_context.get("references")
    if isinstance(references, Mapping) and references:
        items = "、".join(
            f"{name}={value}"
            for name, value in sorted(references.items(), key=lambda item: str(item[0]))
        )
        lines.append(f"- 上次涉及的业务引用（需要时重新核实）：{items}")
    return "\n".join(lines)


def _context_update(session: SessionData, result: Mapping[str, Any]) -> dict[str, Any] | None:
    """这一次处理要交给外层保存的结构化记忆；没有可记的就返回 `None`。

    只有语义线索和来源引用，**没有**任何授权：冻结快照、幂等键、未核实的写引用都留在运行时
    自己的内存里，既不回写外层，也不进模型上下文。金额一律用文本，因为外层的记忆白名单只收
    文本——这不是业务数据通道，业务数据仍然只在银行。

    `answered` 只收**会话槽位**（`SLOTS`），单笔订单金额 `amount_minor` 明确排除：它是这一笔的
    订单参数，不是用户说过的长期条件。把它写进记忆，下一笔没说金额时就会被"继承"上一笔的金额
    ——那正是推荐预算与订单金额分离这条修复要挡住的事。
    """

    goal = str(result.get("request_text") or "").strip()
    answered = {
        name: _memory_text(session["slots"][name])
        for name in SLOTS
        if session["slots"].get(name) is not None
    }
    shown = {name: list(values) for name, values in (session["shown"] or {}).items() if values}
    references = {
        str(name): str(value)
        for name, value in (result.get("business_ids") or {}).items()
        if isinstance(value, str) and value.strip()
    }

    update: dict[str, Any] = {}
    if goal:
        update["goal"] = goal
    if answered:
        update["answered"] = answered
    if shown:
        update["shown"] = shown
    if references:
        update["references"] = references
    return update


def _memory_text(value: Any) -> str:
    """一个槽位值在记忆里的写法：金额与时间写成数字文本，其余原样。"""

    if isinstance(value, bool):
        return str(value).lower()
    return str(value)
