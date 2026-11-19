"""Thin SQLite wrapper with explicit atomic transactions.

``isolation_level=None`` puts the connection in autocommit mode; we issue
``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` ourselves so a multi-statement
write (read + calibration product + samples) is all-or-nothing and takes the
write lock up front.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union


class Store:
    def __init__(
        self,
        path: Union[str, Path] = ":memory:",
        *,
        conn: Optional[sqlite3.Connection] = None,
        foreign_keys: bool = True,
    ):
        if conn is not None:
            self.conn = conn
        else:
            self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if foreign_keys:
            self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    def executescript(self, script: str) -> None:
        self.conn.executescript(script)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write scope.  Rolls back on *any* exception."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
