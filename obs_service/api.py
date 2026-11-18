"""Public entry points: import, semantics queries, reports, explanations.

This module delegates *all* semantics to :mod:`obs_service.semantics` and
*all* versioning/persistence to :mod:`obs_service.store`; it contains no
simplified endpoint logic of its own.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .semantics import (
    TRUE,
    UNCERTAIN,
    SemanticsError,
    format_fraction,
    parse_observation,
    parse_threshold,
    threshold_status,
)
from .store import Store


class ObservationService:
    """High-level façade over the exact semantics and the versioned store."""

    def __init__(self, db_path: str = ":memory:"):
        self.store = Store(db_path)

    def close(self) -> None:
        self.store.conn.close()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def import_dataset(self, dataset: dict) -> dict:
        """Import an ``observations.json``-shaped object.

        Every measurement record is validated (illegal / empty ranges such as
        ``LT(0)`` are rejected) *before* anything is written, and the writes run
        in one transaction.  A bad record rejects the entire dataset.
        """
        batch_id = dataset.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise SemanticsError("dataset requires a non-empty batch_id")
        measurements = dataset.get("measurements", [])
        if not isinstance(measurements, list):
            raise SemanticsError("measurements must be a list")
        project = dataset.get("threshold", {}).get("project")
        if project is None and measurements:
            project = measurements[0].get("project")
        if project is None:
            raise SemanticsError("cannot determine project for dataset")

        result = {"batch_id": batch_id, "project": project}
        thr = dataset.get("threshold")
        if thr is not None:
            result["threshold"] = self.store.register_threshold(
                thr["project"], thr["revision"], thr["value"]
            )
        result["measurements"] = self.store.import_measurements(batch_id, project, measurements)
        return result

    def import_measurements(self, batch_id: str, project: str, records: list) -> dict:
        return self.store.import_measurements(batch_id, project, records)

    def register_threshold(self, project: str, revision: int, value) -> dict:
        return self.store.register_threshold(project, revision, value)

    # ------------------------------------------------------------------
    # Semantics queries (delegated)
    # ------------------------------------------------------------------
    def normalize(self, observation: dict) -> dict:
        """Return the canonical independent representation of an observation."""
        obs = parse_observation(observation)
        if obs.is_missing:
            return {"kind": "MISSING", "range": None, "is_missing": True}
        return {
            "kind": obs.kind,
            "range": obs.range.to_dict(),
            "range_display": repr(obs.range),
            "is_missing": False,
        }

    def intersect(self, a: dict, b: dict) -> dict:
        """Intersect two observations; MISSING does not intersect, returns empty."""
        obs_a = parse_observation(a)
        obs_b = parse_observation(b)
        if obs_a.is_missing or obs_b.is_missing:
            return {"empty": True, "reason": "MISSING is not a measured range",
                    "range": None}
        result = obs_a.range.intersect(obs_b.range)
        if result is None:
            return {"empty": True, "range": None}
        return {"empty": False, "range": result.to_dict(),
                "range_display": repr(result)}

    def threshold_check(self, observation: dict, threshold) -> dict:
        """Classify ``x > T`` for one observation: true / false / uncertain."""
        obs = parse_observation(observation)
        t = threshold if isinstance(threshold, Fraction) else parse_threshold(threshold)
        if obs.is_missing:
            return {"status": "undefined", "threshold": format_fraction(t),
                    "reason": "MISSING has no values to compare"}
        status = threshold_status(obs.range, t)
        return {"status": status, "threshold": format_fraction(t),
                "threshold_revision_free": True, "range_display": repr(obs.range),
                "definitely_exceeds": status == TRUE,
                "possibly_exceeds": status in (TRUE, UNCERTAIN),
                "range": obs.range.to_dict()}

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def create_report(self, report_id: str, batch_id: str, project: str,
                      threshold_revision: int) -> dict:
        return self.store.create_report(report_id, batch_id, project, threshold_revision)

    def read_report(self, report_id: str) -> Optional[dict]:
        return self.store.read_report(report_id)

    def list_reports(self, batch_id: Optional[str] = None) -> list:
        return self.store.list_reports(batch_id)

    def revision_history(self, batch_id: str, project: str, sample_id: str) -> list:
        return self.store.revision_history(batch_id, project, sample_id)

    # ------------------------------------------------------------------
    # Explanation: trace a summary bound / attainability back to samples
    # ------------------------------------------------------------------
    def explain(self, report_id: str, sample_id: Optional[str] = None,
                focus: str = "average") -> dict:
        """Explain how a report's bound or a sample's status arises.

        Reads only the stored snapshot (never current measurements or the
        current threshold), so an old report explains itself identically after
        later imports/threshold changes and after a reopen.
        """
        report = self.store.read_report(report_id)
        if report is None:
            raise KeyError(f"no report {report_id!r}")
        contribs = report["contributions"]
        if sample_id is not None:
            match = [c for c in contribs if c["sample_id"] == sample_id]
            if not match:
                raise KeyError(f"sample {sample_id!r} not in report {report_id!r}")
            return {"report_id": report_id, "focus": "sample",
                    "threshold": report["threshold"], "sample": match[0]}

        if focus != "average":
            raise SemanticsError("focus must be 'average'")

        avg = report["average"]
        detail = []
        for c in contribs:
            if c["is_missing"]:
                detail.append({
                    "sample_id": c["sample_id"],
                    "adopted_revision": c["adopted_revision"],
                    "is_missing": True,
                    "note": "excluded: MISSING contributes to neither sum nor denominator",
                })
                continue
            detail.append({
                "sample_id": c["sample_id"],
                "adopted_revision": c["adopted_revision"],
                "range_display": c["range_display"],
                "lower_numerator": c["lower_numerator"],
                "upper_numerator": c["upper_numerator"],
                "denominator": c["denominator"],
                "lower_contribution": c["lower_contribution"],
                "upper_contribution": c["upper_contribution"],
            })

        if not avg["defined"]:
            return {"report_id": report_id, "focus": "average",
                    "average": avg, "samples": detail,
                    "note": "mean undefined (no valid samples)"}

        lower = Fraction(avg["lower"])
        denom = avg["n"]
        upper = None if avg["upper_unbounded"] else Fraction(avg["upper"])
        # Rebuild the bound as sum(contributions)/n so the reader can hand-check.
        lower_sum = sum(
            (Fraction(c["lower_contribution"]) for c in contribs if not c["is_missing"]),
            Fraction(0),
        )
        upper_sum = (
            None if avg["upper_unbounded"]
            else sum((Fraction(c["upper_contribution"]) for c in contribs if not c["is_missing"]),
                     Fraction(0))
        )
        if lower != lower_sum / denom:
            raise SemanticsError("internal: stored lower bound does not match contributions")
        if upper is not None and upper != upper_sum / denom:
            raise SemanticsError("internal: stored upper bound does not match contributions")

        return {
            "report_id": report_id,
            "focus": "average",
            "threshold": report["threshold"],
            "counts": report["counts"],
            "average": avg,
            "samples": detail,
            "derivation": {
                "denominator_n": denom,
                "lower_sum": format_fraction(lower_sum),
                "lower_sum_over_n": f"{format_fraction(lower_sum)}/{denom}",
                "upper_sum": "+inf" if upper_sum is None else format_fraction(upper_sum),
                "upper_sum_over_n": "+inf" if upper_sum is None else f"{format_fraction(upper_sum)}/{denom}",
                "lower_attainable": avg["lower_attainable"],
                "upper_attainable": avg["upper_attainable"],
                "lower_why": (
                    "lower bound is in the (closed) range" if avg["lower_attainable"]
                    else "at least one sample's lower end is open, so the sum cannot "
                         "simultaneously hit every lower endpoint; the infimum is a limit"
                ),
                "upper_why": (
                    "no sample is unbounded above and every upper end is closed, "
                    "so the supremum is a maximum" if avg["upper_attainable"]
                    else ("at least one sample is unbounded above" if avg["upper_unbounded"]
                          else "at least one sample's upper end is open, so the "
                               "supremum is only a limit")
                ),
            },
        }

    # ------------------------------------------------------------------
    # Operation runner: apply an ``operations.json``-style file
    # ------------------------------------------------------------------
    def run_operations(self, operations: dict) -> list:
        """Execute the listed operations; returns per-operation results.

        Imports that fail (illegal or conflicting) are reported with
        ``"rejected": True`` and the error; the service state is unchanged
        because the store validates and applies atomically.
        """
        results = []
        for op in operations.get("operations", []):
            kind = op.get("op")
            try:
                if kind == "report":
                    out = self.create_report(op["report_id"], op["batch_id"],
                                             op["project"], op["threshold_revision"])
                    results.append({"op": kind, "report_id": op["report_id"],
                                    "summary": out})
                elif kind == "import_measurements":
                    records = op["records"]
                    project = op.get("project")
                    if project is None and records:
                        project = records[0].get("project")
                    if project is None:
                        raise SemanticsError("import_measurements requires a project")
                    batch_id = op.get("batch_id") or _default_batch(operations, records)
                    out = self.store.import_measurements(batch_id, project, records)
                    results.append({"op": kind, "result": out})
                elif kind == "threshold":
                    out = self.store.register_threshold(op["project"], op["revision"], op["value"])
                    results.append({"op": kind, "result": out})
                elif kind == "read_report":
                    out = self.store.read_report(op["report_id"])
                    results.append({"op": kind, "report_id": op["report_id"], "report": out})
                elif kind == "explain":
                    out = self.explain(op["report_id"], op.get("sample_id"),
                                       op.get("focus", "average"))
                    results.append({"op": kind, "explanation": out})
                else:
                    raise SemanticsError(f"unknown operation {kind!r}")
            except (SemanticsError, KeyError) as exc:
                results.append({"op": kind, "rejected": True,
                                "error": str(exc),
                                "error_type": type(exc).__name__})
        return results


def _default_batch(operations: dict, records=None) -> str:
    """Imports in operations.json omit batch_id; reuse the dataset batch."""
    if "batch_id" in operations:
        return operations["batch_id"]
    # Fall back to the batch of the most recently created report target.
    for op in operations.get("operations", []):
        if op.get("op") == "report" and op.get("batch_id"):
            return op["batch_id"]
    return "b1"
