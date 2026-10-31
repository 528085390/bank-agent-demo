"""Drop schemas this repository's manual E2E runs created in the shared test database.

The per-test fixtures drop their own schema in a `finally`; a *manual* run has no such hook,
so the two schemas a hand-driven end-to-end check creates are dropped here. Names must be
passed explicitly: nothing is derived from a pattern, so this can never reach a schema
belonging to someone else.

Usage:  uv run python scripts/drop_test_schemas.py test_e2e_abc123 test_e2e_agent_def456
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from app.core.database_file import sqlalchemy_url

# Only these prefixes are ever dropped: a manual E2E schema, and nothing else.
ALLOWED_PREFIXES = ("test_e2e_",)


def main() -> int:
    names = sys.argv[1:]
    if not names:
        print(__doc__)
        return 2
    refused = [name for name in names if not name.startswith(ALLOWED_PREFIXES)]
    if refused:
        print("refusing to drop schemas that are not manual E2E schemas:", ", ".join(refused))
        return 2

    engine = create_engine(sqlalchemy_url("test"))
    try:
        for name in names:
            with engine.begin() as connection:
                # The name is checked against a fixed prefix above; it is quoted so it is a
                # single identifier either way.
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            print("dropped", name)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
