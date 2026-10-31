"""宽一点的通知关联号。

``email_notification.task_id`` 是「这封通知为哪一次操作发的」的关联标识，不是主键
（唯一键是 ``notification_id``）。它由调用方拼出来，分析报告的拼法是
``analysis_{user_id}_{period}``：

* 旧演示账号的 ``user_id`` 是 ``user_0001``（9 字符），整串约 42 字符；
* 真实账号的 ``user_id`` 是 ``user_`` + 32 位十六进制（37 字符），加上
  ``2026-05-02 ~ 2026-05-29`` 这样的期间，整串约 70 字符。

超过原来的 64 后，写通知记录会被 Postgres 拒绝（``StringDataRightTruncation``），
邮件因此发不出去。把列加长即可，**不需要改生成规则**，关联号仍完整携带用户与期间。

回滚到 64 时如果库里已有更长的值，Postgres 会直接报错——这是期望行为：静默截断
会破坏关联，宁可让迁移失败。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_email_notification_task_id"
down_revision = "0020_merge_bill_period_and_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "email_notification",
        "task_id",
        existing_type=sa.String(64),
        type_=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 已有 >64 字符的 id 时，这一步会被数据库拒绝——不替数据做截断。
    op.alter_column(
        "email_notification",
        "task_id",
        existing_type=sa.String(128),
        type_=sa.String(64),
        existing_nullable=False,
    )
