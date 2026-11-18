"""Typed errors for the cumulative-usage billing service."""


class BillingError(Exception):
    """Base class for all domain errors."""


class ValidationError(BillingError):
    """A record, operation or contract payload failed validation."""


class ContractError(ValidationError):
    """The marginal-band contract is malformed."""


class UsageError(ValidationError):
    """A usage revision is malformed or conflicts with an earlier one."""


class DocumentError(ValidationError):
    """A period-bill/1 document is malformed or conflicts on import."""


class IncompleteChainError(BillingError):
    """Formal fees were requested from an incomplete cumulative chain."""
