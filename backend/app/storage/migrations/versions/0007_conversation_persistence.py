"""Conversations, the message ledger and idempotent submissions.

Revision ID: 0007_conversation_persistence
Revises: 0006_bank_customer_id

Three tables, and deliberately no fourth:

- `conversation` owns the LangGraph `thread_id`, so a conversation migrated from the old
  `chat:{session_id}` scheme keeps the checkpoint it already has.
- `conversation_message` is the append-only ledger of what the user and the assistant saw.
  `run_id` is a plain correlation column -- there is no `conversation_run` table, no
  lifecycle state machine and no foreign key from a message to a run.
- `conversation_submission` is the idempotency receipt: one row per (user, operation, key).

There are no circular foreign keys: both messages and submissions point at `conversation`,
and a submission also points at the message it produced, so `downgrade` drops them in the
reverse order -- submission, message, conversation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_conversation_persistence"
down_revision = "0006_bank_customer_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("app_user.user_id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_message_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("thread_id", sa.String(128), nullable=False, unique=True),
    )
    op.create_index("ix_conversation_user_id", "conversation", ["user_id"])
    op.create_index(
        "ix_conversation_owner_state", "conversation", ["user_id", "deleted_at", "archived_at"]
    )

    op.create_table(
        "conversation_message",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("message_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversation.conversation_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_kind", sa.String(32), nullable=False, server_default="message"),
        sa.Column("question_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_message_sequence"),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_conversation_message_role"),
        sa.CheckConstraint(
            "(message_kind = 'interrupt_answer' AND question_id IS NOT NULL)"
            " OR (message_kind <> 'interrupt_answer' AND question_id IS NULL)",
            name="ck_conversation_message_question",
        ),
    )
    op.create_index("ix_conversation_message_run", "conversation_message", ["run_id"])

    op.create_table(
        "conversation_submission",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("submission_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("app_user.user_id"), nullable=False),
        sa.Column(
            "operation_type", sa.String(32), nullable=False, server_default="conversation_message"
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversation.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.String(64),
            sa.ForeignKey("conversation_message.message_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "operation_type",
            "idempotency_key",
            name="uq_conversation_submission_key",
        ),
    )
    op.create_index("ix_conversation_submission_user_id", "conversation_submission", ["user_id"])


def downgrade() -> None:
    # Reverse dependency order: the submission points at the message, both point at the
    # conversation. Dropping in any other order needs CASCADE and would hide the ordering
    # mistake this list exists to make explicit.
    op.drop_index("ix_conversation_submission_user_id", table_name="conversation_submission")
    op.drop_table("conversation_submission")
    op.drop_index("ix_conversation_message_run", table_name="conversation_message")
    op.drop_table("conversation_message")
    op.drop_index("ix_conversation_owner_state", table_name="conversation")
    op.drop_index("ix_conversation_user_id", table_name="conversation")
    op.drop_table("conversation")
