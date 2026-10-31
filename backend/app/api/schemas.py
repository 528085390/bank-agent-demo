from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeRequest(RequestModel):
    """The answer to a LangGraph interrupt, bound to the question it answers.

    `question_id` names the question the answer belongs to, so an answer meant for an
    earlier question of the same shape cannot be consumed by a later one. It is optional
    only for the transition: runtime fills in the single open question for a client that
    has not been upgraded yet, and the graph still verifies the binding it is handed.

    `value` is deliberately untyped. A string, a list or `null` has to reach the graph,
    where the domain refuses it in its own words and re-asks -- wrapping that in a
    transport error would tell the user nothing about the answer they just gave.
    """

    question_id: str | None = None
    value: Any


class ClientContext(RequestModel):
    """Per-request instructions that are not the message itself.

    `mode="new_goal"` abandons the suspended goal and lets the Supervisor start a new
    one; `resume` answers the pause that is currently waiting. Both are optional, and an
    unknown key is rejected rather than ignored.
    """

    mode: Literal["new_goal"] | None = None
    resume: ResumeRequest | None = None


class ChatMessage(RequestModel):
    session_id: str
    message: str = Field(min_length=1, max_length=4000)
    # Deliberately loose, and parsed by the route instead. FastAPI's validation handler
    # flattens every shape error to `INVALID_PARAMETER`, but a resume that cannot be
    # interpreted is a different failure than a bad parameter and needs its own code.
    client_context: Any | None = None


class ConversationCreate(RequestModel):
    """A new conversation. The client names it; it never names its thread or its owner."""

    title: str | None = Field(default=None, max_length=500)


class ConversationUpdate(RequestModel):
    """A partial update. Every field is optional, and `None` means "leave it alone"."""

    title: str | None = Field(default=None, max_length=500)
    pinned: bool | None = None
    archived: bool | None = None


class ConversationBulkDelete(RequestModel):
    """A batch soft-delete. `all` wipes every live conversation the caller owns and ignores
    `ids`; otherwise only the named ids are touched, and ids that are missing, already deleted
    or not the caller's are ignored rather than rejected."""

    ids: list[str] = Field(default_factory=list, max_length=500)
    all: bool = False


class ConversationMessageCreate(RequestModel):
    """One message submitted to a conversation.

    The same body shape as the legacy chat route, minus `session_id`: the conversation in the
    path is the whole addressing, and `client_context` stays loose for the same reason it is
    loose there.
    """

    message: str = Field(min_length=1, max_length=4000)
    client_context: Any | None = None


class CardPasswordInput(RequestModel):
    old_password: SecretStr | None = Field(default=None, repr=False, max_length=32)
    new_password: SecretStr = Field(repr=False, max_length=32)
    confirm_password: SecretStr = Field(repr=False, max_length=32)


class PendingActionDecision(RequestModel):
    """One click on a confirmation box.

    `decision` is a machine value, never the label the button showed: a client cannot turn
    its own wording into an authorization. `question_id` names the pause the click answers,
    and `pending_id` (in the path) names the pending operation -- both are checked against
    what the server itself recorded, so a click on a preview that has since been replaced is
    refused instead of being applied to the new one.

    `verification_code` is what the user typed into the box the card asked for, and it is
    required exactly when the card said so (`confirmation.requires_code`). It is a secret:
    the route hands it to the turn lifecycle directly, so it is never written to the ledger,
    the checkpoint or an audit row, and it is never part of the request fingerprint.
    """

    question_id: str = Field(min_length=1, max_length=128)
    decision: Literal["confirm", "cancel"]
    verification_code: str | None = Field(default=None, max_length=32)
    transaction_password: CardPasswordInput | None = Field(default=None, repr=False)


class LlmConfigRequest(RequestModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class EmailSettingsRequest(RequestModel):
    email: str = Field(min_length=3, max_length=255)
