"""任务4：点击确认协议（验收矩阵 C01–C11）。

这一层只证明**接收端**：理财怎么接收一个可信的确认事件、怎么把一次确认变成一笔（且只有一笔）
资金写、以及在不具备条件时怎么如实拒绝。全部离线：HTTP 传输替身 + ScriptModel。

三件必须分清楚的事：

1. **确认不是文本。** 聊天里的"确认""已确认，直接买"都是 `kind=message`，模型也不能铸造确认。
   唯一能提交资金的入口是 `kind=confirm` 加一个运行时签发的 `pending_id`。
2. **确认不等于成交。** 当前银行要求验证码（D2），所以本仓库的 `HttpWealthBank` 在发出任何资金
   请求之前就返回能力不兼容；这条分支用 `reason_code=confirmation_transport_unsupported` 标记，
   不是失败原因不明的错误，也不是"已经成交"。
3. **未知不是失败。** 一次结果未知的写既不说成功也不说失败，只能用 `kind=reconcile` 按原幂等键
   读一次银行核实；绝不能把它映射成 failed 之后声称交易失败。

成功路径用 `support.ClickConfirmationBank`（声明支持点击确认的适配器替身）。它**不证明当前银行
兼容**：C02 与 C03 必须分开断言，两者不能互相替代。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import httpx
import pytest

from app.modules.wealth.application.factory import build_wealth_agent
from app.modules.wealth.contracts.bank import PURCHASE_OPERATION, WriteAuthorization
from app.modules.wealth.contracts.requests import WealthRequest, WealthRequestKind
from app.modules.wealth.contracts.responses import WealthStatus
from app.tools.protocol import ErrorCode
from tests.wealth import support

PRODUCTS = "/products"
PRODUCT = "/products/p_demo_cash"
ACCOUNTS = "/accounts"
LATEST = "/wealth/risk-assessments/latest"
PURCHASE = "/products/purchase"
ORDER = "/wealth/orders/order_purchase_1"
LOOKUP = "/wealth/orders/by-idempotency-key"

CUSTOMER = support.DEMO_CUSTOMER
ACCOUNT = "a_wealth_main"
AMOUNT_MINOR = 200000
BALANCE_MINOR = 2000000
SLOTS = {"product_id": "p_demo_cash", "account_id": ACCOUNT, "amount_minor": AMOUNT_MINOR}


# ---------------------------------------------------------------------- 夹具


def _stub_reads(double, *, balances=(BALANCE_MINOR,)):
    double.on("GET", PRODUCTS, data=support.wealth_demo_products())
    double.on("GET", PRODUCT, data=support.product_payload())
    double.on_sequence(
        "GET",
        ACCOUNTS,
        *[
            [support.account_payload(balance_minor=value, available_minor=value)]
            for value in balances
        ],
    )
    double.on("GET", LATEST, data=support.risk_profile_payload(risk_level="low"))
    return double


def _preview_replies():
    return (
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase(dict(SLOTS)),
    )


def _posts(double):
    return [call for call in double.calls if call.method == "POST"]


CARRIER = support.SessionCarrier()


def _preview(agent, context):
    """预览那一轮：会话与待办交给运行时保存（旧用例里它们住在处理器的字典里）。"""

    return CARRIER.handle(
        agent, context, WealthRequest(kind="message", text="买 2000 元的演示灵活理财，用主账户")
    )


def _confirm(agent, context, pending_id, code=None):
    """点确认提交这一笔；`code` 是用户在确认框上输入的验证码（银行要求时必填）。"""

    return CARRIER.handle(
        agent,
        context,
        WealthRequest(kind="confirm", pending_id=pending_id, verification_code=code),
    )


def _reconcile(agent, context, pending_id):
    return CARRIER.handle(agent, context, WealthRequest(kind="reconcile", pending_id=pending_id))


def _pending_write(agent, context):
    """待办里那份写计划（旧的 `agent._pending[pending_id].write`）。"""

    return CARRIER.write(agent, context)


# ---------------------------------------------------------------------- C01 预览


def test_c01_a_preview_waits_for_confirmation_and_writes_nothing(bank, double, clock):
    """C01：生成申购预览 → waiting_confirmation，预览完整，尚未资金写。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)

    response = _preview(agent, support.bound_context())

    assert response.status is WealthStatus.WAITING_CONFIRMATION
    assert response.pending is not None
    assert _posts(double) == []
    # 预览原文与冻结快照都在 facts 里：用户核对的就是这一笔。
    assert response.facts["purchase_preview"]["amount_minor"] == AMOUNT_MINOR
    assert response.facts["purchase_preview"]["from_account_id"] == ACCOUNT


