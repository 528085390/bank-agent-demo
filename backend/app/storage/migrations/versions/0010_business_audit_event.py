"""Business audit: the changes this service makes to its own conversations and settings.

Revision ID: 0010_business_audit_event
Revises: 0009_context_summary

One table, and it is an *append-only ledger of intent* rather than a copy of the data it
describes:

- It records what the Agent service itself did -- renaming, pinning, archiving,
  soft-deleting and restoring a conversation, and editing the LLM configuration. Banking
  actions are a different record and stay in `bank_backend.audit_logs`.
- `result` is constrained to `('ok','rejected','failed')` by
  `ck_business_audit_event_result`: a fourth outcome is not a row the database will hold.
  This period writes only `ok` -- a refusal or a failure leaves no row at all, because the row
  is the record of a change that happened, not of an attempt that did not. `rejected` and
  `failed` keep the field closed and leave room for a later period that decides to record
  declined attempts.
- No old/new snapshot is stored. A full snapshot would be a second copy of state that can
  drift from the tables it copies.

`user_id`, `conversation_id`, `run_id` and `request_id` are plain `String(64)` with no
foreign keys, for the reason `llm_call_audit` gives: an audit row must stay writable and
readable after the conversation it names has been deleted, and deleting a conversation
must never delete the audit trail. Nothing here cascades. All four are nullable, because a
change may arrive from a background job with no conversation and no request behind it.

The three indexes serve the three lookups the audit API performs: by owner and time
(`ix_business_audit_event_user_created`), by conversation and time
(`ix_business_audit_event_conversation`), and by action
(`ix_business_audit_event_action`). `downgrade` drops the indexes before the table, the
reverse of how they were created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_business_audit_event"
down_revision = "0009_context_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_audit_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result IN ('ok','rejected','failed')", name="ck_business_audit_event_result"
        ),
    )
    op.create_index(
        "ix_business_audit_event_user_created",
        "business_audit_event",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_business_audit_event_conversation",
        "business_audit_event",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_business_audit_event_action", "business_audit_event", ["action"])


def downgrade() -> None:
    op.drop_index("ix_business_audit_event_action", table_name="business_audit_event")
    op.drop_index("ix_business_audit_event_conversation", table_name="business_audit_event")
    op.drop_index("ix_business_audit_event_user_created", table_name="business_audit_event")
    op.drop_table("business_audit_event")
