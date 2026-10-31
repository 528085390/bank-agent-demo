"""Keep a malformed NO_PROXY from taking the model boundary down with it.

httpx turns every NO_PROXY entry into a URL pattern while it builds the transport. A
bracketed IPv6 literal such as [::1] is not a URL, so httpx raises InvalidURL("Invalid
port") *before any request is made*: every httpx client in the process then fails to
construct, and the model settings panel reports "cannot reach the provider" for a
configuration that is perfectly good. Windows toolchains really do produce that spelling
(NO_PROXY=localhost,127.0.0.1,::1,[::1]), and the entry carries nothing the bare ::1 does
not already say, so it is dropped rather than allowed to poison the process.

Only entries httpx cannot parse are removed; a bypass list that works is left exactly as
the operator wrote it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

#: Both spellings are read (urllib and httpx accept either), so both are cleaned.
_NAMES = ("NO_PROXY", "no_proxy")


def _is_usable(entry: str) -> bool:
    """Whether httpx can turn this bypass entry into a URL pattern.

    Bracketed hosts are the only shape that fails: httpx reads the text after the colon as
    a port, so [::1] yields "Invalid port: ':1]'". A bare ::1 parses fine.
    """

    return "[" not in entry and "]" not in entry


def sanitize_no_proxy(environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Drop the NO_PROXY entries httpx chokes on, in place, and report what went.

    Returns the dropped entries so a caller can log them; the environment itself is
    rewritten because httpx reads it directly and a copy would not help.
    """

    env = os.environ if environ is None else environ
    dropped: list[str] = []
    for name in [key for key in list(env) if key.upper() == "NO_PROXY"]:
        raw = env.get(name) or ""
        entries = [part.strip() for part in raw.split(",")]
        kept: list[str] = []
        for entry in entries:
            if not entry:
                continue
            if _is_usable(entry):
                kept.append(entry)
            else:
                dropped.append(entry)
                logger.warning(
                    "no_proxy_entry_dropped name=%s entry=%s reason=httpx_cannot_parse_it",
                    name,
                    entry,
                )
        # Only rewrite when something actually went: an untouched list must stay byte for
        # byte identical, spaces and ordering included.
        if len(kept) != len(entries):
            env[name] = ",".join(kept)
    return tuple(dropped)
