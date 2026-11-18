"""Shared validation and the global non-decreasing business-day clock."""

from __future__ import annotations

import sqlite3

from .errors import Conflict, ValidationError

BUSINESS_DAY_KEY = "business_day"


def is_int(value: object) -> bool:
    """``True`` only for real integers (``bool`` is not accepted)."""
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if not is_int(value):
        raise ValidationError(f"{name} must be an integer, got {value!r}")
    value = int(value)
    if minimum is not None and value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}, got {value}")
    return value


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string, got {value!r}")
    return value


def require_day(value: object, name: str = "day") -> int:
    return require_int(value, name, minimum=0)


def advance_day(conn: sqlite3.Connection, day: int) -> int:
    """Advance the global business clock; it may never move backwards.

    Every mutating business operation calls this inside its write
    transaction. Queries deliberately do not move the clock.
    """
    require_day(day)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (BUSINESS_DAY_KEY,)
    ).fetchone()
    if row is None:
        raise Conflict("store is not initialized (missing business day)")
    current = int(row["value"])
    if day < current:
        raise Conflict(
            f"business day may not decrease: current {current}, requested {day}"
        )
    if day > current:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (BUSINESS_DAY_KEY, str(day)),
        )
    return day


def current_day(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (BUSINESS_DAY_KEY,)
    ).fetchone()
    if row is None:
        raise Conflict("store is not initialized")
    return int(row["value"])
