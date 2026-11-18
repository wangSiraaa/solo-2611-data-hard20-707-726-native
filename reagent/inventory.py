"""Inventory service: initialize, query, open, reserve, consume, release.

The service is the only writer of ``bottles`` / ``reservations``. The run
module composes these entry points inside its own transaction, so the
allocation facts the runs depend on are exactly the rows stored here.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from . import bottles
from .bottles import BottleRow
from .errors import Conflict, InsufficientStock, NotFoundError, ValidationError
from .rules import advance_day, require_day, require_int, require_text
from .schema import meta_get, meta_set, transaction

INITIALIZED_KEY = "initialized"
INITIAL_DAY_KEY = "initial_day"


class InventoryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- initialization -----------------------------------------------------
    def initialize(self, document: dict) -> dict:
        """Load a validated inventory document into an empty store."""
        with transaction(self.conn):
            if meta_get(self.conn, INITIALIZED_KEY) == "1":
                raise Conflict("store is already initialized")

            initial_day = require_day(document.get("initial_day"), "initial_day")
            kinds = _validate_kinds(document.get("kinds"))
            bottle_docs = _validate_bottles(document.get("bottles"), kinds, initial_day)
            methods = _validate_methods(document.get("methods"), kinds)

            for kind, lifetime in kinds.items():
                self.conn.execute(
                    "INSERT INTO kinds(kind, open_lifetime_days) VALUES(?, ?)",
                    (kind, lifetime),
                )
            for doc in bottle_docs:
                self.conn.execute(
                    "INSERT INTO bottles(bottle_id, kind, total_portions, "
                    "intake_day, printed_expiry_day, opened_day) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        doc["bottle_id"],
                        doc["kind"],
                        doc["total_portions"],
                        doc["intake_day"],
                        doc["printed_expiry_day"],
                        doc["opened_day"],
                    ),
                )
            for method_id, revisions in methods.items():
                for revision, requirements in revisions.items():
                    self.conn.execute(
                        "INSERT INTO methods(method_id, revision) VALUES(?, ?)",
                        (method_id, revision),
                    )
                    for kind, portions in requirements.items():
                        self.conn.execute(
                            "INSERT INTO method_requirements"
                            "(method_id, revision, kind, portions) "
                            "VALUES(?, ?, ?, ?)",
                            (method_id, revision, kind, portions),
                        )

            meta_set(self.conn, INITIAL_DAY_KEY, str(initial_day))
            meta_set(self.conn, "business_day", str(initial_day))
            meta_set(self.conn, INITIALIZED_KEY, "1")

            return {"initial_day": initial_day, "bottles": len(bottle_docs)}

    # -- reads --------------------------------------------------------------
    def query(self, bottle_id: str, day: int) -> dict:
        """Read-only allocation + validity for one bottle on ``day``."""
        require_day(day)
        bottle = bottles.fetch_bottle(self.conn, bottle_id)
        alloc = bottles.allocation(self.conn, bottle, day)
        return alloc.as_dict()

    def snapshot(self, day: int) -> list[dict]:
        require_day(day)
        rows = self.conn.execute(
            "SELECT bottle_id FROM bottles ORDER BY bottle_id"
        ).fetchall()
        return [
            bottles.allocation(
                self.conn, bottles.fetch_bottle(self.conn, r["bottle_id"]), day
            ).as_dict()
            for r in rows
        ]

    def conservation(self) -> list[dict]:
        return bottles.check_conservation(self.conn)

    # -- manual opening -----------------------------------------------------
    def open_bottle(self, bottle_id: str, day: int) -> dict:
        """Manually open an unopened bottle at the actual operation day.

        Never rewrites an existing opening day and never reverts to unopened.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            bottle = bottles.fetch_bottle(self.conn, bottle_id)
            if bottle.opened_day is not None:
                raise Conflict(
                    f"bottle {bottle_id} is already opened on day "
                    f"{bottle.opened_day}; opening day is immutable"
                )
            if day < bottle.intake_day:
                raise ValidationError(
                    f"cannot open bottle {bottle_id} before intake day "
                    f"{bottle.intake_day}"
                )
            if day >= bottle.printed_expiry_day:
                raise Conflict(
                    f"cannot open bottle {bottle_id} on/after printed expiry "
                    f"day {bottle.printed_expiry_day}"
                )
            self.conn.execute(
                "UPDATE bottles SET opened_day = ? WHERE bottle_id = ?",
                (day, bottle_id),
            )
            reopened = bottles.fetch_bottle(self.conn, bottle_id)
            return bottles.allocation(self.conn, reopened, day).as_dict()

    # -- reservation primitives (used by the run module) --------------------
    def reserve(
        self, holder_id: str, selection: Sequence[tuple[str, int]], day: int
    ) -> None:
        """Insert active reservations for a holder; caller owns the txn.

        A run passes its ``run_id`` here, which is why the run module and the
        inventory entry points share the same allocation rows.
        """
        require_day(day)
        require_text(holder_id, "holder_id")
        seen: set[str] = set()
        planned: list[tuple[BottleRow, int]] = []
        for bottle_id, portions in selection:
            require_text(bottle_id, "bottle_id")
            portions = require_int(portions, "portions", minimum=1)
            if bottle_id in seen:
                raise ValidationError(
                    f"duplicate bottle {bottle_id!r} in one reservation"
                )
            seen.add(bottle_id)
            bottle = bottles.fetch_bottle(self.conn, bottle_id)
            _check_reservable(bottle, day, portions, self.conn)
            planned.append((bottle, portions))

        for index, (bottle, portions) in enumerate(planned):
            self.conn.execute(
                "INSERT INTO reservations(reservation_id, holder_id, bottle_id, "
                "portions, status, created_day) "
                "VALUES(?, ?, ?, ?, 'active', ?)",
                (
                    create_reservation_id(self.conn, holder_id, index),
                    holder_id,
                    bottle.bottle_id,
                    portions,
                    day,
                ),
            )

    # -- independent entry points ------------------------------------------
    def hold(
        self, holder_id: str, selection: Sequence[tuple[str, int] | list], day: int
    ) -> dict:
        """Standalone reservation entry point, usable without a run.

        Reserving never opens a bottle and never spends shelf life.
        """
        require_text(holder_id, "holder_id")
        pairs = _normalize_selection(selection)
        with transaction(self.conn):
            advance_day(self.conn, day)
            self._require_standalone_holder(holder_id)
            self.reserve(holder_id, pairs, day)
            return self.holder_view(holder_id, day)

    def consume_held(self, holder_id: str, day: int) -> list[dict]:
        """Standalone consume entry point: active reservations -> consumed.

        An unopened bottle receives its first opening day equal to this actual
        use day; an already opened bottle keeps its opening day. On failure
        the whole request rolls back and leaves no opening fact behind.
        """
        with transaction(self.conn):
            advance_day(self.conn, day)
            self._require_standalone_holder(holder_id)
            return self.consume(holder_id, day)

    def release_held(self, holder_id: str, day: int, reason: str) -> int:
        with transaction(self.conn):
            advance_day(self.conn, day)
            self._require_standalone_holder(holder_id)
            return self.release(holder_id, day, reason)

    def _require_standalone_holder(self, holder_id: str) -> None:
        """Keep run state transitions inside the run service.

        Runs use their own ``run_id`` as holder; an external holder may not
        take that identity, otherwise it could settle a run's reservations
        behind the run state machine's back.
        """
        if self.conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (holder_id,)
        ).fetchone():
            raise Conflict(
                f"holder {holder_id!r} is a run; use the run entry points"
            )

    def active_reservations(self, holder_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reservations WHERE holder_id = ? AND status = 'active' "
            "ORDER BY reservation_id",
            (holder_id,),
        ).fetchall()

    def all_reservations(self, holder_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reservations WHERE holder_id = ? "
            "ORDER BY reservation_id",
            (holder_id,),
        ).fetchall()

    def consume(self, holder_id: str, day: int) -> list[dict]:
        """Atomically convert all active reservations of a holder to consumed.

        All validity checks are done by the caller *before* this is invoked;
        nevertheless we refuse to consume an unusable bottle. An unopened
        bottle gets its first opening day set to this actual use day; an
        already opened bottle keeps its original opening day.
        """
        require_day(day)
        rows = self.active_reservations(holder_id)
        if not rows:
            raise Conflict(f"holder {holder_id} has no active reservations")
        consumed: list[dict] = []
        for row in rows:
            bottle = bottles.fetch_bottle(self.conn, row["bottle_id"])
            if not bottle.usable_on(day):
                raise Conflict(
                    f"bottle {bottle.bottle_id} became unusable on day {day}"
                )
            self.conn.execute(
                "UPDATE reservations SET status = 'consumed', settled_day = ? "
                "WHERE reservation_id = ? AND status = 'active'",
                (day, row["reservation_id"]),
            )
            if bottle.opened_day is None:
                self.conn.execute(
                    "UPDATE bottles SET opened_day = ? WHERE bottle_id = ? "
                    "AND opened_day IS NULL",
                    (day, bottle.bottle_id),
                )
            consumed.append(
                {
                    "reservation_id": row["reservation_id"],
                    "bottle_id": bottle.bottle_id,
                    "portions": row["portions"],
                    "opened_now": bottle.opened_day is None,
                }
            )
        return consumed

    def release(self, holder_id: str, day: int, reason: str) -> int:
        """Release every active reservation of a holder; no bottle is opened."""
        require_day(day)
        require_text(reason, "reason")
        rows = self.active_reservations(holder_id)
        for row in rows:
            self.conn.execute(
                "UPDATE reservations SET status = 'released', settled_day = ?, "
                "release_reason = ? "
                "WHERE reservation_id = ? AND status = 'active'",
                (day, reason, row["reservation_id"]),
            )
        return len(rows)

    def holder_view(self, holder_id: str, day: int) -> dict:
        """Allocation facts for a holder, all derived from reservation rows."""
        rows = self.conn.execute(
            """
            SELECT r.reservation_id, r.bottle_id, b.kind, r.portions,
                   r.status, r.created_day, r.settled_day, r.release_reason
              FROM reservations r JOIN bottles b ON b.bottle_id = r.bottle_id
             WHERE r.holder_id = ?
             ORDER BY r.reservation_id
            """,
            (holder_id,),
        ).fetchall()
        per_kind: dict[str, dict[str, int]] = {}
        bottle_ids: set[str] = set()
        for row in rows:
            kind = row["kind"]
            per_kind.setdefault(kind, {"active": 0, "consumed": 0, "released": 0})
            per_kind[kind][row["status"]] += int(row["portions"])
            bottle_ids.add(row["bottle_id"])
        bottles_today: dict[str, dict] = {}
        for bottle_id in sorted(bottle_ids):
            bottles_today[bottle_id] = bottles.allocation(
                self.conn, bottles.fetch_bottle(self.conn, bottle_id), day
            ).as_dict()
        return {
            "holder_id": holder_id,
            "day": day,
            "per_kind": per_kind,
            "bottles": bottles_today,
            "reservations": [
                {
                    "reservation_id": row["reservation_id"],
                    "bottle_id": row["bottle_id"],
                    "portions": row["portions"],
                    "status": row["status"],
                    "created_day": row["created_day"],
                    "settled_day": row["settled_day"],
                    "release_reason": row["release_reason"],
                }
                for row in rows
            ],
        }

    def run_allocations(self, holder_id: str) -> dict:
        """Per-kind active/consumed totals for a holder (derived from rows)."""
        rows = self.conn.execute(
            """
            SELECT b.kind, r.status, COALESCE(SUM(r.portions), 0) AS n
              FROM reservations r JOIN bottles b ON b.bottle_id = r.bottle_id
             WHERE r.holder_id = ?
             GROUP BY b.kind, r.status
            """,
            (holder_id,),
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(row["kind"], {"active": 0, "consumed": 0, "released": 0})
            out[row["kind"]][row["status"]] = int(row["n"])
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _check_reservable(
    bottle: BottleRow, day: int, portions: int, conn: sqlite3.Connection
) -> None:
    if not bottle.usable_on(day):
        raise InsufficientStock(
            f"bottle {bottle.bottle_id} is not usable on day {day} "
            f"(status {bottle.validity(day)})"
        )
    alloc = bottles.allocation(conn, bottle, day)
    if alloc.unallocated < portions:
        raise InsufficientStock(
            f"bottle {bottle.bottle_id} has {alloc.unallocated} unallocated "
            f"portions, requested {portions}"
        )


def create_reservation_id(
    conn: sqlite3.Connection, holder_id: str, index: int
) -> str:
    """Deterministic, collision-free reservation id within a holder."""
    base = f"{holder_id}#{index}"
    candidate = base
    suffix = 1
    while conn.execute(
        "SELECT 1 FROM reservations WHERE reservation_id = ?", (candidate,)
    ).fetchone() is not None:
        candidate = f"{base}~{suffix}"
        suffix += 1
    return candidate


def _normalize_selection(
    selection: Sequence[tuple[str, int] | list | dict],
) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for item in selection:
        if isinstance(item, dict):
            pairs.append((item.get("bottle_id"), item.get("portions")))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((item[0], item[1]))
        else:
            raise ValidationError(f"invalid selection entry {item!r}")
    return pairs


# ---------------------------------------------------------------------------
# document validation
# ---------------------------------------------------------------------------
def _validate_kinds(raw: object) -> dict[str, int]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("kinds must be a non-empty list")
    kinds: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"kind entry must be an object: {item!r}")
        kind = require_text(item.get("kind"), "kind")
        if kind in kinds:
            raise ValidationError(f"duplicate kind {kind!r}")
        lifetime = require_int(
            item.get("open_lifetime_days"), "open_lifetime_days", minimum=1
        )
        kinds[kind] = lifetime
    return kinds


