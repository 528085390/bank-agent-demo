"""Framework-only tables. Billing tables remain owned by 0001."""

from alembic import op

from app.storage.migrations.schema_v2 import Base

revision = "0002_framework"
down_revision = "0001_billing"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(op.get_bind())


def downgrade():
    Base.metadata.drop_all(op.get_bind())
