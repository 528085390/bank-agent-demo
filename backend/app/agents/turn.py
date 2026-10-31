"""What a module hands back when it answers a whole turn by itself.

The other shape a module can have is tools: the base runs one call and reads its envelope.
That shape is enough when the answer is one lookup. It is not enough when the answer needs
its own model loop, its own conversation slots and its own gating -- a wealth request is
all three. This is the seam for the second shape: the module is handed the caller's
identity, the model of the moment, and the user's own words, and it returns one result.

Every type here is annotation-only, so this module imports nothing at runtime and cannot
take part in an import cycle with app.modules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.integration.model_client import ModelClient
    from app.tools.protocol import TrustedContext


class TurnStatus(StrEnum):
    COMPLETED = "completed"  # 办完了，summary/facts 可以给用户看
    WAITING_INPUT = "waiting_input"  # 在等用户补充；summary 就是那句追问
    # 在等用户在**确认框**上点一下。它与 WAITING_INPUT 是两件事：补充信息是一条聊天消息，
    # 确认是一次受控的按钮事件，带着服务端签发的待办编号回到模块——聊天里的"确认"两个字
    # 不能当授权用。带上这个状态时 `TurnResult.confirmation` 必须有值，否则调用方会渲染一个
    # 点不出结果的按钮（构造期就拒绝，见 `TurnResult.__post_init__`）。
    WAITING_CONFIRMATION = "waiting_confirmation"
    UNSUPPORTED = "unsupported"  # 这条流程模块自己也没接；summary 是如实说明
    FAILED = "failed"  # 没办成；summary 是给用户看的失败说明
    # 结果未知：模块发出了一次有副作用的请求，但没有拿到明确结果——可能已经成功，也可能没有。
    # 它**不是** FAILED：把一笔可能已经成交的交易说成"没办成"，用户会去重新下单，那是真正的资金
    # 风险。它也不是 COMPLETED：没有回执就不宣布成功。
    #
    # 这个状态必须存在，因为模块的领域状态里本来就有它（理财的 `WealthStatus.UNKNOWN`）。少了它，
    # 模块只能把它塞进 FAILED，而 Supervisor 的模型、`domain_results[].turn_status` 和任何按状态
    # 分支的调用方就都会读到"失败"——这正是本状态要防的事。
    #
    # 事件层（`subtask_end.status`）仍然是冻结的 SUCCEEDED/FAILED/DENIED 三值，见
    # `app.agents.supervisor._TURN_EVENT_STATUS`：真实状态经 `domain_results` 传给模型。
    UNKNOWN = "unknown"  # 有副作用的请求结果不明；既不说成功也不说失败，只能按原键核实


TERMINAL_TURN_STATUSES = frozenset(status.value for status in TurnStatus)

# The `kind` a checkpointed pause carries when it is waiting for a click rather than for
# typed input. It is the one value the base, the API and the client all have to agree on,
# so it lives next to the types rather than in the node that writes it.
CONFIRMATION_KIND = "confirmation"


@dataclass(frozen=True, slots=True)
class TurnConfirmation:
    """一次待确认操作的**展示投影**：确认框要显示什么。

    它是模块从自己冻结的业务快照生成的，只带展示字段——没有幂等键、没有验证码、没有快照
    原文。真正的授权只能由 `TurnAction(kind="confirm", pending_id=...)` 触发，所以把这张卡片
    交给前端，等于把"用户看到的是哪一笔"告诉它，而不是把授权能力交给它。

    `pending_id` 是服务端签发的待办编号：它同时是"确认的是这一笔"的绑定键。修改金额、账户、
    产品或交易方向会让模块换一个意图版本并作废这个编号，旧确认框随即失效。
    """

    pending_id: str
    operation: str
    title: str
    #: 卡面上要显示的金额（分）。``None`` 表示这次操作本来就没有金额 —— 账单发邮件
    #: 就是这种。以前它要求正整数，账单只好塞 1 分冒充，卡片上于是出现一个与操作
    #: 无关的「¥0.01」。金额不参与授权（参数以快照为准），所以「没有」可以直说。
    amount_minor: int | None
    product_name: str
    account_display: str
    confirm_label: str = "确认"
    cancel_label: str = "取消"
    expires_at: str | None = None
    # 银行的能力声明要求点击之外再给一个验证码时，它为真：卡片必须出现验证码输入，用户输入的
    # 值随确认一起交回来。它为假时卡片上不该有验证码输入——那会让用户为一个不存在的门禁输入
    # 秘密。两个判断点（卡片长什么样、提交时带什么）都由这一个值驱动，所以它们不会对不上。
    requires_code: bool = False
    code_label: str = "模拟验证码"

    def __post_init__(self) -> None:
        if not isinstance(self.pending_id, str) or not self.pending_id.strip():
            raise ValueError("TurnConfirmation.pending_id 必须是非空字符串")
        for name in ("operation", "title", "product_name", "account_display", "code_label"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"TurnConfirmation.{name} 必须是字符串")
        if self.amount_minor is not None:
            if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
                raise ValueError("TurnConfirmation.amount_minor 必须是整数分或 None")
            if self.amount_minor <= 0:
                raise ValueError("TurnConfirmation.amount_minor 必须是正整数分或 None")
        if self.expires_at is not None and not isinstance(self.expires_at, str):
            raise ValueError("TurnConfirmation.expires_at 必须是字符串或 None")
        if not isinstance(self.requires_code, bool):
            raise ValueError("TurnConfirmation.requires_code 必须是布尔值")

    def as_payload(self) -> dict[str, Any]:
        """The card as the client receives it. Only display fields travel."""

        return {
            "pending_id": self.pending_id,
            "operation": self.operation,
            "title": self.title,
            "amount_minor": self.amount_minor,
            "product_name": self.product_name,
            "account_display": self.account_display,
            "confirm_label": self.confirm_label,
            "cancel_label": self.cancel_label,
            "expires_at": self.expires_at,
            "requires_code": self.requires_code,
            "code_label": self.code_label,
        }


@dataclass(frozen=True, slots=True)
class TurnAction:
    """一次受控的按钮事件：用户在**已经看到的那一笔预览**上点了确认或取消。

    它只带三样东西：点的是哪一个待办、点的是确认还是取消，以及（银行要求时）用户当场输入的
    验证码。金额、产品、账户都不在这里——那些以服务端冻结的快照为准，改参数会作废这个
    `pending_id`。它也不是用户的一句话：聊天里的"确认"走的是普通消息通道，模块在那个通道里
    没有任何能铸造授权的动作。

    `verification_code` **不是**模型能产生的东西，也不经过图状态：它由鉴权路由接住，沿
    `TurnContext` 送到这里，所以它不会进检查点、不进账本、不进模型上下文。
    """

    kind: str
    pending_id: str
    verification_code: str | None = None

    CONFIRM = "confirm"
    CANCEL = "cancel"

    def __post_init__(self) -> None:
        if self.kind not in (self.CONFIRM, self.CANCEL):
            raise ValueError(f"TurnAction.kind 必须是 {self.CONFIRM} 或 {self.CANCEL}")
        if not isinstance(self.pending_id, str) or not self.pending_id.strip():
            raise ValueError("TurnAction.pending_id 必须是非空字符串")
        if self.verification_code is not None and not isinstance(self.verification_code, str):
            raise ValueError("TurnAction.verification_code 必须是字符串或 None")
        if self.verification_code is not None and self.kind is not self.CONFIRM:
            # 取消不需要验证码。带上它说明调用方把验证码当成了通用字段。
            raise ValueError("TurnAction.verification_code 只能随 confirm 传入")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """One domain's whole answer to one turn.

    summary is words for the user, never an internal note: the Supervisor's model reads it
    and may repeat it verbatim. facts is the detail behind it (product names, terms, rates)
    and is what the user actually asked for; it reaches the Supervisor's prompt.

    `confirmation` is set exactly when `status` is `WAITING_CONFIRMATION`: it is what the
    client renders as a confirmation box, and it is the only place the pending id travels
    out of the module. It is not an authorization credential -- see `TurnConfirmation`.
    """

    status: TurnStatus
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    business_ids: dict[str, Any] = field(default_factory=dict)
    confirmation: TurnConfirmation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TurnStatus):
            raise TypeError(f"turn status {self.status!r}, expected TurnStatus")
        if not isinstance(self.summary, str) or not self.summary:
            raise ValueError("a turn result needs a non-empty summary: it is shown to the user")
        if not isinstance(self.facts, dict):
            raise TypeError(f"turn facts {type(self.facts).__name__}, expected dict")
        if not isinstance(self.business_ids, dict):
            raise TypeError(f"turn business ids {type(self.business_ids).__name__}, expected dict")
        if self.confirmation is not None and not isinstance(self.confirmation, TurnConfirmation):
            raise TypeError(
                f"turn confirmation {type(self.confirmation).__name__}, expected TurnConfirmation"
            )
        if self.status is TurnStatus.WAITING_CONFIRMATION and self.confirmation is None:
            # A pause with nothing to show would render as a box with no operation, no amount
            # and no account: the user cannot tell what they are confirming. Fail where the
            # module built it rather than in front of a user.
            raise ValueError("waiting_confirmation needs a confirmation to show")
        if self.confirmation is not None and self.status is not TurnStatus.WAITING_CONFIRMATION:
            # A card sent with any other status is a button that can never resolve: the
            # pause it belongs to does not exist.
            raise ValueError("only waiting_confirmation may carry a confirmation")


TurnHandler = Callable[["TrustedContext", "ModelClient", str], TurnResult]
TurnActionHandler = Callable[["TrustedContext", "ModelClient", TurnAction], TurnResult]


def action_handler(turn: TurnHandler | None) -> TurnActionHandler | None:
    """The controlled action entry point of a whole-turn handler, if it has one.

    A module's whole-turn handler owns its own session state -- the pending question, the
    slots, the frozen snapshot behind a confirmation. The button event therefore has to
    reach **the same object** that produced the preview; a second factory would build a
    second agent that has never seen it, and the confirmation would be answered by a
    stranger. So the entry point is discovered on the handler the module already built
    (`TurnHandler.act`) instead of being declared separately.

    A handler without one is not broken: it simply cannot confirm anything, and the base
    says so honestly instead of pretending a click went through.
    """

    act = getattr(turn, "act", None)
    return act if callable(act) else None
