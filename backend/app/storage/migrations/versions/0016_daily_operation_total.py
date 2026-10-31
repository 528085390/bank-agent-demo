"""Create daily_operation_total table for operation quota tracking.

Tracks daily accumulated transaction minor units per user and operation kind.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_daily_operation_total"
down_revision = "0015_bill_doc_tenant_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_operation_total",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "business_date", "operation_kind", name="uq_daily_op_user_date_kind"),
    )
    op.create_index("ix_daily_op_user_date", "daily_operation_total", ["user_id", "business_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_op_user_date", table_name="daily_operation_total")
    op.drop_table("daily_operation_total")
