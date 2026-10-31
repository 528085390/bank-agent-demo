"""The life event domain's tools: birthdays, gift plans, preparation, and execution.

Three of the four visible tools read the bank and do their thinking locally: the next
birthday is computed from the contact's stored date, the gift list is filtered by budget,
and life_prepare_order freezes everything the confirmation card needs without writing
anything. The visible write, life_cancel_order, goes through the fund-write facade in
writes.py. The hidden life_execute_order places that frozen order once the user clicks the
card: it is declared here but never offered to the model.

Identity comes from invocation.context.bank_customer_id, never from the model: the bank
answers for whoever the turn is bound to, and a missing binding is a tool failure.

Money thresholds are not repeated here. The only source is
app/policy/types.py DAILY_RED_THRESHOLD_MINOR, reached through classify_operation, and the
day total is read through DailyTotalStore with the invocation's user_id.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.core.clock import CST, SystemClock
from app.modules.base import ERROR_MESSAGES, ToolData, ToolFailure, ToolHandler
from app.modules.life_event.writes import cancel_order, submit_order
from app.modules.money import format_minor
from app.policy.evaluator import classify_operation
from app.policy.types import DAILY_RED_THRESHOLD_MINOR
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, SideEffect, ToolInvocation, ToolSpec

logger = logging.getLogger(__name__)

# 与银行侧一致（bank_backend/app/domain/birthday_service.py）：礼物要在生日前 2 天的
# 早上 8 点下单，留给银行备货。
LEAD_DAYS = 2
ORDER_HOUR = 8

ACCOUNT_TYPE_LABELS = {"current": "活期账户", "fixed": "定期账户"}

_YUAN = re.compile(r"[0-9]{1,9}(\.[0-9]{1,6})?")


def customer(invocation: ToolInvocation) -> str:
    value = invocation.context.bank_customer_id
    if not value:
        raise ToolFailure(
            ErrorCode.NO_CUSTOMER_BINDING, ERROR_MESSAGES[ErrorCode.NO_CUSTOMER_BINDING]
        )
    return value


def next_birthday(today: date, birthday: date) -> date:
    """The next occurrence of a birthday, today included.

    A 29 February birthday has no counterpart in a common year; it is aligned to 1 March
    rather than dropped, because the gift still has to be sent that year.
    """

    try:
        candidate = birthday.replace(year=today.year)
    except ValueError:
        candidate = date(today.year, 3, 1)
    if candidate < today:
        try:
            candidate = birthday.replace(year=today.year + 1)
        except ValueError:
            candidate = date(today.year + 1, 3, 1)
    return candidate


def days_until(today: date, birthday: date) -> int:
    return (next_birthday(today, birthday) - today).days


def pick_products(products: list[dict], *, budget_minor: int | None = None) -> list[dict]:
    """The gift list, cheapest first, narrowed to what the budget can pay for.

    Returns copies: the bank's payload is not ours to mutate, and the frozen snapshot in
    life_prepare_order is built from these dicts.
    """

    picked = [
        dict(item)
        for item in products
        if budget_minor is None or int(item.get("price_minor") or 0) <= budget_minor
    ]
    return sorted(
        picked,
        key=lambda item: (int(item.get("price_minor") or 0), str(item.get("product_code", ""))),
    )


def budget_minor(value: Any) -> int | None:
    """Parse a yuan budget into minor units, truncating sub-cent fractions; None means no budget."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ToolFailure(ErrorCode.VALIDATION, "预算请输入元的十进制数字，例如 200.00。")
    text = str(value).strip()
    if not _YUAN.fullmatch(text):
        raise ToolFailure(ErrorCode.VALIDATION, "预算请输入元的十进制数字，例如 200.00。")
    try:
        minor = int(Decimal(text) * 100)
    except InvalidOperation:  # pragma: no cover - the regex already guarantees digits
        raise ToolFailure(ErrorCode.VALIDATION, "预算请输入元的十进制数字，例如 200.00。")
    if minor <= 0:
        raise ToolFailure(ErrorCode.VALIDATION, "预算必须大于 0。")
    return minor


def _parse_date(value: Any) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ToolFailure(
            ErrorCode.VALIDATION, f"生日日期无法识别：{value}；请用 YYYY-MM-DD 的格式。"
        )


def _month_day(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.month}月{parsed.day}日"


def account_label(account: Mapping[str, Any]) -> str:
    kind = str(account.get("account_type") or "")
    account_id = str(account.get("account_id") or "")
    return f"{ACCOUNT_TYPE_LABELS.get(kind, kind)}（尾号 {account_id[-4:]}）"


