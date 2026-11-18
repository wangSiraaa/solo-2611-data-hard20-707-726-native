"""SQLite schema for the delivery depot (independent of delegation).

Two distinct concepts are stored separately:
  * ``confirmations``  -- immutable formal confirmation snapshots (DEL1, DEL2...);
  * ``shipment_state`` -- current status of a shipment (CONFIRMED / NEEDS_REVIEW)
                          pointing at the active confirmation, without rewriting
                          any historical confirmation.
"""

SCHEMA = r"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id    TEXT PRIMARY KEY,
    consignee      TEXT NOT NULL,
    region         TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    required_parts TEXT NOT NULL,   -- canonical JSON array
    content_hash   TEXT NOT NULL,
    accepted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shipments_consignee ON shipments(consignee);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    receipt_id     TEXT PRIMARY KEY,
    consignee      TEXT NOT NULL,
    signer         TEXT,
    signed_day     INTEGER NOT NULL,
    received_day   INTEGER NOT NULL,
    region         TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    claimed_chain  TEXT NOT NULL,   -- canonical JSON array
    shipment_id    TEXT NOT NULL,
    part_bindings  TEXT NOT NULL,   -- canonical JSON array
    content_hash   TEXT NOT NULL,   -- hash of receipt content WITHOUT supplements
    accepted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dreceipts_shipment ON delivery_receipts(shipment_id);

CREATE TABLE IF NOT EXISTS part_bindings (
    receipt_id  TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    part_id     TEXT NOT NULL,
    PRIMARY KEY (receipt_id, part_id),
    FOREIGN KEY (receipt_id) REFERENCES delivery_receipts(receipt_id)
);

-- Immutable formal confirmations.
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    shipment_id     TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    previous_id     TEXT,
    parts           TEXT NOT NULL,
    covered_by      TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    authority       TEXT NOT NULL,
    evidence_hash   TEXT NOT NULL,
    authority_hash  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    UNIQUE (shipment_id, seq)
);

-- Current status pointer; never rewrites a confirmation row.
CREATE TABLE IF NOT EXISTS shipment_state (
    shipment_id           TEXT PRIMARY KEY,
    status                TEXT NOT NULL,    -- CONFIRMED | NEEDS_REVIEW
    active_confirmation_id TEXT NOT NULL,
    review_reason         TEXT,
    updated_at            TEXT NOT NULL
);

-- Append-only review history.
CREATE TABLE IF NOT EXISTS review_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id     TEXT NOT NULL,
    confirmation_id TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preparations (
    shipment_id    TEXT PRIMARY KEY,
    parts          TEXT NOT NULL,
    evidence       TEXT NOT NULL,
    evidence_hash  TEXT NOT NULL,
    authority      TEXT NOT NULL,
    authority_hash TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_documents (
    doc_key      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    accepted_at  TEXT NOT NULL,
    PRIMARY KEY (doc_key, content_hash)
);

CREATE TABLE IF NOT EXISTS delivery_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