def _validate_bottles(
    raw: object, kinds: dict[str, int], initial_day: int
) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("bottles must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"bottle entry must be an object: {item!r}")
        bottle_id = require_text(item.get("bottle_id"), "bottle_id")
        if bottle_id in seen:
            raise ValidationError(f"duplicate bottle_id {bottle_id!r}")
        seen.add(bottle_id)
        kind = require_text(item.get("kind"), "kind")
        if kind not in kinds:
            raise ValidationError(f"bottle {bottle_id} has unknown kind {kind!r}")
        total = require_int(item.get("total_portions"), "total_portions", minimum=1)
        intake = require_day(item.get("intake_day"), "intake_day")
        printed = require_day(item.get("printed_expiry_day"), "printed_expiry_day")
        if printed <= intake:
            raise ValidationError(
                f"bottle {bottle_id}: printed_expiry_day {printed} must be "
                f"after intake_day {intake}"
            )
        opened = item.get("opened_day")
        if opened is not None:
            opened = require_day(opened, "opened_day")
            if opened < intake:
                raise ValidationError(
                    f"bottle {bottle_id}: opened_day {opened} precedes "
                    f"intake_day {intake}"
                )
            if opened > initial_day:
                raise ValidationError(
                    f"bottle {bottle_id}: initial opened_day {opened} must be "
                    f"within intake day and initial business day {initial_day}"
                )
            if opened >= printed:
                raise ValidationError(
                    f"bottle {bottle_id}: opened_day {opened} must be before "
                    f"printed_expiry_day {printed}"
                )
        out.append(
            {
                "bottle_id": bottle_id,
                "kind": kind,
                "total_portions": total,
                "intake_day": intake,
                "printed_expiry_day": printed,
                "opened_day": opened,
            }
        )
    return out


def _validate_methods(
    raw: object, kinds: dict[str, int]
) -> dict[str, dict[int, dict[str, int]]]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("methods must be a non-empty list")
    out: dict[str, dict[int, dict[str, int]]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError(f"method entry must be an object: {item!r}")
        method_id = require_text(item.get("method_id"), "method_id")
        revision = require_int(item.get("revision"), "revision", minimum=1)
        requirements = _validate_requirements(item.get("requirements"), kinds)
        revisions = out.setdefault(method_id, {})
        if revision in revisions:
            raise ValidationError(
                f"duplicate method {method_id!r} revision {revision}"
            )
        revisions[revision] = requirements
    return out


def _validate_requirements(raw: object, kinds: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("method requirements must be a non-empty object")
    out: dict[str, int] = {}
    for kind, portions in raw.items():
        require_text(kind, "requirement kind")
        if kind not in kinds:
            raise ValidationError(f"requirement has unknown kind {kind!r}")
        out[kind] = require_int(portions, f"requirement portions for {kind}", minimum=1)
    return out


def fetch_method_requirements(
    conn: sqlite3.Connection, method_id: str, revision: int
) -> dict[str, int]:
    method_id = require_text(method_id, "method_id")
    revision = require_int(revision, "revision", minimum=1)
    method = conn.execute(
        "SELECT 1 FROM methods WHERE method_id = ? AND revision = ?",
        (method_id, revision),
    ).fetchone()
    if method is None:
        raise NotFoundError(f"unknown method {method_id!r} revision {revision}")
    rows = conn.execute(
        "SELECT kind, portions FROM method_requirements "
        "WHERE method_id = ? AND revision = ? ORDER BY kind",
        (method_id, revision),
    ).fetchall()
    return {row["kind"]: int(row["portions"]) for row in rows}
