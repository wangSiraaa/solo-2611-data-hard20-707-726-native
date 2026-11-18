"""SQLite schema for the delegation side (independent of delivery)."""

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS grants (
    grant_id        TEXT    NOT NULL,
    revision        INTEGER NOT NULL,
    issuer          TEXT    NOT NULL,
    agent           TEXT    NOT NULL,
    regions         TEXT    NOT NULL,      -- canonical JSON array
    document_types  TEXT    NOT NULL,      -- canonical JSON array
    start_day       INTEGER NOT NULL,
    end_day         INTEGER NOT NULL,
    can_redelegate  INTEGER NOT NULL CHECK (can_redelegate IN (0,1)),
    parent_grant_id TEXT,
    parent_revision INTEGER,
    revoked_from    INTEGER,               -- NULL = not revoked
    payload         TEXT    NOT NULL,      -- verbatim served grant object
    content_hash    TEXT    NOT NULL,
    accepted_at     TEXT    NOT NULL,
    PRIMARY KEY (grant_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_grants_parent
    ON grants(parent_grant_id, parent_revision);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id        TEXT PRIMARY KEY,
    consignee         TEXT NOT NULL,
    signer            TEXT,
    signed_day        INTEGER NOT NULL,
    received_day      INTEGER NOT NULL,    -- intake only; never used for validity
    region            TEXT NOT NULL,
    document_type     TEXT NOT NULL,
    claimed_chain     TEXT NOT NULL,       -- canonical JSON array
    payload           TEXT NOT NULL,       -- verbatim receipt object
    content_hash      TEXT NOT NULL,
    accepted_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_key      TEXT NOT NULL,            -- kinds: grants | receipts
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    accepted_at  TEXT NOT NULL,
    PRIMARY KEY (doc_key, content_hash)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id     TEXT PRIMARY KEY,
    label         TEXT,
    lineage       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    grants_hash   TEXT NOT NULL,
    receipts_hash TEXT NOT NULL,
    payload       TEXT NOT NULL,
    content_hash  TEXT NOT NULL
);
"""
