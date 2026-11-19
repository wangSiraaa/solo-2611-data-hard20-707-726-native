"""Conversion catalog: scale contracts and measurement reads.

A *scale contract* is identified by a stable ``scale_id`` plus a positive
integer ``revision``.  It maps an integer code ``x`` to the exact engineering
position ``base + x*p/q`` and declares the closed legal code interval
``[lo, hi]``.  Contracts are immutable: publishing a new revision never mutates
an older one.

A *read* pins a ``(read_id, revision)`` to one fixed contract revision, a point
name and an integer code.  Its interpretation is computed once, at insertion,
with exact rationals and frozen alongside the original code and the contract
provenance.  Publishing a new read revision with a different contract leaves
the old read and its old value fully queryable.

All three write paths are idempotent on their natural key: re-submitting
identical content returns the stored row, while differing content under the
same key raises :class:`ConflictError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional

from .rationals import fmt
from .store import Store


class CatalogError(Exception):
    """Base class for conversion-catalog failures."""


class NotFoundError(CatalogError):
    """A referenced contract or read revision does not exist."""


class ConflictError(CatalogError):
    """The same key was submitted with different content."""


class IllegalCodeError(CatalogError):
    """An integer code lies outside its contract's legal closed interval."""


def _require_int(value, name: str, *, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{name} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise CatalogError(f"{name} must be >= {minimum}, got {value}")
    return value


def _require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ScaleContract:
    scale_id: str
    revision: int
    base: int
    p: int
    q: int
    lo: int
    hi: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.scale_id, self.revision)

    def interpret(self, code: int) -> Fraction:
        """Exact engineering position for ``code`` (``base + code*p/q``)."""
        if isinstance(code, bool) or not isinstance(code, int):
            raise IllegalCodeError(f"code must be an integer, got {type(code).__name__}")
        if not (self.lo <= code <= self.hi):
            raise IllegalCodeError(
                f"code {code} outside legal closed interval [{self.lo}, {self.hi}] "
                f"for {self.scale_id}@{self.revision}"
            )
        return Fraction(self.base) + Fraction(code * self.p, self.q)


