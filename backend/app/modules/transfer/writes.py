"""转账模块自己的受控写路径。

网关的调用契约是 `ToolHandler(invocation, client)`，没有验证码通道；而 `POST /transfers`
在红档金额上必须带用户当场输入的验证码，所以这一次写走模块自己的路径 —— 用图的
`graph_builder` 交进来的银行客户端，和挂卡时冻结的那把幂等键。别的代码不许调这里。

这一次写仍经 `WriteFacade` 的门面策略（身份、幂等、当日限额复检、审计脱敏）。当日限额在
挂卡时判过一次，用户可能过一会儿才点确认，所以门面在执行前用同一个阈值再判一次：那时没带
验证码、而当日累计已经越线，就把这笔挡回 `CONFIRMATION_REQUIRED`。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.integration.bank_errors import KIND_TO_ERROR_CODE, BankCallError, message_for
from app.integration.write_facade import WriteFacade, WritePolicyError
from app.modules.base import ERROR_MESSAGES, ToolFailure
from app.tools.protocol import ErrorCode, TrustedContext


def _policy_failure(exc: WritePolicyError) -> ToolFailure:
    """A facade policy stop, in the vocabulary the module and the client already speak."""

    return ToolFailure(exc.code, ERROR_MESSAGES[exc.code])


def _bank_failure(failure: BankCallError) -> ToolFailure:
    """The bank's kind, in the vocabulary the module and the client already speak."""

    return ToolFailure(
        KIND_TO_ERROR_CODE[failure.kind], message_for(failure.kind), retryable=failure.retryable
    )


