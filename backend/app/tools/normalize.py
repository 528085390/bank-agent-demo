"""One place where "the model did not provide this" is turned back into an omission.

Strict mode has no notion of an optional property, so `schema.normalize_parameters` sends every
declared optional parameter to the model as a required, nullable one. A model that wants to say
"not provided" therefore has to write `null` -- and a weaker one writes the *string* `"null"`
instead, which the declared view accepts as a perfectly good string. The placeholder then flows
onward as if the user had supplied it: `from_account_id="null"` reads as "the caller picked an
account", so the balance check is never reached and a 999,999-yuan transfer is refused as
NOT_FOUND instead of INSUFFICIENT_BALANCE.

The outbound trip is a framework concern, so the return trip is normalized here, once, for every
tool of every module. New code should call this function rather than growing another placeholder
list; the historical copies in card / bill / subscription are left in place for now -- collapsing
them is deferred by spec §1.1 A4 because they are idempotent and make no behavioral difference.
"""

from __future__ import annotations

from typing import Any, Mapping

# The spellings a model uses to mean "nothing". Empty and whitespace-only strings are included
# because they carry no value either. Matching is on `strip().lower()`, so casing and padding
# do not smuggle a placeholder through -- but a string that merely *contains* one ("Null 先生")
# is real data and survives.
_PLACEHOLDERS: frozenset[str] = frozenset({"", "null", "none", "undefined"})


def normalize_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop the placeholders that mean "not provided", and keep everything else.

    Only two things are dropped: `None`, and a string whose `strip().lower()` is one of
    `_PLACEHOLDERS`. Every other value is kept exactly as it arrived -- `0`, `False`, `[]` and
    `{}` are real values a model may legitimately send, so a truthiness test would be wrong here.
    """

    normalized: dict[str, Any] = {}
    for name, value in (arguments or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS:
            continue
        normalized[name] = value
    return normalized
