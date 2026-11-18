"""Platform scanning and the deferred unloading job repository.

Two separate concerns, two separate sets of tables:

* ``reports`` / ``report_positions`` — immutable observations produced by a
  scan of one composition version.  A report is *not* an unload authorisation.
* ``unload_jobs`` / ``unload_receipts`` — planned work and the authoritative
  record of what was actually unloaded.

Cargo identity is permanent (``cargo_id`` -> permanent ``car_id`` + intrinsic
``local_position``); a cargo is never identified by its current car ordinal.
A cargo can only go LOADED -> UNLOADED, never back.
"""

from __future__ import annotations

import json
from fractions import Fraction

from . import db as _db
from .composition import load_composition
from .model import (
    ConflictError,
    InactiveError,
    NotFoundError,
    UnloadError,
    ValidationError,
    canonical,
    content_hash,
    frac_from_json,
    frac_to_json,
    normalize_id,
    parse_nonnegative_rational,
)

REPORT_SCHEMA = "platform-scan/v1"


def _parse_window(window) -> tuple[Fraction, Fraction]:
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ValidationError("window must be a [start, end) pair")
    start = _parse_num(window[0], "window start")
    end = _parse_num(window[1], "window end")
    if start < 0:
        raise ValidationError("window start must be >= 0")
    if end <= start:
        raise ValidationError("window must satisfy start < end (half-open)")
    return start, end


def _parse_num(value, what: str) -> Fraction:
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be a rational, got bool")
    try:
        if isinstance(value, (int, str, Fraction)):
            return Fraction(value)
        return frac_from_json(value)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValidationError(f"{what} must be a rational: {value!r}") from exc


def _window_json(window) -> dict:
    return {"start": frac_to_json(window[0]), "end": frac_to_json(window[1])}


def _in_window(x: Fraction, window) -> bool:
    return window[0] <= x < window[1]


def _cargo_row(conn, cargo_id: str):
    return conn.execute("SELECT * FROM cargo WHERE cargo_id = ?", (cargo_id,)).fetchone()


def _assignment_map(conn, train_id: str) -> dict[str, object]:
    """car_id -> CarPlacement for the current active composition of a train."""
    return load_composition(conn, train_id).by_car()


def _cargo_x(conn, cargo_id: str, placements: dict) -> tuple[Fraction, Fraction, str]:
    row = _cargo_row(conn, cargo_id)
    if row is None:
        raise NotFoundError(f"cargo {cargo_id!r} does not exist")
    placement = placements.get(row["car_id"])
    if placement is None:
        raise ValidationError(f"cargo {cargo_id!r} is not in this train")
    local = Fraction(row["local_n"], row["local_d"])
    return placement.map_local(local), local, row["car_id"]


# ---------------------------------------------------------------------------
# Platform scans
# ---------------------------------------------------------------------------


