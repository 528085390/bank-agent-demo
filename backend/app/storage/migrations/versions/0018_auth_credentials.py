"""Give the app real accounts: credentials, revocable login sessions, a customer pool.

Before this revision the only way in was `POST /api/session`, which minted a session for a
fixed demo user with no credential at all, and `session.session_id` doubled as the browser
token. That is fine for a single-user demo and unusable for two real accounts, so this
revision adds the pieces a login needs and leaves every existing row in place:

* `app_user` gains `status` / `role` / timestamps. Existing users become `active` / `user`,
  which is what keeps `user_0001` a demo archive rather than an administrator.
* `auth_credential` is new and starts empty. No password is invented for `user_0001`: an
  account with no credential simply cannot log in until ops sets one, and inventing a
  default password would be a backdoor shipped in a migration.
* `session` gains `token_hash` and friends. Old rows keep `token_hash = NULL`, which the
  authenticator treats as unusable -- the point is that a pre-existing row must not become
  a valid login just because the columns appeared.
* `bank_customer_pool` and `login_attempt` are new and empty; the pool is filled by
  `app.storage.seed` plus `bank_backend`'s pool script.

`downgrade()` drops what was added. It cannot un-normalise login names (they were never
stored in another form) and it cannot restore tokens, which never existed for old rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_auth_credentials"
down_revision = "0017_daily_total_single_row"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column(
        "app_user",
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
    )
    op.add_column(
        "app_user",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column(
        "app_user",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "auth_credential",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("login_name", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"]),
        sa.UniqueConstraint("user_id", name="uq_auth_credential_user"),
        sa.UniqueConstraint("login_name", name="uq_auth_credential_login_name"),
    )

    op.add_column("session", sa.Column("token_hash", sa.String(64), nullable=True))
    op.add_column("session", sa.Column("csrf_hash", sa.String(64), nullable=True))
    op.add_column("session", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("session", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "session", sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint("uq_session_token_hash", "session", ["token_hash"])
    # The authenticator looks a session up by token digest on every request, so the index is
    # the hot path, not decoration.
    op.create_index("ix_session_token_hash", "session", ["token_hash"])
    # Legacy rows get the only expiry they ever had. They stay unusable anyway (no token),
    # but a NULL ceiling would make "when does this end" unanswerable for the audit trail.
    op.execute("UPDATE session SET absolute_expires_at = expires_at WHERE absolute_expires_at IS NULL")

    op.create_table(
        "bank_customer_pool",
        sa.Column("customer_id", sa.String(64), primary_key=True),
        sa.Column("claimed_by_user_id", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["claimed_by_user_id"], ["app_user.user_id"]),
        sa.UniqueConstraint("claimed_by_user_id", name="uq_bank_customer_pool_claimant"),
    )

    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("scope", "key", name="uq_login_attempt_scope_key"),
    )


def downgrade() -> None:
    op.drop_table("login_attempt")
    op.drop_table("bank_customer_pool")

    op.drop_index("ix_session_token_hash", table_name="session")
    op.drop_constraint("uq_session_token_hash", "session", type_="unique")
    op.drop_column("session", "absolute_expires_at")
    op.drop_column("session", "revoked_at")
    op.drop_column("session", "last_seen_at")
    op.drop_column("session", "csrf_hash")
    op.drop_column("session", "token_hash")

    op.drop_table("auth_credential")

    op.drop_column("app_user", "updated_at")
    op.drop_column("app_user", "created_at")
    op.drop_column("app_user", "role")
    op.drop_column("app_user", "status")
