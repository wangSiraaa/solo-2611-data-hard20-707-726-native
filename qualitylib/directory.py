"""Unit directory: exact, category-aware, versioned conversion graph.

The directory is independent of any sample data.  It can be validated,
queried and used for conversion entirely on its own.

Design notes
------------
* Edges are *directed* affine maps ``y = a*x + b`` between two units of the
  same quantity.  ``a > 0`` and both ``a``/``b`` are exact rationals.
* ``absolute`` conversions use ``a`` and ``b``; ``difference`` conversions use
  ``a`` only.  The two categories are never mixed.
* A quantity's connected components are validated *after* a whole revision
  batch has been folded into a final edge set.  Every affine cycle in a
  component must reduce to the identity; different components are explicitly
  non-convertible.
* Paths are chosen deterministically (breadth-first, tie-broken by edge id)
  and the returned path lists the *actual* edges and orientations used.
"""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

from .edge import (
    KIND_ABSOLUTE,
    KIND_DIFFERENCE,
    KINDS,
    Affine,
    Edge,
    edge_from_json,
)
from .errors import (
    ConversionError,
    DisconnectedError,
    KindMismatch,
    RevisionError,
    UnknownUnit,
    ValidationError,
)
from .rational import canonical_json, ratio_json


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PathStep:
    """One traversed edge of a conversion path."""

    edge_id: str
    from_unit: str
    to_unit: str
    direction: str  # "forward" or "reverse"
    a: Fraction
    b: Fraction

    @property
    def affine(self) -> Affine:
        return Affine(self.a, self.b)

    def to_json(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "from": self.from_unit,
            "to": self.to_unit,
            "direction": self.direction,
            "a": ratio_json(self.a),
            "b": ratio_json(self.b),
        }


@dataclass(frozen=True)
class Conversion:
    """Result of converting one unit into another for one category."""

    from_unit: str
    to_unit: str
    kind: str
    quantity_id: str
    affine: Affine  # already category-normalised (b = 0 for differences)
    path: tuple[PathStep, ...]

    @property
    def factor(self) -> Fraction:
        return self.affine.a

    def apply(self, value: Fraction) -> Fraction:
        if self.kind == KIND_DIFFERENCE:
            return self.affine.a * value
        return self.affine.a * value + self.affine.b

    def path_json(self) -> list[dict]:
        return [step.to_json() for step in self.path]


