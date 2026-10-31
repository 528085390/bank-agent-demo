"""Former card authorize HTTP — retired in favour of pending-actions decide (A3).

User confirmation for card actions goes only through
`POST /api/conversations/{id}/pending-actions/{pending_id}/decide`, which authorizes at the
bank and resumes the graph in one request. This path remains registered so old clients get
an explicit 410 instead of a silent 404.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError

router = APIRouter(prefix="/api/conversations")


class CardAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1)
    decision: Literal["confirm", "cancel"]
    verification_code: str | None = Field(default=None, max_length=32)


@router.post("/{conversation_id}/card-actions/{action_id}/authorize")
async def authorize_card(
    conversation_id: str, action_id: str, body: CardAuthorization, request: Request
):
    del conversation_id, action_id, body, request
    raise AppError(
        "CARD_AUTHORIZE_GONE",
        "卡片授权已并入确认接口，请使用 pending-actions decide",
        410,
    )
