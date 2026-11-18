"""SQLite persistence: versioned directories, catalog and immutable evaluations.

Invariants
----------
* A directory revision is written once, together with its exact document and a
  content fingerprint.  Conversion always rebuilds the graph from that stored
  document, so deleted edges can never leak back in from a cache.
* An evaluation row snapshots the specification, the original measurement and
  the conversion facts (converted value, unit, path, revision).  Later
  re-evaluations add new rows; old rows are never rewritten.
* Writing a batch of evaluations is one transaction: all or nothing.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .directory import Directory, parse_directory
from .errors import MissingMeasurement, RevisionError
from .rational import canonical_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS directory_revisions (
    revision        TEXT PRIMARY KEY,
    parent_revision TEXT,
    fingerprint     TEXT NOT NULL,
    document        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specifications (
    spec_id    TEXT PRIMARY KEY,
    quantity_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    unit_id    TEXT NOT NULL,
    document   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measurements (
    measurement_id TEXT PRIMARY KEY,
    sample_id      TEXT,
    spec_id        TEXT NOT NULL,
    quantity_id    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    unit_id        TEXT NOT NULL,
    document       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    evaluation_id        TEXT PRIMARY KEY,
    measurement_id       TEXT NOT NULL,
    sample_id            TEXT,
    spec_id              TEXT NOT NULL,
    directory_revision   TEXT NOT NULL,
    directory_fingerprint TEXT NOT NULL,
    quantity_id          TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    converted_unit       TEXT,
    converted_n          INTEGER,
    converted_d          INTEGER,
    passed               INTEGER NOT NULL,
    reason               TEXT,
    spec_snapshot        TEXT NOT NULL,
    measurement_snapshot TEXT NOT NULL,
    conversion_json      TEXT,
    path_json            TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (directory_revision) REFERENCES directory_revisions(revision)
);

CREATE INDEX IF NOT EXISTS idx_eval_measurement
    ON evaluation_runs(measurement_id, created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Directory revisions
# ---------------------------------------------------------------------------
def save_directory(
    conn: sqlite3.Connection, directory: Directory, parent_revision: str | None = None
) -> bool:
    """Persist a directory revision.  Returns False if it already existed.

    Re-using a revision label with a *different* document is an error: revision
    labels are immutable once published.
    """
    row = conn.execute(
        "SELECT fingerprint, document FROM directory_revisions WHERE revision = ?",
        (directory.revision,),
    ).fetchone()
    if row is not None:
        if row["fingerprint"] != directory.fingerprint:
            raise RevisionError(
                f"revision {directory.revision!r} already exists with different content"
            )
        return False
    document = canonical_json(directory.to_json())
    with conn:
        conn.execute(
            "INSERT INTO directory_revisions"
            " (revision, parent_revision, fingerprint, document, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                directory.revision,
                parent_revision,
                directory.fingerprint,
                document,
                now_iso(),
            ),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('current_revision', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (directory.revision,),
        )
    return True


def current_revision(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'current_revision'"
    ).fetchone()
    return row["value"] if row else None


def load_directory(conn: sqlite3.Connection, revision: str | None = None) -> Directory:
    if revision is None:
        revision = current_revision(conn)
    if revision is None:
        raise RevisionError("no directory revision has been stored yet")
    row = conn.execute(
        "SELECT document FROM directory_revisions WHERE revision = ?", (revision,)
    ).fetchone()
    if row is None:
        raise RevisionError(f"unknown directory revision {revision!r}")
    import json

    return parse_directory(json.loads(row["document"]))


def list_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT revision, parent_revision, fingerprint, created_at"
            " FROM directory_revisions ORDER BY rowid"
        )
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def store_catalog(conn: sqlite3.Connection, catalog_doc: dict) -> None:
    """Upsert the *current* catalog definitions (samples/specs/measurements)."""
    specs = catalog_doc.get("specifications", [])
    measurements = catalog_doc.get("measurements", [])
    with conn:
        for spec in specs:
            conn.execute(
                "INSERT INTO specifications(spec_id, quantity_id, kind, unit_id, document)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(spec_id) DO UPDATE SET"
                " quantity_id=excluded.quantity_id, kind=excluded.kind,"
                " unit_id=excluded.unit_id, document=excluded.document",
                (
                    spec["spec_id"],
                    spec["quantity_id"],
                    spec["kind"],
                    spec["unit_id"],
                    canonical_json(spec),
                ),
            )
        for measurement in measurements:
            sample_id = measurement.get("sample_id")
            if sample_id is not None:
                conn.execute(
                    "INSERT INTO samples(sample_id, created_at) VALUES (?, ?)"
                    " ON CONFLICT(sample_id) DO NOTHING",
                    (sample_id, now_iso()),
                )
            conn.execute(
                "INSERT INTO measurements"
                " (measurement_id, sample_id, spec_id, quantity_id, kind, unit_id, document)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(measurement_id) DO UPDATE SET"
                " sample_id=excluded.sample_id, spec_id=excluded.spec_id,"
                " quantity_id=excluded.quantity_id, kind=excluded.kind,"
                " unit_id=excluded.unit_id, document=excluded.document",
                (
                    measurement["measurement_id"],
                    sample_id,
                    measurement["spec_id"],
                    measurement["quantity_id"],
                    measurement["kind"],
                    measurement["unit_id"],
                    canonical_json(measurement),
                ),
            )


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------
def insert_evaluations(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Atomically insert a batch of evaluation rows."""
    with conn:
        for row in rows:
            value = row.get("converted_value")
            conn.execute(
                "INSERT INTO evaluation_runs ("
                " evaluation_id, measurement_id, sample_id, spec_id,"
                " directory_revision, directory_fingerprint, quantity_id, kind,"
                " status, converted_unit, converted_n, converted_d, passed, reason,"
                " spec_snapshot, measurement_snapshot, conversion_json, path_json,"
                " created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["evaluation_id"],
                    row["measurement_id"],
                    row.get("sample_id"),
                    row["spec_id"],
                    row["directory_revision"],
                    row["directory_fingerprint"],
                    row["quantity_id"],
                    row["kind"],
                    row["status"],
                    row.get("converted_unit"),
                    value["n"] if value else None,
                    value["d"] if value else None,
                    1 if row["passed"] else 0,
                    row.get("reason"),
                    canonical_json(row["specification"]),
                    canonical_json(row["measurement"]),
                    canonical_json(row["conversion"]) if row.get("conversion") else None,
                    canonical_json(row["path"]),
                    row["created_at"],
                ),
            )


