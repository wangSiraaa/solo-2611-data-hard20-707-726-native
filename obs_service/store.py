"""Versioned registry of observations, thresholds, and immutable reports.

Design decisions (also documented in ``NOTES.md``):

* **Measurements are append-only.**  A row is keyed by
  ``(batch_id, project, sample_id, revision)``.  Re-registering the exact same
  content is a *replay* (accepted, no change); registering different content
  under an existing revision is a **conflict**.  A batch import is validated
  as a whole and written in one transaction: any illegal record or conflict
  rejects the entire import, so no sample is partially updated.
* **Adoption** is "highest revision per (sample, project)"; MISSING with a
  higher revision means "this sample is missing for this report" while the
  revision history is preserved.  MISSING is never equal to a measured range.
* **Thresholds have explicit revisions** and are registered, not overwritten.
* **Reports are immutable snapshots.**  Report creation resolves the adopted
  revisions and threshold *once* through :mod:`obs_service.semantics`, then
  materialises the exact ranges, per-sample contributions and counts into
  ``reports`` / ``report_contributions``.  Reading an old report never
  re-reads current measurements or the current threshold, and never re-runs
  the predicate against a newer threshold.  ``superseded`` is a convenience
  marker only; it never alters a stored report.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from fractions import Fraction
from typing import Optional

from .semantics import (
    FALSE,
    Observation,
    POS_INF,
    Range,
    RevConflict,
    SemanticsError,
    TRUE,
    UNCERTAIN,
    average_of_ranges,
    format_fraction,
    parse_observation,
    parse_threshold,
    threshold_status,
)


def _canon(obs: Observation) -> str:
    return json.dumps(obs.canonical(), sort_keys=True, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS measurements (
    batch_id      TEXT NOT NULL,
    project       TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL CHECK (revision > 0),
    raw_json      TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (batch_id, project, sample_id, revision)
);
CREATE TABLE IF NOT EXISTS threshold_revisions (
    project      TEXT NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision > 0),
    value        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (project, revision)
);
CREATE TABLE IF NOT EXISTS reports (
    report_id                   TEXT PRIMARY KEY,
    batch_id                    TEXT NOT NULL,
    project                     TEXT NOT NULL,
    threshold_revision          INTEGER NOT NULL,
    threshold_value             TEXT NOT NULL,
    measurement_max_revision    INTEGER NOT NULL,
    is_missing_semantics        TEXT NOT NULL,
    superseded                  INTEGER NOT NULL DEFAULT 0,
    created_seq                 INTEGER NOT NULL,
    data_json                   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_contributions (
    report_id            TEXT NOT NULL,
    sample_id            TEXT NOT NULL,
    adopted_revision     INTEGER NOT NULL,
    is_missing           INTEGER NOT NULL,
    observation_json     TEXT NOT NULL,
    range_json           TEXT NOT NULL,
    range_display        TEXT,
    status               TEXT,
    denominator          INTEGER,
    lower_numerator      TEXT,
    upper_numerator      TEXT,
    lower_contribution   TEXT,
    upper_contribution   TEXT,
    lower_attainable     INTEGER,
    upper_attainable     INTEGER,
    explanation          TEXT NOT NULL,
    PRIMARY KEY (report_id, sample_id),
    FOREIGN KEY (report_id) REFERENCES reports(report_id)
);
"""


