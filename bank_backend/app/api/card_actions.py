"""Card workflow endpoints, separate from the legacy demonstration mutations."""

from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.deps import get_database
from app.api.schemas import envelope
from app.domain.card_action_service import CardActionService

router = APIRouter(
    tags=["Cards"],
    responses={
        404: {"description": "操作或卡片不存在"},
        409: {"description": "未授权、过期或状态变化"},
    },
)


class PrepareCardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    operation: str
    parameters: dict


class CardActionUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)


class AuthorizeCardAction(CardActionUser):
    decision: Literal["confirm", "cancel"]
    verification_code: str | None = Field(default=None, max_length=32)


class AuthorizeCardPassword(AuthorizeCardAction):
    old_password: SecretStr | None = Field(default=None, repr=False, max_length=32)
    new_password: SecretStr = Field(repr=False, max_length=32)
    confirm_password: SecretStr = Field(repr=False, max_length=32)


@router.post("/cards/actions/{action_id}/password/authorize", operation_id="authorizeCardPassword")
def authorize_password(action_id: str, body: AuthorizeCardPassword, database=Depends(get_database)):
    return envelope(
        CardActionService(database).authorize(
            body.user_id,
            action_id,
            body.decision,
            body.verification_code,
            password_credentials={
                name: getattr(body, name).get_secret_value()
                for name in ("old_password", "new_password", "confirm_password")
                if getattr(body, name) is not None
            },
        )
    )


@router.post("/cards/actions", operation_id="prepareCardAction")
def prepare_action(
    body: PrepareCardAction,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    database=Depends(get_database),
):
    return envelope(
        CardActionService(database).prepare(
            body.user_id, body.operation, body.parameters, idempotency_key
        )
    )


@router.post("/cards/actions/{action_id}/authorize", operation_id="authorizeCardAction")
def authorize_action(action_id: str, body: AuthorizeCardAction, database=Depends(get_database)):
    return envelope(
        CardActionService(database).authorize(
            body.user_id, action_id, body.decision, body.verification_code
        )
    )


@router.post("/cards/actions/{action_id}/execute", operation_id="executeCardAction")
def execute_action(action_id: str, body: CardActionUser, database=Depends(get_database)):
    return envelope(CardActionService(database).execute(body.user_id, action_id))
