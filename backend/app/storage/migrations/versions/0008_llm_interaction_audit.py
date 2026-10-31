"""The audit domain: every model call, and every run fact worth tracing.

Revision ID: 0008_llm_interaction_audit
Revises: 0007_conversation_persistence

Two tables, and neither of them is a second conversation or a second execution state:

- `llm_call_audit` is one row per *attempt* at a model call, carrying the whole request and
  the whole answer. A retry appends a second row; nothing is ever updated.
- `agent_run_event` is one row per fact about a run -- started, tool call, tool result,
  interrupted, resumed, succeeded, failed. The LangGraph checkpoint stays the only thing
  that decides what a run may do next; these rows are evidence, not state.

Neither table has a foreign key. An audit row must stay writable and readable for a
conversation that has been deleted, and deleting a conversation must never delete the audit
trail, so the correlation columns are plain strings with indexes of their own. `downgrade`
drops the two tables in the reverse order they were created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_llm_interaction_audit"
down_revision = "0007_conversation_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_call_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("audit_id", sa.String(64), nullable=False, unique=True),
        sa.Column("call_id", sa.String(64), nullable=False, unique=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column(
            "request_format", sa.String(32), nullable=False, server_default="chat_completions"
        ),
        sa.Column("prompt_json", postgresql.JSONB(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("result IN ('SUCCEEDED','FAILED')", name="ck_llm_call_audit_result"),
    )
    op.create_index(
        "ix_llm_call_audit_conversation", "llm_call_audit", ["conversation_id", "created_at"]
    )
    op.create_index("ix_llm_call_audit_run", "llm_call_audit", ["run_id"])
    op.create_index("ix_llm_call_audit_created", "llm_call_audit", ["created_at"])

    op.create_table(
        "agent_run_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("call_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_run_event_conversation", "agent_run_event", ["conversation_id", "created_at"]
    )
    op.create_index("ix_agent_run_event_run", "agent_run_event", ["run_id", "created_at"])
    op.create_index("ix_agent_run_event_call", "agent_run_event", ["call_id"])
    op.create_index("ix_agent_run_event_type", "agent_run_event", ["event_type", "created_at"])


def downgrade() -> None:
    # Reverse creation order. The two tables share no key, so this order is convention rather
    # than necessity -- and it is still spelled out, because a downgrade that has to be
    # reasoned about is a downgrade somebody will get wrong.
    for index, table in (
        ("ix_agent_run_event_type", "agent_run_event"),
        ("ix_agent_run_event_call", "agent_run_event"),
        ("ix_agent_run_event_run", "agent_run_event"),
        ("ix_agent_run_event_conversation", "agent_run_event"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("agent_run_event")
    for index in (
        "ix_llm_call_audit_created",
        "ix_llm_call_audit_run",
        "ix_llm_call_audit_conversation",
    ):
        op.drop_index(index, table_name="llm_call_audit")
    op.drop_table("llm_call_audit")
