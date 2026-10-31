"""card / subscription 的确认卡与统一策略引擎同一口径（Task 6，Ruling R7）。

两个模块的确认卡都是纯函数，这里直接单测 `authorization_question`：不起图、不调模型、
不碰银行网络。唯一需要银行的地方是 subscription 的 `prepare`，它只读一次
`GET /subscriptions`，用的是测试自己的 FakeBankClient。
"""

from app.modules.card.workflow import authorization_question as card_question
from app.modules.subscription.tools import prepare as subscription_prepare
from app.modules.subscription.workflow import authorization_question as subscription_question
from app.policy import classify_operation
from app.tools.protocol import SideEffect, ToolCall, ToolInvocation, TrustedContext
from tests.support.domain_harness import FakeBankClient

CARD = {
    "card_id": "card_private",
    "card_type": "debit",
    "card_number": "6222 **** **** 8801",
    "status": "active",
    "freeze_reason": None,
    "daily_limit_minor": 500000,
    "monthly_limit_minor": 5000000,
    "merchant_blacklist": [],
}

SUBSCRIPTION = {
    "subscription_id": "sub_1",
    "user_id": "u_zhang",
    "from_account_id": "acc_1",
    "name": "视频会员",
    "merchant_name": "某视频",
    "amount_minor": 2500,
    "frequency": "monthly",
    "start_date": "2026-01-01",
    "next_due_date": "2026-11-01",
    "status": "active",
    "auto_renew": True,
    "last_charge_id": None,
}


def card_action(operation, *, permission_level="yellow", parameters=None):
    """One prepared card action, in the shape `POST /cards/actions` answers with."""

    return {
        "action_id": "CA-" + "a" * 32,
        "operation": operation,
        "label": "银行卡操作",
        "status": "pending",
        "before": CARD,
        "parameters": parameters or {"card_id": CARD["card_id"]},
        "permission_level": permission_level,
        "expires_at": "2030-01-01T00:00:00+00:00",
    }


def subscription_action(*, amount_minor, operation="charge"):
    """The frozen snapshot `prepare` produces, read off the tool rather than hand-built."""

    bank = FakeBankClient()
    bank.register("/subscriptions", [{**SUBSCRIPTION, "amount_minor": amount_minor}])
    data = subscription_prepare(
        ToolInvocation(
            agent_id="subscription",
            call=ToolCall(
                tool_name=f"subscription_{operation}",
                arguments={"subscription_id": "sub_1"},
                idempotency_key="idem-alignment",
            ),
            context=TrustedContext(
                user_id="user_0001", session_id="sess_sub", bank_customer_id="u_zhang"
            ),
        ),
        bank,
    )
    return data.facts["action"]


def test_an_unknown_card_operation_is_tightened_to_red_although_the_bank_says_yellow():
    """Fail-Closed 只能单向收紧：银行说黄、本地引擎判红，卡片就必须收验证码。"""

    assert classify_operation("card_unknown_op", side_effect=SideEffect.WRITE).mfa_required is True

    question = card_question(card_action("unknown_op", permission_level="yellow"))

    assert question["confirmation"]["requires_code"] is True
    # 银行自己的分级仍然原样透传：收紧不改变「银行怎么说」这个事实。
    assert question["confirmation"]["permission_level"] == "yellow"


def test_the_card_confirmation_follows_the_engine_for_red_and_yellow_operations():
    """freeze 红、update_limit 黄：本地判定与银行一致的那些操作，真实行为不变。"""

    assert classify_operation("card_freeze", side_effect=SideEffect.WRITE).mfa_required is True
    frozen = card_question(card_action("freeze", permission_level="red"))
    assert frozen["confirmation"]["requires_code"] is True

    # 银行对 freeze 自己的分级是 yellow（bank_backend/app/domain/card_action_service.py:293-295
    # 只把 report_loss 抬到 red），引擎对 card_freeze 是固有红：这一处正是本次的单向收紧。
    assert card_question(card_action("freeze"))["confirmation"]["requires_code"] is True

    raised = {
        "card_id": CARD["card_id"],
        "daily_limit_minor": 100001,
        "monthly_limit_minor": CARD["monthly_limit_minor"],
    }
    assert (
        classify_operation("card_update_limit", side_effect=SideEffect.WRITE).mfa_required is False
    )
    assert (
        card_question(card_action("update_limit", parameters=raised))["confirmation"][
            "requires_code"
        ]
        is False
    )


def test_a_large_subscription_charge_is_red_on_the_card_when_no_database_is_bound():
    """R7：订阅只统一口径、不加门禁 —— 本地判红也不能把确认点击变成 422 死路。

    这条钉的是**没有数据库**的模块：当日累计无从得知，于是只按金额判定，红标写在卡上、
    点击仍可继续（订阅没有验证码通道，硬加门禁就是一条点不过的 422 死路）。组合根总会
    把 database 绑进来（`app/modules/subscription/__init__.py` 的 `bind_subscription`），
    那条路径下这种卡会被 `prepare` 当场拒绝 —— 见 `tests/test_subscription_fund_write.py`
    的 `test_a_charge_that_would_cross_the_cap_is_refused_at_the_card`。
    """

    action = subscription_action(amount_minor=120000)

    assert action["operation"] == "charge"
    assert action["risk_level"] == "red"
    assert "超过日限额" in action["reason"]

    question = subscription_question(action)

    assert action["reason"] in question["display"]
    assert question["confirmation"]["requires_code"] is False


def test_a_small_subscription_charge_stays_yellow_and_the_threshold_is_strict():
    """projected = 0 + amount，严格大于 DAILY_RED_THRESHOLD_MINOR(100000) 才红。"""

    assert subscription_action(amount_minor=60000)["risk_level"] == "yellow"
    # 边界钉住：正好 100000 分仍是黄，不是红。
    assert subscription_action(amount_minor=100000)["risk_level"] == "yellow"
