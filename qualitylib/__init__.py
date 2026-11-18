"""Measurement quality library (exact rational, category-aware).

Public API
----------
* :class:`qualitylib.directory.Directory` -- independent unit directory.
* :func:`qualitylib.directory.parse_directory` / ``revise`` -- validation.
* :class:`qualitylib.service.QualityService` -- catalog + evaluations + reports.
* :mod:`qualitylib.reports` -- ``measurement-report/v1`` writer.

The library deliberately does *not* hard-code any conversion table: every
converted value is derived from the stored directory edges.
"""
from .directory import Directory, parse_directory, revise
from .errors import (
    ConversionError,
    DisconnectedError,
    KindMismatch,
    QualityError,
    RevisionError,
    UnknownUnit,
    ValidationError,
)
from .evaluate import Evaluation, evaluate, evaluate_catalog
from .reports import REPORT_SCHEMA, build_report, write_report
from .service import QualityService

__all__ = [
    "Directory",
    "parse_directory",
    "revise",
    "QualityService",
    "Evaluation",
    "evaluate",
    "evaluate_catalog",
    "QualityError",
    "ValidationError",
    "RevisionError",
    "ConversionError",
    "DisconnectedError",
    "KindMismatch",
    "UnknownUnit",
    "REPORT_SCHEMA",
    "build_report",
    "write_report",
]

__version__ = "1.0.0"
