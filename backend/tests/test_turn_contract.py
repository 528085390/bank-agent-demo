"""The whole-turn contract: what a module returns when it answers a turn by itself.

The other shape a module can have is tools: the base runs one call and reads its envelope.
This file pins the second shape -- its status vocabulary, and the ways a TurnResult can be
malformed at construction time.
"""

import pytest

from app.agents.turn import TERMINAL_TURN_STATUSES, TurnConfirmation, TurnResult, TurnStatus

# A genuinely valid card: every required field is present, and the labels/code defaults are the
# ones a module leaves alone. Spelled out rather than imported from a module, because this file
# owns the contract, not any one module's preview.
CONFIRMATION = TurnConfirmation(
    pending_id="wp_0001",
    operation="申购",
    title="确认申购",
    amount_minor=100000,
    product_name="演示灵活理财",
    account_display="活期账户 ****8801",
)


def test_the_terminal_statuses_are_plain_strings():
    """_already_concluded tests item["turn_status"] in TERMINAL_TURN_STATUSES.

    The values that flow through state are .value strings. A StrEnum member is a str
    subclass, so isinstance and hashing both accept it: only an exact-type check tells
    the shipped strings apart from the members a frozenset(TurnStatus) would hold.

    `unknown` is in the set on purpose: a module that reports an unresolved side effect has
    still concluded its turn, so the Supervisor must not send the same request back to it.
    """

    assert TERMINAL_TURN_STATUSES == {
        "completed",
        "waiting_input",
        "waiting_confirmation",
        "unsupported",
        "failed",
        "unknown",
    }
    assert all(type(item) is str for item in TERMINAL_TURN_STATUSES)


def test_unknown_is_a_status_of_its_own_and_not_failed():
    """结果未知不能被折叠成失败：它既不是成功，也不是"没办成"。

    这条断言存在的意义是钉住一个资金安全性质：模块报告"有副作用的请求结果不明"时，
    基座读到的状态必须是 unknown。少了它，模块只能塞进 FAILED，而 Supervisor 的模型与
    任何按状态分支的调用方都会把它读成一次失败。
    """

    assert TurnStatus.UNKNOWN is not TurnStatus.FAILED
    assert TurnStatus.UNKNOWN.value == "unknown"
    assert TurnStatus("unknown") is TurnStatus.UNKNOWN


def test_a_turn_result_rejects_a_blank_summary():
    with pytest.raises(ValueError):
        TurnResult(status=TurnStatus.COMPLETED, summary="")


def test_a_turn_result_rejects_a_status_that_is_not_a_turn_status():
    with pytest.raises(TypeError):
        TurnResult(status="completed", summary="办完了")


def test_a_turn_result_rejects_facts_that_are_not_a_dict():
    with pytest.raises(TypeError):
        TurnResult(status=TurnStatus.COMPLETED, summary="办完了", facts=[])


def test_a_turn_result_rejects_business_ids_that_are_not_a_dict():
    with pytest.raises(TypeError):
        TurnResult(status=TurnStatus.COMPLETED, summary="办完了", business_ids=[])


def test_a_waiting_confirmation_without_a_card_is_refused_at_construction():
    """等待点击却什么都不展示，是一个点不出结果的按钮：构造期就拒绝。

    这是资金安全性质，不是措辞问题：确认框是用户唯一能看清"确认的是哪一笔"的地方。少了它，
    页面会渲染一个没有操作、没有金额、没有账户的按钮，用户按下去等于授权一笔自己看不见的
    交易。守卫必须在模块构造 TurnResult 时就失败，而不是在用户面前失败。
    """

    with pytest.raises(ValueError, match="needs a confirmation"):
        TurnResult(status=TurnStatus.WAITING_CONFIRMATION, summary="请确认。")


def test_only_waiting_confirmation_may_carry_a_confirmation():
    """非等待状态带上确认框，是一个永远解不开的按钮：它对应的暂停根本不存在。

    同样关乎资金安全：卡片只能出现在 WAITING_CONFIRMATION 这一种结局里。其它状态带卡片，
    说明调用方把"给用户看哪一笔"和"这一笔是否正在等待授权"混在了一起——必须构造期拒绝。
    """

    with pytest.raises(ValueError, match="only waiting_confirmation may carry a confirmation"):
        TurnResult(status=TurnStatus.COMPLETED, summary="办完了", confirmation=CONFIRMATION)


def test_a_card_may_declare_that_it_has_no_amount():
    """没有金额的操作（账单发邮件）就传 None，不必拿 1 分冒充。

    以前这里只接受正整数分，模块只好塞一个假金额，卡面于是印出一行与操作无关的
    「¥0.01」。金额不参与授权（参数以服务端快照为准），所以「没有」可以直说。
    """

    card = TurnConfirmation(
        pending_id="bill-report-abc",
        operation="发送账单报告",
        title="确认发送账单报告",
        amount_minor=None,
        product_name="账单深度分析报告",
        account_display="someone@example.com",
    )

    assert card.as_payload()["amount_minor"] is None


@pytest.mark.parametrize("amount", [0, -1, True, "100"])
def test_a_present_amount_must_still_be_a_positive_integer(amount):
    """放开 None 不等于放开金额本身：写出来的金额仍然必须是正整数分。"""

    with pytest.raises(ValueError):
        TurnConfirmation(
            pending_id="wp_0001",
            operation="申购",
            title="确认申购",
            amount_minor=amount,
            product_name="演示灵活理财",
            account_display="活期账户 ****8801",
        )


def test_a_waiting_confirmation_with_a_card_is_accepted():
    """正例：守卫不能过宽——字段齐全的确认框必须能构造出来。

    没有这条，上面两条的拒绝可能来自一个把合法输入也挡掉的过宽守卫，而不是那两条不变量；
    它同时证明 CONFIRMATION 这个 fixture 本身是真实的、能通过构造期校验的。
    """

    result = TurnResult(
        status=TurnStatus.WAITING_CONFIRMATION,
        summary="请确认这笔申购。",
        confirmation=CONFIRMATION,
    )
    assert result.confirmation is CONFIRMATION
