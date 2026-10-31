"""转账工具：把用户的话解析成一次可确认的转账意图，其余只读查询直接回银行事实。

这一层与 card 的工具层同构：每个 handler 拿 (invocation, client)，身份来自
`invocation.context.bank_customer_id`，金额一律是人民币元字符串（模型不做换算）。
`transfer_prepare` 只解析、不写银行；真正的一次写走 `writes.py` 的受控路径。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from app.audit.context import AuditContext
from app.audit.service import AuditService
from app.core.clock import CST, SystemClock
from app.modules.base import ERROR_MESSAGES, ToolData, ToolFailure
from app.modules.dates import parse_tool_date
from app.modules.money import format_minor
from app.modules.transfer.writes import cancel_aa, cancel_scheduled
from app.policy.evaluator import classify_operation
from app.policy.types import DAILY_RED_THRESHOLD_MINOR
from app.storage.daily_total import DailyTotalStore
from app.tools.protocol import ErrorCode, SideEffect, ToolSpec

logger = logging.getLogger(__name__)

# bank_backend/app/domain/transfer_service.py 的单笔上限。
MAX_TRANSFER_MINOR = 100000000

ACCOUNT_TYPE_LABELS = {"current": "活期账户", "fixed": "定期账户"}
ACCOUNT_STATUS_LABELS = {"active": "正常", "frozen": "已冻结", "closed": "已销户"}
SCHEDULED_STATUSES = ("pending", "completed", "cancelled", "failed")

AA_STATUS_LABELS = {
    "open": "进行中",
    "partial": "部分已付",
    "completed": "已全部付清",
    "cancelled": "已取消",
    "expired": "已过期",
}
AA_PARTICIPANT_LABELS = {"pending": "待付", "paid": "已付", "refused": "已拒绝"}
AA_MAX_PARTICIPANTS = 20

# 金额只要求「元的十进制数」：真实模型经常直接送 number（100 而不是 "100.00"），把它整轮拒掉
# 只会让用户看到一句「参数格式不正确」。这里放宽类型、由 yuan_minor 严格校验取值。
YUAN = {
    "type": ["string", "number"],
    "description": "人民币元金额，例如 5000.00；不要换算为分",
}


def customer(invocation):
    """The bank customer this call is bound to. Missing identity never reaches the bank."""

    value = invocation.context.bank_customer_id
    if not value:
        raise ToolFailure(
            ErrorCode.NO_CUSTOMER_BINDING, ERROR_MESSAGES[ErrorCode.NO_CUSTOMER_BINDING]
        )
    return value


def yuan_minor(value):
    """A plain yuan amount to integer fen. Anything else is the user's problem, not a crash."""

    if isinstance(value, bool):
        raise ToolFailure(ErrorCode.VALIDATION, "金额请输入元的十进制数字，最多两位小数，例如 5000.00。")
    if isinstance(value, (int, float)):
        # 模型送 number 是常见行为：整数直接转字符串，小数按两位截断（银行只接受整数分）。
        value = f"{value:.2f}" if isinstance(value, float) else str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,2})?", value):
        raise ToolFailure(ErrorCode.VALIDATION, "金额请输入元的十进制数字，最多两位小数，例如 5000.00。")
    try:
        minor = int(Decimal(value) * 100)
    except InvalidOperation:
        raise ToolFailure(ErrorCode.VALIDATION, "金额格式不正确。") from None
    if minor <= 0:
        raise ToolFailure(ErrorCode.VALIDATION, "转账金额必须大于 0。")
    if minor > MAX_TRANSFER_MINOR:
        raise ToolFailure(ErrorCode.VALIDATION, "单笔转账不能超过一百万元。")
    return minor


def account_label(account):
    kind = account.get("account_type") or "账户"
    return f"{ACCOUNT_TYPE_LABELS.get(kind, kind)}（尾号 {account['account_id'][-4:]}）"


def contact_label(contact):
    return f"{contact['name']}（尾号 {contact['recipient_account_id'][-4:]}）"


def load_accounts(invocation, client):
    data = client.request("GET", "/accounts", user_id=customer(invocation)).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "账户列表响应不正确。")
    return data


def load_contacts(invocation, client, query=None):
    data = client.request(
        "GET",
        "/contacts",
        user_id=customer(invocation),
        params={"q": query} if query else None,
    ).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "联系人响应不正确。")
    return data


