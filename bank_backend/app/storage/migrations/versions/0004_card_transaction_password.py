"""Private card transaction-password credentials; preserve merged module history."""

import sqlalchemy as sa
from alembic import op

revision = "0004_card_transaction_password"
down_revision = "0003_card_wealth_merge"
branch_labels = None
depends_on = None


def upgrade():
    from app.domain.card_password import initial_password_hash

    connection = op.get_bind()
    columns = {
        "cards": [
            sa.Column("transaction_password_hash", sa.String(256)),
            *(
                sa.Column(name, sa.Integer(), nullable=False, server_default="0")
                for name in ("password_version", "password_failures", "password_code_failures")
            ),
            sa.Column("password_locked_until", sa.DateTime(timezone=True)),
        ],
        "card_actions": [
            sa.Column("expected_password_version", sa.Integer()),
            sa.Column("pending_password_hash", sa.String(256)),
        ],
    }
    expected = {(table, column.name) for table, fields in columns.items() for column in fields}
    present = {
        (table, field["name"])
        for table in columns
        for field in sa.inspect(connection).get_columns(table)
    } & expected
    if present and present != expected:
        raise RuntimeError("交易密码结构只完成一部分，请先检查；未改写迁移历史。")
    if not present:
        for table, fields in columns.items():
            for column in fields:
                op.add_column(table, column)
    for card_id in (
        connection.execute(sa.text("SELECT id FROM cards WHERE transaction_password_hash IS NULL"))
        .scalars()
        .all()
    ):
        connection.execute(
            sa.text("UPDATE cards SET transaction_password_hash=:value WHERE id=:id"),
            {"value": initial_password_hash(), "id": card_id},
        )


def downgrade():
    for name in ("pending_password_hash", "expected_password_version"):
        op.drop_column("card_actions", name)
    for name in (
        "password_locked_until",
        "password_code_failures",
        "password_failures",
        "password_version",
        "transaction_password_hash",
    ):
        op.drop_column("cards", name)
