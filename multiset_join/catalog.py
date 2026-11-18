"""Durable signed-multiset source catalog.

Design summary
--------------
* Every source table is a multiset of *complete* three-column tuples.  The
  identity of a row is the tuple; the multiplicity is a positive integer
  count.  Equal tuples are merged by addition, never deduplicated.
* A batch is merged per (source, tuple) *before* it is validated.  Only the
  final count matters, so ``+1`` / ``-1`` input order is irrelevant and a
  zero net delta disappears.
* Acceptance is atomic: if any final count would be negative, the whole batch
  is rejected and neither the source state nor the version counter changes.
* A batch has a canonical digest over its merged changes.  Re-submitting the
  same batch_id with the same digest is a pure replay (no delta is applied
  again); a different digest is rejected.
* Source state is stored as versioned intervals, so any historical slice can
  still be read after later batches.  A slice is identified by an integer
  version: 0 is the initial import, each accepted batch advances it by one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    BatchConflictError,
    CatalogError,
    NegativeCountError,
    UnknownBatchError,
    UnknownSliceError,
    ValidationError,
)
from .model import (
    SOURCE_R,
    SOURCE_S,
    SOURCES,
    MergedChange,
    SourceRow,
    batch_digest,
    canonical_batch_json,
    initial_canonical_json,
    initial_digest,
    MAX_COUNT,
    is_int,
    merge_batch,
    normalize_initial_rows,
    normalize_tuple,
    slice_fingerprint,
)

SCHEMA_VERSION = "1"

MAX_CHANGES_PER_BATCH = 500


@dataclass(frozen=True)
class BatchRecord:
    seq: int
    batch_id: str
    digest: str
    canonical: str
    accepted_version: int
    changes: tuple[MergedChange, ...]
    accepted_at: str

    @property
    def change_count(self) -> int:
        return len(self.changes)


@dataclass(frozen=True)
class ApplyResult:
    batch_id: str
    status: str  # "applied" | "replayed"
    source_version: int
    digest: str
    changes: tuple[MergedChange, ...]

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "source_version": self.source_version,
            "digest": self.digest,
            "changes": [c.as_dict() for c in self.changes],
            "change_count": len(self.changes),
        }


def _connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per contiguous interval in which a complete tuple has a given
-- positive multiplicity.  At any version at most one interval is open.
CREATE TABLE IF NOT EXISTS source_history (
    source        TEXT    NOT NULL CHECK (source IN ('R','S')),
    f1            TEXT    NOT NULL,
    f2            TEXT    NOT NULL,
    f3            INTEGER NOT NULL,
    multiplicity  INTEGER NOT NULL CHECK (multiplicity > 0),
    from_version  INTEGER NOT NULL CHECK (from_version >= 0),
    to_version    INTEGER          CHECK (to_version IS NULL OR to_version > from_version),
    PRIMARY KEY (source, f1, f2, f3, from_version)
);
CREATE INDEX IF NOT EXISTS ix_source_open
    ON source_history(source, f1, f2, f3)
    WHERE to_version IS NULL;
CREATE INDEX IF NOT EXISTS ix_source_interval
    ON source_history(source, from_version, to_version);

CREATE TABLE IF NOT EXISTS batches (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT    NOT NULL UNIQUE,
    digest           TEXT    NOT NULL,
    canonical        TEXT    NOT NULL,
    accepted_version INTEGER NOT NULL UNIQUE,
    change_count     INTEGER NOT NULL,
    accepted_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_changes (
    batch_seq INTEGER NOT NULL REFERENCES batches(seq) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    source    TEXT    NOT NULL CHECK (source IN ('R','S')),
    f1        TEXT    NOT NULL,
    f2        TEXT    NOT NULL,
    f3        INTEGER NOT NULL,
    delta     INTEGER NOT NULL CHECK (delta <> 0),
    PRIMARY KEY (batch_seq, idx)
);
"""