def accounts(invocation, client):
    rows = load_accounts(invocation, client)
    if not rows:
        return ToolData(summary="您目前没有可用的银行账户。", display={"blocks": []}, facts={"accounts": []})
    lines = []
    for account in rows:
        status = account.get("status") or "未知"
        lines.append(
            f"{account_label(account)}：余额 "
            f"{format_minor(account['balance_minor'], account.get('currency', 'CNY'))}，"
            f"状态 {ACCOUNT_STATUS_LABELS.get(status, status)}。"
        )
    return ToolData(summary="\n".join(lines), display={"blocks": []}, facts={"accounts": rows})


def contacts(invocation, client):
    query = str(invocation.call.arguments.get("query") or "").strip()
    rows = load_contacts(invocation, client, query or None)
    if not rows:
        summary = "没有找到匹配的联系人。"
    else:
        lines = []
        for contact in rows:
            usable = "可转账" if contact.get("recipient_account_id") else "没有可用的收款账户"
            lines.append(
                f"{contact['name']}：手机号 {contact.get('phone') or '无'}，"
                f"备注 {contact.get('remark') or '无'}，{usable}。"
            )
        summary = "\n".join(lines)
    return ToolData(summary=summary, display={"blocks": []}, facts={"contacts": rows})


def scheduled(invocation, client):
    status = invocation.call.arguments.get("status")
    params = {"status": status} if status else None
    data = client.request(
        "GET", "/transfers/scheduled", user_id=customer(invocation), params=params
    ).data
    if not isinstance(data, list):
        raise ToolFailure(ErrorCode.PROTOCOL, "定时转账列表响应不正确。")
    if not data:
        return ToolData(summary="您目前没有定时转账。", display={"blocks": []}, facts={"scheduled": []})
    lines = []
    for task in data:
        amount = task.get("amount_minor")
        money = format_minor(amount) if isinstance(amount, int) and not isinstance(amount, bool) else "金额未知"
        lines.append(
            f"{task['task_id']}：{money}，执行时间 {task.get('execute_at') or '未知'}，"
            f"状态 {task.get('status') or '未知'}。"
        )
    return ToolData(summary="\n".join(lines), display={"blocks": []}, facts={"scheduled": data})


#: 卡片上写的是「活期账户（尾号 rent）」。用户和模型复述时常常丢掉全角括号、换成半角
#: 括号或干脆删掉空格，所以比对前把括号与空白都抹掉，只比实义文字。
_IGNORED_IN_MATCH = str.maketrans("", "", "()[]{}<>（）【】《》「」　 \t\n\r")


def _loose(text: str) -> str:
    """比对用的宽松形式：去掉括号与所有空白，统一小写。"""

    return str(text).translate(_IGNORED_IN_MATCH).lower()

def _account_matches(account, hint):
    needle = _loose(hint)
    if not needle:
        return False
    label = _loose(account_label(account))
    identifier = _loose(account["account_id"])
    return needle in label or needle == identifier or identifier.endswith(needle)


def _select(field, display, candidates, intent):
    labels = [item["label"] for item in candidates]
    if len(set(labels)) != len(labels):
        # Labels come from bank data; two identical ones cannot be told apart by a click.
        raise ToolFailure(ErrorCode.VALIDATION, "候选账户或收款人重名，无法安全选择，请联系人工处理。")
    return ToolData(
        summary=display,
        display={"blocks": []},
        facts={"status": "select", "field": field, "candidates": candidates, "intent": intent},
    )


def _execute_at(invocation, *, now=None):
    """把「执行日期 + 执行时间」压成一个带东八区时区的绝对时刻。

    execute_date 接受项目统一的日期写法（含「明天」「后天」）；execute_time 默认
    09:00，格式 HH:MM。银行的任务表只认带时区的时间，而且预约必须发生在未来。
    """

    args = invocation.call.arguments
    current = now or SystemClock.now()
    day = parse_tool_date(args.get("execute_date"), "execute_date", today=current.date())
    raw_time = str(args.get("execute_time") or "09:00").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw_time)
    if not match:
        raise ToolFailure(ErrorCode.VALIDATION, "执行时间请用 24 小时制 HH:MM，例如 09:00。")
    execute_at = datetime.combine(day, time(int(match.group(1)), int(match.group(2))), tzinfo=CST)
    if execute_at <= current:
        raise ToolFailure(ErrorCode.VALIDATION, "定时转账的执行时间必须晚于当前时间。")
    return execute_at


