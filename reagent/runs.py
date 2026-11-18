"""Run service: fixed method revisions, atomic reserve/start/release.

State machine::

    (create) -> reserved --start ok--> started   [terminal]
                    |
                    +--start invalid--> released [terminal]
                    +--cancel--------> released [terminal]

Terminal runs store a receipt in ``runs.terminal_json``. Replaying a start (or
a release) returns that stored receipt verbatim: it is never recomputed from
today's stock. Terminal runs can never consume, release or return again.

Runs hold no private quantity counters. Every quantity in a run view is a
GROUP BY over the ``reservations`` rows that the inventory service owns.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Sequence

from . import bottles
from .errors import Conflict, NotFoundError, ValidationError
from .inventory import InventoryService, _validate_requirements, fetch_method_requirements
from .rules import advance_day, require_int, require_text
from .schema import transaction

RESERVED = "reserved"
STARTED = "started"
RELEASED = "released"
TERMINAL_STATES = (STARTED, RELEASED)


class RunService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.inventory = InventoryService(conn)

    # -- method revisions ---------------------------------------------------
    def revise_method(
        self, method_id: str, revision: int, requirements: dict, day: int
    ) -> dict:
        """Register a new method revision. Existing runs keep their snapshot."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            method_id = require_text(method_id, "method_id")
            revision = require_int(revision, "revision", minimum=1)
            kinds = {
                row["kind"]: row["open_lifetime_days"]
                for row in self.conn.execute("SELECT * FROM kinds")
            }
            reqs = _validate_requirements(requirements, kinds)
            row = self.conn.execute(
                "SELECT MAX(revision) AS m FROM methods WHERE method_id = ?",
                (method_id,),
            ).fetchone()
            current_max = row["m"]
            if current_max is not None and revision <= current_max:
                raise Conflict(
                    f"method {method_id}: revision {revision} must be greater "
                    f"than existing revision {current_max}"
                )
            self.conn.execute(
                "INSERT INTO methods(method_id, revision) VALUES(?, ?)",
                (method_id, revision),
            )
            for kind, portions in reqs.items():
                self.conn.execute(
                    "INSERT INTO method_requirements"
                    "(method_id, revision, kind, portions) VALUES(?, ?, ?, ?)",
                    (method_id, revision, kind, portions),
                )
            return {
                "method_id": method_id,
                "revision": revision,
                "requirements": reqs,
            }

    # -- create -------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        method_id: str,
        method_revision: int,
        day: int,
        selection: Sequence[dict],
    ) -> dict:
        """Validate demand + availability, then reserve everything at once."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            run_id = require_text(run_id, "run_id")
            existing = self.conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                raise Conflict(f"run {run_id!r} already exists")
            if self.conn.execute(
                "SELECT 1 FROM reservations WHERE holder_id = ? LIMIT 1",
                (run_id,),
            ).fetchone():
                raise Conflict(
                    f"holder {run_id!r} already has reservations outside a run"
                )

            requirements = fetch_method_requirements(
                self.conn, method_id, method_revision
            )
            pairs = _match_selection(self.conn, selection, requirements)

            demand_json = json.dumps(
                {kind: requirements[kind] for kind in sorted(requirements)},
                sort_keys=True,
            )
            self.conn.execute(
                "INSERT INTO runs(run_id, method_id, method_revision, "
                "creation_day, demand_json, state) "
                "VALUES(?, ?, ?, ?, ?, 'reserved')",
                (run_id, method_id, method_revision, day, demand_json),
            )
            for kind, portions in requirements.items():
                self.conn.execute(
                    "INSERT INTO run_requirements(run_id, kind, portions) "
                    "VALUES(?, ?, ?)",
                    (run_id, kind, portions),
                )
            # Inventory owns the allocation rows. A failure here rolls back
            # the run row too: no half-created order.
            self.inventory.reserve(run_id, pairs, day)
            return self.get_run(run_id)

    # -- start --------------------------------------------------------------
    def start_run(self, run_id: str, day: int) -> dict:
        """Recheck today's validity, then consume all reservations atomically."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            run = self._fetch_run(run_id)
            if run["state"] in TERMINAL_STATES:
                return _replay(run)
            if run["state"] != RESERVED:
                raise Conflict(f"run {run_id} cannot be started from {run['state']}")

            active = self.inventory.active_reservations(run_id)
            if not active:
                raise Conflict(f"run {run_id} has no active reservations")

            invalid: list[str] = []
            for row in active:
                bottle = bottles.fetch_bottle(self.conn, row["bottle_id"])
                if not bottle.usable_on(day):
                    invalid.append(
                        f"{bottle.bottle_id} ({bottle.validity(day)}, effective "
                        f"expiry {bottle.effective_expiry_day})"
                    )

            if invalid:
                # Expiry makes the start fail: release *all* reservations of
                # this run, record the reason and make it terminal. No bottle
                # gets an opening fact from this failed attempt.
                reason = f"start failed on day {day}: unusable bottle(s): " + ", ".join(
                    sorted(invalid)
                )
                released = self.inventory.release(run_id, day, reason)
                receipt = {
                    "run_id": run_id,
                    "state": RELEASED,
                    "released_day": day,
                    "reason": reason,
                    "released_reservations": released,
                    "released": [
                        {
                            "reservation_id": row["reservation_id"],
                            "bottle_id": row["bottle_id"],
                            "portions": row["portions"],
                        }
                        for row in active
                    ],
                }
                self.conn.execute(
                    "UPDATE runs SET state = 'released', failure_reason = ?, "
                    "terminal_day = ?, terminal_json = ? WHERE run_id = ?",
                    (reason, day, json.dumps(receipt, sort_keys=True), run_id),
                )
                return _replay(self._fetch_run(run_id), replayed=False)

            consumed = self.inventory.consume(run_id, day)
            receipt = {
                "run_id": run_id,
                "state": STARTED,
                "started_day": day,
                "reason": None,
                "consumed": consumed,
            }
            self.conn.execute(
                "UPDATE runs SET state = 'started', started_day = ?, "
                "terminal_day = ?, terminal_json = ? WHERE run_id = ?",
                (day, day, json.dumps(receipt, sort_keys=True), run_id),
            )
            return _replay(self._fetch_run(run_id), replayed=False)

    # -- release ------------------------------------------------------------
    def release_run(self, run_id: str, day: int, reason: str) -> dict:
        """Cancel a still-reserved run. Terminal runs are never re-released."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            run = self._fetch_run(run_id)
            if run["state"] == RELEASED:
                return _replay(run)
            if run["state"] == STARTED:
                raise Conflict(
                    f"run {run_id} has already started and cannot be released"
                )
            require_text(reason, "reason")
            active = self.inventory.active_reservations(run_id)
            released = self.inventory.release(run_id, day, reason)
            receipt = {
                "run_id": run_id,
                "state": RELEASED,
                "released_day": day,
                "reason": reason,
                "released_reservations": released,
                "released": [
                    {
                        "reservation_id": row["reservation_id"],
                        "bottle_id": row["bottle_id"],
                        "portions": row["portions"],
                    }
                    for row in active
                ],
            }
            self.conn.execute(
                "UPDATE runs SET state = 'released', failure_reason = ?, "
                "terminal_day = ?, terminal_json = ? WHERE run_id = ?",
                (reason, day, json.dumps(receipt, sort_keys=True), run_id),
            )
            return _replay(self._fetch_run(run_id), replayed=False)

    # -- reads --------------------------------------------------------------
    def get_run(self, run_id: str) -> dict:
        run = self._fetch_run(run_id)
        requirements = {
            row["kind"]: int(row["portions"])
            for row in self.conn.execute(
                "SELECT kind, portions FROM run_requirements WHERE run_id = ? "
                "ORDER BY kind",
                (run_id,),
            )
        }
        allocations = self.inventory.run_allocations(run_id)
        reservations = [
            {
                "reservation_id": row["reservation_id"],
                "bottle_id": row["bottle_id"],
                "portions": row["portions"],
                "status": row["status"],
                "created_day": row["created_day"],
                "settled_day": row["settled_day"],
                "release_reason": row["release_reason"],
            }
            for row in self.inventory.all_reservations(run_id)
        ]
        return {
            "run_id": run["run_id"],
            "method_id": run["method_id"],
            "method_revision": run["method_revision"],
            "creation_day": run["creation_day"],
            "state": run["state"],
            "failure_reason": run["failure_reason"],
            "started_day": run["started_day"],
            "terminal_day": run["terminal_day"],
            "requirements": requirements,
            "allocations": allocations,
            "reservations": reservations,
            "terminal": json.loads(run["terminal_json"])
            if run["terminal_json"]
            else None,
        }

    def _fetch_run(self, run_id: str) -> sqlite3.Row:
        require_text(run_id, "run_id")
        run = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise NotFoundError(f"unknown run {run_id!r}")
        return run


def _replay(run: sqlite3.Row, replayed: bool = True) -> dict:
    """Return the stored terminal receipt unchanged, flagged as a replay."""
    if not run["terminal_json"]:
        raise Conflict(f"run {run['run_id']} has no terminal receipt")
    receipt = json.loads(run["terminal_json"])
    receipt["replayed"] = replayed
    return receipt


def _match_selection(
    conn: sqlite3.Connection,
    selection: Sequence[dict],
    requirements: dict[str, int],
) -> list[tuple[str, int]]:
    """Validate that selections per kind exactly equal the method demand."""
    if not isinstance(selection, (list, tuple)) or not selection:
        raise ValidationError("selection must be a non-empty list")
    seen: set[str] = set()
    kind_totals: dict[str, int] = {}
    pairs: list[tuple[str, int]] = []
    for item in selection:
        if not isinstance(item, dict):
            raise ValidationError(f"selection entry must be an object: {item!r}")
        bottle_id = require_text(item.get("bottle_id"), "bottle_id")
        portions = require_int(item.get("portions"), "portions", minimum=1)
        if bottle_id in seen:
            raise ValidationError(
                f"bottle {bottle_id!r} selected more than once in one run"
            )
        seen.add(bottle_id)
        bottle = bottles.fetch_bottle(conn, bottle_id)
        kind_totals[bottle.kind] = kind_totals.get(bottle.kind, 0) + portions
        pairs.append((bottle_id, portions))

    for kind, portions in kind_totals.items():
        if kind not in requirements:
            raise ValidationError(
                f"selection uses kind {kind!r} which method does not require"
            )
        if portions != requirements[kind]:
            raise ValidationError(
                f"kind {kind}: selected {portions} portions but method "
                f"requires {requirements[kind]}"
            )
    for kind, portions in requirements.items():
        got = kind_totals.get(kind, 0)
        if got != portions:
            raise ValidationError(
                f"kind {kind}: selected {got} portions but method requires "
                f"{portions}"
            )
    return pairs
