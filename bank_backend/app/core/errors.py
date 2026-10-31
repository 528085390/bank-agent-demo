"""统一业务错误。

错误码与 `bank_backend/API接口文档.md` 的 ErrorResponse 示例保持一致：
1004 resource_not_found、1005 invalid_state、1012 validation_error。
其余码是本 mock 在同类语义下的细分，HTTP 状态与文档一致。
"""

from __future__ import annotations


class BankError(Exception):
    """所有可预期业务失败的基类。"""

    code = 1005
    http_status = 409
    message = "invalid_state"

    def __init__(self, reason: str | None = None, *, message: str | None = None):
        self.reason = reason or message or self.message
        super().__init__(self.reason)

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "data": None}


class NotFoundError(BankError):
    code = 1004
    http_status = 404
    message = "resource_not_found"


class BusinessError(BankError):
    code = 1005
    http_status = 409
    message = "invalid_state"


class InsufficientBalanceError(BankError):
    code = 1006
    http_status = 409
    message = "insufficient_balance"


class ConfirmationRequiredError(BankError):
    """黄色/红色写接口在未确认时先返回待确认状态。"""

    code = 1007
    http_status = 409
    message = "pending_confirmation"


class VerificationFailedError(ConfirmationRequiredError):
    """模拟验证码明确错误。

    与“还没确认/没带码”区分开：后者是正常流程不计数，前者要留审计并计入资金工具的
    失败计数（见 `security_service.record_failure`）。
    """

    code = 1008
    http_status = 409
    message = "验证码不正确"


class LockedError(BankError):
    """验证码连续错误后资金工具被锁定。"""

    code = 1009
    http_status = 409
    message = "locked"


class ValidationError(BankError):
    code = 1012
    http_status = 422
    message = "validation_error"


class ServiceUnauthorizedError(BankError):
    """没有服务凭证，或凭证不对。

    这不是用户身份问题，是「这个请求根本不是我们的后端发的」。文案与 401 一致，不区分
    「没带」和「带错了」——区分只会给探测者一个信号。
    """

    code = 1001
    http_status = 401
    message = "service_token_required"


class OpsUnauthorizedError(BankError):
    """服务凭证对了，但清库/推时钟这类全局动作缺运维口令。"""

    code = 1002
    http_status = 403
    message = "ops_token_required"


class UserIdRequiredError(BankError):
    """调用方没有说明它以谁的身份操作。

    以前这里会回落到默认客户，等于「不写身份就以演示客户的身份读写」。多用户下这是
    直接越权，所以缺身份必须是硬失败。
    """

    code = 1013
    http_status = 422
    message = "user_id_required"