def test_c01_the_confirmation_projection_is_program_generated_and_carries_no_credential(
    bank, double, clock
):
    """确认卡片是程序生成的展示投影：只有展示字段，没有任何授权凭据。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)

    context = support.bound_context()
    response = _preview(agent, context)
    confirmation = response.confirmation

    assert confirmation is not None
    # operation 用的是银行幂等记录的取值（purchase_product），不是模型能写的动词。
    assert confirmation.operation == "purchase_product"
    assert confirmation.title == "确认申购"
    assert confirmation.amount_minor == AMOUNT_MINOR
    assert confirmation.product_name == "演示灵活理财"
    assert confirmation.account_display == ACCOUNT
    # 幂等键、验证码与快照原文都没有字段可以走这条路。
    rendered = json.dumps(
        {
            "operation": confirmation.operation,
            "title": confirmation.title,
            "amount_minor": confirmation.amount_minor,
            "product_name": confirmation.product_name,
            "account_display": confirmation.account_display,
        },
        ensure_ascii=False,
    )
    write = _pending_write(agent, context)
    assert write.idempotency_key not in rendered


def test_a_confirmation_projection_is_refused_on_any_other_status():
    """确认卡片只在"等用户点击"时有意义：跟着别的状态发出去会渲染出一个点不出结果的按钮。"""

    from app.modules.wealth.contracts.responses import Confirmation, WealthResponse

    projection = Confirmation(
        operation="purchase",
        title="确认申购",
        amount_minor=AMOUNT_MINOR,
        product_name="演示灵活理财",
        account_display=ACCOUNT,
    )
    with pytest.raises(ValueError, match="waiting_confirmation"):
        WealthResponse(status=WealthStatus.COMPLETED, summary="好", confirmation=projection)


# ---------------------------------------------------------------------- C02 能力支持时的一次提交


def test_c02_a_trusted_click_submits_once_and_never_asks_for_a_code(double, clock):
    """C02：可信按钮确认 + 适配器支持新契约 → 复核后一次提交，不问验证码。"""

    _stub_reads(double, balances=(BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR, BALANCE_MINOR))
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1",
            holding_id="h_purchase_1",
            transaction_id="txn_purchase_1",
            amount_minor=AMOUNT_MINOR,
        ),
    )
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.COMPLETED
    assert response.business_ids["order_id"] == "order_purchase_1"
    writes = _posts(double)
    assert len(writes) == 1
    body = writes[0].body
    assert body["confirm"] is True
    assert body["idempotent_key"]
    assert body["user_id"] == CUSTOMER
    # 点击确认不带验证码：请求体里没有 mfa_code，用户也没有被问过验证码。
    assert "mfa_code" not in body
    assert response.pending is None


def test_c02_the_click_never_produces_a_waiting_verification_state(double, clock):
    """新交互路径不产生 waiting_verification：它已经不是这个状态机的一部分。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1", holding_id="h_purchase_1", amount_minor=AMOUNT_MINOR
        ),
    )
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is not WealthStatus.WAITING_VERIFICATION


# ---------------------------------------------------------------------- C03 当前银行要求验证码


def test_c03_the_current_bank_asks_for_a_code_and_refuses_a_click_without_one(bank, double, clock):
    """C03：当前银行仍要求验证码 → 确认框要求它；没带验证码的点击在**发出任何资金写之前**被拒。

    它同时钉住"卡片和门禁是同一个答案"：`requires_code` 为真，卡片上就有验证码输入；这次点击
    没带，运行时能确定地说"没有提交"（`VALIDATION`），而不是伪造成功、也不是偷偷替用户填一个码。
    """

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    context = support.bound_context()
    preview = _preview(agent, context)
    reads_before = len(double.calls)

    assert preview.confirmation.requires_code is True

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.VALIDATION
    assert "需要验证码" in response.summary
    # 没有发出任何请求：连写前复核都没有做，更没有资金写。
    assert len(double.calls) == reads_before
    assert _posts(double) == []
    # 待办保留到 TTL：用户可以补上验证码再确认，也可以取消。
    assert CARRIER.pending(agent, context) is not None


