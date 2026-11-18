"""Independent observation semantics and batch quality reporting.

Two substantive modules cooperate:

* :mod:`obs_service.semantics` -- exact rational range algebra over
  non-negative reals.  It knows nothing about databases.
* :mod:`obs_service.store` -- versioned measurement/threshold registry,
  immutable reports, and per-sample contribution snapshots.

:mod:`obs_service.api` wires them together, :mod:`obs_service.cli` and
:mod:`obs_service.demo` expose entry points.

Only synthetic samples and the rules fixed by the exercise are handled.
Nothing here is a health judgement, and no probability/correlation/
distribution/unit-conversion assumption is made.
"""

from .api import ObservationService
from .semantics import (
    Range,
    SemanticsError,
    Observation,
    RevConflict,
    parse_observation,
    parse_threshold,
    threshold_status,
    average_of_ranges,
    POS_INF,
)

__all__ = [
    "ObservationService",
    "Range",
    "SemanticsError",
    "Observation",
    "RevConflict",
    "parse_observation",
    "parse_threshold",
    "threshold_status",
    "average_of_ranges",
    "POS_INF",
]
