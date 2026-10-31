"""Align the billing baseline with the integrated manual-upload pipeline.

The target project already owns the Alembic chain, so the source project's 0007-0010
revisions are intentionally consolidated here instead of creating a second branch.

由 dev_Analyze 的 0013_bill_compatibility 重编号而来，避免与 dev 的 0013_agent_run_event_call_id 形成双 head；`downgrade()` 不可回滚。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_bill_compatibility"
down_revision = "0013_agent_run_event_call_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "bill_transaction", "amount", type_=sa.BigInteger(),
        existing_type=sa.Numeric(18, 2), postgresql_using="(amount * 100)::bigint",
    )
    op.alter_column(
        "bill_transaction", "balance_after", type_=sa.BigInteger(), nullable=True,
        existing_type=sa.Numeric(18, 2), postgresql_using="(balance_after * 100)::bigint",
    )
    for column in ("credit_total", "debit_total", "net_total"):
        op.alter_column(
            "bill_summary", column, type_=sa.BigInteger(),
            existing_type=sa.Numeric(18, 2), postgresql_using=f"({column} * 100)::bigint",
        )

    op.drop_constraint("bill_transaction_direction_check", "bill_transaction", type_="check")
    op.execute("UPDATE bill_transaction SET direction = lower(direction)")
    op.create_check_constraint(
        "bill_transaction_direction_check", "bill_transaction",
        "direction IN ('debit', 'credit')",
    )
    op.drop_constraint("bill_task_state_check", "bill_task", type_="check")
    op.create_check_constraint(
        "bill_task_state_check", "bill_task",
        "state IN ('WAITING_APPLICATION','WAITING_UPLOAD','WAITING_PASSWORD','FETCHING',"
        "'WAITING_EMAIL','WAITING_SELECTION','EXTRACTING','PARSING','SAVING','ANALYZING',"
        "'COMPLETED','FAILED','CANCELLED')",
    )
    op.drop_constraint("bill_task_source_mode_check", "bill_task", type_="check")
    op.create_check_constraint(
        "bill_task_source_mode_check", "bill_task",
        "source_mode IN ('EMAIL','LOCAL_SAMPLE','manual_upload')",
    )
    op.alter_column(
        "bill_task", "user_id", type_=sa.String(64), existing_type=sa.Uuid(),
        postgresql_using="user_id::text",
    )
    op.create_foreign_key(
        "fk_bill_task_user_id", "bill_task", "app_user", ["user_id"], ["user_id"],
    )
    op.add_column("bill_task", sa.Column("uploaded_file_path", sa.Text(), nullable=True))
    op.add_column("bill_task", sa.Column("uploaded_file_hash", sa.String(64), nullable=True))
    op.add_column("bill_task", sa.Column("uploaded_filename", sa.Text(), nullable=True))
    op.add_column("bill_task", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bill_audit_event", sa.Column("user_id", sa.String(64), nullable=True))
    op.add_column("bill_transaction", sa.Column("booked_at", sa.DateTime(), nullable=True))
    op.add_column("bill_transaction", sa.Column("product", sa.Text(), nullable=False, server_default=""))
    op.create_index("ix_bill_task_uploaded_file_hash", "bill_task", ["uploaded_file_hash"])
    op.create_index("ix_bill_transaction_raw_record_hash", "bill_transaction", ["raw_record_hash"])


def downgrade() -> None:
    raise RuntimeError("账单兼容迁移包含不可安全回退的数据类型变更，请从数据库备份恢复")
