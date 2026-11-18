"""SQLite helpers shared by the catalog and the delivery store.

The two stores are logically independent (the catalog tables never reference
delivery tables and vice versa) but may live in one SQLite file so that a
delivery attempt can read the catalog and write its job atomically.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings used throughout the service."""
    path = str(path)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """An explicit write transaction (BEGIN IMMEDIATE ... COMMIT/ROLLBACK).

    ``isolation_level=None`` in :func:`connect` disables sqlite3's implicit
    transaction handling, so the boundaries are always explicit.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
