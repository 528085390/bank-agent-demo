"""The shared database credentials, in one place.

The repository root `database.txt` is committed on purpose: every developer and the CI use
the same cloud instance, so a fresh clone must work without anyone configuring anything. It
records an account, a password, a host and a port, but no database name -- each project
names its own database on that instance, so the name lives next to the code that uses it.

This file is the single source of truth for the password. To rotate it, edit the repository
root `database.txt` and nothing else.

`DATABASE_FILE` points `read_credentials` at another file of the same four-line shape. The
one-click local script sets it to `database.local.txt` so the application, both alembic
environments, the seeds and the init scripts all move to the local PostgreSQL at once. It is
deliberately a credentials-file switch and not a DSN: this project names its database
`bank_db` and backend names its own `test`, and a single `DATABASE_URL` variable cannot
give two sibling processes two different database names.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

# bank_backend/app/core/database_file.py -> the repository root.
DATABASE_FILE = Path(__file__).resolve().parents[3] / "database.txt"

REQUIRED_FIELDS = ("账户", "密码", "外网地址", "端口")


def resolve_database_file() -> Path:
    """The credentials file to read: `DATABASE_FILE` when set, the committed default else."""

    override = os.getenv("DATABASE_FILE", "").strip()
    return Path(override) if override else DATABASE_FILE


def read_credentials(path: Path | None = None) -> dict[str, str]:
    """Read the four labelled lines of `database.txt`."""

    target = path or resolve_database_file()
    if not target.exists():
        raise RuntimeError(
            f"missing {target}: the shared database credentials are committed, so a "
            "missing file means the checkout is incomplete"
        )
    fields: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if "：" in line:
            key, _, value = line.partition("：")
            fields[key.strip()] = value.strip()
    missing = [key for key in REQUIRED_FIELDS if not fields.get(key)]
    if missing:
        raise RuntimeError(f"{target.name} is missing: {', '.join(missing)}")
    return fields


def sqlalchemy_url(database: str) -> str:
    """The `postgresql+psycopg://` DSN for one database on the shared instance."""

    fields = read_credentials()
    return (
        f"postgresql+psycopg://{quote_plus(fields['账户'])}:{quote_plus(fields['密码'])}"
        f"@{fields['外网地址']}:{fields['端口']}/{database}"
    )
