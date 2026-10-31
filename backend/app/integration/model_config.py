"""The Chat Completions connection settings the settings panel manages.

Only the connection facts live here: where the provider is, which key and model to use,
and how long to wait. The retired structured plan protocol used to sit on top of this
configuration; the multi-agent runtime keeps the configuration and drops the protocol,
because a Supervisor and its domain Agents decide in free-form JSON, not in a plan schema.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

DEFAULT_API_PATH = "/v1"


def normalize_base_url(value: str) -> str:
    """Canonical Chat Completions base URL without rewriting provider paths.

    A bare origin gains the conventional `/v1`; any explicit path is left alone.
    """

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    path = parsed.path.rstrip("/")
    if not path:
        path = DEFAULT_API_PATH
    return parsed._replace(path=path).geturl()


class ChatCompletionsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(min_length=1, max_length=500)
    api_key: SecretStr = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_base_url(value)
