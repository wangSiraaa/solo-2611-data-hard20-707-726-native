"""Composition service: ordered trains, orientation-aware geometry and events.

A *train_id* is a slot.  While a train is active it owns exactly one composition
version and every car in it.  The partial unique index on
``train_cars(car_id) WHERE active = 1`` enforces "a car can only be in one
active train at a time" at the storage layer.

``event_id`` is idempotent: replaying the same content returns the original
successful result, replaying the same id with different content is refused
(``ConflictError``) and nothing changes.
"""

from __future__ import annotations

import json
from fractions import Fraction

from . import db as _db
from .model import (
    CarSpec,
    Composition,
    ConflictError,
    InactiveError,
    NotFoundError,
    ValidationError,
    build_composition,
    canonical,
    content_hash,
    couple_sequences,
    frac_to_json,
    normalize_id,
    parse_int,
    parse_nonnegative_rational,
    parse_positive_rational,
    reverse_composition,
    split_sequence,
)

OPERATIONS = ("couple", "split", "reverse")


def event_request_hash(request: dict) -> str:
    """Stable hash over the semantic content of an event request.

    Unknown fields are excluded: re-sending the same semantic event (possibly
    with extra envelope fields such as ``report_id``) is still a replay.
    """
    return content_hash(
        {
            "event_id": request.get("event_id"),
            "op": request.get("op"),
            "target": request.get("target"),
            "target_end": request.get("target_end"),
            "other": request.get("other"),
            "other_end": request.get("other_end"),
            "train_id": request.get("train_id"),
            "end": request.get("end"),
            "n": request.get("n"),
            "new_train_id": request.get("new_train_id"),
        }
    )