def _contacts(invocation: ToolInvocation, client) -> list[dict]:
    data = client.request("GET", "/birthdays", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "生日联系人响应不正确。")
    return data


def _products(invocation: ToolInvocation, client) -> list[dict]:
    data = client.request("GET", "/mock-products", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "礼物列表响应不正确。")
    return data


def _contact(invocation: ToolInvocation, client, contact_id: str) -> dict:
    if not contact_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供生日联系人的 ID。")
    contacts = _contacts(invocation, client)
    for contact in contacts:
        if str(contact.get("contact_id")) == contact_id:
            return contact
    # 模型偶尔把姓名当 ID 传（transfer 也按姓名找人）。ID 找不到时再按姓名精确匹配；
    # 同名多人就拒绝，让模型回头问用户，别替人挑一个。
    named = [contact for contact in contacts if str(contact.get("name") or "") == contact_id]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = "、".join(str(contact.get("contact_id")) for contact in named)
        raise ToolFailure(
            ErrorCode.VALIDATION, f"有多个联系人叫「{contact_id}」：{ids}；请指定联系人 ID。"
        )
    raise ToolFailure(ErrorCode.NOT_FOUND, f"没有找到联系人：{contact_id}。")


def _product(invocation: ToolInvocation, client, product_code: str) -> dict:
    if not product_code:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供礼物编码：flowers（鲜花）或 cake（蛋糕）。")
    for product in _products(invocation, client):
        if str(product.get("product_code")) == product_code:
            return product
    raise ToolFailure(ErrorCode.NOT_FOUND, f"没有找到礼物：{product_code}。")


def _accounts(invocation: ToolInvocation, client) -> list[dict]:
    data = client.request("GET", "/accounts", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "账户列表响应不正确。")
    return data


def _paying_account(invocation: ToolInvocation, client, requested: Any) -> dict:
    """The account the order will debit: the named one, or the only usable one."""

    usable = [
        account
        for account in _accounts(invocation, client)
        if account.get("account_type") != "fixed" and account.get("status") == "active"
    ]
    wanted = str(requested).strip() if requested else ""
    if wanted:
        for account in usable:
            if str(account.get("account_id")) == wanted:
                return account
        raise ToolFailure(ErrorCode.NOT_FOUND, f"没有找到可以付款的活期账户：{wanted}。")
    if not usable:
        raise ToolFailure(ErrorCode.NOT_FOUND, "没有找到可以付款的活期账户。")
    if len(usable) > 1:
        # 尾号在长账户号上会撞车，也会让模型无法把选项映射回 from_account_id，所以列全 ID。
        names = "、".join(str(account.get("account_id")) for account in usable)
        raise ToolFailure(
            ErrorCode.VALIDATION, f"有多个活期账户可以付款：{names}；请告诉我用哪个账户。"
        )
    return usable[0]


def _daily_accumulated_minor(invocation: ToolInvocation, database) -> int:
    """Today's accumulated amount, or 0 when the database was not injected.

    ``bind_life_event`` injects the runtime database, so in production the total is read
    from ``DailyTotalStore``. When a caller drives the handler without one, the accumulated
    total is unknown and the conservative reading is 0: the bank re-checks the threshold
    when the order is actually submitted, and the confirmation card still asks for a code
    when the day's other writes already crossed the line.
    """

    if database is None:
        logger.debug("life_daily_total_unbound user_id=%s", invocation.context.user_id)
        return 0
    try:
        with database.sessions() as session:
            return DailyTotalStore.get_accumulated_minor(
                session, invocation.context.user_id, SystemClock.today()
            )
    except Exception:
        # Fail closed: an unreadable day total must not make a write look safer than it is.
        logger.warning("life_daily_total_read_failed user_id=%s", invocation.context.user_id)
        return DAILY_RED_THRESHOLD_MINOR + 1


def _execute_at(target: date) -> datetime:
    return datetime.combine(target - timedelta(days=LEAD_DAYS), time(hour=ORDER_HOUR), tzinfo=CST)


def _target_birthday(raw: Any, contact: Mapping[str, Any], today: date) -> date:
    explicit = _parse_date(raw)
    if explicit is None:
        stored = contact.get("birthday")
        if not stored:
            raise ToolFailure(
                ErrorCode.VALIDATION,
                f"{contact.get('name')}没有登记生日；请直接告诉我生日是哪天。",
            )
        return next_birthday(today, date.fromisoformat(str(stored)))
    if explicit < today:
        # 用户给的是出生年份（例如 1995-09-25）：换算成下一次生日。
        return next_birthday(today, explicit)
    return explicit


