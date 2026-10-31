"""Read-only compatibility check after startup encounters an external revision.

Never changes or stamps the shared database. A compatible schema may be used without
running this checkout's older migrations or seeds over another team's schema.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect  # noqa: E402

import app.storage.bill_models  # noqa: E402, F401
from app.core.config import get_settings  # noqa: E402
from app.storage.models import Base  # noqa: E402


def missing_runtime_columns(engine):
    inspector = inspect(engine)
    missing = []
    for name, table in Base.metadata.tables.items():
        if not inspector.has_table(name):
            missing.append(name)
            continue
        columns = {column["name"] for column in inspector.get_columns(name)}
        missing.extend(f"{name}.{column}" for column in sorted(set(table.columns.keys()) - columns))
    return missing


def main():
    engine = create_engine(get_settings().database_url, hide_parameters=True)
    try:
        missing = missing_runtime_columns(engine)
        if missing:
            print("Incompatible existing schema; missing: " + ", ".join(missing))
            return 1
        print("Existing schema has all runtime tables and columns; no migrations or seeds applied.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
