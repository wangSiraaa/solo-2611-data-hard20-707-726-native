"""Domain errors.

Every rejection is a :class:`QualityError` so callers (and the operations
runner) can distinguish a *modelled* refusal from a programming bug.
"""


class QualityError(Exception):
    """Base class for all expected, modelled failures."""


class ValidationError(QualityError):
    """Input is structurally or semantically malformed."""


class RevisionError(QualityError):
    """A candidate directory revision is not admissible (batch unchanged)."""


class UnknownUnit(ValidationError):
    """A unit id is not present in the directory revision."""


class ConversionError(QualityError):
    """Two units cannot be converted under the current directory."""


class DisconnectedError(ConversionError):
    """The units live in different connected components."""


class KindMismatch(ConversionError):
    """An absolute/difference category rule was violated."""


class MissingMeasurement(QualityError):
    """A measurement_id has no stored evaluation."""


class UnknownSpecification(QualityError):
    """A measurement references a specification that was never stored."""
