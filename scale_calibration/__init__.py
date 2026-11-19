"""Exact rational scale-value interpretation and cross-section shift calibration.

Two independent layers:

* :mod:`.catalog` -- the *conversion catalog*: immutable signed-integer scale
  contracts with positive-integer ``p``/``q`` and a legal integer code interval,
  plus measurement *reads* that pin a contract identity + revision and a code.
  A code ``x`` is interpreted exactly as ``base + x*p/q`` using
  :class:`fractions.Fraction`; nothing is ever rounded.

* :mod:`.calibration` -- the *section-calibration intake*: a request names fixed
  read versions with PRIMARY/BACKUP roles, reference positions with allowed
  deviation ``epsilon``, a backup budget, and a closed interval for one
  common shift ``s``.  It selects one read per reference point, bounds the
  number of selected BACKUP reads, and returns the complete set of feasible
  shifts -- as a union of disjoint closed rational intervals.

All persistence is SQLite.  Writes are atomic; identical re-submissions return
the originally stored product and are insulated from later catalog revisions.
"""

from .catalog import (
    CatalogError,
    ConflictError,
    ScaleContract,
    Read,
    Catalog,
)
from .calibration import (
    CalibrationError,
    CalibrationInputError,
    InfeasibleError,
    Calibrator,
    Request,
    Candidate,
    analyze,
)
from .store import Store

__all__ = [
    "CatalogError",
    "ConflictError",
    "ScaleContract",
    "Read",
    "Catalog",
    "CalibrationError",
    "CalibrationInputError",
    "InfeasibleError",
    "Calibrator",
    "Request",
    "Candidate",
    "analyze",
    "Store",
]

__version__ = "1.0.0"
