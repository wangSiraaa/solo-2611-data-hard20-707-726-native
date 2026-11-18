"""Canonical JSON helpers.

Request content is compared by JSON *value*:

* object key order is irrelevant (keys are sorted),
* array order is significant (order lines and coupon lists keep their order),
* whitespace is irrelevant.

Amounts are validated as integers by the qualification/ledger layers; this
module deliberately does not coerce numbers.
"""

from __future__ import annotations

import json
from typing import Any


class NonCanonicalJSON(ValueError):
    """Raised when a value cannot be represented as strict JSON."""


def dumps(value: Any) -> str:
    """Return a deterministic JSON representation of *value*.

    ``sort_keys`` makes object key order irrelevant while list order is left
    untouched.  ``allow_nan`` is disabled because NaN/Infinity are not JSON.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise NonCanonicalJSON(str(exc)) from exc


def loads(text: str) -> Any:
    return json.loads(text)


def canonical(value: Any) -> Any:
    """Return a recursively key-sorted structure (mostly useful in tests)."""

    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def equal_json(left: Any, right: Any) -> bool:
    """Compare two values by canonical JSON value."""

    try:
        return dumps(left) == dumps(right)
    except NonCanonicalJSON:
        return False


def equal_serialized(left_text: str, right_value: Any) -> bool:
    """Compare an existing serialized JSON document with a Python value."""

    try:
        left = json.loads(left_text)
    except (TypeError, ValueError):
        return False
    return equal_json(left, right_value)
