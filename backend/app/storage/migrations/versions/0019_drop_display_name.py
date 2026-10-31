"""Drop `app_user.display_name`: the login name is the only name an account has.

`display_name` was a second, free-text name that nobody could change and nothing could
validate -- registration defaulted it to the login name when left blank, which is what
almost every caller did. Keeping both meant every screen had to decide which one to show,
and `app_user` could disagree with `auth_credential` about what to call the same person.

Removing it makes the login name the single answer. Where a name is displayed, it is read
from `auth_credential.login_name`; an account with no credential (the `user_0001` demo
archive) therefore has no name at all and is shown by its `user_id`, which is honest --
inventing a label for it is what this revision is undoing.

`downgrade()` re-adds the column with an empty default rather than trying to reconstruct
the old values: they were never derived from anything, so there is nothing to derive them
from. Callers that need the pre-revision shape get rows whose display name is the empty
string, which is exactly the state `register()` treated as "use the login name".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_drop_display_name"
down_revision = "0018_auth_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("app_user", "display_name")


def downgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
    )
