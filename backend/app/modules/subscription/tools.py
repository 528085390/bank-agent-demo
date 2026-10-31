"""Subscription tools: the caller's own subscriptions, straight from the bank.

Two bank facts live here so the graph and the tests read one shape: the record's primary key
is `subscription_id`, and `GET /subscriptions` answers a bare array rather than an envelope.

The four write tools are snapshot-and-confirm: `prepare` reads the subscription, freezes a
preview, and hands it back as a fact. The only tool that calls a bank write endpoint is the
hidden `execute`, and only after the user clicks the confirmation card.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.clock import SystemClock
from app.integration.write_facade import WritePolicyError
from app.modules.base import ERROR_MESSAGES, ToolData, ToolFailure
from app.modules.money import format_minor
from app.policy.evaluator import classify_operation
from app.policy.types import DAILY_RED_THRESHOLD_MINOR
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, SideEffect, ToolSpec

logger = logging.getLogger(__name__)

# A charge that would cross the day's cap is refused at the card and again at the facade. The
# wording is shared by both refusals: the user sees one reason, not two vocabularies.
CHARGE_OVER_CAP_MESSAGE = (
    "这笔订阅扣款会使当日累计超过 ¥1,000.00 的限额，今日无法完成，请明天再试。"
)

# What a status or cadence looks like to a user. An unknown value is shown as itself.
SUBSCRIPTION_STATUS = {"active": "生效中", "paused": "已暂停", "cancelled": "已取消"}
FREQUENCY_LABELS = {"daily": "每日", "weekly": "每周", "monthly": "每月", "yearly": "每年"}

# The four write endpoints, exactly as they are spelled in the path after /subscriptions/{id}/.
OPERATIONS = ("cancel", "pause", "resume", "charge")

STATUS_PROPERTY = {
    "type": "string",
    "enum": sorted(SUBSCRIPTION_STATUS),
    "description": "只看某个状态的订阅",
}
SUBSCRIPTION_ID_PROPERTY = {
    "type": "string",
    "description": "订阅 ID；订阅列表不返回它，定位某一笔请优先用 name 或 merchant_name",
}
# The write path is not the read path: `matched_subscriptions` resolves on the id and the
# merchant name only and ignores `name`, so a write tool must not borrow the read tool's
# description and send the model to a selector the graph will refuse.
WRITE_SUBSCRIPTION_ID_PROPERTY = {
    "type": "string",
    "description": "订阅 ID，来自用户明确给出的编号",
}
SUBSCRIPTION_NAME_PROPERTY = {
    "type": "string",
    "description": "订阅名称，来自查询结果里的 name",
}
MERCHANT_NAME_PROPERTY = {
    "type": "string",
    "description": "商户名称，来自查询结果里的 merchant_name",
}

# The four write tools share one selector set: the id when the model knows it, or the merchant
# name so the graph can resolve a subscription the user named without an id.
WRITE_PROPERTIES = {
    "subscription_id": WRITE_SUBSCRIPTION_ID_PROPERTY,
    "merchant_name": MERCHANT_NAME_PROPERTY,
}


def customer(invocation):
    value = invocation.context.bank_customer_id
    if not value:
        raise ToolFailure(
            ErrorCode.NO_CUSTOMER_BINDING, ERROR_MESSAGES[ErrorCode.NO_CUSTOMER_BINDING]
        )
    return value


def subscription_label(row):
    merchant = row.get("merchant_name") or row.get("name") or "未知商户"
    return f"{row.get('name') or merchant}（{merchant}）"


def subscription_text(row):
    status = SUBSCRIPTION_STATUS.get(row.get("status")) or row.get("status") or "未知状态"
    frequency = FREQUENCY_LABELS.get(row.get("frequency")) or row.get("frequency") or "未知频率"
    renew = "自动续费" if row.get("auto_renew") else "手动续费"
    due = row.get("next_due_date") or "未安排"
    return (
        f"{subscription_label(row)}：{status}，{frequency}扣款 {format_minor(row['amount_minor'])}，"
        f"下次扣款 {due}，{renew}。"
    )


def load_subscriptions(invocation, client):
    """The caller's subscriptions. The bank answers a bare array, so anything else is a bug."""

    data = client.request("GET", "/subscriptions", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "订阅列表响应不正确。")
    return data


