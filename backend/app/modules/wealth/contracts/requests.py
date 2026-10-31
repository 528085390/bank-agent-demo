"""WealthAgent 的外部输入：一次 handle 的请求，以及它携带的可信身份。

`TrustedContext` 的定义在 `app.tools.protocol`（工具边界共用一份），这里只做转出：
理财契约不另造一份身份类型，否则“绑定的银行客户号”会有两个来源。

请求里**没有**任何授权标志位：确认是 `kind=confirm`，核实是 `kind=reconcile`，两者都只带
`pending_id`；不存在 `confirmed`／`idempotency_key` 这类模型可以伪造的标志位：dataclass 是
frozen + slots，多余字段直接 `TypeError`，不会被悄悄忽略。

**验证码只在 `kind=confirm` 上，而且只有在银行要求时才存在。** 它由用户在确认框上当场输入，
经受控的按钮事件交给运行时——它**不是**模型能产生的东西，也不进模型上下文。银行的能力声明
（`WealthBank.write_authorization`）说不需要验证码时，这里必须是 `None`；需要时必须是用户
输入的值，运行时不代填、不猜、也不用配置里的固定码冒充。

**旧 `kind=verify` 入口仍然停用**：保留在词表里只是为了能明确拒绝它。点击确认是唯一入口，
验证码是它的一个**参数**，不是一条独立的流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.tools.protocol import TrustedContext

__all__ = ["TrustedContext", "WealthRequest", "WealthRequestKind"]


class WealthRequestKind(StrEnum):
    """一次 handle 的意图类型（计划 §3.4）。"""

    MESSAGE = "message"
    """用户的一句话，可能是新任务、补充信息或一句无关的话。"""

    ANSWER = "answer"
    """回答运行时提出的待办问题，配合 `pending_id` 使用。"""

    CONFIRM = "confirm"
    """用户点击了确认按钮：对已展示的预览授权一次资金提交。

    银行要求验证码时，这次请求同时带上用户输入的 `verification_code`。
    """

    RECONCILE = "reconcile"
    """按原幂等键核实一笔结果未知的写；只读银行，不构成任何写授权。"""

    VERIFY = "verify"
    """**已停用**：旧验证码入口。运行时明确拒绝，不再把它当作正常流程。"""

    CANCEL = "cancel"
    """取消尚未提交的工作；不声称撤销已提交或结果未知的订单。"""


@dataclass(frozen=True, slots=True)
class WealthRequest:
    kind: WealthRequestKind
    text: str | None = None
    pending_id: str | None = None
    verification_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WealthRequestKind):
            try:
                object.__setattr__(self, "kind", WealthRequestKind(self.kind))
            except ValueError as error:
                allowed = ", ".join(kind.value for kind in WealthRequestKind)
                raise ValueError(f"kind 必须是 {allowed} 之一") from error
        for name in ("text", "pending_id", "verification_code"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串或 None")
        if self.verification_code is not None and self.kind not in (
            WealthRequestKind.CONFIRM,
            WealthRequestKind.VERIFY,
        ):
            # 只有"确认"和它那条已停用的旧入口与验证码有关。别的入口带上它，说明调用方把验证码
            # 当成了通用字段——那正是"验证码只走独立受控输入"要挡住的事，所以在构造期就拒绝。
            # `verify` 仍然收下它是刻意的：那条入口要能被**用业务话术拒绝**，而不是抛一个 Python
            # 异常给调用方。
            raise ValueError("verification_code 只能随 kind=confirm 传入")

    def model_input(self) -> dict[str, str]:
        """可以进入模型上下文的部分：只有 `kind` 与 `text`。

        `pending_id` 是运行时的会话状态，`verification_code` 是验证秘密——需求 §6.2 要求
        验证码只走独立受控输入与 HTTP 写请求，不进模型上下文，也不进日志。确认与核实事件
        同样不进模型上下文：它们是程序之间的受控调用，不是用户的一句话。
        """

        return {"kind": self.kind.value, "text": self.text or ""}
