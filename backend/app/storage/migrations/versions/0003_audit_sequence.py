"""Preserve audit insertion order while the logical demo clock is paused."""

import sqlalchemy as sa
from alembic import op

revision = "0003_audit_sequence"
down_revision = "0002_framework"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "audit_event", sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False)
    )
    op.create_unique_constraint("uq_audit_event_sequence", "audit_event", ["sequence"])


def downgrade():
    op.drop_constraint("uq_audit_event_sequence", "audit_event", type_="unique")
    op.drop_column("audit_event", "sequence")
