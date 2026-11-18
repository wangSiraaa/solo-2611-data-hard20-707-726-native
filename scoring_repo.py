"""Scoring repository: respondents, answer revisions and immutable scored reports.

Contract highlights
-------------------
* Answers are validated as a whole.  An unknown question, an out-of-range value
  or a boolean answer rejects the entire submission; nothing is stored.
* A revision stores an exact content hash.  Re-submitting identical content is
  an idempotent no-op; different content under the same revision is rejected.
* Eligible scores are computed from the catalog's per-scale entries only; the
  catalog is read, never mutated by scoring.
* Missing entries contribute their own legal range to totals, never an imputed
  mean.  ``mean`` is the reduced exact rational of answered weighted sum over
  answered weight, or ``undefined`` when no weight was answered.
* Published scores are immutable.  An identical re-publish is a no-op; a
  different result for an existing report id is rejected, never overwritten.
* Only historical (``legacy-*``) products may be imported as ``published-scores/1``
  and they carry no new scores.  New products are always ``scoring-product/2``.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from catalog import Catalog, ScoreResult
from common import (ContractError, canonical_json, content_hash, is_bool, is_int,
                    require_int, sha256_text, transaction, utc_now, validate_id)

NEW_PRODUCT_FORMAT = "scoring-product/2"
NEW_CONTRACT = "per-scale-entry"
LEGACY_PRODUCT_FORMAT = "published-scores/1"
MISSING = "MISSING"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS respondents (
    respondent_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_revisions (
    respondent_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    answers_canonical TEXT NOT NULL,
    answers_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (respondent_id, revision),
    FOREIGN KEY (respondent_id) REFERENCES respondents(respondent_id),
    CHECK (revision >= 1)
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    format TEXT NOT NULL,
    scoring_contract TEXT NOT NULL,
    source TEXT NOT NULL,
    catalog_version TEXT,
    created_at TEXT NOT NULL,
    document_json TEXT NOT NULL,
    document_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS score_entries (
    report_id TEXT NOT NULL,
    scale_id TEXT NOT NULL,
    scale_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    respondent_id TEXT NOT NULL,
    answer_revision INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    ineligible_reason TEXT,
    missing_weight INTEGER NOT NULL,
    missing_weight_limit INTEGER NOT NULL,
    answered_weight INTEGER,
    weighted_sum INTEGER,
    min_total INTEGER,
    max_total INTEGER,
    mean TEXT,
    -- per-entry trace
    question_id TEXT NOT NULL,
    entry_min INTEGER NOT NULL,
    entry_max INTEGER NOT NULL,
    entry_weight INTEGER NOT NULL,
    entry_reverse INTEGER NOT NULL,
    answer TEXT NOT NULL,
    contribution INTEGER,
    possible_contribution_min INTEGER NOT NULL,
    possible_contribution_max INTEGER NOT NULL,
    PRIMARY KEY (report_id, respondent_id, answer_revision, scale_id,
                 scale_version, ordinal),
    FOREIGN KEY (report_id) REFERENCES reports(report_id),
    FOREIGN KEY (respondent_id, answer_revision)
        REFERENCES answer_revisions(respondent_id, revision),
    CHECK (eligible IN (0, 1)),
    CHECK (missing_weight >= 0),
    CHECK (missing_weight_limit >= 0),
    CHECK (entry_weight > 0),
    CHECK (entry_reverse IN (0, 1)),
    CHECK (entry_min <= entry_max)
);
CREATE INDEX IF NOT EXISTS idx_score_entries_lookup
    ON score_entries(respondent_id, answer_revision, scale_id, scale_version);
"""