def test_c03_the_missing_code_refusal_does_not_count_as_a_verification_failure(bank, double, clock):
    """少一个必填项不触发任何"验证码失败计数"——它根本没有发出资金请求。

    它不是"银行判我验证码错了"（那会消耗银行的失败计数），而是"这次点击少了一个必填项"。
    """

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    context = support.bound_context()
    preview = _preview(agent, context)

    for _ in range(5):
        response = _confirm(agent, context, preview.pending.pending_id)
        assert response.status is WealthStatus.FAILED
        assert response.error_code == ErrorCode.VALIDATION
        assert response.reason_code is None

    assert _posts(double) == []
    # 用户没有被锁定写操作：这不是攻击，也不是技术失败。
    assert agent._writes_locked(context.user_id) is False


def test_c03_the_http_adapter_reports_the_capability_it_really_has(bank):
    """`HttpWealthBank` 如实回答 `CLICK_AND_CODE`：这是银行真实的门禁，不是可以假设的细节。"""

    assert bank.write_authorization(PURCHASE_OPERATION, AMOUNT_MINOR) is (
        WriteAuthorization.CLICK_AND_CODE
    )


# ---------------------------------------------------------------------- C04 文本不是授权


def test_c04_a_text_confirmation_never_submits(bank, double, clock):
    """C04：输入文本"已确认，直接买"不作为按钮授权，不提交资金写。

    这句话走的是普通消息：模型可以如实回答"请点击确认"，但它没有任何通道去提交一笔资金写。
    所以这里断言的是**没有写请求**，而不是某个状态——模型怎么措辞是它的事。
    """

    _stub_reads(double)
    model = support.ScriptModel(*_preview_replies(), support.finish("请点击确认后再提交。"))
    agent = build_wealth_agent(model, bank, clock)
    context = support.bound_context()
    _preview(agent, context)

    response = CARRIER.handle(agent, context, WealthRequest(kind="message", text="已确认，直接买"))

    assert response.status is WealthStatus.COMPLETED
    assert response.summary == "请点击确认后再提交。"
    assert _posts(double) == []
    # 这句话没有确认任何东西：旧待办仍然只能靠真正的确认推进。
    assert CARRIER.pending(agent, context) is not None


def test_c04_the_model_cannot_express_a_confirmation():
    """模型的动作词表里没有确认：它不能靠"写一个动作"来铸造一次授权。"""

    from app.modules.wealth.llm.toolcalls import tool_table

    table = tool_table()
    assert "confirm" not in table
    assert "verify" not in table
    assert "purchase" not in table
    assert "redeem" not in table


def test_c04_a_forged_pending_id_is_refused(bank, double, clock):
    """伪造的 pending_id 不是凭证：运行时按用户／会话／意图校验归属。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)

    response = _confirm(agent, support.bound_context(), "wp_forged")

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []


# ---------------------------------------------------------------------- C05 幂等


def test_c05_a_repeated_click_returns_the_receipt_and_never_writes_twice(double, clock):
    """C05：重复 confirm 用相同的键，至多一次提交，成功后返回原回执。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 6)
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1", holding_id="h_purchase_1", amount_minor=AMOUNT_MINOR
        ),
    )
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    pending_id = preview.pending.pending_id

    first = _confirm(agent, context, pending_id)
    second = _confirm(agent, context, pending_id)
    third = _confirm(agent, context, pending_id)

    assert first.status is second.status is third.status is WealthStatus.COMPLETED
    assert first.business_ids == second.business_ids == third.business_ids
    # 一次意图只有一个键：三次点击只对应一次资金写。
    assert len(_posts(double)) == 1


