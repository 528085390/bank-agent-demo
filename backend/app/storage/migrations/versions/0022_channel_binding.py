"""Channel bindings: which internal user an IM identity speaks for.

Revision ID: 0022_channel_binding
Revises: 0021_email_notification_task_id

One table, and it holds the only fact the channel layer cannot derive: the platform knows a
sender by its own id (a Feishu `open_id`), every tool downstream knows a user by
`app_user.user_id`, and `channel_binding` is where the two are related. It carries no
credential and no session of its own -- `conversation_id` names the conversation the channel
last used, and `session`/`app_user` stay the authority on identity and validity.

- `uq_channel_platform_user` is unique on `(channel, platform_user_id)`: the same IM account
  cannot be claimed by two users. The channel column is part of the key so a second channel
  added later reuses this table instead of a second one.
- `ix_channel_binding_user_id` serves the reverse lookup, user to bindings.
- `user_id` is a foreign key to `app_user`, not a free string: binding a user that does not
  exist binds nobody, so the database refuses it rather than failing closed later.
- `conversation_id` is a foreign key to `conversation` for the same reason, and it is
  nullable because a binding exists before its conversation does (the channel opens one on
  first contact and writes it back). Without the constraint a binding can keep naming a
  conversation nobody has, and the channel only finds out from the 404s it gets while trying
  to post an answer.
- Neither timestamp is a server default, and neither is bare either: the ORM supplies
  `now()` on insert and on update (`default` / `onupdate` on the model). That is not the
  shape most tables in this codebase use -- those declare a bare column and the service layer
  fills it -- but it is deliberate here: the channel layer writes this row from a callback
  thread and should not have to remember to stamp it, and `updated_at` has to move by itself
  when a binding is re-pointed. Because there is no server default, an INSERT that bypasses
  the ORM still has to provide both values, which is why they are `nullable=False`.

`downgrade` drops the index before the table, the reverse of how they were created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_channel_binding"
down_revision = "0021_email_notification_task_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_binding",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("platform_user_id", sa.String(128), nullable=False),
        sa.Column("platform_username", sa.String(128), nullable=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("app_user.user_id"), nullable=False),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        sa.UniqueConstraint("channel", "platform_user_id", name="uq_channel_platform_user"),
    )
    op.create_index("ix_channel_binding_user_id", "channel_binding", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_channel_binding_user_id", table_name="channel_binding")
    op.drop_table("channel_binding")
