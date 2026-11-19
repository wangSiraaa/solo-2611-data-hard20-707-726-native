"""Cross-section calibration: choose one read per point and one common shift.

Problem
-------
A request fixes read versions (each with a PRIMARY/BACKUP role), a reference
position and non-negative deviation ``epsilon`` per control point, an upper
bound on how many selected BACKUP reads may be used, and a closed interval in
which a single whole-group shift ``s`` must live.

For a point ``p`` with reference ``ref`` and candidate read position ``pos``
the constraint ``|pos + s - ref| <= epsilon`` is exactly the closed interval

    s in [ref - epsilon - pos,  ref + epsilon - pos].

A selection is feasible when every point is covered by exactly one chosen
candidate and the number of chosen BACKUP candidates is within budget.

Why not enumerate selections
----------------------------
With 20 points and 6 candidates each there are 6^20 selections.  Instead we
sweep the *candidate interval endpoints*.  Coverage of a point is constant
between consecutive endpoints, and the minimum number of backups needed at a
shift ``s`` is simply

    #{points that are covered at s but have no PRIMARY covering s}

because each such point can be served by any one backup independently.  The
whole problem therefore reduces to a linear number of coverage evaluations.

Closed endpoints and isolated feasible points
---------------------------------------------
Coverage is closed, so if a candidate covers an *open* elementary segment it
also covers both endpoints of that segment; hence a feasible open segment can
safely be published as a closed interval.  The converse is not true: a single
event point can be feasible while both neighbouring open segments are not
(e.g. ``[0,1]`` for one point and ``[1,2]`` for another).  We therefore also
evaluate every event point on its own and emit a zero-width interval when it is
feasible.  The final feasible set is an ordered union of disjoint closed
intervals, exactly as required -- never a heuristic envelope.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from .catalog import Catalog, NotFoundError
from .rationals import (
    Interval,
    RationalError,
    contains,
    fmt,
    normalize,
    parse_interval,
    parse_rational,
)
from .store import Store


class CalibrationError(Exception):
    """Base class for calibration failures."""


class CalibrationInputError(CalibrationError):
    """The request references unknown points, missing reads or malformed data.

    Raised *before* any analysis; no calibration product is written.
    """


class InfeasibleError(CalibrationError):
    """The request is well-formed but no selection + shift satisfies it.

    Carries ``reasons`` (human readable) and ``detail`` (structured evidence,
    including each point's achievable shift windows).  No product is written.
    """

    def __init__(self, reasons: list[str], detail: Optional[dict] = None):
        self.reasons = reasons
        self.detail = detail or {}
        super().__init__("; ".join(reasons))


ROLES = ("PRIMARY", "BACKUP")


@dataclass(frozen=True)
class FixedRead:
    read_id: str
    revision: int
    role: str


@dataclass(frozen=True)
class Request:
    request_id: str
    reads: tuple[FixedRead, ...]
    backup_limit: int
    shift: Interval
    references: tuple[tuple[str, Fraction, Fraction], ...]  # (point, ref, eps)

    def fingerprint(self) -> str:
        """Canonical content hash of the request, independent of the catalog.

        Uses only the submitted identity/content, so later contract or read
        revisions can never change it.
        """
        payload = {
            "request_id": self.request_id,
            "backup_limit": self.backup_limit,
            "shift": [fmt(self.shift[0]), fmt(self.shift[1])],
            "reads": sorted(
                [r.read_id, r.revision, r.role] for r in self.reads
            ),
            "references": sorted(
                [point, fmt(ref), fmt(eps)] for point, ref, eps in self.references
            ),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def points(self) -> list[str]:
        return [p for p, _, _ in self.references]

    @property
    def reference_map(self) -> dict[str, tuple[Fraction, Fraction]]:
        return {p: (ref, eps) for p, ref, eps in self.references}


@dataclass(frozen=True)
class Candidate:
    read_id: str
    revision: int
    role: str
    point: str
    position: Fraction
    code: int
    scale_id: str
    scale_revision: int
    # shift window clipped to the request's allowed shift interval
    window: Optional[Interval]
    # raw shift window before clipping (for diagnostics)
    raw_window: Interval

    def covers(self, s: Fraction) -> bool:
        return self.window is not None and self.window[0] <= s <= self.window[1]

    def sort_key(self):
        return (0 if self.role == "PRIMARY" else 1, self.read_id, self.revision)

    @property
    def source(self) -> dict:
        return {
            "scale_id": self.scale_id,
            "scale_revision": self.scale_revision,
            "code": self.code,
        }


def parse_request(raw: dict) -> Request:
    """Validate and normalize a raw request dict.  No catalog access, no writes."""
    if not isinstance(raw, dict):
        raise CalibrationInputError("request must be a JSON object")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise CalibrationInputError("request_id must be a non-empty string")

    raw_reads = raw.get("reads")
    if not isinstance(raw_reads, list) or not raw_reads:
        raise CalibrationInputError("reads must be a non-empty list")
    seen: set[tuple[str, int]] = set()
    fixed: list[FixedRead] = []
    for index, entry in enumerate(raw_reads):
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise CalibrationInputError(
                f"reads[{index}] must be a [read_id, revision, role] triple"
            )
        read_id, revision, role = entry
        if not isinstance(read_id, str) or not read_id.strip():
            raise CalibrationInputError(f"reads[{index}]: read_id must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise CalibrationInputError(f"reads[{index}]: revision must be an integer >= 1")
        if role not in ROLES:
            raise CalibrationInputError(
                f"reads[{index}]: role must be one of {ROLES}, got {role!r}"
            )
        key = (read_id, revision)
        if key in seen:
            raise CalibrationInputError(
                f"reads[{index}]: duplicate candidate {read_id}@{revision}"
            )
        seen.add(key)
        fixed.append(FixedRead(read_id, revision, role))

    backup_limit = raw.get("backup_limit", 0)
    if isinstance(backup_limit, bool) or not isinstance(backup_limit, int) or backup_limit < 0:
        raise CalibrationInputError("backup_limit must be a non-negative integer")
    backup_candidates = sum(1 for r in fixed if r.role == "BACKUP")
    if backup_limit > backup_candidates:
        # Not fatal, but worth surfacing: a budget above the supply is just the
        # supply.  Accept it; the analysis uses the effective minimum.
        pass

    try:
        shift = parse_interval(raw.get("shift"), where="shift")
    except RationalError as exc:
        raise CalibrationInputError(str(exc)) from exc

    raw_refs = raw.get("references")
    if not isinstance(raw_refs, dict) or not raw_refs:
        raise CalibrationInputError("references must be a non-empty object")
    references: list[tuple[str, Fraction, Fraction]] = []
    for point, pair in raw_refs.items():
        if not isinstance(point, str) or not point.strip():
            raise CalibrationInputError("reference point names must be non-empty strings")
        try:
            ref = parse_rational(pair[0], where=f"references[{point}][0]")
            eps = parse_rational(pair[1], where=f"references[{point}][1]")
        except (TypeError, IndexError, KeyError) as exc:
            raise CalibrationInputError(
                f"references[{point}] must be [position, epsilon]"
            ) from exc
        except RationalError as exc:
            raise CalibrationInputError(str(exc)) from exc
        if eps < 0:
            raise CalibrationInputError(
                f"references[{point}]: epsilon must be non-negative, got {fmt(eps)}"
            )
        references.append((point, ref, eps))

    return Request(request_id.strip(), tuple(fixed), backup_limit, shift, tuple(references))


class Calibrator:
    """Load fixed read versions, solve exactly, and persist products atomically."""

    def __init__(self, store: Store, catalog: Catalog):
        self.store = store
        self.catalog = catalog
        store.executescript(
            """
            CREATE TABLE IF NOT EXISTS calibration_requests (
                request_id  TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                product     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calibration_samples (
                request_id        TEXT NOT NULL,
                point             TEXT NOT NULL,
                read_id           TEXT NOT NULL,
                revision          INTEGER NOT NULL,
                role              TEXT NOT NULL,
                position          TEXT NOT NULL,
                shift             TEXT NOT NULL,
                corrected         TEXT NOT NULL,
                residual          TEXT NOT NULL,
                code              INTEGER NOT NULL,
                scale_id          TEXT NOT NULL,
                scale_revision    INTEGER NOT NULL,
                reference         TEXT NOT NULL,
                epsilon           TEXT NOT NULL,
                PRIMARY KEY (request_id, point),
                FOREIGN KEY (request_id)
                    REFERENCES calibration_requests (request_id)
            );
            """
        )

    # -- public entry points ----------------------------------------------

    def submit(self, raw: dict) -> dict:
        """Run a calibration request, or replay the stored product.

        Order matters for durability: the request fingerprint depends only on
        the submitted content, so an identical replay is served from storage
        before any catalog access and is unaffected by later revisions.
        """
        request = parse_request(raw)
        fingerprint = request.fingerprint()

        stored = self.store.one(
            "SELECT fingerprint, product FROM calibration_requests WHERE request_id = ?",
            (request.request_id,),
        )
        if stored is not None:
            if stored["fingerprint"] != fingerprint:
                raise CalibrationInputError(
                    f"request_id {request.request_id} already exists with different "
                    f"input; refusing to overwrite"
                )
            return {**json.loads(stored["product"]), "replayed": True}

        candidates = self._load_candidates(request)  # raises CalibrationInputError
        feasible, witness, diagnostics = analyze(request, candidates)
        if not feasible:
            raise InfeasibleError(diagnostics["reasons"], diagnostics)

        product = _build_product(request, feasible, witness, diagnostics)

        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT fingerprint, product FROM calibration_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise CalibrationInputError(
                        f"request_id {request.request_id} already exists with different input"
                    )
                return {**json.loads(row[1]), "replayed": True}

            now = datetime.now(timezone.utc).isoformat()
            product["created_at"] = now
            conn.execute(
                "INSERT INTO calibration_requests (request_id, fingerprint, product, created_at) "
                "VALUES (?, ?, ?, ?)",
                (request.request_id, fingerprint,
                 json.dumps(product, sort_keys=True), now),
            )
            for sample in product["samples"]:
                conn.execute(
                    "INSERT INTO calibration_samples "
                    "(request_id, point, read_id, revision, role, position, shift, "
                    " corrected, residual, code, scale_id, scale_revision, reference, epsilon) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.request_id,
                        sample["point"],
                        sample["read_id"],
                        sample["revision"],
                        sample["role"],
                        sample["position"],
                        sample["shift"],
                        sample["corrected"],
                        sample["residual"],
                        sample["source"]["code"],
                        sample["source"]["scale_id"],
                        sample["source"]["scale_revision"],
                        sample["reference"],
                        sample["epsilon"],
                    ),
                )
        return {**product, "replayed": False}

    def get_product(self, request_id: str) -> dict:
        """History entry: read the stored product for a request id."""
        row = self.store.one(
            "SELECT product FROM calibration_requests WHERE request_id = ?",
            (request_id,),
        )
        if row is None:
            raise NotFoundError(f"no stored calibration product for request_id {request_id}")
        return json.loads(row["product"])

    def get_samples(self, request_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT point, read_id, revision, role, position, shift, corrected, "
            "residual, code, scale_id, scale_revision, reference, epsilon "
            "FROM calibration_samples WHERE request_id = ? ORDER BY point",
            (request_id,),
        )
        if not rows:
            if self.store.one(
                "SELECT 1 FROM calibration_requests WHERE request_id = ?", (request_id,)
            ) is None:
                raise NotFoundError(f"no stored calibration product for request_id {request_id}")
        return [
            {
                "point": r["point"],
                "read_id": r["read_id"],
                "revision": r["revision"],
                "role": r["role"],
                "position": r["position"],
                "shift": r["shift"],
                "corrected": r["corrected"],
                "residual": r["residual"],
                "reference": r["reference"],
                "epsilon": r["epsilon"],
                "source": {
                    "scale_id": r["scale_id"],
                    "scale_revision": r["scale_revision"],
                    "code": r["code"],
                },
            }
            for r in rows
        ]

    def list_products(self) -> list[dict]:
        rows = self.store.query(
            "SELECT request_id, fingerprint, created_at FROM calibration_requests "
            "ORDER BY request_id"
        )
        return [dict(r) for r in rows]

    # -- internals ---------------------------------------------------------

    def _load_candidates(self, request: Request) -> dict[str, list[Candidate]]:
        ref_map = request.reference_map
        by_point: dict[str, list[Candidate]] = {p: [] for p in request.points}
        for fixed in request.reads:
            try:
                read = self.catalog.get_read(fixed.read_id, fixed.revision)
            except NotFoundError as exc:
                raise CalibrationInputError(
                    f"candidate read {fixed.read_id}@{fixed.revision} not found"
                ) from exc
            if read.point not in ref_map:
                raise CalibrationInputError(
                    f"candidate {fixed.read_id}@{fixed.revision} names unknown point "
                    f"{read.point!r} (references cover {sorted(ref_map)})"
                )
            ref, eps = ref_map[read.point]
            lo = ref - eps - read.position
            hi = ref + eps - read.position
            raw_window = (lo, hi)
            clipped_lo = max(lo, request.shift[0])
            clipped_hi = min(hi, request.shift[1])
            window = (clipped_lo, clipped_hi) if clipped_lo <= clipped_hi else None
            by_point[read.point].append(
                Candidate(fixed.read_id, fixed.revision, fixed.role, read.point,
                          read.position, read.code, read.scale_id, read.scale_revision,
                          window, raw_window)
            )

        missing = [p for p in request.points if not by_point[p]]
        if missing:
            raise CalibrationInputError(
                f"no candidate read covers reference point(s) {missing}"
            )
        for point in by_point:
            by_point[point].sort(key=lambda c: c.sort_key())
        return by_point


# ---------------------------------------------------------------------------
# Pure analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    point: str
    read_id: str
    revision: int
    role: str
    position: Fraction
    reference: Fraction
    epsilon: Fraction
    shift: Fraction
    code: int
    scale_id: str
    scale_revision: int

    @property
    def corrected(self) -> Fraction:
        return self.position + self.shift

    @property
    def residual(self) -> Fraction:
        return abs(self.corrected - self.reference)

    @property
    def source(self) -> dict:
        return {
            "scale_id": self.scale_id,
            "scale_revision": self.scale_revision,
            "code": self.code,
        }


def _min_backups_at(
    s: Fraction,
    points: Sequence[str],
    candidates: dict[str, list[Candidate]],
    get_window,
) -> tuple[int, Optional[list[Candidate]]]:
    """Minimum backups needed at ``s`` and one witness selection.

    Returns ``(-1, None)`` when some point is not covered at all.
    """
    chosen: list[Candidate] = []
    backups = 0
    for point in points:
        covering = [c for c in candidates[point] if _covers(get_window(c), s)]
        if not covering:
            return -1, None
        primaries = [c for c in covering if c.role == "PRIMARY"]
        if primaries:
            chosen.append(primaries[0])
        else:
            chosen.append(covering[0])  # sorted: backups first among themselves
            backups += 1
    return backups, chosen


def _covers(window: Optional[Interval], s: Fraction) -> bool:
    return window is not None and window[0] <= s <= window[1]


def _sweep(
    points: Sequence[str],
    candidates: dict[str, list[Candidate]],
    get_window,
    budget: int,
    extra_events: Iterable[Fraction] = (),
) -> list[Interval]:
    """Complete feasible-shift union for one window view and backup budget.

    Sweeps every candidate interval endpoint (plus any forced events) and the
    open segments between them.  Closed coverage means a feasible open segment
    is published closed; every event point is also tested on its own so an
    isolated feasible point is never dropped.
    """
    events = set(extra_events)
    for point in points:
        for cand in candidates[point]:
            w = get_window(cand)
            if w is not None:
                events.add(w[0])
                events.add(w[1])
    ordered = sorted(events)
    feasible: list[Interval] = []
    for e in ordered:
        backups, _ = _min_backups_at(e, points, candidates, get_window)
        if backups >= 0 and backups <= budget:
            feasible.append((e, e))
    for left, right in zip(ordered, ordered[1:]):
        if left == right:
            continue
        mid = (left + right) / 2
        backups, _ = _min_backups_at(mid, points, candidates, get_window)
        if backups >= 0 and backups <= budget:
            feasible.append((left, right))
    return normalize(feasible)


def analyze(
    request: Request, candidates: dict[str, list[Candidate]]
) -> tuple[list[Interval], Optional[dict], dict]:
    """Return ``(feasible_intervals, witness, diagnostics)``.

    ``witness`` is ``None`` when infeasible.  ``diagnostics`` always carries the
    per-point achievable windows and, on failure, human-readable ``reasons``.
    """
    points = request.points
    feasible = _sweep(
        points, candidates, lambda c: c.window, request.backup_limit,
        extra_events=request.shift,
    )
    diagnostics = _diagnostics(request, candidates)

    if not feasible:
        diagnostics["reasons"] = _failure_reasons(request, candidates)
        return [], None, diagnostics

    # Choose a canonical witness inside the first feasible interval.
    lo, hi = feasible[0]
    s = (lo + hi) / 2
    backups, chosen = _min_backups_at(s, points, candidates, lambda c: c.window)
    if chosen is None or backups > request.backup_limit:  # pragma: no cover - invariant
        raise CalibrationError("internal error: feasible interval has no valid selection")
    selections = []
    for cand in chosen:
        ref, eps = request.reference_map[cand.point]
        selections.append(
            Selection(cand.point, cand.read_id, cand.revision, cand.role,
                      cand.position, ref, eps, s, cand.code, cand.scale_id,
                      cand.scale_revision)
        )
    witness = {
        "shift": fmt(s),
        "shift_decimal": _decimal(s),
        "backup_used": backups,
        "selections": selections,
    }
    return feasible, witness, diagnostics


def _decimal(value: Fraction) -> Optional[str]:
    """Decimal rendering only when exact; otherwise ``None`` (never rounded)."""
    d = value.denominator
    for factor in (2, 5):
        while d % factor == 0:
            d //= factor
    if d != 1:
        return None
    return _exact_decimal(value)


def _exact_decimal(value: Fraction) -> str:
    # Terminating decimal expansion computed exactly via integer arithmetic.
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole = value.numerator // value.denominator
    rem = value.numerator % value.denominator
    digits = []
    while rem:
        rem *= 10
        digits.append(str(rem // value.denominator))
        rem %= value.denominator
    if not digits:
        return f"{sign}{whole}"
    return f"{sign}{whole}." + "".join(digits)


def _diagnostics(request: Request, candidates: dict[str, list[Candidate]]) -> dict:
    per_point = {}
    for point in request.points:
        union = normalize([c.window for c in candidates[point] if c.window is not None])
        per_point[point] = {
            "reference": fmt(request.reference_map[point][0]),
            "epsilon": fmt(request.reference_map[point][1]),
            "candidates": [
                {
                    "read_id": c.read_id,
                    "revision": c.revision,
                    "role": c.role,
                    "position": fmt(c.position),
                    "shift_window_raw": [fmt(c.raw_window[0]), fmt(c.raw_window[1])],
                    "shift_window_in_bounds": (
                        [fmt(c.window[0]), fmt(c.window[1])] if c.window else []
                    ),
                }
                for c in candidates[point]
            ],
            "achievable_shift_union": [[fmt(lo), fmt(hi)] for lo, hi in union],
        }
    return {
        "shift": [fmt(request.shift[0]), fmt(request.shift[1])],
        "backup_limit": request.backup_limit,
        "points": per_point,
    }


def _failure_reasons(
    request: Request, candidates: dict[str, list[Candidate]]
) -> list[str]:
    """Layered explanation of why no product can be published.

    Layer A: raw per-candidate windows, unlimited backups, no shift bounds --
             fails only when the points' individual windows are disjoint.
    Layer B: windows clipped to the allowed shift interval, unlimited backups --
             fails when the shift bounds exclude every common value.
    Layer C: clipped windows with the backup budget -- fails only on budget.
    Each layer is reported only if it is the first to fail, so the message
    names the actual blocking constraint.
    """
    points = request.points
    g0, g1 = request.shift
    unlimited = len(points)  # at most one backup per point, so this is "no cap"
    reasons: list[str] = []

    raw_feasible = _sweep(points, candidates, lambda c: c.raw_window, unlimited)
    if not raw_feasible:
        reasons.append(
            "each point is individually reachable, but no common shift "
            "satisfies every point simultaneously (individual windows are disjoint)"
        )
        return reasons

    bounded_feasible = _sweep(points, candidates, lambda c: c.window, unlimited)
    if not bounded_feasible:
        reasons.append(
            f"a common shift exists, but none lies in the allowed shift interval "
            f"[{fmt(g0)}, {fmt(g1)}]"
        )
        return reasons

    # A common shift exists within bounds given unlimited backups; the only
    # remaining obstruction is the budget.  Coverage changes only at event
    # points, so the true minimum over the region is attained at one of them.
    min_needed = None
    event_points = set()
    for lo, hi in bounded_feasible:
        event_points.add(lo)
        event_points.add(hi)
        for point in points:
            for cand in candidates[point]:
                w = cand.window
                if w is not None and lo <= w[0] <= hi:
                    event_points.add(w[0])
                if w is not None and lo <= w[1] <= hi:
                    event_points.add(w[1])
    for e in event_points:
        if not contains(bounded_feasible, e):
            continue
        backups, _ = _min_backups_at(e, points, candidates, lambda c: c.window)
        if backups >= 0:
            min_needed = backups if min_needed is None else min(min_needed, backups)
    if min_needed is not None and min_needed > request.backup_limit:
        reasons.append(
            f"a common shift exists within the allowed interval, but it requires "
            f"at least {min_needed} BACKUP read(s), exceeding the budget of "
            f"{request.backup_limit}"
        )
    else:
        reasons.append("no feasible shift and selection satisfy all constraints")
    return reasons


def _build_product(
    request: Request,
    feasible: list[Interval],
    witness: dict,
    diagnostics: dict,
) -> dict:
    samples = []
    for sel in witness["selections"]:
        samples.append(
            {
                "point": sel.point,
                "read_id": sel.read_id,
                "revision": sel.revision,
                "role": sel.role,
                "position": fmt(sel.position),
                "shift": fmt(sel.shift),
                "corrected": fmt(sel.corrected),
                "residual": fmt(sel.residual),
                "reference": fmt(sel.reference),
                "epsilon": fmt(sel.epsilon),
                "source": sel.source,
            }
        )
    return {
        "request_id": request.request_id,
        "status": "FEASIBLE",
        "shift_allowed": [fmt(request.shift[0]), fmt(request.shift[1])],
        "backup_limit": request.backup_limit,
        "backup_used": witness["backup_used"],
        "feasible_shifts": [[fmt(lo), fmt(hi)] for lo, hi in feasible],
        "witness": {
            "shift": witness["shift"],
            "shift_decimal": witness["shift_decimal"],
            "backup_used": witness["backup_used"],
            "selections": [
                {
                    "point": s.point,
                    "read_id": s.read_id,
                    "revision": s.revision,
                    "role": s.role,
                }
                for s in witness["selections"]
            ],
        },
        "samples": samples,
        "points": diagnostics["points"],
        "request": {
            "backup_limit": request.backup_limit,
            "shift": [fmt(request.shift[0]), fmt(request.shift[1])],
            "references": {
                p: [fmt(ref), fmt(eps)] for p, ref, eps in request.references
            },
            "reads": [
                {"read_id": r.read_id, "revision": r.revision, "role": r.role}
                for r in request.reads
            ],
        },
    }
