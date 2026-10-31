"""Card tools resolve user-visible selectors against the caller's own cards."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from app.integration.write_facade import WritePolicyError, require_customer_id
from app.modules.base import ERROR_MESSAGES, ToolData, ToolFailure
from app.modules.money import format_minor
from app.tools.protocol import ErrorCode, SideEffect, ToolSpec

CARD_TYPES = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
STATUS = {"active": "正常", "frozen": "已冻结", "blocked": "永久阻断"}
SELECTOR = {
    "card_suffix": {
        "type": "string",
        "pattern": "^[0-9]{4}$",
        "description": "卡号末四位，来自用户或查询结果",
    },
    "card_type": {"type": "string", "enum": list(CARD_TYPES)},
}
SUFFIX_SELECTOR = {
    **SELECTOR,
    "card_type": {
        **SELECTOR["card_type"],
        "description": "仅在尾号歧义（两张及以上同尾号）时用于消歧；不确定时可以不填",
    },
}
YUAN = {
    "type": "string",
    "pattern": "^[0-9]{1,7}(\\.[0-9]{1,2})?$",
    "description": "人民币元的十进制字符串，例如1000.00；不要换算为分",
}


def customer(invocation):
    """Same identity rule as fund writes (W-1): bank_customer_id, fail closed."""
    try:
        return require_customer_id(invocation.context)
    except WritePolicyError as policy:
        raise ToolFailure(policy.code, ERROR_MESSAGES[policy.code]) from policy


def card_label(card):
    return f"{CARD_TYPES[card['card_type']]}（尾号 {card['card_number'][-4:]}）"


def card_text(card):
    status = "已挂失" if card.get("freeze_reason") == "lost" else STATUS[card["status"]]
    merchants = "、".join(card.get("merchant_blacklist", [])) or "无"
    return f"{card_label(card)}：{status}，日交易限额 {format_minor(card['daily_limit_minor'])}，月交易限额 {format_minor(card['monthly_limit_minor'])}，受限商户：{merchants}。"


def matched_cards(cards, arguments):
    suffix = arguments.get("card_suffix")
    identity = [card for card in cards if not suffix or card["card_number"].endswith(suffix)]
    if len(identity) == 1:
        # 尾号是身份：唯一命中就返回，卡型只在歧义时收窄，永不否决。
        return identity
    kind = arguments.get("card_type")
    return [card for card in identity if not kind or card["card_type"] == kind]


def yuan_minor(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,2})?", value):
        raise ToolFailure(ErrorCode.VALIDATION, "限额请输入非负金额，最多两位小数。")
    try:
        minor = int(Decimal(value) * 100)
    except InvalidOperation:
        raise ToolFailure(ErrorCode.VALIDATION, "限额格式不正确。") from None
    if minor > 100000000:
        raise ToolFailure(ErrorCode.VALIDATION, "单项限额不能超过一百万元。")
    return minor


def load_cards(invocation, client):
    data = client.request("GET", "/cards", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "银行卡列表响应不正确。")
    return data


def cards(invocation, client):
    rows = load_cards(invocation, client)
    summary = "您目前没有银行卡。" if not rows else "\n".join(card_text(card) for card in rows)
    return ToolData(summary=summary, display={"blocks": []}, facts={"cards": rows})


def detail(invocation, client):
    matches = matched_cards(load_cards(invocation, client), invocation.call.arguments)
    if len(matches) != 1:
        raise ToolFailure(
            ErrorCode.VALIDATION if matches else ErrorCode.NOT_FOUND, "请明确选择一张本人银行卡。"
        )
    return ToolData(
        summary=card_text(matches[0]), display={"blocks": []}, facts={"card": matches[0]}
    )


def prepare(invocation, client):
    operation = invocation.call.tool_name.removeprefix("card_")
    args = dict(invocation.call.arguments)
    if operation == "apply":
        parameters = {"card_type": args["card_type"]}
    else:
        matches = matched_cards(load_cards(invocation, client), args)
        if len(matches) != 1:
            raise ToolFailure(
                ErrorCode.VALIDATION if matches else ErrorCode.NOT_FOUND,
                "请明确选择一张本人银行卡。",
            )
        parameters = {"card_id": matches[0]["card_id"]}
        if operation == "update_limit":
            for name in ("daily", "monthly"):
                value = args.get(f"{name}_limit_yuan")
                if value is not None:
                    parameters[f"{name}_limit_minor"] = yuan_minor(value)
        elif operation == "update_restrictions":
            parameters.update(mode=args["mode"], merchants=args["merchants"])
    data = client.request(
        "POST",
        "/cards/actions",
        user_id=customer(invocation),
        json={"operation": operation, "parameters": parameters},
        idempotency_key=invocation.call.idempotency_key,
    ).data
    return ToolData(
        summary="操作详情已准备，尚未修改银行卡，等待用户确认或验证。",
        display={"blocks": []},
        facts={"action": data},
    )


def execute(invocation, client):
    # Card action variant: identity via require_customer_id; audit via Tool Gateway (write-path W-5).
    data = client.request(
        "POST",
        f"/cards/actions/{invocation.call.arguments['action_id']}/execute",
        user_id=customer(invocation),
        json={},
        idempotency_key=invocation.call.idempotency_key,
    ).data
    return ToolData(
        summary=(
            "银行卡交易密码修改成功。"
            if data.get("operation") == "change_password"
            else "银行卡操作已完成。"
        )
        + card_text(data),
        display={"blocks": []},
        facts={"result": data},
        business_ids={"operation_reference": data["action_id"]},
    )


def cancel(invocation, client):
    data = client.request(
        "POST",
        f"/cards/actions/{invocation.call.arguments['action_id']}/authorize",
        user_id=customer(invocation),
        json={"decision": "cancel"},
        idempotency_key=invocation.call.idempotency_key,
    ).data
    return ToolData(
        summary="本次银行卡操作已取消，未修改银行卡。",
        display={"blocks": []},
        facts={"action": data},
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
        name=f"card_{name}",
        description=f"{description}。参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}",
        parameters=schema,
        side_effect=SideEffect.WRITE if write else SideEffect.READ,
        output_schema={
            "type": "object",
            "properties": {output: {"type": "array" if output == "cards" else "object"}},
            "required": [output],
            "additionalProperties": False,
        },
    )


SPECS = [
    spec("cards", "查询本人银行卡、状态、交易限额和商户黑名单", output="cards"),
    spec("detail", "查询指定银行卡详情", SUFFIX_SELECTOR, output="card"),
    spec("restrictions", "查询指定卡的交易限额和商户黑名单", SUFFIX_SELECTOR, output="card"),
    spec(
        "apply",
        "准备申请银行卡，代码会要求用户确认；这是模拟发卡，信用卡类型不代表授信审批",
        {"card_type": SELECTOR["card_type"]},
        ("card_type",),
        write=True,
    ),
    spec(
        "update_limit",
        "准备调整日或月交易限额，未提供项保持原值；零表示禁止交易",
        {**SUFFIX_SELECTOR, "daily_limit_yuan": YUAN, "monthly_limit_yuan": YUAN},
        write=True,
        anyOf=[{"required": ["daily_limit_yuan"]}, {"required": ["monthly_limit_yuan"]}],
    ),
    spec(
        "update_restrictions",
        "准备添加、移除或替换商户黑名单；这些是配置，不代表模拟转账会被拦截",
        {
            **SUFFIX_SELECTOR,
            "mode": {"type": "string", "enum": ["add", "remove", "replace"]},
            "merchants": {
                "type": "array",
                "maxItems": 100,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
        ("mode", "merchants"),
        write=True,
    ),
    *[
        spec(name, description, SUFFIX_SELECTOR, write=True)
        for name, description in (
            ("freeze", "准备临时冻结卡片"),
            ("unfreeze", "准备解冻普通冻结卡，需要验证"),
            ("report_loss", "准备挂失卡片，必须强验证，不能当作普通冻结"),
            ("release_loss", "准备解除挂失，必须强验证"),
            (
                "change_password",
                "准备修改银行卡交易密码，必须确认及强验证；只传卡片选择信息，密码仅由用户在独立表单填写",
            ),
        )
    ],
    spec(
        "execute",
        "执行已由用户通过独立接口授权的操作，仅供办理流程调用",
        {"action_id": {"type": "string", "pattern": "^CA-[a-f0-9]{32}$"}},
        ("action_id",),
        write=True,
        output="result",
    ),
    spec(
        "cancel",
        "取消待办理操作，仅供办理流程调用",
        {"action_id": {"type": "string", "pattern": "^CA-[a-f0-9]{32}$"}},
        ("action_id",),
        write=True,
    ),
]
TOOLS = {item.name: item for item in SPECS}
HANDLERS = {name: prepare for name in TOOLS}
HANDLERS.update(
    {
        "card_cards": cards,
        "card_detail": detail,
        "card_restrictions": detail,
        "card_execute": execute,
        "card_cancel": cancel,
    }
)
