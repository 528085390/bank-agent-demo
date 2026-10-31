"""完整演示数据初始化入口。

基础 seed 保持固定且可重复；本模块只补充查询各接口所需的业务样例。
所有记录使用固定 ID，重复执行会先重置基础种子，因此不会累积数据。
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.domain.card_action_service import CardActionService
from app.domain.seed_service import SeedService
from app.domain.wealth_risk_service import RULE_VERSION, VALID_DAYS
from app.storage.models import (
    DOMAIN_TABLES,
    AaParticipant,
    AaRequest,
    Account,
    AuditLog,
    Holding,
    IdempotencyRecord,
    Order,
    ScheduledTask,
    Subscription,
    WealthRiskAssessment,
)
from app.storage.seed import DEMO_TIME


def build_demo_data(database) -> dict:
    result = SeedService(database).seed()
    with database.sessions() as session:
        with session.begin():
            session.add_all(
                [
                    Holding(
                        id="hold_demo_holding", user_id="u_zhang", product_id="p_yuebao",
                        source_account_id="a_zhang_current", principal_minor=30000,
                        current_value_minor=30150, expected_return=Decimal("0.03500"),
                        purchased_at=DEMO_TIME - timedelta(days=30), status="holding",
                    ),
                    Holding(
                        id="hold_demo_partial", user_id="u_zhang", product_id="p_money_fund",
                        source_account_id="a_zhang_current", principal_minor=50000,
                        current_value_minor=50500, expected_return=Decimal("0.04200"),
                        purchased_at=DEMO_TIME - timedelta(days=90), status="partial",
                    ),
                    Holding(
                        id="hold_demo_redeemed", user_id="u_li", product_id="p_fixed_term",
                        source_account_id="a_li_current", principal_minor=100000,
                        current_value_minor=104500, expected_return=Decimal("0.04500"),
                        purchased_at=DEMO_TIME - timedelta(days=365),
                        maturity_date=date(2026, 9, 1), status="redeemed",
                    ),
                    Order(
                        id="order_demo_pending", user_id="u_zhang", order_type="product_purchase",
                        product_id="p_yuebao", amount_minor=10000, status="pending",
                        details={"source": "demo"},
                    ),
                    Order(
                        id="order_demo_completed", user_id="u_zhang", order_type="product_purchase",
                        product_id="p_money_fund", holding_id="hold_demo_partial",
                        amount_minor=50000, status="completed", details={"source": "demo"},
                    ),
                    Order(
                        id="order_demo_failed", user_id="u_li", order_type="product_redeem",
                        holding_id="hold_demo_redeemed", amount_minor=10000, status="failed",
                        details={"reason": "mock_failure"},
                    ),
                    Order(
                        id="order_demo_cancelled", user_id="u_zhang", order_type="birthday",
                        amount_minor=12900, status="cancelled", details={"source": "demo"},
                    ),
                    ScheduledTask(
                        id="task_demo_scheduled", user_id="u_zhang", task_type="scheduled_transfer",
                        execute_at=DEMO_TIME + timedelta(days=3), payload={"demo": True}, status="scheduled",
                    ),
                    ScheduledTask(
                        id="task_demo_completed", user_id="u_zhang", task_type="subscription_charge",
                        execute_at=DEMO_TIME - timedelta(days=2), payload={"demo": True},
                        status="completed", result={"demo": True},
                    ),
                    ScheduledTask(
                        id="task_demo_failed", user_id="u_li", task_type="scheduled_transfer",
                        execute_at=DEMO_TIME - timedelta(days=1), payload={"demo": True},
                        status="failed", error_code="MOCK_FAILURE",
                    ),
                    ScheduledTask(
                        id="task_demo_cancelled", user_id="u_zhang", task_type="birthday_order",
                        execute_at=DEMO_TIME - timedelta(days=1), payload={"demo": True}, status="cancelled",
                    ),
                    AaRequest(
                        id="aa_demo_open", user_id="u_zhang", from_account_id="a_zhang_current",
                        total_amount_minor=30000, participant_count=2, remark="演示聚餐",
                        status="open", expires_at=DEMO_TIME + timedelta(days=7),
                    ),
                    AaRequest(
                        id="aa_demo_partial", user_id="u_zhang", from_account_id="a_zhang_current",
                        total_amount_minor=30000, participant_count=2, remark="演示部分到账",
                        status="partial", expires_at=DEMO_TIME + timedelta(days=7),
                    ),
                    AaRequest(
                        id="aa_demo_completed", user_id="u_zhang", from_account_id="a_zhang_current",
                        total_amount_minor=30000, participant_count=2, remark="演示完成",
                        status="completed", expires_at=DEMO_TIME - timedelta(days=1),
                    ),
                    AaRequest(
                        id="aa_demo_cancelled", user_id="u_zhang", from_account_id="a_zhang_current",
                        total_amount_minor=30000, participant_count=2, remark="演示取消",
                        status="cancelled", expires_at=DEMO_TIME + timedelta(days=7),
                    ),
                    AaRequest(
                        id="aa_demo_expired", user_id="u_zhang", from_account_id="a_zhang_current",
                        total_amount_minor=30000, participant_count=2, remark="演示过期",
                        status="expired", expires_at=DEMO_TIME - timedelta(days=1),
                    ),
                    Subscription(
                        id="sub_demo_paused", user_id="u_li", from_account_id="a_li_current",
                        name="云盘", merchant_name="Mock Cloud", amount_minor=1800,
                        frequency="monthly", start_date=date(2026, 1, 1),
                        next_due_date=date(2026, 10, 1), status="paused", auto_renew=False,
                    ),
                    Subscription(
                        id="sub_demo_cancelled", user_id="u_partner", from_account_id="a_partner_current",
                        name="视频会员", merchant_name="Mock Video", amount_minor=2500,
                        frequency="monthly", start_date=date(2026, 1, 1),
                        next_due_date=date(2026, 9, 1), status="cancelled", auto_renew=False,
                    ),
                    AuditLog(
                        id="audit_demo_seed", request_id="req_demo_seed", user_id="u_zhang",
                        action="demo_seed", resource_type="seed", resource_id="demo",
                        new_value={"version": "1.0"}, permission_level="green",
                        confirm_required=False, mfa_required=False, result="success",
                        reason="完整演示数据",
                    ),
                    # 演示数据里的有效测评：申购还需要 confirm 与演示验证码。
                    WealthRiskAssessment(
                        id="assess_demo_zhang", user_id="u_zhang",
                        answers={"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2},
                        score=10, risk_level="high", rule_version=RULE_VERSION,
                        assessed_at=DEMO_TIME,
                        expires_at=DEMO_TIME + timedelta(days=VALID_DAYS),
                    ),
                    IdempotencyRecord(
                        user_id="u_zhang", operation_type="demo_seed", idempotent_key="demo-1",
                        request_hash="demo", result={"status": "success"},
                    ),
                    AaParticipant(id="aa_demo_open_p1", aa_request_id="aa_demo_open", position=0,
                                  name="张三", phone="13800138000", account_id="a_zhang_current",
                                  amount_minor=15000, status="pending"),
                    AaParticipant(id="aa_demo_open_p2", aa_request_id="aa_demo_open", position=1,
                                  name="李四", phone="13800138001", account_id="a_li_current",
                                  amount_minor=15000, status="pending"),
                    AaParticipant(id="aa_demo_partial_p1", aa_request_id="aa_demo_partial", position=0,
                                  name="张三", amount_minor=15000, status="paid",
                                  transaction_id="txn_seed_transfer_in"),
                    AaParticipant(id="aa_demo_partial_p2", aa_request_id="aa_demo_partial", position=1,
                                  name="爱人", account_id="a_partner_current", amount_minor=15000, status="pending"),
                    AaParticipant(id="aa_demo_completed_p1", aa_request_id="aa_demo_completed", position=0,
                                  name="张三", amount_minor=15000, status="paid"),
                    AaParticipant(id="aa_demo_completed_p2", aa_request_id="aa_demo_completed", position=1,
                                  name="李四", amount_minor=15000, status="paid"),
                    AaParticipant(id="aa_demo_cancelled_p1", aa_request_id="aa_demo_cancelled", position=0,
                                  name="张三", amount_minor=15000, status="refused"),
                    AaParticipant(id="aa_demo_cancelled_p2", aa_request_id="aa_demo_cancelled", position=1,
                                  name="李四", amount_minor=15000, status="refused"),
                    AaParticipant(id="aa_demo_expired_p1", aa_request_id="aa_demo_expired", position=0,
                                  name="张三", amount_minor=15000, status="pending"),
                    AaParticipant(id="aa_demo_expired_p2", aa_request_id="aa_demo_expired", position=1,
                                  name="李四", amount_minor=15000, status="pending"),
                ]
            )
            session.flush()
        CardActionService(database).prepare(
            "u_zhang", "apply", {"card_type": "virtual"}, "demo-card-application"
        )
        counts = {model.__tablename__: session.scalar(select(func.count()).select_from(model)) for model in DOMAIN_TABLES}
        balances = dict(session.execute(select(Account.id, Account.balance_minor)).all())
    result.update({"counts": counts, "balances": balances, "examples": {
        "birthday_pending": "order_demo_pending", "aa_partial": "aa_demo_partial",
    }})
    return result
