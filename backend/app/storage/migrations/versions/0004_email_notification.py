"""新增邮件通知表与用户邮箱字段。

Revision ID: 0004_email_notification
Revises: 0003_audit_sequence
Create Date: 2026-09-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_email_notification"
down_revision = "0004_base_without_governance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("app_user", sa.Column("email", sa.String(255), nullable=True))
    op.create_table(
        "email_notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("email_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id"),
    )
    op.create_index("ix_email_notification_task", "email_notification", ["task_id"])
    op.create_index("ix_email_notification_status", "email_notification", ["status", "created_at"])


def downgrade():
    op.drop_index("ix_email_notification_status", table_name="email_notification")
    op.drop_index("ix_email_notification_task", table_name="email_notification")
    op.drop_table("email_notification")
    op.drop_column("app_user", "email")
