"""附件处理器（B 交付物）。

自包含的纯函数包：输入一个附件（或字节流），识别真实类型，按类型解压或提取内容，
输出统一的 ``AttachmentResult`` 供下游解析。

**不 import 基底的任何一层**（agents / tools / api / runtime / main / storage），
也不依赖 LangGraph、工具网关与模型。未来接入 Agent 时只需新增一个 ToolAdapter，
本包一行都不用改（方案 §9）。
"""

from app.billing.contracts import (
    DEFAULT_MAX_DEPTH,
    FORBIDDEN_SUFFIXES,
    KINDS,
    ArchiveLimits,
    AttachmentRequest,
    AttachmentResult,
    ExtractedEntry,
    Kind,
)
from app.billing.errors import (
    ARCHIVE_REJECTED,
    ATTACHMENT_UNSUPPORTED,
    ERROR_CODES,
    INTERNAL_ERROR,
    NESTING_TOO_DEEP,
    PASSWORD_INVALID,
    PASSWORD_REQUIRED,
    BillingError,
)
from app.billing.handlers import process_attachment
from app.billing.sandbox import DEFAULT_SANDBOX_ROOT, TaskSandbox

__all__ = [
    "ARCHIVE_REJECTED",
    "ATTACHMENT_UNSUPPORTED",
    "ArchiveLimits",
    "AttachmentRequest",
    "AttachmentResult",
    "BillingError",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_SANDBOX_ROOT",
    "ERROR_CODES",
    "ExtractedEntry",
    "FORBIDDEN_SUFFIXES",
    "INTERNAL_ERROR",
    "KINDS",
    "Kind",
    "NESTING_TOO_DEEP",
    "PASSWORD_INVALID",
    "PASSWORD_REQUIRED",
    "TaskSandbox",
    "process_attachment",
]