class CompositionService:
    def __init__(self, conn):
        self.conn = conn

    # -- loading ---------------------------------------------------------

    def _load_specs(self) -> dict[str, CarSpec]:
        rows = self.conn.execute("SELECT car_id, length_n, length_d FROM cars").fetchall()
        return {
            r["car_id"]: CarSpec(r["car_id"], Fraction(r["length_n"], r["length_d"]))
            for r in rows
        }

    def _load_sequence(self, train_id: str, active_only: bool = True) -> list[tuple[str, int]]:
        where = "AND active = 1" if active_only else ""
        rows = self.conn.execute(
            f"SELECT car_id, orientation FROM train_cars "
            f"WHERE train_id = ? {where} ORDER BY seq",
            (train_id,),
        ).fetchall()
        return [(r["car_id"], r["orientation"]) for r in rows]

    def _load_composition(self, train_id: str, active_only: bool = True) -> Composition:
        return build_composition(
            self._load_sequence(train_id, active_only), self._load_specs()
        )

    def _train_row(self, train_id: str):
        return self.conn.execute(
            "SELECT * FROM trains WHERE train_id = ?", (train_id,)
        ).fetchone()

    def _require_train(self, train_id: str):
        row = self._train_row(train_id)
        if row is None:
            raise NotFoundError(f"train {train_id!r} does not exist")
        return row

    def _require_active(self, train_id: str):
        row = self._require_train(train_id)
        if not row["active"]:
            raise InactiveError(f"train {train_id!r} is not an active composition")
        return row

    def _car_row(self, car_id: str):
        return self.conn.execute("SELECT * FROM cars WHERE car_id = ?", (car_id,)).fetchone()

    # -- registration ----------------------------------------------------

    def add_car(self, car_id: str, length) -> dict:
        """Register a permanent car (id + positive rational length).

        Registration alone does not put the car on a train; use
        ``create_train`` to build its first active composition.
        """
        car_id = normalize_id(car_id, "car_id")
        length_f = parse_positive_rational(length, "length")
        with _db.write_tx(self.conn):
            if self._car_row(car_id) is not None:
                raise ValidationError(f"car {car_id!r} already exists")
            self.conn.execute(
                "INSERT INTO cars(car_id, length_n, length_d) VALUES (?, ?, ?)",
                (car_id, length_f.numerator, length_f.denominator),
            )
        return {"car_id": car_id, "length": frac_to_json(length_f)}

    def create_train(self, train_id: str, cars: list[dict]) -> dict:
        """Create a train from ``[{"car_id","orientation"}]`` (default +1)."""
        train_id = normalize_id(train_id, "train_id")
        if not isinstance(cars, list) or not cars:
            raise ValidationError("cars must be a non-empty list")
        sequence: list[tuple[str, int]] = []
        for item in cars:
            if not isinstance(item, dict):
                raise ValidationError("each car must be an object")
            car_id = normalize_id(item.get("car_id"), "car_id")
            orientation = parse_int(item.get("orientation", 1), "orientation")
            if orientation not in (1, -1):
                raise ValidationError(f"orientation of {car_id!r} must be +1 or -1")
            sequence.append((car_id, orientation))
        ids = [c for c, _ in sequence]
        if len(ids) != len(set(ids)):
            raise ValidationError("a car may appear only once per train")
        specs = self._load_specs()
        for car_id in ids:
            if car_id not in specs:
                raise NotFoundError(f"car {car_id!r} does not exist")
        with _db.write_tx(self.conn):
            if self._train_row(train_id) is not None:
                raise ValidationError(f"train {train_id!r} already exists")
            occupied = self._active_owners(ids)
            if occupied:
                raise ValidationError(
                    "cars already active elsewhere: "
                    + ", ".join(f"{c}@{t}" for c, t in sorted(occupied.items()))
                )
            seq = _db.next_seq(self.conn)
            comp = self._insert_train(train_id, sequence, seq)
        return self.composition(train_id)

    def _active_owners(self, car_ids: list[str]) -> dict[str, str]:
        if not car_ids:
            return {}
        marks = ",".join("?" for _ in car_ids)
        rows = self.conn.execute(
            f"SELECT car_id, train_id FROM train_cars "
            f"WHERE active = 1 AND car_id IN ({marks})",
            car_ids,
        ).fetchall()
        return {r["car_id"]: r["train_id"] for r in rows}

    # -- persistence helpers --------------------------------------------

    def _insert_train(self, train_id: str, sequence, seq: int) -> Composition:
        comp = build_composition(sequence, self._load_specs())
        self.conn.execute(
            "INSERT INTO trains(train_id, composition_version, revision, active, "
            "created_seq, updated_seq) VALUES (?, ?, 1, 1, ?, ?)",
            (train_id, comp.version(), seq, seq),
        )
        self._write_sequence(train_id, comp)
        return comp

    def _write_sequence(self, train_id: str, comp: Composition) -> None:
        self.conn.execute("DELETE FROM train_cars WHERE train_id = ?", (train_id,))
        for seq, p in enumerate(comp.placements):
            self.conn.execute(
                "INSERT INTO train_cars(train_id, seq, car_id, orientation, "
                "start_n, start_d, end_n, end_d, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    train_id,
                    seq,
                    p.car_id,
                    p.orientation,
                    p.start.numerator,
                    p.start.denominator,
                    p.end.numerator,
                    p.end.denominator,
                ),
            )

    # -- queries ---------------------------------------------------------

    def composition(self, train_id: str, include_inactive: bool = False) -> dict:
        row = self._require_train(train_id) if include_inactive else self._require_active(train_id)
        comp = self._load_composition(train_id, active_only=bool(row["active"]))
        return {
            "train_id": train_id,
            "composition_version": row["composition_version"],
            "revision": row["revision"],
            "active": bool(row["active"]),
            "length": frac_to_json(comp.length),
            "cars": [p.to_json() for p in comp.placements],
        }

    def list_trains(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trains ORDER BY created_seq").fetchall()
        return [
            {
                "train_id": r["train_id"],
                "composition_version": r["composition_version"],
                "revision": r["revision"],
                "active": bool(r["active"]),
            }
            for r in rows
        ]

    def train_detail(self, train_id: str) -> dict:
        """Full order + orientations + version, also for absorbed trains."""
        return self.composition(train_id, include_inactive=True)

    def car_coordinate(self, train_id: str, car_id: str, local_position) -> dict:
        local = parse_nonnegative_rational(local_position, "local_position")
        row = self._require_active(train_id)
        comp = self._load_composition(train_id)
        placement = comp.by_car().get(car_id)
        if placement is None:
            raise NotFoundError(f"car {car_id!r} is not in train {train_id!r}")
        if local > placement.length:
            raise ValidationError(
                f"local_position {local} is outside car length {placement.length}"
            )
        x = placement.map_local(local)
        return {
            "train_id": train_id,
            "composition_version": row["composition_version"],
            "car_id": car_id,
            "orientation": placement.orientation,
            "local_position": frac_to_json(local),
            "x": frac_to_json(x),
            "start": frac_to_json(placement.start),
            "end": frac_to_json(placement.end),
        }

    # -- events ----------------------------------------------------------

    def apply_event(self, request: dict) -> dict:
        """Idempotent dispatcher for couple/split/reverse."""
        if not isinstance(request, dict):
            raise ValidationError("event request must be an object")
        op = request.get("op")
        if op not in OPERATIONS:
            raise ValidationError(f"unsupported composition op: {op!r}")
        event_id = normalize_id(request.get("event_id"), "event_id")
        request_hash = event_request_hash(request)
        seen = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if seen is not None:
            if seen["request_hash"] != request_hash:
                raise ConflictError(
                    f"event_id {event_id!r} was already used with different content"
                )
            return json.loads(seen["result_json"])
        with _db.write_tx(self.conn):
            again = self.conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if again is not None:
                if again["request_hash"] != request_hash:
                    raise ConflictError(
                        f"event_id {event_id!r} was already used with different content"
                    )
                return json.loads(again["result_json"])
            seq = _db.next_seq(self.conn)
            if op == "couple":
                result = self._couple(request, seq)
            elif op == "split":
                result = self._split(request, seq)
            else:
                result = self._reverse(request, seq)
            self.conn.execute(
                "INSERT INTO events(event_id, op, train_id, request_hash, "
                "request_json, result_json, seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    op,
                    result["train_id"],
                    request_hash,
                    canonical(request),
                    canonical(result),
                    seq,
                ),
            )
        return result

    def _couple(self, request: dict, seq: int) -> dict:
        target = normalize_id(request.get("target"), "target")
        other = normalize_id(request.get("other"), "other")
        target_end = request.get("target_end")
        other_end = request.get("other_end")
        if target == other:
            raise ValidationError("a train cannot be coupled to itself")
        if target_end not in ("head", "tail"):
            raise ValidationError("target_end must be 'head' or 'tail'")
        if other_end not in ("head", "tail"):
            raise ValidationError("other_end must be 'head' or 'tail'")
        target_row = self._require_active(target)
        other_row = self._require_active(other)
        new_seq, rule = couple_sequences(
            self._load_sequence(target),
            self._load_sequence(other),
            target_end,
            other_end,
        )
        new_comp = build_composition(new_seq, self._load_specs())
        # 'other' stops being an active composition first: deactivating releases
        # its cars so the merged sequence can claim them under the partial
        # unique index without a transient double ownership.
        self.conn.execute(
            "UPDATE train_cars SET active = 0 WHERE train_id = ?", (other,)
        )
        self.conn.execute(
            "UPDATE trains SET active = 0, updated_seq = ? WHERE train_id = ?",
            (seq, other),
        )
        self._write_sequence(target, new_comp)
        self.conn.execute(
            "UPDATE trains SET composition_version = ?, revision = revision + 1, "
            "updated_seq = ? WHERE train_id = ?",
            (new_comp.version(), seq, target),
        )
        return {
            "event_id": request["event_id"],
            "op": "couple",
            "train_id": target,
            "absorbed_train_id": other,
            "target_end": target_end,
            "other_end": other_end,
            "layout_rule": rule,
            "composition_version": new_comp.version(),
            "cars": [p.to_json() for p in new_comp.placements],
            "previous": {
                "target_version": target_row["composition_version"],
                "other_version": other_row["composition_version"],
            },
        }

    def _split(self, request: dict, seq: int) -> dict:
        train_id = normalize_id(request.get("train_id"), "train_id")
        end = request.get("end")
        n = parse_int(request.get("n"), "n")
        new_train_id = normalize_id(request.get("new_train_id"), "new_train_id")
        if new_train_id == train_id:
            raise ValidationError("new_train_id must differ from the split train")
        row = self._require_active(train_id)
        moved, remaining = split_sequence(self._load_sequence(train_id), end, n)
        if self._train_row(new_train_id) is not None:
            raise ValidationError(f"train id {new_train_id!r} already exists")
        specs = self._load_specs()
        moved_comp = build_composition(moved, specs)
        remaining_comp = build_composition(remaining, specs)
        # remaining first (cars stay active), moved piece goes to the new id
        self._write_sequence(train_id, remaining_comp)
        self.conn.execute(
            "UPDATE trains SET composition_version = ?, revision = revision + 1, "
            "updated_seq = ? WHERE train_id = ?",
            (remaining_comp.version(), seq, train_id),
        )
        self._insert_train(new_train_id, moved, seq)
        return {
            "event_id": request["event_id"],
            "op": "split",
            "train_id": train_id,
            "new_train_id": new_train_id,
            "end": end,
            "n": n,
            "composition_version": remaining_comp.version(),
            "new_composition_version": moved_comp.version(),
            "previous": {"train_version": row["composition_version"]},
            "cars": [p.to_json() for p in remaining_comp.placements],
            "new_cars": [p.to_json() for p in moved_comp.placements],
        }

    def _reverse(self, request: dict, seq: int) -> dict:
        train_id = normalize_id(request.get("train_id"), "train_id")
        row = self._require_active(train_id)
        new_comp = build_composition(
            reverse_composition(self._load_sequence(train_id)), self._load_specs()
        )
        self._write_sequence(train_id, new_comp)
        self.conn.execute(
            "UPDATE trains SET composition_version = ?, revision = revision + 1, "
            "updated_seq = ? WHERE train_id = ?",
            (new_comp.version(), seq, train_id),
        )
        return {
            "event_id": request["event_id"],
            "op": "reverse",
            "train_id": train_id,
            "composition_version": new_comp.version(),
            "previous": {"train_version": row["composition_version"]},
            "cars": [p.to_json() for p in new_comp.placements],
        }

    # -- event lookup ----------------------------------------------------

    def get_event(self, event_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"event {event_id!r} does not exist")
        return {
            "event_id": row["event_id"],
            "op": row["op"],
            "train_id": row["train_id"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }

    def list_events(self) -> list[dict]:
        rows = self.conn.execute("SELECT event_id FROM events ORDER BY seq").fetchall()
        return [self.get_event(r["event_id"]) for r in rows]



# ---------------------------------------------------------------------------
# Shared loaders (also used by the unload repository)
# ---------------------------------------------------------------------------


def load_specs(conn) -> dict[str, CarSpec]:
    rows = conn.execute("SELECT car_id, length_n, length_d FROM cars").fetchall()
    return {
        r["car_id"]: CarSpec(r["car_id"], Fraction(r["length_n"], r["length_d"]))
        for r in rows
    }


def load_active_sequence(conn, train_id: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT car_id, orientation FROM train_cars "
        "WHERE train_id = ? AND active = 1 ORDER BY seq",
        (train_id,),
    ).fetchall()
    return [(r["car_id"], r["orientation"]) for r in rows]


def load_composition(conn, train_id: str) -> Composition:
    return build_composition(load_active_sequence(conn, train_id), load_specs(conn))
