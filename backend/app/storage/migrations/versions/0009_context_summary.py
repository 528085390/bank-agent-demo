"""Prompt-layer compression: the summaries a conversation's ledger has been folded into.

Revision ID: 0009_context_summary
Revises: 0008_llm_interaction_audit

One table, and it is a *derived view* of the ledger rather than a second copy of it:

- `conversation_message` stays the single source of truth for what the user and the
  assistant saw. A row in `context_summary` records that a contiguous stretch of that
  ledger was summarised, which summary it was derived from, and what came out. Nothing
  about a run's lifecycle is stored here, so a row here can never disagree with the ledger
  about what happened.
- `version` is unique per conversation (`uq_context_summary_version`): two summaries
  claiming the same number for the same conversation is the one inconsistency the database
  itself can refuse.
- `ix_context_summary_conversation_version` serves the lookup that compression performs on
  every turn -- the latest version of this conversation.

`base_summary_id` is deliberately a plain `String(64)` with no self-referencing foreign key.
The chain's integrity is checked by the application layer, which writes the versions in
order; a self-referencing key would make each row's insertability depend on its predecessor
already being present, so a write the application has already ordered would have to satisfy
the database's order as well. The column is nullable because version 1 summarises raw
ledger messages and was derived from no summary at all.

`covered_from_sequence` and `covered_to_sequence` are `BigInteger`, the same type as
`conversation_message.sequence`. They are inclusive bounds and are not foreign keys: they
name a *range* of the ledger, whose ends are ordinary integers rather than two particular
rows. `downgrade` drops the index before the table, the reverse of how they were created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_context_summary"
down_revision = "0008_llm_interaction_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_summary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("summary_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversation.conversation_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_summary_id", sa.String(64), nullable=True),
        sa.Column("covered_from_sequence", sa.BigInteger(), nullable=False),
        sa.Column("covered_to_sequence", sa.BigInteger(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "version", name="uq_context_summary_version"),
    )
    op.create_index(
        "ix_context_summary_conversation_version",
        "context_summary",
        ["conversation_id", sa.text("version DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_context_summary_conversation_version", table_name="context_summary")
    op.drop_table("context_summary")
