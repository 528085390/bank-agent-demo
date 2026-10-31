"""0002_wealth_support 迁移与 seed/reset 顺序的 schema 契约测试。

覆盖计划任务 1 的断言：旧库升级时产品保留、redeem_rule 按期限回填、
新表与约束存在；重置顺序满足新的用户外键。

全部用例只操作本文件自己创建并核验名称的 schema，不触碰共享库。
"""

from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ALEMBIC_INI, _activate, _create_schema, _drop_schema, _restore

BASE_REVISION = "0001_bank_mock_domain"
HEAD_REVISION = "0002_wealth_support"

LEGACY_PRODUCTS = (
    ("p_legacy_flexible", "旧灵活产品", 0),
    ("p_legacy_fixed", "旧定期产品", 12),
)


@pytest.fixture
def schema_at_base(database_url):
    """只升到 0001 的独立 schema，用于验证旧库升级路径。"""

    name = "test_wealth_migration_" + uuid4().hex[:12]
    _create_schema(database_url, name)
    previous = _activate(database_url, name)
    try:
        command.upgrade(Config(str(ALEMBIC_INI)), BASE_REVISION)
        yield name
    finally:
        _restore(previous)
        _drop_schema(database_url, name)


@pytest.fixture
def legacy_engine(database_url, schema_at_base):
    engine = create_engine(database_url, connect_args={"connect_timeout": 10})
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_legacy_products(engine) -> None:
    with engine.begin() as connection:
        for product_id, name, months in LEGACY_PRODUCTS:
            connection.execute(
                text(
                    "INSERT INTO products "
                    "(id, name, product_type, min_amount_minor, max_amount_minor, "
                    " expected_return, risk_level, duration_months, status) "
                    "VALUES (:id, :name, 'fixed_term', 100, 100000000, 0.03500, "
                    "        'low', :months, 'available')"
                ),
                {"id": product_id, "name": name, "months": months},
            )


def _upgrade_to_head() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


def test_migration_preserves_old_products_and_backfills_redeem_rule(legacy_engine):
    _insert_legacy_products(legacy_engine)
    _upgrade_to_head()

    with legacy_engine.connect() as connection:
        rows = dict(
            connection.execute(
                text("SELECT id, redeem_rule FROM products ORDER BY id")
            ).all()
        )
        assert rows == {"p_legacy_flexible": "flexible", "p_legacy_fixed": "maturity_only"}

        tables = set(inspect(legacy_engine).get_table_names())
        assert "wealth_risk_assessments" in tables
        columns = {column["name"]: column for column in inspect(legacy_engine).get_columns("products")}
        assert columns["redeem_rule"]["nullable"] is False

        # 约束生效：非法赎回规则被库层拒绝。
        with pytest.raises(IntegrityError):
            connection.execute(
                text("UPDATE products SET redeem_rule = 'whenever' WHERE id = 'p_legacy_fixed'")
            )


def test_new_table_has_user_foreign_key_and_index(legacy_engine):
    _upgrade_to_head()

    inspector = inspect(legacy_engine)
    columns = {column["name"] for column in inspector.get_columns("wealth_risk_assessments")}
    assert {
        "id",
        "user_id",
        "answers",
        "score",
        "risk_level",
        "rule_version",
        "assessed_at",
        "expires_at",
    } <= columns

    foreign_keys = inspector.get_foreign_keys("wealth_risk_assessments")
    assert [key["referred_table"] for key in foreign_keys] == ["users"]
    assert foreign_keys[0]["options"].get("ondelete") == "RESTRICT"

    index_names = {index["name"] for index in inspector.get_indexes("wealth_risk_assessments")}
    assert "ix_wealth_risk_user_assessed" in index_names


def test_downgrade_removes_wealth_objects(legacy_engine):
    _upgrade_to_head()
    command.downgrade(Config(str(ALEMBIC_INI)), BASE_REVISION)

    inspector = inspect(legacy_engine)
    assert "wealth_risk_assessments" not in set(inspector.get_table_names())
    assert "redeem_rule" not in {
        column["name"] for column in inspector.get_columns("products")
    }


def test_seed_reset_respects_new_foreign_key(database, client):
    """有测评记录时显式重置不因新外键失败，且重置后测评被清空。"""

    from app.domain.seed_service import SeedService
    from app.storage.models import User, WealthRiskAssessment

    SeedService(database).seed()
    response = client.post(
        "/api/wealth/risk-assessments",
        json={"user_id": "u_zhang", "answers": {"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2}},
    )
    assert response.status_code == 201, response.text

    with database.sessions() as session:
        count = session.scalar(select(func.count()).select_from(WealthRiskAssessment))
        assert count == 1

    SeedService(database).reset()

    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(WealthRiskAssessment)) == 0
        assert session.scalar(select(func.count()).select_from(User)) == 4