def test_c05_a_concurrent_click_does_not_start_a_second_write(double, clock):
    """C05：同一待办的提交正在进行时，第二次点击如实返回，绝不发起第二次资金写。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)

    class ReentrantBank(support.ClickConfirmationBank):
        """在第一次资金写的过程中再点一次确认，模拟并发／重入。"""

        def __init__(self, inner, agent_holder):
            super().__init__(inner)
            self._holder = agent_holder
            self.reentrant = None

        def purchase(self, context, intent, verification_code, idempotency_key):
            holder = self._holder
            if holder.get("agent") is not None and self.reentrant is None:
                self.reentrant = _confirm(holder["agent"], context, holder["pending_id"])
            return super().purchase(context, intent, verification_code, idempotency_key)

    holder: dict = {}
    reentrant = ReentrantBank(double.client(), holder)
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1", holding_id="h_purchase_1", amount_minor=AMOUNT_MINOR
        ),
    )
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), reentrant, clock)
    context = support.bound_context()
    preview = _preview(agent, context)
    holder["agent"] = agent
    holder["pending_id"] = preview.pending.pending_id

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.COMPLETED
    assert reentrant.reentrant is not None
    assert reentrant.reentrant.status is WealthStatus.FAILED
    assert reentrant.reentrant.error_code == ErrorCode.INVALID_STATE
    assert len(_posts(double)) == 1


# ---------------------------------------------------------------------- C06 改参失效


def test_c06_changing_the_amount_invalidates_the_old_confirmation(bank, double, clock):
    """C06：修改金额后点击旧确认 → 拒绝旧待办，要求新预览。"""

    _stub_reads(double, balances=(BALANCE_MINOR,))
    model = support.ScriptModel(
        *_preview_replies(),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase({**SLOTS, "amount_minor": 1000000}),
    )
    agent = build_wealth_agent(model, bank, clock)
    context = support.bound_context()
    first = _preview(agent, context)
    old_pending_id = first.pending.pending_id

    second = CARRIER.handle(agent, context, WealthRequest(kind="message", text="算了，改成一万块"))

    assert second.status is WealthStatus.WAITING_CONFIRMATION
    assert second.pending.pending_id != old_pending_id
    assert second.facts["purchase_preview"]["amount_minor"] == 1000000

    stale = _confirm(agent, context, old_pending_id)

    assert stale.status is WealthStatus.FAILED
    assert stale.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []


# ---------------------------------------------------------------------- C07 过期与归属


def test_c07_an_expired_confirmation_is_refused(bank, double, clock):
    """C07：过期的确认被拒绝，且不泄露待办归属信息。"""

    from datetime import timedelta

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    context = support.bound_context()
    preview = _preview(agent, context)

    clock.advance(timedelta(minutes=6))
    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []


def test_c07_a_confirmation_from_another_session_is_refused(bank, double, clock):
    """C07：跨会话的确认被拒绝。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    owner = support.bound_context(session_id="sess_owner")
    preview = _preview(agent, owner)

    stranger = support.bound_context(user_id="user_0002", session_id="sess_stranger")
    response = _confirm(agent, stranger, preview.pending.pending_id)

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []


def test_c07_a_confirmation_from_another_user_is_refused(bank, double, clock):
    """C07：跨用户的确认被拒绝，且不透露待办是否存在。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    owner = support.bound_context(user_id="user_0001")
    preview = _preview(agent, owner)

    stranger = support.bound_context(user_id="user_0002", session_id=owner.session_id)
    response = _confirm(agent, stranger, preview.pending.pending_id)

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert _posts(double) == []
    # 同一个错误码与同一句话：调用方分不出"不是你的"和"不存在"，所以拒绝本身不泄露归属。
    missing = _confirm(agent, owner, "wp_nonexistent")
    assert (response.error_code, response.summary) == (missing.error_code, missing.summary)


# ---------------------------------------------------------------------- C08 迟到取消


def test_c08_a_late_cancel_does_not_claim_a_completed_order_was_reversed(double, clock):
    """C08：已成交的订单不能被"取消"声称撤销。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 6)
    double.on("POST", PURCHASE, data=support.purchase_receipt_payload(), status=201)
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1", holding_id="h_purchase_1", amount_minor=AMOUNT_MINOR
        ),
    )
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    assert _confirm(agent, context, preview.pending.pending_id).status is WealthStatus.COMPLETED

    response = CARRIER.handle(agent, context, WealthRequest(kind="cancel"))

    assert response.status is WealthStatus.CANCELLED
    # 没有声称撤销了任何东西：这笔订单已经在银行成交。
    assert "撤销" not in response.summary or "不代表" in response.summary
    assert len(_posts(double)) == 1


