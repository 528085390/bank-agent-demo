"""How a bank_backend failure is classified, and what the user is told about it.

The bank's own `message` is never shown: `BankError.to_payload()` returns the *class*
default (`resource_not_found`), not the reason, so it is both unhelpful and English. Every
kind maps to a Chinese sentence here instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from app.tools.protocol import ErrorCode


class BankErrorKind(StrEnum):
    CONNECT = "CONNECT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER = "SERVER"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    LOCKED = "LOCKED"
    VALIDATION = "VALIDATION"
    CLIENT = "CLIENT"
    PROTOCOL = "PROTOCOL"


# The only failures worth retrying. Everything else is either the user's fault or a state
# the same call would reach again.
RETRYABLE_KINDS = frozenset(
    {
        BankErrorKind.CONNECT,
        BankErrorKind.TIMEOUT,
        BankErrorKind.RATE_LIMITED,
        BankErrorKind.SERVER,
    }
)

# One-to-one with ErrorCode, so a bank failure never loses its meaning on the way out.
# Transient kinds all collapse into UPSTREAM_UNAVAILABLE: by the time the client sees the
# code, `run_tool` has already spent the one retry it is allowed, so "try again" is a lie.
KIND_TO_ERROR_CODE: Mapping[BankErrorKind, str] = {
    BankErrorKind.NOT_FOUND: ErrorCode.NOT_FOUND,
    BankErrorKind.INVALID_STATE: ErrorCode.INVALID_STATE,
    BankErrorKind.INSUFFICIENT_BALANCE: ErrorCode.INSUFFICIENT_BALANCE,
    BankErrorKind.CONFIRMATION_REQUIRED: ErrorCode.CONFIRMATION_REQUIRED,
    BankErrorKind.VERIFICATION_FAILED: ErrorCode.VERIFICATION_FAILED,
    BankErrorKind.LOCKED: ErrorCode.LOCKED,
    BankErrorKind.VALIDATION: ErrorCode.VALIDATION,
    BankErrorKind.CLIENT: ErrorCode.CLIENT,
    BankErrorKind.PROTOCOL: ErrorCode.PROTOCOL,
    BankErrorKind.CONNECT: ErrorCode.UPSTREAM_UNAVAILABLE,
    BankErrorKind.TIMEOUT: ErrorCode.UPSTREAM_UNAVAILABLE,
    BankErrorKind.RATE_LIMITED: ErrorCode.UPSTREAM_UNAVAILABLE,
    BankErrorKind.SERVER: ErrorCode.UPSTREAM_UNAVAILABLE,
}

BANK_ERROR_MESSAGES: dict[BankErrorKind, str] = {
    BankErrorKind.CONNECT: "银行服务暂时无法连接，请稍后重试。",
    BankErrorKind.TIMEOUT: "银行服务响应超时，请稍后重试。",
    BankErrorKind.RATE_LIMITED: "银行服务繁忙，请稍后重试。",
    BankErrorKind.SERVER: "银行服务暂时不可用，请稍后重试。",
    BankErrorKind.NOT_FOUND: "没有找到对应的银行记录。",
    BankErrorKind.INVALID_STATE: "该业务当前状态不允许此操作。",
    BankErrorKind.INSUFFICIENT_BALANCE: "账户余额不足。",
    BankErrorKind.CONFIRMATION_REQUIRED: "该操作需要额外确认。",
    BankErrorKind.VERIFICATION_FAILED: "验证码不正确。",
    BankErrorKind.LOCKED: "该资金工具已锁定，请稍后重试。",
    BankErrorKind.VALIDATION: "请求参数不符合银行服务的要求。",
    BankErrorKind.CLIENT: "银行服务拒绝了这次请求。",
    BankErrorKind.PROTOCOL: "银行服务返回了无法识别的内容。",
}


class BankCallError(Exception):
    """A bank_backend call that did not yield a business result."""

    def __init__(self, kind: BankErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


def message_for(kind: BankErrorKind) -> str:
    """The sentence a user sees. Never the bank's own message."""

    return BANK_ERROR_MESSAGES[kind]
