"""SQLite-backed stock ledger for synthetic one-dimensional coils.

Design notes
------------
* Physical truth lives in the ledger, not the geometry module: pieces store the
  root interval ``[root_lo, root_hi)`` and their current orientation.  The
  ledger calls :func:`coilcut.geometry.cut_geometry` and persists its exact
  result, so every child's final root source is always explicit.
* Every mutation runs in a single ``BEGIN IMMEDIATE`` transaction.  A failed
  cut/delivery rolls back completely and leaves no child rows, no idempotency
  key and no partial revision bump.
* ``cuts.cut_id`` and ``deliveries.delivery_id`` are idempotency keys: replaying
  identical content returns the stored result verbatim; reusing the key with
  different content raises :class:`ConflictError`.  Rows are only written on
  success, so failed attempts never occupy an id.
* ``cuts`` has ``UNIQUE(parent_piece_id)`` and the parent is retired in the same
  transaction, so one parent can never be consumed by two cuts.
* Retired parents stay in ``pieces`` for lineage but are excluded from root
  coverage accounting; their children and kerf replace them exactly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Iterable, Iterator

from .errors import (
    AlreadyInitializedError,
    ConflictError,
    DuplicatePieceIdError,
    InvalidStateError,
    InvariantError,
    LengthMismatchError,
    NotFoundError,
    PieceConsumedError,
    StaleOrientationError,
    StaleRevisionError,
)
from .geometry import PieceGeometry, cut_geometry
from .rational import as_fraction, fparse, fstr, jscalar

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roots (
    root_id TEXT PRIMARY KEY,
    length  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pieces (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id      TEXT NOT NULL UNIQUE,
    root_id       TEXT NOT NULL REFERENCES roots(root_id),
    root_lo       TEXT NOT NULL,
    root_hi       TEXT NOT NULL,
    orientation   INTEGER NOT NULL CHECK (orientation IN (-1, 1)),
    revision      INTEGER NOT NULL CHECK (revision >= 1),
    status        TEXT NOT NULL CHECK (status IN ('active', 'delivered', 'retired')),
    origin_cut_id TEXT
);

CREATE TABLE IF NOT EXISTS cuts (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    cut_id          TEXT NOT NULL UNIQUE,
    root_id         TEXT NOT NULL REFERENCES roots(root_id),
    parent_piece_id TEXT NOT NULL UNIQUE REFERENCES pieces(piece_id),
    parent_revision INTEGER NOT NULL,
    parent_orientation INTEGER NOT NULL,
    u               TEXT NOT NULL,
    kerf            TEXT NOT NULL,
    left_child_id   TEXT NOT NULL REFERENCES pieces(piece_id),
    right_child_id  TEXT NOT NULL REFERENCES pieces(piece_id),
    kerf_lo         TEXT NOT NULL,
    kerf_hi         TEXT NOT NULL,
    fingerprint     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flips (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id        TEXT NOT NULL REFERENCES pieces(piece_id),
    old_revision    INTEGER NOT NULL,
    new_revision    INTEGER NOT NULL,
    old_orientation INTEGER NOT NULL,
    new_orientation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  TEXT NOT NULL UNIQUE,
    piece_id     TEXT NOT NULL UNIQUE REFERENCES pieces(piece_id),
    required_length TEXT NOT NULL,
    actual_length   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pieces_root ON pieces(root_id, status);
CREATE INDEX IF NOT EXISTS idx_cuts_root ON cuts(root_id);
"""

DELIVERY_SCHEMA = "piece-delivery/v1"


