"""Collapse the daily total to one row per user per day.

``daily_operation_total`` was keyed by ``(user_id, business_date, operation_kind)``, but the
read side sums across kinds anyway (``app/storage/daily_total.py``), so the kind never changed
a decision -- it only split one day's money across rows. The table now keeps one row per
``(user_id, business_date)``; the operation name still reaches the audit trail, which is where
it belongs.

``upgrade()`` merges the duplicates first (summing ``total_minor`` into the oldest row of each
group), so no amount is lost on the way down to a single row. ``downgrade()`` is the exact
reverse, except that the kind cannot be recovered: every row comes back as ``transfer``, a
kind in ``FUND_WRITE_OPERATIONS``, so the merged amounts stay inside the day's total instead
of silently dropping out of it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_daily_total_single_row"
down_revision = "0016_daily_operation_total"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先合并：同一 (user_id, business_date) 只留 id 最小的那一行，金额相加，再删掉其余行。
    # 两条语句分开写：UPDATE 把总额写进保留行，DELETE 才知道该删谁。
    op.execute(
        """
        UPDATE daily_operation_total AS target
        SET total_minor = merged.total_minor,
            updated_at = now()
        FROM (
            SELECT user_id, business_date, MIN(id) AS keep_id, SUM(total_minor) AS total_minor
            FROM daily_operation_total
            GROUP BY user_id, business_date
            HAVING COUNT(*) > 1
        ) AS merged
        WHERE target.id = merged.keep_id
        """
    )
    op.execute(
        """
        DELETE FROM daily_operation_total AS extra
        USING (
            SELECT user_id, business_date, MIN(id) AS keep_id
            FROM daily_operation_total
            GROUP BY user_id, business_date
            HAVING COUNT(*) > 1
        ) AS merged
        WHERE extra.user_id = merged.user_id
          AND extra.business_date = merged.business_date
          AND extra.id <> merged.keep_id
        """
    )
    op.drop_constraint("uq_daily_op_user_date_kind", "daily_operation_total", type_="unique")
    op.drop_column("daily_operation_total", "operation_kind")
    op.create_unique_constraint(
        "uq_daily_op_user_date", "daily_operation_total", ["user_id", "business_date"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_daily_op_user_date", "daily_operation_total", type_="unique")
    # 已有的行没有类型可回填：给一个在 FUND_WRITE_OPERATIONS 里的值，合并后的金额仍被算进
    # 当日累计（回填成词表外的值会让读侧把它们静默跳过）。回填完就把默认值撤掉，让列回到
    # 0016 的「没有 server default」。
    op.add_column(
        "daily_operation_total",
        sa.Column("operation_kind", sa.String(32), nullable=False, server_default="transfer"),
    )
    op.alter_column("daily_operation_total", "operation_kind", server_default=None)
    op.create_unique_constraint(
        "uq_daily_op_user_date_kind",
        "daily_operation_total",
        ["user_id", "business_date", "operation_kind"],
    )