@dataclass(frozen=True)
class Directory:
    revision: str
    quantities: Mapping[str, frozenset]
    units: Mapping[str, str]
    edges: Mapping[str, Edge]

    # -- basic lookups ------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)

    def quantity_of(self, unit_id: str) -> str:
        try:
            return self.units[unit_id]
        except KeyError as exc:
            raise UnknownUnit(f"unknown unit {unit_id!r}") from exc

    def allowed_kinds(self, quantity_id: str) -> frozenset:
        try:
            return self.quantities[quantity_id]
        except KeyError as exc:
            raise ValidationError(f"unknown quantity {quantity_id!r}") from exc

    def require_kind(self, quantity_id: str, kind: str) -> None:
        if kind not in KINDS:
            raise KindMismatch(f"unknown kind {kind!r}")
        if kind not in self.allowed_kinds(quantity_id):
            raise KindMismatch(
                f"quantity {quantity_id!r} does not allow {kind!r}"
            )

    # -- graph helpers ------------------------------------------------------
    def _adjacency(self) -> dict[str, list[tuple[str, Edge, str]]]:
        adj: dict[str, list[tuple[str, Edge, str]]] = {u: [] for u in self.units}
        for edge in self.edges.values():
            adj.setdefault(edge.from_unit, []).append((edge.to_unit, edge, "forward"))
            adj.setdefault(edge.to_unit, []).append((edge.from_unit, edge, "reverse"))
        for neighbours in adj.values():
            # Deterministic BFS: order by edge id then peer unit.
            neighbours.sort(key=lambda item: (item[1].edge_id, item[0], item[2]))
        return adj

    def components(self, quantity_id: str | None = None) -> list[frozenset[str]]:
        """Connected components (optionally restricted to one quantity)."""
        adj = self._adjacency()
        allowed = set(self.units)
        if quantity_id is not None:
            allowed = {u for u, q in self.units.items() if q == quantity_id}
        seen: set[str] = set()
        result: list[frozenset[str]] = []
        for unit in sorted(allowed):
            if unit in seen:
                continue
            comp: set[str] = set()
            queue = deque([unit])
            seen.add(unit)
            while queue:
                cur = queue.popleft()
                comp.add(cur)
                for peer, _edge, _direction in adj.get(cur, ()):
                    if peer in allowed and peer not in seen:
                        seen.add(peer)
                        queue.append(peer)
            result.append(frozenset(comp))
        return result

    def is_connected(self, u1: str, u2: str) -> bool:
        self.quantity_of(u1)
        self.quantity_of(u2)
        for comp in self.components():
            if u1 in comp and u2 in comp:
                return True
        return False

    # -- conversion ---------------------------------------------------------
    def conversion(self, from_unit: str, to_unit: str, kind: str) -> Conversion:
        q_from = self.quantity_of(from_unit)
        q_to = self.quantity_of(to_unit)
        if q_from != q_to:
            raise ConversionError(
                f"{from_unit!r} ({q_from}) and {to_unit!r} ({q_to}) belong to "
                f"different quantities"
            )
        self.require_kind(q_from, kind)

        if from_unit == to_unit:
            return Conversion(
                from_unit, to_unit, kind, q_from, Affine(Fraction(1), Fraction(0)), ()
            )

        adj = self._adjacency()
        # BFS storing (unit, accumulated affine, path steps).
        start = Affine(Fraction(1), Fraction(0))
        queue: deque[tuple[str, Affine, tuple[PathStep, ...]]] = deque(
            [(from_unit, start, ())]
        )
        seen = {from_unit}
        found: Conversion | None = None
        while queue and found is None:
            unit, acc, steps = queue.popleft()
            for peer, edge, direction in adj.get(unit, ()):
                if peer in seen:
                    continue
                step_map = edge.affine if direction == "forward" else edge.affine.reverse
                new_acc = step_map.compose_into(acc)
                step = PathStep(
                    edge_id=edge.edge_id,
                    from_unit=unit,
                    to_unit=peer,
                    direction=direction,
                    a=step_map.a,
                    b=step_map.b,
                )
                new_steps = steps + (step,)
                if peer == to_unit:
                    found = Conversion(
                        from_unit,
                        to_unit,
                        kind,
                        q_from,
                        new_acc.normalize(kind),
                        new_steps,
                    )
                    break
                seen.add(peer)
                queue.append((peer, new_acc, new_steps))
        if found is None:
            raise DisconnectedError(
                f"no conversion path from {from_unit!r} to {to_unit!r} "
                f"in revision {self.revision!r}"
            )
        return found

    def convert(self, value: Fraction, from_unit: str, to_unit: str, kind: str) -> Fraction:
        if not isinstance(value, Fraction):
            raise TypeError("value must be an exact Fraction")
        return self.conversion(from_unit, to_unit, kind).apply(value)

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "revision": self.revision,
            "quantities": [
                {"quantity_id": qid, "allowed_kinds": sorted(kinds)}
                for qid, kinds in sorted(self.quantities.items())
            ],
            "units": [
                {"unit_id": uid, "quantity_id": qid}
                for uid, qid in sorted(self.units.items())
            ],
            "edges": [self.edges[eid].to_json() for eid in sorted(self.edges)],
        }


def canonical_fingerprint(directory: Directory) -> str:
    payload = directory.to_json()
    payload.pop("revision", None)
    blob = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------