class SourceCatalog:
    """An independent multiset source catalog backed by one SQLite file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = _connect(self.path)
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('next_version', '0')"
        )

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SourceCatalog":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # --------------------------------------------------------------- import/replay
    def is_initialized(self) -> bool:
        return self._meta("initial_digest") is not None

    def initial_digest(self) -> str:
        digest = self._meta("initial_digest")
        if digest is None:
            raise CatalogError("source catalog has not been imported")
        return digest

    def import_initial(self, r_rows: Any, s_rows: Any) -> dict:
        """Import the initial R/S multisets.

        Re-importing the same canonical initial multiset is a no-op; importing
        different content into an already initialised catalog is an error.
        This makes the source-import entry point safe to call on reopen.
        """
        r_normalized = normalize_initial_rows(SOURCE_R, r_rows)
        s_normalized = normalize_initial_rows(SOURCE_S, s_rows)
        for row in (*r_normalized, *s_normalized):
            if row.multiplicity > MAX_COUNT:
                raise ValidationError(
                    f"initial multiplicity for {row.source}{row.tuple} exceeds "
                    f"{MAX_COUNT}"
                )
        digest = initial_digest(r_normalized, s_normalized)
        with _immediate(self._conn):
            existing = self._meta("initial_digest")
            if existing is not None:
                if existing != digest:
                    raise CatalogError(
                        "source catalog was already imported with different "
                        "initial content"
                    )
                version = self.current_version()
                return {
                    "status": "already_imported",
                    "initial_digest": digest,
                    "source_version": version,
                    "r_tuples": self.distinct_tuple_count(SOURCE_R, version),
                    "s_tuples": self.distinct_tuple_count(SOURCE_S, version),
                }
            self._conn.executemany(
                """
                INSERT INTO source_history
                    (source, f1, f2, f3, multiplicity, from_version, to_version)
                VALUES (?, ?, ?, ?, ?, 0, NULL)
                """,
                [
                    (row.source, row.tuple[0], row.tuple[1], row.tuple[2],
                     row.multiplicity)
                    for row in (*r_normalized, *s_normalized)
                ],
            )
            self._set_meta("initial_digest", digest)
            self._set_meta("next_version", "0")
            self._set_meta("initial_canonical", initial_canonical_json(r_normalized, s_normalized))
        return {
            "status": "imported",
            "initial_digest": digest,
            "source_version": 0,
            "r_tuples": len(r_normalized),
            "s_tuples": len(s_normalized),
        }

    # ------------------------------------------------------------------- batches
    def apply_batch(self, batch_id: Any, changes: Any) -> ApplyResult:
        """Validate and atomically apply one batch.

        If ``batch_id`` was already accepted with the same canonical merged
        content, this is a replay and returns the original version without
        applying the deltas again.
        """
        if not isinstance(batch_id, str) or not batch_id:
            raise ValidationError("batch_id must be a non-empty string")
        if not isinstance(changes, list):
            raise ValidationError("batch changes must be a list")
        if len(changes) > MAX_CHANGES_PER_BATCH:
            raise ValidationError(
                f"batch has {len(changes)} raw changes; at most "
                f"{MAX_CHANGES_PER_BATCH} are allowed"
            )
        merged = tuple(merge_batch(changes))
        digest = batch_digest(merged)

        with _immediate(self._conn):
            if not self.is_initialized():
                raise CatalogError("source catalog has not been imported")

            existing = self._batch_by_id(batch_id)
            if existing is not None:
                if existing.digest != digest:
                    raise BatchConflictError(
                        f"batch_id {batch_id!r} was already accepted with "
                        f"different canonical content "
                        f"(stored {existing.digest}, submitted {digest})"
                    )
                return ApplyResult(
                    batch_id=batch_id,
                    status="replayed",
                    source_version=existing.accepted_version,
                    digest=existing.digest,
                    changes=existing.changes,
                )

            current_version = self.current_version()
            current_counts: dict[tuple[str, tuple], int] = {}
            violations: list[dict] = []
            finals: list[tuple[MergedChange, int]] = []

            for change in merged:
                key = (change.source, change.tuple)
                if key not in current_counts:
                    current_counts[key] = self._current_count(change.source, change.tuple)
                final = current_counts[key] + change.delta
                if final < 0:
                    violations.append(
                        {
                            "source": change.source,
                            "tuple": list(change.tuple),
                            "current": current_counts[key],
                            "delta": change.delta,
                            "final": final,
                        }
                    )
                if final > MAX_COUNT:
                    raise ValidationError(
                        f"final multiplicity for {change.source}{change.tuple} "
                        f"exceeds {MAX_COUNT}"
                    )
                finals.append((change, final))

            if violations:
                # _immediate rolls back; nothing below is written.
                raise NegativeCountError(violations)

            new_version = current_version + 1
            for change, final in finals:
                self._conn.execute(
                    """
                    UPDATE source_history
                       SET to_version = ?
                     WHERE source = ? AND f1 = ? AND f2 = ? AND f3 = ?
                       AND to_version IS NULL
                    """,
                    (new_version, change.source, change.tuple[0],
                     change.tuple[1], change.tuple[2]),
                )
                if final > 0:
                    self._conn.execute(
                        """
                        INSERT INTO source_history
                            (source, f1, f2, f3, multiplicity, from_version, to_version)
                        VALUES (?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (change.source, change.tuple[0], change.tuple[1],
                         change.tuple[2], final, new_version),
                    )

            canonical = canonical_batch_json(merged)
            accepted_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            cursor = self._conn.execute(
                """
                INSERT INTO batches
                    (batch_id, digest, canonical, accepted_version, change_count, accepted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, digest, canonical, new_version, len(merged), accepted_at),
            )
            seq = int(cursor.lastrowid)
            self._conn.executemany(
                """
                INSERT INTO batch_changes (batch_seq, idx, source, f1, f2, f3, delta)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (seq, index, change.source, change.tuple[0], change.tuple[1],
                     change.tuple[2], change.delta)
                    for index, change in enumerate(merged)
                ],
            )
            self._set_meta("next_version", str(new_version))

        return ApplyResult(
            batch_id=batch_id,
            status="applied",
            source_version=new_version,
            digest=digest,
            changes=merged,
        )

    def replay_batch(self, batch_id: str, changes: Any = None) -> BatchRecord:
        """Look up an already accepted batch without applying it again.

        If ``changes`` is given, its canonical merged content must match the
        stored batch exactly; otherwise the replay is rejected.
        """
        existing = self.get_batch(batch_id)
        if changes is not None:
            if not isinstance(changes, list):
                raise ValidationError("batch changes must be a list")
            merged = tuple(merge_batch(changes))
            submitted = batch_digest(merged)
            if submitted != existing.digest:
                raise BatchConflictError(
                    f"replay of batch_id {batch_id!r} has different canonical "
                    f"content (stored {existing.digest}, submitted {submitted})"
                )
        return existing

    def get_batch(self, batch_id: str) -> BatchRecord:
        record = self._batch_by_id(batch_id)
        if record is None:
            raise UnknownBatchError(f"batch_id {batch_id!r} has not been accepted")
        return record

    def list_batches(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT seq, batch_id, digest, accepted_version, change_count, accepted_at
              FROM batches ORDER BY seq
            """
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------- reading
    def current_version(self) -> int:
        return int(self._meta("next_version") or "0")

    def resolve_version(self, version: Any = None) -> int:
        current = self.current_version()
        if version is None:
            return current
        if not is_int(version) or version < 0:
            raise UnknownSliceError(f"invalid source version {version!r}")
        if version > current:
            raise UnknownSliceError(
                f"source version {version} does not exist (current {current})"
            )
        return version

    def read_source_rows(self, source: str, version: Any = None) -> list[SourceRow]:
        if source not in SOURCES:
            raise ValidationError(f"unknown source {source!r}")
        resolved = self.resolve_version(version)
        rows = self._conn.execute(
            """
            SELECT source, f1, f2, f3, multiplicity
              FROM source_history
             WHERE source = ?
               AND from_version <= ?
               AND (to_version IS NULL OR to_version > ?)
             ORDER BY f1, f2, f3
            """,
            (source, resolved, resolved),
        ).fetchall()
        return [
            SourceRow(row["source"], (row["f1"], row["f2"], row["f3"]),
                      int(row["multiplicity"]))
            for row in rows
        ]

    def read_slice(self, version: Any = None) -> dict:
        resolved = self.resolve_version(version)
        return {
            "version": resolved,
            "R": [
                {"tuple": list(row.tuple), "multiplicity": row.multiplicity}
                for row in self.read_source_rows(SOURCE_R, resolved)
            ],
            "S": [
                {"tuple": list(row.tuple), "multiplicity": row.multiplicity}
                for row in self.read_source_rows(SOURCE_S, resolved)
            ],
            "fingerprint": self.slice_fingerprint(resolved),
        }

    def tuple_count(self, source: str, raw_tuple: Any, version: Any = None) -> int:
        if source not in SOURCES:
            raise ValidationError(f"unknown source {source!r}")
        key = normalize_tuple(source, raw_tuple)
        resolved = self.resolve_version(version)
        row = self._conn.execute(
            """
            SELECT multiplicity
              FROM source_history
             WHERE source = ? AND f1 = ? AND f2 = ? AND f3 = ?
               AND from_version <= ?
               AND (to_version IS NULL OR to_version > ?)
            """,
            (source, key[0], key[1], key[2], resolved, resolved),
        ).fetchone()
        return int(row["multiplicity"]) if row else 0

    def distinct_tuple_count(self, source: str, version: Any = None) -> int:
        return len(self.read_source_rows(source, version))

    def total_multiplicity(self, source: str, version: Any = None) -> int:
        return sum(row.multiplicity for row in self.read_source_rows(source, version))

    def slice_fingerprint(self, version: Any = None) -> str:
        resolved = self.resolve_version(version)
        return slice_fingerprint(
            self.read_source_rows(SOURCE_R, resolved),
            self.read_source_rows(SOURCE_S, resolved),
        )

    # -------------------------------------------------------------------- helpers
    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _current_count(self, source: str, key: tuple) -> int:
        row = self._conn.execute(
            """
            SELECT multiplicity FROM source_history
             WHERE source = ? AND f1 = ? AND f2 = ? AND f3 = ?
               AND to_version IS NULL
            """,
            (source, key[0], key[1], key[2]),
        ).fetchone()
        return int(row["multiplicity"]) if row else 0

    def _batch_by_id(self, batch_id: str) -> BatchRecord | None:
        row = self._conn.execute(
            """
            SELECT seq, batch_id, digest, canonical, accepted_version, change_count,
                   accepted_at
              FROM batches WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        change_rows = self._conn.execute(
            """
            SELECT source, f1, f2, f3, delta
              FROM batch_changes WHERE batch_seq = ? ORDER BY idx
            """,
            (row["seq"],),
        ).fetchall()
        changes = tuple(
            MergedChange(ch["source"], (ch["f1"], ch["f2"], ch["f3"]), int(ch["delta"]))
            for ch in change_rows
        )
        return BatchRecord(
            seq=int(row["seq"]),
            batch_id=str(row["batch_id"]),
            digest=str(row["digest"]),
            canonical=str(row["canonical"]),
            accepted_version=int(row["accepted_version"]),
            changes=changes,
            accepted_at=str(row["accepted_at"]),
        )
