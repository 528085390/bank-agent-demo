"""业务 ID 生成。

成功写操作返回稳定业务 ID（transaction_id、task_id、order_id、holding_id、
subscription_id 等），前缀让演示时的返回值可读、可归属到具体业务域。
"""

from __future__ import annotations

import uuid

ACCOUNT = "acct"
CARD = "card"
CONTACT = "ct"
TRANSACTION = "txn"
HOLDING = "hold"
ORDER = "order"
TASK = "task"
AA = "aa"
AA_PARTICIPANT = "aap"
SUBSCRIPTION = "sub"
CHARGE = "chg"
AUDIT = "audit"
REFERENCE = "ref"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