def _parse_quantities(raw) -> dict[str, frozenset]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("directory.quantities must be a non-empty list")
    quantities: dict[str, frozenset] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("quantity entry must be an object")
        qid = item.get("quantity_id")
        if not isinstance(qid, str) or not qid:
            raise ValidationError("quantity_id must be a non-empty string")
        if qid in quantities:
            raise ValidationError(f"duplicate quantity {qid!r}")
        kinds = item.get("allowed_kinds")
        if not isinstance(kinds, list) or not kinds:
            raise ValidationError(f"quantity {qid!r} needs a non-empty allowed_kinds")
        kind_set = set()
        for kind in kinds:
            if kind not in KINDS:
                raise ValidationError(f"quantity {qid!r}: unknown kind {kind!r}")
            kind_set.add(kind)
        quantities[qid] = frozenset(kind_set)
    return quantities


def _parse_units(raw, quantities) -> dict[str, str]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("directory.units must be a non-empty list")
    units: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("unit entry must be an object")
        uid = item.get("unit_id")
        qid = item.get("quantity_id")
        if not isinstance(uid, str) or not uid:
            raise ValidationError("unit_id must be a non-empty string")
        if uid in units:
            raise ValidationError(f"duplicate unit {uid!r}")
        if qid not in quantities:
            raise ValidationError(f"unit {uid!r} references unknown quantity {qid!r}")
        units[uid] = qid
    return units


def _validate_edges(edges: Mapping[str, Edge], units: Mapping[str, str], quantities) -> None:
    for eid, edge in edges.items():
        if edge.from_unit not in units:
            raise ValidationError(f"edge {eid!r} references unknown unit {edge.from_unit!r}")
        if edge.to_unit not in units:
            raise ValidationError(f"edge {eid!r} references unknown unit {edge.to_unit!r}")
        q_from = units[edge.from_unit]
        q_to = units[edge.to_unit]
        if q_from != q_to:
            raise ValidationError(
                f"edge {eid!r} crosses quantities {q_from!r} -> {q_to!r}"
            )
        if edge.a <= 0:
            raise ValidationError(f"edge {eid!r}: coefficient a must be positive")


def _check_components(directory: Directory) -> None:
    """Every affine cycle in every component must reduce to the identity."""
    adj = directory._adjacency()
    for qid in sorted(directory.quantities):
        unit_set = {u for u, q in directory.units.items() if q == qid}
        if not unit_set:
            continue
        requires_absolute = KIND_ABSOLUTE in directory.quantities[qid]
        seen: set[str] = set()
        for root in sorted(unit_set):
            if root in seen:
                continue
            potentials: dict[str, Affine] = {root: Affine(Fraction(1), Fraction(0))}
            seen.add(root)
            queue = deque([root])
            while queue:
                cur = queue.popleft()
                pot_cur = potentials[cur]
                for peer, edge, direction in adj.get(cur, ()):
                    if peer not in unit_set:
                        continue
                    step = edge.affine if direction == "forward" else edge.affine.reverse
                    cand = step.compose_into(pot_cur)
                    if peer in potentials:
                        if not _affine_equal(
                            cand, potentials[peer], requires_absolute, edge_id=edge.edge_id
                        ):
                            raise RevisionError(
                                f"inconsistent affine cycle in quantity {qid!r} "
                                f"involving edge {edge.edge_id!r}"
                            )
                    else:
                        potentials[peer] = cand
                        seen.add(peer)
                        queue.append(peer)


def _affine_equal(lhs: Affine, rhs: Affine, absolute: bool, *, edge_id: str) -> bool:
    if lhs.a != rhs.a:
        return False
    if absolute:
        return lhs.b == rhs.b
    return True


def parse_directory(obj: Mapping) -> Directory:
    """Parse and fully validate a directory document."""
    if not isinstance(obj, Mapping):
        raise ValidationError("directory must be a JSON object")
    revision = obj.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ValidationError("directory.revision must be a non-empty string")
    quantities = _parse_quantities(obj.get("quantities"))
    units = _parse_units(obj.get("units"), quantities)
    raw_edges = obj.get("edges")
    if not isinstance(raw_edges, list):
        raise ValidationError("directory.edges must be a list")
    edges: dict[str, Edge] = {}
    for index, raw in enumerate(raw_edges):
        edge = edge_from_json(raw, where=f"edges[{index}]")
        if edge.edge_id in edges:
            raise ValidationError(f"duplicate edge_id {edge.edge_id!r}")
        edges[edge.edge_id] = edge
    _validate_edges(edges, units, quantities)
    directory = Directory(
        revision=revision,
        quantities=dict(quantities),
        units=dict(units),
        edges=dict(edges),
    )
    _check_components(directory)
    return directory


