"""Delivery confirmation depot.

Business responsibilities:
  * maintain shipments (consignee / region / document_type / required parts);
  * accept receipt supplements and their fixed part bindings;
  * compute current part coverage from proof-backed originals only;
  * own formal confirmations (immutable), their lineage, preparations, and the
    NEEDS_REVIEW lifecycle.

It does not recompute delegation authority: it consumes an injected
AuthorityPort.  It holds no per-part delegation coverage and no shipment-level
authority state on the delegation side.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import (
    ConflictError,
    NotFoundError,
    StalePreparationError,
    StateError,
    ValidationError,
)
from .model import parse_shipment, parse_supplement
from .schema import SCHEMA
from .util import canonical, content_hash, utc_now


class DeliveryService:
    def __init__(self, db_path: str | Path, authority: Any):
        self.db_path = str(db_path)
        self.authority = authority
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DeliveryService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- shipments
    def ingest_shipments(self, payload: Any) -> dict:
        if isinstance(payload, list):
            raw = payload
        elif isinstance(payload, dict) and isinstance(payload.get("shipments"), list):
            raw = payload["shipments"]
        else:
            raise ValidationError("shipments must be a list or an object with 'shipments'")
        parsed = [parse_shipment(s) for s in raw]

        seen: dict[str, str] = {}
        for s in parsed:
            if s["shipment_id"] in seen and seen[s["shipment_id"]] != s["content_hash"]:
                raise ConflictError(
                    f"shipment {s['shipment_id']} appears twice with different content"
                )
            seen[s["shipment_id"]] = s["content_hash"]

        now = utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            new = replayed = 0
            for s in parsed:
                row = self._conn.execute(
                    "SELECT content_hash FROM shipments WHERE shipment_id=?",
                    (s["shipment_id"],),
                ).fetchone()
                if row is not None:
                    if row["content_hash"] != s["content_hash"]:
                        raise ConflictError(
                            f"shipment {s['shipment_id']} already stored with different content"
                        )
                    replayed += 1
                    continue
                self._conn.execute(
                    """INSERT INTO shipments (shipment_id, consignee, region, document_type,
                           required_parts, content_hash, accepted_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (s["shipment_id"], s["consignee"], s["region"], s["document_type"],
                     canonical(s["required_parts"]), s["content_hash"], now),
                )
                new += 1
            self._record_document("shipments", payload, now)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {"offered": len(parsed), "new": new, "replayed": replayed}

    def get_shipment(self, shipment_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"shipment {shipment_id!r} not found")
        return {
            "shipment_id": row["shipment_id"],
            "consignee": row["consignee"],
            "region": row["region"],
            "document_type": row["document_type"],
            "required_parts": json.loads(row["required_parts"]),
            "content_hash": row["content_hash"],
            "accepted_at": row["accepted_at"],
        }

    def list_shipments(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT shipment_id FROM shipments ORDER BY shipment_id"
        ).fetchall()
        return [self.get_shipment(r["shipment_id"]) for r in rows]

    # ------------------------------------------------------ receipt supplements
    def ingest_supplements(self, payload: Any) -> dict:
        """Accept receipt supplements (original content + shipment/part binding).

        Whole batch is validated before writing.  Same receipt_id + same content
        is an idempotent replay; different content is rejected (异文拒绝).  A
        binding may not be re-pointed at another shipment, and every part must
        belong to the bound shipment.
        """
        if isinstance(payload, list):
            raw = payload
        elif isinstance(payload, dict) and isinstance(payload.get("receipts"), list):
            raw = payload["receipts"]
        else:
            raise ValidationError("receipt supplements must be a list or object with 'receipts'")
        parsed = [parse_supplement(r) for r in raw]

        seen: dict[str, str] = {}
        for r in parsed:
            key = r["receipt_id"]
            if key in seen and seen[key] != r["core_hash"]:
                raise ConflictError(
                    f"receipt {key} appears twice with different content in the batch"
                )
            seen[key] = r["core_hash"]

        now = utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            new = replayed = 0
            for r in parsed:
                shipment = self._shipment_row(r["shipment_id"])
                if shipment is None:
                    raise ValidationError(
                        f"receipt {r['receipt_id']}: unknown shipment {r['shipment_id']!r}"
                    )
                required = set(json.loads(shipment["required_parts"]))
                unknown = [p for p in r["part_ids"] if p not in required]
                if unknown:
                    raise ValidationError(
                        f"receipt {r['receipt_id']}: parts {unknown} do not belong to "
                        f"shipment {r['shipment_id']}"
                    )
                # Consignee / region / type consistency is a *coverage* condition,
                # not a binding error: a supplement whose receipt context differs
                # from the shipment is accepted but never covers any part.

                existing = self._conn.execute(
                    "SELECT * FROM delivery_receipts WHERE receipt_id=?",
                    (r["receipt_id"],),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != r["core_hash"]:
                        raise ConflictError(
                            f"receipt {r['receipt_id']} already stored with different content"
                        )
                    stored_parts = json.loads(existing["part_bindings"])
                    if existing["shipment_id"] != r["shipment_id"] or set(stored_parts) != set(r["part_ids"]):
                        raise ConflictError(
                            f"receipt {r['receipt_id']} binding differs from stored binding "
                            f"({existing['shipment_id']}:{stored_parts}); a receipt cannot be "
                            f"reused for another shipment or different parts"
                        )
                    replayed += 1
                    continue

                self._conn.execute(
                    """INSERT INTO delivery_receipts (receipt_id, consignee, signer,
                           signed_day, received_day, region, document_type, claimed_chain,
                           shipment_id, part_bindings, content_hash, accepted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["receipt_id"], r["consignee"], r["signer"], r["signed_day"],
                     r["received_day"], r["region"], r["document_type"],
                     canonical(r["claimed_chain"]), r["shipment_id"], canonical(r["part_ids"]),
                     r["core_hash"], now),
                )
                for part in r["part_ids"]:
                    self._conn.execute(
                        "INSERT INTO part_bindings (receipt_id, shipment_id, part_id) "
                        "VALUES (?,?,?)",
                        (r["receipt_id"], r["shipment_id"], part),
                    )
                new += 1
            self._record_document("supplements", payload, now)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {"offered": len(parsed), "new": new, "replayed": replayed}

    def _shipment_row(self, shipment_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)
        ).fetchone()

    def list_delivery_receipts(self, shipment_id: str | None = None) -> list[dict]:
        if shipment_id is None:
            rows = self._conn.execute(
                "SELECT * FROM delivery_receipts ORDER BY receipt_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM delivery_receipts WHERE shipment_id=? ORDER BY receipt_id",
                (shipment_id,),
            ).fetchall()
        return [self._delivery_receipt_to_dict(r) for r in rows]

    def _delivery_receipt_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "receipt_id": row["receipt_id"],
            "consignee": row["consignee"],
            "signer": row["signer"],
            "signed_day": row["signed_day"],
            "received_day": row["received_day"],
            "region": row["region"],
            "document_type": row["document_type"],
            "claimed_chain": json.loads(row["claimed_chain"]),
            "shipment_id": row["shipment_id"],
            "part_ids": json.loads(row["part_bindings"]),
            "core_hash": row["content_hash"],
            "accepted_at": row["accepted_at"],
        }

    def get_delivery_receipt(self, receipt_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM delivery_receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"delivery receipt {receipt_id!r} not found")
        return self._delivery_receipt_to_dict(row)

    # --------------------------------------------------------------- coverage
    def _evaluate_evidence(self, shipment: dict) -> list[dict]:
        """Ask the authority port about every supplement bound to a shipment.

        A supplement only counts if:
          * its consignee and region/type match the shipment;
          * the delegation side matched an original with the same content and
            judged it valid.
        Parts are unioned by part_id; repeated originals never add quantity.
        """
        evidence = []
        for rec in self.list_delivery_receipts(shipment["shipment_id"]):
            core = {
                "receipt_id": rec["receipt_id"],
                "consignee": rec["consignee"],
                "signer": rec["signer"],
                "signed_day": rec["signed_day"],
                "received_day": rec["received_day"],
                "region": rec["region"],
                "document_type": rec["document_type"],
                "claimed_grant_ids": rec["claimed_chain"],
            }
            verdict = self.authority.assess(core)
            context_ok = (
                rec["consignee"] == shipment["consignee"]
                and rec["region"] == shipment["region"]
                and rec["document_type"] == shipment["document_type"]
            )
            counts = bool(verdict.get("matched")) and bool(verdict.get("valid")) and context_ok
            reasons = []
            if not context_ok:
                reasons.append("receipt consignee/region/type do not match shipment")
            if not verdict.get("matched"):
                reasons.append("no matching receipt original on delegation side")
            elif not verdict.get("valid"):
                reasons.append(verdict.get("reason") or "delegation assessment invalid")
            evidence.append(
                {
                    "receipt_id": rec["receipt_id"],
                    "part_ids": rec["part_ids"],
                    "context_ok": context_ok,
                    "authority_matched": bool(verdict.get("matched")),
                    "authority_valid": bool(verdict.get("valid")),
                    "counts": counts,
                    "authority_fingerprint": verdict.get("authority_fingerprint"),
                    "original_hash": verdict.get("original_hash"),
                    "reason": None if counts else "; ".join(reasons),
                    "levels": verdict.get("levels", []),
                }
            )
        return evidence

    def coverage(self, shipment_id: str) -> dict:
        shipment = self.get_shipment(shipment_id)
        evidence = self._evaluate_evidence(shipment)
        covered: dict[str, list[str]] = {}
        for ev in evidence:
            if ev["counts"]:
                for part in ev["part_ids"]:
                    covered.setdefault(part, []).append(ev["receipt_id"])
        required = shipment["required_parts"]
        missing = [p for p in required if p not in covered]
        return {
            "shipment_id": shipment_id,
            "required_parts": required,
            "covered_parts": sorted(covered),
            "covered_by": {p: sorted(covered[p]) for p in sorted(covered)},
            "missing_parts": missing,
            "complete": not missing,
            "evidence": evidence,
        }

    # ---------------------------------------------------------- preparation
    def prepare(self, shipment_id: str) -> dict:
        """Snapshot the current complete evidence so confirmation can be pinned to it."""
        cov = self.coverage(shipment_id)
        if not cov["complete"]:
            raise StateError(
                f"shipment {shipment_id} is not complete; missing parts {cov['missing_parts']}"
            )
        evidence_snapshot = self._evidence_snapshot(cov)
        evidence_hash = content_hash(evidence_snapshot)
        authority_snapshot = self._authority_snapshot(cov["evidence"])
        authority_hash = content_hash(authority_snapshot)
        now = utc_now()
        self._conn.execute(
            """INSERT INTO preparations (shipment_id, parts, evidence, evidence_hash,
                   authority, authority_hash, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(shipment_id) DO UPDATE SET
                   parts=excluded.parts, evidence=excluded.evidence,
                   evidence_hash=excluded.evidence_hash, authority=excluded.authority,
                   authority_hash=excluded.authority_hash, created_at=excluded.created_at""",
            (shipment_id, canonical(cov["required_parts"]), canonical(evidence_snapshot),
             evidence_hash, canonical(authority_snapshot), authority_hash, now),
        )
        return {
            "shipment_id": shipment_id,
            "parts": cov["required_parts"],
            "covered_by": cov["covered_by"],
            "evidence": evidence_snapshot,
            "evidence_hash": evidence_hash,
            "authority": authority_snapshot,
            "authority_hash": authority_hash,
            "created_at": now,
        }

    def _evidence_snapshot(self, cov: dict) -> dict:
        contributing = sorted({r for rs in cov["covered_by"].values() for r in rs})
        return {
            "required_parts": cov["required_parts"],
            "covered_by": cov["covered_by"],
            "contributing_receipts": contributing,
            "original_hashes": {
                ev["receipt_id"]: ev["original_hash"]
                for ev in cov["evidence"] if ev["counts"]
            },
        }

    def _authority_snapshot(self, evidence: list[dict]) -> dict:
        return {
            "receipts": {
                ev["receipt_id"]: {
                    "fingerprint": ev["authority_fingerprint"],
                    "original_hash": ev["original_hash"],
                    "levels": ev["levels"],
                }
                for ev in evidence if ev["counts"]
            }
        }

    def _current_evidence_hash(self, shipment_id: str) -> tuple[dict, str]:
        cov = self.coverage(shipment_id)
        return cov, content_hash(self._evidence_snapshot(cov))

    # --------------------------------------------------------- confirmation
    def _ensure_review_state(self, shipment_id: str) -> sqlite3.Row | None:
        """Auto-flag a CONFIRMED shipment whose live coverage has regressed.

        Persisted once; idempotent thereafter.  This is what makes an authority
        correction visibly move the shipment to NEEDS_REVIEW even outside an
        explicit batch reconcile.
        """
        state = self._state_row(shipment_id)
        if state is None or state["status"] != "CONFIRMED":
            return state
        cov = self.coverage(shipment_id)
        if cov["complete"]:
            return state
        self.mark_needs_review(
            shipment_id,
            "current evidence no longer covers all required parts: "
            f"missing {cov['missing_parts']}",
        )
        return self._state_row(shipment_id)

    def confirmation_status(self, shipment_id: str) -> dict:
        state = self._ensure_review_state(shipment_id)
        cov = self.coverage(shipment_id)
        if state is None:
            return {
                "shipment_id": shipment_id,
                "status": "INCOMPLETE" if not cov["complete"] else "READY",
                "current_complete": cov["complete"],
                "missing_parts": cov["missing_parts"],
                "confirmation": None,
            }
        confirmation = self.get_confirmation(state["active_confirmation_id"])
        return {
            "shipment_id": shipment_id,
            "status": state["status"],
            "current_complete": cov["complete"],
            "missing_parts": cov["missing_parts"],
            "confirmation": confirmation,
            "confirmed_complete": state["status"] == "CONFIRMED",
            "review_reason": state["review_reason"],
        }

    def _state_row(self, shipment_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM shipment_state WHERE shipment_id=?", (shipment_id,)
        ).fetchone()

    def confirm(self, shipment_id: str, expected_evidence_hash: str | None = None) -> dict:
        """Confirm only when the pinned preparation still matches complete evidence.

        Idempotent when the current status is already CONFIRMED with the same
        evidence.  From NEEDS_REVIEW, a new confirmation linked to the prior one
        is created; the prior confirmation row is never modified.
        """
        shipment = self.get_shipment(shipment_id)
        cov, evidence_hash = self._current_evidence_hash(shipment_id)
        if not cov["complete"]:
            raise StateError(
                f"shipment {shipment_id} is not complete; missing parts {cov['missing_parts']}"
            )

        evidence_snapshot = self._evidence_snapshot(cov)
        authority_snapshot = self._authority_snapshot(cov["evidence"])
        authority_hash = content_hash(authority_snapshot)

        state = self._state_row(shipment_id)
        if state is not None and state["status"] == "CONFIRMED":
            current = self.get_confirmation(state["active_confirmation_id"])
            if current["evidence_hash"] == evidence_hash:
                return current
            raise StateError(
                f"shipment {shipment_id} is already confirmed with different evidence"
            )

        prep = self._conn.execute(
            "SELECT * FROM preparations WHERE shipment_id=?", (shipment_id,)
        ).fetchone()
        if prep is None:
            raise StalePreparationError(
                f"shipment {shipment_id}: a preparation is required before confirmation"
            )
        if prep["evidence_hash"] != evidence_hash:
            raise StalePreparationError(
                f"shipment {shipment_id}: evidence changed since preparation; prepare again"
            )
        if prep["authority_hash"] != authority_hash:
            raise StalePreparationError(
                f"shipment {shipment_id}: authority state changed since preparation; "
                f"prepare again"
            )
        if expected_evidence_hash is not None and expected_evidence_hash != evidence_hash:
            raise StalePreparationError(
                f"shipment {shipment_id}: caller's evidence hash does not match current evidence"
            )

        previous_id = state["active_confirmation_id"] if state is not None else None
        seq = self._next_seq(shipment_id)
        confirmation_id = self._next_global_id()
        now = utc_now()
        body = {
            "confirmation_id": confirmation_id,
            "shipment_id": shipment_id,
            "seq": seq,
            "previous_confirmation_id": previous_id,
            "parts": cov["required_parts"],
            "covered_by": cov["covered_by"],
            "evidence": evidence_snapshot,
            "authority": authority_snapshot,
            "evidence_hash": evidence_hash,
            "authority_hash": authority_hash,
            "created_at": now,
            "consignee": shipment["consignee"],
        }

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """INSERT INTO confirmations (confirmation_id, shipment_id, seq, previous_id,
                       parts, covered_by, evidence, authority, evidence_hash, authority_hash,
                       created_at, payload, content_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (confirmation_id, shipment_id, seq, previous_id,
                 canonical(cov["required_parts"]), canonical(cov["covered_by"]),
                 canonical(evidence_snapshot), canonical(authority_snapshot), evidence_hash,
                 authority_hash, now, canonical(body), content_hash(body)),
            )
            self._conn.execute(
                """INSERT INTO shipment_state (shipment_id, status, active_confirmation_id,
                       review_reason, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(shipment_id) DO UPDATE SET
                       status=excluded.status,
                       active_confirmation_id=excluded.active_confirmation_id,
                       review_reason=excluded.review_reason,
                       updated_at=excluded.updated_at""",
                (shipment_id, "CONFIRMED", confirmation_id, None, now),
            )
            self._conn.execute(
                "DELETE FROM preparations WHERE shipment_id=?", (shipment_id,)
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return body

    def _next_seq(self, shipment_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM confirmations WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        return row["n"]

    def _next_global_id(self) -> str:
        """Monotonic DEL<n> across the whole depot (unique primary key)."""
        row = self._conn.execute(
            "SELECT value FROM delivery_meta WHERE key='confirmation_counter'"
        ).fetchone()
        n = (int(row["value"]) if row else 0) + 1
        self._conn.execute(
            "INSERT INTO delivery_meta (key, value) VALUES ('confirmation_counter', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(n),),
        )
        return f"DEL{n}"

    # ---------------------------------------------------------------- review
    def mark_needs_review(self, shipment_id: str, reason: str) -> dict:
        """Move a CONFIRMED shipment to NEEDS_REVIEW without touching the confirmation.

        The active confirmation identity is retained; only shipment_state changes
        (and an append-only review_log entry is added).
        """
        state = self._state_row(shipment_id)
        if state is None:
            raise StateError(f"shipment {shipment_id} has no confirmation to review")
        if state["status"] == "NEEDS_REVIEW":
            return self.confirmation_status(shipment_id)
        now = utc_now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE shipment_state SET status=?, review_reason=?, updated_at=? "
                "WHERE shipment_id=?",
                ("NEEDS_REVIEW", reason, now, shipment_id),
            )
            self._conn.execute(
                "INSERT INTO review_log (shipment_id, confirmation_id, reason, created_at) "
                "VALUES (?,?,?,?)",
                (shipment_id, state["active_confirmation_id"], reason, now),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.confirmation_status(shipment_id)

    def get_confirmation(self, confirmation_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM confirmations WHERE confirmation_id=?", (confirmation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"confirmation {confirmation_id!r} not found")
        return json.loads(row["payload"])

    def confirmations_for(self, shipment_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload FROM confirmations WHERE shipment_id=? ORDER BY seq",
            (shipment_id,),
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def review_history(self, shipment_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_log WHERE shipment_id=? ORDER BY seq", (shipment_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- reconcile
    def reconcile(self, shipment_ids: list[str] | None = None) -> dict:
        """Flag confirmed shipments whose current evidence no longer covers them."""
        if shipment_ids is None:
            rows = self._conn.execute(
                "SELECT shipment_id FROM shipment_state WHERE status='CONFIRMED'"
            ).fetchall()
            shipment_ids = [r["shipment_id"] for r in rows]
        flagged = []
        for sid in sorted(set(shipment_ids)):
            state = self._state_row(sid)
            if state is None or state["status"] != "CONFIRMED":
                continue
            cov = self.coverage(sid)
            if not cov["complete"]:
                rec = self.mark_needs_review(
                    sid,
                    "current evidence no longer covers all required parts: "
                    f"missing {cov['missing_parts']}",
                )
                flagged.append(rec)
        return {"flagged": flagged, "flagged_count": len(flagged)}

    # -------------------------------------------------------------- documents
    def _record_document(self, key: str, payload: Any, now: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO delivery_documents (doc_key, content_hash, payload, accepted_at) "
            "VALUES (?,?,?,?)",
            (key, content_hash(payload), canonical(payload), now),
        )

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO delivery_meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM delivery_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None
