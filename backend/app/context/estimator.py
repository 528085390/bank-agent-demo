"""Heuristic token estimation for the prompt layer.

Every budget decision the context manager makes is driven by these numbers, and they are
deliberately pessimistic: CJK characters cost a full token each and everything else 0.3,
which sits above the empirical ~0.25 tokens per ASCII character. Over-estimating wastes a
little budget; under-estimating means a prompt that silently overflows the model's window,
so an over-optimistic estimate is the most likely failure mode of this design.
"""

from __future__ import annotations

import math

ESTIMATOR_VERSION: str = "heuristic_v1"

# What the chat protocol adds around a message's content, whatever its length.
MESSAGE_OVERHEAD: int = 4

# The six ranges counted at 1.0 token per character: CJK symbols and punctuation, kana,
# CJK extensions A and unified ideographs, compatibility ideographs, and fullwidth forms.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


def _is_cjk(ch: str) -> bool:
    """True when `ch` is in one of the six ranges counted at a full token per character."""

    return any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate the tokens in `text`, rounding up. Carries no per-message overhead."""

    cjk_chars = sum(1 for ch in text if _is_cjk(ch))
    other_chars = len(text) - cjk_chars
    return math.ceil(cjk_chars * 1.0 + other_chars * 0.3)


def estimate_message_tokens(content: str) -> int:
    """Estimate one chat message: its content plus the protocol overhead."""

    return estimate_tokens(content) + MESSAGE_OVERHEAD
