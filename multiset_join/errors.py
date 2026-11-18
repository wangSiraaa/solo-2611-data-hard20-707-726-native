"""Errors shared by the source catalog and the derived store."""

from __future__ import annotations


class MultisetError(Exception):
    """Base class for all domain errors."""


class ValidationError(MultisetError):
    """An input tuple, multiplicity, delta or batch definition is invalid."""


class CatalogError(MultisetError):
    """The source catalog cannot serve the request."""


class NegativeCountError(CatalogError):
    """A batch would drive at least one tuple multiplicity below zero."""

    def __init__(self, violations: list[dict]):
        self.violations = violations
        detail = ", ".join(
            f"{v['source']}{tuple(v['tuple'])} current={v['current']} delta={v['delta']}"
            for v in violations
        )
        super().__init__(f"batch rejected atomically; negative final count: {detail}")


class BatchConflictError(CatalogError):
    """A batch_id was already accepted with different canonical content."""


class UnknownBatchError(CatalogError):
    """A batch_id has never been accepted."""


class UnknownSliceError(CatalogError):
    """A slice/version does not exist."""


class ResultConflictError(MultisetError):
    """A result_id exists but the caller demanded a different binding."""


class UnknownResultError(MultisetError):
    """A result_id has never been built."""
