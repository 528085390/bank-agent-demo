"""bind a project user to the bank customer it acts as

Revision ID: 0006_bank_customer_id
Revises: 0005_email_notification

The file name and the revision match, unlike the email migration it follows.

`schema_v2.py` is deliberately NOT touched: migration 0002 builds the table from it with
`create_all`, so a fresh database would already have this column and this migration would
fail on it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_bank_customer_id"
down_revision = "0005_email_notification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("bank_customer_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_app_user_bank_customer_id", "app_user", ["bank_customer_id"])


def downgrade() -> None:
    op.drop_constraint("uq_app_user_bank_customer_id", "app_user", type_="unique")
    op.drop_column("app_user", "bank_customer_id")
