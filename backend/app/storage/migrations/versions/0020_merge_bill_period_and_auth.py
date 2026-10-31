"""Make the bill-period and auth migrations one history again.

Two revisions were written on top of `0017_daily_total_single_row`: `0018_bill_task_period_nullable`
(from the bill work) and `0018_auth_credentials` (from the account work). Both were merged into
`dev`, and nothing here conflicts -- they touch different tables -- but Alembic saw two heads and
refused to upgrade: "Multiple head revisions are present for given argument 'head'".

The cost of leaving it was not theoretical. `start.ps1` migrates before it starts anything, and the
deploy entrypoint does the same, so both would stop at this error and no environment could reach
the account schema. This revision is the join: it changes no table, it only records that the two
branches are one line of history from here on.

`downgrade()` does nothing for the same reason, and because there is no single "before" to go back
to -- un-merging would put the two heads back, which is the state this revision exists to remove.
"""

from __future__ import annotations

revision = "0020_merge_bill_period_and_auth"
down_revision = ("0018_bill_task_period_nullable", "0019_drop_display_name")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