def _row_to_evaluation(row: sqlite3.Row) -> dict:
    import json

    converted = None
    if row["converted_n"] is not None:
        converted = {"n": row["converted_n"], "d": row["converted_d"]}
    return {
        "evaluation_id": row["evaluation_id"],
        "measurement_id": row["measurement_id"],
        "sample_id": row["sample_id"],
        "spec_id": row["spec_id"],
        "directory_revision": row["directory_revision"],
        "directory_fingerprint": row["directory_fingerprint"],
        "quantity_id": row["quantity_id"],
        "kind": row["kind"],
        "status": row["status"],
        "converted_unit": row["converted_unit"],
        "converted_value": converted,
        "passed": bool(row["passed"]),
        "reason": row["reason"],
        "specification": json.loads(row["spec_snapshot"]),
        "measurement": json.loads(row["measurement_snapshot"]),
        "conversion": json.loads(row["conversion_json"]) if row["conversion_json"] else None,
        "path": json.loads(row["path_json"]),
        "created_at": row["created_at"],
    }


def get_evaluation(conn: sqlite3.Connection, evaluation_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM evaluation_runs WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchone()
    if row is None:
        raise MissingMeasurement(f"unknown evaluation {evaluation_id!r}")
    return _row_to_evaluation(row)


def list_evaluations(conn: sqlite3.Connection, measurement_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM evaluation_runs WHERE measurement_id = ?"
        " ORDER BY created_at, rowid",
        (measurement_id,),
    )
    return [_row_to_evaluation(row) for row in rows]


def latest_evaluation(conn: sqlite3.Connection, measurement_id: str) -> dict:
    rows = list_evaluations(conn, measurement_id)
    if not rows:
        raise MissingMeasurement(f"no evaluation for measurement {measurement_id!r}")
    return rows[-1]


def all_evaluations(conn: sqlite3.Connection, revision: str | None = None) -> list[dict]:
    if revision is None:
        rows = conn.execute(
            "SELECT * FROM evaluation_runs ORDER BY rowid"
        )
    else:
        rows = conn.execute(
            "SELECT * FROM evaluation_runs WHERE directory_revision = ? ORDER BY rowid",
            (revision,),
        )
    return [_row_to_evaluation(row) for row in rows]
