"""Custody-responsibility handoff warehouse (SQLite-backed).

This store is independent of the physical catalog in
:mod:`asset_handoff.catalog` and owns three custody facts:

1. the **current** custodian of every asset (one row per asset id);
2. **preparations**: a handoff request validated against the current catalog
   and the current custodians, with its full member set materialized at
   prepare time;
3. **frozen handoff history**: once committed, the exact member identities,
   their types and the catalog revision at prepare time never change, even
   though the assets may later be moved around the physical forest.

The warehouse calls back into the catalog for identity/type lookups and root
expansion; it stores no parent edges and the catalog stores no custodians, so
neither side can silently derive the other's responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from .catalog import AssetCatalog
from .errors import (
    CustodianMismatchError,
    DuplicateHandoffError,
    HandoffNotFoundError,
    PreparedStaleError,
)

ASSET_HANDOFF_FORMAT = "asset-handoff/1"
STATUS_PREPARED = "PREPARED"
STATUS_COMMITTED = "COMMITTED"

_CUSTODY_VERSION_KEY = "custody_version"
_INITIAL_CUSTODY_VERSION = 1


class HandoffWarehouse:
    """Stores current custodians and frozen handoff memberships."""

    def __init__(self, path: str, catalog: AssetCatalog):
        """Open the warehouse database and bind it to an open ``catalog``.

        The two databases are separate files: closing/reopening one does not
        rebuild any state in the other, and the warehouse always reads current
        forest data through the live catalog object.
        """
        self.path = path
        self.catalog = catalog
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS custody (
                    asset_id  TEXT PRIMARY KEY,
                    custodian TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id          TEXT PRIMARY KEY,
                    status              TEXT NOT NULL
                        CHECK (status IN ('PREPARED', 'COMMITTED')),
                    roots_json          TEXT NOT NULL,
                    from_party          TEXT NOT NULL,
                    to_party            TEXT NOT NULL,
                    catalog_revision    INTEGER NOT NULL,
                    custody_version     INTEGER NOT NULL,
                    created_revision    INTEGER NOT NULL,
                    committed_revision  INTEGER
                );
                CREATE TABLE IF NOT EXISTS handoff_members (
                    handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
                    position   INTEGER NOT NULL,
                    asset_id   TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    custodian  TEXT NOT NULL,
                    PRIMARY KEY (handoff_id, position)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (_CUSTODY_VERSION_KEY, _INITIAL_CUSTODY_VERSION),
            )

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------- custody version
    @property
    def custody_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_CUSTODY_VERSION_KEY,)
        ).fetchone()
        return int(row[0])

    def _bump_custody_version(self) -> None:
        self.conn.execute(
            "UPDATE meta SET value = value + 1 WHERE key = ?",
            (_CUSTODY_VERSION_KEY,),
        )

    # --------------------------------------------------------------- current state
    def set_custodian(self, asset_id: str, custodian: str) -> None:
        """Set or replace the current custodian of one known asset.

        Used for the synthetic initial state and explicit custody edits. Any
        write bumps the custody version, so preparations made before the edit
        can no longer be committed.
        """
        self.catalog.get_asset_type(asset_id)  # unknown id raises early
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO custody(asset_id, custodian) VALUES (?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET custodian = excluded.custodian
                """,
                (asset_id, custodian),
            )
            self._bump_custody_version()

    def set_custodians(self, mapping: dict[str, str]) -> None:
        """Bulk variant of :meth:`set_custodian` in one transaction/version bump."""
        for asset_id in mapping:
            self.catalog.get_asset_type(asset_id)
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO custody(asset_id, custodian) VALUES (?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET custodian = excluded.custodian
                """,
                list(mapping.items()),
            )
            self._bump_custody_version()

    def get_custodian(self, asset_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT custodian FROM custody WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        return None if row is None else row[0]

    def current_custodians(self) -> dict[str, str]:
        return {
            asset_id: custodian
            for asset_id, custodian in self.conn.execute(
                "SELECT asset_id, custodian FROM custody ORDER BY asset_id"
            )
        }

    # ---------------------------------------------------------------- preparation
    def prepare(
        self,
        handoff_id: str,
        roots: Sequence[str],
        from_party: str,
        to_party: str,
    ) -> dict:
        """Validate a request against current state and freeze its members.

        The scanned ``roots`` are expanded through the catalog (deduped by
        asset id, containers included). Every expanded member must currently be
        held by ``from_party``; ownership of an outer box never implies
        ownership of its contents, so one mismatching member rejects the whole
        request and writes nothing.

        On success the full member set, each member's type/custodian and the
        catalog + custody revisions are materialized. Any later catalog or
        custody change makes the preparation uncommittable.
        """
        if from_party == to_party:
            raise ValueError("from_party and to_party must differ")

        existing = self.conn.execute(
            "SELECT 1 FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if existing is not None:
            raise DuplicateHandoffError(handoff_id)

        root_list = list(roots)
        if not root_list:
            raise ValueError("a handoff needs at least one scanned root")
        if len(set(root_list)) != len(root_list):
            raise ValueError("duplicate scanned root in request")

        # Catalog call: identity validation + forest expansion live there.
        member_ids = self.catalog.expand_roots(root_list)
        current = self.current_custodians()
        mismatches = [
            (asset_id, current.get(asset_id))
            for asset_id in member_ids
            if current.get(asset_id) != from_party
        ]
        if mismatches:
            raise CustodianMismatchError(from_party, mismatches)

        catalog_revision = self.catalog.revision
        custody_version = self.custody_version
        member_rows = [
            (
                position,
                asset_id,
                self.catalog.get_asset_type(asset_id),
                current[asset_id],
            )
            for position, asset_id in enumerate(member_ids)
        ]

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO handoffs(handoff_id, status, roots_json, from_party,
                                     to_party, catalog_revision, custody_version,
                                     created_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    STATUS_PREPARED,
                    json.dumps(root_list),
                    from_party,
                    to_party,
                    catalog_revision,
                    custody_version,
                    catalog_revision,
                ),
            )
            self.conn.executemany(
                """
                INSERT INTO handoff_members(
                    handoff_id, position, asset_id, asset_type, custodian)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (handoff_id, position, asset_id, asset_type, custodian)
                    for position, asset_id, asset_type, custodian in member_rows
                ],
            )
        return self.get_handoff(handoff_id)

    def commit(self, handoff_id: str) -> dict:
        """Commit a prepared handoff if the prepared snapshot is still current.

        Staleness rules (any one raises :class:`PreparedStaleError`):

        * the catalog revision moved since prepare (assets added or the forest
          rearranged), or
        * the custody version moved since prepare (any custodian changed);

        plus a per-member re-check that the frozen members are still held by
        ``from_party`` as defence in depth. On success every member's current
        custodian is atomically updated to ``to_party`` and the handoff becomes
        immutable history. Root-only updates are never enough: the frozen
        member rows are the exact set transferred.
        """
        row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise HandoffNotFoundError(handoff_id)
        record = self._row_to_dict(row)
        if record["status"] == STATUS_COMMITTED:
            raise PreparedStaleError(
                f"handoff {handoff_id!r} is already committed"
            )

        if self.catalog.revision != record["catalog_revision"]:
            raise PreparedStaleError(
                f"catalog changed since prepare: revision "
                f"{record['catalog_revision']} -> {self.catalog.revision}"
            )
        if self.custody_version != record["custody_version"]:
            raise PreparedStaleError(
                "custodians changed since prepare: custody version "
                f"{record['custody_version']} -> {self.custody_version}"
            )

        members = self._members(handoff_id)
        current = self.current_custodians()
        mismatches = [
            (member["asset_id"], current.get(member["asset_id"]))
            for member in members
            if current.get(member["asset_id"]) != record["from_party"]
        ]
        if mismatches:
            raise PreparedStaleError(
                f"members no longer held by {record['from_party']!r}: "
                + ", ".join(asset_id for asset_id, _ in mismatches)
            )

        member_ids = [member["asset_id"] for member in members]
        placeholders = ",".join("?" for _ in member_ids)
        with self.conn:
            self.conn.execute(
                f"UPDATE custody SET custodian = ? "
                f"WHERE asset_id IN ({placeholders})",
                [record["to_party"], *member_ids],
            )
            self.conn.execute(
                "UPDATE handoffs SET status = ?, committed_revision = ? "
                "WHERE handoff_id = ?",
                (STATUS_COMMITTED, self.catalog.revision, handoff_id),
            )
            self._bump_custody_version()
        return self.get_handoff(handoff_id)

    def prepare_and_commit(
        self,
        handoff_id: str,
        roots: Sequence[str],
        from_party: str,
        to_party: str,
    ) -> dict:
        """Convenience wrapper: prepare then immediately commit."""
        self.prepare(handoff_id, roots, from_party, to_party)
        return self.commit(handoff_id)

    # -------------------------------------------------------------------- reads
    def _members(self, handoff_id: str) -> list[dict]:
        return [
            {"asset_id": asset_id, "type": asset_type, "custodian": custodian}
            for _, asset_id, asset_type, custodian in self.conn.execute(
                """
                SELECT position, asset_id, asset_type, custodian
                FROM handoff_members WHERE handoff_id = ? ORDER BY position
                """,
                (handoff_id,),
            )
        ]

    def _row_to_dict(self, row: sqlite3.Row | tuple) -> dict:
        cols = (
            "handoff_id",
            "status",
            "roots_json",
            "from_party",
            "to_party",
            "catalog_revision",
            "custody_version",
            "created_revision",
            "committed_revision",
        )
        data = dict(zip(cols, row))
        data["roots"] = json.loads(data.pop("roots_json"))
        return data

    def get_handoff(self, handoff_id: str) -> dict:
        """Return one handoff with its frozen member list (any status)."""
        row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise HandoffNotFoundError(handoff_id)
        record = self._row_to_dict(row)
        record["members"] = self._members(handoff_id)
        return record

    def list_handoffs(self, include_prepared: bool = True) -> list[dict]:
        if include_prepared:
            rows = self.conn.execute(
                "SELECT * FROM handoffs ORDER BY handoff_id"
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM handoffs WHERE status = ? ORDER BY handoff_id",
                (STATUS_COMMITTED,),
            )
        return [self._row_to_dict(row) for row in rows]

    # --------------------------------------------------------------- artifact I/O
    def export_artifact(self, handoff_id: str, path: str) -> dict:
        """Write a handoff as a read-only ``asset-handoff/1`` JSON artifact.

        The artifact is self-describing: members carry their frozen types, so
        the supplied ``handoff_reader.py`` client needs no catalog or database
        to verify historical membership.
        """
        record = self.get_handoff(handoff_id)
        if record["status"] != STATUS_COMMITTED:
            raise PreparedStaleError(
                f"handoff {handoff_id!r} is not committed; "
                "only committed handoffs are exported as artifacts"
            )
        payload = {
            "format": ASSET_HANDOFF_FORMAT,
            "handoff_id": record["handoff_id"],
            "membership_revision": record["catalog_revision"],
            "roots": list(record["roots"]),
            "from": record["from_party"],
            "to": record["to_party"],
            "members": [
                {"asset_id": member["asset_id"], "type": member["type"]}
                for member in record["members"]
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return payload
