"""账单任务 API 的请求模型。

未知字段一律拒绝（``extra="forbid"``），与基底 ``app.api.schemas.RequestModel`` 一致。
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(RequestModel):
    bank_code: str = Field(min_length=1, max_length=16)
    #: 手动上传可以不填周期：真正的起止由解析结果决定，解析完成后回填。
    #: 「申请流水」那条支路仍然会填 —— 它要告诉用户去银行申请哪一段。
    period_start: date | None = None
    period_end: date | None = None
    # 当前流程固定为手动上传，前端不应发送其它值。
    source_mode: Literal["manual_upload"] = "manual_upload"


class SubmitPasswordRequest(RequestModel):
    # 不设 min_length：空密码应走到服务层返回 PASSWORD_REQUIRED，而不是落到 Pydantic
    # 的统一 INVALID_PARAMETER，前端才能按业务码分支。
    password: str = Field(max_length=200)
