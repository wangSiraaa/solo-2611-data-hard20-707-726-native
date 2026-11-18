"""Replayable LEFT JOIN derived store.

The store reads R/S *only* from the independent source catalog at one pinned
source version.  It never keeps a "current source" copy of its own: every
result is bound to ``(source_version, source_fingerprint)`` and stores the
output counts plus the provenance tuples/multipliers that produced them.

Rebuild policy
--------------
Each derive performs a full rebuild from one immutable source slice.  At the
stated scale (10k distinct source tuples per table, 500 changes per batch)
this is cheap and, more importantly, it makes the three classic incremental
mistakes impossible:

* when a sku moves from "no match" to "has match", the old synthetic NULL row
  simply does not exist in the rebuild, so it cannot survive alongside the new
  matched rows;
* when a batch touches both R and S, both net counts are read from the same
  accepted slice, so cross terms are never missed;
* a tuple whose final count is zero is absent from the slice, so it cannot
  contribute a row at all.

An affected-sku incremental path would have to explicitly evict old rows,
delete/re-add the NULL row on match-set transitions, and recompute both sides
of a changed sku; full rebuild buys that correctness for a linear scan.  The
stored result is still queryable by result/region/sku through indexes, so read
cost does not depend on rebuild cost.  Old results are retained verbatim.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .catalog import SourceCatalog
from .errors import (
    CatalogError,
    ResultConflictError,
    UnknownResultError,
    ValidationError,
)
from .model import MAX_COUNT, SOURCE_R, SOURCE_S, SourceRow, is_int, slice_fingerprint


DERIVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS derived_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    result_id           TEXT PRIMARY KEY,
    source_version      INTEGER NOT NULL,
    source_fingerprint  TEXT    NOT NULL,
    built_at            TEXT    NOT NULL,
    derived_row_count   INTEGER NOT NULL,
    quoted_row_count    INTEGER NOT NULL,
    unquoted_row_count  INTEGER NOT NULL,
    quoted_amount       INTEGER NOT NULL,
    output_tuple_count  INTEGER NOT NULL,
    provenance_count    INTEGER NOT NULL,
    rebuild_policy      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS result_rows (
    result_id     TEXT    NOT NULL REFERENCES results(result_id) ON DELETE CASCADE,
    region        TEXT    NOT NULL,
    sku           TEXT    NOT NULL,
    quantity      INTEGER NOT NULL,
    is_quoted     INTEGER NOT NULL CHECK (is_quoted IN (0,1)),
    supplier_key  TEXT    NOT NULL,  -- '' when unquoted; avoids NULL PK semantics
    price_key     INTEGER NOT NULL,  -- 0 when unquoted
    supplier      TEXT,
    price         INTEGER,
    multiplicity  INTEGER NOT NULL CHECK (multiplicity > 0),
    PRIMARY KEY (result_id, region, sku, quantity, is_quoted, supplier_key, price_key),
    CHECK ((is_quoted = 1 AND supplier IS NOT NULL AND price IS NOT NULL)
        OR (is_quoted = 0 AND supplier IS NULL AND price IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_result_rows_region
    ON result_rows(result_id, region, sku);

CREATE TABLE IF NOT EXISTS result_provenance (
    provenance_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id           TEXT    NOT NULL REFERENCES results(result_id) ON DELETE CASCADE,
    region              TEXT    NOT NULL,
    sku                 TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    is_quoted           INTEGER NOT NULL CHECK (is_quoted IN (0,1)),
    supplier_key        TEXT    NOT NULL,
    price_key           INTEGER NOT NULL,
    output_multiplicity INTEGER NOT NULL CHECK (output_multiplicity > 0),
    left_region         TEXT    NOT NULL,
    left_sku            TEXT    NOT NULL,
    left_quantity       INTEGER NOT NULL,
    left_multiplicity   INTEGER NOT NULL,
    right_sku           TEXT,
    right_supplier      TEXT,
    right_price         INTEGER,
    right_multiplicity  INTEGER,
    contribution        INTEGER NOT NULL CHECK (contribution > 0)
);
CREATE INDEX IF NOT EXISTS ix_provenance_output
    ON result_provenance(result_id, region, sku, is_quoted, supplier_key, price_key);
CREATE INDEX IF NOT EXISTS ix_provenance_left
    ON result_provenance(result_id, left_sku, left_region, left_quantity);

CREATE TABLE IF NOT EXISTS region_summary (
    result_id           TEXT    NOT NULL REFERENCES results(result_id) ON DELETE CASCADE,
    region              TEXT    NOT NULL,
    derived_row_count   INTEGER NOT NULL,
    quoted_row_count    INTEGER NOT NULL,
    unquoted_row_count  INTEGER NOT NULL,
    quoted_amount       INTEGER NOT NULL,
    output_tuple_count  INTEGER NOT NULL,
    PRIMARY KEY (result_id, region)
);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def _check_range(value: int, label: str) -> int:
    if value > MAX_COUNT:
        raise ValidationError(f"{label} exceeds SQLite signed 64-bit range")
    return value


@dataclass(frozen=True)
class OutputKey:
    region: str
    sku: str
    quantity: int
    is_quoted: bool
    supplier: str | None
    price: int | None

    @property
    def supplier_key(self) -> str:
        return self.supplier if self.supplier is not None else ""

    @property
    def price_key(self) -> int:
        return self.price if self.price is not None else 0


@dataclass(frozen=True)
class Provenance:
    key: OutputKey
    left: SourceRow
    right: SourceRow | None
    contribution: int

    @property
    def right_multiplicity(self) -> int | None:
        return self.right.multiplicity if self.right else None


def derive_slice(r_rows: list[SourceRow], s_rows: list[SourceRow]) -> dict:
    """Pure LEFT JOIN computation over explicit R/S multisets.

    ``R LEFT JOIN S USING(sku)`` on multisets: a left tuple with multiplicity
    ``mR`` and a right tuple with multiplicity ``mS`` contributes
    ``mR * mS`` to the joined output; an unmatched left tuple contributes
    ``mR`` to a single synthetic ``(NULL, NULL)`` output.  Equal business
    outputs are added, never deduplicated to one.
    """
    by_sku: dict[str, list[SourceRow]] = {}
    for row in s_rows:
        by_sku.setdefault(row.tuple[0], []).append(row)

    outputs: dict[OutputKey, int] = {}
    provenance: list[Provenance] = []
    regions: dict[str, dict[str, int]] = {}

    def add(key: OutputKey, left: SourceRow, right: SourceRow | None, contribution: int) -> None:
        if contribution <= 0:
            raise ValidationError("derived contribution must be positive")
        _check_range(contribution, "derived contribution")
        new_total = outputs.get(key, 0) + contribution
        _check_range(new_total, "derived output multiplicity")
        outputs[key] = new_total
        provenance.append(Provenance(key, left, right, contribution))

        summary = regions.setdefault(
            key.region,
            {
                "derived_row_count": 0,
                "quoted_row_count": 0,
                "unquoted_row_count": 0,
                "quoted_amount": 0,
                "output_tuple_count": 0,
            },
        )
        summary["derived_row_count"] += contribution
        if key.is_quoted:
            summary["quoted_row_count"] += contribution
            amount = contribution * key.quantity * key.price  # type: ignore[operator]
            _check_range(amount, "quoted amount")
            summary["quoted_amount"] += amount
            _check_range(summary["quoted_amount"], "region quoted amount")
        else:
            summary["unquoted_row_count"] += contribution

    for left in sorted(r_rows):
        region, sku, quantity = left.tuple
        matches = by_sku.get(sku, [])
        if matches:
            for right in matches:
                _, supplier, price = right.tuple
                if left.multiplicity > MAX_COUNT // right.multiplicity:
                    raise ValidationError(
                        f"multiplicity product for R{left.tuple} x S{right.tuple} "
                        "exceeds signed 64-bit range"
                    )
                add(
                    OutputKey(region, sku, quantity, True, supplier, price),
                    left,
                    right,
                    left.multiplicity * right.multiplicity,
                )
        else:
            add(
                OutputKey(region, sku, quantity, False, None, None),
                left,
                None,
                left.multiplicity,
            )

    for key, count in outputs.items():
        regions[key.region]["output_tuple_count"] += 1

    totals = {
        "derived_row_count": sum(v["derived_row_count"] for v in regions.values()),
        "quoted_row_count": sum(v["quoted_row_count"] for v in regions.values()),
        "unquoted_row_count": sum(v["unquoted_row_count"] for v in regions.values()),
        "quoted_amount": sum(v["quoted_amount"] for v in regions.values()),
        "output_tuple_count": len(outputs),
        "provenance_count": len(provenance),
    }
    return {
        "outputs": outputs,
        "provenance": provenance,
        "regions": regions,
        "totals": totals,
    }


class DerivedStore:
    """A LEFT JOIN derived store over an independent source catalog."""

    def __init__(self, source_db: str | Path, derived_db: str | Path):
        self.source_db = str(source_db)
        self.derived_db = str(derived_db)
        self._conn = _connect(self.derived_db)
        self._conn.executescript(DERIVED_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO derived_meta(key, value) VALUES ('schema_version', '1')"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DerivedStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------- build
    def build_result(
        self,
        result_id: str,
        source_version: int | None = None,
        replace: bool = False,
    ) -> dict:
        """Build (or return) an immutable result pinned to a source slice.

        If ``result_id`` already exists it is returned unchanged.  A different
        requested version is a conflict; old results never migrate to a new
        source batch.
        """
        if not isinstance(result_id, str) or not result_id:
            raise ValidationError("result_id must be a non-empty string")

        existing = self._result_row(result_id)
        if existing is not None and not replace:
            if source_version is not None and int(existing["source_version"]) != source_version:
                raise ResultConflictError(
                    f"result_id {result_id!r} is already bound to source "
                    f"version {existing['source_version']}, not {source_version}"
                )
            return self.get_result(result_id, status="existing")

        with SourceCatalog(self.source_db) as catalog:
            version = catalog.resolve_version(source_version)
            r_rows = catalog.read_source_rows(SOURCE_R, version)
            s_rows = catalog.read_source_rows(SOURCE_S, version)
            fingerprint = slice_fingerprint(r_rows, s_rows)
            # The same immutable slice must hash identically whether read in
            # one go or through the catalog's own audit helper.
            if catalog.slice_fingerprint(version) != fingerprint:
                raise CatalogError(
                    f"source slice at version {version} changed while being read"
                )

        computed = derive_slice(r_rows, s_rows)
        built_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        totals = computed["totals"]

        with _immediate(self._conn):
            clash = self._result_row(result_id)
            if clash is not None:
                if not replace:
                    raise ResultConflictError(
                        f"result_id {result_id!r} was built concurrently"
                    )
                # Replacing happens in the same transaction as the insert, so
                # readers never observe a missing result.
                self._conn.execute(
                    "DELETE FROM results WHERE result_id = ?", (result_id,)
                )
            self._conn.execute(
                """
                INSERT INTO results
                    (result_id, source_version, source_fingerprint, built_at,
                     derived_row_count, quoted_row_count, unquoted_row_count,
                     quoted_amount, output_tuple_count, provenance_count,
                     rebuild_policy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    version,
                    fingerprint,
                    built_at,
                    totals["derived_row_count"],
                    totals["quoted_row_count"],
                    totals["unquoted_row_count"],
                    totals["quoted_amount"],
                    totals["output_tuple_count"],
                    totals["provenance_count"],
                    "full_rebuild_from_pinned_slice",
                ),
            )
            self._conn.executemany(
                """
                INSERT INTO result_rows
                    (result_id, region, sku, quantity, is_quoted, supplier_key,
                     price_key, supplier, price, multiplicity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result_id,
                        key.region,
                        key.sku,
                        key.quantity,
                        1 if key.is_quoted else 0,
                        key.supplier_key,
                        key.price_key,
                        key.supplier,
                        key.price,
                        count,
                    )
                    for key, count in computed["outputs"].items()
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO result_provenance
                    (result_id, region, sku, quantity, is_quoted, supplier_key,
                     price_key, output_multiplicity, left_region, left_sku,
                     left_quantity, left_multiplicity, right_sku, right_supplier,
                     right_price, right_multiplicity, contribution)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result_id,
                        prov.key.region,
                        prov.key.sku,
                        prov.key.quantity,
                        1 if prov.key.is_quoted else 0,
                        prov.key.supplier_key,
                        prov.key.price_key,
                        computed["outputs"][prov.key],
                        prov.left.tuple[0],
                        prov.left.tuple[1],
                        prov.left.tuple[2],
                        prov.left.multiplicity,
                        prov.right.tuple[0] if prov.right else None,
                        prov.right.tuple[1] if prov.right else None,
                        prov.right.tuple[2] if prov.right else None,
                        prov.right_multiplicity,
                        prov.contribution,
                    )
                    for prov in computed["provenance"]
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO region_summary
                    (result_id, region, derived_row_count, quoted_row_count,
                     unquoted_row_count, quoted_amount, output_tuple_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result_id,
                        region,
                        summary["derived_row_count"],
                        summary["quoted_row_count"],
                        summary["unquoted_row_count"],
                        summary["quoted_amount"],
                        summary["output_tuple_count"],
                    )
                    for region, summary in sorted(computed["regions"].items())
                ],
            )
        return self.get_result(result_id, status="built")

    # ------------------------------------------------------------------- query
    def get_result(self, result_id: str, status: str = "existing") -> dict:
        row = self._result_row(result_id)
        if row is None:
            raise UnknownResultError(f"result_id {result_id!r} does not exist")
        regions = self._conn.execute(
            """
            SELECT region, derived_row_count, quoted_row_count,
                   unquoted_row_count, quoted_amount, output_tuple_count
              FROM region_summary WHERE result_id = ? ORDER BY region
            """,
            (result_id,),
        ).fetchall()
        return {
            "status": status,
            "result_id": result_id,
            "source_version": int(row["source_version"]),
            "source_fingerprint": row["source_fingerprint"],
            "built_at": row["built_at"],
            "rebuild_policy": row["rebuild_policy"],
            "totals": {
                "derived_row_count": int(row["derived_row_count"]),
                "quoted_row_count": int(row["quoted_row_count"]),
                "unquoted_row_count": int(row["unquoted_row_count"]),
                "quoted_amount": int(row["quoted_amount"]),
                "output_tuple_count": int(row["output_tuple_count"]),
                "provenance_count": int(row["provenance_count"]),
            },
            "by_region": [
                {
                    "region": r["region"],
                    "derived_row_count": int(r["derived_row_count"]),
                    "quoted_row_count": int(r["quoted_row_count"]),
                    "unquoted_row_count": int(r["unquoted_row_count"]),
                    "quoted_amount": int(r["quoted_amount"]),
                    "output_tuple_count": int(r["output_tuple_count"]),
                }
                for r in regions
            ],
        }

    def read_result_rows(
        self,
        result_id: str,
        region: str | None = None,
        sku: str | None = None,
        quoted: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        self._require_result(result_id)
        clauses = ["result_id = ?"]
        params: list[Any] = [result_id]
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        if sku is not None:
            clauses.append("sku = ?")
            params.append(sku)
        if quoted is not None:
            clauses.append("is_quoted = ?")
            params.append(1 if quoted else 0)
        sql = (
            "SELECT region, sku, quantity, supplier, price, multiplicity, is_quoted "
            "FROM result_rows WHERE "
            + " AND ".join(clauses)
            + " ORDER BY region, sku, quantity, is_quoted DESC, supplier, price"
        )
        if limit is not None:
            if not is_int(limit) or limit < 0:
                raise ValidationError("limit must be a non-negative integer")
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "tuple": [row["region"], row["sku"], row["quantity"],
                          row["supplier"], row["price"]],
                "region": row["region"],
                "sku": row["sku"],
                "quantity": int(row["quantity"]),
                "supplier": row["supplier"],
                "price": None if row["price"] is None else int(row["price"]),
                "multiplicity": int(row["multiplicity"]),
                "quoted": bool(row["is_quoted"]),
            }
            for row in rows
        ]

    def region_summary(self, result_id: str) -> list[dict]:
        self._require_result(result_id)
        rows = self._conn.execute(
            """
            SELECT region, derived_row_count, quoted_row_count,
                   unquoted_row_count, quoted_amount, output_tuple_count
              FROM region_summary WHERE result_id = ? ORDER BY region
            """,
            (result_id,),
        ).fetchall()
        return [
            {
                "region": row["region"],
                "derived_row_count": int(row["derived_row_count"]),
                "quoted_row_count": int(row["quoted_row_count"]),
                "unquoted_row_count": int(row["unquoted_row_count"]),
                "quoted_amount": int(row["quoted_amount"]),
                "output_tuple_count": int(row["output_tuple_count"]),
            }
            for row in rows
        ]

    def explain(
        self,
        result_id: str,
        region: str | None = None,
        sku: str | None = None,
        quantity: int | None = None,
        quoted: bool | None = None,
        supplier: str | None = None,
        price: int | None = None,
    ) -> dict:
        """Explain output counts from their source tuples and multipliers."""
        meta = self.get_result(result_id)
        clauses = ["result_id = ?"]
        params: list[Any] = [result_id]
        if region is not None:
            clauses.append("region = ?")
            params.append(region)
        if sku is not None:
            clauses.append("sku = ?")
            params.append(sku)
        if quantity is not None:
            clauses.append("quantity = ?")
            params.append(quantity)
        if quoted is not None:
            clauses.append("is_quoted = ?")
            params.append(1 if quoted else 0)
        if supplier is not None:
            clauses.append("is_quoted = 1 AND supplier_key = ?")
            params.append(supplier)
        if price is not None:
            clauses.append("is_quoted = 1 AND price_key = ?")
            params.append(price)

        rows = self._conn.execute(
            """
            SELECT region, sku, quantity, is_quoted, supplier_key, price_key,
                   output_multiplicity, left_region, left_sku, left_quantity,
                   left_multiplicity, right_sku, right_supplier, right_price,
                   right_multiplicity, contribution
              FROM result_provenance WHERE """
            + " AND ".join(clauses)
            + " ORDER BY region, sku, quantity, is_quoted, supplier_key, price_key, "
              "left_region, left_quantity, right_supplier, right_price",
            params,
        ).fetchall()

        contributions = []
        for row in rows:
            quoted_row = bool(row["is_quoted"])
            left_mult = int(row["left_multiplicity"])
            right_mult = row["right_multiplicity"]
            contribution = int(row["contribution"])
            if quoted_row:
                formula = f"{left_mult} x {int(right_mult)} = {contribution}"
            else:
                formula = f"{left_mult} (match set empty -> NULL, NULL)"
            contributions.append(
                {
                    "output": {
                        "region": row["region"],
                        "sku": row["sku"],
                        "quantity": int(row["quantity"]),
                        "supplier": row["supplier_key"] if quoted_row else None,
                        "price": int(row["price_key"]) if quoted_row else None,
                    },
                    "output_multiplicity": int(row["output_multiplicity"]),
                    "left_tuple": [row["left_region"], row["left_sku"],
                                   int(row["left_quantity"])],
                    "left_multiplicity": left_mult,
                    "right_tuple": (
                        [row["right_sku"], row["right_supplier"],
                         int(row["right_price"])]
                        if quoted_row else None
                    ),
                    "right_multiplicity": int(right_mult) if quoted_row else None,
                    "contribution": contribution,
                    "formula": formula,
                }
            )
        return {
            "result_id": result_id,
            "source_version": meta["source_version"],
            "source_fingerprint": meta["source_fingerprint"],
            "filters": {
                "region": region,
                "sku": sku,
                "quantity": quantity,
                "quoted": quoted,
                "supplier": supplier,
                "price": price,
            },
            "totals": meta["totals"],
            "contribution_count": len(contributions),
            "contributions": contributions,
        }

    # ----------------------------------------------------------------- helpers
    def _result_row(self, result_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM results WHERE result_id = ?", (result_id,)
        ).fetchone()

    def _require_result(self, result_id: str) -> None:
        if self._result_row(result_id) is None:
            raise UnknownResultError(f"result_id {result_id!r} does not exist")
