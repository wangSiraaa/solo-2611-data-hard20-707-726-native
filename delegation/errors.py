"""Errors raised by the delegation service."""


class DelegationError(Exception):
    """Base class for delegation service errors."""


class ValidationError(DelegationError):
    """Malformed or structurally illegal input (whole batch is rejected)."""


class ConflictError(DelegationError):
    """Same identity/key with different content (异文拒绝)."""


class NotFoundError(DelegationError):
    """Requested object does not exist."""


class IntegrityError(DelegationError):
    """Stored artefact hash or reference check failed on reopen/verify."""
