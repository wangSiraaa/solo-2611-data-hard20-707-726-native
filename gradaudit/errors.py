"""Domain errors raised by the catalog / audit library.

Every error carries a stable machine-readable ``code`` plus a human readable
``message`` and an optional ``location`` (record id, group id, revision ...).
Callers (CLI, verify entry point) turn them into located reasons instead of
letting a generic exception escape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class GradAuditError(Exception):
    """Base class for all located, user-facing errors."""

    code = "error"

    def __init__(self, message: str, *, location: Optional[str] = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.location = location
        self.detail = detail

    def as_reason(self) -> dict:
        reason = {"code": self.code, "message": self.message}
        if self.location is not None:
            reason["location"] = self.location
        if self.detail is not None:
            reason["detail"] = self.detail
        return reason

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.location:
            return f"[{self.code}] {self.location}: {self.message}"
        return f"[{self.code}] {self.message}"


class SchemaError(GradAuditError):
    code = "schema"


class RevisionNotFoundError(GradAuditError):
    code = "revision_not_found"


class RevisionConflictError(GradAuditError):
    """Same revision number imported with different content (spec: must reject)."""

    code = "revision_conflict"


class ShapeMismatchError(GradAuditError):
    """A plan's declared identity does not match the requested inputs."""

    code = "shape_mismatch"


class UnknownReviserError(GradAuditError):
    code = "revision_unknown"


class VerificationError(GradAuditError):
    code = "invalid_plan"


class NotPreparedError(GradAuditError):
    code = "not_prepared"


class ConfirmationError(GradAuditError):
    code = "confirmation"


class ConfirmationStaleError(GradAuditError):
    """Current prepared revisions changed since the audit was confirmed."""

    code = "confirmation_stale"


class BatchAtomicityError(GradAuditError):
    """A batch transition could not be applied atomically (none applied)."""

    code = "batch_atomicity"
