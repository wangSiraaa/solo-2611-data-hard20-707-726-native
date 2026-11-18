"""One facade over the SQLite store that owns all four services."""

from __future__ import annotations

import sqlite3

from . import db as _db
from .composition import CompositionService
from .unload import PlatformRepository, UnloadService


class RailwayStore:
    """A single connection, four cooperating repositories.

    Sharing one connection keeps the composition-version check in
    ``confirm_unload`` and the cargo status flip inside the same SQLite
    transaction, which is the atomic boundary the task requires.
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn: sqlite3.Connection = _db.connect(path)
        self.composition = CompositionService(self.conn)
        self.platform = PlatformRepository(self.conn)
        self.unload = UnloadService(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RailwayStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # convenience aliases -------------------------------------------------

    def add_car(self, car_id: str, length) -> dict:
        return self.composition.add_car(car_id, length)

    def add_cargo(self, cargo_id: str, car_id: str, local_position) -> dict:
        from .unload import add_cargo

        return add_cargo(self.conn, cargo_id, car_id, local_position)

    def get_cargo(self, cargo_id: str) -> dict:
        return self.unload.get_cargo(cargo_id)

    def create_train(self, train_id: str, cars: list[dict]) -> dict:
        return self.composition.create_train(train_id, cars)

    def apply_event(self, request: dict) -> dict:
        return self.composition.apply_event(request)

    def composition_view(self, train_id: str) -> dict:
        return self.composition.composition(train_id)

    def coordinate(self, train_id: str, car_id: str, local_position) -> dict:
        return self.composition.car_coordinate(train_id, car_id, local_position)

    def cargo_coordinate(self, train_id: str, cargo_id: str) -> dict:
        return self.unload.cargo_coordinate(train_id, cargo_id)

    def scan(
        self,
        train_id: str,
        report_id: str,
        window,
        composition_version: str | None = None,
        cargo_ids: list[str] | None = None,
    ) -> dict:
        return self.platform.scan_platform(
            train_id, report_id, window, composition_version, cargo_ids
        )

    def prepare_unload(
        self, job_id: str, train_id: str, composition_version: str, window, cargo_ids
    ) -> dict:
        return self.unload.prepare_unload(
            job_id, train_id, composition_version, window, cargo_ids
        )

    def confirm_unload(self, job_id: str) -> dict:
        return self.unload.confirm_unload(job_id)

    def cancel_unload(self, job_id: str, reason: str | None = None) -> dict:
        return self.unload.cancel_unload(job_id, reason)
