"""Money display formatting, in one place.

bank_backend speaks integer minor units (fen) and the orchestrator speaks yuan; every
conversion between them goes through this module. Integer arithmetic only: a float here
would be a 100x money bug waiting for a specific amount.
"""

from __future__ import annotations

# What a currency looks like to a user. An unknown code is shown as itself.
CURRENCY_LABELS = {"CNY": "元"}


def format_minor(minor: int, currency: str = "CNY") -> str:
    """`832000` -> `"8,320.00 元"`. Raises TypeError on anything but an int.

    `bool` is an `int` subclass, so it is rejected explicitly: `True` would otherwise
    silently format as `0.01 元`.
    """

    if isinstance(minor, bool) or not isinstance(minor, int):
        raise TypeError(f"minor must be an int, got {type(minor).__name__}")
    sign = "-" if minor < 0 else ""
    yuan, fen = divmod(abs(minor), 100)
    return f"{sign}{yuan:,}.{fen:02d} {CURRENCY_LABELS.get(currency, currency)}"
