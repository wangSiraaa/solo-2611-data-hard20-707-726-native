"""Stable error types for the geometry service and the stock ledger.

Every error carries a machine-readable ``code`` plus optional details so the
caller can distinguish "this cut is geometrically impossible" from "this piece
was already consumed" without parsing messages.
"""

from __future__ import annotations

from typing import Any


class CoilCutError(Exception):
    code = "coilcut_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        out.update(self.details)
        return out


class GeometryError(CoilCutError):
    code = "geometry_error"


class InvalidKerfError(GeometryError):
    code = "invalid_kerf"


class OutOfBoundsError(GeometryError):
    code = "out_of_bounds"


class ZeroLengthPieceError(GeometryError):
    code = "zero_length_child"


class NotFoundError(CoilCutError):
    code = "not_found"


class InvalidStateError(CoilCutError):
    code = "invalid_state"


class PieceConsumedError(InvalidStateError):
    """The piece is retired (already cut) or delivered (frozen)."""

    code = "piece_consumed"


class StaleRevisionError(CoilCutError):
    code = "stale_revision"


class StaleOrientationError(CoilCutError):
    code = "stale_orientation"


class DuplicatePieceIdError(CoilCutError):
    code = "duplicate_piece_id"


class ConflictError(CoilCutError):
    """An idempotency key was reused with different content."""

    code = "conflict"


class LengthMismatchError(CoilCutError):
    code = "length_mismatch"


class InvariantError(CoilCutError):
    code = "invariant_violation"


class InvalidOperationError(CoilCutError):
    code = "invalid_operation"


class AlreadyInitializedError(CoilCutError):
    code = "already_initialized"
