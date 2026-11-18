"""Address catalog: cities, districts, streets, scoped names and coverages.

The catalog is a standalone component.  It has no dependency on the delivery
job store: you can open just the catalog tables, resolve addresses and query
catalog state without any parcel/job table being present.

Resolution always follows the *full parent chain* city -> district -> street.
District text is resolved within exactly one city; street text is resolved only
inside the districts that the district text selected.  We never pick a single
district first and never use coverage, active flags or station results to
disambiguate an address.

Normalisation is limited to NFC + trimming; identical normalised text may
legitimately point at several entities and is not a catalog error.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import connect, transaction
from .errors import CatalogError
from .models import AddressInput
from .normalization import normalize_text

PARITIES = ("any", "odd", "even")


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreetCandidate:
    street_id: str
    district_id: str
    city_id: str
    name: str
    active: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "street_id": self.street_id,
            "district_id": self.district_id,
            "city_id": self.city_id,
            "name": self.name,
            "active": self.active,
        }


@dataclass(frozen=True)
class Resolution:
    """The address-level interpretation, before any station lookup."""

    city_id: str
    district_text: str
    street_text: str
    house_no: int
    city_found: bool
    district_ids: tuple[str, ...]
    candidates: tuple[StreetCandidate, ...]
    status: str  # "unique" | "unknown" | "ambiguous"

    @property
    def street(self) -> StreetCandidate | None:
        return self.candidates[0] if self.status == "unique" else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "city_id": self.city_id,
            "district_text": normalize_text(self.district_text),
            "street_text": normalize_text(self.street_text),
            "house_no": self.house_no,
            "city_found": self.city_found,
            "district_ids": list(self.district_ids),
            "candidates": [c.as_dict() for c in self.candidates],
            "status": self.status,
        }


@dataclass(frozen=True)
class CoverageHit:
    coverage_id: str
    street_id: str
    station_id: str
    lo: int
    hi: int
    parity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "coverage_id": self.coverage_id,
            "street_id": self.street_id,
            "station_id": self.station_id,
            "lo": self.lo,
            "hi": self.hi,
            "parity": self.parity,
        }


# ---------------------------------------------------------------------------
# Read-transaction helper
# ---------------------------------------------------------------------------


class _ReadSnapshot:
    """Consistent multi-statement read even when called outside a transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def __enter__(self):
        self._owns = not self.conn.in_transaction
        if self._owns:
            self.conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._owns:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


def read_snapshot(conn) -> _ReadSnapshot:
    """Public alias for wrap-around reads spanning several catalog queries."""
    return _ReadSnapshot(conn)