@dataclass(frozen=True)
class Read:
    read_id: str
    revision: int
    point: str
    scale_id: str
    scale_revision: int
    code: int
    position: Fraction

    @property
    def key(self) -> tuple[str, int]:
        return (self.read_id, self.revision)

    def as_dict(self) -> dict:
        return {
            "read_id": self.read_id,
            "revision": self.revision,
            "point": self.point,
            "scale_id": self.scale_id,
            "scale_revision": self.scale_revision,
            "code": self.code,
            "position": fmt(self.position),
            "source": {
                "scale_id": self.scale_id,
                "scale_revision": self.scale_revision,
            },
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scale_contracts (
    scale_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    base     INTEGER NOT NULL,
    p        INTEGER NOT NULL CHECK (p > 0),
    q        INTEGER NOT NULL CHECK (q > 0),
    lo       INTEGER NOT NULL,
    hi       INTEGER NOT NULL CHECK (hi >= lo),
    PRIMARY KEY (scale_id, revision)
);

CREATE TABLE IF NOT EXISTS reads (
    read_id        TEXT NOT NULL,
    revision       INTEGER NOT NULL CHECK (revision > 0),
    point          TEXT NOT NULL,
    scale_id       TEXT NOT NULL,
    scale_revision INTEGER NOT NULL,
    code           INTEGER NOT NULL,
    position       TEXT NOT NULL,
    PRIMARY KEY (read_id, revision),
    FOREIGN KEY (scale_id, scale_revision)
        REFERENCES scale_contracts (scale_id, revision)
);
"""


class Catalog:
    """Exact integer-code interpretation backed by SQLite."""

    def __init__(self, store: Store):
        self.store = store
        store.executescript(_SCHEMA)

    # -- contracts ---------------------------------------------------------

    @staticmethod
    def _normalise_contract(
        scale_id, revision, base, p, q, lo, hi
    ) -> ScaleContract:
        scale_id = _require_text(scale_id, "scale_id")
        revision = _require_int(revision, "revision", minimum=1)
        base = _require_int(base, "base")
        p = _require_int(p, "p", minimum=1)
        q = _require_int(q, "q", minimum=1)
        lo = _require_int(lo, "lo")
        hi = _require_int(hi, "hi")
        if lo > hi:
            raise CatalogError(f"contract lo={lo} exceeds hi={hi}")
        return ScaleContract(scale_id, revision, base, p, q, lo, hi)

    def add_contract(self, scale_id, revision, base, p, q, lo, hi) -> dict:
        contract = self._normalise_contract(scale_id, revision, base, p, q, lo, hi)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT base, p, q, lo, hi FROM scale_contracts "
                "WHERE scale_id = ? AND revision = ?",
                contract.key,
            ).fetchone()
            if row is not None:
                existing = (row[0], row[1], row[2], row[3], row[4])
                incoming = (contract.base, contract.p, contract.q, contract.lo, contract.hi)
                if existing != incoming:
                    raise ConflictError(
                        f"contract {contract.scale_id}@{contract.revision} already "
                        f"exists with different content: stored {existing}, got {incoming}"
                    )
                created = False
            else:
                conn.execute(
                    "INSERT INTO scale_contracts "
                    "(scale_id, revision, base, p, q, lo, hi) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*contract.key, contract.base, contract.p, contract.q, contract.lo, contract.hi),
                )
                created = True
        return {
            "scale_id": contract.scale_id,
            "revision": contract.revision,
            "base": contract.base,
            "p": contract.p,
            "q": contract.q,
            "lo": contract.lo,
            "hi": contract.hi,
            "created": created,
        }

    def get_contract(self, scale_id: str, revision: int) -> ScaleContract:
        _require_text(scale_id, "scale_id")
        _require_int(revision, "revision", minimum=1)
        row = self.store.conn.execute(
            "SELECT scale_id, revision, base, p, q, lo, hi FROM scale_contracts "
            "WHERE scale_id = ? AND revision = ?",
            (scale_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"contract {scale_id}@{revision} not found")
        return ScaleContract(*row)

    def list_contracts(self, scale_id: Optional[str] = None) -> list[ScaleContract]:
        if scale_id is None:
            rows = self.store.conn.execute(
                "SELECT scale_id, revision, base, p, q, lo, hi FROM scale_contracts "
                "ORDER BY scale_id, revision"
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT scale_id, revision, base, p, q, lo, hi FROM scale_contracts "
                "WHERE scale_id = ? ORDER BY revision",
                (scale_id,),
            ).fetchall()
        return [ScaleContract(*r) for r in rows]

    def interpret_code(self, scale_id: str, revision: int, code: int) -> dict:
        """Independent code-value lookup: contract revision + integer code."""
        contract = self.get_contract(scale_id, revision)
        position = contract.interpret(code)
        return {
            "scale_id": contract.scale_id,
            "scale_revision": contract.revision,
            "code": code,
            "base": contract.base,
            "p": contract.p,
            "q": contract.q,
            "lo": contract.lo,
            "hi": contract.hi,
            "position": fmt(position),
            "formula": f"{contract.base} + {code}*{contract.p}/{contract.q}",
        }

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _normalise_read(read_id, revision, point, scale_id, scale_revision, code) -> tuple:
        read_id = _require_text(read_id, "read_id")
        revision = _require_int(revision, "revision", minimum=1)
        point = _require_text(point, "point")
        scale_id = _require_text(scale_id, "scale_id")
        scale_revision = _require_int(scale_revision, "scale_revision", minimum=1)
        code = _require_int(code, "code")
        return read_id, revision, point, scale_id, scale_revision, code

    def add_read(
        self, read_id, revision, point, scale_id, scale_revision, code
    ) -> dict:
        read_id, revision, point, scale_id, scale_revision, code = self._normalise_read(
            read_id, revision, point, scale_id, scale_revision, code
        )
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT point, scale_id, scale_revision, code, position FROM reads "
                "WHERE read_id = ? AND revision = ?",
                (read_id, revision),
            ).fetchone()
            if row is not None:
                incoming = (point, scale_id, scale_revision, code)
                existing = (row[0], row[1], row[2], row[3])
                if existing != incoming:
                    raise ConflictError(
                        f"read {read_id}@{revision} already exists with different "
                        f"content: stored {existing}, got {incoming}"
                    )
                read = Read(read_id, revision, point, scale_id, scale_revision, code,
                            Fraction(row[4]))
                return {**read.as_dict(), "created": False}

            contract_row = conn.execute(
                "SELECT base, p, q, lo, hi FROM scale_contracts "
                "WHERE scale_id = ? AND revision = ?",
                (scale_id, scale_revision),
            ).fetchone()
            if contract_row is None:
                raise NotFoundError(
                    f"read {read_id}@{revision} references missing contract "
                    f"{scale_id}@{scale_revision}"
                )
            contract = ScaleContract(scale_id, scale_revision, *contract_row)
            position = contract.interpret(code)  # raises IllegalCodeError
            conn.execute(
                "INSERT INTO reads "
                "(read_id, revision, point, scale_id, scale_revision, code, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (read_id, revision, point, scale_id, scale_revision, code, fmt(position)),
            )
        read = Read(read_id, revision, point, scale_id, scale_revision, code, position)
        return {**read.as_dict(), "created": True}

    def get_read(self, read_id: str, revision: int) -> Read:
        _require_text(read_id, "read_id")
        _require_int(revision, "revision", minimum=1)
        row = self.store.conn.execute(
            "SELECT read_id, revision, point, scale_id, scale_revision, code, position "
            "FROM reads WHERE read_id = ? AND revision = ?",
            (read_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"read {read_id}@{revision} not found")
        return Read(row[0], row[1], row[2], row[3], row[4], row[5], Fraction(row[6]))

    def read_exists(self, read_id: str, revision: int) -> bool:
        row = self.store.conn.execute(
            "SELECT 1 FROM reads WHERE read_id = ? AND revision = ?",
            (read_id, revision),
        ).fetchone()
        return row is not None

    def interpret_read(self, read_id: str, revision: int) -> dict:
        """Look up a stored read's frozen interpretation by its revision."""
        return self.get_read(read_id, revision).as_dict()

    def list_read_revisions(self, read_id: str) -> list[dict]:
        """Full revision history for one read id, oldest first."""
        _require_text(read_id, "read_id")
        rows = self.store.conn.execute(
            "SELECT read_id, revision, point, scale_id, scale_revision, code, position "
            "FROM reads WHERE read_id = ? ORDER BY revision",
            (read_id,),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"read {read_id} not found")
        return [
            Read(r[0], r[1], r[2], r[3], r[4], r[5], Fraction(r[6])).as_dict()
            for r in rows
        ]

    def list_reads(self) -> list[Read]:
        rows = self.store.conn.execute(
            "SELECT read_id, revision, point, scale_id, scale_revision, code, position "
            "FROM reads ORDER BY read_id, revision"
        ).fetchall()
        return [Read(r[0], r[1], r[2], r[3], r[4], r[5], Fraction(r[6])) for r in rows]

    def load_contracts(self, refs: Iterable[tuple]) -> dict[tuple[str, int], ScaleContract]:
        return {key: self.get_contract(*key) for key in refs}
