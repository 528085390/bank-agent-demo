"""The interrupt payload <-> HTTP adaptation for the pending-action route.

`app.api.routes` owns `POST /conversations/{conversation_id}/pending-actions/{pending_id}/decide`.
What belongs to the interrupt/HTTP seam rather than to HTTP itself lives here: recognising a
bank card action, reading whether the open pause asks for a verification code, and
authorizing the card action at the bank before the turn resumes. The route stays a thin
caller.
"""

import re
from dataclasses import dataclass
from typing import Literal

from starlette.concurrency import run_in_threadpool

from app.agents.turn import CONFIRMATION_KIND
from app.core.errors import AppError
from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError

# Bank card action ids (`CA-` + 32 hex). Used to recognise a card confirmation so decide can
# authorize at the bank before resuming the graph — the only user-facing confirmation entry.
_CARD_ACTION_ID = re.compile(r"^CA-[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class CardAuthorizationOutcome:
    """这一次点击在银行侧的结果。cancelled 表示银行已把该 action 作废。"""

    status: Literal["none", "authorized", "cancelled"]
    remaining: int | None = None


async def _authorize_card_action_if_needed(
    rt,
    who: dict,
    pending: dict | None,
    decision: str,
    verification_code: str | None,
    *,
    transaction_password=None,
) -> CardAuthorizationOutcome:
    """Authorize a prepared card action at the bank when this click confirms one.

    Cancel is left to the card graph (`card_cancel`). Confirm must hit the bank authorize
    endpoint here so execute on resume finds an authorized record — and so a wrong code is
    refused before the turn lifecycle writes anything. The code is handed to the bank only;
    it never enters the resume value, the fingerprint, or the ledger.
    """

    if decision != "confirm":
        return CardAuthorizationOutcome(status="none")
    confirmation = (pending or {}).get("confirmation")
    if not isinstance(confirmation, dict):
        return CardAuthorizationOutcome(status="none")
    action_id = confirmation.get("pending_id")
    if not isinstance(action_id, str) or not _CARD_ACTION_ID.fullmatch(action_id):
        return CardAuthorizationOutcome(status="none")
    customer = await run_in_threadpool(rt.identity.bank_customer_id, who["user_id"])
    if not customer:
        raise AppError("NO_CUSTOMER_BINDING", "当前用户未绑定银行客户号", 409)
    password_action = confirmation.get("operation") in ("apply", "change_password")
    if password_action and transaction_password is None:
        raise AppError("INVALID_PARAMETER", "请填写独立的交易密码表单", 422)
    if transaction_password is not None and not password_action:
        raise AppError("INVALID_PARAMETER", "当前待办不接受交易密码", 422)
    payload = {"decision": "confirm", "verification_code": verification_code}
    if password_action:
        payload.update(
            {
                name: getattr(transaction_password, name).get_secret_value()
                for name in ("old_password", "new_password", "confirm_password")
                if getattr(transaction_password, name) is not None
            }
        )
    try:
        result = await run_in_threadpool(
            rt.bank_client.request,
            "POST",
            f"/cards/actions/{action_id}/password/authorize"
            if password_action
            else f"/cards/actions/{action_id}/authorize",
            user_id=customer,
            json=payload,
        )
    except BankCallError as error:
        raise AppError(
            KIND_TO_ERROR_CODE[error.kind], error.message, 409, retryable=error.retryable
        ) from None
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("status") == "cancelled":
        # 银行已经把这次操作作废（连错达到上限）。这不是可重试的验证失败。
        return CardAuthorizationOutcome(status="cancelled", remaining=0)
    if data.get("verification_passed", True):
        return CardAuthorizationOutcome(status="authorized")
    # Wrong code: keep the pause; do not start the turn.
    failures = int(data.get("verification_failures") or 0)
    remaining = int(data.get("remaining_attempts", max(0, 3 - failures)))
    reason = {
        "old_password": "旧交易密码不正确",
        "verification_code": "验证码不正确",
        "locked": "该卡交易密码验证暂时锁定，请十五分钟后重试",
    }.get(data.get("credential_error"), "验证未通过")
    raise AppError(
        "VERIFICATION_FAILED",
        f"{reason}，还可尝试 {remaining} 次。",
        422,
        details={
            "verification_failures": failures,
            "question_id": (pending or {}).get("question_id"),
            "status": data.get("status"),
        },
    )


def _requires_code(pending: dict | None) -> bool:
    """Whether the open card asks for a verification code.

    The answer comes from the module's own projection, which the server wrote into the pause:
    the route does not decide policy, it reads what the module said the bank requires.
    """

    confirmation = (pending or {}).get("confirmation")
    return bool(isinstance(confirmation, dict) and confirmation.get("requires_code"))


def _click_matches(pending: dict | None, question_id: str, pending_id: str) -> bool:
    """Whether the open pause is exactly the one this click names.

    Both halves are required. `question_id` binds the click to the pause the server recorded;
    `pending_id` binds it to the operation that pause was built from, so a click on a preview
    that has since been replaced cannot be applied to whatever is open now.
    """

    if not isinstance(pending, dict) or pending.get("kind") != CONFIRMATION_KIND:
        return False
    confirmation = pending.get("confirmation")
    return (
        pending.get("question_id") == question_id
        and isinstance(confirmation, dict)
        and confirmation.get("pending_id") == pending_id
    )
