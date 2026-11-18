"""Read-only snapshot, compatible with the supplied ``ledger_reader.py``.

The extra tables the service adds are appended, and every pre-existing table is
read with its original columns so the meaning of the history stays legible.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import db

# (table, order-by) exactly as the material reader expects; the service's
# additive tables follow.
BASE_TABLES = [
    ("coupon_instances", "instance_id"),
    ("redemptions", "order_id"),
    ("redemption_uses", "order_id,instance_id"),
    ("cancellations", "cancel_id"),
]
EXTRA_TABLES = [
    ("request_results", "request_id"),
    ("cancellation_results", "cancel_id"),
    ("coupon_templates", "template_id,version"),
    ("current_templates", "template_id"),
]


def snapshot(path: str | Path, *, include_extra: bool = True) -> dict[str, list[dict[str, Any]]]:
    con = db.connect(path, readonly=True)
    try:
        return snapshot_connection(con, include_extra=include_extra)
    finally:
        con.close()


def snapshot_connection(
    con: sqlite3.Connection, *, include_extra: bool = True
) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = {}
    tables = list(BASE_TABLES)
    if include_extra:
        tables.extend(EXTRA_TABLES)
    for table, order in tables:
        if not db.has_table(con, table):
            data[table] = []
            continue
        data[table] = [
            dict(row)
            for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]
    return data


def legacy_snapshot(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Exactly the four tables read by the material's read-only client."""

    return snapshot(path, include_extra=False)
