"""Record whether a model call was streamed.

Revision ID: 0012_llm_call_audit_stream
Revises: 0011_conversation_serial_no

The audit row already says what was sent and what came back. It could not say how: a streamed
call and a buffered one produce the same response_text, so a reader could not tell whether the
answer arrived in one piece or in deltas. The column is added NOT NULL with a server default of
false, which is the honest reading of every row written before it existed: those calls were
buffered, because streaming did not exist yet.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_llm_call_audit_stream"
down_revision = "0011_conversation_serial_no"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_call_audit",
        sa.Column("stream", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("llm_call_audit", "stream")
