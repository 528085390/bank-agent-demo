"""请求与响应模型。

请求体字段与 `bank_backend/API接口文档.md` 的 components.schemas 一一对应。
所有响应统一为 {code, message, data} 信封：成功 code=0，失败使用统一错误码。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.timeutil import CST


class ErrorResponse(BaseModel):
    code: int
    message: str
    data: None = None


def envelope(data: Any) -> dict:
    return {"code": 0, "message": "success", "data": data}


class Request(BaseModel):
    """忽略多余字段，允许 API 文档里的示例体直接使用。"""

    model_config = ConfigDict(extra="ignore")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=CST)


class TransferRequest(Request):
    user_id: str | None = None
    from_account_id: str
    to_account_id: str
    amount_minor: int = Field(gt=0, le=100000000)
    remark: str | None = Field(default=None, max_length=200)
    confirm: bool = False
    mfa_code: str | None = None
    idempotent_key: str | None = None


class ScheduledTransferRequest(TransferRequest):
    execute_at: datetime

    @field_validator("execute_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return _aware(value)


class ConfirmRequest(Request):
    confirm: bool = True
    mfa_code: str | None = None


class AaParticipantRequest(Request):
    name: str = Field(min_length=1, max_length=128)
    phone: str | None = None
    account_id: str | None = None


class AaCreateRequest(Request):
    user_id: str | None = None
    from_account_id: str | None = None
    total_amount_minor: int = Field(gt=0)
    participants: list[AaParticipantRequest] = Field(min_length=2)
    remark: str | None = None
    idempotent_key: str | None = None


class AaPayRequest(Request):
    # 付款人必须明说：分摊项扣的是 `participant.account_id` 那个账户的钱，不写身份就等于
    # 「谁都能替别人付」。以前这里没有 user_id，服务端只能回落成发起人，那是越权。
    user_id: str | None = None
    participant_index: int = Field(ge=0)
    confirm: bool


class ClockRequest(Request):
    current_time: datetime

    @field_validator("current_time")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return _aware(value)


class BillAnalyzeRequest(Request):
    user_id: str | None = None
    start_date: date
    end_date: date
    category: str | None = None
    min_amount_minor: int | None = Field(default=None, ge=0)
    keywords: list[str] | None = None


class ProductRecommendRequest(Request):
    user_id: str | None = None
    risk_level: str | None = None
    amount_minor: int | None = Field(default=None, ge=0)


class ProductCompareRequest(Request):
    product_ids: list[str] = Field(min_length=2)


class PurchaseRequest(Request):
    user_id: str | None = None
    from_account_id: str
    product_id: str
    amount_minor: int = Field(gt=0)
    term_months: int | None = Field(default=None, ge=0)
    confirm: bool = False
    mfa_code: str | None = None
    idempotent_key: str | None = None


class RedeemRequest(Request):
    user_id: str | None = None
    holding_id: str
    amount_minor: int = Field(gt=0)
    to_account_id: str | None = None
    confirm: bool = False
    mfa_code: str | None = None
    idempotent_key: str | None = None


class SimulateInterestRequest(Request):
    as_of: date | None = None


class RiskAnswers(BaseModel):
    """五题问卷答案：题号 q1..q5 对应选项 0／1／2。

    `strict=True` 让 bool（`True`/`False`）和字符串都不算合法数值；
    `extra="forbid"` 让客户端不能夹带自造题目。
    """

    model_config = ConfigDict(extra="forbid")

    q1: int = Field(strict=True, ge=0, le=2)
    q2: int = Field(strict=True, ge=0, le=2)
    q3: int = Field(strict=True, ge=0, le=2)
    q4: int = Field(strict=True, ge=0, le=2)
    q5: int = Field(strict=True, ge=0, le=2)


class RiskAssessmentRequest(Request):
    """保存测评请求。

    `extra="forbid"`：最终等级、分数、有效期一律由银行按 wealth-risk-v1 计算，
    客户端提交 `risk_level`、`score` 这类字段会被直接拒绝（1012）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    answers: RiskAnswers


class CardApplyRequest(Request):
    user_id: str | None = None
    account_id: str | None = None
    card_type: Literal["debit", "credit", "virtual"]
    idempotent_key: str | None = None


class CardLimitRequest(Request):
    daily_limit_minor: int = Field(ge=0)
    monthly_limit_minor: int = Field(ge=0)


class CardRestrictionRequest(Request):
    daily_limit_minor: int | None = Field(default=None, ge=0)
    monthly_limit_minor: int | None = Field(default=None, ge=0)
    merchant_blacklist: list[str] | None = None


class SubscriptionRequest(Request):
    user_id: str | None = None
    name: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=128)
    amount_minor: int = Field(gt=0)
    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    from_account_id: str
    start_date: date
    auto_renew: bool = True
    idempotent_key: str | None = None


class BirthdayOrderRequest(Request):
    user_id: str | None = None
    contact_id: str
    product_code: Literal["flowers", "cake"]
    from_account_id: str
    birthday_date: date
    confirm: bool = True
    mfa_code: str | None = None
    idempotent_key: str | None = None


class UserRequest(Request):
    user_id: str | None = None


class SeedResetRequest(Request):
    expected_run_id: str | None = None


class TaskRunRequest(Request):
    birthday_outcome: Literal["success", "fail"] = "success"