def test_c08_a_late_cancel_cannot_cancel_a_newer_pending(bank, double, clock):
    """C08：迟到的旧取消事件不得取消新的待办。"""

    _stub_reads(double)
    model = support.ScriptModel(
        *_preview_replies(),
        support.call_read("wealth_products"),
        support.call_read("wealth_accounts"),
        support.prepare_purchase({**SLOTS, "amount_minor": 1000000}),
    )
    agent = build_wealth_agent(model, bank, clock)
    context = support.bound_context()
    first = _preview(agent, context)
    second = CARRIER.handle(agent, context, WealthRequest(kind="message", text="改成一万块"))
    new_pending_id = second.pending.pending_id

    # 旧待办的取消事件：运行时只认当前活动待办，新待办不受影响。
    stale_cancel = CARRIER.handle(
        agent, context, WealthRequest(kind="cancel", pending_id=first.pending.pending_id)
    )

    assert stale_cancel.status is WealthStatus.CANCELLED
    assert CARRIER.pending_id(agent, context) == new_pending_id


def test_c08_a_stranger_cannot_cancel_someone_elses_pending(bank, double, clock):
    """C08：跨用户的取消被拒绝——归属校验用的是**调用方身份**，不是会话自己抄的字段。

    这条用例钉住实现：`_cancel` 曾经把会话对象当成 `TrustedContext` 传给归属校验，而会话自己的
    `user_id` 永远等于调用方，于是那次"校验"成了同义反复，什么也读不出来。
    """

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    owner = support.bound_context(user_id="user_0001", session_id="sess_1")
    preview = _preview(agent, owner)
    pending_id = preview.pending.pending_id

    stranger = support.bound_context(user_id="user_0002", session_id="sess_2")
    response = CARRIER.handle(agent, stranger, WealthRequest(kind="cancel", pending_id=pending_id))

    assert response.status is WealthStatus.CANCELLED
    assert response.summary == "当前没有正在进行的理财操作，已停止本次查询。"
    # 别人的待办没有被取消：它还在，而且仍然只能由本人确认。
    assert CARRIER.pending_id(agent, owner) == pending_id


# ---------------------------------------------------------------------- C09 未知结果只核实


def test_c09_an_unknown_outcome_is_never_reported_as_success_or_failure(double, clock):
    """C09：写请求超时 → 原键只读核实，不补单；查不到仍是 UNKNOWN。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.UNKNOWN
    assert response.error_code == ErrorCode.TRANSACTION_UNKNOWN
    # 只发过一次写请求，核实是只读的；没有第二次下单。
    assert len(_posts(double)) == 1
    # 核实引用交给受控调用方，模型拿不到它。
    assert response.facts["reconciliation"]["found"] is False


def test_c09_reconcile_after_an_unknown_reads_by_the_original_key(double, clock):
    """C09：随后 reconcile → 按原幂等键读一次，查不到仍 UNKNOWN，不补单。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    pending_id = preview.pending.pending_id
    unknown = _confirm(agent, context, pending_id)
    write_key = unknown.facts["reconciliation"]["idempotent_key"]

    response = _reconcile(agent, context, pending_id)

    assert response.status is WealthStatus.UNKNOWN
    lookups = [call for call in double.calls if call.path == LOOKUP]
    assert lookups
    assert lookups[-1].query["idempotent_key"] == write_key
    # 仍然只有最初那一次写请求：核实从不重发写。
    assert len(_posts(double)) == 1


def test_c09_a_found_record_reports_the_order_as_reconciled(double, clock):
    """C09：查到了且与确认过的意图一致 → 如实报告成交，并说明是按原键核实回来的。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 6)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on(
        "GET",
        LOOKUP,
        data=support.lookup_hit(
            "purchase_product",
            "idem-1",
            support.purchase_receipt_payload(),
        ),
    )
    double.on(
        "GET",
        ORDER,
        data=support.order_payload(
            order_id="order_purchase_1", holding_id="h_purchase_1", amount_minor=AMOUNT_MINOR
        ),
    )
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)

    response = _confirm(agent, context, preview.pending.pending_id)

    assert response.status is WealthStatus.COMPLETED
    assert response.business_ids["order_id"] == "order_purchase_1"
    assert "核实" in response.summary
    assert len(_posts(double)) == 1


def test_c09_repeating_a_confirm_after_an_unknown_never_writes_again(double, clock):
    """C09：未知之后再次 confirm 只核实，绝不当成再次下单。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    pending_id = preview.pending.pending_id
    _confirm(agent, context, pending_id)

    again = _confirm(agent, context, pending_id)

    assert again.status is WealthStatus.UNKNOWN
    assert len(_posts(double)) == 1


