"""SQLite connection helpers and additive schema.

The material database is used as-is: every pre-existing table and column keeps
its original meaning.  The service only *adds* the ``cancellation_results``
table (frozen cancellation outcomes) and never rewrites history rows.  The
read-only ``ledger_reader.py`` therefore keeps working unchanged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


BUSY_TIMEOUT_MS = 15_000

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS cancellation_results(
    cancel_id    TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL UNIQUE,
    user_id      TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json  TEXT NOT NULL
);
"""


def _uri(path: str | Path, readonly: bool) -> str:
    resolved = Path(path).expanduser().resolve()
    if readonly:
        return resolved.as_uri() + "?mode=ro"
    return resolved.as_uri() + "?mode=rw"


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open *path* with explicit, manual transaction control.

    ``isolation_level=None`` disables sqlite3's implicit transactions so the
    ledger can use ``BEGIN IMMEDIATE`` and hold the write lock while it reads
    catalog versions, evaluates eligibility and decrements balances.
    """

    con = sqlite3.connect(
        _uri(path, readonly),
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if readonly:
        con.execute("PRAGMA query_only=ON")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """Apply additive schema to a writable connection."""

    con.executescript(EXTRA_SCHEMA)
    con.commit()


def has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None