def subscriptions(invocation, client):
    rows = load_subscriptions(invocation, client)
    status = invocation.call.arguments.get("status")
    if status:
        rows = [row for row in rows if row.get("status") == status]
    summary = "您目前没有订阅。" if not rows else "\n".join(subscription_text(row) for row in rows)
    return ToolData(summary=summary, display={"blocks": []}, facts={"subscriptions": rows})


def detail(invocation, client):
    """The one subscription the caller named, by id, display name or merchant name.

    Every selector the caller gave has to hit the same row, so a conflicting pair matches
    nothing. A missing selector does not filter, so no selector at all matches every row and
    the 0 / 1 / many decision falls out of the same two refusals a write tool raises.
    """

    arguments = invocation.call.arguments
    wanted = {
        field: arguments[field]
        for field in ("subscription_id", "name", "merchant_name")
        if arguments.get(field) is not None
    }
    matches = [
        row
        for row in load_subscriptions(invocation, client)
        if all(row.get(field) == value for field, value in wanted.items())
    ]
    if not matches:
        raise ToolFailure(ErrorCode.NOT_FOUND, "没有找到对应的订阅。")
    if len(matches) > 1:
        # A display name or a merchant name can legitimately name two rows, and a duplicated
        # `subscription_id` means a corrupt response. Either way the choice is the user's, so
        # it is the same validation refusal card raises for its ambiguous selector.
        raise ToolFailure(ErrorCode.VALIDATION, "请明确选择一笔本人订阅。")
    return ToolData(
        summary=subscription_text(matches[0]),
        display={"blocks": []},
        facts={"subscription": matches[0]},
    )


def matched_subscriptions(rows, arguments):
    """Every row the arguments select, in bank order.

    Mirrors `card.tools.matched_cards`: it never collapses to one row and never raises, because
    the 0 / 1 / many decision belongs to the graph, which can park on a selection instead of
    guessing. A missing selector matches every row.
    """

    wanted_id = arguments.get("subscription_id")
    wanted_merchant = arguments.get("merchant_name")
    return [
        row
        for row in rows
        if (not wanted_id or row.get("subscription_id") == wanted_id)
        and (not wanted_merchant or row.get("merchant_name") == wanted_merchant)
    ]


def _select_subscription(invocation, client):
    """The one subscription a write tool will act on, read fresh from the bank.

    The graph has already resolved the 0 / 1 / many choice before a write tool runs; this is
    the direct-handler fallback, so it refuses an ambiguous request rather than guessing. Every
    selector the caller passed is checked against the same row, so a conflicting pair matches
    nothing rather than acting on the id alone.
    """

    rows = load_subscriptions(invocation, client)
    matches = matched_subscriptions(rows, invocation.call.arguments)
    if not matches:
        raise ToolFailure(ErrorCode.NOT_FOUND, "没有找到对应的订阅。")
    if len(matches) > 1:
        raise ToolFailure(ErrorCode.VALIDATION, "请明确选择一笔本人订阅。")
    return matches[0]


def _amount_minor(row):
    """The amount in minor units. A non-positive or non-integer amount cannot be confirmed."""

    amount = row.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ToolFailure(ErrorCode.VALIDATION, "订阅金额不正确，无法办理该操作。")
    return amount


def _daily_accumulated_minor(invocation, database):
    """The day's fund-write total, or 0 when no database is bound.

    A failed read counts as already over the cap (fail closed), the same call `transfer_prepare`
    makes: the card is the only place a charge is judged, so an unknown total must not turn into
    "probably fine". A read that fails is worth a warning, never a traceback in the chat.
    """

    if database is None:
        return 0
    try:
        with database.sessions() as session:
            return DailyTotalStore.get_accumulated_minor(
                session, invocation.context.user_id, SystemClock.today()
            )
    except Exception:
        logger.warning(
            "subscription_daily_total_read_failed user_id=%s", invocation.context.user_id
        )
        return DAILY_RED_THRESHOLD_MINOR + 1


