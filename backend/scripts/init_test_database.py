"""Create the shared development and test database on the cloud instance.

Run this once per environment, before `pytest`:

    cd backend
    uv run python scripts/init_test_database.py

It only ever creates the one database named here. It does not migrate it, seed it or
touch any other database on the instance: the test fixtures create a private schema per
test inside it and drop that schema when the test ends. Being able to run this command
twice is the point -- it is a no-op when the database already exists.

Development and test share this database. They can, because the tests never use its
`public` schema: each test gets a schema of its own, so development's tables and a test
run's tables cannot see each other. The main database of the other project on this
instance is not touched by anything here.

The connection details come from the repository root `database.txt`, which is committed and
records an account, a password, a host and a port. That file names no database of its own,
so the database name lives here rather than there.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote_plus

import psycopg

# Running as a script puts `scripts/` on sys.path, not `backend/`, so the `app` package
# would not be importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database_file import read_credentials  # noqa: E402

TEST_DATABASE = "test"


def main() -> int:
    fields = read_credentials()
    admin_dsn = (
        f"postgresql://{quote_plus(fields['账户'])}:{quote_plus(fields['密码'])}"
        f"@{fields['外网地址']}:{fields['端口']}/postgres"
    )

    # CREATE DATABASE cannot run inside a transaction, hence autocommit.
    with psycopg.connect(admin_dsn, connect_timeout=10, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)
        ).fetchone()
        if exists:
            print(f"database {TEST_DATABASE} already exists; nothing to do")
            return 0
        # The name is a literal in this file, never built from input.
        connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')
        print(f"created database {TEST_DATABASE} on {fields['外网地址']}:{fields['端口']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