# ---------------------------------------------------------------------------
# Catalog store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cities (
    city_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS districts (
    district_id TEXT PRIMARY KEY,
    city_id     TEXT NOT NULL REFERENCES cities(city_id),
    name        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_districts_city ON districts(city_id);
CREATE TABLE IF NOT EXISTS district_names (
    district_id TEXT NOT NULL REFERENCES districts(district_id),
    text        TEXT NOT NULL,           -- normalised name/alias
    is_primary  INTEGER NOT NULL,        -- 1 = canonical name, 0 = alias
    PRIMARY KEY (district_id, text)
);
CREATE INDEX IF NOT EXISTS idx_district_names_text ON district_names(text);
CREATE TABLE IF NOT EXISTS streets (
    street_id   TEXT PRIMARY KEY,
    district_id TEXT NOT NULL REFERENCES districts(district_id),
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_version INTEGER NOT NULL,
    deactivated_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_streets_district ON streets(district_id);
CREATE TABLE IF NOT EXISTS street_names (
    street_id  TEXT NOT NULL REFERENCES streets(street_id),
    text       TEXT NOT NULL,            -- normalised name/alias, valid in district
    is_primary INTEGER NOT NULL,
    PRIMARY KEY (street_id, text)
);
CREATE INDEX IF NOT EXISTS idx_street_names_text ON street_names(text);
CREATE TABLE IF NOT EXISTS coverages (
    coverage_id TEXT PRIMARY KEY,
    street_id   TEXT NOT NULL REFERENCES streets(street_id),
    station_id  TEXT NOT NULL,
    lo          INTEGER NOT NULL,
    hi          INTEGER NOT NULL,
    parity      TEXT NOT NULL CHECK (parity IN ('any','odd','even'))
);
CREATE INDEX IF NOT EXISTS idx_coverages_street ON coverages(street_id);
"""


class Catalog:
    """Catalog facade over a SQLite connection.

    The same connection may be shared with :class:`~address_delivery.delivery.DeliveryStore`
    so a delivery commit can read catalog state and write its job atomically.
    """

    def __init__(self, conn, *, init_schema: bool = True) -> None:
        self.conn = conn
        if init_schema:
            self.conn.executescript(_SCHEMA)
            self._ensure_meta()

    # -- construction helpers ------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, create: bool = True) -> "Catalog":
        conn = connect(path)
        if not create:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='cities'"
            ).fetchone()
            if row is None:
                raise CatalogError(f"no catalog found at {path}")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def _ensure_meta(self) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO catalog_meta(key,value) VALUES('version','0')"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO catalog_meta(key,value) VALUES('revision','')"
            )

    def _meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM catalog_meta WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else row["value"]

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO catalog_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- catalog properties --------------------------------------------------

    @property
    def version(self) -> int:
        return int(self._meta("version", "0"))

    @property
    def revision(self) -> str:
        return self._meta("revision", "")

    # -- initial load --------------------------------------------------------

    def load_initial(self, catalog_doc: dict, coverage_doc: dict) -> None:
        """Load the r1 catalog + coverage atomically as version 1."""
        self._validate_catalog_doc(catalog_doc)
        self._validate_coverage_doc(coverage_doc)
        with transaction(self.conn):
            if self.conn.execute("SELECT 1 FROM cities LIMIT 1").fetchone():
                raise CatalogError("catalog is already initialised")
            for city in catalog_doc.get("cities", []):
                self.conn.execute(
                    "INSERT INTO cities(city_id,name) VALUES(?,?)",
                    (city["city_id"], normalize_text(city["name"])),
                )
            for district in catalog_doc.get("districts", []):
                self.conn.execute(
                    "INSERT INTO districts(district_id,city_id,name) VALUES(?,?,?)",
                    (
                        district["district_id"],
                        district["city_id"],
                        normalize_text(district["name"]),
                    ),
                )
                names = [district["name"]] + list(district.get("aliases", []))
                self._insert_district_names(district["district_id"], names)
            for street in catalog_doc.get("streets", []):
                self.conn.execute(
                    "INSERT INTO streets(street_id,district_id,name,active,"
                    "created_version,deactivated_version) VALUES(?,?,?,?,?,NULL)",
                    (
                        street["street_id"],
                        street["district_id"],
                        normalize_text(street["name"]),
                        1 if street.get("active", True) else 0,
                        0,
                    ),
                )
                names = [street["name"]] + list(street.get("aliases", []))
                self._insert_street_names(street["street_id"], names)
            for coverage in coverage_doc.get("coverages", []):
                self._insert_coverage(coverage)
            self._bump_version(catalog_doc.get("revision", "r1"))

    def _insert_district_names(self, district_id: str, names: Iterable[str]) -> None:
        seen: set[str] = set()
        for i, raw in enumerate(names):
            text = normalize_text(raw)
            if not text:
                raise CatalogError(f"empty district name for {district_id}")
            if text in seen:
                # Duplicate alias text on the same entity is harmless; the
                # primary flag wins if it appears again.
                if i == 0:
                    self.conn.execute(
                        "UPDATE district_names SET is_primary=1 "
                        "WHERE district_id=? AND text=?",
                        (district_id, text),
                    )
                continue
            seen.add(text)
            self.conn.execute(
                "INSERT INTO district_names(district_id,text,is_primary) VALUES(?,?,?)",
                (district_id, text, 1 if i == 0 else 0),
            )

    def _insert_street_names(self, street_id: str, names: Iterable[str]) -> None:
        seen: set[str] = set()
        for i, raw in enumerate(names):
            text = normalize_text(raw)
            if not text:
                raise CatalogError(f"empty street name for {street_id}")
            if text in seen:
                if i == 0:
                    self.conn.execute(
                        "UPDATE street_names SET is_primary=1 "
                        "WHERE street_id=? AND text=?",
                        (street_id, text),
                    )
                continue
            seen.add(text)
            self.conn.execute(
                "INSERT INTO street_names(street_id,text,is_primary) VALUES(?,?,?)",
                (street_id, text, 1 if i == 0 else 0),
            )

    def _insert_coverage(self, coverage: dict) -> None:
        self.conn.execute(
            "INSERT INTO coverages(coverage_id,street_id,station_id,lo,hi,parity) "
            "VALUES(?,?,?,?,?,?)",
            (
                coverage["coverage_id"],
                coverage["street_id"],
                coverage["station_id"],
                int(coverage["lo"]),
                int(coverage["hi"]),
                coverage["parity"],
            ),
        )

    def _bump_version(self, revision: str) -> None:
        new_version = self.version + 1
        self._set_meta("version", str(new_version))
        self._set_meta("revision", revision)

    # -- document validation -------------------------------------------------

    @staticmethod
    def _require(cond: bool, message: str) -> None:
        if not cond:
            raise CatalogError(message)

    def _validate_catalog_doc(self, doc: dict) -> None:
        self._require(isinstance(doc, dict), "catalog document must be an object")
        cities = doc.get("cities", [])
        districts = doc.get("districts", [])
        streets = doc.get("streets", [])
        for collection, name in ((cities, "cities"), (districts, "districts"), (streets, "streets")):
            self._require(isinstance(collection, list), f"{name} must be a list")
        city_ids: set[str] = set()
        for city in cities:
            self._require(isinstance(city, dict), "city entries must be objects")
            self._require(isinstance(city.get("city_id"), str) and city["city_id"], "city_id required")
            self._require(city["city_id"] not in city_ids, f"duplicate city_id {city['city_id']}")
            city_ids.add(city["city_id"])
            self._require(isinstance(city.get("name"), str), "city name required")
        district_ids: set[str] = set()
        for district in districts:
            self._require(isinstance(district, dict), "district entries must be objects")
            did = district.get("district_id")
            self._require(isinstance(did, str) and did, "district_id required")
            self._require(did not in district_ids, f"duplicate district_id {did}")
            district_ids.add(did)
            self._require(district.get("city_id") in city_ids, f"district {did} has unknown city")
            self._require(isinstance(district.get("name"), str), "district name required")
            self._require(self._aliases_ok(district.get("aliases", [])), f"district {did} aliases invalid")
        street_ids: set[str] = set()
        for street in streets:
            self._require(isinstance(street, dict), "street entries must be objects")
            sid = street.get("street_id")
            self._require(isinstance(sid, str) and sid, "street_id required")
            self._require(sid not in street_ids, f"duplicate street_id {sid}")
            street_ids.add(sid)
            self._require(street.get("district_id") in district_ids, f"street {sid} has unknown district")
            self._require(isinstance(street.get("name"), str), "street name required")
            self._require(self._aliases_ok(street.get("aliases", [])), f"street {sid} aliases invalid")
            self._require(isinstance(street.get("active", True), bool), "active must be bool")

    @staticmethod
    def _aliases_ok(aliases) -> bool:
        return isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)

    def _validate_coverage_doc(self, doc: dict) -> None:
        self._require(isinstance(doc, dict), "coverage document must be an object")
        coverages = doc.get("coverages", [])
        self._require(isinstance(coverages, list), "coverages must be a list")
        seen: set[str] = set()
        for coverage in coverages:
            self._require(isinstance(coverage, dict), "coverage entries must be objects")
            cid = coverage.get("coverage_id")
            self._require(isinstance(cid, str) and cid, "coverage_id required")
            self._require(cid not in seen, f"duplicate coverage_id {cid}")
            seen.add(cid)
            self._require(isinstance(coverage.get("street_id"), str), "coverage street_id required")
            self._require(isinstance(coverage.get("station_id"), str) and coverage["station_id"], "station_id required")
            lo, hi = coverage.get("lo"), coverage.get("hi")
            self._require(_is_int(lo) and _is_int(hi), "coverage lo/hi must be integers")
            self._require(0 <= lo <= hi, f"coverage {cid} has invalid range")
            self._require(coverage.get("parity") in PARITIES, f"coverage {cid} parity invalid")

    # -- revisions -----------------------------------------------------------

    def apply_revision(self, revision_doc: dict) -> int:
        """Apply a revision atomically and return the new catalog version.

        Supported operations:
          * ``add_street_alias``    {street_id, alias}
          * ``remove_street_alias`` {street_id, alias}
          * ``deactivate_street``   {street_id}
          * ``activate_street``     {street_id}
          * ``remove_coverage``     {coverage_id}
          * ``add_coverage``        {coverage_id, street_id, station_id, lo, hi, parity}
        """
        self._require(isinstance(revision_doc, dict), "revision document must be an object")
        operations = revision_doc.get("operations")
        self._require(isinstance(operations, list), "operations must be a list")
        with transaction(self.conn):
            for op in operations:
                self._apply_operation(op)
            self._bump_version(str(revision_doc.get("revision", "")))
            return self.version

    def _apply_operation(self, op: dict) -> None:
        self._require(isinstance(op, dict), "operation must be an object")
        kind = op.get("op")
        if kind == "add_street_alias":
            street_id = op.get("street_id")
            alias = normalize_text(op.get("alias", "") if isinstance(op.get("alias"), str) else "")
            self._require(self._street_exists(street_id), f"unknown street {street_id}")
            self._require(bool(alias), "alias must be non-empty")
            self.conn.execute(
                "INSERT OR IGNORE INTO street_names(street_id,text,is_primary) VALUES(?,?,0)",
                (street_id, alias),
            )
        elif kind == "remove_street_alias":
            street_id = op.get("street_id")
            alias = normalize_text(op.get("alias", "") if isinstance(op.get("alias"), str) else "")
            self._require(self._street_exists(street_id), f"unknown street {street_id}")
            row = self.conn.execute(
                "SELECT is_primary FROM street_names WHERE street_id=? AND text=?",
                (street_id, alias),
            ).fetchone()
            self._require(row is not None, f"alias {alias!r} not found on {street_id}")
            self._require(row["is_primary"] == 0, "cannot remove the primary name via alias op")
            self.conn.execute(
                "DELETE FROM street_names WHERE street_id=? AND text=?",
                (street_id, alias),
            )
        elif kind == "deactivate_street":
            street_id = op.get("street_id")
            self._require(self._street_exists(street_id), f"unknown street {street_id}")
            self.conn.execute(
                "UPDATE streets SET active=0, deactivated_version=? "
                "WHERE street_id=? AND active=1",
                (self.version + 1, street_id),
            )
        elif kind == "activate_street":
            street_id = op.get("street_id")
            self._require(self._street_exists(street_id), f"unknown street {street_id}")
            self.conn.execute(
                "UPDATE streets SET active=1, deactivated_version=NULL WHERE street_id=?",
                (street_id,),
            )
        elif kind == "remove_coverage":
            coverage_id = op.get("coverage_id")
            row = self.conn.execute(
                "SELECT 1 FROM coverages WHERE coverage_id=?", (coverage_id,)
            ).fetchone()
            self._require(row is not None, f"unknown coverage {coverage_id}")
            self.conn.execute(
                "DELETE FROM coverages WHERE coverage_id=?", (coverage_id,)
            )
        elif kind == "add_coverage":
            coverage = {
                "coverage_id": op.get("coverage_id"),
                "street_id": op.get("street_id"),
                "station_id": op.get("station_id"),
                "lo": op.get("lo"),
                "hi": op.get("hi"),
                "parity": op.get("parity"),
            }
            self._validate_coverage_doc({"coverages": [coverage]})
            self._require(self._street_exists(op.get("street_id")), f"unknown street {op.get('street_id')}")
            existing = self.conn.execute(
                "SELECT 1 FROM coverages WHERE coverage_id=?", (op.get("coverage_id"),)
            ).fetchone()
            self._require(existing is None, f"coverage {op.get('coverage_id')} already exists")
            self._insert_coverage(coverage)
        else:
            raise CatalogError(f"unsupported revision operation: {kind!r}")

    def _street_exists(self, street_id: str | None) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM streets WHERE street_id=?", (street_id,)
            ).fetchone()
        )

    # -- resolution ----------------------------------------------------------

    def resolve(self, city_id: str, district_text: str, street_text: str, house_no: int) -> Resolution:
        """Resolve one address through the complete parent chain.

        Inactive streets remain candidates: the address can still be
        *identified*; only the delivery step refuses it.  Inactive streets are
        therefore never used to break an address tie.
        """
        district_key = normalize_text(district_text)
        street_key = normalize_text(street_text)
        with _ReadSnapshot(self.conn):
            city = self.conn.execute(
                "SELECT city_id FROM cities WHERE city_id=?", (city_id,)
            ).fetchone()
            city_found = city is not None
            if not city_found:
                return Resolution(
                    city_id, district_text, street_text, house_no, False, (), (), "unknown"
                )
            district_rows = self.conn.execute(
                "SELECT DISTINCT d.district_id FROM districts d "
                "JOIN district_names dn ON dn.district_id=d.district_id "
                "WHERE d.city_id=? AND dn.text=? ORDER BY d.district_id",
                (city_id, district_key),
            ).fetchall()
            district_ids = tuple(row["district_id"] for row in district_rows)
            if not district_ids:
                return Resolution(
                    city_id, district_text, street_text, house_no, True, (), (), "unknown"
                )
            placeholders = ",".join("?" for _ in district_ids)
            candidate_rows = self.conn.execute(
                "SELECT DISTINCT s.street_id, s.district_id, d.city_id, s.name, s.active "
                "FROM streets s "
                "JOIN districts d ON d.district_id=s.district_id "
                "JOIN street_names sn ON sn.street_id=s.street_id "
                f"WHERE s.district_id IN ({placeholders}) AND sn.text=? "
                "ORDER BY s.district_id, s.street_id",
                (*district_ids, street_key),
            ).fetchall()
            candidates = tuple(
                StreetCandidate(
                    street_id=row["street_id"],
                    district_id=row["district_id"],
                    city_id=row["city_id"],
                    name=row["name"],
                    active=bool(row["active"]),
                )
                for row in candidate_rows
            )
        status = "unique" if len(candidates) == 1 else ("unknown" if not candidates else "ambiguous")
        return Resolution(
            city_id,
            district_text,
            street_text,
            house_no,
            True,
            district_ids,
            candidates,
            status,
        )

    def resolve_input(self, address: AddressInput) -> Resolution:
        return self.resolve(
            address.city_id, address.district_text, address.street_text, address.house_no
        )

    def resolve_batch(self, addresses: Sequence[AddressInput]) -> list[Resolution]:
        """Resolve many addresses independently (no delivery store involved).

        A shared read snapshot is *not* held across the batch: the catalog is
        immutable between revisions, and each parcel is an independent query.
        Call within an outer transaction if you need batch-level consistency.
        """
        return [self.resolve_input(a) for a in addresses]

    def resolve_streets(
        self, city_id: str, district_text: str, street_text: str
    ) -> list[StreetCandidate]:
        """Address candidates ignoring house number (for catalog queries)."""
        return list(self.resolve(city_id, district_text, street_text, 1).candidates)

    # -- coverage ------------------------------------------------------------

    def coverage_hits(self, street_id: str, house_no: int) -> list[CoverageHit]:
        if isinstance(house_no, bool) or not isinstance(house_no, int) or house_no <= 0:
            raise ValueError("house_no must be a positive integer")
        rows = self.conn.execute(
            "SELECT coverage_id, street_id, station_id, lo, hi, parity "
            "FROM coverages WHERE street_id=? ORDER BY coverage_id",
            (street_id,),
        ).fetchall()
        hits: list[CoverageHit] = []
        for row in rows:
            if not (row["lo"] <= house_no <= row["hi"]):
                continue
            parity = row["parity"]
            if parity == "odd" and house_no % 2 == 0:
                continue
            if parity == "even" and house_no % 2 == 1:
                continue
            hits.append(
                CoverageHit(
                    row["coverage_id"],
                    row["street_id"],
                    row["station_id"],
                    row["lo"],
                    row["hi"],
                    parity,
                )
            )
        return hits

    def station_for(self, street_id: str, house_no: int) -> tuple[str | None, list[CoverageHit], list[str]]:
        """Return ``(station_id, hits, stations)`` for a house number.

        ``station_id`` is set only when exactly one distinct station matches.
        """
        hits = self.coverage_hits(street_id, house_no)
        stations = sorted({h.station_id for h in hits})
        return (stations[0] if len(stations) == 1 else None), hits, stations

    # -- catalog queries -----------------------------------------------------

    def get_city(self, city_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT city_id, name FROM cities WHERE city_id=?", (city_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def get_district(self, district_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT district_id, city_id, name FROM districts WHERE district_id=?",
            (district_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["names"] = [
            r["text"]
            for r in self.conn.execute(
                "SELECT text FROM district_names WHERE district_id=? "
                "ORDER BY is_primary DESC, text",
                (district_id,),
            )
        ]
        return result

    def get_street(self, street_id: str) -> dict | None:
        """Historical stable-ID lookup; works for inactive streets too."""
        row = self.conn.execute(
            "SELECT s.street_id, s.district_id, s.name, s.active, "
            "s.created_version, s.deactivated_version, d.city_id "
            "FROM streets s JOIN districts d ON d.district_id=s.district_id "
            "WHERE s.street_id=?",
            (street_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["active"] = bool(result["active"])
        result["names"] = [
            r["text"]
            for r in self.conn.execute(
                "SELECT text FROM street_names WHERE street_id=? "
                "ORDER BY is_primary DESC, text",
                (street_id,),
            )
        ]
        return result

    def lookup_street_by_name(self, city_id: str, district_text: str, street_text: str) -> list[dict]:
        return [c.as_dict() for c in self.resolve_streets(city_id, district_text, street_text)]

    def list_cities(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT city_id, name FROM cities ORDER BY city_id")]

    def list_districts(self, city_id: str | None = None) -> list[dict]:
        if city_id is None:
            rows = self.conn.execute(
                "SELECT district_id, city_id, name FROM districts ORDER BY district_id"
            )
        else:
            rows = self.conn.execute(
                "SELECT district_id, city_id, name FROM districts WHERE city_id=? ORDER BY district_id",
                (city_id,),
            )
        return [dict(r) for r in rows]

    def list_streets(self, *, include_inactive: bool = True, district_id: str | None = None) -> list[dict]:
        sql = (
            "SELECT s.street_id, s.district_id, d.city_id, s.name, s.active, "
            "s.created_version, s.deactivated_version "
            "FROM streets s JOIN districts d ON d.district_id=s.district_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            clauses.append("s.active=1")
        if district_id is not None:
            clauses.append("s.district_id=?")
            params.append(district_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY s.district_id, s.street_id"
        results = []
        for row in self.conn.execute(sql, params):
            item = dict(row)
            item["active"] = bool(item["active"])
            results.append(item)
        return results

    def list_coverages(self, street_id: str | None = None) -> list[dict]:
        if street_id is None:
            rows = self.conn.execute(
                "SELECT coverage_id, street_id, station_id, lo, hi, parity "
                "FROM coverages ORDER BY coverage_id"
            )
        else:
            rows = self.conn.execute(
                "SELECT coverage_id, street_id, station_id, lo, hi, parity "
                "FROM coverages WHERE street_id=? ORDER BY coverage_id",
                (street_id,),
            )
        return [dict(r) for r in rows]

    def snapshot(self) -> dict:
        return {
            "revision": self.revision,
            "version": self.version,
            "cities": self.list_cities(),
            "districts": self.list_districts(),
            "streets": self.list_streets(),
            "coverages": self.list_coverages(),
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_catalog_files(catalog_path: str | Path, coverage_path: str | Path) -> tuple[dict, dict]:
    catalog_doc = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    coverage_doc = json.loads(Path(coverage_path).read_text(encoding="utf-8"))
    return catalog_doc, coverage_doc
