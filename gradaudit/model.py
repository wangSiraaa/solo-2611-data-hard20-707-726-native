"""Input parsing / validation for the four public JSON documents.

Public field meanings are exactly the contract:

* ``course-catalog/1``      -- courses (id + category set) and equivalences
* records (student revision) -- stable record_id, external course, positive int points
* requirements              -- at most 3 groups: allowed categories + minimum points
* ``course-allocation/1``   -- identity fields + assignments (record/course/group)

Nothing here guesses equivalences by name: only the catalog's explicit
``equivalences`` map is consulted for candidate local courses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .errors import SchemaError

MAX_GROUPS = 3
MAX_POINTS = 6
MIN_POINTS = 1


# --------------------------------------------------------------------------- #
# canonicalisation helpers
# --------------------------------------------------------------------------- #
def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _require(cond: bool, message: str, *, location: Optional[str] = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


def _as_positive_int(value: Any, *, field_name: str, location: str) -> int:
    # bool is a subclass of int -> reject explicitly so True is not read as 1.
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{field_name} must be an integer",
        location=location,
    )
    _require(value >= 1, f"{field_name} must be a positive integer", location=location)
    return value


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Course:
    course_id: str
    categories: FrozenSet[str]


@dataclass(frozen=True)
class Catalog:
    revision: int
    courses: Dict[str, Course]          # course_id -> Course
    equivalences: Dict[str, Tuple[str, ...]]  # external -> tuple(local ids)

    def candidates(self, external_course: str) -> Tuple[str, ...]:
        """Candidates come *only* from the explicit equivalence map."""
        return self.equivalences.get(external_course, ())

    def candidates_with_categories(self, external_course: str) -> List[dict]:
        out = []
        for cid in self.candidates(external_course):
            out.append({"course_id": cid, "categories": sorted(self.courses[cid].categories)})
        return out

    def raw_document(self) -> dict:
        return {
            "format": "course-catalog/1",
            "revision": self.revision,
            "courses": [
                {"course_id": c.course_id, "categories": sorted(c.categories)}
                for c in sorted(self.courses.values(), key=lambda c: c.course_id)
            ],
            "equivalences": {ext: list(cids) for ext, cids in sorted(self.equivalences.items())},
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_catalog(doc: Any) -> Catalog:
    _require(isinstance(doc, dict), "catalog must be a JSON object")
    _require(doc.get("format") == "course-catalog/1", "catalog format must be 'course-catalog/1'", location="format")
    revision = doc.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             "catalog revision must be a positive integer", location="revision")

    raw_courses = doc.get("courses")
    _require(isinstance(raw_courses, list) and raw_courses, "courses must be a non-empty list", location="courses")
    courses: Dict[str, Course] = {}
    for idx, entry in enumerate(raw_courses):
        loc = f"courses[{idx}]"
        _require(isinstance(entry, dict), "course must be an object", location=loc)
        cid = entry.get("course_id")
        _require(isinstance(cid, str) and cid.strip() != "", "course_id must be a non-empty string", location=loc)
        _require(cid not in courses, f"duplicate course_id {cid!r}", location=cid)
        cats = entry.get("categories")
        _require(isinstance(cats, list) and cats, f"categories for {cid!r} must be a non-empty list", location=cid)
        norm = set()
        for cat in cats:
            _require(isinstance(cat, str) and cat.strip() != "", "category must be a non-empty string", location=cid)
            norm.add(cat)
        courses[cid] = Course(course_id=cid, categories=frozenset(norm))

    raw_eq = doc.get("equivalences")
    _require(isinstance(raw_eq, dict), "equivalences must be an object", location="equivalences")
    equivalences: Dict[str, Tuple[str, ...]] = {}
    for ext, targets in raw_eq.items():
        _require(isinstance(ext, str) and ext.strip() != "", "external course key must be a non-empty string", location="equivalences")
        _require(isinstance(targets, list) and targets, f"equivalence {ext!r} must be a non-empty list", location=ext)
        seen: List[str] = []
        for cid in targets:
            _require(isinstance(cid, str) and cid.strip() != "", "local course id must be a non-empty string", location=ext)
            _require(cid in courses, f"equivalence {ext!r} references unknown local course {cid!r}", location=ext)
            if cid in seen:
                raise SchemaError(f"equivalence {ext!r} lists {cid!r} more than once", location=ext)
            seen.append(cid)
        equivalences[ext] = tuple(seen)

    return Catalog(revision=revision, courses=courses, equivalences=equivalences)


# --------------------------------------------------------------------------- #
# student records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Record:
    record_id: str
    external_course: str
    points: int


@dataclass(frozen=True)
class Records:
    student_id: str
    revision: int
    records: Dict[str, Record]

    def raw_document(self) -> dict:
        return {
            "student_id": self.student_id,
            "revision": self.revision,
            "records": [
                {"record_id": r.record_id, "external_course": r.external_course, "points": r.points}
                for r in sorted(self.records.values(), key=lambda r: r.record_id)
            ],
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_records(doc: Any, *, expected_student_id: Optional[str] = None) -> Records:
    _require(isinstance(doc, dict), "records document must be a JSON object")
    student_id = doc.get("student_id")
    _require(isinstance(student_id, str) and student_id.strip() != "", "student_id must be a non-empty string", location="student_id")
    if expected_student_id is not None:
        _require(student_id == expected_student_id,
                 f"records student_id {student_id!r} != expected {expected_student_id!r}", location="student_id")
    revision = doc.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             "records revision must be a positive integer", location="revision")
    raw = doc.get("records")
    _require(isinstance(raw, list), "records must be a list", location="records")
    records: Dict[str, Record] = {}
    for idx, entry in enumerate(raw):
        loc = f"records[{idx}]"
        _require(isinstance(entry, dict), "record must be an object", location=loc)
        rid = entry.get("record_id")
        _require(isinstance(rid, str) and rid.strip() != "", "record_id must be a non-empty string", location=loc)
        _require(rid not in records, f"duplicate record_id {rid!r}", location=rid)
        ext = entry.get("external_course")
        _require(isinstance(ext, str) and ext.strip() != "", f"external_course for {rid!r} must be a non-empty string", location=rid)
        pts = _as_positive_int(entry.get("points"), field_name="points", location=rid)
        records[rid] = Record(record_id=rid, external_course=ext, points=pts)
    return Records(student_id=student_id, revision=revision, records=records)


# --------------------------------------------------------------------------- #
# requirements
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Group:
    group_id: str
    allowed_categories: FrozenSet[str]
    minimum_points: int


@dataclass(frozen=True)
class Requirements:
    request_id: str
    revision: int
    groups: Tuple[Group, ...]

    def group(self, group_id: str) -> Optional[Group]:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None

    def raw_document(self) -> dict:
        return {
            "request_id": self.request_id,
            "revision": self.revision,
            "groups": [
                {
                    "group_id": g.group_id,
                    "allowed_categories": sorted(g.allowed_categories),
                    "minimum_points": g.minimum_points,
                }
                for g in self.groups
            ],
        }

    def content_hash(self) -> str:
        return content_hash(self.raw_document())


def parse_requirements(doc: Any, *, expected_request_id: Optional[str] = None) -> Requirements:
    _require(isinstance(doc, dict), "requirements document must be a JSON object")
    request_id = doc.get("request_id")
    _require(isinstance(request_id, str) and request_id.strip() != "", "request_id must be a non-empty string", location="request_id")
    if expected_request_id is not None:
        _require(request_id == expected_request_id,
                 f"requirements request_id {request_id!r} != expected {expected_request_id!r}", location="request_id")
    revision = doc.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             "requirements revision must be a positive integer", location="revision")
    raw_groups = doc.get("groups")
    _require(isinstance(raw_groups, list), "groups must be a list", location="groups")
    _require(len(raw_groups) <= MAX_GROUPS, f"at most {MAX_GROUPS} groups are supported", location="groups")
    groups: List[Group] = []
    seen_ids = set()
    for idx, entry in enumerate(raw_groups):
        loc = f"groups[{idx}]"
        _require(isinstance(entry, dict), "group must be an object", location=loc)
        gid = entry.get("group_id")
        _require(isinstance(gid, str) and gid.strip() != "", "group_id must be a non-empty string", location=loc)
        _require(gid not in seen_ids, f"duplicate group_id {gid!r}", location=gid)
        seen_ids.add(gid)
        cats = entry.get("allowed_categories")
        _require(isinstance(cats, list) and cats, f"allowed_categories for {gid!r} must be a non-empty list", location=gid)
        norm = set()
        for cat in cats:
            _require(isinstance(cat, str) and cat.strip() != "", "category must be a non-empty string", location=gid)
            norm.add(cat)
        minimum = _as_positive_int(entry.get("minimum_points"), field_name="minimum_points", location=gid)
        groups.append(Group(group_id=gid, allowed_categories=frozenset(norm), minimum_points=minimum))
    return Requirements(request_id=request_id, revision=revision, groups=tuple(groups))


# --------------------------------------------------------------------------- #
# allocation plan (course-allocation/1)
# --------------------------------------------------------------------------- #
PLAN_FORMAT = "course-allocation/1"


@dataclass(frozen=True)
class Assignment:
    record_id: str
    course_id: str
    group_id: str


@dataclass(frozen=True)
class Plan:
    student_id: str
    catalog_revision: int
    records_revision: int
    request_id: str
    requirements_revision: int
    assignments: Tuple[Assignment, ...]
    raw: dict = field(default_factory=dict, compare=False)

    def as_document(self) -> dict:
        return {
            "format": PLAN_FORMAT,
            "student_id": self.student_id,
            "catalog_revision": self.catalog_revision,
            "records_revision": self.records_revision,
            "request_id": self.request_id,
            "requirements_revision": self.requirements_revision,
            "assignments": [
                {"record_id": a.record_id, "course_id": a.course_id, "group_id": a.group_id}
                for a in self.assignments
            ],
        }


def parse_plan(doc: Any) -> Plan:
    _require(isinstance(doc, dict), "plan must be a JSON object")
    _require(doc.get("format") == PLAN_FORMAT, f"plan format must be {PLAN_FORMAT!r}", location="format")
    for key in ("student_id", "request_id"):
        val = doc.get(key)
        _require(isinstance(val, str) and val.strip() != "", f"{key} must be a non-empty string", location=key)
    for key in ("catalog_revision", "records_revision", "requirements_revision"):
        val = doc.get(key)
        _require(isinstance(val, int) and not isinstance(val, bool) and val >= 1,
                 f"{key} must be a positive integer", location=key)

    raw_assign = doc.get("assignments", [])
    _require(isinstance(raw_assign, list), "assignments must be a list", location="assignments")
    assignments: List[Assignment] = []
    for idx, entry in enumerate(raw_assign):
        loc = f"assignments[{idx}]"
        _require(isinstance(entry, dict), "assignment must be an object", location=loc)
        rid = entry.get("record_id")
        cid = entry.get("course_id")
        gid = entry.get("group_id")
        for name, val in (("record_id", rid), ("course_id", cid), ("group_id", gid)):
            _require(isinstance(val, str) and val.strip() != "", f"{name} must be a non-empty string", location=loc)
        # Duplicate record ids are a semantic violation reported by the
        # independent verifier as a located ``duplicate_record`` reason; parsing
        # stays structural so the verifier (not the parser) owns that rule.
        # points are ignored on purpose: they MUST come from the record, never the plan.
        assignments.append(Assignment(record_id=rid, course_id=cid, group_id=gid))

    return Plan(
        student_id=doc["student_id"],
        catalog_revision=doc["catalog_revision"],
        records_revision=doc["records_revision"],
        request_id=doc["request_id"],
        requirements_revision=doc["requirements_revision"],
        assignments=tuple(assignments),
        raw=doc,
    )
