"""WealthAgent 的对外响应：状态、事实、业务 ID、待办和错误码。

状态集合就是调用方需要分支的全部情况（计划 §3.4）。两个不变量在这一层强制：

- `waiting_*` 必须带 `pending`：没有 `pending_id` 的等待响应，调用方无从继续，属于契约
  错误而不是业务失败。
- 响应没有授权字段：模型不能从一个响应里拿到“已确认”的凭据，`confirmed` 之类的字段
  会直接 `TypeError`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["Confirmation", "Pending", "WealthResponse", "WealthStatus"]


class WealthStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_VERIFICATION = "waiting_verification"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


# 这三个状态都要靠 pending 才能被调用方继续推进。
WAITING_STATUSES = frozenset(
    {
        WealthStatus.WAITING_INPUT,
        WealthStatus.WAITING_CONFIRMATION,
        WealthStatus.WAITING_VERIFICATION,
    }
)


@dataclass(frozen=True, slots=True)
class Pending:
    """一个等待用户继续的待办，只公开“等什么”。

    金额、账户、产品／持仓、意图版本、会话归属等由运行时保存：换意图必须由运行时重新
    确认，不能靠这个对象里的字段绕过确认。
    """

    pending_id: str
    prompt: str = ""
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pending_id, str) or not self.pending_id.strip():
            raise ValueError("pending_id 必须是非空字符串")
        if not isinstance(self.prompt, str):
            raise ValueError("prompt 必须是字符串")
        if self.expires_at is not None and not isinstance(self.expires_at, str):
            raise ValueError("expires_at 必须是字符串或 None")


@dataclass(frozen=True, slots=True)
class Confirmation:
    """待确认预览的**展示投影**：调用方渲染确认卡片所需的最小字段集合。

    它是程序从冻结快照生成的，不是内部 facts 的外发：金额、产品名和账户展示名都来自预览那一刻
    冻结的业务事实，没有的数据就不填（`None`），绝不造一个值。它**不含**任何授权凭据——没有
    幂等键、没有验证码、没有快照原文；真正的授权仍然只能由 `kind=confirm` + `pending_id` 触发。

    `requires_code` 说的是**这次确认还缺什么**：银行的能力声明要求点击之外再给一个验证码时，
    它为真，调用方就必须在卡片上渲染一个验证码输入，并把用户输入的值随确认一起交回来。
    它为假时卡片上不该出现验证码输入——那会让用户为一个不存在的门禁输入秘密。它由运行时按
    `WealthBank.write_authorization` 计算，所以卡片和银行的门禁永远是同一个答案。

    `code_label` 是那个输入框的标题，同样由程序给：理财和银行卡的验证码不是同一个东西，
    文案不该由调用方猜。
    """

    operation: str
    title: str
    amount_minor: int
    product_name: str
    account_display: str
    confirm_label: str = "确认"
    cancel_label: str = "取消"
    expires_at: str | None = None
    requires_code: bool = False
    code_label: str = "模拟验证码"

    def __post_init__(self) -> None:
        for name in ("operation", "title", "product_name", "account_display", "code_label"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise ValueError("amount_minor 必须是整数分")
        if self.amount_minor <= 0:
            raise ValueError("amount_minor 必须是正整数分")
        if not isinstance(self.requires_code, bool):
            raise ValueError("requires_code 必须是布尔值")


@dataclass(frozen=True, slots=True)
class WealthResponse:
    status: WealthStatus
    summary: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)
    business_ids: Mapping[str, str] = field(default_factory=dict)
    pending: Pending | None = None
    error_code: str | None = None
    trace_id: str | None = None
    # 结构化记忆回写（可选）：只有语义线索与来源引用，由调用方校验后交给外层保存。
    # 它**不是**事实通道：确认凭据、验证码、幂等键与冻结快照都没有字段可以走这里，
    # 模型也拿不到它——它由运行时按会话状态生成，不经模型。
    context_update: Mapping[str, Any] | None = None
    # 程序生成的确认卡片投影（可选）：只有 `waiting_confirmation` 会带它。
    confirmation: Confirmation | None = None
    # 机器可读的补充原因（可选）。错误码说明"哪一类失败"，它说明"具体是哪一种情况"，
    # 例如 `confirmation_transport_unsupported`：这次确认没有变成资金请求，因为当前银行的
    # 写通道要求验证码。接入方据此区分"集成未打通"与"业务被拒"。
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WealthStatus):
            try:
                object.__setattr__(self, "status", WealthStatus(self.status))
            except ValueError as error:
                allowed = ", ".join(status.value for status in WealthStatus)
                raise ValueError(f"status 必须是 {allowed} 之一") from error
        if self.status in WAITING_STATUSES and self.pending is None:
            raise ValueError(f"{self.status.value} 必须带 pending，否则调用方无法继续")
        if not isinstance(self.summary, str):
            raise ValueError("summary 必须是字符串")
        for name in ("facts", "business_ids"):
            if not isinstance(getattr(self, name), Mapping):
                raise ValueError(f"{name} 必须是映射")
        for name in ("error_code", "reason_code"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串或 None")
        if self.context_update is not None and not isinstance(self.context_update, Mapping):
            raise ValueError("context_update 必须是映射或 None")
        if self.confirmation is not None and not isinstance(self.confirmation, Confirmation):
            raise ValueError("confirmation 必须是 Confirmation 或 None")
        if self.confirmation is not None and self.status is not WealthStatus.WAITING_CONFIRMATION:
            # 确认卡片只在"等用户点击"时有意义：跟着别的状态发出去，调用方会渲染一个
            # 永远点不出结果的按钮。
            raise ValueError("只有 waiting_confirmation 可以带 confirmation")
