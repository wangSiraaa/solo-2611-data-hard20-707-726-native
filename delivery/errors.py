"""Errors raised by the delivery depot (separate hierarchy from delegation)."""


class DeliveryError(Exception):
    """Base class for delivery depot errors."""


class ValidationError(DeliveryError):
    """Malformed input; the whole batch is rejected."""


class ConflictError(DeliveryError):
    """Same receipt id with different receipt content (异文拒绝)."""


class NotFoundError(DeliveryError):
    """Requested shipment / part / receipt / confirmation does not exist."""


class StalePreparationError(DeliveryError):
    """Evidence or authority changed after a preparation was made."""


class StateError(DeliveryError):
    """Operation not allowed in the current confirmation/review state."""