def prepare(invocation, client, *, database=None):
    """Freeze one subscription operation into a snapshot and ask the user to confirm it.

    Reading is `GET /subscriptions`, so a write tool's recorded requests stay read-only. The
    snapshot carries everything a later execute step needs, including the wall-clock deadline.

    A charge is a fund write, so its card is judged on the day's total as well: `database` is the
    composition root's database, read the same way `transfer_prepare` reads it. A charge that
    would cross the cap is refused here and no snapshot is frozen -- a subscription has no
    verification-code channel, so a card that could never be clicked through is worse than a
    refusal. Without a database the total is unknown, so the card keeps the amount-only verdict
    and stays clickable. Cancel/pause/resume never read the total: they are not fund writes.
    """

    # Imported at call time, not module scope: the graph is the module that owns
    # PENDING_EXPIRY_MINUTES and imports selectors from here, so importing back would cycle.
    from app.modules.subscription.workflow import PENDING_EXPIRY_MINUTES

    operation = invocation.call.tool_name.removeprefix("subscription_")
    row = _select_subscription(invocation, client)
    amount_minor = _amount_minor(row)
    pending_id = f"sub_{uuid4().hex}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=PENDING_EXPIRY_MINUTES)
    ).isoformat()
    action = {
        "pending_id": pending_id,
        "operation": operation,
        "subscription_id": row.get("subscription_id"),
        "product_name": row.get("name") or row.get("merchant_name") or "该订阅",
        "merchant_name": row.get("merchant_name") or "",
        "amount_minor": amount_minor,
        "account_display": row.get("from_account_id") or "未指定账户",
        "frequency": row.get("frequency"),
        "next_due_date": row.get("next_due_date"),
        "expires_at": expires_at,
        "idempotency_key": invocation.call.idempotency_key or pending_id,
    }
    if operation == "charge":
        # A charge is a fund write, so the card is judged on the day's total too. A red charge is
        # refused instead of frozen: there is no code channel to clear it (build_subscription_graph
        # ignores the turn and execute never sends a code), so such a card could only fail at the
        # facade after the user clicked.
        #
        # Only a bound module can refuse. Without a database the day's total is unknowable, so the
        # verdict is the pre-C2b amount-only one and the card stays clickable: refusing there would
        # guess at a total nobody read. The assembled runtime always binds `database`.
        accumulated_minor = _daily_accumulated_minor(invocation, database)
        decision = classify_operation(
            f"subscription_{operation}",
            amount_minor=amount_minor,
            daily_accumulated_minor=accumulated_minor,
        )
        if database is not None and decision.mfa_required:
            raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, CHARGE_OVER_CAP_MESSAGE)
        action["risk_level"] = decision.level.value
        action["reason"] = decision.reason
    return ToolData(
        summary="操作详情已准备，尚未修改订阅，等待用户确认。",
        display={"blocks": []},
        facts={"action": action},
    )


def _send_charge(invocation, path, arguments, writes):
    """Send one frozen charge through the facade, on the amount the card froze.

    The frozen amount is the only amount used: re-reading it from the bank would let the charge
    differ from the card the user confirmed. A missing or non-positive amount is refused rather
    than repaired. `writes is None` means the module was assembled without its binder, and a
    charge without the facade would be exactly the bypass this path exists to close.
    """

    amount_minor = arguments.get("amount_minor")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ToolFailure(ErrorCode.VALIDATION, "扣款金额缺失，无法办理该操作。")
    if writes is None:
        raise ToolFailure(ErrorCode.INTERNAL, "扣款通道未就绪，请稍后再试。")
    try:
        return writes.send(
            "POST",
            path,
            context=invocation.context,
            json={},
            idempotency_key=invocation.call.idempotency_key,
            operation="subscription_charge",
            amount_minor=amount_minor,
        ).data
    except WritePolicyError as policy:
        # The facade's cap stop is the same refusal the card gives, so it keeps the same wording.
        if policy.code == ErrorCode.CONFIRMATION_REQUIRED:
            raise ToolFailure(ErrorCode.CONFIRMATION_REQUIRED, CHARGE_OVER_CAP_MESSAGE) from policy
        raise ToolFailure(policy.code, ERROR_MESSAGES[policy.code]) from policy


