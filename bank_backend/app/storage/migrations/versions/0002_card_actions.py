"""Card authorization intents and a distinct loss marker."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_card_actions"
down_revision = "0001_bank_mock_domain"
branch_labels = None
depends_on = None


def upgrade():
    # Earlier card-only deployments added these objects without stamping the wealth history.
    schema = sa.inspect(op.get_bind())
    column_exists = "freeze_reason" in {item["name"] for item in schema.get_columns("cards")}
    table_exists = schema.has_table("card_actions")
    if column_exists != table_exists:
        raise RuntimeError("Incomplete card migration; inspect the existing schema before upgrading.")
    if table_exists:
        return
    op.add_column("cards", sa.Column("freeze_reason", sa.String(16), nullable=True))
    op.create_table(
        "card_actions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("parameters", JSONB, nullable=False),
        sa.Column("before", JSONB, nullable=False),
        sa.Column("permission_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verification_failures", sa.Integer, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_card_actions_user_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'authorized', 'cancelled', 'completed')",
            name="ck_card_actions_status",
        ),
        sa.CheckConstraint(
            "permission_level IN ('yellow', 'red')", name="ck_card_actions_permission"
        ),
        sa.CheckConstraint("verification_failures >= 0", name="ck_card_actions_failures"),
    )
    op.create_index("ix_card_actions_user_id", "card_actions", ["user_id"])


def downgrade():
    op.drop_table("card_actions")
    op.drop_column("cards", "freeze_reason")
