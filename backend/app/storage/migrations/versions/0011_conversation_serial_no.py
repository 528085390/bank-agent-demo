"""Number every conversation, per owner, in the order it was created.

Revision ID: 0011_conversation_serial_no
Revises: 0010_business_audit_event

A conversation gets a small number of its own -- 1, 2, 3 -- scoped to the user who owns it,
so a user can say "the third one" and mean the same row tomorrow. Two properties follow from
that intent and drive the shape below:

- The number is a property of the *row*, not of the list. Archiving or soft-deleting a
  conversation does not hand its number to anyone else: numbers are assigned once and never
  recycled, so a number that named a row yesterday names that same row today.
- The numbers are contiguous from 1 per owner, which means the backfill has to invent them
  for conversations that already exist. It walks `(user_id, created_at, conversation_id)`:
  oldest first within an owner, with the conversation id breaking ties so that two rows
  written in the same instant still land in a stable, reproducible order. That is the order
  the history was written in, and reading the list back by number reads as the history did.

The column is added nullable, filled, then made NOT NULL: an `ALTER TABLE ... SET NOT NULL`
is only legal once no row is left without a value. `uq_conversation_serial_no_owner` is the
constraint that keeps a number unique within its owner, and it is what a concurrent pair of
creates collides with -- `ConversationService.create` takes a lock on the owner row so that
the collision cannot happen in the first place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_conversation_serial_no"
down_revision = "0010_business_audit_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("serial_no", sa.BigInteger(), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT conversation_id, user_id FROM conversation"
            " ORDER BY user_id, created_at, conversation_id"
        )
    ).all()
    owner: str | None = None
    serial_no = 0
    for conversation_id, user_id in rows:
        if user_id != owner:
            owner = user_id
            serial_no = 0
        serial_no += 1
        connection.execute(
            sa.text(
                "UPDATE conversation SET serial_no = :serial_no"
                " WHERE conversation_id = :conversation_id"
            ),
            {"serial_no": serial_no, "conversation_id": conversation_id},
        )

    op.alter_column("conversation", "serial_no", nullable=False)
    op.create_unique_constraint(
        "uq_conversation_serial_no_owner", "conversation", ["user_id", "serial_no"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_conversation_serial_no_owner", "conversation", type_="unique")
    op.drop_column("conversation", "serial_no")
