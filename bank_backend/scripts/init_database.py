"""Create the mock bank's own database on the configured PostgreSQL instance.

Run this once per environment, before `alembic upgrade head`:

    cd bank_backend
    uv run python scripts/init_database.py

It only ever creates the one database named here, and it is a no-op when that database
already exists -- which is why the one-click script can run it on every start. It does not
migrate, seed or touch any other database on the instance.

The connection details come from the repository root `database.txt`, or from whatever
`DATABASE_FILE` points at: the local one-click entry sets that variable so this script
creates `bank_db` on the local server instead of the cloud instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote_plus

import psycopg

# Running as a script puts `scripts/` on sys.path, not `bank_backend/`, so the `app` package
# would not be importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import BANK_DATABASE  # noqa: E402
from app.core.database_file import read_credentials  # noqa: E402


def main() -> int:
    fields = read_credentials()
    admin_dsn = (
        f"postgresql://{quote_plus(fields['账户'])}:{quote_plus(fields['密码'])}"
        f"@{fields['外网地址']}:{fields['端口']}/postgres"
    )

    # CREATE DATABASE cannot run inside a transaction, hence autocommit.
    with psycopg.connect(admin_dsn, connect_timeout=10, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (BANK_DATABASE,)
        ).fetchone()
        if exists:
            print(f"database {BANK_DATABASE} already exists; nothing to do")
            return 0
        # The name is a literal constant in app.core.config, never built from input.
        connection.execute(f'CREATE DATABASE "{BANK_DATABASE}"')
        print(f"created database {BANK_DATABASE} on {fields['外网地址']}:{fields['端口']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
