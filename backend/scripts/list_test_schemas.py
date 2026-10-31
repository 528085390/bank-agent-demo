"""List the test schemas still present in the shared test database.

Used to confirm a run cleaned up after itself (and to find leftovers from a killed run).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.database_file import sqlalchemy_url

engine = create_engine(sqlalchemy_url("test"))
with engine.connect() as connection:
    rows = [
        row[0]
        for row in connection.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'test_%' ORDER BY 1"
            )
        )
    ]
print(f"{len(rows)} test schema(s):")
for name in rows:
    print(" ", name)
engine.dispose()
