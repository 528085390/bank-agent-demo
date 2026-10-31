from dataclasses import FrozenInstanceError

import pytest

from app.modules import ALL_MODULES
from app.policy import (
    DAILY_RED_THRESHOLD_MINOR,
    TOOL_TO_OPERATION,
    PolicyDecision,
    RiskLevel,
    classify_operation,
    requires_code,
)
from app.policy.evaluator import FUND_WRITE_OPERATIONS, INTRINSIC_YELLOW_OPERATIONS
from app.tools.protocol import SideEffect


def test_risk_level_enum():
    assert RiskLevel.GREEN == "green"
    assert RiskLevel.YELLOW == "yellow"
    assert RiskLevel.RED == "red"
    assert isinstance(RiskLevel.GREEN, str)


def test_policy_decision_dataclass():
    decision = PolicyDecision(
        level=RiskLevel.GREEN,
        confirm_required=False,
        mfa_required=False,
        reason="测试原因",
    )
    assert decision.level == RiskLevel.GREEN
    assert decision.confirm_required is False
    assert decision.mfa_required is False
    assert decision.reason == "测试原因"

    with pytest.raises(FrozenInstanceError):
        decision.confirm_required = True  # type: ignore


def test_requires_code():
    assert requires_code(RiskLevel.RED) is True
    assert requires_code("red") is True
    assert requires_code("RED") is True
    assert requires_code(RiskLevel.YELLOW) is False
    assert requires_code("yellow") is False
    assert requires_code(RiskLevel.GREEN) is False
    assert requires_code("green") is False
    assert requires_code("unknown") is False


@pytest.mark.parametrize(
    "op",
    ["card_loss", "card_freeze", "wealth_purchase", " CARD_LOSS ", "Card_Freeze"],
)
def test_classify_operation_intrinsic_red(op):
    decision = classify_operation(op)
    assert decision.level == RiskLevel.RED
    assert decision.confirm_required is True
    assert decision.mfa_required is True
    assert "高风险" in decision.reason


@pytest.mark.parametrize(
    "tool_name",
    [
        "card_report_loss",
        "card_freeze",
        "wealth_order_purchase",
    ],
)
def test_classify_operation_real_tool_names_intrinsic_red(tool_name):
    decision = classify_operation(tool_name)
    assert decision.level == RiskLevel.RED
    assert decision.confirm_required is True
    assert decision.mfa_required is True


@pytest.mark.parametrize(
    "op",
    [
        "card_limit_modify",
        "card_restriction",
        "card_unfreeze",
        "card_unloss",
        "card_apply",
        "subscription_cancel",
        "subscription_pause",
        "subscription_resume",
        "wealth_redeem",
        "card_cancel",
    ],
)
def test_classify_operation_intrinsic_yellow(op):
    decision = classify_operation(op)
    assert decision.level == RiskLevel.YELLOW
    assert decision.confirm_required is True
    assert decision.mfa_required is False
    assert "配置变更" in decision.reason or "确认" in decision.reason


@pytest.mark.parametrize(
    "tool_name",
    [
        "card_update_limit",
        "card_update_restrictions",
        "card_release_loss",
        "card_cancel",
        "wealth_order_redeem",
        "subscription_cancel",
        "transfer_cancel",
    ],
)
def test_classify_operation_real_tool_names_intrinsic_yellow(tool_name):
    decision = classify_operation(tool_name)
    assert decision.level == RiskLevel.YELLOW
    assert decision.confirm_required is True
    assert decision.mfa_required is False


@pytest.mark.parametrize(
    "op",
    [
        "transfer",
        "transfer_execute",
        "birthday_order",
        "life_prepare_order",
        "subscription_charge",
    ],
)
def test_classify_operation_fund_write_thresholds(op):
    # 500 元未超限 -> 黄标
    dec_under = classify_operation(op, amount_minor=50000, daily_accumulated_minor=0)
    assert dec_under.level == RiskLevel.YELLOW
    assert dec_under.confirm_required is True
    assert dec_under.mfa_required is False
    assert "常规资金变动" in dec_under.reason

    # 1000 元整未超限 -> 黄标
    dec_exact = classify_operation(op, amount_minor=100000, daily_accumulated_minor=0)
    assert dec_exact.level == RiskLevel.YELLOW
    assert dec_exact.mfa_required is False

    # 1000.01 元超限 -> 红标
    dec_over = classify_operation(op, amount_minor=100001, daily_accumulated_minor=0)
    assert dec_over.level == RiskLevel.RED
    assert dec_over.confirm_required is True
    assert dec_over.mfa_required is True
    assert "超过日限额" in dec_over.reason

    # 累计超限 -> 红标
    dec_accum = classify_operation(op, amount_minor=1000, daily_accumulated_minor=100000)
    assert dec_accum.level == RiskLevel.RED
    assert dec_accum.confirm_required is True
    assert dec_accum.mfa_required is True


