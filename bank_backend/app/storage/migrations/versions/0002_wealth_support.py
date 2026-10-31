"""wealth support

Revision ID: 0002_wealth_support
Revises: 0001_bank_mock_domain
Create Date: 2026-10-05

理财子智能体所需的银行侧最小迁移，不改动 0001 已使用的结构：

- `products` 新增 `redeem_rule`（flexible / maturity_only），并按 `duration_months`
  回填旧数据：0 个月为 flexible，正数为 maturity_only；
- 新增 `wealth_risk_assessments`：每次问卷追加一条，保留历史，不覆盖旧记录。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_wealth_support"
down_revision: str | None = "0001_bank_mock_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "redeem_rule",
            sa.String(length=16),
            server_default="flexible",
            nullable=False,
        ),
    )
    # 旧数据回填：先给默认值，再按期限改写，最后才加约束。
    op.execute(
        "UPDATE products SET redeem_rule = "
        "CASE WHEN duration_months = 0 THEN 'flexible' ELSE 'maturity_only' END"
    )
    op.create_check_constraint(
        "ck_products_redeem_rule",
        "products",
        "redeem_rule IN ('flexible', 'maturity_only')",
    )

    op.create_table(
        "wealth_risk_assessments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')", name="ck_wealth_risk_level"
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 10", name="ck_wealth_risk_score"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wealth_risk_user_assessed",
        "wealth_risk_assessments",
        ["user_id", sa.literal_column("assessed_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_wealth_risk_user_assessed", table_name="wealth_risk_assessments")
    op.drop_table("wealth_risk_assessments")
    op.drop_constraint("ck_products_redeem_rule", "products", type_="check")
    op.drop_column("products", "redeem_rule")