def schedule_prepare(invocation, client, database=None, *, now=None):
    """解析一笔定时转账，复用即时转账的账户/收款人/风控判定，只多一个执行时刻。

    真正的创建仍发生在用户点击确认之后（writes.create_scheduled）；这里只读银行，
    所以和 prepare 一样是 READ 工具。选择付款账户/收款人时，冻结的 intent 里也要带上
    execute_at，否则用户选完后会退化成即时转账。
    """

    execute_at = _execute_at(invocation, now=now)
    result = prepare(invocation, client, database=database)
    facts = result.facts
    status = facts.get("status")
    if status not in ("ready", "select"):
        return result
    facts["operation"] = "schedule_transfer"
    intent = facts.setdefault("intent", {})
    intent["execute_at"] = execute_at.isoformat()
    intent["execute_at_label"] = execute_at.strftime("%Y-%m-%d %H:%M")
    facts["execute_at_label"] = intent["execute_at_label"]
    if status == "ready":
        summary = (
            result.summary.replace("请确认这笔转账", "请确认这笔定时转账")
            .replace(
                "点击「确认」后才会发起转账",
                f"执行时间：{facts['execute_at_label']}；点击「确认」后将创建定时转账",
            )
        )
        return replace(result, summary=summary)
    return result


def prepare(invocation, client, database=None):
    """Resolve the user's words into one frozen intent. It never writes the bank.

    `database` 只用来读当日累计金额（与写侧记账同一个 `DailyTotalStore`）；不传时按 0 计，
    仅供不接数据库的工具层测试使用。生产装配由 `app.modules.production_modules` 注入。
    """

    args = dict(invocation.call.arguments)
    amount_minor = yuan_minor(args.get("amount_yuan"))
    to_name = str(args.get("to_name") or "").strip()
    if not to_name:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供收款人姓名。")
    from_hint = str(args.get("from_hint") or "").strip()
    from_account_id = str(args.get("from_account_id") or "").strip()
    to_account_id = str(args.get("to_account_id") or "").strip()
    remark = args.get("remark") or None

    writable = [
        account
        for account in load_accounts(invocation, client)
        if account.get("account_type") != "fixed" and account.get("status") == "active"
    ]
    if from_account_id:
        matches = [account for account in writable if account["account_id"] == from_account_id]
    elif from_hint:
        matches = [account for account in writable if _account_matches(account, from_hint)]
    else:
        matches = writable
    if not matches:
        raise ToolFailure(ErrorCode.NOT_FOUND, "没有找到符合条件的付款账户。")
    if len(matches) > 1:
        return _select(
            "from_account",
            "有多张账户符合条件，请选择付款账户。",
            [{"label": account_label(item), "value": item["account_id"]} for item in matches],
            {"amount_minor": amount_minor, "to_name": to_name, "remark": remark},
        )
    payer = matches[0]
    if payer.get("available_minor", 0) < amount_minor:
        raise ToolFailure(
            ErrorCode.INSUFFICIENT_BALANCE, f"{account_label(payer)}的可用余额不足。"
        )

    found = load_contacts(invocation, client, to_name)
    exact = [contact for contact in found if contact.get("name") == to_name] or found
    usable = [contact for contact in exact if contact.get("recipient_account_id")]
    if to_account_id:
        usable = [
            contact for contact in usable if contact["recipient_account_id"] == to_account_id
        ]
    if not usable:
        raise ToolFailure(ErrorCode.NOT_FOUND, f"没有找到可以向「{to_name}」转账的收款账户。")
    if len(usable) > 1:
        return _select(
            "to_account",
            "有多个同名收款人，请选择收款人。",
            [
                {"label": contact_label(contact), "value": contact["recipient_account_id"]}
                for contact in usable
            ],
            {"amount_minor": amount_minor, "from_account_id": payer["account_id"], "remark": remark},
        )
    payee = usable[0]
    if payee["recipient_account_id"] == payer["account_id"]:
        raise ToolFailure(ErrorCode.VALIDATION, "转出与转入账户不能相同。")

    from_label = account_label(payer)
    to_label = payee["name"]
    accumulated_minor = 0
    if database is not None:
        try:
            with database.sessions() as session:
                accumulated_minor = DailyTotalStore.get_accumulated_minor(
                    session, invocation.context.user_id, SystemClock.today()
                )
        except Exception:
            # 读不到当日累计就按「已超限」判红：宁可多要一次验证码，不可漏判。
            accumulated_minor = DAILY_RED_THRESHOLD_MINOR + 1
            logger.warning(
                "transfer_daily_total_read_failed user_id=%s", invocation.context.user_id
            )
    decision = classify_operation(
        "transfer",
        amount_minor=amount_minor,
        daily_accumulated_minor=accumulated_minor,
        side_effect=SideEffect.WRITE,
    )
    requires_code = decision.mfa_required
    if requires_code and database is not None:
        try:
            AuditService(database).record_business_event(
                action="transfer_prepare",
                resource_type="policy_decision",
                resource_id=decision.level.value,
                result="ok",
                reason=decision.reason,
                context=AuditContext(user_id=invocation.context.user_id),
            )
        except Exception:
            # 留痕失败不能拖垮这张确认卡；日志是这里唯一还能留下的信号。
            logger.warning(
                "transfer_policy_audit_failed user_id=%s level=%s",
                invocation.context.user_id,
                decision.level.value,
            )
    lines = [f"请确认这笔转账：{format_minor(amount_minor)}，从{from_label}转给{to_label}。"]
    if remark:
        lines.append(f"备注：{remark}")
    if requires_code:
        lines.append(f"需要验证码：{decision.reason}")
    else:
        lines.append(f"风险等级：{decision.level.value}，确认卡片点一下即可发起。")
    lines.append("点击「确认」后才会发起转账；取消不会发起。")
    return ToolData(
        summary="\n".join(lines),
        display={"blocks": []},
        facts={
            "status": "ready",
            "intent": {
                "from_account_id": payer["account_id"],
                "to_account_id": payee["recipient_account_id"],
                "amount_minor": amount_minor,
                "remark": remark,
            },
            "from_label": from_label,
            "to_label": to_label,
            "requires_code": requires_code,
            "risk_level": decision.level.value,
            "reason": decision.reason,
        },
    )