# ---------------------------------------------------------------------------
# Revisions: fold first, validate the final graph, adopt atomically
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RevisionOperation:
    op: str
    edge_id: str
    edge: Edge | None = None


def parse_operations(obj: Mapping) -> tuple[str | None, list[RevisionOperation]]:
    """Parse a revision batch without validating the resulting graph."""
    if not isinstance(obj, Mapping):
        raise RevisionError("revision must be an object")
    revision = obj.get("revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise RevisionError("revision.revision must be a non-empty string")
    raw_ops = obj.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise RevisionError("revision.operations must be a non-empty list")
    ops: list[RevisionOperation] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_ops):
        where = f"operations[{index}]"
        if not isinstance(raw, dict):
            raise RevisionError(f"{where} must be an object")
        op = raw.get("op")
        if op == "delete":
            edge_id = raw.get("edge_id")
            if not isinstance(edge_id, str) or not edge_id:
                raise RevisionError(f"{where}.edge_id must be a non-empty string")
            if edge_id in seen_ids:
                raise RevisionError(f"edge_id {edge_id!r} appears more than once in batch")
            seen_ids.add(edge_id)
            ops.append(RevisionOperation("delete", edge_id, None))
        elif op in ("upsert", "add", "replace"):
            edge = edge_from_json(raw.get("edge"), where=f"{where}.edge")
            if edge.edge_id in seen_ids:
                raise RevisionError(
                    f"edge_id {edge.edge_id!r} appears more than once in batch"
                )
            seen_ids.add(edge.edge_id)
            ops.append(RevisionOperation(op, edge.edge_id, edge))
        else:
            raise RevisionError(f"{where}: unsupported op {op!r}")
    return revision, ops


def fold_operations(current: Directory, operations: Iterable[RevisionOperation]) -> dict[str, Edge]:
    """Apply operations in order into a *candidate* final edge set.

    No per-step graph validation happens here: only the final graph counts.
    """
    final: dict[str, Edge] = dict(current.edges)
    for operation in operations:
        if operation.op == "delete":
            if operation.edge_id not in final:
                raise RevisionError(
                    f"delete of unknown edge {operation.edge_id!r}"
                )
            del final[operation.edge_id]
        elif operation.op == "add":
            if operation.edge_id in final:
                raise RevisionError(f"add of existing edge {operation.edge_id!r}")
            final[operation.edge_id] = operation.edge
        elif operation.op == "replace":
            if operation.edge_id not in final:
                raise RevisionError(f"replace of unknown edge {operation.edge_id!r}")
            final[operation.edge_id] = operation.edge
        elif operation.op == "upsert":
            final[operation.edge_id] = operation.edge
        else:  # pragma: no cover - parse_operations guards this
            raise RevisionError(f"unsupported op {operation.op!r}")
    return final


def revise(current: Directory, revision_doc: Mapping) -> Directory:
    """Return a new validated Directory, or raise ``RevisionError``.

    The current directory object is never mutated; a rejected batch leaves the
    caller's current revision untouched.
    """
    label, ops = parse_operations(revision_doc)
    if label is not None and label == current.revision:
        raise RevisionError(f"revision label {label!r} already in use")
    final_edges = fold_operations(current, ops)
    candidate = Directory(
        revision=label or _next_revision_label(current.revision),
        quantities=dict(current.quantities),
        units=dict(current.units),
        edges=final_edges,
    )
    try:
        _validate_edges(candidate.edges, candidate.units, candidate.quantities)
        _check_components(candidate)
    except RevisionError:
        raise
    except ValidationError as exc:
        raise RevisionError(str(exc)) from exc
    return candidate


def _next_revision_label(label: str) -> str:
    if label.startswith("r") and label[1:].isdigit():
        return f"r{int(label[1:]) + 1}"
    return f"{label}+1"
