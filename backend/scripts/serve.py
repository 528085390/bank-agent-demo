"""Run the API.

    cd backend
    uv run python scripts/serve.py

Exists because uvicorn 0.53 hard-codes `asyncio.ProactorEventLoop` on Windows and ignores
the event loop policy, while psycopg's async connections require a selector loop. Passing
the factory explicitly is the only way to get one. Without this the server dies during
startup with "Psycopg cannot use the 'ProactorEventLoop' to run in async mode".

Binds to 127.0.0.1 unless BANK_AGENT_HOST says otherwise. Set it to 0.0.0.0 (and point
CHANNEL_MFA_BASE_URL at http://<this-machine-LAN-ip>:8000) when a phone on the same network
has to open the IM verification page: the short link carried by the Feishu card is only as
reachable as the listener behind it. Keep the default otherwise -- the demo API has no
authorisation beyond its session cookie, so it should not sit on a shared network by accident.
Loopback stays reachable either way, which is what the health check in start.ps1 polls.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.core.eventloop import configure_event_loop  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = (".env", ".env.channels")


def env_sources(root: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Merge the dotenv files the app already reads, letting the process env win."""
    merged: dict[str, str] = {}
    for name in ENV_FILES:
        path = root / name
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                merged[key] = value
    merged.update(os.environ if environ is None else environ)
    return merged


def bind_target(environ: dict[str, str] | None = None) -> tuple[str, int]:
    """Read BANK_AGENT_HOST / BANK_AGENT_PORT, falling back to loopback and 8000."""
    env = os.environ if environ is None else environ
    host = (env.get("BANK_AGENT_HOST") or "").strip() or DEFAULT_HOST
    port = DEFAULT_PORT
    raw_port = (env.get("BANK_AGENT_PORT") or "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            port = DEFAULT_PORT
    return host, port


def main() -> int:
    host, port = bind_target(env_sources(BACKEND_ROOT))
    parser = argparse.ArgumentParser(description="Run the Agent API")
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    configure_event_loop()
    uvicorn.run(
        "app.main:app",
        host=host,
        port=args.port,
        loop="app.core.eventloop:selector_loop",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