def cancel(invocation, client):
    """取消一笔已存在的定时转账。图上把它拦成确认卡之后才会被调用。"""

    task_id = str(invocation.call.arguments.get("task_id") or "").strip()
    if not task_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供要取消的定时转账编号。")
    response = cancel_scheduled(
        client, invocation.context, task_id, idempotency_key=invocation.call.idempotency_key
    )
    return ToolData(
        summary=f"已取消定时转账 {task_id}。",
        display={"blocks": []},
        facts={"task": response.data},
        business_ids={"task_id": task_id},
    )


def _split_amount(total_minor, count):
    """与银行 aa_service.split_amount 逐字对齐：平均拆分，余数由最后一名承担。"""

    if count < 2:
        raise ToolFailure(ErrorCode.VALIDATION, "AA 收款至少需要 2 名参与者。")
    if total_minor < count:
        raise ToolFailure(ErrorCode.VALIDATION, "总金额不足以让每位参与者至少分摊 1 分。")
    base, remainder = divmod(total_minor, count)
    return [base] * (count - 1) + [base + remainder]


def _aa_participant_names(args):
    """把模型给的参与人收敛成姓名列表：显式姓名优先，没有姓名时按人数生成占位。"""

    include_me = bool(args.get("include_me"))
    names = []
    raw = args.get("participants")
    if isinstance(raw, list):
        names = [str(item or "").strip() for item in raw]
        names = [name for name in names if name]
    if not names:
        count = args.get("participant_count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
            raise ToolFailure(ErrorCode.VALIDATION, "participant_count 请传人数整数。")
        if count is None:
            raise ToolFailure(
                ErrorCode.VALIDATION, "请提供参与人姓名列表（participants）或总人数（participant_count）。"
            )
        # participant_count 是含发起人的总人数：我占一份时，其余人是 count-1 位。
        others = count - (1 if include_me else 0)
        if others < 1:
            raise ToolFailure(ErrorCode.VALIDATION, "AA 收款至少需要 2 名参与者。")
        names = [f"参与者{index}" for index in range(1, others + 1)]
    return names, include_me


def _load_aa(invocation, client, aa_request_id):
    data = client.request(
        "GET", f"/transfers/aa/{aa_request_id}", user_id=customer(invocation)
    ).data
    if not isinstance(data, dict):
        raise ToolFailure(ErrorCode.PROTOCOL, "AA 收款详情响应不正确。")
    return data


def aa_collect(invocation, client):
    """解析一笔 AA 收款：总额、参与人、是否含我；只解析不发起，点击确认后才创建。

    发起只是生成收款请求（钱是向内收），不动我的资金，所以判定为黄标且不占当日累计；
    真正动钱的是每位参与者支付自己的份额（见 aa_pay）。
    """

    args = dict(invocation.call.arguments)
    total_minor = yuan_minor(args.get("amount_yuan"))
    names, include_me = _aa_participant_names(args)
    if not 2 <= len(names) + (1 if include_me else 0) <= AA_MAX_PARTICIPANTS:
        raise ToolFailure(
            ErrorCode.VALIDATION, f"AA 收款的参与人数须在 2 到 {AA_MAX_PARTICIPANTS} 之间。"
        )
    remark = args.get("remark") or None

    # 收款账户：钱收进来落到我名下的哪个活期账户，解析口径与转账的付款账户一致。
    from_hint = str(args.get("from_hint") or "").strip()
    from_account_id = str(args.get("from_account_id") or "").strip()
    writable = [
        account
        for account in load_accounts(invocation, client)
        if account.get("account_type") != "fixed" and account.get("status") == "active"
    ]
    if from_account_id:
        matches = [account for account in writable if account["account_id"] == from_account_id]
    elif from_hint:
        matches = [account for account in writable if _account_matches(account, from_hint)]
    else:
        matches = writable
    if not matches:
        raise ToolFailure(ErrorCode.NOT_FOUND, "没有找到符合条件的收款账户。")
    if len(matches) > 1:
        return _select(
            "from_account",
            "有多张账户符合条件，请选择收款账户。",
            [{"label": account_label(item), "value": item["account_id"]} for item in matches],
            {
                "total_amount_minor": total_minor,
                "participants": names,
                "include_me": include_me,
                "remark": remark,
            },
        )
    receiving = matches[0]

    # 参与人绑定：**只有精确同名**的唯一联系人候选才绑账户；模糊命中在这里宁可
    # 不绑 —— 把张三的模糊结果绑给李四，比「未绑定」糟糕得多。未绑定的参与人
    # 照样入列收款，只是不能经银行支付，卡片上如实展示。
    participants = []
    for name in names:
        bound = None
        found = load_contacts(invocation, client, name)
        exact = [contact for contact in found if contact.get("name") == name]
        usable = [contact for contact in exact if contact.get("recipient_account_id")]
        if len(usable) == 1 and usable[0]["recipient_account_id"] != receiving["account_id"]:
            # 绑到收款账户本人的联系人付不了款（银行拒绝对同一账户自转），退回未绑定。
            bound = usable[0]
        participants.append(
            {
                "name": name,
                "phone": bound.get("phone") if bound else None,
                "account_id": bound.get("recipient_account_id") if bound else None,
            }
        )
    if include_me:
        # 我的一份绑到「收款账户之外」的活期账户；名下没有第二个活期就只能未绑定。
        spare = [
            account for account in writable if account["account_id"] != receiving["account_id"]
        ]
        participants.append(
            {
                "name": "我",
                "phone": None,
                "account_id": spare[0]["account_id"] if spare else None,
            }
        )

    count = len(participants)
    amounts = _split_amount(total_minor, count)
    decision = classify_operation("aa_collect")

    lines = [
        f"请确认发起 AA 收款：总额 {format_minor(total_minor)}，{count} 人分摊，"
        f"收款账户 {account_label(receiving)}。"
    ]
    for participant, amount in zip(participants, amounts):
        if participant.get("account_id"):
            bound = f"（尾号 {participant['account_id'][-4:]}）"
        else:
            bound = "（未绑定收款账户）"
        lines.append(f"{participant['name']}{bound}：{format_minor(amount)}")
    if remark:
        lines.append(f"备注：{remark}")
    lines.append(f"风险等级：{decision.level.value}，确认卡片点一下即可发起。")
    lines.append("点击「确认」后才会生成收款请求；取消不会发起。")
    return ToolData(
        summary="\n".join(lines),
        display={"blocks": []},
        facts={
            "status": "ready",
            "operation": "aa_collect",
            "intent": {
                "operation": "aa_collect",
                "from_account_id": receiving["account_id"],
                "total_amount_minor": total_minor,
                "participants": participants,
                "remark": remark,
            },
            "from_label": account_label(receiving),
            "participant_count": count,
            "split_preview": [
                {"name": participant["name"], "amount_minor": amount}
                for participant, amount in zip(participants, amounts)
            ],
            "requires_code": decision.mfa_required,
            "risk_level": decision.level.value,
            "reason": decision.reason,
        },
    )


def aa_status(invocation, client):
    """查询一笔 AA 收款的分摊进度。银行没有列表端点，编号来自发起回执或用户原话。"""

    aa_request_id = str(invocation.call.arguments.get("aa_request_id") or "").strip()
    if not aa_request_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供 AA 收款编号。")
    detail = _load_aa(invocation, client, aa_request_id)

    # 标注「我的一份」需要账户列表；查不到也不影响进度本身，降级为不加标注。
    try:
        mine = {account["account_id"] for account in load_accounts(invocation, client)}
    except Exception:
        logger.warning("aa_status_accounts_failed user_id=%s", invocation.context.user_id)
        mine = set()

    status = detail.get("status") or "未知"
    total = detail.get("total_amount_minor")
    money = (
        format_minor(total) if isinstance(total, int) and not isinstance(total, bool) else "金额未知"
    )
    lines = [
        f"{aa_request_id}：总额 {money}，状态 {AA_STATUS_LABELS.get(status, status)}，"
        f"到期 {detail.get('expires_at') or '未知'}。"
    ]
    for item in detail.get("participants") or []:
        part_status = item.get("status") or "未知"
        amount = item.get("amount_minor")
        share = (
            format_minor(amount)
            if isinstance(amount, int) and not isinstance(amount, bool)
            else "金额未知"
        )
        mine_tag = "（我的一份）" if item.get("account_id") in mine else ""
        lines.append(
            f"第 {item.get('participant_index')} 位 {item.get('name')}{mine_tag}："
            f"{AA_PARTICIPANT_LABELS.get(part_status, part_status)} {share}"
        )
    return ToolData(
        summary="\n".join(lines),
        display={"blocks": []},
        facts={"aa": detail},
        business_ids={"aa_request_id": aa_request_id},
    )


def aa_pay(invocation, client, database=None):
    """解析支付我在某笔 AA 收款里的那一份：只解析不支付，点击确认后才走受控写。

    这是真实的资金写（钱从我的账户出去），所以与即时转账同一套日累计动态分级；
    `database` 的语义与 prepare 相同：读当日累计，读不到按超限 fail-closed。
    """

    args = dict(invocation.call.arguments)
    aa_request_id = str(args.get("aa_request_id") or "").strip()
    if not aa_request_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供 AA 收款编号。")
    detail = _load_aa(invocation, client, aa_request_id)
    status = detail.get("status")
    if status in ("cancelled", "expired", "completed"):
        raise ToolFailure(
            ErrorCode.INVALID_STATE,
            f"这笔 AA 收款{AA_STATUS_LABELS.get(status, status)}，不能再支付。",
        )

    writable = [
        account
        for account in load_accounts(invocation, client)
        if account.get("account_type") != "fixed" and account.get("status") == "active"
    ]
    by_id = {account["account_id"]: account for account in writable}
    receiving = detail.get("from_account_id")
    rows = [item for item in detail.get("participants") or [] if isinstance(item, dict)]
    mine = [
        item
        for item in rows
        if item.get("account_id") in by_id and item.get("account_id") != receiving
    ]
    if not mine:
        if any(item.get("account_id") == receiving for item in rows if item.get("account_id") in by_id):
            raise ToolFailure(
                ErrorCode.VALIDATION, "我的一份绑定在收款账户上，转出与转入账户不能相同，无法支付。"
            )
        raise ToolFailure(
            ErrorCode.NOT_FOUND, f"您不是 AA 收款 {aa_request_id} 的参与者，或未绑定可付账户。"
        )
    pending = [item for item in mine if item.get("status") == "pending"]
    if not pending:
        if any(item.get("status") == "paid" for item in mine):
            raise ToolFailure(ErrorCode.INVALID_STATE, "我的一份已经支付过了。")
        raise ToolFailure(ErrorCode.INVALID_STATE, "我的一份当前状态不允许支付。")

    chosen_index = args.get("participant_index")
    if chosen_index is not None:
        chosen = next(
            (item for item in pending if item.get("participant_index") == chosen_index), None
        )
        if chosen is None:
            raise ToolFailure(ErrorCode.VALIDATION, "指定的分摊项不是我的待付份额。")
    elif len(pending) > 1:
        return _select(
            "aa_participant",
            "有多笔我名下的待付份额，请选择要支付的一份。",
            [
                {
                    "label": (
                        f"{item.get('name')} · {account_label(by_id[item['account_id']])} · "
                        f"{format_minor(item['amount_minor'])}"
                    ),
                    "value": item["participant_index"],
                }
                for item in pending
            ],
            {"aa_request_id": aa_request_id},
        )
    else:
        chosen = pending[0]

    amount_minor = chosen.get("amount_minor")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ToolFailure(ErrorCode.PROTOCOL, "分摊金额响应不正确。")
    payer = by_id[chosen["account_id"]]
    if payer.get("available_minor", 0) < amount_minor:
        raise ToolFailure(ErrorCode.INSUFFICIENT_BALANCE, f"{account_label(payer)}的可用余额不足。")

    accumulated_minor = 0
    if database is not None:
        try:
            with database.sessions() as session:
                accumulated_minor = DailyTotalStore.get_accumulated_minor(
                    session, invocation.context.user_id, SystemClock.today()
                )
        except Exception:
            # 读不到当日累计就按「已超限」判红：宁可多要一次验证码，不可漏判。
            accumulated_minor = DAILY_RED_THRESHOLD_MINOR + 1
            logger.warning(
                "transfer_daily_total_read_failed user_id=%s", invocation.context.user_id
            )
    decision = classify_operation(
        "aa_payment",
        amount_minor=amount_minor,
        daily_accumulated_minor=accumulated_minor,
        side_effect=SideEffect.WRITE,
    )
    requires_code = decision.mfa_required
    if requires_code and database is not None:
        try:
            AuditService(database).record_business_event(
                action="transfer_aa_pay",
                resource_type="policy_decision",
                resource_id=decision.level.value,
                result="ok",
                reason=decision.reason,
                context=AuditContext(user_id=invocation.context.user_id),
            )
        except Exception:
            # 留痕失败不能拖垮这张确认卡；日志是这里唯一还能留下的信号。
            logger.warning(
                "transfer_policy_audit_failed user_id=%s level=%s",
                invocation.context.user_id,
                decision.level.value,
            )

    receiving_tail = receiving[-4:] if isinstance(receiving, str) else "未知"
    remark = detail.get("remark")
    lines = [
        f"请确认支付 AA 分摊：{format_minor(amount_minor)}，从{account_label(payer)}"
        f"支付到对方收款账户（尾号 {receiving_tail}），AA 编号 {aa_request_id}。"
    ]
    if remark:
        lines.append(f"备注：{remark}")
    if requires_code:
        lines.append(f"需要验证码：{decision.reason}")
    else:
        lines.append(f"风险等级：{decision.level.value}，确认卡片点一下即可支付。")
    lines.append("点击「确认」后才会支付；取消不会支付。")
    return ToolData(
        summary="\n".join(lines),
        display={"blocks": []},
        facts={
            "status": "ready",
            "operation": "aa_payment",
            "intent": {
                "operation": "aa_payment",
                "aa_request_id": aa_request_id,
                "participant_index": chosen["participant_index"],
                "amount_minor": amount_minor,
                "from_account_id": payer["account_id"],
            },
            "from_label": account_label(payer),
            "aa": {
                "remark": remark,
                "total_amount_minor": detail.get("total_amount_minor"),
                "participant_count": detail.get("participant_count"),
            },
            "requires_code": requires_code,
            "risk_level": decision.level.value,
            "reason": decision.reason,
        },
    )


def aa_cancel(invocation, client):
    """取消一笔由我发起的 AA 收款。图上把它拦成确认卡之后才会被调用。"""

    aa_request_id = str(invocation.call.arguments.get("aa_request_id") or "").strip()
    if not aa_request_id:
        raise ToolFailure(ErrorCode.VALIDATION, "请提供要取消的 AA 收款编号。")
    response = cancel_aa(
        client, invocation.context, aa_request_id, idempotency_key=invocation.call.idempotency_key
    )
    return ToolData(
        summary=f"已取消 AA 收款 {aa_request_id}，未支付的份额转为已拒绝。",
        display={"blocks": []},
        facts={"aa": response.data},
        business_ids={"aa_request_id": aa_request_id},
    )


def spec(name, description, properties=None, required=(), *, write=False):
    """One declared tool. No output_schema: transfer_prepare's facts have two shapes."""

    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }
    return ToolSpec(
        name=f"transfer_{name}",
        description=f"{description}。参数 JSON Schema：{json.dumps(schema, ensure_ascii=False)}",
        parameters=schema,
        side_effect=SideEffect.WRITE if write else SideEffect.READ,
    )


