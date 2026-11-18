"""Independent plan verifier (the ``verify-plan`` entry point).

This module is intentionally standalone: it does **not** import the solver and
does not compare a plan against the solver's chosen allocation.  Any plan that
satisfies the full contract is accepted, even if it differs from what
:mod:`gradaudit.solver` would produce.  It re-derives everything from the
supplied catalog / records / requirements:

identity fields -> candidate local course -> course categories -> group
membership -> point totals from the *records* (never the plan).

A record missing from ``assignments`` is treated as unused.  Each problem is
reported with a located reason (``code`` + ``location``) so callers can point
at the offending record / group / field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Catalog, Plan, Records, Requirements
from .errors import GradAuditError


@dataclass
class VerifyResult:
    accepted: bool
    reasons: List[dict] = field(default_factory=list)
    student_id: Optional[str] = None
    request_id: Optional[str] = None
    group_totals: Dict[str, int] = field(default_factory=dict)
    group_satisfied: Dict[str, bool] = field(default_factory=dict)
    used_records: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "identity": {"student_id": self.student_id, "request_id": self.request_id},
            "group_totals": dict(self.group_totals),
            "group_satisfied": dict(self.group_satisfied),
            "used_records": list(self.used_records),
        }


def _reason(code: str, message: str, location: Optional[str] = None, detail=None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    if detail is not None:
        r["detail"] = detail
    return r


def verify_plan(
    plan: Plan,
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    *,
    expected_student_id: Optional[str] = None,
    expected_request_id: Optional[str] = None,
    expected_catalog_revision: Optional[int] = None,
    expected_records_revision: Optional[int] = None,
    expected_requirements_revision: Optional[int] = None,
) -> VerifyResult:
    """Verify ``plan`` against explicit inputs; no solver involved.

    The ``expected_*`` arguments let a caller (e.g. the review library) demand a
    specific revision even when it also has the source document to hand.  When
    they are omitted the check uses the documents actually supplied.
    """
    reasons: List[dict] = []

    # ---- identity -----------------------------------------------------------
    if expected_student_id is not None and plan.student_id != expected_student_id:
        reasons.append(_reason(
            "identity_mismatch",
            f"plan student_id {plan.student_id!r} does not match {expected_student_id!r}",
            location="student_id",
        ))
    if plan.student_id != records.student_id:
        reasons.append(_reason(
            "identity_mismatch",
            f"plan student_id {plan.student_id!r} does not match records student {records.student_id!r}",
            location="student_id",
        ))

    if expected_request_id is not None and plan.request_id != expected_request_id:
        reasons.append(_reason(
            "identity_mismatch",
            f"plan request_id {plan.request_id!r} does not match {expected_request_id!r}",
            location="request_id",
        ))
    if plan.request_id != requirements.request_id:
        reasons.append(_reason(
            "identity_mismatch",
            f"plan request_id {plan.request_id!r} does not match requirements {requirements.request_id!r}",
            location="request_id",
        ))

    checks = [
        ("catalog_revision", plan.catalog_revision, catalog.revision),
        ("records_revision", plan.records_revision, records.revision),
        ("requirements_revision", plan.requirements_revision, requirements.revision),
    ]
    expected_map = {
        "catalog_revision": expected_catalog_revision,
        "records_revision": expected_records_revision,
        "requirements_revision": expected_requirements_revision,
    }
    for name, plan_val, doc_val in checks:
        if plan_val != doc_val:
            reasons.append(_reason(
                "revision_mismatch",
                f"plan {name} {plan_val} does not match supplied {name} {doc_val}",
                location=name,
            ))
        exp = expected_map[name]
        if exp is not None and plan_val != exp:
            reasons.append(_reason(
                "revision_mismatch",
                f"plan {name} {plan_val} does not match required {name} {exp}",
                location=name,
            ))

    # ---- assignments --------------------------------------------------------
    seen: Dict[str, int] = {}
    totals: Dict[str, int] = {g.group_id: 0 for g in requirements.groups}
    satisfied: Dict[str, bool] = {g.group_id: False for g in requirements.groups}
    used: List[str] = []

    for idx, a in enumerate(plan.assignments):
        loc = a.record_id or f"assignments[{idx}]"

        # duplicate record id within the plan (splitting / reuse)
        if a.record_id in seen:
            reasons.append(_reason(
                "duplicate_record",
                f"record {a.record_id!r} appears more than once; a record's points cannot be "
                f"split or counted in two groups",
                location=a.record_id,
            ))
            continue

        rec = records.records.get(a.record_id)
        if rec is None:
            reasons.append(_reason(
                "unknown_record",
                f"record {a.record_id!r} is not present in records revision {records.revision}",
                location=a.record_id,
            ))
            continue

        group = requirements.group(a.group_id)
        if group is None:
            reasons.append(_reason(
                "unknown_group",
                f"group {a.group_id!r} is not present in requirements revision {requirements.revision}",
                location=a.group_id,
            ))
            # still record it so totals stay honest? No: without a group we can't
            # count it. Mark the record used to detect reuse.
            seen[a.record_id] = idx
            used.append(a.record_id)
            continue

        # candidate check: the chosen course must be an explicit equivalence
        candidates = catalog.candidates(rec.external_course)
        if a.course_id not in candidates:
            reasons.append(_reason(
                "course_not_candidate",
                f"course {a.course_id!r} is not a candidate for external course "
                f"{rec.external_course!r} under catalog revision {catalog.revision} "
                f"(candidates: {list(candidates)})",
                location=a.record_id,
            ))
            seen[a.record_id] = idx
            used.append(a.record_id)
            continue

        # category check: chosen course must hit an allowed category of the group
        course = catalog.courses[a.course_id]
        hit = sorted(set(course.categories) & set(group.allowed_categories))
        if not hit:
            reasons.append(_reason(
                "category_mismatch",
                f"course {a.course_id!r} categories {sorted(course.categories)} do not "
                f"intersect group {a.group_id!r} allowed categories "
                f"{sorted(group.allowed_categories)}",
                location=a.record_id,
            ))
            seen[a.record_id] = idx
            used.append(a.record_id)
            continue

        seen[a.record_id] = idx
        used.append(a.record_id)
        # Points come from the record, never from the plan.
        totals[a.group_id] += rec.points

    # ---- group achievement --------------------------------------------------
    for g in requirements.groups:
        satisfied[g.group_id] = totals.get(g.group_id, 0) >= g.minimum_points
        if not satisfied[g.group_id]:
            reasons.append(_reason(
                "group_underfilled",
                f"group {g.group_id!r} has {totals.get(g.group_id, 0)} points from assigned "
                f"records, below minimum {g.minimum_points}",
                location=g.group_id,
                detail={"achieved": totals.get(g.group_id, 0), "minimum": g.minimum_points},
            ))

    return VerifyResult(
        accepted=not reasons,
        reasons=reasons,
        student_id=plan.student_id,
        request_id=plan.request_id,
        group_totals=totals,
        group_satisfied=satisfied,
        used_records=used,
    )


def verify_plan_document(plan_doc, catalog, records, requirements, **kwargs) -> VerifyResult:
    """Like :func:`verify_plan` but accepts a raw plan document (dict)."""
    from .model import parse_plan

    try:
        plan = parse_plan(plan_doc)
    except GradAuditError as exc:
        return VerifyResult(accepted=False, reasons=[exc.as_reason()])
    return verify_plan(plan, catalog, records, requirements, **kwargs)


__all__ = ["VerifyResult", "verify_plan", "verify_plan_document"]
