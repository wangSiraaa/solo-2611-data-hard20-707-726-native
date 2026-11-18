"""Independent delivery job store.

The store receives parcels (identity + original address) and attempts delivery
against the catalog.  A job is committed only when, in one transaction, the
address resolves to exactly one street, that street is active, and the house
number maps to exactly one station.

A parcel's *first* successful commit freezes its identity.  Later input under
the same ``parcel_id``:

* identical content (city id, NFC/trimmed district+street text, integer house)
  replays the stored result verbatim - even if the catalog has since renamed,
  deactivated or re-covered the street;
* different content is rejected and leaves the stored job untouched.

Source file and source row are recorded for provenance but never compared.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .catalog import Catalog, CoverageHit, Resolution
from .db import connect, transaction
from .models import AddressInput
from .normalization import make_identity, normalize_text

# Outcome vocabulary ---------------------------------------------------------

SUBMITTED = "submitted"
REPLAYED = "replayed"
REJECTED_IDENTITY = "rejected_identity_conflict"
FAILED_CITY_UNKNOWN = "failed_city_unknown"
FAILED_DISTRICT_UNKNOWN = "failed_district_unknown"
FAILED_STREET_UNKNOWN = "failed_street_unknown"
FAILED_AMBIGUOUS_ADDRESS = "failed_ambiguous_address"
FAILED_STREET_INACTIVE = "failed_street_inactive"
FAILED_STATION_NONE = "failed_station_none"
FAILED_STATION_AMBIGUOUS = "failed_station_ambiguous"

SUCCESS_OUTCOMES = {SUBMITTED, REPLAYED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    parcel_id              TEXT PRIMARY KEY,
    identity_key           TEXT NOT NULL,
    city_id                TEXT NOT NULL,
    district_text_original TEXT NOT NULL,
    street_text_original   TEXT NOT NULL,
    district_text_norm     TEXT NOT NULL,
    street_text_norm       TEXT NOT NULL,
    house_no               INTEGER NOT NULL,
    district_id            TEXT NOT NULL,
    street_id              TEXT NOT NULL,
    station_id             TEXT NOT NULL,
    catalog_version        INTEGER NOT NULL,
    catalog_revision       TEXT NOT NULL,
    source_file            TEXT,
    source_row             INTEGER,
    interpretation_json    TEXT NOT NULL,
    submitted_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(identity_key);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id       TEXT NOT NULL,
    source_file     TEXT,
    source_row      INTEGER,
    city_id         TEXT NOT NULL,
    district_text   TEXT NOT NULL,
    street_text     TEXT NOT NULL,
    house_no        INTEGER NOT NULL,
    catalog_version INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    message         TEXT,
    job_created     INTEGER NOT NULL DEFAULT 0,
    detail_json     TEXT,
    attempted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_parcel ON attempts(parcel_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AttemptResult:
    parcel_id: str
    source_row: int | None
    outcome: str
    message: str
    job: dict[str, Any] | None
    resolution: dict[str, Any] | None
    station_id: str | None
    coverage_hits: list[dict[str, Any]]
    attempt_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES

    @property
    def submitted_new(self) -> bool:
        return self.outcome == SUBMITTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "source_row": self.source_row,
            "outcome": self.outcome,
            "ok": self.ok,
            "submitted_new": self.submitted_new,
            "message": self.message,
            "station_id": self.station_id,
            "job": self.job,
            "resolution": self.resolution,
            "coverage_hits": self.coverage_hits,
            "attempt_id": self.attempt_id,
        }


@dataclass
class BatchResult:
    source_file: str | None
    rows: list[AttemptResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "rows": [r.as_dict() for r in self.rows],
            "submitted": sum(1 for r in self.rows if r.outcome == SUBMITTED),
            "replayed": sum(1 for r in self.rows if r.outcome == REPLAYED),
            "failed": sum(1 for r in self.rows if not r.ok),
        }


class DeliveryStore:
    """Parcel/job store backed by SQLite.

    Pass a shared ``conn`` alongside a :class:`Catalog` so catalog reads and job
    writes share one transaction; otherwise the store opens its own connection
    to *db_path* whose database must already contain the catalog tables.
    """

    def __init__(self, conn, *, init_schema: bool = True) -> None:
        self.conn = conn
        if init_schema:
            self.conn.executescript(_SCHEMA)

    @classmethod
    def open(cls, db_path: str) -> "DeliveryStore":
        return cls(connect(db_path))

    def close(self) -> None:
        self.conn.close()

    # -- job / attempt queries ----------------------------------------------

    def get_job(self, parcel_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE parcel_id=?", (parcel_id,)
        ).fetchone()
        return None if row is None else _job_row(row)

    def get_attempts(self, parcel_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM attempts WHERE parcel_id=? ORDER BY attempt_id",
            (parcel_id,),
        ).fetchall()
        return [_attempt_row(r) for r in rows]

    def final_destination(self, parcel_id: str) -> dict[str, Any] | None:
        """Query entry point for a parcel's final committed destination."""
        job = self.get_job(parcel_id)
        if job is None:
            return None
        return {
            "parcel_id": parcel_id,
            "street_id": job["street_id"],
            "district_id": job["district_id"],
            "city_id": job["city_id"],
            "house_no": job["house_no"],
            "station_id": job["station_id"],
            "catalog_version": job["catalog_version"],
            "catalog_revision": job["catalog_revision"],
            "identity": job["identity_key"],
            "original": {
                "city_id": job["city_id"],
                "district_text": job["district_text_original"],
                "street_text": job["street_text_original"],
                "house_no": job["house_no"],
            },
            "interpretation": job["interpretation"],
            "source_file": job["source_file"],
            "source_row": job["source_row"],
            "submitted_at": job["submitted_at"],
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        return [_job_row(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY parcel_id")]

    # -- attempts ------------------------------------------------------------

    def attempt(self, item: AddressInput, catalog: Catalog) -> AttemptResult:
        """Attempt delivery for one parcel, atomically.

        The frozen-job check, catalog read, coverage lookup and job/attempt
        write all run in one ``BEGIN IMMEDIATE`` transaction, so a reported
        submission is durable and consistent with the catalog version recorded
        on it.
        """
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM jobs WHERE parcel_id=?", (item.parcel_id,)
            ).fetchone()
            if existing is not None:
                return self._handle_existing(item, existing, catalog)

            resolution = catalog.resolve(
                item.city_id, item.district_text, item.street_text, item.house_no
            )
            outcome, message, station_id, hits = self._classify(resolution, catalog)
            detail = {
                "resolution": resolution.as_dict(),
                "coverage_hits": [h.as_dict() for h in hits],
            }
            job = None
            job_created = False
            if outcome == SUBMITTED:
                job = self._insert_job(item, resolution, station_id, hits, catalog)
                job_created = True
            attempt_id = self._insert_attempt(
                item, outcome, message, job_created, detail, catalog
            )
            return AttemptResult(
                parcel_id=item.parcel_id,
                source_row=item.source_row,
                outcome=outcome,
                message=message,
                job=job,
                resolution=resolution.as_dict(),
                station_id=station_id,
                coverage_hits=[h.as_dict() for h in hits],
                attempt_id=attempt_id,
            )

    def _handle_existing(self, item: AddressInput, existing, catalog: Catalog) -> AttemptResult:
        """Handle a re-sent parcel that already has a committed job."""
        identity_key = make_identity(
            item.city_id, item.district_text, item.street_text, item.house_no
        )
        interpretation = json.loads(existing["interpretation_json"])
        if identity_key == existing["identity_key"]:
            # Replay the frozen result; the catalog is not consulted at all.
            attempt_id = self._insert_attempt(
                item,
                REPLAYED,
                "identity matches committed job; replaying frozen result",
                False,
                {"replayed_job": _job_row(existing)},
                catalog,
            )
            return AttemptResult(
                parcel_id=item.parcel_id,
                source_row=item.source_row,
                outcome=REPLAYED,
                message="replayed frozen result",
                job=_job_row(existing),
                resolution=interpretation.get("resolution"),
                station_id=existing["station_id"],
                coverage_hits=interpretation.get("coverage_hits", []),
                attempt_id=attempt_id,
            )
        attempt_id = self._insert_attempt(
            item,
            REJECTED_IDENTITY,
            "parcel_id already committed with different address content",
            False,
            {
                "existing_identity": json.loads(existing["identity_key"]),
                "incoming_identity": json.loads(identity_key),
                "existing_job": _job_row(existing),
            },
            catalog,
        )
        return AttemptResult(
            parcel_id=item.parcel_id,
            source_row=item.source_row,
            outcome=REJECTED_IDENTITY,
            message="identity conflict: parcel already committed with different content",
            job=_job_row(existing),
            resolution=None,
            station_id=existing["station_id"],
            coverage_hits=[],
            attempt_id=attempt_id,
        )

    def _classify(
        self, resolution: Resolution, catalog: Catalog
    ) -> tuple[str, str, str | None, list[CoverageHit]]:
        if not resolution.city_found:
            return FAILED_CITY_UNKNOWN, "unknown city", None, []
        if not resolution.district_ids:
            return FAILED_DISTRICT_UNKNOWN, "district text not found in city", None, []
        if resolution.status == "unknown":
            return FAILED_STREET_UNKNOWN, "street text not found under matched districts", None, []
        if resolution.status == "ambiguous":
            return (
                FAILED_AMBIGUOUS_ADDRESS,
                f"address matches {len(resolution.candidates)} streets",
                None,
                [],
            )
        street = resolution.candidates[0]
        if not street.active:
            # Identification succeeds, but new delivery is refused.  Active
            # status never removes the street from candidate sets.
            return FAILED_STREET_INACTIVE, "street is deactivated", None, []
        station_id, hits, stations = catalog.station_for(street.street_id, resolution.house_no)
        if not hits:
            return FAILED_STATION_NONE, "no coverage for house number", None, []
        if station_id is None:
            return (
                FAILED_STATION_AMBIGUOUS,
                "house number covered by multiple stations: " + ",".join(stations),
                None,
                hits,
            )
        return SUBMITTED, "committed", station_id, hits

    def _insert_job(
        self,
        item: AddressInput,
        resolution: Resolution,
        station_id: str,
        hits: Sequence[CoverageHit],
        catalog: Catalog,
    ) -> dict[str, Any]:
        street = resolution.candidates[0]
        interpretation = {
            "resolution": resolution.as_dict(),
            "coverage_hits": [h.as_dict() for h in hits],
        }
        self.conn.execute(
            "INSERT INTO jobs(parcel_id,identity_key,city_id,district_text_original,"
            "street_text_original,district_text_norm,street_text_norm,house_no,"
            "district_id,street_id,station_id,catalog_version,catalog_revision,"
            "source_file,source_row,interpretation_json,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.parcel_id,
                make_identity(item.city_id, item.district_text, item.street_text, item.house_no),
                item.city_id,
                item.district_text,
                item.street_text,
                normalize_text(item.district_text),
                normalize_text(item.street_text),
                item.house_no,
                street.district_id,
                street.street_id,
                station_id,
                catalog.version,
                catalog.revision,
                item.source_file,
                item.source_row,
                json.dumps(interpretation, ensure_ascii=False),
                _now(),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE parcel_id=?", (item.parcel_id,)
        ).fetchone()
        return _job_row(row)

    def _insert_attempt(
        self,
        item: AddressInput,
        outcome: str,
        message: str,
        job_created: bool,
        detail: dict,
        catalog: Catalog,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO attempts(parcel_id,source_file,source_row,city_id,"
            "district_text,street_text,house_no,catalog_version,outcome,message,"
            "job_created,detail_json,attempted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.parcel_id,
                item.source_file,
                item.source_row,
                item.city_id,
                item.district_text,
                item.street_text,
                item.house_no,
                catalog.version,
                outcome,
                message,
                1 if job_created else 0,
                json.dumps(detail, ensure_ascii=False),
                _now(),
            ),
        )
        return int(cur.lastrowid)

    # -- batch ---------------------------------------------------------------

    def attempt_batch(self, items: Sequence[AddressInput], catalog: Catalog) -> BatchResult:
        """Attempt each row independently, preserving source row/order.

        Structural validation is the caller's responsibility (see
        :mod:`address_delivery.input`); once here, a business failure on one row
        never prevents another row from being attempted.
        """
        results = [self.attempt(item, catalog) for item in items]
        source_file = next((i.source_file for i in items if i.source_file), None)
        return BatchResult(source_file=source_file, rows=results)


def _job_row(row) -> dict[str, Any]:
    return {
        "parcel_id": row["parcel_id"],
        "identity_key": json.loads(row["identity_key"]),
        "city_id": row["city_id"],
        "district_text_original": row["district_text_original"],
        "street_text_original": row["street_text_original"],
        "district_text": row["district_text_norm"],
        "street_text": row["street_text_norm"],
        "house_no": row["house_no"],
        "district_id": row["district_id"],
        "street_id": row["street_id"],
        "station_id": row["station_id"],
        "catalog_version": row["catalog_version"],
        "catalog_revision": row["catalog_revision"],
        "source_file": row["source_file"],
        "source_row": row["source_row"],
        "interpretation": json.loads(row["interpretation_json"]),
        "submitted_at": row["submitted_at"],
    }


def _attempt_row(row) -> dict[str, Any]:
    return {
        "attempt_id": row["attempt_id"],
        "parcel_id": row["parcel_id"],
        "source_file": row["source_file"],
        "source_row": row["source_row"],
        "city_id": row["city_id"],
        "district_text": row["district_text"],
        "street_text": row["street_text"],
        "house_no": row["house_no"],
        "catalog_version": row["catalog_version"],
        "outcome": row["outcome"],
        "message": row["message"],
        "job_created": bool(row["job_created"]),
        "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
        "attempted_at": row["attempted_at"],
    }