def execute(invocation, client, *, writes=None):
    """Carry out the operation a frozen snapshot describes, on the key that snapshot froze.

    The bank takes the subscription id in the path and ignores `user_id`; the key travels in
    the `Idempotency-Key` header, so a retried click replays instead of charging twice.

    A charge is a fund write, so it goes through the facade: the day's cap is re-checked right
    before the bank call, and the daily total and the business audit row are recorded only after
    the bank accepts. Cancel/pause/resume move no money, so they still call the bank directly and
    never enter the daily total.
    """

    arguments = invocation.call.arguments
    path = f"/subscriptions/{arguments['subscription_id']}/{arguments['operation']}"
    if arguments["operation"] == "charge":
        data = _send_charge(invocation, path, arguments, writes)
    else:
        data = client.request(
            "POST",
            path,
            user_id=customer(invocation),
            json={},
            idempotency_key=invocation.call.idempotency_key,
        ).data
    return ToolData(
        summary=f"订阅操作已完成。{subscription_text(data)}",
        display={"blocks": []},
        facts={"result": data},
        business_ids={
            "subscription_id": data.get("subscription_id") or arguments["subscription_id"]
        },
    )


def spec(
    name, description, properties=None, required=(), *, write=False, output="action", **constraints
):
    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
        **constraints,
    }
    return ToolSpec(
        name=f"subscription_{name}",
        description=f"{description}。参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}",
        parameters=schema,
        side_effect=SideEffect.WRITE if write else SideEffect.READ,
        output_schema={
            "type": "object",
            "properties": {output: {"type": "array" if output == "subscriptions" else "object"}},
            "required": [output],
            "additionalProperties": False,
        },
    )


# Two read tools answer questions; four write tools freeze an operation for confirmation, and
# the hidden execute tool carries out the click. Only `execute` reaches a bank write endpoint.
SPECS = [
    spec(
        "list",
        "查询本人订阅、状态、扣款频率和下次扣款日",
        {"status": STATUS_PROPERTY},
        output="subscriptions",
    ),
    spec(
        "detail",
        "查询指定订阅的详情",
        {
            "subscription_id": SUBSCRIPTION_ID_PROPERTY,
            "name": SUBSCRIPTION_NAME_PROPERTY,
            "merchant_name": MERCHANT_NAME_PROPERTY,
        },
        output="subscription",
    ),
    # The four write tools are snapshot-and-confirm. Their arguments are optional because a user
    # may not have named the subscription; the code refuses an ambiguous request rather than guess.
    spec(
        "cancel",
        "取消一笔订阅（不可恢复）",
        WRITE_PROPERTIES,
        write=True,
    ),
    spec("pause", "暂停一笔订阅（可恢复）", WRITE_PROPERTIES, write=True),
    spec("resume", "恢复一笔已暂停的订阅", WRITE_PROPERTIES, write=True),
    spec("charge", "立即为一笔订阅扣款", WRITE_PROPERTIES, write=True),
    spec(
        "execute",
        "执行已由用户通过确认卡授权的订阅操作，仅供办理流程调用",
        {
            "subscription_id": WRITE_SUBSCRIPTION_ID_PROPERTY,
            "operation": {"type": "string", "enum": list(OPERATIONS)},
            # The amount the card froze, passed down by the workflow with the click: a charge
            # must spend exactly what the user confirmed, never a number read again at click time.
            "amount_minor": {
                "type": "integer",
                "description": "确认卡冻结的扣款金额（分），由办理流程随点击传入；扣款必填",
            },
        },
        ("subscription_id", "operation"),
        write=True,
        output="result",
    ),
]
TOOLS = {item.name: item for item in SPECS}
HANDLERS = {
    "subscription_list": subscriptions,
    "subscription_detail": detail,
    "subscription_cancel": prepare,
    "subscription_pause": prepare,
    "subscription_resume": prepare,
    "subscription_charge": prepare,
    "subscription_execute": execute,
}
