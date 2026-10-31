"""轻量权限分级。

首版不做真实认证、MFA 或权限阻断：permission_level、confirm_required、
mfa_required 只作为可观察字段和演示确认步骤。分级按金额固定规则计算。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ConfirmationRequiredError

GREEN = "green"
YELLOW = "yellow"
RED = "red"

# 1000 元起进入黄色（需确认），5000 元起进入红色（需确认 + MFA）。
YELLOW_THRESHOLD_MINOR = 100000
RED_THRESHOLD_MINOR = 500000


@dataclass(frozen=True)
class Decision:
    permission_level: str
    confirm_required: bool
    mfa_required: bool

    def as_dict(self) -> dict:
        return {
            "permission_level": self.permission_level,
            "confirm_required": self.confirm_required,
            "mfa_required": self.mfa_required,
        }


def classify(amount_minor: int) -> Decision:
    if amount_minor >= RED_THRESHOLD_MINOR:
        return Decision(RED, confirm_required=True, mfa_required=True)
    if amount_minor >= YELLOW_THRESHOLD_MINOR:
        return Decision(YELLOW, confirm_required=True, mfa_required=False)
    return Decision(GREEN, confirm_required=False, mfa_required=False)


def require_confirmation(
    decision: Decision, *, confirm: bool, mfa_code: str | None, action: str
) -> None:
    """黄色/红色写接口在未确认时先返回 pending_confirmation。

    模拟 MFA 的第一版口径：只校验「非空且全是数字」，不校验具体值——card 与 investment
    会再比对 settings.mock_mfa_code，转账**有意不跟随**：一旦校验具体值，错误验证码会以
    VERIFICATION_FAILED 返回，而 backend/app/modules/transfer/workflow.py:430-441 只处理
    CONFIRMATION_REQUIRED，会把本轮直接判死并丢掉确认卡。
    """
    if not decision.confirm_required:
        return
    if not confirm:
        raise ConfirmationRequiredError(f"{action} 需要 confirm=true 才会执行")
    if decision.mfa_required and not (mfa_code and mfa_code.isdigit()):
        raise ConfirmationRequiredError(f"{action} 需要数字型 mfa_code")
