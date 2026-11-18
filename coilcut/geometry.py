"""Pure one-dimensional cutting geometry.

This module deliberately has no database, no inventory and no mutable state.
It maps a *local* cut coordinate onto the root coil axis and returns exact
rational intervals for both children and the kerf.

Local coordinate rule
---------------------
A piece occupies the half-open root interval ``[root_lo, root_hi)`` with
``length = root_hi - root_lo``.  Its local coordinate ``u`` is measured from
the *current direction's start*:

* orientation ``+1``: local ``0`` is ``root_lo``; ``u`` maps to
  ``root_lo + u``.
* orientation ``-1``: local ``0`` is ``root_hi``; ``u`` maps to
  ``root_hi - u``.

A cut at local ``u`` with positive kerf ``k`` removes the local interval
``[u, u + k)``.  The local-left child is ``[0, u)``, the local-right child is
``[u + k, length)``.  After a flip the *meaning* of local-left changes even
though the physical root interval does not, so all coordinates here are always
reported in both local and root space.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import (
    GeometryError,
    InvalidKerfError,
    OutOfBoundsError,
    ZeroLengthPieceError,
)
from .rational import as_fraction, jscalar


def _orientation(value: Any) -> int:
    if isinstance(value, bool) or value not in (-1, 1):
        raise GeometryError("orientation must be +1 or -1", orientation=value)
    return int(value)


@dataclass(frozen=True)
class Interval:
    """A half-open interval ``[lo, hi)`` on a root axis."""

    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        lo = as_fraction(self.lo, field="interval.lo")
        hi = as_fraction(self.hi, field="interval.hi")
        if hi <= lo:
            raise GeometryError(
                "interval must have positive length",
                lo=jscalar(lo),
                hi=jscalar(hi),
            )
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)

    @property
    def length(self) -> Fraction:
        return self.hi - self.lo

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_lo": jscalar(self.lo),
            "root_hi": jscalar(self.hi),
            "length": jscalar(self.length),
        }


@dataclass(frozen=True)
class PieceGeometry:
    """The immutable physical source of a piece plus its current direction."""

    piece_id: str
    root_id: str
    root_lo: Fraction
    root_hi: Fraction
    orientation: int

    def __post_init__(self) -> None:
        if not isinstance(self.piece_id, str) or not self.piece_id:
            raise GeometryError("piece_id must be a non-empty string")
        if not isinstance(self.root_id, str) or not self.root_id:
            raise GeometryError("root_id must be a non-empty string")
        lo = as_fraction(self.root_lo, field="root_lo")
        hi = as_fraction(self.root_hi, field="root_hi")
        if lo < 0:
            raise GeometryError("root_lo must be >= 0", root_lo=jscalar(lo))
        if hi <= lo:
            raise GeometryError(
                "piece must have positive length",
                root_lo=jscalar(lo),
                root_hi=jscalar(hi),
            )
        object.__setattr__(self, "root_lo", lo)
        object.__setattr__(self, "root_hi", hi)
        object.__setattr__(self, "orientation", _orientation(self.orientation))

    @property
    def length(self) -> Fraction:
        return self.root_hi - self.root_lo

    def root_interval(self) -> Interval:
        return Interval(self.root_lo, self.root_hi)

    def to_dict(self) -> dict[str, Any]:
        return {
            "piece_id": self.piece_id,
            "root_id": self.root_id,
            "root_lo": jscalar(self.root_lo),
            "root_hi": jscalar(self.root_hi),
            "orientation": self.orientation,
            "length": jscalar(self.length),
        }


@dataclass(frozen=True)
class CutGeometry:
    """Exact result of one cut, independent of any ledger state."""

    root_id: str
    parent: PieceGeometry
    u: Fraction
    kerf: Fraction
    left: PieceGeometry
    right: PieceGeometry
    kerf_interval: Interval

    @property
    def left_length(self) -> Fraction:
        return self.left.length

    @property
    def right_length(self) -> Fraction:
        return self.right.length

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "parent": self.parent.to_dict(),
            "local": {
                "u": jscalar(self.u),
                "kerf": jscalar(self.kerf),
                "left_length": jscalar(self.left_length),
                "right_length": jscalar(self.right_length),
            },
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "kerf_interval": self.kerf_interval.to_dict(),
        }


def validate_piece(piece: PieceGeometry, root_length: Any | None = None) -> None:
    """Validate a piece against its root, if the root length is known."""

    if not isinstance(piece, PieceGeometry):
        raise GeometryError("expected a PieceGeometry")
    if root_length is not None:
        total = as_fraction(root_length, field="root_length")
        if total <= 0:
            raise GeometryError("root_length must be positive", root_length=jscalar(total))
        if piece.root_hi > total:
            raise OutOfBoundsError(
                "piece extends beyond its root",
                root_id=piece.root_id,
                root_lo=jscalar(piece.root_lo),
                root_hi=jscalar(piece.root_hi),
                root_length=jscalar(total),
            )


def flip_geometry(piece: PieceGeometry) -> PieceGeometry:
    """Reverse only the local direction; the root source is unchanged."""

    validate_piece(piece)
    return PieceGeometry(
        piece_id=piece.piece_id,
        root_id=piece.root_id,
        root_lo=piece.root_lo,
        root_hi=piece.root_hi,
        orientation=-piece.orientation,
    )


def local_interval_to_root(piece: PieceGeometry, local_lo: Any, local_hi: Any) -> Interval:
    """Map a local half-open interval onto the root axis.

    ``0`` always denotes the start of the *current* direction.
    """

    lo = as_fraction(local_lo, field="local_lo")
    hi = as_fraction(local_hi, field="local_hi")
    length = piece.length
    if lo < 0 or hi > length or hi <= lo:
        raise OutOfBoundsError(
            "local interval is outside the piece",
            local_lo=jscalar(lo),
            local_hi=jscalar(hi),
            piece_length=jscalar(length),
        )
    if piece.orientation == 1:
        return Interval(piece.root_lo + lo, piece.root_lo + hi)
    return Interval(piece.root_hi - hi, piece.root_hi - lo)


def cut_geometry(
    parent: PieceGeometry,
    u: Any,
    kerf: Any,
    left_child_id: str,
    right_child_id: str,
    *,
    root_length: Any | None = None,
) -> CutGeometry:
    """Compute one cut without consulting inventory.

    ``u`` is a local coordinate and ``kerf`` is a positive local width.  The
    kerf occupies local ``[u, u + kerf)``.  Both children must have strictly
    positive length.
    """

    validate_piece(parent, root_length)
    u_f = as_fraction(u, field="u")
    k_f = as_fraction(kerf, field="kerf")
    if k_f <= 0:
        raise InvalidKerfError("kerf must be positive", kerf=jscalar(k_f))
    if not isinstance(left_child_id, str) or not left_child_id:
        raise GeometryError("left_child_id must be a non-empty string")
    if not isinstance(right_child_id, str) or not right_child_id:
        raise GeometryError("right_child_id must be a non-empty string")
    if left_child_id == right_child_id:
        raise GeometryError(
            "left and right children need distinct piece_id values",
            child_id=left_child_id,
        )
    if parent.piece_id in (left_child_id, right_child_id):
        raise GeometryError(
            "a child piece_id must differ from its parent piece_id",
            piece_id=parent.piece_id,
        )

    length = parent.length
    if u_f < 0 or u_f + k_f > length:
        raise OutOfBoundsError(
            "cut and kerf must lie within the piece",
            u=jscalar(u_f),
            kerf=jscalar(k_f),
            piece_length=jscalar(length),
        )
    if u_f == 0:
        raise ZeroLengthPieceError(
            "local-left child would have zero length",
            side="left",
            u=jscalar(u_f),
            kerf=jscalar(k_f),
            piece_length=jscalar(length),
        )
    if u_f + k_f == length:
        raise ZeroLengthPieceError(
            "local-right child would have zero length",
            side="right",
            u=jscalar(u_f),
            kerf=jscalar(k_f),
            piece_length=jscalar(length),
        )

    left_interval = local_interval_to_root(parent, 0, u_f)
    kerf_interval = local_interval_to_root(parent, u_f, u_f + k_f)
    right_interval = local_interval_to_root(parent, u_f + k_f, length)

    left = PieceGeometry(
        piece_id=left_child_id,
        root_id=parent.root_id,
        root_lo=left_interval.lo,
        root_hi=left_interval.hi,
        orientation=parent.orientation,
    )
    right = PieceGeometry(
        piece_id=right_child_id,
        root_id=parent.root_id,
        root_lo=right_interval.lo,
        root_hi=right_interval.hi,
        orientation=parent.orientation,
    )

    # Physical conservation check in root space.
    return CutGeometry(
        root_id=parent.root_id,
        parent=parent,
        u=u_f,
        kerf=k_f,
        left=left,
        right=right,
        kerf_interval=kerf_interval,
    )
