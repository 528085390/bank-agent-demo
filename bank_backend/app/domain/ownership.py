"""资源归属校验的唯一入口。

为什么单独一个模块：越权在两个地方最容易漏——一是按 ID 取资源时只查「这条记录存不存在」，
忘了查「它是不是你的」；二是漏掉某个端点。把判定压成一个函数后，每个按 ID 读取的路径
都长一个样，review 时扫一眼就能看出谁没走这里。

**不存在的资源和别人的资源回同一句话、同一个 404。** 返回 403 等于告诉探测者「这个 ID
真实存在，只是不归你」，那本身就是信息泄露；404 让「猜到了别人的持仓号」和「瞎编一个
持仓号」得到完全一样的回答。
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError

Model = TypeVar("Model")


def owned(
    session: Session,
    model: type[Model],
    object_id: str,
    user_id: str,
    *,
    label: str,
) -> Model:
    """取出 `object_id`，并确认它属于 `user_id`。"""

    row = session.get(model, object_id)
    if row is None or getattr(row, "user_id", None) != user_id:
        raise NotFoundError(f"{label}不存在: {object_id}")
    return row


def owned_or_none(
    session: Session,
    model: type[Model],
    object_id: str,
    user_id: str,
) -> Model | None:
    """与 `owned` 相同，但把「不是你」折成 None，供需要自己决定语义的调用方使用。"""

    row = session.get(model, object_id)
    if row is None or getattr(row, "user_id", None) != user_id:
        return None
    return row
