"""High-level service tying the catalog and the delivery store together.

``AddressService`` owns one SQLite connection shared by the catalog and the
delivery store, which is what makes a delivery commit atomic with respect to
the catalog state it read.  The two components themselves stay independent:
a catalog-only or delivery-only caller can still open just its own tables.

Operations mirror ``materials/707/operations.jsonl``:

* :meth:`import_csv` / :meth:`import_address_document` - read a sender file,
  reject structurally in full, then attempt each row independently.
* :meth:`apply_revision` - apply a catalog revision atomically.
* :meth:`retry` - re-attempt named parcels against the *current* catalog from
  their original address text.  Parcels that already committed are replayed
  instead, so a stale failure can never overwrite a success.
* :meth:`replay_submitted` - replay a committed parcel's frozen fact.
* :meth:`submit_new` - submit a new parcel directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .catalog import Catalog
from .db import connect
from .delivery import AttemptResult, BatchResult, DeliveryStore
from .input import parse_address_input_document, read_csv_addresses
from .models import AddressInput, validate_address_input


# Where to find a parcel's original address when retrying.
RETRY_ORIGINAL = "original"
RETRY_LAST_ATTEMPT = "last_attempt"


class AddressService:
    """Catalog + delivery facade over a single SQLite database."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self.conn = connect(self.db_path)
        self.catalog = Catalog(self.conn)
        self.delivery = DeliveryStore(self.conn)

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def from_materials(cls, materials_dir: str | Path, db_path: str | Path = ":memory:") -> "AddressService":
        """Create a service and load r1 catalog + coverage from a materials dir."""
        materials_dir = Path(materials_dir)
        service = cls(db_path)
        service.load_initial(materials_dir / "catalog.json", materials_dir / "coverage.json")
        return service

    @classmethod
    def open(cls, db_path: str | Path) -> "AddressService":
        """Re-open an existing database (schema already present)."""
        service = cls(db_path)
        return service

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AddressService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- catalog loading -----------------------------------------------------

    def load_initial(self, catalog_path: str | Path, coverage_path: str | Path) -> None:
        catalog_doc = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        coverage_doc = json.loads(Path(coverage_path).read_text(encoding="utf-8"))
        self.catalog.load_initial(catalog_doc, coverage_doc)

    def apply_revision(self, revision: dict | str | Path) -> int:
        if isinstance(revision, (str, Path)):
            revision = json.loads(Path(revision).read_text(encoding="utf-8"))
        return self.catalog.apply_revision(revision)

    # -- intakes -------------------------------------------------------------

    def import_csv(self, path: str | Path, *, source_file: str | None = None) -> BatchResult:
        """Read + structurally validate a CSV, then attempt each row.

        A structural defect raises :class:`InputFormatError` before any row is
        attempted, so the whole file is rejected without side effects.
        """
        items = read_csv_addresses(path, source_file=source_file)
        return self.delivery.attempt_batch(items, self.catalog)

    def import_address_document(self, document: dict, *, source_file: str | None = None) -> BatchResult:
        items = parse_address_input_document(document, source_file=source_file)
        return self.delivery.attempt_batch(items, self.catalog)

    def attempt_items(self, items: Sequence[AddressInput]) -> BatchResult:
        for item in items:
            validate_address_input(item)
        return self.delivery.attempt_batch(items, self.catalog)

    def submit_new(
        self,
        parcel_id: str,
        city_id: str,
        district_text: str,
        street_text: str,
        house_no: int,
        *,
        source_file: str | None = None,
        source_row: int | None = None,
    ) -> AttemptResult:
        item = AddressInput(
            parcel_id=parcel_id,
            city_id=city_id,
            district_text=district_text,
            street_text=street_text,
            house_no=house_no,
            source_file=source_file,
            source_row=source_row,
            kind="new_parcel",
        )
        validate_address_input(item)
        return self.delivery.attempt(item, self.catalog)

    # -- retry / replay ------------------------------------------------------

    def retry(
        self,
        parcel_ids: Iterable[str],
        *,
        original: str = RETRY_ORIGINAL,
    ) -> list[AttemptResult]:
        """Re-resolve failed parcels against the *current* catalog.

        Only parcels without a committed job are retried; a parcel that already
        succeeded is replayed (or identity-checked) instead, so a stale failure
        can never overwrite a success.

        ``original`` selects which stored text to re-resolve:

        * ``"original"`` - the text of the parcel's first attempt (the sender
          CSV / original input);
        * ``"last_attempt"`` - the text of the most recent attempt.
        """
        if original not in (RETRY_ORIGINAL, RETRY_LAST_ATTEMPT):
            raise ValueError("original must be 'original' or 'last_attempt'")
        results: list[AttemptResult] = []
        for parcel_id in parcel_ids:
            results.append(self._retry_one(parcel_id, original))
        return results

    def _retry_one(self, parcel_id: str, original: str) -> AttemptResult:
        job = self.delivery.get_job(parcel_id)
        attempts = self.delivery.get_attempts(parcel_id)
        if job is not None:
            item = _source_item_from_job(job)
            return self.delivery.attempt(item, self.catalog)
        if not attempts:
            return AttemptResult(
                parcel_id=parcel_id,
                source_row=None,
                outcome="unknown_parcel",
                message="no stored attempt to retry",
                job=None,
                resolution=None,
                station_id=None,
                coverage_hits=[],
            )
        attempt = attempts[0] if original == RETRY_ORIGINAL else attempts[-1]
        item = AddressInput(
            parcel_id=parcel_id,
            city_id=attempt["city_id"],
            district_text=attempt["district_text"],
            street_text=attempt["street_text"],
            house_no=attempt["house_no"],
            source_file=attempt["source_file"],
            source_row=attempt["source_row"],
            kind="retry",
        )
        return self.delivery.attempt(item, self.catalog)

    def replay_submitted(self, parcel_id: str) -> AttemptResult | None:
        """Replay a committed parcel's frozen result (no catalog lookup)."""
        job = self.delivery.get_job(parcel_id)
        if job is None:
            return None
        item = _source_item_from_job(job)
        return self.delivery.attempt(item, self.catalog)

    # -- queries -------------------------------------------------------------

    def resolve_address(
        self, city_id: str, district_text: str, street_text: str, house_no: int
    ) -> dict[str, Any]:
        """Candidate resolution plus coverage hits (no commit).

        Coverage is reported for *every* candidate so it can be inspected, but
        it never influences which candidate is the address: an ambiguous
        candidate set stays ambiguous even if only one candidate has a station.
        """
        resolution = self.catalog.resolve(city_id, district_text, street_text, house_no)
        payload = resolution.as_dict()
        candidate_coverage = []
        for candidate in resolution.candidates:
            station_id, hits, stations = self.catalog.station_for(
                candidate.street_id, house_no
            )
            candidate_coverage.append(
                {
                    "street_id": candidate.street_id,
                    "district_id": candidate.district_id,
                    "active": candidate.active,
                    "hits": [h.as_dict() for h in hits],
                    "stations": stations,
                    "station_id": station_id,
                }
            )
        payload["candidate_coverage"] = candidate_coverage
        street = resolution.street
        if street is None:
            payload["coverage"] = {"hits": [], "stations": [], "station_id": None}
        else:
            station_id, hits, stations = self.catalog.station_for(
                street.street_id, house_no
            )
            payload["coverage"] = {
                "hits": [h.as_dict() for h in hits],
                "stations": stations,
                "station_id": station_id,
            }
        return payload

    def resolve_input(self, item: AddressInput) -> dict[str, Any]:
        return self.resolve_address(
            item.city_id, item.district_text, item.street_text, item.house_no
        )

    def explain(self, parcel_id: str) -> dict[str, Any] | None:
        """Full audit: original text, attempts, candidates and final destination."""
        try:
            job = self.delivery.get_job(parcel_id)
            attempts = self.delivery.get_attempts(parcel_id)
        except Exception:  # pragma: no cover - defensive
            return None
        if job is None and not attempts:
            return None
        source_attempt = attempts[0] if attempts else None
        if source_attempt is not None:
            resolution = self.resolve_address(
                source_attempt["city_id"],
                source_attempt["district_text"],
                source_attempt["street_text"],
                source_attempt["house_no"],
            )
            original_text = {
                "city_id": source_attempt["city_id"],
                "district_text": source_attempt["district_text"],
                "street_text": source_attempt["street_text"],
                "house_no": source_attempt["house_no"],
                "source_file": source_attempt["source_file"],
                "source_row": source_attempt["source_row"],
            }
        else:
            resolution = None
            original_text = None
        return {
            "parcel_id": parcel_id,
            "original": original_text,
            "current_resolution": resolution,
            "attempts": attempts,
            "final_destination": self.delivery.final_destination(parcel_id),
        }

    # -- passthrough catalog queries ----------------------------------------

    def catalog_snapshot(self) -> dict[str, Any]:
        return self.catalog.snapshot()

    def catalog_street(self, street_id: str) -> dict[str, Any] | None:
        return self.catalog.get_street(street_id)

    def catalog_candidates(
        self, city_id: str, district_text: str, street_text: str
    ) -> list[dict[str, Any]]:
        return self.catalog.lookup_street_by_name(city_id, district_text, street_text)

    def coverage_hits(self, street_id: str, house_no: int) -> list[dict[str, Any]]:
        return [h.as_dict() for h in self.catalog.coverage_hits(street_id, house_no)]


def _source_item_from_job(job: dict[str, Any]) -> AddressInput:
    """Reconstruct the address originally submitted for a committed parcel."""
    return AddressInput(
        parcel_id=job["parcel_id"],
        city_id=job["city_id"],
        district_text=job["district_text_original"],
        street_text=job["street_text_original"],
        house_no=job["house_no"],
        source_file=job["source_file"],
        source_row=job["source_row"],
        kind="replay",
    )