class PlatformRepository:
    def __init__(self, conn):
        self.conn = conn

    def scan_platform(
        self,
        train_id: str,
        report_id: str,
        window,
        composition_version: str | None = None,
        cargo_ids: list[str] | None = None,
    ) -> dict:
        """Observe cargo coordinates for one composition version.

        ``window`` is half-open ``[start, end)``.  If ``composition_version`` is
        supplied it must be the current version of the train; scans never
        fabricate a version from a stale view.
        """
        train_id = normalize_id(train_id, "train_id")
        report_id = normalize_id(report_id, "report_id")
        start, end = _parse_window(window)
        if composition_version is not None:
            composition_version = normalize_id(composition_version, "composition_version")
        request_hash = content_hash(
            {
                "op": "scan_platform",
                "train_id": train_id,
                "report_id": report_id,
                "composition_version": composition_version,
                "window": [frac_to_json(start), frac_to_json(end)],
                "cargo_ids": sorted(cargo_ids) if cargo_ids is not None else None,
            }
        )
        existing = self.conn.execute(
            "SELECT request_hash, report_json FROM reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ConflictError(
                    f"report_id {report_id!r} already exists with different content"
                )
            return json.loads(existing["report_json"])

        row = self.conn.execute(
            "SELECT * FROM trains WHERE train_id = ?", (train_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"train {train_id!r} does not exist")
        if not row["active"]:
            raise InactiveError(f"train {train_id!r} is not an active composition")
        if composition_version is not None and row["composition_version"] != composition_version:
            raise ValidationError(
                f"train {train_id!r} is at version {row['composition_version']}, "
                f"not {composition_version}"
            )
        version = row["composition_version"]

        placements = _assignment_map(self.conn, train_id)
        if cargo_ids is None:
            cars = list(placements)
            marks = ",".join("?" for _ in cars) or "''"
            rows = self.conn.execute(
                f"SELECT cargo_id FROM cargo WHERE car_id IN ({marks}) ORDER BY cargo_id",
                cars,
            ).fetchall()
            selected = [r["cargo_id"] for r in rows]
        else:
            if len(cargo_ids) != len(set(cargo_ids)):
                raise ValidationError("cargo_ids must not repeat")
            selected = list(cargo_ids)

        positions = []
        for cargo_id in selected:
            cargo_id = normalize_id(cargo_id, "cargo_id")
            x, local, car_id = _cargo_x(self.conn, cargo_id, placements)
            crow = _cargo_row(self.conn, cargo_id)
            positions.append(
                {
                    "cargo_id": cargo_id,
                    "car_id": car_id,
                    "x": frac_to_json(x),
                    "in_window": _in_window(x, (start, end)),
                    "orientation": placements[car_id].orientation,
                    "local_position": frac_to_json(local),
                    "cargo_status": crow["status"],
                }
            )

        report = {
            "schema": REPORT_SCHEMA,
            "report_id": report_id,
            "train_id": train_id,
            "composition_version": version,
            "window": _window_json((start, end)),
            "cargo_positions": positions,
        }
        with _db.write_tx(self.conn):
            again = self.conn.execute(
                "SELECT request_hash, report_json FROM reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
            if again is not None:
                if again["request_hash"] != request_hash:
                    raise ConflictError(
                        f"report_id {report_id!r} already exists with different content"
                    )
                return json.loads(again["report_json"])
            seq = _db.next_seq(self.conn)
            self.conn.execute(
                "INSERT INTO reports(report_id, schema, train_id, composition_version, "
                "window_start_n, window_start_d, window_end_n, window_end_d, "
                "request_hash, report_json, created_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    REPORT_SCHEMA,
                    train_id,
                    version,
                    start.numerator,
                    start.denominator,
                    end.numerator,
                    end.denominator,
                    request_hash,
                    canonical(report),
                    seq,
                ),
            )
            for i, item in enumerate(positions):
                self.conn.execute(
                    "INSERT INTO report_positions(report_id, seq, cargo_id, car_id, "
                    "x_n, x_d, in_window, cargo_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        report_id,
                        i,
                        item["cargo_id"],
                        item["car_id"],
                        item["x"]["n"],
                        item["x"]["d"],
                        int(item["in_window"]),
                        item["cargo_status"],
                    ),
                )
        return report

    def get_report(self, report_id: str) -> dict:
        row = self.conn.execute(
            "SELECT report_json FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"report {report_id!r} does not exist")
        return json.loads(row["report_json"])

    def list_reports(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT report_id FROM reports ORDER BY created_seq"
        ).fetchall()
        return [self.get_report(r["report_id"]) for r in rows]

    def write_report(self, report_id: str, path: str) -> None:
        report = self.get_report(report_id)
        from pathlib import Path

        Path(path).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Unload jobs
# ---------------------------------------------------------------------------

JOB_PREPARED = "PREPARED"
JOB_CONFIRMED = "CONFIRMED"
JOB_CANCELLED = "CANCELLED"


def _prepare_hash(train_id, version, window, cargo_ids) -> str:
    return content_hash(
        {
            "op": "prepare_unload",
            "train_id": train_id,
            "composition_version": version,
            "window": [frac_to_json(window[0]), frac_to_json(window[1])],
            "cargo_ids": sorted(cargo_ids),
        }
    )


class UnloadService:
    def __init__(self, conn):
        self.conn = conn

    # -- helpers ---------------------------------------------------------

    def _job_row(self, job_id: str):
        return self.conn.execute(
            "SELECT * FROM unload_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

    def _validate_basis(
        self, train_id, version, window, cargo_ids, revision=None, with_revision=False
    ):
        """Check the job basis against current state.  Read-only.

        With ``with_revision`` the current revision is returned alongside the
        item list (used when recording a newly prepared job).
        """
        train = self.conn.execute(
            "SELECT * FROM trains WHERE train_id = ?", (train_id,)
        ).fetchone()
        if train is None:
            raise NotFoundError(f"train {train_id!r} does not exist")
        if not train["active"]:
            raise InactiveError(f"train {train_id!r} is not an active composition")
        if train["composition_version"] != version or (
            revision is not None and train["revision"] != revision
        ):
            raise UnloadError(
                f"composition of {train_id!r} is not the requested basis "
                f"(now revision {train['revision']}, "
                f"{train['composition_version']})"
            )
        placements = _assignment_map(self.conn, train_id)
        items = []
        for cargo_id in cargo_ids:
            row = _cargo_row(self.conn, cargo_id)
            if row is None:
                raise ValidationError(f"cargo {cargo_id!r} does not exist")
            if row["status"] != "LOADED":
                raise UnloadError(f"cargo {cargo_id!r} is {row['status']}, not LOADED")
            placement = placements.get(row["car_id"])
            if placement is None:
                raise UnloadError(
                    f"cargo {cargo_id!r} is not in the active composition of {train_id!r}"
                )
            local = Fraction(row["local_n"], row["local_d"])
            x = placement.map_local(local)
            if not _in_window(x, window):
                raise UnloadError(
                    f"cargo {cargo_id!r} at x={x} is outside platform window "
                    f"[{window[0]}, {window[1]})"
                )
            items.append(
                {
                    "cargo_id": cargo_id,
                    "car_id": row["car_id"],
                    "local_position": frac_to_json(local),
                    "x": frac_to_json(x),
                    "in_window": True,
                    "orientation": placement.orientation,
                    "previous_status": row["status"],
                }
            )
        if with_revision:
            return items, train["revision"]
        return items

    # -- prepare ---------------------------------------------------------

    def prepare_unload(
        self,
        job_id: str,
        train_id: str,
        composition_version: str,
        window,
        cargo_ids,
    ) -> dict:
        job_id = normalize_id(job_id, "job_id")
        train_id = normalize_id(train_id, "train_id")
        composition_version = normalize_id(composition_version, "composition_version")
        start, end = _parse_window(window)
        if not isinstance(cargo_ids, (list, tuple)) or not cargo_ids:
            raise ValidationError("cargo_ids must be a non-empty list")
        ids = [normalize_id(c, "cargo_id") for c in cargo_ids]
        if len(ids) != len(set(ids)):
            raise ValidationError("cargo_ids must not contain duplicates")
        req_hash = _prepare_hash(train_id, composition_version, (start, end), ids)

        existing = self._job_row(job_id)
        if existing is not None:
            if existing["preparation_hash"] != req_hash:
                raise ConflictError(
                    f"job_id {job_id!r} already exists with different content"
                )
            return self._job_view(existing)

        # Validate the basis before opening a write transaction so a refused
        # prepare reports its own reason (wrong version, cargo outside window,
        # already UNLOADED, …) instead of a generic composition-changed error.
        self._validate_basis(train_id, composition_version, (start, end), ids)

        with _db.write_tx(self.conn):
            again = self._job_row(job_id)
            if again is not None:
                if again["preparation_hash"] != req_hash:
                    raise ConflictError(
                        f"job_id {job_id!r} already exists with different content"
                    )
                return self._job_view(again)
            items, revision = self._validate_basis(
                train_id, composition_version, (start, end), ids, with_revision=True
            )
            seq = _db.next_seq(self.conn)
            self.conn.execute(
                "INSERT INTO unload_jobs(job_id, train_id, composition_version, "
                "prepared_revision, state, "
                "window_start_n, window_start_d, window_end_n, window_end_d, "
                "cargo_ids_json, preparation_hash, items_json, prepared_seq) "
                "VALUES (?, ?, ?, ?, 'PREPARED', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    train_id,
                    composition_version,
                    revision,
                    start.numerator,
                    start.denominator,
                    end.numerator,
                    end.denominator,
                    canonical(sorted(ids)),
                    req_hash,
                    canonical(items),
                    seq,
                ),
            )
            row = self._job_row(job_id)
        return self._job_view(row)

    # -- confirm ---------------------------------------------------------

    def confirm_unload(self, job_id: str) -> dict:
        job_id = normalize_id(job_id, "job_id")
        row = self._job_row(job_id)
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        if row["state"] == JOB_CONFIRMED:
            # terminal replay: same content -> original receipt
            return self._job_view(row)
        if row["state"] == JOB_CANCELLED:
            raise UnloadError(f"job {job_id!r} was cancelled and cannot be confirmed")

        with _db.write_tx(self.conn):
            row = self._job_row(job_id)
            if row is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            if row["state"] == JOB_CONFIRMED:
                return self._job_view(row)
            if row["state"] == JOB_CANCELLED:
                raise UnloadError(f"job {job_id!r} was cancelled and cannot be confirmed")
            cargo_ids = json.loads(row["cargo_ids_json"])
            window = (
                Fraction(row["window_start_n"], row["window_start_d"]),
                Fraction(row["window_end_n"], row["window_end_d"]),
            )
            # Atomic re-validation of the whole job basis.  Any failure raises
            # and the transaction rolls back, leaving every cargo LOADED.
            items = self._validate_basis(
                row["train_id"],
                row["composition_version"],
                window,
                cargo_ids,
                revision=row["prepared_revision"],
            )
            seq = _db.next_seq(self.conn)
            receipt_id = f"RC-{job_id}"
            receipt = {
                "receipt_id": receipt_id,
                "job_id": job_id,
                "train_id": row["train_id"],
                "composition_version": row["composition_version"],
                "window": _window_json(window),
                "items": items,
            }
            for item in items:
                cur = self.conn.execute(
                    "UPDATE cargo SET status = 'UNLOADED', unload_job_id = ?, "
                    "updated_seq = ? WHERE cargo_id = ? AND status = 'LOADED'",
                    (job_id, seq, item["cargo_id"]),
                )
                if cur.rowcount != 1:
                    raise UnloadError(
                        f"cargo {item['cargo_id']!r} changed state during confirm"
                    )
            self.conn.execute(
                "UPDATE unload_jobs SET state = 'CONFIRMED', confirmed_seq = ?, "
                "receipt_id = ?, receipt_json = ? WHERE job_id = ?",
                (seq, receipt_id, canonical(receipt), job_id),
            )
            self.conn.execute(
                "INSERT INTO unload_receipts(receipt_id, job_id, train_id, "
                "composition_version, window_start_n, window_start_d, window_end_n, "
                "window_end_d, items_json, receipt_json, created_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    job_id,
                    row["train_id"],
                    row["composition_version"],
                    window[0].numerator,
                    window[0].denominator,
                    window[1].numerator,
                    window[1].denominator,
                    canonical(items),
                    canonical(receipt),
                    seq,
                ),
            )
            row = self._job_row(job_id)
        return self._job_view(row)

    # -- cancel ----------------------------------------------------------

    def cancel_unload(self, job_id: str, reason: str | None = None) -> dict:
        job_id = normalize_id(job_id, "job_id")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("reason must be a string")
        reason_hash = content_hash({"op": "cancel_unload", "job_id": job_id, "reason": reason})
        row = self._job_row(job_id)
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        if row["state"] == JOB_CONFIRMED:
            raise UnloadError(f"job {job_id!r} is confirmed and cannot be cancelled")
        if row["state"] == JOB_CANCELLED:
            if row["cancel_hash"] != reason_hash:
                raise ConflictError(
                    f"job {job_id!r} was cancelled with different content"
                )
            return self._job_view(row)
        with _db.write_tx(self.conn):
            row = self._job_row(job_id)
            if row is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            if row["state"] == JOB_CONFIRMED:
                raise UnloadError(f"job {job_id!r} is confirmed and cannot be cancelled")
            if row["state"] == JOB_CANCELLED:
                if row["cancel_hash"] != reason_hash:
                    raise ConflictError(
                        f"job {job_id!r} was cancelled with different content"
                    )
                return self._job_view(row)
            seq = _db.next_seq(self.conn)
            self.conn.execute(
                "UPDATE unload_jobs SET state = 'CANCELLED', cancel_reason = ?, "
                "cancel_hash = ? WHERE job_id = ?",
                (reason, reason_hash, job_id),
            )
            row = self._job_row(job_id)
        return self._job_view(row)

    # -- queries ---------------------------------------------------------

    def _job_view(self, row) -> dict:
        base = {
            "job_id": row["job_id"],
            "train_id": row["train_id"],
            "composition_version": row["composition_version"],
            "prepared_revision": row["prepared_revision"],
            "state": row["state"],
            "window": {
                "start": frac_to_json(Fraction(row["window_start_n"], row["window_start_d"])),
                "end": frac_to_json(Fraction(row["window_end_n"], row["window_end_d"])),
            },
            "cargo_ids": json.loads(row["cargo_ids_json"]),
            "items": json.loads(row["items_json"]),
            "prepared_seq": row["prepared_seq"],
        }
        if row["state"] == JOB_CONFIRMED:
            base["confirmed_seq"] = row["confirmed_seq"]
            base["receipt"] = json.loads(row["receipt_json"])
        if row["state"] == JOB_CANCELLED:
            base["cancel_reason"] = row["cancel_reason"]
        return base

    def get_job(self, job_id: str) -> dict:
        row = self._job_row(job_id)
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        return self._job_view(row)

    def list_jobs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT job_id FROM unload_jobs ORDER BY prepared_seq"
        ).fetchall()
        return [self.get_job(r["job_id"]) for r in rows]

    def get_receipt(self, job_id: str) -> dict:
        row = self.conn.execute(
            "SELECT receipt_json FROM unload_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        if row["receipt_json"] is None:
            raise UnloadError(f"job {job_id!r} has no receipt (not confirmed)")
        return json.loads(row["receipt_json"])

    def get_cargo(self, cargo_id: str) -> dict:
        row = _cargo_row(self.conn, cargo_id)
        if row is None:
            raise NotFoundError(f"cargo {cargo_id!r} does not exist")
        return {
            "cargo_id": row["cargo_id"],
            "car_id": row["car_id"],
            "local_position": frac_to_json(Fraction(row["local_n"], row["local_d"])),
            "status": row["status"],
            "unload_job_id": row["unload_job_id"],
        }

    def list_cargo(self) -> list[dict]:
        rows = self.conn.execute("SELECT cargo_id FROM cargo ORDER BY cargo_id").fetchall()
        return [self.get_cargo(r["cargo_id"]) for r in rows]

    def cargo_coordinate(self, train_id: str, cargo_id: str) -> dict:
        """Current whole-train coordinate of one cargo in an active train."""
        train_id = normalize_id(train_id, "train_id")
        cargo_id = normalize_id(cargo_id, "cargo_id")
        train = self.conn.execute(
            "SELECT * FROM trains WHERE train_id = ?", (train_id,)
        ).fetchone()
        if train is None:
            raise NotFoundError(f"train {train_id!r} does not exist")
        if not train["active"]:
            raise InactiveError(f"train {train_id!r} is not an active composition")
        placements = _assignment_map(self.conn, train_id)
        x, local, car_id = _cargo_x(self.conn, cargo_id, placements)
        row = _cargo_row(self.conn, cargo_id)
        return {
            "train_id": train_id,
            "composition_version": train["composition_version"],
            "cargo_id": cargo_id,
            "car_id": car_id,
            "orientation": placements[car_id].orientation,
            "local_position": frac_to_json(local),
            "x": frac_to_json(x),
            "status": row["status"],
        }


# ---------------------------------------------------------------------------
# Cargo registration / material loading
# ---------------------------------------------------------------------------


def add_cargo(conn, cargo_id: str, car_id: str, local_position) -> dict:
    """Register permanent cargo identity, always starting LOADED.

    There is deliberately no API to register cargo already UNLOADED: the only
    LOADED -> UNLOADED transition is a confirmed unload job.
    """
    cargo_id = normalize_id(cargo_id, "cargo_id")
    car_id = normalize_id(car_id, "car_id")
    local = parse_nonnegative_rational(local_position, "local_position")
    with _db.write_tx(conn):
        if conn.execute("SELECT 1 FROM cargo WHERE cargo_id = ?", (cargo_id,)).fetchone():
            raise ValidationError(f"cargo {cargo_id!r} already exists")
        car = conn.execute("SELECT * FROM cars WHERE car_id = ?", (car_id,)).fetchone()
        if car is None:
            raise NotFoundError(f"car {car_id!r} does not exist")
        length = Fraction(car["length_n"], car["length_d"])
        if local > length:
            raise ValidationError(
                f"local_position {local} is outside car {car_id!r} length {length}"
            )
        seq = _db.next_seq(conn)
        conn.execute(
            "INSERT INTO cargo(cargo_id, car_id, local_n, local_d, status, updated_seq) "
            "VALUES (?, ?, ?, ?, 'LOADED', ?)",
            (cargo_id, car_id, local.numerator, local.denominator, seq),
        )
    return {
        "cargo_id": cargo_id,
        "car_id": car_id,
        "local_position": frac_to_json(local),
        "status": "LOADED",
    }