# ------------------------------------------------------------------ tools


def birthdays(invocation: ToolInvocation, client) -> ToolData:
    today = SystemClock.today()
    rows: list[dict] = []
    for contact in _contacts(invocation, client):
        stored = contact.get("birthday")
        if not stored:
            continue
        birthday = date.fromisoformat(str(stored))
        upcoming = next_birthday(today, birthday)
        rows.append(
            {
                "contact_id": contact.get("contact_id"),
                "name": contact.get("name"),
                "birthday": birthday.isoformat(),
                "next_birthday": upcoming.isoformat(),
                "days_until": (upcoming - today).days,
            }
        )
    rows.sort(key=lambda row: (row["days_until"], str(row["contact_id"])))

    if not rows:
        summary = "您还没有登记生日信息的联系人。"
    else:
        lines = []
        for row in rows:
            when = "就是今天" if row["days_until"] == 0 else f"还有 {row['days_until']} 天"
            lines.append(
                f"{row['name']}：生日 {_month_day(row['birthday'])}，"
                f"下次生日 {row['next_birthday']}，{when}。"
            )
        summary = "\n".join(lines)

    return ToolData(summary=summary, display={"blocks": []}, facts={"contacts": rows})


def gift_plan(invocation: ToolInvocation, client) -> ToolData:
    budget = budget_minor(invocation.call.arguments.get("budget_yuan"))
    products = pick_products(_products(invocation, client), budget_minor=budget)

    if not products:
        summary = (
            "预算内没有可选礼物；可以放宽预算再挑。"
            if budget is not None
            else "银行还没有可选的生日礼物。"
        )
    else:
        head = (
            "生日礼物候选"
            if budget is None
            else f"生日礼物候选（预算 {format_minor(budget)} 以内）"
        )
        lines = [f"{head}："]
        for item in products:
            price = format_minor(
                int(item.get("price_minor") or 0), str(item.get("currency") or "CNY")
            )
            lines.append(f"- {item.get('name')}（{item.get('product_code')}）：{price}")
        summary = "\n".join(lines)

    facts = {
        "products": [
            {
                "product_code": item.get("product_code"),
                "name": item.get("name"),
                "price_minor": int(item.get("price_minor") or 0),
                "currency": str(item.get("currency") or "CNY"),
            }
            for item in products
        ],
        "budget_minor": budget,
    }
    return ToolData(summary=summary, display={"blocks": []}, facts=facts)


def prepare_order(invocation: ToolInvocation, client, database=None) -> ToolData:
    """Freeze everything a birthday order needs, without writing anything.

    The returned facts are what the confirmation card and life_execute_order read: the
    contact, the product, the amount, the account, and the moment the bank will place the
    order. Nothing is sent to the bank here, so cancelling the card costs nothing.
    """

    arguments = invocation.call.arguments
    today = SystemClock.today()
    contact = _contact(invocation, client, str(arguments.get("contact_id") or "").strip())
    product = _product(invocation, client, str(arguments.get("product_code") or "").strip())
    target = _target_birthday(arguments.get("birthday_date"), contact, today)
    remaining = (target - today).days
    execute_at = _execute_at(target)

    if remaining < LEAD_DAYS or execute_at <= SystemClock.now():
        raise ToolFailure(
            ErrorCode.VALIDATION,
            f"{contact.get('name')}的生日只剩 {remaining} 天，礼物要在生日前 {LEAD_DAYS} 天送达，"
            "已经来不及了；可以换一个更晚的日期，或者直接联系人工处理。",
        )

    account = _paying_account(invocation, client, arguments.get("from_account_id"))
    amount_minor = int(product.get("price_minor") or 0)
    decision = classify_operation(
        "birthday_order",
        amount_minor=amount_minor,
        daily_accumulated_minor=_daily_accumulated_minor(invocation, database),
        side_effect=SideEffect.WRITE,
    )

    intent = {
        "contact_id": contact.get("contact_id"),
        "contact_name": contact.get("name"),
        "product_code": product.get("product_code"),
        "product_name": product.get("name"),
        "amount_minor": amount_minor,
        "from_account_id": account.get("account_id"),
        "from_account_label": account_label(account),
        "birthday_date": target.isoformat(),
        "execute_at": execute_at.isoformat(),
    }

    lines = [
        f"给{intent['contact_name']}的生日礼物：{intent['product_name']}"
        f"（{format_minor(amount_minor)}），生日 {intent['birthday_date']}，"
        f"银行会在 {execute_at.strftime('%Y-%m-%d %H:%M')} 自动下单。",
        f"从{account_label(account)}扣款。",
    ]
    if decision.mfa_required:
        lines.append(f"需要验证码：{decision.reason}")
    else:
        lines.append(f"风险等级：{decision.level.value}，确认卡片点一下就下单。")
    lines.append("点击「确认」后才会下单；取消不会发起。")

    return ToolData(
        summary="\n".join(lines),
        display={"blocks": []},
        facts={
            "status": "ready",
            "intent": intent,
            "requires_code": decision.mfa_required,
            "risk_level": decision.level.value,
            "reason": decision.reason,
        },
    )