def _fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class Ledger:
    """A transactional stock ledger.  One instance == one SQLite connection."""

    def __init__(self, path: str, *, timeout: float = 30.0) -> None:
        self.path = path
        self._conn = sqlite3.connect(
            path, isolation_level=None, timeout=timeout, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    # ---- connection helpers -------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- initialisation -----------------------------------------------------

    def initialize(self, roots: Iterable[dict[str, Any]], active_pieces: Iterable[dict[str, Any]], kerf: Any, tool_id: str = "SAW") -> None:
        """Seed roots, initial pieces and the default tool kerf exactly once."""

        roots = list(roots)
        pieces = list(active_pieces)
        with self._write_tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
            if row is not None:
                raise AlreadyInitializedError("ledger is already initialized")
            for root in roots:
                length = as_fraction(root["length"], field="root.length")
                if length <= 0:
                    raise InvalidStateError("root length must be positive", root_id=root.get("root_id"))
                conn.execute(
                    "INSERT INTO roots(root_id, length) VALUES (?, ?)",
                    (root["root_id"], fstr(length)),
                )
            for piece in pieces:
                root_id = piece["root_id"]
                root_row = conn.execute("SELECT length FROM roots WHERE root_id=?", (root_id,)).fetchone()
                if root_row is None:
                    raise NotFoundError("unknown root for initial piece", root_id=root_id)
                geo = PieceGeometry(
                    piece_id=piece["piece_id"],
                    root_id=root_id,
                    root_lo=as_fraction(piece["root_lo"], field="root_lo"),
                    root_hi=as_fraction(piece["root_hi"], field="root_hi"),
                    orientation=int(piece["orientation"]),
                )
                if geo.root_hi > fparse(root_row["length"]):
                    raise InvalidStateError(
                        "initial piece exceeds root", piece_id=geo.piece_id, root_id=root_id
                    )
                conn.execute(
                    "INSERT INTO pieces(piece_id, root_id, root_lo, root_hi, orientation, revision, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active')",
                    (
                        geo.piece_id,
                        geo.root_id,
                        fstr(geo.root_lo),
                        fstr(geo.root_hi),
                        geo.orientation,
                        int(piece.get("revision", 1)),
                    ),
                )
            k = as_fraction(kerf, field="kerf")
            if k <= 0:
                raise InvalidStateError("kerf must be positive")
            conn.execute("INSERT INTO meta(key, value) VALUES('kerf', ?)", (fstr(k),))
            conn.execute("INSERT INTO meta(key, value) VALUES('tool_id', ?)", (str(tool_id),))
            conn.execute("INSERT INTO meta(key, value) VALUES('initialized', '1')")

    # ---- reads --------------------------------------------------------------

    def default_kerf(self) -> Fraction:
        row = self._conn.execute("SELECT value FROM meta WHERE key='kerf'").fetchone()
        if row is None:
            raise InvalidStateError("ledger has no default kerf; call initialize()")
        return fparse(row["value"])

    def root_length(self, root_id: str) -> Fraction:
        row = self._conn.execute("SELECT length FROM roots WHERE root_id=?", (root_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown root", root_id=root_id)
        return fparse(row["length"])

    @staticmethod
    def _piece_row_to_geo(row: sqlite3.Row) -> PieceGeometry:
        return PieceGeometry(
            piece_id=row["piece_id"],
            root_id=row["root_id"],
            root_lo=fparse(row["root_lo"]),
            root_hi=fparse(row["root_hi"]),
            orientation=row["orientation"],
        )

    def get_piece(self, piece_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown piece", piece_id=piece_id)
        out = self._piece_row_to_geo(row).to_dict()
        out["revision"] = row["revision"]
        out["status"] = row["status"]
        out["origin_cut_id"] = row["origin_cut_id"]
        return out

    def _require_active(self, conn: sqlite3.Connection, piece_id: str, expected_revision: int | None, expected_orientation: int | None) -> PieceGeometry:
        row = conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown piece", piece_id=piece_id)
        if row["status"] != "active":
            raise PieceConsumedError(
                "piece is not active", piece_id=piece_id, status=row["status"]
            )
        if expected_revision is not None and row["revision"] != expected_revision:
            raise StaleRevisionError(
                "expected_revision does not match the active piece",
                piece_id=piece_id,
                expected_revision=expected_revision,
                actual_revision=row["revision"],
            )
        if expected_orientation is not None and row["orientation"] != expected_orientation:
            raise StaleOrientationError(
                "expected_orientation does not match the active piece",
                piece_id=piece_id,
                expected_orientation=expected_orientation,
                actual_orientation=row["orientation"],
            )
        return self._piece_row_to_geo(row)

    # ---- cut ----------------------------------------------------------------

    def preview_cut(
        self,
        *,
        piece_id: str,
        u: Any,
        left_child: str,
        right_child: str,
        kerf: Any | None = None,
    ) -> dict[str, Any]:
        """Compute a cut without touching inventory.

        The preview records the piece revision *and* orientation it was computed
        against.  Committing it later through :meth:`commit_preview` re-checks
        both, so a flip between preview and commit can never submit the stale
        direction.
        """

        row = self._conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown piece", piece_id=piece_id)
        if row["status"] != "active":
            raise PieceConsumedError(
                "piece is not active", piece_id=piece_id, status=row["status"]
            )
        parent = self._piece_row_to_geo(row)
        kerf_f = as_fraction(kerf, field="kerf") if kerf is not None else self.default_kerf()
        geo = cut_geometry(
            parent,
            as_fraction(u, field="u"),
            kerf_f,
            left_child,
            right_child,
            root_length=self.root_length(parent.root_id),
        )
        return {
            "piece_id": piece_id,
            "revision": row["revision"],
            "orientation": row["orientation"],
            "u": jscalar(geo.u),
            "kerf": jscalar(geo.kerf),
            "left_child": left_child,
            "right_child": right_child,
            "geometry": geo.to_dict(),
        }

    def commit_preview(self, cut_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        """Commit a preview only if the piece is still at the previewed state."""

        return self.cut(
            cut_id=cut_id,
            piece_id=preview["piece_id"],
            expected_revision=preview["revision"],
            expected_orientation=preview["orientation"],
            u=preview["u"],
            kerf=preview["kerf"],
            left_child=preview["left_child"],
            right_child=preview["right_child"],
        )

    def cut(
        self,
        *,
        cut_id: str,
        piece_id: str,
        expected_revision: int | None,
        u: Any,
        left_child: str,
        right_child: str,
        kerf: Any | None = None,
        expected_orientation: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(cut_id, str) or not cut_id:
            raise InvalidStateError("cut_id must be a non-empty string")
        if expected_revision is None:
            raise InvalidStateError(
                "expected_revision is required to cut an active piece",
                piece_id=piece_id,
            )
        u_f = as_fraction(u, field="u")
        with self._write_tx() as conn:
            existing = conn.execute("SELECT * FROM cuts WHERE cut_id=?", (cut_id,)).fetchone()
            if existing is not None:
                # Omitted kerf means "the kerf this cut actually used"; an
                # explicitly different kerf is different content.
                kerf_f = (
                    as_fraction(kerf, field="kerf")
                    if kerf is not None
                    else fparse(existing["kerf"])
                )
                if existing["kerf"] != fstr(kerf_f):
                    raise ConflictError(
                        "cut_id was already used with a different kerf",
                        cut_id=cut_id,
                    )
                expected_fp = self._cut_fingerprint(
                    piece_id, expected_revision, u_f, kerf_f, left_child, right_child, expected_orientation
                )
                if existing["fingerprint"] != expected_fp:
                    raise ConflictError(
                        "cut_id was already used with different content",
                        cut_id=cut_id,
                    )
                return self._cut_result(conn, existing)

            parent = self._require_active(conn, piece_id, expected_revision, expected_orientation)
            kerf_f = as_fraction(kerf, field="kerf") if kerf is not None else self.default_kerf()
            root_len = self.root_length(parent.root_id)
            geo = cut_geometry(
                parent, u_f, kerf_f, left_child, right_child, root_length=root_len
            )

            for child_id in (left_child, right_child):
                if conn.execute("SELECT 1 FROM pieces WHERE piece_id=?", (child_id,)).fetchone():
                    raise DuplicatePieceIdError(
                        "child piece_id already exists", piece_id=child_id
                    )

            parent_row = conn.execute(
                "SELECT revision, orientation FROM pieces WHERE piece_id=?", (piece_id,)
            ).fetchone()
            parent_revision = parent_row["revision"]
            parent_orientation = parent_row["orientation"]

            fingerprint = self._cut_fingerprint(
                piece_id, expected_revision, u_f, kerf_f, left_child, right_child, expected_orientation
            )

            for child in (geo.left, geo.right):
                conn.execute(
                    "INSERT INTO pieces(piece_id, root_id, root_lo, root_hi, orientation, revision, status, origin_cut_id)"
                    " VALUES (?, ?, ?, ?, ?, 1, 'active', ?)",
                    (
                        child.piece_id,
                        child.root_id,
                        fstr(child.root_lo),
                        fstr(child.root_hi),
                        child.orientation,
                        cut_id,
                    ),
                )
            conn.execute(
                "UPDATE pieces SET status='retired' WHERE piece_id=?",
                (piece_id,),
            )
            conn.execute(
                "INSERT INTO cuts(cut_id, root_id, parent_piece_id, parent_revision, parent_orientation,"
                " u, kerf, left_child_id, right_child_id, kerf_lo, kerf_hi, fingerprint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cut_id,
                    parent.root_id,
                    piece_id,
                    parent_revision,
                    parent_orientation,
                    fstr(u_f),
                    fstr(kerf_f),
                    left_child,
                    right_child,
                    fstr(geo.kerf_interval.lo),
                    fstr(geo.kerf_interval.hi),
                    fingerprint,
                ),
            )
            row = conn.execute("SELECT * FROM cuts WHERE cut_id=?", (cut_id,)).fetchone()
            return self._cut_result(conn, row)

    @staticmethod
    def _cut_fingerprint(
        piece_id: str,
        expected_revision: int | None,
        u: Fraction,
        kerf: Fraction,
        left_child: str,
        right_child: str,
        expected_orientation: int | None,
    ) -> str:
        # "Same content" means the same request text, including the
        # preconditions the caller asserted.  Replaying with a different
        # expected_revision/orientation is a conflict, not a replay.
        return _fingerprint(
            {
                "op": "cut",
                "piece_id": piece_id,
                "expected_revision": expected_revision,
                "expected_orientation": expected_orientation,
                "u": fstr(u),
                "kerf": fstr(kerf),
                "left_child": left_child,
                "right_child": right_child,
            }
        )

    def replay_cut(self, cut_id: str) -> dict[str, Any]:
        """Return a successful cut's stored result verbatim, read-only."""

        row = self._conn.execute("SELECT * FROM cuts WHERE cut_id=?", (cut_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown cut_id", cut_id=cut_id)
        return self._cut_result(self._conn, row)

    def replay_delivery(self, delivery_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("unknown delivery_id", delivery_id=delivery_id)
        return self._delivery_result(self._conn, row)

    def _cut_result(self, conn: sqlite3.Connection, cut_row: sqlite3.Row) -> dict[str, Any]:
        parent_row = conn.execute(
            "SELECT * FROM pieces WHERE piece_id=?", (cut_row["parent_piece_id"],)
        ).fetchone()
        parent_geo = PieceGeometry(
            piece_id=parent_row["piece_id"],
            root_id=parent_row["root_id"],
            root_lo=fparse(parent_row["root_lo"]),
            root_hi=fparse(parent_row["root_hi"]),
            orientation=cut_row["parent_orientation"],
        )
        left_row = conn.execute(
            "SELECT * FROM pieces WHERE piece_id=?", (cut_row["left_child_id"],)
        ).fetchone()
        right_row = conn.execute(
            "SELECT * FROM pieces WHERE piece_id=?", (cut_row["right_child_id"],)
        ).fetchone()
        left_geo = self._piece_row_to_geo(left_row)
        right_geo = self._piece_row_to_geo(right_row)
        return {
            "cut_id": cut_row["cut_id"],
            "root_id": cut_row["root_id"],
            "parent": {
                **parent_geo.to_dict(),
                "revision": cut_row["parent_revision"],
            },
            "local": {
                "u": jscalar(fparse(cut_row["u"])),
                "kerf": jscalar(fparse(cut_row["kerf"])),
                "left_length": jscalar(left_geo.length),
                "right_length": jscalar(right_geo.length),
            },
            "left": {**left_geo.to_dict(), "revision": 1},
            "right": {**right_geo.to_dict(), "revision": 1},
            "kerf_interval": {
                "root_lo": jscalar(fparse(cut_row["kerf_lo"])),
                "root_hi": jscalar(fparse(cut_row["kerf_hi"])),
                "length": jscalar(fparse(cut_row["kerf_hi"]) - fparse(cut_row["kerf_lo"])),
            },
        }

    # ---- flip ---------------------------------------------------------------

    def flip(self, piece_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        if expected_revision is None:
            raise InvalidStateError(
                "expected_revision is required to flip an active piece",
                piece_id=piece_id,
            )
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
            if row is None:
                raise NotFoundError("unknown piece", piece_id=piece_id)
            if row["status"] != "active":
                raise PieceConsumedError(
                    "only active, undelivered pieces may be flipped",
                    piece_id=piece_id,
                    status=row["status"],
                )
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StaleRevisionError(
                    "expected_revision does not match the active piece",
                    piece_id=piece_id,
                    expected_revision=expected_revision,
                    actual_revision=row["revision"],
                )
            old_orientation = row["orientation"]
            new_orientation = -old_orientation
            old_revision = row["revision"]
            new_revision = old_revision + 1
            conn.execute(
                "UPDATE pieces SET orientation=?, revision=? WHERE piece_id=?",
                (new_orientation, new_revision, piece_id),
            )
            conn.execute(
                "INSERT INTO flips(piece_id, old_revision, new_revision, old_orientation, new_orientation)"
                " VALUES (?, ?, ?, ?, ?)",
                (piece_id, old_revision, new_revision, old_orientation, new_orientation),
            )
            geo = self._piece_row_to_geo(
                conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
            )
            return {
                **geo.to_dict(),
                "revision": new_revision,
                "previous_orientation": old_orientation,
                "previous_revision": old_revision,
            }

    # ---- delivery -----------------------------------------------------------

    def deliver(self, *, delivery_id: str, piece_id: str, required_length: Any) -> dict[str, Any]:
        if not isinstance(delivery_id, str) or not delivery_id:
            raise InvalidStateError("delivery_id must be a non-empty string")
        required = as_fraction(required_length, field="required_length")
        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if existing is not None:
                expected_fp = self._delivery_fingerprint(piece_id, required)
                if existing["fingerprint"] != expected_fp:
                    raise ConflictError(
                        "delivery_id was already used with different content",
                        delivery_id=delivery_id,
                    )
                return self._delivery_result(conn, existing)

            row = conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
            if row is None:
                raise NotFoundError("unknown piece", piece_id=piece_id)
            if row["status"] != "active":
                raise PieceConsumedError(
                    "only active pieces can be delivered",
                    piece_id=piece_id,
                    status=row["status"],
                )
            geo = self._piece_row_to_geo(row)
            if geo.length != required:
                raise LengthMismatchError(
                    "actual piece length does not match required_length",
                    piece_id=piece_id,
                    required_length=jscalar(required),
                    actual_length=jscalar(geo.length),
                )
            conn.execute("UPDATE pieces SET status='delivered' WHERE piece_id=?", (piece_id,))
            fingerprint = self._delivery_fingerprint(piece_id, required)
            conn.execute(
                "INSERT INTO deliveries(delivery_id, piece_id, required_length, actual_length, fingerprint)"
                " VALUES (?, ?, ?, ?, ?)",
                (delivery_id, piece_id, fstr(required), fstr(geo.length), fingerprint),
            )
            return self._delivery_result(conn, conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone())

    @staticmethod
    def _delivery_fingerprint(piece_id: str, required: Fraction) -> str:
        return _fingerprint(
            {"op": "deliver", "piece_id": piece_id, "required_length": fstr(required)}
        )

    def _delivery_result(self, conn: sqlite3.Connection, delivery_row: sqlite3.Row) -> dict[str, Any]:
        piece_row = conn.execute(
            "SELECT * FROM pieces WHERE piece_id=?", (delivery_row["piece_id"],)
        ).fetchone()
        geo = self._piece_row_to_geo(piece_row)
        manifest = {
            "schema": DELIVERY_SCHEMA,
            "delivery_id": delivery_row["delivery_id"],
            "piece_id": geo.piece_id,
            "root_id": geo.root_id,
            "root_lo": jscalar(geo.root_lo),
            "root_hi": jscalar(geo.root_hi),
            "orientation": geo.orientation,
            "length": jscalar(geo.length),
        }
        return {
            "delivery_id": delivery_row["delivery_id"],
            "piece_id": geo.piece_id,
            "required_length": jscalar(fparse(delivery_row["required_length"])),
            "actual_length": jscalar(fparse(delivery_row["actual_length"])),
            "manifest": manifest,
        }

    # ---- queries ------------------------------------------------------------

    def lineage(self, piece_id: str) -> dict[str, Any]:
        """Explain a piece through its parent cuts, sibling and kerf."""

        row = self._conn.execute("SELECT * FROM pieces WHERE piece_id=?", (piece_id,)).fetchone()
        if row is None:
            raise NotFoundError("unknown piece", piece_id=piece_id)
        geo = self._piece_row_to_geo(row)

        chain: list[dict[str, Any]] = []
        current = piece_id
        seen: set[str] = set()
        while True:
            if current in seen:
                raise InvariantError("cycle in piece lineage", piece_id=current)
            seen.add(current)
            cut = self._conn.execute(
                "SELECT * FROM cuts WHERE left_child_id=? OR right_child_id=?",
                (current, current),
            ).fetchone()
            if cut is None:
                break
            side = "left" if cut["left_child_id"] == current else "right"
            sibling = cut["right_child_id"] if side == "left" else cut["left_child_id"]
            parent_flips = [
                {
                    "old_revision": f["old_revision"],
                    "new_revision": f["new_revision"],
                    "old_orientation": f["old_orientation"],
                    "new_orientation": f["new_orientation"],
                }
                for f in self._conn.execute(
                    "SELECT * FROM flips WHERE piece_id=? AND new_revision <= ? ORDER BY seq",
                    (cut["parent_piece_id"], cut["parent_revision"]),
                )
            ]
            chain.append(
                {
                    "cut_id": cut["cut_id"],
                    "child": current,
                    "side": side,
                    "parent_piece_id": cut["parent_piece_id"],
                    "parent_revision_at_cut": cut["parent_revision"],
                    "parent_orientation_at_cut": cut["parent_orientation"],
                    "parent_flips": parent_flips,
                    "sibling_piece_id": sibling,
                    "local_u": jscalar(fparse(cut["u"])),
                    "kerf": jscalar(fparse(cut["kerf"])),
                    "kerf_interval": {
                        "root_lo": jscalar(fparse(cut["kerf_lo"])),
                        "root_hi": jscalar(fparse(cut["kerf_hi"])),
                    },
                }
            )
            current = cut["parent_piece_id"]

        flips = [
            {
                "old_revision": r["old_revision"],
                "new_revision": r["new_revision"],
                "old_orientation": r["old_orientation"],
                "new_orientation": r["new_orientation"],
            }
            for r in self._conn.execute(
                "SELECT * FROM flips WHERE piece_id=? ORDER BY seq", (piece_id,)
            )
        ]
        delivery = self._conn.execute(
            "SELECT delivery_id FROM deliveries WHERE piece_id=?", (piece_id,)
        ).fetchone()

        children = [
            r["cut_id"]
            for r in self._conn.execute(
                "SELECT cut_id FROM cuts WHERE parent_piece_id=? ORDER BY seq", (piece_id,)
            )
        ]
        return {
            "piece": {**geo.to_dict(), "revision": row["revision"], "status": row["status"]},
            "cut_ancestry": chain,
            "flips": flips,
            "cuts_as_parent": children,
            "delivered_as": delivery["delivery_id"] if delivery else None,
        }

    def root_report(self, root_id: str) -> dict[str, Any]:
        """Physical accounting: active + delivered + kerfs must partition the root."""

        root_len = self.root_length(root_id)
        active = []
        delivered = []
        segments: list[tuple[Fraction, Fraction, str, str]] = []
        for row in self._conn.execute(
            "SELECT * FROM pieces WHERE root_id=? ORDER BY seq", (root_id,)
        ):
            lo = fparse(row["root_lo"])
            hi = fparse(row["root_hi"])
            entry = {
                "piece_id": row["piece_id"],
                "root_lo": jscalar(lo),
                "root_hi": jscalar(hi),
                "orientation": row["orientation"],
                "revision": row["revision"],
            }
            if row["status"] == "active":
                active.append(entry)
                segments.append((lo, hi, "active", row["piece_id"]))
            elif row["status"] == "delivered":
                delivered.append(entry)
                segments.append((lo, hi, "delivered", row["piece_id"]))

        kerfs = []
        for row in self._conn.execute(
            "SELECT * FROM cuts WHERE root_id=? ORDER BY seq", (root_id,)
        ):
            lo = fparse(row["kerf_lo"])
            hi = fparse(row["kerf_hi"])
            kerfs.append(
                {
                    "cut_id": row["cut_id"],
                    "root_lo": jscalar(lo),
                    "root_hi": jscalar(hi),
                }
            )
            segments.append((lo, hi, "kerf", row["cut_id"]))

        segments.sort(key=lambda s: (s[0], s[1]))

        cursor = Fraction(0)
        for lo, hi, label, ident in segments:
            if lo < 0 or hi > root_len:
                raise InvariantError(
                    "segment outside root",
                    root_id=root_id, kind=label, ident=ident,
                    root_lo=jscalar(lo), root_hi=jscalar(hi),
                )
            if lo < cursor:
                raise InvariantError(
                    "physical overlap between segments",
                    root_id=root_id, kind=label, ident=ident,
                    root_lo=jscalar(lo), root_hi=jscalar(hi),
                )
            if lo > cursor:
                raise InvariantError(
                    "gap in root coverage",
                    root_id=root_id, gap_lo=jscalar(cursor), gap_hi=jscalar(lo),
                )
            cursor = hi
        if cursor != root_len:
            raise InvariantError(
                "root not fully covered",
                root_id=root_id, covered=jscalar(cursor), root_length=jscalar(root_len),
            )

        total_active = sum((hi - lo for lo, hi, kind, _ in segments if kind == "active"), Fraction(0))
        total_delivered = sum((hi - lo for lo, hi, kind, _ in segments if kind == "delivered"), Fraction(0))
        total_kerf = sum((hi - lo for lo, hi, kind, _ in segments if kind == "kerf"), Fraction(0))
        return {
            "root_id": root_id,
            "root_length": jscalar(root_len),
            "active": active,
            "delivered": delivered,
            "kerfs": kerfs,
            "totals": {
                "active": jscalar(total_active),
                "delivered": jscalar(total_delivered),
                "kerf": jscalar(total_kerf),
                "sum": jscalar(total_active + total_delivered + total_kerf),
            },
        }

    def cuts_for_root(self, root_id: str) -> list[dict[str, Any]]:
        return [self._cut_result(self._conn, row) for row in self._conn.execute(
            "SELECT * FROM cuts WHERE root_id=? ORDER BY seq", (root_id,)
        )]

    def deliveries(self) -> list[dict[str, Any]]:
        return [self._delivery_result(self._conn, row) for row in self._conn.execute(
            "SELECT * FROM deliveries ORDER BY seq"
        )]
