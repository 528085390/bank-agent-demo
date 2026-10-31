"""事务化 seed / reset。

一次 rebuild 在单个事务内完成：清空业务数据 → 写入固定种子 → 逐账户对账。
任何一步失败都整体回滚，不会留下半套种子。
每次 rebuild 生成新的 run_id，reset 可要求校验 expected_run_id。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import BusinessError
from app.core.timeutil import iso
from app.domain.money_service import reconcile
from app.storage.database import Database
from app.storage.models import (
    AaParticipant,
    AaRequest,
    Account,
    AuditLog,
    Card,
    CardAction,
    Contact,
    Holding,
    IdempotencyRecord,
    Order,
    Product,
    ScheduledTask,
    SecurityState,
    SimulationState,
    Subscription,
    SubscriptionCharge,
    Transaction,
    User,
    WealthRiskAssessment,
)
from app.storage.seed import (
    ACCOUNTS,
    DEMO_TIME,
    EXPECTED_CLOSING_BALANCES,
    SEED_VERSION,
    apply_seed,
)

# 按外键依赖顺序删除：先子表后父表（所有外键都是 ON DELETE RESTRICT）。
# orders 引用 holdings，因此必须排在 holdings 之前；
# wealth_risk_assessments 引用 users，因此必须排在 users 之前。
DELETE_ORDER = (
    CardAction,
    AaParticipant,
    SubscriptionCharge,
    Order,
    Transaction,
    Holding,
    ScheduledTask,
    AaRequest,
    Subscription,
    Card,
    Contact,
    SecurityState,
    IdempotencyRecord,
    AuditLog,
    SimulationState,
    WealthRiskAssessment,
    Account,
    Product,
    User,
)


class SeedService:
    def __init__(self, database: Database):
        self.database = database

    def current_run_id(self) -> uuid.UUID | None:
        with self.database.sessions() as session:
            state = session.get(SimulationState, 1)
            return state.run_id if state else None

    def current_time(self) -> datetime | None:
        with self.database.sessions() as session:
            state = session.get(SimulationState, 1)
            return state.current_time if state else None

    def seed(self) -> dict:
        """按固定种子初始化，不校验既有 run_id。"""
        return self._rebuild(expected_run_id=None)

    def reset(self, expected_run_id: str | uuid.UUID | None = None) -> dict:
        """重置固定种子；传入 expected_run_id 时先校验，不匹配则拒绝。"""
        return self._rebuild(expected_run_id=expected_run_id)

    def _rebuild(self, expected_run_id: str | uuid.UUID | None) -> dict:
        with self.database.sessions() as session:
            with session.begin():
                if expected_run_id is not None:
                    state = session.get(SimulationState, 1)
                    current = str(state.run_id) if state else None
                    if current is None or current != str(expected_run_id):
                        raise BusinessError(
                            f"expected_run_id 不匹配：当前 {current}，请求 {expected_run_id}"
                        )

                for model in DELETE_ORDER:
                    session.execute(delete(model))

                run_id = uuid.uuid4()
                apply_seed(session, run_id=run_id, now=DEMO_TIME)

                for spec in ACCOUNTS:
                    account = session.get_one(Account, spec["id"])
                    expected = EXPECTED_CLOSING_BALANCES[spec["id"]]
                    actual = reconcile(session, account, spec["opening_minor"])
                    if actual != expected:
                        raise AssertionError(
                            f"账户 {account.id} 期末余额 {actual} 与文档规定的 {expected} 不一致"
                        )

                return {
                    "run_id": str(run_id),
                    "seed_version": SEED_VERSION,
                    "current_time": DEMO_TIME.isoformat(),
                }

    def ready(self) -> dict:
        """最小就绪检查：库连得上、核心表在、模拟时钟已初始化。

        **不看任何余额，也不看种子版本。** 上一版 `health()` 用在这里，它会逐个账户校验
        `available_minor == balance_minor`，任何一笔正常交易或一个新注册客户的入金在
        极短的中间态下都可能让它返回 503——健康检查不该因为业务正常运转而报警。

        `seeded=False` 不是失败：服务已经能连库、能建客户，只是还没人跑种子。部署脚本
        自己会判这个字段决定要不要初始化。
        """

        state = None
        try:
            with self.database.sessions() as session:
                state = session.get(SimulationState, 1)
                session.execute(select(func.count()).select_from(User)).scalar_one()
        except SQLAlchemyError as exc:
            raise BusinessError(f"数据库不可用: {exc.__class__.__name__}") from exc
        return {
            "status": "ok",
            "database": "ok",
            "seed_version": SEED_VERSION,
            "seeded": state is not None,
            "run_id": str(state.run_id) if state else None,
            "current_time": iso(state.current_time) if state else None,
        }

    def health(self) -> dict:
        """完整检查：库可达、种子版本、run_id，并校验 available_minor 与余额一致。

        只给 `/api/health/deep` 和运维脚本用。它比 `ready()` 严格，适合排障，不适合当
        容器探针。
        """

        with self.database.sessions() as session:
            state = session.get(SimulationState, 1)
            if state is None:
                raise BusinessError("模拟状态未初始化，请先执行 seed")
            accounts = session.execute(select(Account).order_by(Account.id)).scalars().all()
            for account in accounts:
                if account.available_minor != account.balance_minor:
                    raise BusinessError(
                        f"账户 {account.id} 的 available_minor({account.available_minor}) "
                        f"与 balance_minor({account.balance_minor}) 不一致"
                    )
            return {
                "status": "ok",
                "database": "ok",
                "seed_version": SEED_VERSION,
                "run_id": str(state.run_id),
                "current_time": iso(state.current_time),
                "account_count": len(accounts),
            }

    def snapshot(self) -> dict:
        """种子内容的只读快照，供健康检查与测试使用。"""
        with self.database.sessions() as session:
            accounts = session.execute(select(Account).order_by(Account.id)).scalars().all()
            return {
                "accounts": {a.id: a.balance_minor for a in accounts},
                "available": {a.id: a.available_minor for a in accounts},
                "users": [
                    u.id for u in session.execute(select(User).order_by(User.id)).scalars().all()
                ],
            }