def cancel(invocation: ToolInvocation, client, *, writes=None) -> ToolData:
    order_id = str(invocation.call.arguments.get("order_id") or "").strip()
    if not order_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供要取消的生日订单号。")
    response = cancel_order(
        client,
        invocation.context,
        order_id,
        idempotency_key=invocation.call.idempotency_key or "",
        writes=writes,
    )
    order = dict(response.data) if isinstance(response.data, dict) else {}
    return ToolData(
        summary=f"生日订单 {order_id} 已取消。",
        display={"blocks": []},
        facts={"order": order},
        business_ids={"order_id": order_id},
    )


def execute_order(
    invocation: ToolInvocation,
    client,
    *,
    writes=None,
    verification_code: str | None = None,
) -> ToolData:
    """Place the frozen birthday order after the user clicked the confirmation card.

    Hidden from the model's menu: only the process node calls it, with the facts
    life_prepare_order froze. Nothing here re-prices the gift or re-picks the paying
    account -- the card the user saw is the order that goes to the bank.

    The verification code is a keyword argument, never a tool argument. Arguments reach the
    browser in the `tool_call` SSE frame (`app/agents/sdk.py`) and are stored whole in the
    run-event audit (`app/tools/gateway.py::_record_call` -> `app/audit/service.py`), so a
    code declared in the schema would leak. Task 3's process node passes
    `turn.verification_code` here, the same way `transfer/workflow.py` passes it to
    `submit_transfer`.

    R12: this tool only checks the *shape* of the code (a non-digit is refused before the
    socket, in `submit_order`). Whether this card was allowed to ask for a code at all -- a
    yellow card (`requires_code=False`) that arrives with one must be refused -- is the
    process node's ruling, not this tool's.
    """

    arguments = invocation.call.arguments
    intent: dict[str, Any] = {
        "contact_id": str(arguments.get("contact_id") or "").strip(),
        "product_code": str(arguments.get("product_code") or "").strip(),
        "from_account_id": str(arguments.get("from_account_id") or "").strip(),
        "birthday_date": str(arguments.get("birthday_date") or "").strip(),
    }
    if not all(intent.values()):
        raise ToolFailure(ErrorCode.VALIDATION, "生日订单参数不完整，请重新发起办理。")
    amount_minor = arguments.get("amount_minor")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        # 金额是确认卡冻结的那一笔。缺了它，门面会把这单当成「不动钱」的写，跳过当日限额。
        raise ToolFailure(ErrorCode.VALIDATION, "生日订单金额无效，请重新发起办理。")
    intent["amount_minor"] = amount_minor

    response = submit_order(
        client,
        invocation.context,
        intent,
        idempotency_key=invocation.call.idempotency_key or "",
        verification_code=verification_code,
        writes=writes,
    )
    order = dict(response.data) if isinstance(response.data, dict) else {}
    details = order.get("details") if isinstance(order.get("details"), dict) else {}
    order_id = str(order.get("order_id") or "")
    summary = (
        f"生日订单 {order_id} 已提交：{details.get('product_name') or intent['product_code']}"
        f"（{format_minor(int(order.get('amount_minor') or amount_minor))}）"
    )
    if details.get("execute_at"):
        summary += f"，银行将在 {str(details['execute_at'])[:16].replace('T', ' ')} 自动下单"
    summary += "。"
    return ToolData(
        summary=summary,
        display={"blocks": []},
        facts={"order": order},
        business_ids={"order_id": order_id} if order_id else {},
    )


# ------------------------------------------------------------------ specs


def spec(
    name: str,
    description: str,
    properties: Mapping[str, Any] | None = None,
    required: tuple[str, ...] = (),
    *,
    write: bool = False,
    facts: Mapping[str, Any],
) -> ToolSpec:
    """One tool, with the arguments and the facts spelled out for the model.

    The parameter schema travels inside the description as well: the Supervisor shows the
    model the text, and a tool that does not say what it takes is a tool the model guesses
    at.
    """

    schema = {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }
    output = {
        "type": "object",
        "properties": dict(facts),
        "required": sorted(facts),
        "additionalProperties": False,
    }
    return ToolSpec(
        name=f"life_{name}",
        description=f"{description}。参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}",
        parameters=schema,
        side_effect=SideEffect.WRITE if write else SideEffect.READ,
        output_schema=output,
    )


