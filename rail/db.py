"""SQLite connection, schema and small persistence helpers.

All writes are serialised through ``BEGIN IMMEDIATE`` transactions so that the
composition-version check and the cargo status flip in ``confirm`` happen in a
single atomic boundary.  Readers use the same connection and only ever see
committed state.

The partial unique index ``ux_train_cars_active_car`` is the storage-level
guarantee that a car belongs to at most one *active* composition.  Inactive
trains keep their last known sequence for audit, but their slots are not active.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cars (
    car_id   TEXT PRIMARY KEY,
    length_n INTEGER NOT NULL CHECK (length_n > 0),
    length_d INTEGER NOT NULL CHECK (length_d > 0)
);

CREATE TABLE IF NOT EXISTS cargo (
    cargo_id       TEXT PRIMARY KEY,
    car_id         TEXT NOT NULL REFERENCES cars(car_id),
    local_n        INTEGER NOT NULL,
    local_d        INTEGER NOT NULL CHECK (local_d > 0),
    status         TEXT NOT NULL DEFAULT 'LOADED'
                   CHECK (status IN ('LOADED','UNLOADED')),
    unload_job_id  TEXT,
    updated_seq    INTEGER
);

CREATE TABLE IF NOT EXISTS trains (
    train_id            TEXT PRIMARY KEY,
    composition_version TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_seq         INTEGER NOT NULL,
    updated_seq         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS train_cars (
    train_id      TEXT NOT NULL REFERENCES trains(train_id),
    seq           INTEGER NOT NULL,
    car_id        TEXT NOT NULL REFERENCES cars(car_id),
    orientation   INTEGER NOT NULL CHECK (orientation IN (1,-1)),
    start_n       INTEGER NOT NULL,
    start_d       INTEGER NOT NULL CHECK (start_d > 0),
    end_n         INTEGER NOT NULL,
    end_d         INTEGER NOT NULL CHECK (end_d > 0),
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    PRIMARY KEY (train_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_train_cars_active_car
    ON train_cars(car_id) WHERE active = 1;

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    op           TEXT NOT NULL,
    train_id     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    seq          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id           TEXT PRIMARY KEY,
    schema              TEXT NOT NULL,
    train_id            TEXT NOT NULL,
    composition_version TEXT NOT NULL,
    window_start_n      INTEGER NOT NULL,
    window_start_d      INTEGER NOT NULL CHECK (window_start_d > 0),
    window_end_n        INTEGER NOT NULL,
    window_end_d        INTEGER NOT NULL CHECK (window_end_d > 0),
    request_hash        TEXT NOT NULL,
    report_json         TEXT NOT NULL,
    created_seq         INTEGER NOT NULL
);

-- One row per cargo point observed by one report.  Deliberately a different
-- table from unload receipts: scans are observations, receipts are authority.
CREATE TABLE IF NOT EXISTS report_positions (
    report_id    TEXT NOT NULL REFERENCES reports(report_id),
    seq          INTEGER NOT NULL,
    cargo_id     TEXT NOT NULL,
    car_id       TEXT NOT NULL,
    x_n          INTEGER NOT NULL,
    x_d          INTEGER NOT NULL CHECK (x_d > 0),
    in_window    INTEGER NOT NULL CHECK (in_window IN (0,1)),
    cargo_status TEXT NOT NULL,
    PRIMARY KEY (report_id, cargo_id)
);

CREATE TABLE IF NOT EXISTS unload_jobs (
    job_id              TEXT PRIMARY KEY,
    train_id            TEXT NOT NULL,
    composition_version TEXT NOT NULL,
    prepared_revision   INTEGER NOT NULL,
    state               TEXT NOT NULL
                        CHECK (state IN ('PREPARED','CONFIRMED','CANCELLED')),
    window_start_n      INTEGER NOT NULL,
    window_start_d      INTEGER NOT NULL CHECK (window_start_d > 0),
    window_end_n        INTEGER NOT NULL,
    window_end_d        INTEGER NOT NULL CHECK (window_end_d > 0),
    cargo_ids_json      TEXT NOT NULL,
    preparation_hash    TEXT NOT NULL,
    items_json          TEXT NOT NULL,
    prepared_seq        INTEGER NOT NULL,
    confirmed_seq       INTEGER,
    receipt_id          TEXT,
    receipt_json        TEXT,
    cancel_reason       TEXT,
    cancel_hash         TEXT
);

CREATE TABLE IF NOT EXISTS unload_receipts (
    receipt_id          TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL UNIQUE REFERENCES unload_jobs(job_id),
    train_id            TEXT NOT NULL,
    composition_version TEXT NOT NULL,
    window_start_n      INTEGER NOT NULL,
    window_start_d      INTEGER NOT NULL CHECK (window_start_d > 0),
    window_end_n        INTEGER NOT NULL,
    window_end_d        INTEGER NOT NULL CHECK (window_end_d > 0),
    items_json          TEXT NOT NULL,
    receipt_json        TEXT NOT NULL,
    created_seq         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cargo_car ON cargo(car_id);
CREATE INDEX IF NOT EXISTS ix_train_cars_train ON train_cars(train_id, seq);
CREATE INDEX IF NOT EXISTS ix_reports_train ON reports(train_id, created_seq);
CREATE INDEX IF NOT EXISTS ix_unload_jobs_train ON unload_jobs(train_id, prepared_seq);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO counters(name, value) VALUES ('seq', 0) "
        "ON CONFLICT(name) DO NOTHING"
    )


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Exclusive write transaction: either every statement lands or none does."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_seq(conn: sqlite3.Connection) -> int:
    """Monotonic sequence number; only called inside a write transaction."""
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'seq'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'seq'").fetchone()
    return int(row[0])


def get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
