"""Scope the statement file hash to its owning user.

``docs/contracts/sql/001_init.sql:109-110`` declared
``statement_document.source_file_hash`` globally unique, while the import path's file-level
dedupe looks that hash up *within one user* (``app/services/bill_import.py``). A second user
uploading a byte-identical file therefore missed the lookup, hit the global constraint, and
their task ended FAILED/INTERNAL. Uniqueness moves to ``(user_id, source_file_hash)`` so the
lookup and the constraint finally agree.

``downgrade()`` is intentionally not reversible: the backfill leaves ``user_id`` NOT NULL and
nothing in a row records that its document was once shared across owners.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_bill_doc_tenant_unique"
down_revision = "0014_bill_compatibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("statement_document", sa.Column("user_id", sa.String(64), nullable=True))
    # 归属人只存在于拥有这份文档的 bill_task 上；先回填再收紧成 NOT NULL。
    op.execute(
        "UPDATE statement_document d SET user_id = t.user_id "
        "FROM bill_task t WHERE d.task_id = t.id"
    )
    op.alter_column(
        "statement_document", "user_id", nullable=False, existing_type=sa.String(64)
    )
    op.drop_constraint(
        "statement_document_source_file_hash_key", "statement_document", type_="unique"
    )
    op.create_unique_constraint(
        "uq_statement_document_user_file", "statement_document", ["user_id", "source_file_hash"]
    )


def downgrade() -> None:
    raise RuntimeError("账单文档归属迁移包含不可安全回退的数据变更，请从数据库备份恢复")
