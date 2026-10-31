"""客户池的自愈补位。

池子的设计取舍写在 `bank_backend/app/domain/pool_service.py` 的模块注释里：预生成 N 套
客户，注册时在 app 库单事务里领一个，避免跨库建客户的分布式状态机。代价是池子会空，
而空了之后新注册的用户**没有任何回补路径**——他们的 `bank_customer_id` 是 NULL，补池
脚本也只恢复「已经领过」的人。

这里把那个代价补上：每成功领走一个，就让银行侧再建一个补到池尾。于是池子维持水位，
不需要人盯，也不需要任务表和状态机。

三个失败容忍点，都是有意为之：

1. **补不上就不补。** `refill()` 永不抛异常。注册已经成功，池子少一格是运维问题，不是
   这个用户的问题——把它变成注册失败，等于用最贵的代价处理最不要紧的事。
2. **跨两库只做加法。** 银行侧建客户 -> app 库加一行池记录。中间失败会留下一个「银行里
   存在但 app 不知道」的客户；因为它占的是池号，下次补位会算出同一个号并被幂等跳过，
   所以残留会自动被吸收，不会被浪费。
3. **号码冲突交给数据库。** 两个并发注册各补一次，会算出同一个号；银行侧靠唯一约束重算
   下一个号，app 侧靠 `ON CONFLICT DO NOTHING` 忽略重复行。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.integration.bank_errors import BankCallError

logger = logging.getLogger(__name__)

PROVISION_PATH = "/pool/provision"


class PoolRefiller:
    """一个尽力而为的补位器：拿一个号，写一行池记录。"""

    def __init__(self, database, bank_client) -> None:
        self.database = database
        self.bank_client = bank_client

    def refill(self) -> str | None:
        """补一格到池尾（谁也没领），返回新客户号；任何一步失败都返回 None。"""

        customer_id = self._provision()
        if customer_id is None:
            return None
        self._record(customer_id, user_id=None)
        return customer_id

    def provision_for(self, user_id: str) -> str | None:
        """池子当时是空的：直接建一个并绑给这位用户。

        这条路径只在注册撞上空池（或注册时银行不可用）时走。它和 `refill` 的区别只是
        池表那一行记不记领取人，客户本身的创建方式完全一样——所以它天然幂等：重试拿到
        的号要么已存在（银行侧跳过并继续算下一个），要么被 `ON CONFLICT` 忽略。
        """

        customer_id = self._provision()
        if customer_id is None:
            return None
        # 绑不上就不认这个号：返回 id 会让注册接口在响应里宣称一个并不存在的绑定。
        if not self._record(customer_id, user_id=user_id):
            return None
        return customer_id

    def _provision(self) -> str | None:
        try:
            response = self.bank_client.service_request("POST", PROVISION_PATH)
        except BankCallError as exc:
            logger.warning("客户池补位失败（银行侧）：%s", exc.kind)
            return None
        data = response.data or {}
        customer_id = data.get("customer_id") if isinstance(data, dict) else None
        if not customer_id:
            # 银行侧重试也没抢到号：池子正被批量补，下次再说。
            return None
        return str(customer_id)

    def _record(self, customer_id: str, *, user_id: str | None) -> bool:
        """把这一格记账；绑定用户时一并写 `app_user.bank_customer_id`。

        两处写入放在同一个事务里，因为「池表说这号归他」和「用户说他绑了这号」如果只成
        一半，之后没人能分清哪个是真的。

        先更新用户再记池行，并且**以用户更新是否命中为准**：如果那个用户已经不在了
        （并发注销、或者调用方拼错了 id），这一格绝不能写成「已被某人领取」——那会让一个
        没人持有的号永远无法再被领走。写不成就退回成未领取的池行，等于把格子放回池尾。
        """

        try:
            with self.database.sessions.begin() as db:
                bound = False
                if user_id is not None:
                    result = db.execute(
                        text(
                            """
                            UPDATE app_user
                            SET bank_customer_id = :customer_id, updated_at = now()
                            WHERE user_id = :user_id
                            """
                        ),
                        {"customer_id": customer_id, "user_id": user_id},
                    )
                    bound = result.rowcount > 0
                claimed_at = datetime.now(timezone.utc) if bound else None
                db.execute(
                    text(
                        """
                        INSERT INTO bank_customer_pool (customer_id, claimed_by_user_id, claimed_at)
                        VALUES (:customer_id, :claimed_by, :claimed_at)
                        ON CONFLICT (customer_id) DO NOTHING
                        """
                    ),
                    {
                        "customer_id": customer_id,
                        "claimed_by": user_id if bound else None,
                        "claimed_at": claimed_at,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 补位失败绝不能影响注册
            logger.warning("客户池补位失败（app 库写入）：%s", exc)
            return False
        # 纯补位（没有用户）只要行写进去了就算成功；绑定用户时还要求那条 UPDATE 真的命中。
        return user_id is None or bound
