"""Widen the tool-call audit id so the id the Supervisor mints fits.

Revision ID: 0013_agent_run_event_call_id
Revises: 0012_llm_call_audit_stream

`agent_run_event.call_id` was `String(64)`, but the Supervisor mints
`f"{agent_id}:{tool_name}:{uuid4().hex}"` for every tool call (supervisor.py:1356 for the
idempotency key and :1373 for the call id), and ToolGateway writes that call id to this
column before the adapter is handed the call (gateway.py:209 vs :215). The two columns
beside it already declare the widths the id is built from -- `agent_id` is `String(64)`
and `tool_name` is `String(128)` -- so the declared maximum is
64 + 1 + 128 + 1 + 32 = 226. With `agent_id = 'subscription'` (12 characters), five of the
module's seven tools minted 65-66 characters and the INSERT raised
`psycopg.errors.StringDataRightTruncation`, so the tool never ran: every subscription write
ends at `subscription_execute`, which was one of the five.

256 is the smallest round number at or above the declared maximum. The widening is a
metadata-only `ALTER TABLE ... TYPE` in PostgreSQL, so it does not rewrite the table.
`downgrade` narrows it back to 64: a row the narrower column cannot hold is exactly the
row this migration exists to admit, and a downgrade should fail loudly rather than
silently truncate it.

The revision id is kept to 28 characters because `alembic_version.version_num` is
`String(32)`; a longer id fails the version-stamp UPDATE after the column change has run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_agent_run_event_call_id"
down_revision = "0012_llm_call_audit_stream"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_run_event",
        "call_id",
        existing_type=sa.String(64),
        type_=sa.String(256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "agent_run_event",
        "call_id",
        existing_type=sa.String(256),
        type_=sa.String(64),
        existing_nullable=True,
    )