SPECS = (
    spec(
        "birthdays",
        "查询联系人登记的生日，以及距离下次生日还有几天。用户问「谁快过生日」「我老婆生日是哪天」时先调用它，"
        "拿到联系人姓名、生日日期和剩余天数；下单时可以把姓名作为 contact_id 传给 life_prepare_order",
        facts={"contacts": {"type": "array", "items": {"type": "object"}}},
    ),
    spec(
        "gift_plan",
        "列出银行提供的生日礼物（鲜花/蛋糕），按价格从低到高，可按预算过滤。预算用元表示，例如 200 或 "
        '"200.00"；不传表示不限',
        properties={
            "budget_yuan": {
                "type": ["string", "number"],
                "description": '预算（元），例如 200 或 "200.00"；不传表示不限',
            }
        },
        facts={
            "products": {"type": "array", "items": {"type": "object"}},
            "budget_minor": {"type": ["integer", "null"]},
        },
    ),
    spec(
        "prepare_order",
        "为某位联系人的生日冻结一份生日礼物订单快照：校验联系人、礼物、付款账户和日期，按当日累计判定风险等级，"
        "返回待确认的参数。不会下单；生日日期不传就用联系人档案里的生日，距生日不足 2 天时拒绝",
        properties={
            "contact_id": {
                "type": "string",
                "description": "生日联系人 ID 或姓名；姓名为 life_birthdays 返回的联系人姓名，必须与通讯录完全一致",
            },
            "product_code": {
                "type": "string",
                "enum": ["flowers", "cake"],
                "description": "礼物：flowers 鲜花，cake 蛋糕",
            },
            "birthday_date": {
                "type": "string",
                "description": "生日日期 YYYY-MM-DD；不传就用联系人档案里的生日",
            },
            "from_account_id": {
                "type": "string",
                "description": "付款账户 ID；不传就自动选唯一的活期账户",
            },
        },
        required=("contact_id", "product_code"),
        facts={
            "status": {"type": "string"},
            "intent": {"type": "object"},
            "requires_code": {"type": "boolean"},
            "risk_level": {"type": "string"},
            "reason": {"type": "string"},
        },
    ),
    spec(
        "cancel_order",
        "取消一张还没执行的生日订单。用户说「取消生日订单」时调用；用户说「刚才那单」时可以不传订单号，默认取消本对话最近提交的生日订单",
        properties={
            "order_id": {
                "type": "string",
                "description": "要取消的生日订单号；不传就用本对话最近提交的订单",
            }
        },
        write=True,
        facts={"order": {"type": "object"}},
    ),
    spec(
        "execute_order",
        "执行已由用户通过确认卡授权的生日订单，仅由确认卡回调触发；参数是 life_prepare_order 冻结的"
        "订单快照，执行侧不重新定价、不重新选账户",
        properties={
            "contact_id": {"type": "string", "description": "冻结的生日联系人 ID"},
            "product_code": {"type": "string", "description": "冻结的礼物代码：flowers 或 cake"},
            "from_account_id": {"type": "string", "description": "冻结的付款账户 ID"},
            "birthday_date": {"type": "string", "description": "冻结的生日日期 YYYY-MM-DD"},
            "amount_minor": {
                "type": "integer",
                "description": "确认卡冻结的订单金额（分），由办理流程随点击传入；下单必填",
            },
        },
        required=("contact_id", "product_code", "from_account_id", "birthday_date", "amount_minor"),
        write=True,
        facts={"order": {"type": "object"}},
    ),
)

TOOLS: dict[str, ToolSpec] = {item.name: item for item in SPECS}

HANDLERS: dict[str, ToolHandler] = {
    "life_birthdays": birthdays,
    "life_gift_plan": gift_plan,
    "life_prepare_order": prepare_order,
    "life_cancel_order": cancel,
    "life_execute_order": execute_order,
}

__all__ = [
    "HANDLERS",
    "LEAD_DAYS",
    "ORDER_HOUR",
    "TOOLS",
    "account_label",
    "birthdays",
    "budget_minor",
    "cancel",
    "customer",
    "days_until",
    "execute_order",
    "gift_plan",
    "next_birthday",
    "pick_products",
    "prepare_order",
]
