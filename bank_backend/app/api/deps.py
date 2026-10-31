"""FastAPI 依赖。

数据库引擎进程内缓存；测试通过 dependency_overrides 替换为私有 schema 的引擎。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import UserIdRequiredError
from app.storage.database import Database


@lru_cache(maxsize=1)
def _cached_database() -> Database:
    return Database(get_settings().database_url)


def get_database() -> Database:
    return _cached_database()


def require_user_id(value: str | None) -> str:
    """调用方必须以谁的身份操作。缺了就拒绝，不做默认客户回落。

    上一版这里写着「user_id 缺省为 u_zhang，只做基础存在检查」，等于「不写身份就以演示
    客户的身份读写」。服务凭证只能证明请求来自我们的后端，不能证明它代表哪位用户，
    所以身份必须由调用方明确给出，缺了就是参数错误。归属是否匹配由各领域服务校验。
    """

    resolved = (value or "").strip()
    if not resolved:
        raise UserIdRequiredError()
    return resolved


def resolve_idempotent_key(header_value: str | None, body_value: str | None) -> str | None:
    """幂等键可来自 Idempotency-Key 头或请求体的 idempotent_key，请求体优先。"""
    return body_value or header_value or None
