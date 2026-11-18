"""SQLite-backed course-equivalence catalog + graduation audit library.

Responsibilities
----------------
* Persist catalog revisions, student-record revisions and requirement revisions.
  Re-importing the *same revision number with different content* is rejected
  (:class:`~gradaudit.errors.RevisionConflictError`); identical re-imports are
  idempotent.  Old revisions stay queryable for history.
* Prepare an audit: pin (catalog, records, requirements) revisions and persist
  an independent verification of a candidate plan, including every record's
  point destination.
* Confirm: persist the plan + used revisions.  The original plan/verification
  rows are never deleted.  A confirmation is only valid for the revisions that
  were current when it was prepared; if any of them changes, the confirmation
  is reported stale and cannot be re-confirmed without a fresh prepare.
* Reopen: record an explicit, auditable "no longer currently confirmed" event
  while keeping all history.

The DB is a single file; pass ``:memory:`` for an ephemeral instance.

Schema sketch (all tables created if absent)::

    meta(key PK, value)
    catalog_revisions(revision PK, content_hash, doc_json, created_at)
    catalog_courses(catalog_revision, course_id, PK(catalog_revision,course_id))
    catalog_course_categories(catalog_revision, course_id, category)
    catalog_equivalences(catalog_revision, external_course, ordinal, course_id)
    records_revisions(revision PK, student_id, content_hash, doc_json, created_at)
    transfer_records(records_revision, record_id, external_course, points)
    requirements_revisions(revision PK, request_id, content_hash, doc_json, created_at)
    requirement_groups(requirements_revision, group_id, ordinal, minimum_points)
    requirement_group_categories(requirements_revision, group_id, category)
    audit_requests(request_id PK, subject_key, requirements_revision, ...)
    audit_preparations(preparation_id PK, request_id, subject_key, catalog_revision,
                       records_revision, requirements_revision, plan_json, plan_hash,
                       accepted, verification_json, prepared_at)
    audit_confirmations(confirmation_id PK, preparation_id FK, request_id,
                        catalog_revision, records_revision, requirements_revision,
                        plan_hash, confirmed_at, reopened_at, reopened_reason)
    audit_events(event_id PK, request_id, kind, detail_json, at)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .errors import (
    ConfirmationError,
    ConfirmationStaleError,
    GradAuditError,
    NotPreparedError,
    RevisionConflictError,
    RevisionNotFoundError,
    SchemaError,
)
from .model import (
    Catalog,
    Plan,
    Records,
    Requirements,
    content_hash,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
)
from .verify import VerifyResult, verify_plan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """A thin, explicit persistence layer over SQLite.

    All public methods are transactional; the connection is used in its default
    (autocommit-off) mode with explicit ``commit``/``rollback`` via
    :meth:`_txn`.
    """

    def __init__(self, db_path: str = ":memory:", *, subject_key: Optional[str] = None):
        self.db_path = db_path
        self.subject_key = subject_key or "default"
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def _txn(self):
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS catalog_revisions (
                revision INTEGER PRIMARY KEY,
                content_hash TEXT NOT NULL,
                doc_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_courses (
                catalog_revision INTEGER NOT NULL,
                course_id TEXT NOT NULL,
                PRIMARY KEY (catalog_revision, course_id),
                FOREIGN KEY (catalog_revision) REFERENCES catalog_revisions(revision)
            );
            CREATE TABLE IF NOT EXISTS catalog_course_categories (
                catalog_revision INTEGER NOT NULL,
                course_id TEXT NOT NULL,
                category TEXT NOT NULL,
                PRIMARY KEY (catalog_revision, course_id, category)
            );
            CREATE TABLE IF NOT EXISTS catalog_equivalences (
                catalog_revision INTEGER NOT NULL,
                external_course TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                course_id TEXT NOT NULL,
                PRIMARY KEY (catalog_revision, external_course, ordinal)
            );

            CREATE TABLE IF NOT EXISTS records_revisions (
                revision INTEGER PRIMARY KEY,
                student_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                doc_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transfer_records (
                records_revision INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                external_course TEXT NOT NULL,
                points INTEGER NOT NULL,
                PRIMARY KEY (records_revision, record_id)
            );

            CREATE TABLE IF NOT EXISTS requirements_revisions (
                revision INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                doc_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requirement_groups (
                requirements_revision INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                minimum_points INTEGER NOT NULL,
                PRIMARY KEY (requirements_revision, group_id)
            );
            CREATE TABLE IF NOT EXISTS requirement_group_categories (
                requirements_revision INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                category TEXT NOT NULL,
                PRIMARY KEY (requirements_revision, group_id, category)
            );

            CREATE TABLE IF NOT EXISTS audit_requests (
                request_id TEXT PRIMARY KEY,
                subject_key TEXT NOT NULL,
                requirements_revision INTEGER NOT NULL,
                catalog_revision INTEGER NOT NULL,
                records_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_preparations (
                preparation_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                catalog_revision INTEGER NOT NULL,
                records_revision INTEGER NOT NULL,
                requirements_revision INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                verification_json TEXT NOT NULL,
                prepared_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preparation_points (
                preparation_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                points INTEGER NOT NULL,
                PRIMARY KEY (preparation_id, record_id)
            );

            CREATE TABLE IF NOT EXISTS audit_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                preparation_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                catalog_revision INTEGER NOT NULL,
                records_revision INTEGER NOT NULL,
                requirements_revision INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                reopened_at TEXT,
                reopened_reason TEXT,
                FOREIGN KEY (preparation_id) REFERENCES audit_preparations(preparation_id)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # -- helpers ----------------------------------------------------------
    def _event(self, request_id: str, kind: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit_events(request_id, subject_key, kind, detail_json, at) VALUES (?,?,?,?,?)",
            (request_id, self.subject_key, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()),
        )

    def _require_current(
        self,
        *,
        catalog_revision: int,
        records_revision: int,
        requirements_revision: int,
    ) -> None:
        if self.get_catalog(catalog_revision) is None:
            raise RevisionNotFoundError(f"catalog revision {catalog_revision} is not imported", location="catalog_revision")
        if self.get_records(records_revision) is None:
            raise RevisionNotFoundError(f"records revision {records_revision} is not imported", location="records_revision")
        if self.get_requirements(requirements_revision) is None:
            raise RevisionNotFoundError(f"requirements revision {requirements_revision} is not imported", location="requirements_revision")

    # =====================================================================
    # catalog
    # =====================================================================
    def import_catalog(self, doc: Any) -> Catalog:
        catalog = parse_catalog(doc)
        h = catalog.content_hash()
        existing = self.get_catalog(catalog.revision)
        if existing is not None:
            if existing.content_hash() != h:
                raise RevisionConflictError(
                    f"catalog revision {catalog.revision} already imported with different content; "
                    f"refusing to overwrite (bump the revision to publish a new mapping)",
                    location=f"catalog:{catalog.revision}",
                )
            return existing  # idempotent re-import

        with self._txn():
            self.conn.execute(
                "INSERT INTO catalog_revisions(revision, content_hash, doc_json, created_at) VALUES (?,?,?,?)",
                (catalog.revision, h, json.dumps(catalog.raw_document(), ensure_ascii=False, sort_keys=True), _now()),
            )
            for c in catalog.courses.values():
                self.conn.execute(
                    "INSERT INTO catalog_courses(catalog_revision, course_id) VALUES (?,?)",
                    (catalog.revision, c.course_id),
                )
                for cat in sorted(c.categories):
                    self.conn.execute(
                        "INSERT INTO catalog_course_categories(catalog_revision, course_id, category) VALUES (?,?,?)",
                        (catalog.revision, c.course_id, cat),
                    )
            for ext, targets in catalog.equivalences.items():
                for ordinal, cid in enumerate(targets):
                    self.conn.execute(
                        "INSERT INTO catalog_equivalences(catalog_revision, external_course, ordinal, course_id) VALUES (?,?,?,?)",
                        (catalog.revision, ext, ordinal, cid),
                    )
        return catalog

    def get_catalog(self, revision: int) -> Optional[Catalog]:
        row = self.conn.execute(
            "SELECT doc_json FROM catalog_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            return None
        return parse_catalog(json.loads(row["doc_json"]))

    def list_catalog_revisions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT revision, content_hash, created_at FROM catalog_revisions ORDER BY revision"
        ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # records
    # =====================================================================
    def import_records(self, doc: Any, *, expected_student_id: Optional[str] = None) -> Records:
        records = parse_records(doc, expected_student_id=expected_student_id)
        h = records.content_hash()
        existing = self.get_records(records.revision)
        if existing is not None:
            if existing.content_hash() != h:
                raise RevisionConflictError(
                    f"records revision {records.revision} already imported with different content",
                    location=f"records:{records.revision}",
                )
            return existing
        with self._txn():
            self.conn.execute(
                "INSERT INTO records_revisions(revision, student_id, content_hash, doc_json, created_at) VALUES (?,?,?,?,?)",
                (records.revision, records.student_id, h, json.dumps(records.raw_document(), ensure_ascii=False, sort_keys=True), _now()),
            )
            for r in records.records.values():
                self.conn.execute(
                    "INSERT INTO transfer_records(records_revision, record_id, external_course, points) VALUES (?,?,?,?)",
                    (records.revision, r.record_id, r.external_course, r.points),
                )
        return records

    def get_records(self, revision: int) -> Optional[Records]:
        row = self.conn.execute(
            "SELECT doc_json FROM records_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            return None
        return parse_records(json.loads(row["doc_json"]))

    def list_records_revisions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT revision, student_id, content_hash, created_at FROM records_revisions ORDER BY revision"
        ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # requirements
    # =====================================================================
    def import_requirements(self, doc: Any, *, expected_request_id: Optional[str] = None) -> Requirements:
        requirements = parse_requirements(doc, expected_request_id=expected_request_id)
        h = requirements.content_hash()
        existing = self.get_requirements(requirements.revision)
        if existing is not None:
            if existing.content_hash() != h:
                raise RevisionConflictError(
                    f"requirements revision {requirements.revision} already imported with different content",
                    location=f"requirements:{requirements.revision}",
                )
            return existing
        with self._txn():
            self.conn.execute(
                "INSERT INTO requirements_revisions(revision, request_id, content_hash, doc_json, created_at) VALUES (?,?,?,?,?)",
                (requirements.revision, requirements.request_id, h, json.dumps(requirements.raw_document(), ensure_ascii=False, sort_keys=True), _now()),
            )
            for ordinal, g in enumerate(requirements.groups):
                self.conn.execute(
                    "INSERT INTO requirement_groups(requirements_revision, group_id, ordinal, minimum_points) VALUES (?,?,?,?)",
                    (requirements.revision, g.group_id, ordinal, g.minimum_points),
                )
                for cat in sorted(g.allowed_categories):
                    self.conn.execute(
                        "INSERT INTO requirement_group_categories(requirements_revision, group_id, category) VALUES (?,?,?)",
                        (requirements.revision, g.group_id, cat),
                    )
        return requirements

    def get_requirements(self, revision: int) -> Optional[Requirements]:
        row = self.conn.execute(
            "SELECT doc_json FROM requirements_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            return None
        return parse_requirements(json.loads(row["doc_json"]))

    def list_requirements_revisions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT revision, request_id, content_hash, created_at FROM requirements_revisions ORDER BY revision"
        ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # catalog inspection (list candidates + categories, never auto-choose)
    # =====================================================================
    def list_candidates(self, catalog_revision: int, external_course: str) -> List[dict]:
        catalog = self.get_catalog(catalog_revision)
        if catalog is None:
            raise RevisionNotFoundError(f"catalog revision {catalog_revision} is not imported", location="catalog_revision")
        return catalog.candidates_with_categories(external_course)

    def get_course(self, catalog_revision: int, course_id: str) -> dict:
        catalog = self.get_catalog(catalog_revision)
        if catalog is None:
            raise RevisionNotFoundError(f"catalog revision {catalog_revision} is not imported", location="catalog_revision")
        course = catalog.courses.get(course_id)
        if course is None:
            raise RevisionNotFoundError(f"course {course_id!r} not in catalog revision {catalog_revision}", location=course_id)
        return {"course_id": course.course_id, "categories": sorted(course.categories)}

    # =====================================================================
    # request preparation / verification / confirmation
    # =====================================================================
    def register_request(
        self,
        *,
        request_id: str,
        catalog_revision: int,
        records_revision: int,
        requirements_revision: int,
    ) -> None:
        """Remember which revisions a request is currently prepared against."""
        self._require_current(
            catalog_revision=catalog_revision,
            records_revision=records_revision,
            requirements_revision=requirements_revision,
        )
        with self._txn():
            row = self.conn.execute(
                "SELECT requirements_revision, catalog_revision, records_revision FROM audit_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO audit_requests(request_id, subject_key, requirements_revision, catalog_revision, records_revision, created_at) VALUES (?,?,?,?,?,?)",
                    (request_id, self.subject_key, requirements_revision, catalog_revision, records_revision, _now()),
                )
            else:
                self.conn.execute(
                    "UPDATE audit_requests SET requirements_revision=?, catalog_revision=?, records_revision=? WHERE request_id=?",
                    (requirements_revision, catalog_revision, records_revision, request_id),
                )

    def current_prepared_revisions(self, request_id: str) -> Dict[str, int]:
        row = self.conn.execute(
            "SELECT requirements_revision, catalog_revision, records_revision FROM audit_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise NotPreparedError(
                f"request {request_id!r} has no registered preparation; call prepare_audit first",
                location=request_id,
            )
        return {
            "catalog_revision": row["catalog_revision"],
            "records_revision": row["records_revision"],
            "requirements_revision": row["requirements_revision"],
        }

    def prepare_audit(
        self,
        plan_doc: Any,
        *,
        request_id: str,
        catalog_revision: int,
        records_revision: int,
        requirements_revision: int,
    ) -> dict:
        """Independently verify ``plan_doc`` and persist the preparation.

        The verifier is the same independent routine used by ``verify-plan``:
        it never consults a solver or the solver's chosen allocation.  Also
        records every assigned record's point destination for later review.
        """
        self._require_current(
            catalog_revision=catalog_revision,
            records_revision=records_revision,
            requirements_revision=requirements_revision,
        )
        catalog = self.get_catalog(catalog_revision)
        records = self.get_records(records_revision)
        requirements = self.get_requirements(requirements_revision)
        assert catalog and records and requirements
        if requirements.request_id != request_id:
            raise SchemaError(
                f"requirements revision {requirements_revision} belongs to request "
                f"{requirements.request_id!r}, not {request_id!r}",
                location="request_id",
            )

        # A plan can be rejected either at the schema level (e.g. the same
        # record_id listed twice) or at the semantic level.  Both are persisted
        # as a *rejected* preparation so the audit trail shows why; neither can
        # ever be confirmed.
        try:
            plan = parse_plan(plan_doc)
        except GradAuditError as exc:
            prep_id = uuid.uuid4().hex
            reason = exc.as_reason()
            plan_json = json.dumps(plan_doc, ensure_ascii=False, sort_keys=True)
            with self._txn():
                self.conn.execute(
                    """INSERT INTO audit_preparations(
                           preparation_id, request_id, subject_key, catalog_revision,
                           records_revision, requirements_revision, plan_json, plan_hash,
                           accepted, verification_json, prepared_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        prep_id, request_id, self.subject_key, catalog_revision,
                        records_revision, requirements_revision, plan_json,
                        content_hash(plan_doc), 0,
                        json.dumps({"accepted": False, "reasons": [reason]}, ensure_ascii=False, sort_keys=True),
                        _now(),
                    ),
                )
                self._event(request_id, "prepared", {"preparation_id": prep_id, "accepted": False})
            return {
                "preparation_id": prep_id,
                "accepted": False,
                "verification": {"accepted": False, "reasons": [reason]},
                "revisions": {
                    "catalog_revision": catalog_revision,
                    "records_revision": records_revision,
                    "requirements_revision": requirements_revision,
                },
            }

        result = verify_plan(plan, catalog, records, requirements)

        prep_id = uuid.uuid4().hex
        plan_json = json.dumps(plan.as_document(), ensure_ascii=False, sort_keys=True)
        with self._txn():
            self.conn.execute(
                """INSERT INTO audit_preparations(
                       preparation_id, request_id, subject_key, catalog_revision,
                       records_revision, requirements_revision, plan_json, plan_hash,
                       accepted, verification_json, prepared_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prep_id, request_id, self.subject_key, catalog_revision,
                    records_revision, requirements_revision, plan_json,
                    content_hash(plan.as_document()), 1 if result.accepted else 0,
                    json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True), _now(),
                ),
            )
            for a in plan.assignments:
                rec = records.records.get(a.record_id)
                if rec is None:
                    continue
                self.conn.execute(
                    "INSERT OR REPLACE INTO preparation_points(preparation_id, record_id, course_id, group_id, points) VALUES (?,?,?,?,?)",
                    (prep_id, a.record_id, a.course_id, a.group_id, rec.points),
                )
            self.conn.execute(
                "INSERT INTO audit_requests(request_id, subject_key, requirements_revision, catalog_revision, records_revision, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "requirements_revision=excluded.requirements_revision, catalog_revision=excluded.catalog_revision, records_revision=excluded.records_revision",
                (request_id, self.subject_key, requirements_revision, catalog_revision, records_revision, _now()),
            )
            self._event(request_id, "prepared", {"preparation_id": prep_id, "accepted": result.accepted})
        return {
            "preparation_id": prep_id,
            "accepted": result.accepted,
            "verification": result.as_dict(),
            "revisions": {
                "catalog_revision": catalog_revision,
                "records_revision": records_revision,
                "requirements_revision": requirements_revision,
            },
        }

    def _latest_catalog_revision(self) -> Optional[int]:
        row = self.conn.execute("SELECT MAX(revision) AS r FROM catalog_revisions").fetchone()
        return row["r"]

    def _latest_requirements_revision(self, request_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(revision) AS r FROM requirements_revisions WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return row["r"]

    def _latest_records_revision(self, student_id: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(revision) AS r FROM records_revisions WHERE student_id = ?",
            (student_id,),
        ).fetchone()
        return row["r"]

    def effective_current_revisions(self, request_id: str) -> Dict[str, Optional[int]]:
        """The revisions a *new* preparation today would use.

        This is the newest imported catalog revision, the newest requirements
        revision for this request, and the newest records revision for the
        student who owns the request.  Any of these moving past the pinned
        revisions invalidates an old confirmation ("after preparing, a change
        to catalog / requirements / student records => old plan cannot stay
        confirmed").
        """
        row = self.conn.execute(
            "SELECT records_revision FROM audit_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        student_id = None
        if row is not None:
            rec = self.get_records(row["records_revision"])
            if rec is not None:
                student_id = rec.student_id
        return {
            "catalog_revision": self._latest_catalog_revision(),
            "records_revision": self._latest_records_revision(student_id) if student_id else None,
            "requirements_revision": self._latest_requirements_revision(request_id),
        }

    def _activation_ok(self, prep: sqlite3.Row) -> Tuple[bool, Optional[dict]]:
        """Current effective revisions must equal the preparation's revisions."""
        try:
            cur = self.current_prepared_revisions(prep["request_id"])
        except NotPreparedError:
            return False, {"code": "not_prepared", "message": "request is no longer prepared"}
        effective = self.effective_current_revisions(prep["request_id"])
        expected = {
            "catalog_revision": prep["catalog_revision"],
            "records_revision": prep["records_revision"],
            "requirements_revision": prep["requirements_revision"],
        }
        drift = {}
        for k, v in expected.items():
            current = effective.get(k)
            if current is None:
                current = cur[k]
            if v != current:
                drift[k] = {"prepared": v, "current": current}
        if drift:
            return False, {
                "code": "confirmation_stale",
                "message": "prepared catalog/records/requirements revisions changed; re-prepare before confirming",
                "location": prep["request_id"],
                "detail": drift,
            }
        return True, None

    def confirm_audit(self, *, request_id: str, preparation_id: Optional[str] = None) -> dict:
        """Activate a preparation.

        The preparation must have been *accepted* by the independent verifier,
        and the current prepared revisions must still equal the ones it used.
        The original preparation (and its plan) is preserved; this only inserts
        a confirmation row.
        """
        prep = None
        if preparation_id is not None:
            prep = self.conn.execute(
                "SELECT * FROM audit_preparations WHERE preparation_id = ? AND request_id = ?",
                (preparation_id, request_id),
            ).fetchone()
            if prep is None:
                raise NotPreparedError(
                    f"preparation {preparation_id!r} not found for request {request_id!r}",
                    location=preparation_id,
                )
        else:
            prep = self.conn.execute(
                "SELECT * FROM audit_preparations WHERE request_id = ? ORDER BY prepared_at DESC, rowid DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            if prep is None:
                raise NotPreparedError(
                    f"request {request_id!r} has not been prepared", location=request_id
                )

        if not prep["accepted"]:
            verification = json.loads(prep["verification_json"])
            raise ConfirmationError(
                "cannot confirm: the prepared plan was rejected by the independent verifier",
                location=request_id,
                detail={"reasons": verification.get("reasons", [])},
            )

        ok, reason = self._activation_ok(prep)
        if not ok:
            raise ConfirmationStaleError(
                reason["message"], location=request_id, detail=reason.get("detail")
            )

        conf_id = uuid.uuid4().hex
        with self._txn():
            # Any prior live confirmation for this request is superseded (kept).
            self.conn.execute(
                "UPDATE audit_confirmations SET reopened_at = ?, reopened_reason = ? "
                "WHERE request_id = ? AND subject_key = ? AND reopened_at IS NULL",
                (_now(), "superseded by new confirmation", request_id, self.subject_key),
            )
            self.conn.execute(
                """INSERT INTO audit_confirmations(
                       confirmation_id, preparation_id, request_id, subject_key,
                       catalog_revision, records_revision, requirements_revision,
                       plan_json, plan_hash, confirmed_at, reopened_at, reopened_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                (
                    conf_id, prep["preparation_id"], request_id, self.subject_key,
                    prep["catalog_revision"], prep["records_revision"], prep["requirements_revision"],
                    prep["plan_json"], prep["plan_hash"], _now(),
                ),
            )
            self._event(request_id, "confirmed", {"confirmation_id": conf_id, "preparation_id": prep["preparation_id"]})
        return {
            "confirmation_id": conf_id,
            "request_id": request_id,
            "revisions": {
                "catalog_revision": prep["catalog_revision"],
                "records_revision": prep["records_revision"],
                "requirements_revision": prep["requirements_revision"],
            },
        }

    def confirmation_status(self, request_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM audit_confirmations WHERE request_id = ? AND subject_key = ? AND reopened_at IS NULL "
            "ORDER BY confirmed_at DESC, rowid DESC LIMIT 1",
            (request_id, self.subject_key),
        ).fetchone()
        if row is None:
            hist = self.conn.execute(
                "SELECT COUNT(*) AS n FROM audit_confirmations WHERE request_id = ? AND subject_key = ?",
                (request_id, self.subject_key),
            ).fetchone()
            return {
                "request_id": request_id,
                "confirmed": False,
                "reason": "no live confirmation",
                "historical_confirmations": hist["n"],
            }
        stale = None
        try:
            cur = self.current_prepared_revisions(request_id)
            effective = self.effective_current_revisions(request_id)
            expected = {
                "catalog_revision": row["catalog_revision"],
                "records_revision": row["records_revision"],
                "requirements_revision": row["requirements_revision"],
            }
            drift = {}
            for k, v in expected.items():
                current = effective.get(k)
                if current is None:
                    current = cur[k]
                if v != current:
                    drift[k] = {"confirmed": v, "current": current}
            if drift:
                stale = drift
        except NotPreparedError:
            stale = {"__all__": "request no longer prepared"}
        return {
            "request_id": request_id,
            "confirmed": stale is None,
            "stale": stale,
            "confirmation_id": row["confirmation_id"],
            "preparation_id": row["preparation_id"],
            "revisions": {
                "catalog_revision": row["catalog_revision"],
                "records_revision": row["records_revision"],
                "requirements_revision": row["requirements_revision"],
            },
            "plan_hash": row["plan_hash"],
            "confirmed_at": row["confirmed_at"],
        }

    def reopen_audit(self, *, request_id: str, reason: str) -> dict:
        """Explicitly re-open the request (keep history, clear live confirmation)."""
        row = self.conn.execute(
            "SELECT confirmation_id FROM audit_confirmations "
            "WHERE request_id = ? AND subject_key = ? AND reopened_at IS NULL",
            (request_id, self.subject_key),
        ).fetchone()
        with self._txn():
            if row is not None:
                self.conn.execute(
                    "UPDATE audit_confirmations SET reopened_at = ?, reopened_reason = ? WHERE confirmation_id = ?",
                    (_now(), reason, row["confirmation_id"]),
                )
            self._event(request_id, "reopened", {"reason": reason, "confirmation_id": row["confirmation_id"] if row else None})
        return {
            "request_id": request_id,
            "reopened": row is not None,
            "reason": reason,
        }

    # =====================================================================
    # history / audit trail
    # =====================================================================
    def history(self, request_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT event_id, kind, detail_json, at FROM audit_events WHERE request_id = ? AND subject_key = ? ORDER BY event_id",
            (request_id, self.subject_key),
        ).fetchall()
        return [
            {"event_id": r["event_id"], "kind": r["kind"], "detail": json.loads(r["detail_json"]), "at": r["at"]}
            for r in rows
        ]

    def list_confirmations(self, request_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_confirmations WHERE request_id = ? AND subject_key = ? ORDER BY confirmed_at, rowid",
            (request_id, self.subject_key),
        ).fetchall()
        return [
            {
                "confirmation_id": r["confirmation_id"],
                "preparation_id": r["preparation_id"],
                "catalog_revision": r["catalog_revision"],
                "records_revision": r["records_revision"],
                "requirements_revision": r["requirements_revision"],
                "plan_hash": r["plan_hash"],
                "plan": json.loads(r["plan_json"]),
                "confirmed_at": r["confirmed_at"],
                "reopened_at": r["reopened_at"],
                "reopened_reason": r["reopened_reason"],
            }
            for r in rows
        ]

    def preparation_points(self, preparation_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT record_id, course_id, group_id, points FROM preparation_points WHERE preparation_id = ? ORDER BY record_id",
            (preparation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_preparations(self, request_id: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT preparation_id, accepted, plan_hash, prepared_at, catalog_revision, records_revision, requirements_revision "
            "FROM audit_preparations WHERE request_id = ? AND subject_key = ? ORDER BY prepared_at, rowid",
            (request_id, self.subject_key),
        ).fetchall()
        return [dict(r) for r in rows]