def test_c09_the_unknown_response_hands_back_the_pending_to_reconcile_with(double, clock):
    """C09：结果未知时必须把**同一个 pending_id** 交回调用方，否则核实入口走不到。

    调用方（CLI、未来的确认卡片通道）只有拿着这个编号才能发 `kind=reconcile`。只给一个
    "有东西要核实"而不给编号，等于让核实入口在实践里不可达。
    """

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    pending_id = preview.pending.pending_id

    unknown = _confirm(agent, context, pending_id)

    assert unknown.status is WealthStatus.UNKNOWN
    assert unknown.pending is not None
    assert unknown.pending.pending_id == pending_id

    # 拿这个编号去核实：能走到"按原键读一次银行"，而不是"没有需要核实的交易"。
    reconciled = _reconcile(agent, context, unknown.pending.pending_id)

    assert reconciled.status is WealthStatus.UNKNOWN
    assert [call.path for call in double.calls].count(LOOKUP) == 2


def test_c09_an_unresolved_write_blocks_a_new_transaction(double, clock):
    """C09：同会话还有一笔未核实的写时，不允许开一笔新交易。"""

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies(), *_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )
    context = support.bound_context()
    preview = _preview(agent, context)
    _confirm(agent, context, preview.pending.pending_id)

    second = _preview(agent, context)

    assert second.status is WealthStatus.FAILED
    assert second.error_code == ErrorCode.INVALID_STATE


# ---------------------------------------------------------------------- C10 旧验证码入口


def test_c10_the_retired_verify_entry_is_refused_and_never_reaches_the_bank(bank, double, clock):
    """C10：旧 verify 输入不恢复旧交互，也不进入模型、不碰银行。"""

    _stub_reads(double)
    agent = build_wealth_agent(support.ScriptModel(*_preview_replies()), bank, clock)
    context = support.bound_context()
    preview = _preview(agent, context)
    reads_before = len(double.calls)

    response = CARRIER.handle(
        agent,
        context,
        WealthRequest(
            kind="verify", pending_id=preview.pending.pending_id, verification_code="246810"
        ),
    )

    assert response.status is WealthStatus.FAILED
    assert response.error_code == ErrorCode.INVALID_STATE
    assert "停用" in response.summary
    # 没有多一次银行调用，更没有资金写。
    assert len(double.calls) == reads_before
    assert _posts(double) == []


def test_c10_the_retired_entry_is_not_offered_by_the_module_surface():
    """CLI 与契约都不再提供验证码入口：它只能被拒绝，不能被走通。"""

    from app.modules.wealth.cli import WealthChat

    assert not hasattr(WealthChat, "verify")
    assert not hasattr(WealthChat, "needs_code")
    # 词表里留着它，正是为了能明确拒绝（删掉会让旧输入变成一句 Python 异常）。
    assert WealthRequestKind.VERIFY in set(WealthRequestKind)


def test_c10_a_verification_code_never_enters_the_model_context(bank, double, clock, caplog):
    """C10：验证码不进模型上下文，也不进日志。"""

    import logging

    _stub_reads(double)
    model = support.ScriptModel(*_preview_replies())
    agent = build_wealth_agent(model, bank, clock)
    context = support.bound_context()
    preview = _preview(agent, context)
    model.prompts.clear()

    with caplog.at_level(logging.INFO):
        CARRIER.handle(
            agent,
            context,
            WealthRequest(
                kind="verify", pending_id=preview.pending.pending_id, verification_code="246810"
            ),
        )

    assert "246810" not in "\n".join(model.prompts)
    assert "246810" not in caplog.text


# ---------------------------------------------------------------------- C11 整轮通道