SPECS = [
    spec("accounts", "查询本人银行账户、余额与状态"),
    spec(
        "contacts",
        "按姓名、手机号或备注搜索收款人；不提供条件时列出全部收款人",
        {"query": {"type": "string", "maxLength": 100}},
    ),
    spec(
        "scheduled",
        "查询定时转账列表",
        {"status": {"type": "string", "enum": list(SCHEDULED_STATUSES)}},
    ),
    spec(
        "prepare",
        "解析转账意图：金额、收款人、付款账户；只解析不发起，代码会要求用户确认",
        {
            "amount_yuan": YUAN,
            "to_name": {"type": "string", "minLength": 1, "maxLength": 64},
            "from_hint": {"type": "string", "maxLength": 64},
            "from_account_id": {"type": "string", "maxLength": 64},
            "to_account_id": {"type": "string", "maxLength": 64},
            "remark": {"type": "string", "maxLength": 200},
        },
        ("amount_yuan", "to_name"),
    ),
    spec(
        "schedule",
        "解析定时转账意图：金额、收款人、执行日期与时间；只解析不创建，代码会要求用户确认",
        {
            "amount_yuan": YUAN,
            "to_name": {"type": "string", "minLength": 1, "maxLength": 64},
            "execute_date": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "description": "执行日期，可用 2026-11-01、明天、后天 等写法",
            },
            "execute_time": {
                "type": "string",
                "maxLength": 5,
                "description": "执行时间 HH:MM，默认 09:00",
            },
            "from_hint": {"type": "string", "maxLength": 64},
            "from_account_id": {"type": "string", "maxLength": 64},
            "to_account_id": {"type": "string", "maxLength": 64},
            "remark": {"type": "string", "maxLength": 200},
        },
        ("amount_yuan", "to_name", "execute_date"),
    ),
    spec(
        "cancel",
        "取消一笔已存在的定时转账，仅供办理流程调用",
        {"task_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        ("task_id",),
        write=True,
    ),
    spec(
        "aa",
        "解析 AA 收款意图：收取总额、参与人、是否含我自己；只解析不发起，代码会要求用户确认",
        {
            "amount_yuan": YUAN,
            "participants": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "maxItems": 20,
                "description": "参与人姓名列表；用户只说人数时改用 participant_count",
            },
            "participant_count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 20,
                "description": "总人数（含发起人自己），仅在用户没给出姓名时使用",
            },
            "include_me": {"type": "boolean", "description": "发起人自己也占一份"},
            "from_hint": {"type": "string", "maxLength": 64},
            "from_account_id": {"type": "string", "maxLength": 64},
            "remark": {"type": "string", "maxLength": 200},
        },
        ("amount_yuan",),
    ),
    spec(
        "aa_status",
        "查询一笔 AA 收款的分摊进度（编号在发起成功的回复里）",
        {"aa_request_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        ("aa_request_id",),
    ),
    spec(
        "aa_pay",
        "解析支付我在某笔 AA 收款中的那一份；只解析不支付，代码会要求用户确认",
        {
            "aa_request_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "participant_index": {"type": "integer", "minimum": 0},
        },
        ("aa_request_id",),
    ),
    spec(
        "aa_cancel",
        "取消一笔由我发起的 AA 收款，仅供办理流程调用",
        {"aa_request_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        ("aa_request_id",),
        write=True,
    ),
]

TOOLS = {item.name: item for item in SPECS}
HANDLERS = {
    "transfer_accounts": accounts,
    "transfer_contacts": contacts,
    "transfer_scheduled": scheduled,
    "transfer_prepare": prepare,
    "transfer_schedule": schedule_prepare,
    "transfer_cancel": cancel,
    "transfer_aa": aa_collect,
    "transfer_aa_status": aa_status,
    "transfer_aa_pay": aa_pay,
    "transfer_aa_cancel": aa_cancel,
}
