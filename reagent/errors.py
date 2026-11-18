"""Exception hierarchy for the reagent inventory / run service."""

from __future__ import annotations


class ReagentError(Exception):
    """Base class for all domain errors."""

    error_type = "ReagentError"


class ValidationError(ReagentError):
    error_type = "ValidationError"


class NotFoundError(ReagentError):
    error_type = "NotFoundError"


class Conflict(ReagentError):
    """The request conflicts with the current or terminal state."""

    error_type = "Conflict"


class InsufficientStock(ReagentError):
    error_type = "InsufficientStock"


class InventoryInvariantError(ReagentError):
    """A conservation / allocation invariant was violated."""

    error_type = "InventoryInvariantError"
