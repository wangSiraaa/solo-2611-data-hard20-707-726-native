"""Independent delivery confirmation depot (分件交付确认仓).

Owns shipments, receipt originals with shipment/part bindings, part coverage,
formal confirmations and review state.  It never computes delegation authority
itself: it consumes the delegation service through an authority port.
"""

from .service import DeliveryService
from .errors import (
    ConflictError,
    DeliveryError,
    NotFoundError,
    ValidationError,
    StalePreparationError,
)

__all__ = [
    "DeliveryService",
    "DeliveryError",
    "ValidationError",
    "ConflictError",
    "NotFoundError",
    "StalePreparationError",
]
