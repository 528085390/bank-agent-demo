"""Import the existing billing contract verbatim as its own revision."""

from pathlib import Path

from alembic import op

revision = "0001_billing"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    baseline = Path(__file__).resolve().parents[5] / "docs/contracts/sql/001_init.sql"
    sql = baseline.read_text(encoding="utf-8")
    # The source is a standalone transaction; Alembic owns this transaction instead.
    sql = sql.replace("\nBEGIN;", "\n", 1).replace("\nCOMMIT;", "\n", 1)
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("账单基线不自动删除；请按账单模块的数据迁移流程操作")
