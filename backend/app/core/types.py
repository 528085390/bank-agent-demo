import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

MONEY_PATTERN = r"^(0|[1-9][0-9]{0,15})(\.[0-9]{1,2})?(?![\s\S])"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(MONEY_PATTERN, value):
        raise ValueError("金额必须是最多两位小数的非负十进制字符串")
    return Decimal(value).quantize(Decimal("0.01"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value
