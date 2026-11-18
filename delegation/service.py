"""Delegation verification service.

Responsibilities (and deliberately nothing else):
  * accept / revise grant revisions with in-batch validation;
  * store receipt originals under idempotent, same-content replay;
  * assess each receipt strictly along its claimed grant chain;
  * publish immutable assessment reports and read them back later.

It never sees shipments, parts, coverage or delivery confirmations.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .authority import assess_receipt
from .errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from .model import parse_grant, parse_receipt
from .schema import SCHEMA
from .util import canonical, content_hash, utc_now


class DelegationService:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DelegationService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --------------------------------------------------------------- ingest
    def ingest_grants(self, payload: Any) -> dict:
        """Validate a whole grants document, then apply it atomically.

        Structural defects raise ValidationError and nothing is written.
        Per-grant conflicts / supersession are detected across the batch so a
        partial apply never happens.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("grants"), list):
            raise ValidationError("grants document must be an object with a 'grants' list")
        raw = payload["grants"]
        parsed = [parse_grant(g) for g in raw]

        seen: dict[tuple[str, int], str] = {}
        for g in parsed:
            key = (g["grant_id"], g["revision"])
            if key in seen:
                if seen[key] != g["content_hash"]:
                    raise ConflictError(
                        f"grant {g['grant_id']} rev{g['revision']} appears twice "
                        f"with different content in the same batch"
                    )
                continue
            seen[key] = g["content_hash"]

        self._check_batch_structure(parsed)

        now = utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            applied = 0
            # Insert roots before children so parent_revision can be recorded.
            for g in sorted(parsed, key=lambda x: (x["parent_grant_id"] is not None,
                                                    x["grant_id"], x["revision"])):
                existing = self._grant_row(g["grant_id"], g["revision"])
                if existing is not None:
                    if existing["content_hash"] != g["content_hash"]:
                        raise ConflictError(
                            f"grant {g['grant_id']} rev{g['revision']} already stored "
                            f"with different content"
                        )
                    continue  # idempotent replay
                for prior in self.list_grants(g["grant_id"]):
                    if prior["revision"] == g["revision"]:
                        continue
                    if prior["content_hash"] == g["content_hash"]:
                        raise ConflictError(
                            f"grant {g['grant_id']} rev{g['revision']} duplicates "
                            f"existing rev{prior['revision']} content"
                        )
                    if prior["revision"] > g["revision"]:
                        raise ValidationError(
                            f"grant {g['grant_id']} rev{g['revision']} is older than "
                            f"stored rev{prior['revision']}"
                        )
                self._insert_grant(g, now)
                applied += 1
            self._record_document("grants", payload, now)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {"offered": len(parsed), "applied": applied}

    def ingest_receipts(self, payload: Any) -> dict:
        """Validate a whole receipt batch, then store idempotently."""
        if not isinstance(payload, dict) or not isinstance(payload.get("receipts"), list):
            raise ValidationError("receipts document must be an object with a 'receipts' list")
        parsed = [parse_receipt(r) for r in payload["receipts"]]

        seen: dict[str, str] = {}
        for r in parsed:
            key = r["receipt_id"]
            if key in seen and seen[key] != r["content_hash"]:
                raise ConflictError(
                    f"receipt {key} appears twice with different content in the same batch"
                )
            seen[key] = r["content_hash"]

        now = utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            new = 0
            replayed = 0
            for r in parsed:
                existing = self._conn.execute(
                    "SELECT content_hash FROM receipts WHERE receipt_id=?", (r["receipt_id"],)
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != r["content_hash"]:
                        raise ConflictError(
                            f"receipt {r['receipt_id']} already stored with different content"
                        )
                    replayed += 1
                    continue
                self._conn.execute(
                    """INSERT INTO receipts (receipt_id, consignee, signer, signed_day,
                           received_day, region, document_type, claimed_chain, payload,
                           content_hash, accepted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        r["receipt_id"], r["consignee"], r["signer"], r["signed_day"],
                        r["received_day"], r["region"], r["document_type"],
                        canonical(r["claimed_chain"]), canonical(r["payload"]),
                        r["content_hash"], now,
                    ),
                )
                new += 1
            self._record_document("receipts", payload, now)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {
            "offered": len(parsed),
            "new": new,
            "replayed": replayed,
            "distinct_receipts": self.count_receipts(),
        }

    def _record_document(self, key: str, payload: Any, now: str) -> None:
        h = content_hash(payload)
        self._conn.execute(
            "INSERT OR IGNORE INTO documents (doc_key, content_hash, payload, accepted_at) "
            "VALUES (?,?,?,?)",
            (key, h, canonical(payload), now),
        )

    # ------------------------------------------------------------- structure
    def _check_batch_structure(self, parsed: list[dict]) -> None:
        """Within-batch link checks that do not need the DB."""
        for g in parsed:
            if g["parent_grant_id"] is None:
                continue
            parent = self._resolve_in_batch(g["parent_grant_id"], parsed)
            if parent is None:
                raise ValidationError(
                    f"grant {g['grant_id']}: parent {g['parent_grant_id']!r} is not "
                    f"known (supply the parent in this batch or ingest it first)"
                )
            self._check_one_link(g, parent)

    def _resolve_in_batch(self, grant_id: str, parsed: list[dict]) -> dict | None:
        latest = self._latest_in_batch(grant_id, parsed)
        if latest is None:
            return self._latest_stored(grant_id)
        return latest

    def _latest_in_batch(self, grant_id: str, parsed: list[dict]) -> dict | None:
        rows = [g for g in parsed if g["grant_id"] == grant_id]
        return max(rows, key=lambda g: g["revision"]) if rows else None

    def _latest_stored(self, grant_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM grants WHERE grant_id=? ORDER BY revision DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
        return self._grant_to_dict(row) if row else None

    def _check_one_link(self, child: dict, parent: dict) -> None:
        if parent["parent_grant_id"] is not None:
            raise ValidationError(
                f"grant {child['grant_id']}: parent {parent['grant_id']} is itself "
                f"delegated; maximum depth is 2"
            )
        if child["issuer"] != parent["agent"]:
            raise ValidationError(
                f"grant {child['grant_id']}: issuer {child['issuer']!r} is not the "
                f"parent agent {parent['agent']!r}"
            )
        if not parent["can_redelegate"]:
            raise ValidationError(
                f"grant {child['grant_id']}: parent {parent['grant_id']} forbids redelegation"
            )
        if child["start"] < parent["start"] or child["end"] > parent["end"]:
            raise ValidationError(
                f"grant {child['grant_id']}: nominal interval exceeds parent"
            )
        if not set(child["regions"]).issubset(parent["regions"]):
            raise ValidationError(f"grant {child['grant_id']}: regions exceed parent")
        if not set(child["document_types"]).issubset(parent["document_types"]):
            raise ValidationError(f"grant {child['grant_id']}: document_types exceed parent")

    # --------------------------------------------------------------- storage
    def _insert_grant(self, g: dict, now: str) -> None:
        self._conn.execute(
            """INSERT INTO grants (grant_id, revision, issuer, agent, regions,
                   document_types, start_day, end_day, can_redelegate,
                   parent_grant_id, parent_revision, revoked_from, payload,
                   content_hash, accepted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                g["grant_id"], g["revision"], g["issuer"], g["agent"],
                canonical(g["regions"]), canonical(g["document_types"]),
                g["start"], g["end"], int(g["can_redelegate"]),
                g["parent_grant_id"], self._parent_revision(g["parent_grant_id"]),
                g["revoked_from"], canonical(g["payload"]), g["content_hash"], now,
            ),
        )

    def _parent_revision(self, grant_id: str | None) -> int | None:
        if grant_id is None:
            return None
        row = self._conn.execute(
            "SELECT revision FROM grants WHERE grant_id=? ORDER BY revision DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
        return row["revision"] if row else None

    def _grant_row(self, grant_id: str, revision: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM grants WHERE grant_id=? AND revision=?",
            (grant_id, revision),
        ).fetchone()

    def list_grants(self, grant_id: str | None = None) -> list[dict]:
        if grant_id is None:
            rows = self._conn.execute(
                "SELECT * FROM grants ORDER BY grant_id, revision"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM grants WHERE grant_id=? ORDER BY revision",
                (grant_id,),
            ).fetchall()
        return [self._grant_to_dict(r) for r in rows]

    def latest_grant(self, grant_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM grants WHERE grant_id=? ORDER BY revision DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
        return self._grant_to_dict(row) if row else None

    def _grant_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "grant_id": row["grant_id"],
            "revision": row["revision"],
            "issuer": row["issuer"],
            "agent": row["agent"],
            "regions": json.loads(row["regions"]),
            "document_types": json.loads(row["document_types"]),
            "start": row["start_day"],
            "end": row["end_day"],
            "can_redelegate": bool(row["can_redelegate"]),
            "parent_grant_id": row["parent_grant_id"],
            "parent_revision": row["parent_revision"],
            "revoked_from": row["revoked_from"],
            "content_hash": row["content_hash"],
            "accepted_at": row["accepted_at"],
        }

    # -------------------------------------------------------------- receipts
    def list_receipts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM receipts ORDER BY receipt_id"
        ).fetchall()
        return [self._receipt_to_dict(r) for r in rows]

    def get_receipt(self, receipt_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"receipt {receipt_id!r} not found")
        return self._receipt_to_dict(row)

    def _receipt_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "receipt_id": row["receipt_id"],
            "consignee": row["consignee"],
            "signer": row["signer"],
            "signed_day": row["signed_day"],
            "received_day": row["received_day"],
            "region": row["region"],
            "document_type": row["document_type"],
            "claimed_chain": json.loads(row["claimed_chain"]),
            "content_hash": row["content_hash"],
            "accepted_at": row["accepted_at"],
        }

    def count_receipts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def count_grants(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]

    # ------------------------------------------------------------- assessment
    def _resolve_chain(self, receipt: dict, as_of: int) -> list[dict]:
        """Map claimed grant ids to the current (newest) revision of each grant.

        Only the claimed ids are followed -- no substitution of another grant is
        ever attempted.  The newest revision is authoritative for a new
        assessment: a later nominal/revocation correction is meant to change
        today's verdict, while earlier published reports stay as they were.
        """
        out = []
        for gid in receipt["claimed_chain"]:
            current = self.latest_grant(gid)
            if current is None:
                out.append({"grant_id": gid, "_missing": True})
                continue
            out.append({**current, "resolved_revision": current["revision"]})
        return out

    def assess_receipt(self, receipt_id: str, as_of: int | None = None) -> dict:
        receipt = self.get_receipt(receipt_id)
        ref = as_of if as_of is not None else self._default_as_of()
        chain = self._resolve_chain(receipt, ref)
        return assess_receipt(receipt, chain, ref)

    def _default_as_of(self) -> int:
        row = self._conn.execute("SELECT MAX(end_day) AS m FROM grants").fetchone()
        return (row["m"] or 0) - 1

    def assess_all(self, as_of: int | None = None) -> dict:
        ref = as_of if as_of is not None else self._default_as_of()
        results = []
        for receipt in self.list_receipts():
            chain = self._resolve_chain(receipt, ref)
            results.append(assess_receipt(receipt, chain, ref))
        valid = sum(1 for r in results if r["valid"])
        return {
            "as_of": ref,
            "grants_hash": self._current_hash("grants"),
            "receipts_hash": self._current_hash("receipts"),
            "receipt_count": len(results),
            "valid_count": valid,
            "invalid_count": len(results) - valid,
            "receipts": results,
        }

    def _current_hash(self, doc_key: str) -> str:
        rows = self._conn.execute(
            "SELECT content_hash FROM documents WHERE doc_key=? ORDER BY accepted_at, content_hash",
            (doc_key,),
        ).fetchall()
        return content_hash([r["content_hash"] for r in rows])

    # ---------------------------------------------------------------- reports
    def publish(self, label: str | None = None, report_id: str | None = None) -> dict:
        """Publish an immutable snapshot; default ids increase strictly (REPORTn)."""
        assessment = self.assess_all()
        lineage = self._conn.execute(
            "SELECT COALESCE(MAX(lineage), 0) + 1 AS n FROM reports"
        ).fetchone()["n"]
        report_id = report_id or f"REPORT{lineage}"
        now = utc_now()
        body = {
            "report_id": report_id,
            "label": label,
            "lineage": lineage,
            "created_at": now,
            **assessment,
        }
        h = content_hash(body)
        row = self._conn.execute(
            "SELECT content_hash FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is not None:
            if row["content_hash"] == h:
                return self.get_report(report_id)  # idempotent replay
            raise ConflictError(
                f"report {report_id} already exists with different content; reports are immutable"
            )
        self._conn.execute(
            """INSERT INTO reports (report_id, label, lineage, created_at, grants_hash,
                   receipts_hash, payload, content_hash)
               VALUES (?,?,?,?,?,?,?,?)""",
            (report_id, label, lineage, now, body["grants_hash"],
             body["receipts_hash"], canonical(body), h),
        )
        return {**body, "content_hash": h}

    def get_report(self, report_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"report {report_id!r} not found")
        return json.loads(row["payload"])

    def verify_report(self, report_id: str) -> dict:
        """Re-read a stored report and re-hash it; confirms it was not altered."""
        payload = self.get_report(report_id)
        row = self._conn.execute(
            "SELECT content_hash FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if content_hash(payload) != row["content_hash"]:
            raise IntegrityError(f"report {report_id} content hash mismatch on reopen")
        return {
            "report_id": report_id,
            "content_hash": row["content_hash"],
            "intact": True,
            "grants_hash": payload["grants_hash"],
            "receipts_hash": payload["receipts_hash"],
            "is_current_grants": payload["grants_hash"] == self._current_hash("grants"),
            "is_current_receipts": payload["receipts_hash"] == self._current_hash("receipts"),
        }

    def list_reports(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT report_id, label, lineage, created_at, grants_hash, receipts_hash, "
            "content_hash FROM reports ORDER BY lineage"
        ).fetchall()
        return [dict(r) for r in rows]


