"""理财写接口契约：确认验证、适当性、归属、到期限制、幂等与事务原子性。

覆盖计划任务 3 的断言。理财写固定需要 `confirm=true` 且验证码等于配置演示码，
所以这些用例都显式准备有效测评与正确演示码，不用跳过校验来维持旧行为。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event, Thread, Timer

import pytest
from sqlalchemy import func, select

from app.core.errors import (
    BusinessError,
    ConfirmationRequiredError,
    NotFoundError,
    ValidationError,
    VerificationFailedError,
)
from app.domain.investment_service import InvestmentService
from app.domain.security_service import SecurityService
from app.domain.wealth_risk_service import WealthRiskService
from app.storage.database import Database
from app.storage.models import (
    Account,
    AuditLog,
    Holding,
    IdempotencyRecord,
    Order,
    SimulationState,
    Transaction,
)
from app.storage.wealth_seed import SAMPLE_HOLDING_ID, apply_wealth_seed
from tests.conftest import LOW_RISK_ANSWERS, mfa_code, submit_assessment

MFA = mfa_code()


def _balance(database, account_id: str) -> int:
    with database.sessions() as session:
        return session.get_one(Account, account_id).balance_minor


def _count(database, model) -> int:
    with database.sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def _purchase(service, **overrides):
    payload = {
        "user_id": "u_zhang",
        "from_account_id": "a_zhang_current",
        "product_id": "p_yuebao",
        "amount_minor": 100000,
        "confirm": True,
        "mfa_code": MFA,
    }
    payload.update(overrides)
    return service.purchase(**payload)


def _redeem(service, holding_id: str, amount_minor: int, **overrides):
    payload = {
        "user_id": "u_zhang",
        "holding_id": holding_id,
        "amount_minor": amount_minor,
        "confirm": True,
        "mfa_code": MFA,
    }
    payload.update(overrides)
    return service.redeem(**payload)


def _seed_wealth_demo(database) -> dict:
    with database.sessions() as session:
        with session.begin():
            state = session.get_one(SimulationState, 1)
            return apply_wealth_seed(session, now=state.current_time)


# ---------------------------------------------------------------- 确认与验证


def test_small_purchase_still_requires_verification(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)

    # 100 分是旧规则里的 green，理财写仍然要确认。
    with pytest.raises(ConfirmationRequiredError):
        _purchase(service, amount_minor=100, confirm=False, mfa_code=None)
    # 只传任意数字也不够：必须等于配置的演示码。
    with pytest.raises(VerificationFailedError):
        _purchase(service, amount_minor=100, confirm=True, mfa_code="000000")
    with pytest.raises(VerificationFailedError):
        _purchase(service, amount_minor=100, confirm=True, mfa_code=None)

    assert _balance(seeded_database, "a_zhang_current") == 1250050
    assert _count(seeded_database, Holding) == 0


def test_missing_confirmation_is_audited_but_not_counted_as_attack(
    seeded_database, wealth_risk_profile
):
    service = InvestmentService(seeded_database)
    with pytest.raises(ConfirmationRequiredError):
        _purchase(service, confirm=False, mfa_code=None)

    # 待确认是正常流程：不给理财工具累计失败次数。
    states = {state["tool_name"]: state for state in SecurityService(seeded_database).status("u_zhang")}
    assert "fund_purchase" not in states
    with seeded_database.sessions() as session:
        failures = session.execute(
            select(AuditLog).where(
                AuditLog.action == "purchase_product", AuditLog.result == "failure"
            )
        ).scalars().all()
    assert len(failures) == 1


def test_wrong_verification_code_is_counted_and_audited(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    with pytest.raises(VerificationFailedError):
        _purchase(service, mfa_code="999999")

    states = {state["tool_name"]: state for state in SecurityService(seeded_database).status("u_zhang")}
    assert states["fund_purchase"]["failure_count"] == 1
    # 其他域（如转账）的计数不受理财写影响。
    assert states["fund_transfer"]["failure_count"] == 0
    assert _balance(seeded_database, "a_zhang_current") == 1250050

    with seeded_database.sessions() as session:
        failure = session.execute(
            select(AuditLog).where(
                AuditLog.action == "purchase_product", AuditLog.result == "failure"
            )
        ).scalars().one()
    assert "验证码" in failure.reason
    assert "999999" not in (failure.reason or ""), "验证码不落审计"


def test_redeem_requires_confirmation_and_verification(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    purchased = _purchase(service)

    with pytest.raises(ConfirmationRequiredError):
        _redeem(service, purchased["holding_id"], 1000, confirm=False, mfa_code=None)
    with pytest.raises(VerificationFailedError):
        _redeem(service, purchased["holding_id"], 1000, mfa_code="123123")

    holding = service.get_holding(purchased["holding_id"], user_id="u_zhang")
    assert holding["principal_minor"] == 100000
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


# ---------------------------------------------------------------- 适当性


def test_purchase_needs_persisted_valid_profile(seeded_database):
    service = InvestmentService(seeded_database)

    # 没有测评：不能扣款。
    with pytest.raises(BusinessError) as missing:
        _purchase(service)
    assert "风险测评" in missing.value.reason
    assert _balance(seeded_database, "a_zhang_current") == 1250050

    # 有测评但已过期（按银行模拟时钟）：仍然不能扣款。
    submit_assessment(seeded_database, "u_zhang")
    with seeded_database.sessions() as session:
        with session.begin():
            state = session.get_one(SimulationState, 1)
            state.current_time = state.current_time + timedelta(days=31)
    assert WealthRiskService(seeded_database).latest(user_id="u_zhang")["valid"] is False
    with pytest.raises(BusinessError):
        _purchase(service)
    assert _balance(seeded_database, "a_zhang_current") == 1250050

    # 重新测评成 low 之后，买 medium 产品被拒绝。
    submit_assessment(seeded_database, "u_zhang", LOW_RISK_ANSWERS)
    with pytest.raises(BusinessError) as mismatch:
        _purchase(service, product_id="p_fixed_term", amount_minor=1000000)
    assert "高于本人测评等级" in mismatch.value.reason
    assert _balance(seeded_database, "a_zhang_current") == 1250050

    # low 测评买 low 产品可以成功。
    assert _purchase(service, amount_minor=100000)["status"] == "completed"
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


def test_account_and_holding_ownership_on_writes(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)

    # 用 A 的身份扣 B 的账户。
    with pytest.raises(NotFoundError):
        _purchase(service, from_account_id="a_li_current")
    assert _balance(seeded_database, "a_li_current") == 850000

    # 用 A 的身份赎回 B 的持仓。
    submit_assessment(seeded_database, "u_li")
    li_purchase = _purchase(
        service, user_id="u_li", from_account_id="a_li_current", idempotent_key="li-1"
    )
    with pytest.raises(NotFoundError):
        _redeem(service, li_purchase["holding_id"], 1000)
    with pytest.raises(NotFoundError):
        service.get_holding(li_purchase["holding_id"], user_id="u_zhang")
    assert _balance(seeded_database, "a_li_current") == 850000 - 100000


def test_redeem_to_another_users_account_is_rejected(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    purchased = _purchase(service)
    with pytest.raises(NotFoundError):
        _redeem(service, purchased["holding_id"], 1000, to_account_id="a_li_current")
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


# ---------------------------------------------------------------- 期限与赎回规则


def test_maturity_rule_cannot_be_shortened(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)

    # 显式传 0 个月想绕过 12 个月期限：直接拒绝。
    with pytest.raises(ValidationError):
        _purchase(service, product_id="p_fixed_term", amount_minor=1000000, term_months=0)
    with pytest.raises(ValidationError):
        _purchase(service, product_id="p_fixed_term", amount_minor=1000000, term_months=6)

    # 未到期的 maturity_only 持仓不可赎回，银行按模拟时钟判断。
    fixed = _purchase(service, product_id="p_fixed_term", amount_minor=1000000)
    assert service.get_holding(fixed["holding_id"], user_id="u_zhang")["redeemable_minor"] == 0
    with pytest.raises(BusinessError) as not_matured:
        _redeem(service, fixed["holding_id"], 1000)
    assert "不可赎回" in not_matured.value.reason
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 1000000


def test_redeem_does_not_require_an_assessment(seeded_database):
    """赎回不要求测评：演示客户没有任何测评也能赎回已持有产品。"""

    _seed_wealth_demo(seeded_database)
    service = InvestmentService(seeded_database)
    assert WealthRiskService(seeded_database).latest(user_id="u_wealth") is None

    result = service.redeem(
        user_id="u_wealth",
        holding_id=SAMPLE_HOLDING_ID,
        amount_minor=1000,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["holding_status"] == "partial"
    assert _balance(seeded_database, "a_wealth_main") == 2000000 + 1000


# ---------------------------------------------------------------- 幂等


def test_idempotency_hash_conflict_rejected(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    first = _purchase(service, idempotent_key="conflict-1")
    assert first["replayed"] is False

    with pytest.raises(BusinessError):
        _purchase(service, amount_minor=200000, idempotent_key="conflict-1")
    with pytest.raises(BusinessError):
        _purchase(service, from_account_id="a_zhang_fixed", idempotent_key="conflict-1")

    # 同键同参数仍然返回原回执，且只扣了一次。
    replay = _purchase(service, idempotent_key="conflict-1")
    assert replay["replayed"] is True
    assert replay["order_id"] == first["order_id"]
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000
    assert _count(seeded_database, Order) == 1
    assert _count(seeded_database, Holding) == 1


def test_replay_after_full_redemption_returns_original(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    purchased = _purchase(service, idempotent_key="full-buy")
    redeemed = _redeem(service, purchased["holding_id"], 100000, idempotent_key="full-sell")
    assert redeemed["holding_status"] == "redeemed"

    # 全部赎回后重放申购：仍然返回原回执，不因余额或持仓变化而失败。
    replay_buy = _purchase(service, idempotent_key="full-buy")
    assert replay_buy["replayed"] is True
    assert replay_buy["order_id"] == purchased["order_id"]

    # 全部赎回后重放赎回：返回原成功结果，不二次入账。
    replay_sell = _redeem(service, purchased["holding_id"], 100000, idempotent_key="full-sell")
    assert replay_sell["replayed"] is True
    assert replay_sell["order_id"] == redeemed["order_id"]
    assert _balance(seeded_database, "a_zhang_current") == 1250050
    assert _count(seeded_database, Order) == 2


def test_same_user_concurrent_same_key_has_one_order(
    database_url, seeded_database, wealth_risk_profile
):
    """两个并发请求用同一幂等键：只产生一笔订单、一组资金变化。

    只声称覆盖理财写入口：同用户写操作先锁用户行串行化，再查幂等记录。
    """

    barrier = Barrier(2)

    def attempt() -> dict:
        database = Database(database_url)
        try:
            service = InvestmentService(database)
            barrier.wait(timeout=20)
            try:
                return service.purchase(
                    user_id="u_zhang",
                    from_account_id="a_zhang_current",
                    product_id="p_yuebao",
                    amount_minor=100000,
                    confirm=True,
                    mfa_code=MFA,
                    idempotent_key="concurrent-1",
                )
            except Exception as exc:  # noqa: BLE001 - 结果用于断言
                return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            database.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(attempt), pool.submit(attempt)]]

    assert all("error" not in item for item in results), results
    order_ids = {item["order_id"] for item in results}
    assert len(order_ids) == 1
    assert sorted(item["replayed"] for item in results) == [False, True]

    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000
    assert _count(seeded_database, Order) == 1
    assert _count(seeded_database, Holding) == 1
    assert _count(seeded_database, IdempotencyRecord) == 1


# ---------------------------------------------------------------- 资金分摊与原子性


def test_partial_redeem_releases_floor_principal_and_income(seeded_database):
    """P=100000、V=100500、R=50250 → 释放本金 50000、实现收益 250。"""

    _seed_wealth_demo(seeded_database)
    service = InvestmentService(seeded_database)

    result = service.redeem(
        user_id="u_wealth",
        holding_id=SAMPLE_HOLDING_ID,
        amount_minor=50250,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["principal_redeemed_minor"] == 50000
    assert result["interest_minor"] == 250
    assert result["remaining_principal_minor"] == 50000
    assert result["remaining_value_minor"] == 50250
    assert result["holding_status"] == "partial"

    with seeded_database.sessions() as session:
        order = session.get_one(Order, result["order_id"])
        assert order.details["principal_redeemed_minor"] == 50000
        assert order.details["interest_redeemed_minor"] == 250
        assert order.details["full_redemption"] is False
        assert order.to_account_id == "a_wealth_main"

    holding = service.get_holding(SAMPLE_HOLDING_ID, user_id="u_wealth")
    assert holding["realized_income_minor"] == 250
    assert holding["redeemable_minor"] == 50250
    assert _balance(seeded_database, "a_wealth_main") == 2000000 + 50250


def test_full_redeem_clears_holding_and_keeps_history(seeded_database):
    _seed_wealth_demo(seeded_database)
    service = InvestmentService(seeded_database)

    result = service.redeem(
        user_id="u_wealth",
        holding_id=SAMPLE_HOLDING_ID,
        amount_minor=100500,
        confirm=True,
        mfa_code=MFA,
    )
    assert result["principal_redeemed_minor"] == 100000
    assert result["interest_minor"] == 500
    assert result["holding_status"] == "redeemed"
    assert result["remaining_principal_minor"] == 0
    assert result["remaining_value_minor"] == 0
    assert _balance(seeded_database, "a_wealth_main") == 2000000 + 100500

    holding = service.get_holding(SAMPLE_HOLDING_ID, user_id="u_wealth")
    assert holding["redeemable_minor"] == 0
    assert holding["realized_income_minor"] == 500
    assert _count(seeded_database, Holding) == 1, "全部赎回保留持仓记录，不删除"


def test_write_is_atomic(monkeypatch, seeded_database, wealth_risk_profile):
    """在订单／流水写入后、事务提交前注入异常：没有任何半笔业务数据。"""

    def boom(*args, **kwargs):
        raise BusinessError("注入的提交前失败")

    monkeypatch.setattr(InvestmentService, "_remember", staticmethod(boom))
    service = InvestmentService(seeded_database)

    before_transactions = _count(seeded_database, Transaction)
    with pytest.raises(BusinessError) as failure:
        _purchase(service, idempotent_key="atomic-1")
    assert "注入的提交前失败" in failure.value.reason

    assert _balance(seeded_database, "a_zhang_current") == 1250050
    assert _count(seeded_database, Holding) == 0
    assert _count(seeded_database, Order) == 0
    assert _count(seeded_database, Transaction) == before_transactions
    assert _count(seeded_database, IdempotencyRecord) == 0

    # 失败审计允许独立保存，但它不代表资金发生了变化。
    with seeded_database.sessions() as session:
        failures = session.execute(
            select(AuditLog).where(AuditLog.result == "failure")
        ).scalars().all()
    assert len(failures) == 1
    assert "注入的提交前失败" in failures[0].reason


def test_redeem_amount_above_redeemable_is_rejected(seeded_database, wealth_risk_profile):
    service = InvestmentService(seeded_database)
    purchased = _purchase(service)
    with pytest.raises(BusinessError):
        _redeem(service, purchased["holding_id"], 100001)
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000
    assert _count(seeded_database, Order) == 1


# ---------------------------------------------------------------- 行锁复检


@contextmanager
def _row_lock_holder(database_url: str, model, row_id: str):
    """在另一事务里持有某行锁，直到上下文退出；用于验证理财写确实等待行锁。"""

    holder = Database(database_url)
    ready = Event()
    release = Event()

    def hold() -> None:
        with holder.sessions() as session:
            with session.begin():
                session.execute(
                    select(model).where(model.id == row_id).with_for_update()
                ).scalar_one()
                ready.set()
                release.wait(timeout=60)

    thread = Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=30), "另一事务未能取得行锁"
        yield release
    finally:
        release.set()
        thread.join(timeout=30)
        holder.dispose()


def test_purchase_waits_for_the_account_row_lock(
    database_url, seeded_database, wealth_risk_profile
):
    """账户行被其他事务锁住时，申购必须等待锁释放后再复检，而不是先读后写。"""

    service = InvestmentService(seeded_database)
    with _row_lock_holder(database_url, Account, "a_zhang_current") as release:
        timer = Timer(1.5, release.set)
        timer.start()
        started = time.monotonic()
        try:
            result = _purchase(service, idempotent_key="lock-account-1")
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started

    assert result["status"] == "completed"
    assert elapsed >= 1.0, "申购没有等待账户行锁，说明未对账户加 FOR UPDATE"
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000


def test_redeem_waits_for_the_holding_row_lock(
    database_url, seeded_database, wealth_risk_profile
):
    """持仓行被其他事务锁住时，赎回必须等待锁释放后再复检可赎回额。"""

    service = InvestmentService(seeded_database)
    purchased = _purchase(service)

    with _row_lock_holder(database_url, Holding, purchased["holding_id"]) as release:
        timer = Timer(1.5, release.set)
        timer.start()
        started = time.monotonic()
        try:
            result = _redeem(service, purchased["holding_id"], 40000)
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started

    assert result["holding_status"] == "partial"
    assert elapsed >= 1.0, "赎回没有等待持仓行锁，说明未对持仓加 FOR UPDATE"
    assert _balance(seeded_database, "a_zhang_current") == 1250050 - 100000 + 40000
