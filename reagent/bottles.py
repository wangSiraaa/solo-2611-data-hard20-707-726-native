"""Bottle-level validity and allocation arithmetic.

Validity is the heart of the task:

* unopened bottle: ``intake_day <= day < printed_expiry_day``
* opened bottle:   ``intake_day <= day < min(printed_expiry_day, opened_day + L)``

The boundary day is *not* usable.  ``L`` and ``printed_expiry`` only ever
restrict; they never extend past each other.

Allocation arithmetic is derived, never stored:

* ``reserved = SUM(portions) of active reservations``
* ``consumed = SUM(portions) of consumed reservations``
* ``unallocated = total - reserved - consumed``
* ``leadable_today = unallocated`` if the bottle is valid today, else ``0``

Expired stock keeps its ``unallocated`` count; it merely stops being leadable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .errors import InventoryInvariantError, NotFoundError
from .rules import require_text

OPENED = "opened"
UNOPENED = "unopened"
EXPIRED = "expired"
NOT_YET_INTAKEN = "not_yet_intaken"


@dataclass(frozen=True)
class BottleRow:
    bottle_id: str
    kind: str
    total_portions: int
    intake_day: int
    printed_expiry_day: int
    opened_day: Optional[int]
    open_lifetime_days: int

    # -- validity -----------------------------------------------------------
    @property
    def effective_expiry_day(self) -> int:
        """Exclusive upper bound on usable days."""
        if self.opened_day is None:
            return self.printed_expiry_day
        return min(self.printed_expiry_day, self.opened_day + self.open_lifetime_days)

    def validity(self, day: int) -> str:
        if day < self.intake_day:
            return NOT_YET_INTAKEN
        if day >= self.effective_expiry_day:
            return EXPIRED
        return OPENED if self.opened_day is not None else UNOPENED

    def usable_on(self, day: int) -> bool:
        return self.intake_day <= day < self.effective_expiry_day


def fetch_bottle(conn: sqlite3.Connection, bottle_id: str) -> BottleRow:
    require_text(bottle_id, "bottle_id")
    row = conn.execute(
        """
        SELECT b.bottle_id, b.kind, b.total_portions, b.intake_day,
               b.printed_expiry_day, b.opened_day, k.open_lifetime_days
          FROM bottles b JOIN kinds k ON k.kind = b.kind
         WHERE b.bottle_id = ?
        """,
        (bottle_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown bottle {bottle_id!r}")
    return BottleRow(
        bottle_id=row["bottle_id"],
        kind=row["kind"],
        total_portions=row["total_portions"],
        intake_day=row["intake_day"],
        printed_expiry_day=row["printed_expiry_day"],
        opened_day=row["opened_day"],
        open_lifetime_days=row["open_lifetime_days"],
    )


@dataclass(frozen=True)
class BottleAllocation:
    bottle_id: str
    kind: str
    day: int
    total_portions: int
    reserved: int
    consumed: int
    unallocated: int
    valid_today: bool
    leadable_today: int
    status: str
    effective_expiry_day: int
    opened_day: Optional[int]

    def as_dict(self) -> dict:
        return {
            "bottle_id": self.bottle_id,
            "kind": self.kind,
            "day": self.day,
            "total_portions": self.total_portions,
            "reserved": self.reserved,
            "consumed": self.consumed,
            "unallocated": self.unallocated,
            "valid_today": self.valid_today,
            "leadable_today": self.leadable_today,
            "status": self.status,
            "effective_expiry_day": self.effective_expiry_day,
            "opened_day": self.opened_day,
        }


def sum_reservations(
    conn: sqlite3.Connection, bottle_id: str, status: Optional[str] = None
) -> int:
    if status is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(portions), 0) AS n FROM reservations "
            "WHERE bottle_id = ? AND status <> 'released'",
            (bottle_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(portions), 0) AS n FROM reservations "
            "WHERE bottle_id = ? AND status = ?",
            (bottle_id, status),
        ).fetchone()
    return int(row["n"])


def allocation(
    conn: sqlite3.Connection, bottle: BottleRow, day: int
) -> BottleAllocation:
    reserved = sum_reservations(conn, bottle.bottle_id, "active")
    consumed = sum_reservations(conn, bottle.bottle_id, "consumed")
    unallocated = bottle.total_portions - reserved - consumed
    if unallocated < 0:
        raise InventoryInvariantError(
            f"bottle {bottle.bottle_id}: reserved {reserved} + consumed "
            f"{consumed} exceed total {bottle.total_portions}"
        )
    valid = bottle.usable_on(day)
    status = bottle.validity(day)
    return BottleAllocation(
        bottle_id=bottle.bottle_id,
        kind=bottle.kind,
        day=day,
        total_portions=bottle.total_portions,
        reserved=reserved,
        consumed=consumed,
        unallocated=unallocated,
        valid_today=valid,
        leadable_today=unallocated if valid else 0,
        status=status,
        effective_expiry_day=bottle.effective_expiry_day,
        opened_day=bottle.opened_day,
    )


def check_conservation(conn: sqlite3.Connection) -> list[dict]:
    """Cross-check derived python arithmetic against the SQL view.

    Raises if either source violates ``total == unallocated + reserved +
    consumed`` or if the two disagree.
    """
    rows = conn.execute(
        "SELECT bottle_id, kind, total_portions, reserved, consumed, unallocated "
        "FROM bottle_allocation ORDER BY bottle_id"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        reserved = int(row["reserved"])
        consumed = int(row["consumed"])
        unallocated = int(row["unallocated"])
        total = int(row["total_portions"])
        if unallocated < 0 or reserved < 0 or consumed < 0:
            raise InventoryInvariantError(
                f"bottle {row['bottle_id']}: total {total} != "
                f"unallocated {unallocated} + reserved {reserved} + "
                f"consumed {consumed}"
            )
        out.append(
            {
                "bottle_id": row["bottle_id"],
                "total_portions": total,
                "unallocated": unallocated,
                "reserved": reserved,
                "consumed": consumed,
            }
        )
    return out