def test_c11_the_whole_turn_channel_returns_the_preview_as_a_confirmation_card(bank, double, clock):
    """C11：整轮通道把预览交成 `WAITING_CONFIRMATION` 加一张结构化确认卡片。

    C1（前端→主助手的可信按钮事件通道）已经打通，所以这里不再是 UNSUPPORTED：状态、金额、
    产品、账户和待办编号一起交给基座，由它写成检查点里的暂停。预览本身仍然不写钱。
    """

    from app.agents.turn import TurnStatus

    _stub_reads(double)
    context = support.bound_context()
    turn = support.GraphTurn(double, context=context, clock=clock)
    model = support.ScriptModel(*_preview_replies())

    result = turn.lap(model, "买 2000 元的演示灵活理财", session=context.session_id)

    assert result.status is TurnStatus.WAITING_CONFIRMATION
    assert result.confirmation is not None
    assert result.confirmation.pending_id
    assert result.confirmation.amount_minor == AMOUNT_MINOR
    assert _posts(double) == []


def test_c11_a_chat_message_cannot_forge_a_confirmation_through_the_turn(bank, double, clock):
    """C11：整轮通道里的一句"确认"不能伪造确认，也不会提交资金写。"""

    from app.agents.turn import TurnStatus

    _stub_reads(double)
    context = support.bound_context()
    turn = support.GraphTurn(double, context=context, clock=clock)
    model = support.ScriptModel(*_preview_replies(), support.finish("请点击确认后再提交。"))
    turn.lap(
        model, "买 2000 元的演示灵活理财", session=context.session_id, conversation_id="conv_c11"
    )

    # 暂停之后又来一句普通消息：它只是一句话，不是按钮事件。
    result = turn.lap(model, "确认", session=context.session_id, conversation_id="conv_c11")

    assert result.status in (TurnStatus.COMPLETED, TurnStatus.WAITING_CONFIRMATION)
    assert _posts(double) == []


def test_c11_the_button_event_is_the_only_way_in_and_it_carries_only_the_pending_id(
    bank, double, clock
):
    """C11：受控按钮事件是唯一的确认入口，而且它只带一个决定。

    `click` 与 `lap` 是**同一个会话**上的两个入口：预览留下的待办就在检查点上，所以点击校验
    的正是用户看到的那一笔。金额、产品、账户都不在事件里——它们以服务端冻结的快照为准。
    """

    from app.agents.turn import TurnStatus

    _stub_reads(double)
    context = support.bound_context()
    turn = support.GraphTurn(double, context=context, clock=clock)
    preview = turn.lap(support.ScriptModel(*_preview_replies()), "买", session=context.session_id)
    assert preview.confirmation is not None

    # 取消：只终止未提交的待办，不声称撤销任何订单，也不碰银行。
    cancelled = turn.click(
        support.ScriptModel(), preview, "取消", message="取消", session=context.session_id
    )

    assert cancelled.status is TurnStatus.COMPLETED
    assert "没有提交任何交易" in cancelled.summary
    assert _posts(double) == []


def test_c11_a_click_that_names_another_pending_cannot_confirm_this_one(bank, double, clock):
    """C11：点击里夹带别人的待办编号 → 这次点击不算数，更不会提交。

    待办编号由服务端绑定，客户端只交一个决定；多带一个字段就是对不上当前问题的补充信息，
    于是它被退回重问，而不是被当成"点了别人那一笔"。
    """

    from app.agents.turn import TurnStatus

    _stub_reads(double)
    context = support.bound_context()
    turn = support.GraphTurn(double, context=context, clock=clock)
    preview = turn.lap(support.ScriptModel(*_preview_replies()), "买", session=context.session_id)
    assert preview.confirmation is not None

    result = turn.answer(
        support.ScriptModel(),
        preview.question or {},
        {"decision": "确认", "pending_id": "wp_not_mine"},
        message="确认",
        session=context.session_id,
    )

    assert result.status is TurnStatus.WAITING_INPUT
    assert "请重新提供" in result.summary
    assert _posts(double) == []
    # 原来那一笔待办没有被这次伪造点击动过。
    assert result.pending is not None


