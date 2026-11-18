"""Exact solver for the graduation requirement audit problem.

Model
-----
Each transfer ``record`` may be *used at most once*.  When used it is:

* assigned exactly one candidate local course (from the catalog equivalence
  map -- never guessed from names), and
* fully assigned to exactly one requirement group (points are never split).

A used record counts its *full* point value toward its group only if the
chosen local course shares at least one category with the group's allowed
categories.  Groups may be over-satisfied; plans need not be minimal nor
unique.  Records may be left unused.

Search state (up to 3 groups)
-----------------------------
``state = (pointer, gains)`` where

* ``pointer`` -- index of the next record to decide on (records fixed in id
  order; each is used once or skipped).  Every record before ``pointer`` has
  already been decided, so a separate "used mask" is unnecessary: which
  records were consumed is implied by the choices on the path.
* ``gains``  -- points per group, *capped at that group's minimum*.  Capping
  is safe because meeting the minimum is all that matters, and it collapses
  states: two partial allocations that already satisfy the same set of groups
  become identical.

Cost
----
The number of distinct gain vectors is at most ``prod_g(min_g + 1)``; with
``G <= 3`` groups and ``min_g <= 6`` that is <= 343, so there are at most
``n * 343`` states for ``n <= 12`` records (~4k).  Each state branches over
its remaining candidate courses and groups, i.e.
``O(n * prod(min+1) * (1 + sum_c |cand_c|) * G)``.  Multi-candidate records
add *transitions*, not states; point variety only widens the capped gain
grid.  This is an exact DP, not a heuristic -- it proves infeasibility too.

The solver returns *a* feasible allocation, not the minimal or unique one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import SchemaError
from .model import Assignment, Catalog, Plan, Record, Records, Requirements

# Guard rails so an input outside the documented contract cannot hang us.
MAX_RECORDS = 12
MAX_CANDIDATES = 3
MAX_POINTS = 6


@dataclass
class GroupOutcome:
    group_id: str
    minimum_points: int
    max_ignoring_other_groups: int
    satisfied: bool


@dataclass
class MasterResult:
    feasible: bool
    student_id: str
    request_id: str
    catalog_revision: int
    records_revision: int
    requirements_revision: int
    assignments: List[Assignment] = field(default_factory=list)
    group_totals: Dict[str, int] = field(default_factory=dict)
    groups: List[GroupOutcome] = field(default_factory=list)
    states_explored: int = 0
    elapsed_ms: float = 0.0
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "identity": {
                "student_id": self.student_id,
                "request_id": self.request_id,
                "catalog_revision": self.catalog_revision,
                "records_revision": self.records_revision,
                "requirements_revision": self.requirements_revision,
            },
            "assignments": [
                {"record_id": a.record_id, "course_id": a.course_id, "group_id": a.group_id}
                for a in self.assignments
            ],
            "group_totals": dict(self.group_totals),
            "groups": [
                {
                    "group_id": g.group_id,
                    "minimum_points": g.minimum_points,
                    "max_ignoring_other_groups": g.max_ignoring_other_groups,
                    "satisfied": g.satisfied,
                }
                for g in self.groups
            ],
            "search": {
                "states_explored": self.states_explored,
                "elapsed_ms": round(self.elapsed_ms, 3),
            },
            "note": self.note,
        }

    def as_plan(self) -> Plan:
        return Plan(
            student_id=self.student_id,
            catalog_revision=self.catalog_revision,
            records_revision=self.records_revision,
            request_id=self.request_id,
            requirements_revision=self.requirements_revision,
            assignments=tuple(self.assignments),
        )

    def as_plan_document(self) -> dict:
        return self.as_plan().as_document()


def _validate_scale(records: Records) -> None:
    if len(records.records) > MAX_RECORDS:
        raise SchemaError(
            f"at most {MAX_RECORDS} records are supported (got {len(records.records)})",
            location="records",
        )
    for rec in records.records.values():
        if not (1 <= rec.points <= MAX_POINTS):
            raise SchemaError(
                f"points must be in 1..{MAX_POINTS} (record {rec.record_id} has {rec.points})",
                location=rec.record_id,
            )


def single_group_max(
    catalog: Catalog,
    records: Records,
    group_id: str,
    allowed_categories: Sequence[str],
    minimum_points: int,
) -> GroupOutcome:
    """Max points a group can gather if it ignores every competing group.

    Each record contributes its full point value when it has *at least one*
    candidate local course in an allowed category; the course choice does not
    consume the record for another group in this relaxation.  Records with no
    matching candidate contribute nothing.  The reported number is the true
    total (not capped at the minimum): e.g. under catalog revision 2 the
    CORE-only relaxation reaches 8 points while OPT reaches only 2.
    """
    allowed = set(allowed_categories)
    total = 0
    for rec in records.records.values():
        cands = catalog.candidates(rec.external_course)
        if any(allowed & set(catalog.courses[cid].categories) for cid in cands):
            total += rec.points
    return GroupOutcome(
        group_id=group_id,
        minimum_points=minimum_points,
        max_ignoring_other_groups=total,
        satisfied=total >= minimum_points,
    )


def solve(
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    *,
    student_id: Optional[str] = None,
) -> MasterResult:
    started = time.perf_counter()
    _validate_scale(records)
    sid = student_id if student_id is not None else records.student_id

    groups = requirements.groups
    group_ids = [g.group_id for g in groups]
    n_groups = len(groups)
    minima = [g.minimum_points for g in groups]
    allowed = [set(g.allowed_categories) for g in groups]

    recs: List[Record] = sorted(records.records.values(), key=lambda r: r.record_id)
    n = len(recs)

    # Per record and group: candidate course ids that would count for that group.
    # value_options[i][g] is a tuple of course ids; empty => record cannot feed g.
    value_options: List[List[Tuple[str, ...]]] = []
    for rec in recs:
        cands = catalog.candidates(rec.external_course)
        per_group: List[Tuple[str, ...]] = []
        for g in range(n_groups):
            usable = tuple(
                cid for cid in cands if allowed[g] & set(catalog.courses[cid].categories)
            )
            per_group.append(usable)
        value_options.append(per_group)

    per_group = [
        single_group_max(
            catalog, records, group_ids[g], groups[g].allowed_categories, minima[g]
        )
        for g in range(n_groups)
    ]

    identity = dict(
        student_id=sid,
        request_id=requirements.request_id,
        catalog_revision=catalog.revision,
        records_revision=records.revision,
        requirements_revision=requirements.revision,
    )

    if n_groups == 0:
        return MasterResult(
            feasible=True, assignments=[], group_totals={}, groups=[],
            states_explored=0, elapsed_ms=(time.perf_counter() - started) * 1000, **identity,
        )

    # DP over (pointer, gains).  ``parents`` maps a state to (parent_state, action)
    # where action is None (skip) or (record_index, course_id, group_index).
    start = (0, tuple(0 for _ in range(n_groups)))
    # layer: gains -> parent tuple (None for the start state)
    layer: Dict[Tuple[int, ...], Optional[Tuple[Tuple, Optional[Tuple]]]] = {start[1]: None}
    parents: Dict[Tuple, Optional[Tuple[Tuple, Optional[Tuple]]]] = {}
    success_gains: Optional[Tuple[int, ...]] = None
    states_explored = 1

    for pointer in range(n):
        next_layer: Dict[Tuple[int, ...], Tuple[Tuple[int, ...], Optional[Tuple]]] = {}
        for gains in layer:
            # Skip the record (unused is legal).
            if gains not in next_layer:
                next_layer[gains] = (gains, None)
            pts = recs[pointer].points
            for g in range(n_groups):
                options = value_options[pointer][g]
                if not options:
                    continue
                new_gain = min(gains[g] + pts, minima[g])
                if new_gain == gains[g]:
                    new_gains = gains  # already saturated, but still consumes record
                else:
                    lst = list(gains)
                    lst[g] = new_gain
                    new_gains = tuple(lst)
                for cid in options:
                    if new_gains not in next_layer:
                        next_layer[new_gains] = (gains, (pointer, cid, g))
        # record parent links from this layer to the next
        for new_gains, (prev_gains, action) in next_layer.items():
            parents[(pointer + 1, new_gains)] = ((pointer, prev_gains), action)
        layer = {ng: None for ng in next_layer}
        states_explored += len(layer)
        if not layer:
            break

    # Did we reach a fully-satisfied gains vector at pointer == n?
    final_pointer = n
    satisfied = [g for g in layer if all(g[i] >= minima[i] for i in range(n_groups))]
    if satisfied:
        success_gains = min(satisfied, key=lambda gv: sum(gv))  # deterministic pick
        # Reconstruct.
        allocations: List[Assignment] = []
        cur = (final_pointer, success_gains)
        while cur[0] > 0:
            link = parents.get(cur)
            if link is None:
                break
            prev_state, action = link
            if action is not None:
                pointer, cid, g = action
                rec = recs[pointer]
                allocations.append(
                    Assignment(record_id=rec.record_id, course_id=cid, group_id=group_ids[g])
                )
            cur = prev_state
        allocations.reverse()

        totals: Dict[str, int] = {gid: 0 for gid in group_ids}
        for a in allocations:
            rec = records.records[a.record_id]
            totals[a.group_id] = totals.get(a.group_id, 0) + rec.points

        return MasterResult(
            feasible=True,
            assignments=allocations,
            group_totals=totals,
            groups=[
                GroupOutcome(
                    group_id=group_ids[g],
                    minimum_points=minima[g],
                    max_ignoring_other_groups=per_group[g].max_ignoring_other_groups,
                    satisfied=totals.get(group_ids[g], 0) >= minima[g],
                )
                for g in range(n_groups)
            ],
            states_explored=states_explored,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            note=None,
            **identity,
        )

    # Infeasible.  Report per-group independent maxima to make the "each group
    # is reachable alone, yet not together" situation legible.
    return MasterResult(
        feasible=False,
        assignments=[],
        group_totals={gid: 0 for gid in group_ids},
        groups=per_group,
        states_explored=states_explored,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        note=(
            "no allocation satisfies all groups. groups[].max_ignoring_other_groups shows "
            "each group's best reachable total in isolation; a group reaching its minimum "
            "alone does not make the request feasible because a single record cannot be "
            "split or counted in two groups."
        ),
        **identity,
    )
