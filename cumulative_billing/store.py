"""SQLite storage shared by the pricing service and the bill repository.

The database is the durable boundary: usage revisions and contracts are
appended, and issued/imported/adjustment documents are stored as immutable
period-bill/1 rows.  Nothing here re-derives fees; the repository consumes the
pricing service's output.

Key design choice (affects propagation, gap detection and idempotency):

* Usage is stored as **per (account, year, period) revision chains**, not as a
  single mutable quantity.  ``confirmed quantity at revision R`` is the row
  with the greatest ``revision <= R``.  A period is *confirmed* only if at
  least one usage row exists for it; absence is *missing*, not zero.
* Billing documents are **immutable**.  A ``BILL``/``ADJUSTMENT`` row keeps its
  original amount forever.  The *net issued amount* of a bill is the bill plus
  all adjustments whose ``previous_statement_id`` points at it (directly or by
  transitive ancestry), so later corrections are computed against the net, not
  the original.
* Adjustment *identity* is ``(account, year, period, sequence)`` where
  ``sequence = 1 + max(previous sequence at that period)``.  Re-running the
  same recalculation therefore addresses the same (period, target-net) state
  and emits no duplicate.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (contract_id, revision)
);

CREATE TABLE IF NOT EXISTS usage_revisions (
    account_id  TEXT NOT NULL,
    year        INTEGER NOT NULL,
    period      INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (account_id, year, period, revision)
);

CREATE TABLE IF NOT EXISTS documents (
    document_id           TEXT PRIMARY KEY,
    format                TEXT NOT NULL,
    document_type         TEXT NOT NULL,
    account_id            TEXT NOT NULL,
    year                  INTEGER NOT NULL,
    period                INTEGER NOT NULL,
    amount_cents          INTEGER NOT NULL,
    contract_revision     INTEGER NOT NULL,
    usage_revisions       TEXT NOT NULL,   -- JSON object {"period": revision, ...}
    previous_statement_id TEXT,
    source                TEXT NOT NULL,   -- imported | recalculated
    payload               TEXT NOT NULL,   -- canonical period-bill/1 JSON
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS ix_documents_account_year
    ON documents (account_id, year, period);

CREATE TABLE IF NOT EXISTS adjustments (
    document_id           TEXT PRIMARY KEY REFERENCES documents (document_id),
    account_id            TEXT NOT NULL,
    year                  INTEGER NOT NULL,
    period                INTEGER NOT NULL,
    sequence              INTEGER NOT NULL,   -- adjustment identity within period
    previous_statement_id TEXT NOT NULL,      -- the bill this adjustment states against
    target_amount_cents   INTEGER NOT NULL,   -- period fee at this recalculation
    recalc_run_id         TEXT NOT NULL,
    UNIQUE (account_id, year, period, sequence)
);

CREATE TABLE IF NOT EXISTS recalc_runs (
    run_id        TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    year          INTEGER NOT NULL,
    through_period INTEGER NOT NULL,
    save_as       TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