class Store:
    """SQLite-backed observation/report store with exact semantics."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Batches and observation import
    # ------------------------------------------------------------------
    def ensure_batch(self, batch_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO batches(batch_id) VALUES (?)", (batch_id,)
        )

    def import_measurements(self, batch_id: str, project: str, records: list) -> dict:
        """Validate and apply a batch of measurement records atomically.

        Raises :class:`SemanticsError` / :class:`RevConflict` for any problem;
        in that case nothing is written.  Returns a summary with counts of
        inserted / replayed records.
        """
        if not records:
            raise SemanticsError("empty import")

        # --- Phase 1: validate everything in memory first ---------------
        prepared = []          # (sample_id, revision, raw_json, canonical, hash)
        seen = {}              # (sample_id, revision) -> content_hash
        for rec in records:
            if not isinstance(rec, dict):
                raise SemanticsError("record must be an object")
            sample_id = rec.get("sample_id")
            rev = rec.get("revision")
            rec_project = rec.get("project", project)
            if not isinstance(sample_id, str) or not sample_id:
                raise SemanticsError("record requires a non-empty sample_id")
            if not isinstance(rev, int) or isinstance(rev, bool) or rev <= 0:
                raise SemanticsError("revision must be a positive integer")
            if rec_project != project:
                raise SemanticsError(
                    f"record project {rec_project!r} does not match import project {project!r}"
                )
            if "observation" not in rec:
                raise SemanticsError("record requires an observation")
            # Semantics service parses and rejects empty ranges (e.g. LT(0)).
            obs = parse_observation(rec["observation"])
            canonical = _canon(obs)
            h = _hash(canonical)
            key = (sample_id, rev)
            if key in seen:
                if seen[key] != h:
                    raise RevConflict(
                        f"conflicting duplicate revision {rev} for sample {sample_id!r} in one import"
                    )
                # identical duplicate within the payload: keep one
                continue
            seen[key] = h
            prepared.append((sample_id, rev, json.dumps(rec["observation"], sort_keys=True), canonical, h))

        # --- Phase 2: existing-row conflict checks ----------------------
        for sample_id, rev, _raw, canonical, h in prepared:
            row = self.conn.execute(
                "SELECT content_hash FROM measurements WHERE batch_id=? AND project=? AND sample_id=? AND revision=?",
                (batch_id, project, sample_id, rev),
            ).fetchone()
            if row is not None and row["content_hash"] != h:
                raise RevConflict(
                    f"sample {sample_id!r} revision {rev} already registered with different semantics"
                )

        # --- Phase 3: write inside one transaction ----------------------
        inserted = replayed = 0
        try:
            with self.conn:  # commits or rolls back
                self.ensure_batch(batch_id)
                for sample_id, rev, raw, canonical, h in prepared:
                    cur = self.conn.execute(
                        """INSERT OR IGNORE INTO measurements
                           (batch_id, project, sample_id, revision, raw_json, canonical_json, content_hash)
                           VALUES (?,?,?,?,?,?,?)""",
                        (batch_id, project, sample_id, rev, raw, canonical, h),
                    )
                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        replayed += 1
        except Exception:
            raise
        return {"batch_id": batch_id, "project": project,
                "inserted": inserted, "replayed": replayed}

    def register_threshold(self, project: str, revision: int, value) -> dict:
        """Register (or replay) a threshold revision.  Revisions are immutable."""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise SemanticsError("threshold revision must be a positive integer")
        parsed = parse_threshold(value, revision=revision)
        canonical = str(parsed)
        h = _hash(canonical)
        row = self.conn.execute(
            "SELECT content_hash FROM threshold_revisions WHERE project=? AND revision=?",
            (project, revision),
        ).fetchone()
        if row is not None:
            if row["content_hash"] != h:
                raise RevConflict(
                    f"threshold {project!r} revision {revision} already has a different value"
                )
            return {"project": project, "revision": revision,
                    "value": canonical, "replayed": True}
        with self.conn:
            self.conn.execute(
                "INSERT INTO threshold_revisions(project, revision, value, content_hash) VALUES (?,?,?,?)",
                (project, revision, canonical, h),
            )
        return {"project": project, "revision": revision,
                "value": canonical, "replayed": False}

    # ------------------------------------------------------------------
    # Read-side helpers
    # ------------------------------------------------------------------
    def get_threshold(self, project: str, revision: int) -> Optional[Fraction]:
        row = self.conn.execute(
            "SELECT value FROM threshold_revisions WHERE project=? AND revision=?",
            (project, revision),
        ).fetchone()
        return None if row is None else Fraction(row["value"])

    def adopted_revisions(self, batch_id: str, project: str) -> list:
        """One row per sample: the highest registered revision."""
        rows = self.conn.execute(
            """SELECT m.sample_id, m.revision, m.raw_json, m.canonical_json
               FROM measurements m
               JOIN (SELECT sample_id, MAX(revision) AS maxrev
                     FROM measurements WHERE batch_id=? AND project=?
                     GROUP BY sample_id) x
                 ON m.sample_id = x.sample_id AND m.revision = x.maxrev
               WHERE m.batch_id=? AND m.project=?
               ORDER BY m.sample_id""",
            (batch_id, project, batch_id, project),
        ).fetchall()
        return [dict(r) for r in rows]

    def measurement_max_revision(self, batch_id: str, project: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS m FROM measurements WHERE batch_id=? AND project=?",
            (batch_id, project),
        ).fetchone()
        return int(row["m"])

    def revision_history(self, batch_id: str, project: str, sample_id: str) -> list:
        rows = self.conn.execute(
            """SELECT revision, raw_json, canonical_json FROM measurements
               WHERE batch_id=? AND project=? AND sample_id=? ORDER BY revision""",
            (batch_id, project, sample_id),
        ).fetchall()
        return [{"revision": r["revision"],
                 "observation": json.loads(r["raw_json"])} for r in rows]

    # ------------------------------------------------------------------
    # Report creation (snapshot)
    # ------------------------------------------------------------------
    def create_report(self, report_id: str, batch_id: str, project: str,
                      threshold_revision: int) -> dict:
        if self.conn.execute("SELECT 1 FROM reports WHERE report_id=?", (report_id,)).fetchone():
            raise SemanticsError(f"report {report_id!r} already exists (reports are immutable)")

        threshold = self.get_threshold(project, threshold_revision)
        if threshold is None:
            raise SemanticsError(
                f"threshold {project!r} rev {threshold_revision} not registered"
            )

        adopted = self.adopted_revisions(batch_id, project)
        if not adopted:
            raise SemanticsError(
                f"batch {batch_id!r} has no measurements for project {project!r}"
            )

        contributions = []
        valid_ranges = []
        definite = possible = missing = 0
        for row in adopted:
            obs = parse_observation(json.loads(row["raw_json"]))
            if obs.is_missing:
                missing += 1
                contributions.append({
                    "sample_id": row["sample_id"],
                    "adopted_revision": row["revision"],
                    "is_missing": True,
                    "observation": json.loads(row["raw_json"]),
                    "range": None,
                    "range_display": None,
                    "status": None,
                    "denominator": None,
                    "lower_numerator": None,
                    "upper_numerator": None,
                    "lower_contribution": None,
                    "upper_contribution": None,
                    "lower_attainable": None,
                    "upper_attainable": None,
                    "explanation": (
                        "MISSING: no admissible value; excluded from the denominator "
                        "and from both exceedance counts."
                    ),
                })
                continue

            rng = obs.range
            valid_ranges.append(rng)
            status = threshold_status(rng, threshold)
            if status == TRUE:
                definite += 1
                possible += 1
            elif status == UNCERTAIN:
                possible += 1

            lo_num = rng.lo if rng.lo is not None else Fraction(0)
            hi_num = rng.hi  # may be None
            lo_attr = rng.lo_closed
            hi_attr = False if hi_num is None else rng.hi_closed
            contributions.append({
                "sample_id": row["sample_id"],
                "adopted_revision": row["revision"],
                "is_missing": False,
                "observation": json.loads(row["raw_json"]),
                "range": rng.to_dict(),
                "range_display": repr(rng),
                "status": status,
                "denominator": None,  # filled after n is known
                "lower_numerator": format_fraction(lo_num),
                "upper_numerator": POS_INF if hi_num is None else format_fraction(hi_num),
                "lower_contribution": format_fraction(lo_num),
                "upper_contribution": POS_INF if hi_num is None else format_fraction(hi_num),
                "lower_attainable": lo_attr,
                "upper_attainable": hi_attr,
                "explanation": self._explain(rng, status, threshold),
            })

        valid = len(valid_ranges)
        avg = average_of_ranges(valid_ranges)

        # A non-exceedance that is *possible* is "uncertain"; the report's
        # "possibly exceeds" count includes the definite ones by definition.
        uncertain = possible - definite

        for c in contributions:
            if not c["is_missing"]:
                c["denominator"] = valid

        summary = {
            "report_id": report_id,
            "batch_id": batch_id,
            "project": project,
            "threshold": {"revision": threshold_revision,
                          "value": format_fraction(threshold)},
            "counts": {
                "total_samples": len(adopted),
                "valid_samples": valid,
                "definitely_exceeds": definite,
                "possibly_exceeds": possible,
                "uncertain": uncertain,
                "missing": missing,
            },
            "average": self._average_summary(avg),
            "measurement_max_revision": self.measurement_max_revision(batch_id, project),
            "missing_semantics": "MISSING is no observation and is never a measured range.",
        }

        with self.conn:
            # Convenience marker only; stored reports are never modified otherwise.
            self.conn.execute(
                "UPDATE reports SET superseded=1 WHERE batch_id=? AND project=? AND superseded=0",
                (batch_id, project),
            )
            seq = self.conn.execute("SELECT COALESCE(MAX(created_seq),0)+1 AS s FROM reports").fetchone()["s"]
            self.conn.execute(
                """INSERT INTO reports
                   (report_id, batch_id, project, threshold_revision, threshold_value,
                    measurement_max_revision, is_missing_semantics, superseded, created_seq, data_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (report_id, batch_id, project, threshold_revision, str(threshold),
                 summary["measurement_max_revision"], summary["missing_semantics"],
                 0, seq, json.dumps(summary, sort_keys=True)),
            )
            for c in contributions:
                self.conn.execute(
                    """INSERT INTO report_contributions
                       (report_id, sample_id, adopted_revision, is_missing, observation_json,
                        range_json, range_display, status, denominator, lower_numerator,
                        upper_numerator, lower_contribution, upper_contribution,
                        lower_attainable, upper_attainable, explanation)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (report_id, c["sample_id"], c["adopted_revision"], int(c["is_missing"]),
                     json.dumps(c["observation"], sort_keys=True),
                     json.dumps(c["range"], sort_keys=True) if c["range"] else "",
                     c["range_display"], c["status"], c["denominator"],
                     c["lower_numerator"], c["upper_numerator"],
                     c["lower_contribution"], c["upper_contribution"],
                     None if c["lower_attainable"] is None else int(c["lower_attainable"]),
                     None if c["upper_attainable"] is None else int(c["upper_attainable"]),
                     c["explanation"]),
                )
        return self.read_report(report_id)

    @staticmethod
    def _explain(rng: Range, status: str, threshold: Fraction) -> str:
        t = format_fraction(threshold)
        if status == TRUE:
            return (f"Every admissible value in {rng!r} is > {t}; "
                    "the sample is definitely over threshold.")
        if status == FALSE:
            return (f"No admissible value in {rng!r} is > {t} "
                    f"(the range stays within [0, {t}]); the sample is not over.")
        return (f"{rng!r} straddles {t}: values both > {t} and <= {t} are admissible, "
                "so exceedance is uncertain (not a decision, no probability assumed).")

    @staticmethod
    def _average_summary(avg: Optional[dict]) -> dict:
        if avg is None:
            return {
                "defined": False,
                "n": 0,
                "reason": "no non-missing observation: arithmetic mean is undefined",
            }
        lo = avg["lo"]
        hi = avg["hi"]
        lower_display = format_fraction(lo)
        upper_display = "+inf" if hi is None else format_fraction(hi)
        left = "[" if avg["lo_closed"] else "("
        right = "]" if (hi is not None and avg["hi_closed"]) else ")"
        return {
            "defined": True,
            "n": avg["n"],
            "lower": format_fraction(lo),
            "lower_closed": avg["lo_closed"],
            "upper": None if hi is None else format_fraction(hi),
            "upper_closed": False if hi is None else avg["hi_closed"],
            "upper_unbounded": avg["upper_unbounded"],
            "lower_attainable": avg["lower_attainable"],
            "upper_attainable": avg["upper_attainable"],
            "range_display": f"{left}{lower_display},{upper_display}{right}",
            "unique": (hi is not None and lo == hi),
        }

    # ------------------------------------------------------------------
    # Report reads
    # ------------------------------------------------------------------
    def list_reports(self, batch_id: Optional[str] = None) -> list:
        if batch_id is None:
            rows = self.conn.execute(
                "SELECT report_id, batch_id, project, threshold_revision, superseded, created_seq "
                "FROM reports ORDER BY created_seq"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT report_id, batch_id, project, threshold_revision, superseded, created_seq "
                "FROM reports WHERE batch_id=? ORDER BY created_seq", (batch_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def read_report(self, report_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            return None
        summary = json.loads(row["data_json"])
        contribs = self.conn.execute(
            "SELECT * FROM report_contributions WHERE report_id=? ORDER BY sample_id",
            (report_id,),
        ).fetchall()
        summary["superseded"] = bool(row["superseded"])
        summary["contributions"] = [self._contrib_row(c) for c in contribs]
        return summary

    @staticmethod
    def _contrib_row(c: sqlite3.Row) -> dict:
        is_missing = bool(c["is_missing"])
        return {
            "sample_id": c["sample_id"],
            "adopted_revision": c["adopted_revision"],
            "is_missing": is_missing,
            "observation": json.loads(c["observation_json"]),
            "range": json.loads(c["range_json"]) if c["range_json"] else None,
            "range_display": c["range_display"],
            "status": c["status"],
            "denominator": c["denominator"],
            "lower_numerator": c["lower_numerator"],
            "upper_numerator": c["upper_numerator"],
            "lower_contribution": c["lower_contribution"],
            "upper_contribution": c["upper_contribution"],
            "lower_attainable": None if c["lower_attainable"] is None else bool(c["lower_attainable"]),
            "upper_attainable": None if c["upper_attainable"] is None else bool(c["upper_attainable"]),
            "explanation": c["explanation"],
        }