def test_transfer_prepare_as_read_tool_is_green():
    """N2 修复：transfer_prepare 是 READ 意图解析工具，绝不能当成资金写操作弹确认卡。"""
    decision = classify_operation("transfer_prepare", side_effect=SideEffect.READ)
    assert decision.level == RiskLevel.GREEN
    assert decision.confirm_required is False
    assert decision.mfa_required is False


def test_card_cancel_write_tool_is_yellow():
    """N3 修复：card_cancel (撤销发卡申请) 属于配置变更，走黄色确认卡，不触发红色验证码。"""
    decision = classify_operation("card_cancel", side_effect=SideEffect.WRITE)
    assert decision.level == RiskLevel.YELLOW
    assert decision.confirm_required is True
    assert decision.mfa_required is False


def test_classify_operation_fail_closed_on_unknown_write():
    # 核心安全测试：未知操作 + side_effect=WRITE -> 强制 RED
    decision = classify_operation("unknown_fund_drain", amount_minor=100, side_effect=SideEffect.WRITE)
    assert decision.level == RiskLevel.RED
    assert decision.confirm_required is True
    assert decision.mfa_required is True
    assert "未知写操作" in decision.reason


def test_classify_operation_fail_closed_on_unspecified_unknown():
    # 未知操作且未指定 side_effect -> 强制 RED 兜底
    decision = classify_operation("some_unregistered_dangerous_action")
    assert decision.level == RiskLevel.RED
    assert decision.confirm_required is True
    assert decision.mfa_required is True
    assert "安全防御阻断" in decision.reason


def test_aa_payment_is_back_in_the_fund_write_vocabulary_now_that_it_has_a_tool():
    """词表与能力对齐：`transfer_aa_pay` 已交付，`aa_payment` 回到资金写词表。

    2026-10-23 它因「仓库里没有 AA 收款的工具」被移出（当时的测试断言 `not in`）；
    2026-10-30 AA 分摊支付接入，承诺兑现。动态口径与即时转账一致：单笔或单笔+当日
    累计合计越过 1000 元就升红要码，否则黄标确认卡；发起 AA（`aa_collect`）只生成
    收款请求不动钱，是内在黄标且不进资金写词表。
    """

    assert "aa_payment" in FUND_WRITE_OPERATIONS
    assert TOOL_TO_OPERATION["transfer_aa_pay"] == "aa_payment"
    assert TOOL_TO_OPERATION["transfer_aa_cancel"] == "aa_cancel"
    # 解析/查询工具刻意不映射 —— 同 transfer_prepare，卡片由图挂，不进策略词表。
    assert "transfer_aa" not in TOOL_TO_OPERATION
    assert "transfer_aa_status" not in TOOL_TO_OPERATION
    assert "aa_collect" in INTRINSIC_YELLOW_OPERATIONS
    assert "aa_collect" not in FUND_WRITE_OPERATIONS

    under = classify_operation("aa_payment", amount_minor=100000, daily_accumulated_minor=0)
    assert under.level == RiskLevel.YELLOW
    assert under.mfa_required is False

    over = classify_operation("aa_payment", amount_minor=100001, daily_accumulated_minor=0)
    assert over.level == RiskLevel.RED
    assert over.mfa_required is True

    accumulated = classify_operation(
        "aa_payment", amount_minor=50000, daily_accumulated_minor=51000
    )
    assert accumulated.level == RiskLevel.RED
    assert accumulated.mfa_required is True


def test_classify_operation_read_allows_green():
    # 未知操作但是声明为只读 READ -> 放行 GREEN
    decision = classify_operation("some_safe_query", side_effect=SideEffect.READ)
    assert decision.level == RiskLevel.GREEN
    assert decision.confirm_required is False
    assert decision.mfa_required is False
    assert "只读查询" in decision.reason


def test_classify_operation_clamp_negative():
    decision = classify_operation("transfer", amount_minor=-500, daily_accumulated_minor=-1000)
    assert decision.level == RiskLevel.YELLOW
    assert decision.confirm_required is True
    assert decision.mfa_required is False


def test_daily_red_threshold_constant():
    assert DAILY_RED_THRESHOLD_MINOR == 100000


def test_all_declared_write_tools_never_fail_open_green():
    """跨模块契约断言：所有在系统注册的 side_effect==WRITE 的工具，策略引擎绝不可判为 GREEN！"""
    for module in ALL_MODULES:
        for tool_name, spec in module.tools.items():
            if spec.side_effect == SideEffect.WRITE:
                decision = classify_operation(tool_name, amount_minor=0, side_effect=spec.side_effect)
                assert decision.level in (RiskLevel.YELLOW, RiskLevel.RED), (
                    f"Write tool {tool_name} in module {module.name} was classified as GREEN! Security breach."
                )
                assert decision.confirm_required is True