def submit_transfer(
    client,
    context: TrustedContext,
    intent: Mapping,
    pending: Mapping,
    *,
    verification_code: str | None,
    writes: WriteFacade | None = None,
):
    """点击「确认」之后唯一一次银行写。intent 与 pending 都是挂卡时冻结的。"""

    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        # 银行只接受数字码（risk.require_confirmation）；提前拒并给中文提示，银行仍以 1007 兜底。
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")
    writes = writes or WriteFacade(client)
    key = pending.get("idempotency_key")
    try:
        return writes.send(
            "POST",
            "/transfers",
            context=context,
            json={
                "from_account_id": intent["from_account_id"],
                "to_account_id": intent["to_account_id"],
                "amount_minor": intent["amount_minor"],
                "remark": intent.get("remark"),
                "confirm": True,
                "mfa_code": code,
                "idempotent_key": key,
            },
            idempotency_key=key,
            operation="transfer_submit",
            # `operation` 只记「哪一次写」进审计；`amount_minor` 同时是当日累计的信号 ——
            # 只有真的动了钱（正数金额）的写才记进日累计，转账正是这种写。
            amount_minor=intent["amount_minor"],
            # 门面用它判断「当日限额已经用码回答过了」；码本身仍只进请求体，门面不留存。
            verification_code=code,
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f


def create_scheduled(
    client,
    context: TrustedContext,
    intent: Mapping,
    pending: Mapping,
    *,
    verification_code: str | None,
    writes: WriteFacade | None = None,
):
    """点击「确认」之后创建一笔定时转账。

    这是银行的任务单写入，不是资金移动：execute_at 之前不会扣款。因此门面不传
    amount_minor —— 未来金额不能算进今天的日累计，也不能在今天触发限额复检；真正
    执行时由银行任务执行器按当时的规则处理。确认与验证码仍由本次点击提供。
    """

    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")
    execute_at = intent.get("execute_at")
    if not isinstance(execute_at, str) or not execute_at.strip():
        raise ToolFailure(ErrorCode.VALIDATION, "定时转账缺少执行时间，请重新说明。")
    writes = writes or WriteFacade(client)
    key = pending.get("idempotency_key")
    try:
        return writes.send(
            "POST",
            "/transfers/scheduled",
            context=context,
            json={
                "from_account_id": intent["from_account_id"],
                "to_account_id": intent["to_account_id"],
                "amount_minor": intent["amount_minor"],
                "execute_at": execute_at,
                "remark": intent.get("remark"),
                "confirm": True,
                "mfa_code": code,
                "idempotent_key": key,
            },
            idempotency_key=key,
            operation="schedule_transfer",
            amount_minor=None,
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f


def cancel_scheduled(
    client,
    context: TrustedContext,
    task_id: str,
    *,
    idempotency_key: str | None,
    writes: WriteFacade | None = None,
):
    """取消一笔定时转账：银行侧本来就不要 confirm/MFA。"""

    writes = writes or WriteFacade(client)
    try:
        return writes.send(
            "POST",
            f"/transfers/scheduled/{task_id}/cancel",
            context=context,
            json={},
            idempotency_key=idempotency_key,
            operation="cancel_scheduled_transfer",
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f


def create_aa(
    client,
    context: TrustedContext,
    intent: Mapping,
    pending: Mapping,
    *,
    verification_code: str | None,
    writes: WriteFacade | None = None,
):
    """点击「确认」之后创建一笔 AA 收款请求。

    这是收款请求的写入，不是我的资金移动：钱是参与者向内支付，发起时一分不出。
    因此门面不传 amount_minor —— 它不占当日累计，也不触发限额复检；每位参与者
    支付自己份额时（`pay_aa`）才是真资金写。确认与验证码仍由本次点击提供。
    """

    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")
    writes = writes or WriteFacade(client)
    key = pending.get("idempotency_key")
    try:
        return writes.send(
            "POST",
            "/transfers/aa",
            context=context,
            json={
                "from_account_id": intent["from_account_id"],
                "total_amount_minor": intent["total_amount_minor"],
                "participants": intent["participants"],
                "remark": intent.get("remark"),
                "confirm": True,
                "mfa_code": code,
                "idempotent_key": key,
            },
            idempotency_key=key,
            operation="aa_collect",
            amount_minor=None,
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f


def pay_aa(
    client,
    context: TrustedContext,
    intent: Mapping,
    pending: Mapping,
    *,
    verification_code: str | None,
    writes: WriteFacade | None = None,
):
    """点击「确认」之后支付我在一笔 AA 收款里的那一份。

    真实资金写：amount_minor 是我的分摊额，点击时限额复检、成功后记当日累计，
    与即时转账同一套口径。银行按冻结的参与者份额扣款（请求体不带金额），
    mfa_code 银行侧不校验，但照送以保持审计脱敏口径一致。
    """

    code = (verification_code or "").strip() or None
    if code is not None and not code.isdigit():
        # 银行不校验这个码；这道前置校验保护的是门面的「有码即视为已回答限额」语义。
        raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, "验证码只能是数字。")
    if not isinstance(intent.get("participant_index"), int) or isinstance(
        intent.get("participant_index"), bool
    ):
        raise ToolFailure(ErrorCode.VALIDATION, "AA 分摊支付缺少参与者序号，请重新说明。")
    writes = writes or WriteFacade(client)
    key = pending.get("idempotency_key")
    try:
        return writes.send(
            "POST",
            f"/transfers/aa/{intent['aa_request_id']}/pay",
            context=context,
            json={
                "participant_index": intent["participant_index"],
                "confirm": True,
                "mfa_code": code,
                "idempotent_key": key,
            },
            idempotency_key=key,
            operation="aa_payment",
            amount_minor=intent["amount_minor"],
            verification_code=code,
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f


def cancel_aa(
    client,
    context: TrustedContext,
    aa_request_id: str,
    *,
    idempotency_key: str | None,
    writes: WriteFacade | None = None,
):
    """取消一笔 AA 收款：银行侧本来就不要 confirm/MFA，只有发起人能取消。"""

    writes = writes or WriteFacade(client)
    try:
        return writes.send(
            "POST",
            f"/transfers/aa/{aa_request_id}/cancel",
            context=context,
            json={},
            idempotency_key=idempotency_key,
            operation="aa_cancel",
        )
    except WritePolicyError as e:
        raise _policy_failure(e) from e
    except BankCallError as f:
        raise _bank_failure(f) from f
