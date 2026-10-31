"""Let a bill task exist before its period is known.

Uploading a statement no longer asks the user for a date range: the only range that
matters is the one the parser reads out of the file itself (``statement_document
.period_start/period_end``, computed from the actual transactions). So a task is created
with no period, and ``bill_processing`` backfills it from the parsed document once the
file has been read.

The ``period_start <= period_end`` CHECK needs no change: in SQL a comparison with NULL
is unknown, and a CHECK only fails on false.

``downgrade()`` cannot invent the range a task never had, so it fills the missing side
with the current date. That is deliberately a *value* rather than a refusal: this table's
period is display metadata for unfinished tasks, not money, and failing the whole
downgrade would leave a half-applied schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_bill_task_period_nullable"
down_revision = "0017_daily_total_single_row"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("bill_task", "period_start", existing_type=sa.Date(), nullable=True)
    op.alter_column("bill_task", "period_end", existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    # 先补值再收紧：还有未解析完的任务时，直接把列改回 NOT NULL 会失败。
    op.execute("UPDATE bill_task SET period_start = current_date WHERE period_start IS NULL")
    op.execute("UPDATE bill_task SET period_end = current_date WHERE period_end IS NULL")
    op.alter_column("bill_task", "period_end", existing_type=sa.Date(), nullable=False)
    op.alter_column("bill_task", "period_start", existing_type=sa.Date(), nullable=False)
