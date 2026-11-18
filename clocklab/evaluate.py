"""Session endpoint resolution and evaluation.

This module is deliberately free of any storage concerns: it takes an
immutable catalogue and a session definition and either returns an
``EvaluationResult`` or raises ``EvaluationRejected`` with a machine-readable
reason.  Storage decides what to persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .clock import (
    ClockCatalogue,
    InvalidOccurrence,
    Occurrence,
    UnknownZone,
    Zone,
)


OCCURRENCES = (None, "earlier", "later")

# Rejection codes.  They are part of the public report format, so keep them
# stable.
INVALID_INPUT = "invalid_input"
UNKNOWN_ZONE = "unknown_zone"
NO_LOCAL_CANDIDATE = "no_local_candidate"
AMBIGUOUS_LOCAL = "ambiguous_local"
NON_POSITIVE_DURATION = "non_positive_duration"


class InvalidSession(ValueError):
    """The supplied session definition is not well formed."""


class EvaluationRejected(Exception):
    """The session cannot be settled under this catalogue version."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def to_json(self, evaluation_id: str | None = None) -> dict[str, Any]:
        return {
            "evaluation_id": evaluation_id,
            "status": "rejected",
            "reason_code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


def _require_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSession(f"{what} must be an integer, got {value!r}")
    return value


def _require_occurrence(value: Any, what: str) -> str | None:
    if value not in OCCURRENCES:
        raise InvalidSession(
            f"{what} must be 'earlier', 'later' or absent, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class SessionInput:
    session_id: str
    zone_id: str
    local_start: int
    start_occurrence: str | None
    local_end: int
    end_occurrence: str | None
    rate_per_utc_minute: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SessionInput":
        if not isinstance(raw, Mapping):
            raise InvalidSession("session must be an object")
        session_id = raw.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise InvalidSession("session needs a non-empty session_id")
        zone_id = raw.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidSession(
                f"session {session_id!r} needs a non-empty zone_id"
            )
        try:
            local_start = _require_int(raw["local_start"], "local_start")
            local_end = _require_int(raw["local_end"], "local_end")
            rate = _require_int(
                raw["rate_per_utc_minute"], "rate_per_utc_minute"
            )
        except KeyError as exc:
            raise InvalidSession(
                f"session {session_id!r} is missing {exc.args[0]}"
            ) from None
        if rate < 0:
            raise InvalidSession(
                f"session {session_id!r} rate_per_utc_minute must be "
                f"non-negative, got {rate}"
            )
        start_occurrence = _require_occurrence(
            raw.get("start_occurrence"), "start_occurrence"
        )
        end_occurrence = _require_occurrence(
            raw.get("end_occurrence"), "end_occurrence"
        )
        return cls(
            session_id=session_id,
            zone_id=zone_id,
            local_start=local_start,
            start_occurrence=start_occurrence,
            local_end=local_end,
            end_occurrence=end_occurrence,
            rate_per_utc_minute=rate,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "zone_id": self.zone_id,
            "local_start": self.local_start,
            "start_occurrence": self.start_occurrence,
            "local_end": self.local_end,
            "end_occurrence": self.end_occurrence,
            "rate_per_utc_minute": self.rate_per_utc_minute,
        }


@dataclass
class EndpointResolution:
    local: int
    occurrence: str | None
    candidates: list[Occurrence] = field(default_factory=list)
    chosen: Occurrence | None = None

    @property
    def status(self) -> str:
        if not self.candidates:
            return "no_candidate"
        if len(self.candidates) == 1:
            return "unique"
        if self.chosen is None:
            return "ambiguous"
        return "selected"

    def to_json(self) -> dict[str, Any]:
        return {
            "local": self.local,
            "occurrence": self.occurrence,
            "status": self.status,
            "candidates": [occ.to_json() for occ in self.candidates],
            "chosen": None if self.chosen is None else self.chosen.to_json(),
        }

    def rejection(self, endpoint: str) -> EvaluationRejected | None:
        if self.chosen is not None:
            return None
        if not self.candidates:
            return EvaluationRejected(
                NO_LOCAL_CANDIDATE,
                f"{endpoint} local {self.local} is produced by no segment "
                f"(wall-clock gap); refusing to shift it into range",
                {"endpoint": endpoint, "resolution": self.to_json()},
            )
        return EvaluationRejected(
            AMBIGUOUS_LOCAL,
            f"{endpoint} local {self.local} has {len(self.candidates)} "
            f"candidate UTC instants and no earlier/later selection",
            {"endpoint": endpoint, "resolution": self.to_json()},
        )


def resolve_endpoint(zone: Zone, local: int, occurrence: str | None) -> EndpointResolution:
    """Resolve one local endpoint against one zone table.

    Never guesses: an empty candidate list or an unselected multi-element
    list is reported to the caller as such.
    """

    if occurrence not in OCCURRENCES:
        raise InvalidOccurrence(
            f"occurrence must be 'earlier', 'later' or absent, got {occurrence!r}"
        )
    resolution = EndpointResolution(
        local=local,
        occurrence=occurrence,
        candidates=zone.local_candidates(local),
    )
    if len(resolution.candidates) == 1:
        # One candidate: earlier/later both mean that candidate.
        resolution.chosen = resolution.candidates[0]
    elif len(resolution.candidates) > 1 and occurrence is not None:
        resolution.chosen = (
            resolution.candidates[0]
            if occurrence == "earlier"
            else resolution.candidates[-1]
        )
    return resolution


@dataclass
class EvaluationResult:
    evaluation_id: str
    session: SessionInput
    revision: str
    zone_id: str
    start: EndpointResolution
    end: EndpointResolution
    utc_start: int
    utc_end: int
    rate_per_utc_minute: int
    segments: list[dict[str, int]]
    catalogue_snapshot: dict[str, Any]

    @property
    def duration_minutes(self) -> int:
        return self.utc_end - self.utc_start

    @property
    def cost(self) -> int:
        return self.duration_minutes * self.rate_per_utc_minute

    def to_json(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "status": "ok",
            "session": self.session.to_json(),
            "revision": self.revision,
            "zone_id": self.zone_id,
            "start": self.start.to_json(),
            "end": self.end.to_json(),
            "utc_start": self.utc_start,
            "utc_end": self.utc_end,
            "duration_minutes": self.duration_minutes,
            "rate_per_utc_minute": self.rate_per_utc_minute,
            "cost": self.cost,
            "segments": self.segments,
            "catalogue_snapshot": self.catalogue_snapshot,
        }


def evaluate_session(
    session: SessionInput,
    catalogue: ClockCatalogue,
    evaluation_id: str,
) -> EvaluationResult:
    """Interpret both endpoints under one catalogue version.

    The two endpoints are resolved against the same immutable catalogue, then
    duration and cost are computed from the real UTC difference.  The wall
    clock difference is never used as a duration.
    """

    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise InvalidSession("evaluation_id must be a non-empty string")
    try:
        zone = catalogue.zone(session.zone_id)
    except UnknownZone as exc:
        raise EvaluationRejected(
            UNKNOWN_ZONE,
            str(exc),
            {"session": session.to_json(), "revision": catalogue.revision},
        ) from None

    start = resolve_endpoint(zone, session.local_start, session.start_occurrence)
    rejection = start.rejection("start")
    if rejection is not None:
        rejection.detail.update(
            {
                "session": session.to_json(),
                "revision": catalogue.revision,
                "endpoint": "start",
            }
        )
        raise rejection

    end = resolve_endpoint(zone, session.local_end, session.end_occurrence)
    rejection = end.rejection("end")
    if rejection is not None:
        rejection.detail.update(
            {
                "session": session.to_json(),
                "revision": catalogue.revision,
                "endpoint": "end",
            }
        )
        raise rejection

    assert start.chosen is not None and end.chosen is not None
    utc_start = start.chosen.utc
    utc_end = end.chosen.utc

    if utc_end <= utc_start:
        raise EvaluationRejected(
            NON_POSITIVE_DURATION,
            "actual UTC end must be strictly after actual UTC start: "
            f"{utc_start} -> {utc_end}",
            {
                "session": session.to_json(),
                "revision": catalogue.revision,
                "utc_start": utc_start,
                "utc_end": utc_end,
                "wall_clock_minutes": session.local_end - session.local_start,
                "start": start.to_json(),
                "end": end.to_json(),
            },
        )

    return EvaluationResult(
        evaluation_id=evaluation_id,
        session=session,
        revision=catalogue.revision,
        zone_id=zone.zone_id,
        start=start,
        end=end,
        utc_start=utc_start,
        utc_end=utc_end,
        rate_per_utc_minute=session.rate_per_utc_minute,
        segments=zone.segment_chain(utc_start, utc_end),
        catalogue_snapshot=catalogue.to_json(),
    )


def resolve_local_point(
    catalogue: ClockCatalogue,
    zone_id: str,
    local: int,
    occurrence: str | None = None,
) -> dict[str, Any]:
    """Public, session-free point resolution with every candidate and source."""

    zone = catalogue.zone(zone_id)
    resolution = resolve_endpoint(zone, local, occurrence)
    return {
        "revision": catalogue.revision,
        "zone_id": zone_id,
        "local": local,
        "occurrence": occurrence,
        "status": resolution.status,
        "candidates": [occ.to_json() for occ in resolution.candidates],
        "chosen": None if resolution.chosen is None else resolution.chosen.to_json(),
    }
