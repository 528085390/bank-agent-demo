from __future__ import annotations

from app.policy.types import DAILY_RED_THRESHOLD_MINOR, PolicyDecision, RiskLevel
from app.tools.protocol import SideEffect

# 1. 固有红标敏感操作表（任何金额均红标）
INTRINSIC_RED_OPERATIONS = frozenset(
    {
        "card_loss",
        "card_password_modify",
        "card_freeze",
        "wealth_purchase",
    }
)

# 2. 资金变动类操作表（按 单笔 + 当日累计 判定）
#
# 只列**已经有实现、并且真的走写门面记账**的操作。词表写在这里就等于承诺它受日限额管，
# 所以没接的工具不进表：`aa_payment` 曾在这里，但仓库里一直没有 AA 收款的工具，
# 2026-10-23 移除；2026-10-30 `transfer_aa_pay` 落地（AA 分摊支付真实扣款，
# 经 `writes.pay_aa` 走门面记日累计），兑现承诺重新入表。
FUND_WRITE_OPERATIONS = frozenset(
    {
        "transfer",
        "birthday_order",
        "subscription_charge",
        "aa_payment",
    }
)

# 3. 固有黄标配置类操作（需用户确认但免验证码）
INTRINSIC_YELLOW_OPERATIONS = frozenset(
    {
        "card_limit_modify",
        "card_restriction",
        "card_unfreeze",
        "card_unloss",
        "card_apply",
        "subscription_cancel",
        "subscription_pause",
        "subscription_resume",
        "wealth_redeem",
        "transfer_cancel",
        "birthday_cancel_order",
        "aa_collect",  # 发起 AA 只生成收款请求（钱向内收），不动发起人资金，不占日累计
        "aa_cancel",
        "card_cancel",  # 撤销申请属于配置变更，判定为黄标
    }
)

# 工具名到策略操作名的映射表
TOOL_TO_OPERATION: dict[str, str] = {
    # 转账（注意：transfer_prepare / transfer_schedule / transfer_aa 是 READ 意图解析工具，
    # transfer_aa_status 是 READ 查询工具，均不在此处映射为资金写操作）
    "transfer": "transfer",
    "transfer_execute": "transfer",
    "transfer_cancel": "transfer_cancel",
    "transfer_aa_pay": "aa_payment",
    "transfer_aa_cancel": "aa_cancel",
    # 银行卡
    "card_report_loss": "card_loss",
    "card_change_password": "card_password_modify",
    "card_loss": "card_loss",
    "card_freeze": "card_freeze",
    "card_update_limit": "card_limit_modify",
    "card_limit_modify": "card_limit_modify",
    "card_update_restrictions": "card_restriction",
    "card_restriction": "card_restriction",
    "card_unfreeze": "card_unfreeze",
    "card_release_loss": "card_unloss",
    "card_unloss": "card_unloss",
    "card_apply": "card_apply",
    "card_execute": "card_apply",
    "card_cancel": "card_cancel",
    # 理财
    "wealth_purchase": "wealth_purchase",
    "wealth_order_purchase": "wealth_purchase",
    "wealth_redeem": "wealth_redeem",
    "wealth_order_redeem": "wealth_redeem",
    # 订阅
    "subscription_cancel": "subscription_cancel",
    "subscription_pause": "subscription_pause",
    "subscription_resume": "subscription_resume",
    "subscription_charge": "subscription_charge",
    "subscription_execute": "subscription_charge",
    # 生活事件
    "life_prepare_order": "birthday_order",
    "life_execute_order": "birthday_order",
    "birthday_order": "birthday_order",
    "life_cancel_order": "birthday_cancel_order",
}


def classify_operation(
    operation: str,
    amount_minor: int = 0,
    daily_accumulated_minor: int = 0,
    side_effect: SideEffect | str | None = None,
) -> PolicyDecision:
    """根据操作类型、单笔金额、当日已累计金额及副作用做出分级决策。

    安全设计准则：
    1. 真实工具名自动通过 TOOL_TO_OPERATION 归一化；
    2. 资金类操作根据日累计动态升降级；
    3. 未知写操作 (side_effect == WRITE) 或未声明副作用的未知操作一律 Fail-Closed 判定为 RED，防范绕过；
    4. 纯读工具 (side_effect == READ) 不拦截为写操作。
    """
    raw_op = (operation or "").strip().lower()
    amt = max(0, int(amount_minor or 0))
    accumulated = max(0, int(daily_accumulated_minor or 0))

    op = TOOL_TO_OPERATION.get(raw_op, raw_op)

    # 1. 固有红标
    if op in INTRINSIC_RED_OPERATIONS:
        return PolicyDecision(
            level=RiskLevel.RED,
            confirm_required=True,
            mfa_required=True,
            reason=f"高风险敏感操作 [{raw_op}] 强制执行红标验证码安全核验",
        )

    # 2. 资金类写操作（动态累计升降级）
    if op in FUND_WRITE_OPERATIONS:
        projected = accumulated + amt
        if projected > DAILY_RED_THRESHOLD_MINOR:
            return PolicyDecision(
                level=RiskLevel.RED,
                confirm_required=True,
                mfa_required=True,
                reason=(
                    f"当日累计金额达到 ¥{projected / 100:.2f} "
                    f"（超过日限额 ¥{DAILY_RED_THRESHOLD_MINOR / 100:.2f}），升级为红标验证码核验"
                ),
            )
        return PolicyDecision(
            level=RiskLevel.YELLOW,
            confirm_required=True,
            mfa_required=False,
            reason=f"常规资金变动操作 [{raw_op}]，需要用户卡片确认",
        )

    # 3. 固有黄标
    if op in INTRINSIC_YELLOW_OPERATIONS:
        return PolicyDecision(
            level=RiskLevel.YELLOW,
            confirm_required=True,
            mfa_required=False,
            reason=f"配置变更操作 [{raw_op}]，需要用户卡片确认",
        )

    # 4. 未知操作：依据 side_effect 实施 Fail-Closed 防御
    effect_str = str(side_effect).upper() if side_effect is not None else None
    if effect_str == SideEffect.WRITE.value or effect_str == "WRITE":
        return PolicyDecision(
            level=RiskLevel.RED,
            confirm_required=True,
            mfa_required=True,
            reason=f"未知写操作 [{raw_op or 'empty'}] 触发安全熔断防御，强制升级为红标验证码核验",
        )

    if effect_str == SideEffect.READ.value or effect_str == "READ":
        return PolicyDecision(
            level=RiskLevel.GREEN,
            confirm_required=False,
            mfa_required=False,
            reason=f"只读查询操作 [{raw_op or 'empty'}]，自动放行",
        )

    # 既不在白名单，又未明确声明为 READ 的操作：严格 Fail-Closed 兜底
    if not raw_op:
        return PolicyDecision(
            level=RiskLevel.GREEN,
            confirm_required=False,
            mfa_required=False,
            reason="空操作放行",
        )

    return PolicyDecision(
        level=RiskLevel.RED,
        confirm_required=True,
        mfa_required=True,
        reason=f"未声明副作用且未在白名单的未知操作 [{raw_op}]，触发安全防御阻断并强制红标",
    )


def requires_code(level: RiskLevel | str) -> bool:
    """当前等级是否要求验证码。"""
    val = level.value if isinstance(level, RiskLevel) else str(level)
    return val.strip().lower() == RiskLevel.RED.value