class ScoringRepository:
    def __init__(self, connection: sqlite3.Connection, catalog: Catalog) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.catalog = catalog

    @classmethod
    def open(cls, path: str, catalog: Catalog) -> "ScoringRepository":
        connection = sqlite3.connect(path, isolation_level=None)
        repo = cls(connection, catalog)
        repo.conn.executescript(SCHEMA)
        repo.conn.execute(
            "INSERT INTO meta(key, value) VALUES('scoring_schema', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        return repo

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- validation
    def validate_answers(self, answers: Any) -> dict[str, Any]:
        """Validate a whole answer map against the current question bank."""
        if not isinstance(answers, dict):
            raise ContractError("invalid_answers", "answers must be an object")
        validated: dict[str, Any] = {}
        for question_id, value in answers.items():
            validate_id(question_id, "question_id")
            question = self.catalog.get_question(question_id)
            if question is None:
                raise ContractError("unknown_question",
                                    "answer references a question outside the bank",
                                    question_id=question_id)
            if value == MISSING:
                validated[question_id] = MISSING
                continue
            if is_bool(value):
                raise ContractError("boolean_answer",
                                    "boolean answers are rejected",
                                    question_id=question_id, value=repr(value))
            if not is_int(value):
                raise ContractError("invalid_answer",
                                    "answer must be an integer or MISSING",
                                    question_id=question_id, value=repr(value))
            if not (question["min"] <= value <= question["max"]):
                raise ContractError(
                    "answer_out_of_range",
                    "answer outside the question's allowed range",
                    question_id=question_id, answer=value,
                    min=question["min"], max=question["max"])
            validated[question_id] = value
        return validated

    # ------------------------------------------------------------ answers
    def submit_answers(self, respondent_id: Any, revision: Any,
                       answers: Any) -> dict[str, Any]:
        """Store one answer revision atomically.

        Returns whether the revision was stored, replayed identically, or
        rejected because the same revision already holds different content.
        """
        validate_id(respondent_id, "respondent_id")
        require_int(revision, "revision", minimum=1, code="invalid_revision")
        validated = self.validate_answers(answers)
        canonical = canonical_json(validated)
        digest = sha256_text(canonical)

        # Validate before opening the write transaction.
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT answers_canonical, answers_hash FROM answer_revisions "
                "WHERE respondent_id = ? AND revision = ?",
                (respondent_id, revision)).fetchone()
            if existing is not None:
                if existing["answers_hash"] != digest:
                    raise ContractError(
                        "answer_revision_conflict",
                        "same revision already stored with different content",
                        respondent_id=respondent_id, revision=revision)
                return {"status": "duplicate_identical", "respondent_id": respondent_id,
                        "revision": revision, "answers_hash": digest,
                        "answers": validated}

            max_row = self.conn.execute(
                "SELECT MAX(revision) AS r FROM answer_revisions WHERE respondent_id = ?",
                (respondent_id,)).fetchone()
            max_revision = max_row["r"]
            if max_revision is not None and revision <= max_revision:
                raise ContractError(
                    "answer_revision_rollback",
                    "new answer revision must be greater than the latest revision",
                    respondent_id=respondent_id, revision=revision,
                    latest=max_revision)

            self.conn.execute(
                "INSERT INTO respondents(respondent_id, created_at) VALUES(?, ?) "
                "ON CONFLICT(respondent_id) DO NOTHING",
                (respondent_id, utc_now()))
            self.conn.execute(
                "INSERT INTO answer_revisions(respondent_id, revision, answers_canonical, "
                "answers_hash, created_at) VALUES(?,?,?,?,?)",
                (respondent_id, revision, canonical, digest, utc_now()))
        return {"status": "stored", "respondent_id": respondent_id,
                "revision": revision, "answers_hash": digest, "answers": validated}

    def get_answers(self, respondent_id: Any, revision: Any) -> dict[str, Any] | None:
        validate_id(respondent_id, "respondent_id")
        require_int(revision, "revision", minimum=1)
        row = self.conn.execute(
            "SELECT answers_canonical FROM answer_revisions "
            "WHERE respondent_id = ? AND revision = ?",
            (respondent_id, revision)).fetchone()
        if row is None:
            return None
        return {"respondent_id": respondent_id, "revision": revision,
                "answers": json.loads(row["answers_canonical"])}

    def list_answer_revisions(self, respondent_id: str | None = None) -> list[dict[str, Any]]:
        if respondent_id is None:
            rows = self.conn.execute(
                "SELECT respondent_id, revision, answers_hash, created_at "
                "FROM answer_revisions ORDER BY respondent_id, revision")
        else:
            validate_id(respondent_id, "respondent_id")
            rows = self.conn.execute(
                "SELECT respondent_id, revision, answers_hash, created_at "
                "FROM answer_revisions WHERE respondent_id = ? ORDER BY revision",
                (respondent_id,))
        return [{"respondent_id": r["respondent_id"], "revision": r["revision"],
                 "answers_hash": r["answers_hash"], "created_at": r["created_at"]}
                for r in rows]

    # ------------------------------------------------------------ scoring
    def score_answers(self, respondent_id: Any, revision: Any,
                      scale_id: Any, version: Any) -> dict[str, Any]:
        """Compute (without publishing) the score for a stored revision."""
        stored = self.get_answers(respondent_id, revision)
        if stored is None:
            raise ContractError("unknown_answer_revision",
                                "no such stored answer revision",
                                respondent_id=respondent_id, revision=revision)
        result = self.catalog.score(scale_id, version, stored["answers"])
        return self._score_dict(result, respondent_id, revision)

    def _score_dict(self, result: ScoreResult, respondent_id: str,
                    revision: int) -> dict[str, Any]:
        payload = result.to_dict()
        payload = {
            "respondent_id": respondent_id,
            "answer_revision": revision,
            **payload,
        }
        if not result.eligible:
            # Ineligible products keep no official aggregate or mean.
            payload.update({"eligible": False,
                            "weighted_sum": None,
                            "answered_weight": None,
                            "min_total": None,
                            "max_total": None,
                            "mean": None})
        return payload

    @staticmethod
    def _comparison_key(score: dict[str, Any]) -> str:
        entries = [[e["question_id"], e["min"], e["max"], e["weight"], e["reverse"],
                    e["answer"], e["contribution"],
                    e["possible_contribution_min"], e["possible_contribution_max"]]
                   for e in score["entries"]]
        return canonical_json({
            "respondent_id": score["respondent_id"],
            "answer_revision": score["answer_revision"],
            "scale_id": score["scale_id"],
            "scale_version": score["scale_version"],
            "eligible": score["eligible"],
            "ineligible_reason": score["ineligible_reason"],
            "missing_weight_limit": score["missing_weight_limit"],
            "missing_weight": score["missing_weight"],
            "answered_weight": score["answered_weight"],
            "weighted_sum": score["weighted_sum"],
            "min_total": score["min_total"],
            "max_total": score["max_total"],
            "mean": score["mean"],
            "entries": entries,
        })

    def _next_report_id(self) -> str:
        rows = self.conn.execute(
            "SELECT report_id FROM reports WHERE report_id LIKE 'SCORE_%'").fetchall()
        highest = 0
        for row in rows:
            suffix = row["report_id"][len("SCORE_"):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"SCORE_{highest + 1:06d}"

    def _build_document(self, report_id: str, scores: list[dict[str, Any]],
                        catalog_version: str | None) -> dict[str, Any]:
        doc = {
            "format": NEW_PRODUCT_FORMAT,
            "report_id": report_id,
            "scoring_contract": NEW_CONTRACT,
            "direction_source": "per-scale-entry",
            "generated_at": utc_now(),
            "catalog_version": catalog_version,
            "scores": scores,
        }
        doc["report_hash"] = content_hash({k: v for k, v in doc.items()
                                           if k != "report_hash"})
        return doc

    def publish_scores(self, selections: list[dict[str, Any]],
                       catalog_version: str | None = None,
                       report_id: str | None = None) -> dict[str, Any]:
        """Compute all requested scores and atomically publish one new product.

        Every requested score is computed first.  Only if all succeed is a
        single report written; any failure rolls back the entire publication.
        """
        if not isinstance(selections, list) or not selections:
            raise ContractError("invalid_selection", "selections must be a non-empty list")
        computed: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for selection in selections:
            if not isinstance(selection, dict):
                raise ContractError("invalid_selection", "selection must be an object")
            respondent_id = validate_id(selection.get("respondent_id"), "respondent_id")
            revision = require_int(selection.get("answer_revision"), "answer_revision",
                                   minimum=1, code="invalid_revision")
            scale_id = validate_id(selection.get("scale_id"), "scale_id")
            version = require_int(selection.get("scale_version"), "scale_version",
                                  minimum=1, code="invalid_version")
            key = (respondent_id, revision, scale_id, version)
            if key in seen_keys:
                raise ContractError("duplicate_selection",
                                    "the same respondent/scale was selected twice",
                                    selection=list(key))
            seen_keys.add(key)
            score = self.score_answers(respondent_id, revision, scale_id, version)
            computed.append(score)

        with transaction(self.conn):
            assigned_id = report_id or self._next_report_id()
            if report_id is not None:
                validate_id(report_id, "report_id")
                existing = self.conn.execute(
                    "SELECT document_json FROM reports WHERE report_id = ?",
                    (report_id,)).fetchone()
                if existing is not None:
                    existing_doc = json.loads(existing["document_json"])
                    if existing_doc.get("format") != NEW_PRODUCT_FORMAT:
                        raise ContractError(
                            "report_id_conflict",
                            "report id already holds a non-new-format product",
                            report_id=report_id, format=existing_doc.get("format"))
                    if self._documents_equivalent(existing_doc, computed):
                        existing_doc["status"] = "already_published"
                        return existing_doc
                    raise ContractError(
                        "publication_content_conflict",
                        "report id exists with a different scored result",
                        report_id=report_id)
            document = self._build_document(assigned_id, computed, catalog_version)
            self._insert_report(document, computed, source="new")
        document["status"] = "published"
        return document

    @staticmethod
    def _documents_equivalent(existing_doc: dict[str, Any],
                              computed: list[dict[str, Any]]) -> bool:
        existing_scores = existing_doc.get("scores", [])
        if len(existing_scores) != len(computed):
            return False
        existing_keys = sorted(ScoringRepository._comparison_key(s)
                               for s in existing_scores)
        new_keys = sorted(ScoringRepository._comparison_key(s) for s in computed)
        return existing_keys == new_keys

    def _insert_report(self, document: dict[str, Any], scores: list[dict[str, Any]],
                       source: str) -> None:
        self.conn.execute(
            "INSERT INTO reports(report_id, format, scoring_contract, source, "
            "catalog_version, created_at, document_json, document_hash) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (document["report_id"], document["format"], document["scoring_contract"],
             source, document.get("catalog_version"), document["generated_at"],
             canonical_json(document), document["report_hash"]))
        for score in scores:
            for ordinal, entry in enumerate(score["entries"]):
                self.conn.execute(
                    "INSERT INTO score_entries(report_id, scale_id, scale_version, ordinal, "
                    "respondent_id, answer_revision, eligible, ineligible_reason, "
                    "missing_weight, missing_weight_limit, answered_weight, weighted_sum, "
                    "min_total, max_total, mean, question_id, entry_min, entry_max, "
                    "entry_weight, entry_reverse, answer, contribution, "
                    "possible_contribution_min, possible_contribution_max) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (document["report_id"], score["scale_id"], score["scale_version"],
                     ordinal, score["respondent_id"], score["answer_revision"],
                     1 if score["eligible"] else 0, score["ineligible_reason"],
                     score["missing_weight"], score["missing_weight_limit"],
                     score["answered_weight"], score["weighted_sum"],
                     score["min_total"], score["max_total"], score["mean"],
                     entry["question_id"], entry["min"], entry["max"],
                     entry["weight"], 1 if entry["reverse"] else 0,
                     "MISSING" if entry["answer"] == "MISSING" else str(entry["answer"]),
                     entry["contribution"], entry["possible_contribution_min"],
                     entry["possible_contribution_max"]))

    # ------------------------------------------------------- history import
    def import_history_document(self, payload: Any,
                                catalog_version: str | None = None) -> dict[str, Any]:
        """Import an already delivered legacy product verbatim.

        The historical values are preserved exactly.  No new score is computed
        and the current catalog is never applied.  The original report id is
        kept and cannot later be overwritten by a new-format publish.
        """
        if not isinstance(payload, dict):
            raise ContractError("invalid_history", "history must be an object")
        if payload.get("format") != LEGACY_PRODUCT_FORMAT:
            raise ContractError("unsupported_history_format",
                                "historical import accepts published-scores/1 only")
        report_id = validate_id(payload.get("report_id"), "report_id")
        contract = payload.get("scoring_contract")
        validate_id(contract, "scoring_contract")
        scores = payload.get("scores")
        if not isinstance(scores, list):
            raise ContractError("invalid_history", "scores must be a list")
        for score in scores:
            if not isinstance(score, dict):
                raise ContractError("invalid_history", "each score must be an object")
            validate_id(score.get("respondent_id"), "respondent_id")
            validate_id(score.get("scale_id"), "scale_id")
            require_int(score.get("scale_version"), "scale_version", minimum=1)
            require_int(score.get("answer_revision"), "answer_revision", minimum=1)
            if not is_bool(score.get("eligible")):
                raise ContractError("invalid_history", "eligible must be a boolean")
        document = dict(payload)
        document["imported_at"] = utc_now()
        document["import_source"] = "legacy-file"
        digest = content_hash(document)

        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT format, document_json FROM reports WHERE report_id = ?",
                (report_id,)).fetchone()
            if existing is not None:
                if existing["format"] == NEW_PRODUCT_FORMAT:
                    raise ContractError(
                        "report_id_conflict",
                        "report id belongs to a new-format product", report_id=report_id)
                current = json.loads(existing["document_json"])
                if content_hash({k: v for k, v in current.items()
                                 if k not in ("imported_at",)}) == \
                        content_hash({k: v for k, v in document.items()
                                      if k not in ("imported_at",)}):
                    return {"status": "already_imported", "report_id": report_id,
                            "scores": len(current.get("scores", []))}
                raise ContractError("history_conflict",
                                    "same report id imported with different content",
                                    report_id=report_id)
            self.conn.execute(
                "INSERT INTO reports(report_id, format, scoring_contract, source, "
                "catalog_version, created_at, document_json, document_hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (report_id, document["format"], contract, "legacy-history",
                 catalog_version, document["imported_at"], canonical_json(document),
                 digest))
        return {"status": "imported", "report_id": report_id,
                "scores": len(scores), "scoring_contract": contract,
                "document_hash": digest}

    # --------------------------------------------------------------- reads
    def get_report(self, report_id: Any) -> dict[str, Any] | None:
        validate_id(report_id, "report_id")
        row = self.conn.execute(
            "SELECT document_json FROM reports WHERE report_id = ?",
            (report_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["document_json"])

    def list_reports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT report_id, format, scoring_contract, source, catalog_version, "
            "created_at, document_hash FROM reports ORDER BY report_id")
        return [{"report_id": r["report_id"], "format": r["format"],
                 "scoring_contract": r["scoring_contract"], "source": r["source"],
                 "catalog_version": r["catalog_version"], "created_at": r["created_at"],
                 "document_hash": r["document_hash"]} for r in rows]

    def lookup_score(self, report_id: Any, respondent_id: Any,
                     scale_id: Any, scale_version: Any,
                     answer_revision: Any) -> dict[str, Any] | None:
        """Normalized single-score lookup backed by the score_entries table."""
        validate_id(report_id, "report_id")
        validate_id(respondent_id, "respondent_id")
        validate_id(scale_id, "scale_id")
        require_int(scale_version, "scale_version", minimum=1)
        require_int(answer_revision, "answer_revision", minimum=1)
        rows = self.conn.execute(
            "SELECT * FROM score_entries WHERE report_id = ? AND respondent_id = ? "
            "AND scale_id = ? AND scale_version = ? AND answer_revision = ? "
            "ORDER BY ordinal",
            (report_id, respondent_id, scale_id, scale_version,
             answer_revision)).fetchall()
        if not rows:
            return None
        head = rows[0]
        return {
            "report_id": report_id,
            "respondent_id": head["respondent_id"],
            "answer_revision": head["answer_revision"],
            "scale_id": head["scale_id"],
            "scale_version": head["scale_version"],
            "eligible": bool(head["eligible"]),
            "ineligible_reason": head["ineligible_reason"],
            "missing_weight": head["missing_weight"],
            "missing_weight_limit": head["missing_weight_limit"],
            "answered_weight": head["answered_weight"],
            "weighted_sum": head["weighted_sum"],
            "min_total": head["min_total"],
            "max_total": head["max_total"],
            "mean": head["mean"],
            "entries": [
                {"question_id": r["question_id"], "min": r["entry_min"],
                 "max": r["entry_max"], "weight": r["entry_weight"],
                 "reverse": bool(r["entry_reverse"]),
                 "answer": r["answer"] if r["answer"] == MISSING
                 else int(r["answer"]),
                 "contribution": r["contribution"],
                 "possible_contribution_min": r["possible_contribution_min"],
                 "possible_contribution_max": r["possible_contribution_max"]}
                for r in rows
            ],
        }

    def list_scores_for_respondent(self, respondent_id: str) -> list[dict[str, Any]]:
        validate_id(respondent_id, "respondent_id")
        rows = self.conn.execute(
            "SELECT DISTINCT report_id, scale_id, scale_version, answer_revision "
            "FROM score_entries WHERE respondent_id = ? "
            "ORDER BY report_id, scale_id", (respondent_id,)).fetchall()
        return [self.lookup_score(r["report_id"], respondent_id, r["scale_id"],
                                  r["scale_version"], r["answer_revision"])
                for r in rows]

    # ------------------------------------------------------------- snapshot
    def write_snapshot(self, path: str, *, include_history: bool = True) -> dict[str, Any]:
        """Dump catalog and all reports; records a manifest in the repo.

        Unlike a score publish this is an explicit, standalone historical
        export.  It does not change any current product.
        """
        from common import atomic_write_json
        reports = []
        for summary in self.list_reports():
            if not include_history and summary["format"] != NEW_PRODUCT_FORMAT:
                continue
            reports.append(self.get_report(summary["report_id"]))
        snapshot = {
            "format": "scoring-snapshot/1",
            "generated_at": utc_now(),
            "catalog": self.catalog.exported_document(),
            "reports": reports,
        }
        digest = content_hash(snapshot)
        snapshot["snapshot_hash"] = digest
        atomic_write_json(path, snapshot)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
            (f"snapshot:{digest}", path))
        return {"path": path, "snapshot_hash": digest,
                "report_count": len(reports),
                "catalog_versions": len(snapshot["catalog"]["scales"])}
