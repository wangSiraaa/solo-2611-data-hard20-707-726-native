"""SQLite schema and connection helpers.

Design notes
------------
Allocation facts live in exactly one place: the ``reservations`` table, keyed
by a generic ``holder_id``. A run uses its ``run_id`` as holder, so the run
module consumes the very same rows the inventory entry points write; neither
side keeps a parallel quantity.

A reservation is either ``active`` (counts as reserved), ``consumed`` (counts
as consumed) or ``released`` (counts as neither, portions flow back to
unallocated).

``unallocated`` is derived on every read as
``total - active_reserved - consumed``. Expired but unallocated portions stay
in that number; expiration only changes whether they are *leadable today*, it
never removes them from the conservation equation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS kinds (
    kind                 TEXT PRIMARY KEY,
    open_lifetime_days   INTEGER NOT NULL CHECK (open_lifetime_days > 0)
);

CREATE TABLE IF NOT EXISTS bottles (
    bottle_id           TEXT PRIMARY KEY,
    kind                TEXT NOT NULL REFERENCES kinds(kind),
    total_portions      INTEGER NOT NULL CHECK (total_portions > 0),
    intake_day          INTEGER NOT NULL CHECK (intake_day >= 0),
    printed_expiry_day  INTEGER NOT NULL,
    opened_day          INTEGER,
    CHECK (printed_expiry_day > intake_day),
    CHECK (opened_day IS NULL OR (opened_day >= intake_day
                                  AND opened_day < printed_expiry_day))
);

CREATE TABLE IF NOT EXISTS methods (
    method_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (method_id, revision)
);

CREATE TABLE IF NOT EXISTS method_requirements (
    method_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    kind        TEXT NOT NULL REFERENCES kinds(kind),
    portions    INTEGER NOT NULL CHECK (portions > 0),
    PRIMARY KEY (method_id, revision, kind),
    FOREIGN KEY (method_id, revision) REFERENCES methods(method_id, revision)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    method_id            TEXT NOT NULL,
    method_revision      INTEGER NOT NULL,
    creation_day         INTEGER NOT NULL,
    -- demand snapshot at creation; later revisions never touch it
    demand_json          TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
                             'reserved', 'started', 'released')),
    failure_reason       TEXT,
    started_day          INTEGER,
    terminal_day         INTEGER,
    -- original terminal receipt; replayed verbatim, never recomputed
    terminal_json        TEXT,
    FOREIGN KEY (method_id, method_revision)
        REFERENCES methods(method_id, revision)
);

CREATE TABLE IF NOT EXISTS run_requirements (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    kind        TEXT NOT NULL REFERENCES kinds(kind),
    portions    INTEGER NOT NULL CHECK (portions > 0),
    PRIMARY KEY (run_id, kind)
);

-- A reservation is an inventory fact identified by a generic holder. A run
-- uses its run_id as the holder; the inventory API can also be driven with an
-- independent holder. Runs reference facts through the holder join and never
-- store a private portion counter.
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id   TEXT PRIMARY KEY,
    holder_id        TEXT NOT NULL,
    bottle_id        TEXT NOT NULL REFERENCES bottles(bottle_id),
    portions         INTEGER NOT NULL CHECK (portions > 0),
    status           TEXT NOT NULL CHECK (status IN (
                          'active', 'consumed', 'released')),
    created_day      INTEGER NOT NULL,
    settled_day      INTEGER,
    release_reason   TEXT,
    CHECK ((status = 'active' AND settled_day IS NULL)
        OR (status IN ('consumed', 'released') AND settled_day IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS reservations_holder_idx
    ON reservations(holder_id, status);
CREATE INDEX IF NOT EXISTS reservations_bottle_idx
    ON reservations(bottle_id);
CREATE INDEX IF NOT EXISTS reservations_bottle_status_idx
    ON reservations(bottle_id, status);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Allocation facts per bottle. ``unallocated`` is derived from the
-- reservation rows, so an expired-but-unallocated portion can never be lost
-- from the conservation equation.
CREATE VIEW IF NOT EXISTS bottle_allocation AS
SELECT b.bottle_id,
       b.kind,
       b.total_portions,
       COALESCE((SELECT SUM(r.portions) FROM reservations r
                  WHERE r.bottle_id = b.bottle_id AND r.status = 'active'), 0)
           AS reserved,
       COALESCE((SELECT SUM(r.portions) FROM reservations r
                  WHERE r.bottle_id = b.bottle_id AND r.status = 'consumed'), 0)
           AS consumed,
       b.total_portions
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'active'), 0)
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'consumed'), 0)
           AS unallocated
  FROM bottles b;

-- Structural conservation: reserved + consumed can never exceed total.
CREATE TRIGGER IF NOT EXISTS reservations_conservation_insert
BEFORE INSERT ON reservations
WHEN NEW.status <> 'released'
BEGIN
    SELECT RAISE(ABORT, 'reservation exceeds bottle total')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND status IN ('active', 'consumed')), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

CREATE TRIGGER IF NOT EXISTS reservations_conservation_update
BEFORE UPDATE ON reservations
WHEN NEW.status <> 'released'
BEGIN
    SELECT RAISE(ABORT, 'reservation exceeds bottle total')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND reservation_id <> OLD.reservation_id
                          AND status IN ('active', 'consumed')), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

-- A settled reservation (consumed/released) never changes again; its
-- quantities, bottle and status are all frozen.
CREATE TRIGGER IF NOT EXISTS reservations_terminal_immutable
BEFORE UPDATE ON reservations
WHEN OLD.status <> 'active'
BEGIN
    SELECT RAISE(ABORT, 'settled reservation is immutable');
END;

CREATE TRIGGER IF NOT EXISTS reservations_terminal_no_delete
BEFORE DELETE ON reservations
WHEN OLD.status <> 'active'
BEGIN
    SELECT RAISE(ABORT, 'settled reservation cannot be deleted');
END;

-- A run's terminal state and its demand snapshot are frozen once terminal.
CREATE TRIGGER IF NOT EXISTS runs_terminal_immutable
BEFORE UPDATE ON runs
WHEN OLD.state IN ('started', 'released')
BEGIN
    SELECT RAISE(ABORT, 'terminal run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS run_requirements_immutable
BEFORE UPDATE ON run_requirements
BEGIN
    SELECT RAISE(ABORT, 'run requirements are immutable after creation');
END;

CREATE TRIGGER IF NOT EXISTS run_requirements_no_delete
BEFORE DELETE ON run_requirements
BEGIN
    SELECT RAISE(ABORT, 'run requirements are immutable after creation');
END;

-- Opening day is monotonic: once opened it can never move or be cleared.
CREATE TRIGGER IF NOT EXISTS bottles_open_immutable
BEFORE UPDATE OF opened_day ON bottles
WHEN OLD.opened_day IS NOT NULL
 AND (NEW.opened_day IS NULL OR NEW.opened_day <> OLD.opened_day)
BEGIN
    SELECT RAISE(ABORT, 'opened_day is immutable');
END;
"""


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection with the isolation / pragma settings we rely on."""
    path = str(path)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """``BEGIN IMMEDIATE`` transaction, or a savepoint-less nested join.

    ``BEGIN IMMEDIATE`` takes the write lock up front. Two competing
    connections therefore serialize on the same last portion: the loser waits
    (``busy_timeout``) until the winner commits, then re-reads availability and
    fails cleanly instead of over-allocating.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