def test_c11_a_confirm_that_names_another_pending_is_refused_and_never_submitted(
    bank, double, clock
):
    """C11（旧断言原样保留）：确认通道只认服务端绑定的那一笔。

    旧通道让按钮事件自己带 `pending_id`，靠"查不到就报错"挡回"点了别人那一笔"；新通道里点击
    只交一个决定（见上一个用例），编号由服务端的确认问题绑定，客户端连"我要确认哪一笔"都表达
    不了。于是这条防线要在请求层验证：这里把检查点上的确认问题改写成指向另一个待办
    （`wp_not_mine`），而用户真正的那一笔仍然活着——确认通道读到的是问题里的编号，查到的是
    "没有这一笔"，结果必须是 FAILED + "没有待确认的理财操作"，并且一次写请求都不发。
    """

    from app.agents.turn import TurnStatus

    _stub_reads(double)
    context = support.bound_context()
    graph = support.wealth_domain_graph(
        support.ScriptModel(*_preview_replies()), double, context=context, clock=clock
    )
    turn = support.GraphTurn(double, context=context, clock=clock, graph=graph)
    preview = turn.lap(support.ScriptModel(), "买", session=context.session_id)
    assert preview.confirmation is not None
    assert preview.pending is not None
    live = preview.pending["pending_id"]

    question = dict(preview.question or {})
    question["confirmation"] = {**dict(question["confirmation"]), "pending_id": "wp_not_mine"}
    config = {"configurable": {"thread_id": f"chat:{context.session_id}"}}

    async def rewrite():
        snapshot = await graph.aget_state(config)
        for task in snapshot.tasks:
            nested = getattr(task, "state", None)
            if isinstance(nested, Mapping) and "configurable" in nested:
                await graph.aupdate_state(nested, {"pending_question": question})
                return
        raise AssertionError("没有找到领域子图的检查点命名空间")

    asyncio.run(rewrite())

    result = turn.click(
        support.ScriptModel(), preview, "确认", message="确认", session=context.session_id
    )

    assert result.status is TurnStatus.FAILED
    assert "没有待确认的理财操作" in result.summary
    assert _posts(double) == []
    # 活着的那一笔没有被这次点错动过。
    assert result.pending is not None
    assert result.pending["pending_id"] == live


# ---------------------------------------------------------------------- 结果未知不能被折叠成失败


def test_unknown_is_never_mapped_onto_the_failed_default():
    """理财的 UNKNOWN 必须映射成 `TurnStatus.UNKNOWN`，不能落进映射表的 FAILED 默认值。

    这是资金安全性质，不是措辞问题：把一笔可能已经成交的交易报成"没办成"，用户会去重新下单。
    """

    from app.agents.turn import TurnStatus
    from app.modules.wealth.contracts.responses import WealthResponse
    from app.modules.wealth.contracts.responses import WealthStatus as WS
    from app.modules.wealth.workflow.graph import _turn_result

    result = _turn_result(
        WealthResponse(status=WS.UNKNOWN, summary="银行没有返回明确结果。"),
    )

    assert result.status is TurnStatus.UNKNOWN
    assert result.status is not TurnStatus.FAILED
    # 摘要如实说明"未知"，而且明确程序不会重复下单。
    assert "结果暂时未知" in result.summary
    assert "不会重复下单" in result.summary


def test_an_unknown_write_reaches_the_base_as_unknown(double, clock):
    """端到端：一次超时的资金写经整轮适配后，基座读到的是 unknown 而不是 failed。"""

    from app.agents.turn import TurnStatus
    from app.modules.wealth.workflow.graph import _turn_result

    _stub_reads(double, balances=(BALANCE_MINOR,) * 4)
    double.on_failure("POST", PURCHASE, httpx.ConnectTimeout("timed out"))
    double.on("GET", LOOKUP, data=support.lookup_miss())
    context = support.bound_context()
    # 声明支持点击确认的适配器替身，才能真的产生一次结果未知的资金写。它不改变 `_turn_result`
    # 的映射，只负责造出那个状态。
    agent = build_wealth_agent(
        support.ScriptModel(*_preview_replies()),
        support.ClickConfirmationBank(double.client()),
        clock,
    )

    preview = _preview(agent, context)
    unknown = _confirm(agent, context, preview.pending.pending_id)

    assert unknown.status is WealthStatus.UNKNOWN
    result = _turn_result(unknown)

    assert result.status is TurnStatus.UNKNOWN
    assert "结果暂时未知" in result.summary
    # 只发过一次写请求：结果未知之后没有任何重发。
    assert len(_posts(double)) == 1
    # 核实引用随 facts 一起交给上层，模型拿不到幂等键本身的能力没有变化。
    assert result.facts["reconciliation"]["found"] is False


# ---------------------------------------------------------------------- 工具
