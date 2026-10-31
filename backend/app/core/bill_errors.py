"""账单域的 API 错误码与 HTTP 状态映射。

统一用基底的 ``app.core.errors.AppError`` 抛出（它已有异常处理器与响应信封），
这里只集中定义账单域错误码，避免服务层散落魔法字符串。错误码取值与前端（D）契约
对齐，见《任务方案-A》§A1-2。
"""

# 通用
VALIDATION = "VALIDATION"
NOT_FOUND = "NOT_FOUND"
PERMISSION_DENIED = "PERMISSION_DENIED"

# 任务状态
INVALID_TASK_STATE = "INVALID_TASK_STATE"
TASK_CANCELLED = "TASK_CANCELLED"

# 文件
FILE_TOO_LARGE = "FILE_TOO_LARGE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"

# 密码
PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
PASSWORD_INVALID = "PASSWORD_INVALID"
PASSWORD_ATTEMPTS_EXCEEDED = "PASSWORD_ATTEMPTS_EXCEEDED"

# 解析与入库
PARSE_FAILED = "PARSE_FAILED"
DUPLICATE_IMPORT = "DUPLICATE_IMPORT"

#: 错误码 → HTTP 状态码。
STATUS: dict[str, int] = {
    VALIDATION: 422,
    NOT_FOUND: 404,
    PERMISSION_DENIED: 403,
    INVALID_TASK_STATE: 409,
    TASK_CANCELLED: 409,
    FILE_TOO_LARGE: 413,
    FILE_NOT_FOUND: 404,
    UNSUPPORTED_FILE_TYPE: 422,
    PASSWORD_REQUIRED: 422,
    PASSWORD_INVALID: 422,
    PASSWORD_ATTEMPTS_EXCEEDED: 429,
    PARSE_FAILED: 422,
    DUPLICATE_IMPORT: 409,
}


def status_of(code: str) -> int:
    return STATUS.get(code, 409)


def raise_bill_error(code: str, message: str, *, retryable: bool = False, details=None):
    from app.core.errors import AppError

    raise AppError(code, message, status_of(code), retryable=retryable, details=details)
