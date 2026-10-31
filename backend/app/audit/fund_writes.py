"""资金写的业务审计行：成功一行 ok，失败一行 failed，别的事件一行都不落。

`WriteFacade` 对每一次资金写会发四个事件，本模块只认两个：

* `fund_write_success` -> 一行 `ok`：钱动了，记下是谁、动了多少、哪一笔（幂等键）；
* `fund_write_failure` -> 一行 `failed`：银行拒绝了，钱没动，但「试过并且失败」正是
  审计时间线要回答的问题；
* `fund_write_attempt` 与 `daily_total_*` -> 不落行（Ruling R3）。attempt 每次写都发一次、
  重放安全、不带额外信息；日累计是记账内部细节，它自己的 skipped / write_failed 事件留在
  门面的钩子上，不进业务审计表。一次资金写因此只对应一行账。

钩子**绝不抛**。它在银行已经接受这笔写之后才被调用（`write_facade.py:144`），抛出去
等于钱已经转出去了却给用户一个 500。审计写不进去是必须被看见的故障，但看见它的地方是日志
（`fund_write_audit_failed`），不是用户的请求。

身份用 `payload["user_id"]`（应用用户 id，`user_0001`），不是
`payload["bank_customer_id"]`（银行客户号，`u_wealth`）：读者
`AuditReader.business_events` 从 user_id 起查，写错钥匙的行等于没写。客户号只上发
银行、只在对账时用。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.audit.context import AuditContext, current_context
from app.audit.service import BUSINESS_RESULT_FAILED, BUSINESS_RESULT_OK, AuditService
from app.integration.write_facade import WriteAuditHook

logger = logging.getLogger(__name__)

EVENT_SUCCESS = "fund_write_success"
EVENT_FAILURE = "fund_write_failure"
#: 只有这两个事件对应一行账；其余事件进钩子也直接返回。
AUDITED_EVENTS: frozenset[str] = frozenset({EVENT_SUCCESS, EVENT_FAILURE})

RESOURCE_TYPE = "fund_write"
#: `operation` 缺席时的兜底动作名：宁可记一行名字含糊的账，也不丢一行账。
DEFAULT_ACTION = "fund_write"

#: 列宽（`storage/models.py` 的 `business_audit_event`）。超长时裁掉尾巴而不是丢掉
#: 整行：幂等键的尾巴没有信息量，丢一整行账才有。user_id 不裁——裁身份等于记到别人名下。
ACTION_MAX = 64
RESOURCE_ID_MAX = 64
REASON_MAX = 255


def build_fund_write_audit_hook(audit: AuditService) -> WriteAuditHook:
    """Build the hook `WriteFacade` calls for every fund write.

    It lives here rather than inline in `Runtime.__init__` so that tests can build
    the very same hook the process runs -- a hook written twice is a hook whose second copy
    nobody reviewed.
    """

    def hook(event: str, payload: Mapping[str, Any]) -> None:
        if event not in AUDITED_EVENTS:
            return
        try:
            _write(audit, event, payload)
        except Exception as exc:
            # 这里兜底是刻意的：银行已经接受这笔写，审计失败不能让请求失败。
            # 不带异常文本，与 `audit/service.py:290-297` 同一个理由——它可能带着行里的值。
            logger.error(
                "fund_write_audit_failed event=%s action=%s resource_id=%s error=%s",
                event,
                _text(payload.get("operation")),
                _text(payload.get("idempotency_key")),
                type(exc).__name__,
            )

    return hook


def _write(audit: AuditService, event: str, payload: Mapping[str, Any]) -> None:
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        # 没有作者的行读者永远查不到，写它等于没写：宁可留一条日志，也不写一行无主的账。
        raise ValueError("fund write audit payload carries no user_id")
    action = _clip(payload.get("operation"), ACTION_MAX) or DEFAULT_ACTION
    audit.record_business_event(
        action=action,
        resource_type=RESOURCE_TYPE,
        resource_id=_clip(payload.get("idempotency_key"), RESOURCE_ID_MAX),
        result=BUSINESS_RESULT_OK if event == EVENT_SUCCESS else BUSINESS_RESULT_FAILED,
        reason=_reason(event, payload, action),
        # 留住发起这笔写的会话与 run（`merged` 忽略 None），user_id 以 payload 为准。
        context=(current_context() or AuditContext()).merged(user_id=user_id),
    )


def _reason(event: str, payload: Mapping[str, Any], action: str) -> str:
    """One human-readable line: what was attempted, and how much money moved."""

    if event == EVENT_FAILURE:
        return _clip(f"资金操作失败: {_text(payload.get('error_kind')) or 'UNKNOWN'}", REASON_MAX)
    amount = _amount_minor(payload.get("body"))
    if amount is None:
        # 没有金额的写（撤销定时任务）：说清是哪一次写，不编一个 0 分。
        return _clip(f"资金操作成功: {action}", REASON_MAX)
    return _clip(f"资金操作成功: {action} 金额 {amount} 分", REASON_MAX)


def _amount_minor(body: Any) -> int | None:
    """The amount the write moved, or None when the body does not say (never a made-up 0)."""

    if not isinstance(body, Mapping):
        return None
    value = body.get("amount_minor")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _clip(value: Any, limit: int) -> str:
    return _text(value)[:limit]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
