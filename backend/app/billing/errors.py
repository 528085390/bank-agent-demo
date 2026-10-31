"""附件处理器的错误码与异常。

本模块只依赖标准库，不 import 框架的任何一层（agents / tools / api / runtime / storage）。
"""

from __future__ import annotations

from typing import Any

# 错误码。取值来自方案 §4.4，不在本模块内新增同义词。
PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
PASSWORD_INVALID = "PASSWORD_INVALID"
ATTACHMENT_UNSUPPORTED = "ATTACHMENT_UNSUPPORTED"
ARCHIVE_REJECTED = "ARCHIVE_REJECTED"
NESTING_TOO_DEEP = "NESTING_TOO_DEEP"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: 调用方可以原样重试的错误码；其余重试需要换输入。
RETRYABLE_CODES: frozenset[str] = frozenset({PASSWORD_REQUIRED, PASSWORD_INVALID})

ERROR_CODES: frozenset[str] = frozenset(
    {
        PASSWORD_REQUIRED,
        PASSWORD_INVALID,
        ATTACHMENT_UNSUPPORTED,
        ARCHIVE_REJECTED,
        NESTING_TOO_DEEP,
        INTERNAL_ERROR,
    }
)


class BillingError(Exception):
    """带错误码的处理失败。

    ``message`` 是给调用方展示的说明，**永远不得包含密码明文**：密码只以
    ``AttachmentRequest.password`` 的形式在本模块内部传递，不参与任何消息拼接。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"未知的错误码：{code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def password_required() -> BillingError:
    return BillingError(PASSWORD_REQUIRED, "附件已加密，需要提供解压密码")


def password_invalid(attempts_left: int | None = None) -> BillingError:
    details = {} if attempts_left is None else {"attempts_left": attempts_left}
    return BillingError(PASSWORD_INVALID, "解压密码错误", details=details)


def unsupported(message: str) -> BillingError:
    return BillingError(ATTACHMENT_UNSUPPORTED, message)


def missing_dependency(package: str, purpose: str) -> BillingError:
    """依赖缺失时的统一报错：明确写出安装命令，且不影响其他格式。"""

    return BillingError(
        ATTACHMENT_UNSUPPORTED,
        f"{purpose}需要安装 {package}（uv add {package}）",
    )


def archive_rejected(reason: str) -> BillingError:
    return BillingError(ARCHIVE_REJECTED, f"压缩包安全检查未通过：{reason}")


def nesting_too_deep(depth: int) -> BillingError:
    return BillingError(NESTING_TOO_DEEP, f"压缩包嵌套层数超过上限（{depth} 层）")
