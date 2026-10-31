"""External migration compatibility must be read-only and fail on missing columns."""

from sqlalchemy import text

from scripts.check_runtime_schema import missing_runtime_columns


def test_existing_schema_compatibility_preserves_identity_and_rejects_missing_field(rt):
    engine = rt.database.engine
    with engine.connect() as connection:
        before = connection.execute(text("SELECT user_id, bank_customer_id FROM app_user")).all()
    assert missing_runtime_columns(engine) == []
    with engine.begin() as connection:
        assert (
            connection.execute(text("SELECT user_id, bank_customer_id FROM app_user")).all()
            == before
        )
        connection.execute(text("ALTER TABLE app_user DROP COLUMN email"))
    assert missing_runtime_columns(engine) == ["app_user.email"]
