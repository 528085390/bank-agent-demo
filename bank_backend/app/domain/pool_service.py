"""客户池：给注册用户预先生成一套独立的演示客户。

为什么是「预生成」而不是「注册时创建」：注册在 app 库里是一个事务，银行客户在另一个库，
跨库没有原子事务。要在注册时创建客户，就得引入任务表、状态机、超时恢复和重试接口——那是
一整套分布式作业，而它要解决的问题（两位用户看到不同的余额）只需要一个池子就能解决。

代价是池子有上限。所以池子现在会**自愈**：每被领走一个，app 后端就调一次
`POST /api/pool/provision`（走 `provision_next`），按当前最大池号 +1 建一个新客户补到池尾，
号码已存在就跳过。池子空到连一格都没有时，注册流程会直接为那位用户建号并绑定，不再出现
「账号建成了但永远没有银行客户」的用户。运维脚本 `scripts/seed_customer_pool.py` 仍然保留，
用于一次性把池子拉到指定水位。

每个客户的数据都是**独立生成的 ID**，不是 `u_wealth` 的副本：ID 派生自客户号，余额通过
`post_entry` 入金，因此流水和余额始终可对账，也和任何别的客户没有共享行。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
from app.domain.money_service import CREDIT, post_entry
from app.storage.models import Account, Contact, SecurityState, Subscription, User

POOL_CUSTOMER_PREFIX = "u_pool_"
POOL_PHONE_PREFIX = "139"

# 每个客户的开户入金（整数分）：活期 15000 元、定期 3000 元。金额与 `u_wealth` 不同是有意的
# ——一眼就能看出这是新客户，而不是某个演示档案的复制品。
CURRENT_OPENING_MINOR = 1_500_000
FIXED_OPENING_MINOR = 300_000


def pool_customer_id(index: int) -> str:
    return f"{POOL_CUSTOMER_PREFIX}{index:02d}"


def provision_pool(session: Session, *, size: int, now: datetime) -> dict:
    """确保 1..size 的客户都存在。已存在的直接跳过，不修改任何数据。"""

    created: list[str] = []
    existing: list[str] = []
    for index in range(1, size + 1):
        customer_id = pool_customer_id(index)
        if session.get(User, customer_id) is not None:
            existing.append(customer_id)
            continue
        provision_customer(session, customer_id=customer_id, index=index, now=now)
        created.append(customer_id)
    session.flush()
    return {"created": created, "existing": existing, "size": size}


def next_pool_index(session: Session) -> int:
    """现有池客户里最大的编号 + 1。"""

    ids = session.execute(
        select(User.id).where(User.id.like(f"{POOL_CUSTOMER_PREFIX}%"))
    ).scalars().all()
    highest = 0
    for customer_id in ids:
        suffix = customer_id[len(POOL_CUSTOMER_PREFIX) :]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def provision_next(session: Session, *, now: datetime, attempts: int = 3) -> str | None:
    """建下一个池客户并返回它的号；补不上时返回 None。

    这是「自愈补位」的银行侧一半：注册领走一个之后，app 调一次，池子就长回一格，所以
    池子永远不会用尽，也不会有人需要手工 `--count`。

    并发时两个补位请求会算出同一个号，唯一约束是唯一仲裁者——撞了就重算，落到下一个
    号。**返回 None 不是错误**：它只意味着这次水位没补上，下一个注册会再试，任何用户
    都不受影响。这比为了补一格而把失败传播给注册请求要划算得多。
    """

    for _ in range(attempts):
        index = next_pool_index(session)
        customer_id = pool_customer_id(index)
        try:
            # 用 SAVEPOINT：撞号只回滚这一次尝试，外层事务（如果有）留着继续重算。
            with session.begin_nested():
                provision_customer(session, customer_id=customer_id, index=index, now=now)
            return customer_id
        except (IntegrityError, BusinessError):
            continue
    return None


def _demo_birthday(today: date) -> date:
    """池客户的生日：永远在开户后的第 9 天，跨场景演示不必再手工补数据。

    生活事件域只列「登记了生日」的联系人；日期放在 9 天后，既超过准备订单要求的
    2 天提前量，又不会因为环境重建而失效。2 月 29 日出生在非闰年落到 28 日。
    """

    target = today + timedelta(days=9)
    try:
        return target.replace(year=today.year - 30)
    except ValueError:
        return target.replace(year=today.year - 30, day=28)


def provision_customer(
    session: Session, *, customer_id: str, index: int, now: datetime
) -> dict:
    """建一个完整可用的演示客户：账户、开户流水、联系人、订阅、安全状态。

    故意不建持仓和风险测评：那两样是用户自己走流程才会有的东西，预先塞进去会让「第一次
    买理财」的对话看起来像在跟一个已经有持仓的人说话。
    """

    if session.get(User, customer_id) is not None:
        raise BusinessError(f"客户已存在: {customer_id}")

    session.add(
        User(
            id=customer_id,
            name=f"演示客户{index:02d}",
            phone=f"{POOL_PHONE_PREFIX}{index:08d}",
            risk_level="medium",
            status="active",
        )
    )
    session.flush()

    current = _open_account(
        session,
        customer_id=customer_id,
        suffix="current",
        account_type="current",
        opening_minor=CURRENT_OPENING_MINOR,
        now=now,
    )
    _open_account(
        session,
        customer_id=customer_id,
        suffix="fixed",
        account_type="fixed",
        opening_minor=FIXED_OPENING_MINOR,
        now=now,
    )

    session.add(
        Contact(
            id=f"c_{customer_id}_family",
            user_id=customer_id,
            name="家人",
            phone=f"{POOL_PHONE_PREFIX}9{index:07d}",
            remark="演示联系人",
            birthday=_demo_birthday(now.date()),
        )
    )
    session.add(
        Subscription(
            id=f"sub_{customer_id}_video",
            user_id=customer_id,
            from_account_id=current.id,
            name="演示视频会员",
            merchant_name="演示视频平台",
            amount_minor=2500,
            frequency="monthly",
            start_date=now.date(),
            next_due_date=(now + timedelta(days=10)).date(),
            status="active",
            auto_renew=True,
        )
    )
    session.add(
        SecurityState(
            user_id=customer_id,
            tool_name="fund_transfer",
            failure_count=0,
            locked_until=None,
            last_reason="ok",
        )
    )
    return {"customer_id": customer_id, "accounts": [current.id]}


def _open_account(
    session: Session,
    *,
    customer_id: str,
    suffix: str,
    account_type: str,
    opening_minor: int,
    now: datetime,
) -> Account:
    account = Account(
        id=f"a_{customer_id}_{suffix}",
        user_id=customer_id,
        account_type=account_type,
        balance_minor=0,
        available_minor=0,
        interest_rate=Decimal("0.00300") if account_type == "current" else Decimal("0.02500"),
        status="active",
    )
    session.add(account)
    session.flush()
    # 余额只能由流水推导出来，开户也一样：直接写 balance_minor 会让「余额 = 流水之和」
    # 这条对账不变式在第一个客户身上就破掉。
    post_entry(
        session,
        account=account,
        direction=CREDIT,
        amount_minor=opening_minor,
        transaction_type="transfer_in",
        category="other",
        remark="开户入金",
        created_at=now,
        transaction_id=f"txn_{customer_id}_{suffix}_open",
    )
    return account
