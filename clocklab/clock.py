"""Synthetic wall-clock catalogue.

A zone is a finite, integer-minute UTC domain ``[domain_start, domain_end)``
covered by contiguous, non-overlapping half-open segments.  Each segment
carries an integer ``offset`` and ``local = utc + offset``.

UTC -> local is therefore unique.  local -> UTC is a list of candidates, one
per covering segment: zero (a local value that no segment produces), one
(canonical) or several (overlap).  Nothing here ever guesses an offset or
shifts a missing local value into range; callers decide what an empty or
multi-element candidate list means.

All state in this module is immutable.  A revision produces a *new*
``ClockCatalogue``; it never mutates the old one, so callers holding the old
object keep the old table.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class ClockError(Exception):
    """Base class for catalogue errors."""


class InvalidCatalogue(ClockError):
    """A segment table or revision is not a legal catalogue."""


class UnknownZone(ClockError):
    """No zone with the requested id exists."""


class UnknownRevision(ClockError):
    """No stored catalogue carries the requested revision name."""


class RevisionConflict(ClockError):
    """A revision name already exists with different content."""


class OutOfDomain(ClockError):
    """A UTC value falls outside the declared domain."""


class InvalidOccurrence(ClockError):
    """An occurrence selector is not ``None``, ``'earlier'`` or ``'later'``."""


@dataclass(frozen=True)
class Segment:
    utc_start: int
    utc_end: int
    offset: int

    @property
    def utc_span(self) -> int:
        return self.utc_end - self.utc_start

    @property
    def local_start(self) -> int:
        return self.utc_start + self.offset

    @property
    def local_end(self) -> int:
        return self.utc_end + self.offset

    def to_local(self, utc: int) -> int:
        return utc + self.offset

    def to_utc(self, local: int) -> int:
        return local - self.offset

    def contains_utc(self, utc: int) -> bool:
        return self.utc_start <= utc < self.utc_end

    def contains_local(self, local: int) -> bool:
        # Half-open on the local axis as well, since local is utc + offset.
        return self.local_start <= local < self.local_end

    def to_json(self) -> dict[str, int]:
        return {
            "utc_start": self.utc_start,
            "utc_end": self.utc_end,
            "offset": self.offset,
        }


def _as_int(value: Any, what: str) -> int:
    # bool is an int subclass; a boolean is never a meaningful minute.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCatalogue(f"{what} must be an integer, got {value!r}")
    return value


def validate_segments(
    segments: Iterable[Mapping[str, Any]],
    domain: tuple[int, int],
) -> tuple[Segment, ...]:
    """Validate a whole segment table and return it in UTC order.

    Rules enforced:
      * at least one segment, each with integer bounds and a non-empty span;
      * segment 0 starts at ``domain[0]`` and the last ends at ``domain[1]``;
      * each segment starts exactly where the previous one ended
        (contiguous, no overlap and no hole).
    """

    domain_start, domain_end = domain
    if domain_start >= domain_end:
        raise InvalidCatalogue(
            f"utc_domain must be a non-empty [start, end), got {list(domain)}"
        )

    parsed: list[Segment] = []
    for index, raw in enumerate(segments):
        if not isinstance(raw, Mapping):
            raise InvalidCatalogue(f"segment {index} is not an object")
        try:
            start = _as_int(raw["utc_start"], f"segment {index} utc_start")
            end = _as_int(raw["utc_end"], f"segment {index} utc_end")
            offset = _as_int(raw["offset"], f"segment {index} offset")
        except KeyError as exc:
            raise InvalidCatalogue(
                f"segment {index} is missing field {exc.args[0]}"
            ) from None
        if start >= end:
            raise InvalidCatalogue(
                f"segment {index} is empty or reversed: "
                f"[{start}, {end})"
            )
        parsed.append(Segment(start, end, offset))

    if not parsed:
        raise InvalidCatalogue("a zone needs at least one segment")

    parsed.sort(key=lambda seg: seg.utc_start)
    if parsed[0].utc_start != domain_start:
        raise InvalidCatalogue(
            f"table starts at {parsed[0].utc_start}, domain starts at "
            f"{domain_start}"
        )
    if parsed[-1].utc_end != domain_end:
        raise InvalidCatalogue(
            f"table ends at {parsed[-1].utc_end}, domain ends at {domain_end}"
        )
    for index, (left, right) in enumerate(zip(parsed, parsed[1:])):
        if right.utc_start != left.utc_end:
            kind = "overflow" if right.utc_start < left.utc_end else "gap"
            raise InvalidCatalogue(
                f"segments {index} and {index + 1} are not contiguous: "
                f"{kind} at UTC {left.utc_end} (next starts {right.utc_start})"
            )
    return tuple(parsed)


@dataclass(frozen=True, order=True)
class Occurrence:
    """One local -> UTC interpretation, with the segment that produced it."""

    utc: int
    segment_index: int
    offset: int
    utc_start: int
    utc_end: int

    @classmethod
    def from_segment(cls, index: int, segment: Segment, utc: int) -> "Occurrence":
        return cls(
            utc=utc,
            segment_index=index,
            offset=segment.offset,
            utc_start=segment.utc_start,
            utc_end=segment.utc_end,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "utc": self.utc,
            "segment_index": self.segment_index,
            "offset": self.offset,
            "segment_utc_start": self.utc_start,
            "segment_utc_end": self.utc_end,
            "segment_utc_span": self.utc_end - self.utc_start,
            "segment_source": "utc",
        }


class Zone:
    """An immutable zone table with a lazily built local-candidate index."""

    __slots__ = ("zone_id", "domain", "segments", "_local_index")

    def __init__(
        self,
        zone_id: str,
        domain: tuple[int, int],
        segments: Sequence[Segment],
    ) -> None:
        self.zone_id = zone_id
        self.domain = domain
        self.segments = tuple(segments)
        self._local_index: tuple[tuple[int, Segment], ...] | None = None

    @property
    def utc_start(self) -> int:
        return self.domain[0]

    @property
    def utc_end(self) -> int:
        return self.domain[1]

    def __len__(self) -> int:
        return len(self.segments)

    def segment_index_at(self, utc: int) -> int:
        """Index of the segment whose UTC half-open interval contains ``utc``."""

        if not (self.utc_start <= utc < self.utc_end):
            raise OutOfDomain(
                f"UTC {utc} is outside zone {self.zone_id!r} domain "
                f"[{self.utc_start}, {self.utc_end})"
            )
        lo, hi = 0, len(self.segments)
        while lo < hi:
            mid = (lo + hi) // 2
            seg = self.segments[mid]
            if utc < seg.utc_start:
                hi = mid
            elif utc >= seg.utc_end:
                lo = mid + 1
            else:
                return mid
        raise AssertionError("validated tables are contiguous; unreachable")

    def segment_at(self, utc: int) -> Segment:
        """Segment whose UTC half-open interval contains ``utc``."""

        return self.segments[self.segment_index_at(utc)]

    def segment_chain(self, utc_start: int, utc_end: int) -> list[dict[str, int]]:
        """Break a real UTC interval down into the segments it crosses."""

        if utc_start > utc_end:
            raise ValueError("segment_chain needs utc_start <= utc_end")
        chain: list[dict[str, int]] = []
        utc = utc_start
        while utc < utc_end:
            index = self.segment_index_at(utc)
            seg = self.segments[index]
            stop = min(seg.utc_end, utc_end)
            chain.append(
                {
                    "segment_index": index,
                    "utc_start": utc,
                    "utc_end": stop,
                    "offset": seg.offset,
                    "minutes": stop - utc,
                    "segment_utc_start": seg.utc_start,
                    "segment_utc_end": seg.utc_end,
                }
            )
            utc = stop
        return chain

    def utc_to_local(self, utc: int) -> int:
        return self.segment_at(utc).to_local(utc)

    def local_candidates(self, local: int) -> list[Occurrence]:
        """Enumerate *every* segment that maps ``local`` to a UTC instant."""

        if self._local_index is None:
            # Cost choice: the domain is finite and small (<= 20 segments per
            # zone), so a linear scan that needs no persistence and no
            # invalidation is the simplest correct option; see README for the
            # alternative of a persistent local-candidate index.
            self._local_index = tuple(
                (index, seg)
                for index, seg in sorted(
                    enumerate(self.segments), key=lambda item: item[1].local_start
                )
            )
        found = []
        for index, seg in self._local_index:
            if seg.contains_local(local):
                found.append(Occurrence.from_segment(index, seg, seg.to_utc(local)))
        # Segments can share a local span (overlap), never a UTC span; sort by
        # UTC so a caller's "earlier"/"later" always refers to real time.
        found.sort(key=lambda occ: (occ.utc, occ.segment_index))
        return found

    def to_json(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "utc_domain": list(self.domain),
            "segments": [seg.to_json() for seg in self.segments],
        }


class ClockCatalogue:
    """A named revision containing one or more zones."""

    __slots__ = ("revision", "zones")

    def __init__(self, revision: str, zones: Mapping[str, Zone]) -> None:
        self.revision = revision
        self.zones: Mapping[str, Zone] = MappingProxyType(dict(zones))

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "ClockCatalogue":
        if not isinstance(document, Mapping):
            raise InvalidCatalogue("clock document must be an object")
        revision = document.get("revision")
        if not isinstance(revision, str) or not revision:
            raise InvalidCatalogue("clock document needs a non-empty revision")

        raw_zones = document.get("zones")
        if not isinstance(raw_zones, Sequence) or isinstance(raw_zones, (str, bytes)):
            raise InvalidCatalogue("clock document needs a zones array")
        if len(raw_zones) > 10:
            raise InvalidCatalogue("at most 10 zones are supported")

        zones: dict[str, Zone] = {}
        for raw in raw_zones:
            zone = cls._parse_zone(raw)
            if zone.zone_id in zones:
                raise InvalidCatalogue(f"duplicate zone {zone.zone_id!r}")
            zones[zone.zone_id] = zone
        if not zones:
            raise InvalidCatalogue("clock document needs at least one zone")
        return cls(revision, zones)

    @staticmethod
    def _parse_zone(raw: Mapping[str, Any]) -> Zone:
        if not isinstance(raw, Mapping):
            raise InvalidCatalogue("zone entry is not an object")
        zone_id = raw.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidCatalogue("zone needs a non-empty zone_id")

        domain = raw.get("utc_domain")
        if (
            not isinstance(domain, Sequence)
            or isinstance(domain, (str, bytes))
            or len(domain) != 2
        ):
            raise InvalidCatalogue(
                f"zone {zone_id!r} needs utc_domain = [start, end]"
            )
        domain_start = _as_int(domain[0], f"zone {zone_id!r} domain start")
        domain_end = _as_int(domain[1], f"zone {zone_id!r} domain end")

        raw_segments = raw.get("segments")
        if (
            not isinstance(raw_segments, Sequence)
            or isinstance(raw_segments, (str, bytes))
        ):
            raise InvalidCatalogue(f"zone {zone_id!r} needs a segments array")
        if len(raw_segments) > 20:
            raise InvalidCatalogue(f"zone {zone_id!r} has more than 20 segments")

        segments = validate_segments(raw_segments, (domain_start, domain_end))
        return Zone(zone_id, (domain_start, domain_end), segments)

    def zone(self, zone_id: str) -> Zone:
        try:
            return self.zones[zone_id]
        except KeyError:
            raise UnknownZone(f"no zone {zone_id!r} in revision {self.revision!r}") from None

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "zones": [zone.to_json() for zone in self.zones.values()],
        }


class ClockStore:
    """Mutable holder of catalogue versions, keyed by revision name.

    Applying a revision either swaps in a fully validated new catalogue or
    leaves every existing version and the active pointer untouched
    (whole-batch atomicity).  ``preview_revision`` builds without mutating, so
    a caller that also persists the new catalogue can commit storage first and
    only then install it in memory.
    """

    def __init__(
        self,
        initial: ClockCatalogue | None = None,
        *,
        catalogues: Iterable[ClockCatalogue] = (),
        active: str | None = None,
    ) -> None:
        self._by_revision: dict[str, ClockCatalogue] = {}
        if initial is not None:
            self._by_revision[initial.revision] = initial
        for catalogue in catalogues:
            self._by_revision[catalogue.revision] = catalogue
        if not self._by_revision:
            raise InvalidCatalogue("clock store needs at least one catalogue")
        if active is None:
            active = initial.revision if initial is not None else next(iter(self._by_revision))
        if active not in self._by_revision:
            raise UnknownRevision(f"active revision {active!r} is not loaded")
        self._active = active

    @property
    def active(self) -> ClockCatalogue:
        return self._by_revision[self._active]

    def get(self, revision: str) -> ClockCatalogue:
        try:
            return self._by_revision[revision]
        except KeyError:
            raise UnknownRevision(f"unknown revision {revision!r}") from None

    def revisions(self) -> list[str]:
        return list(self._by_revision)

    def preview_revision(
        self, document: Mapping[str, Any], revision: str | None = None
    ) -> ClockCatalogue:
        """Validate a revision document and build a catalogue without storing it.

        The document may use the full-document form
        ``{"revision", "zones": [...]}`` or the replacement form
        ``{"revision", "zone_id", "replace_segments": [...]}``.  In the
        replacement form the zone keeps its declared ``utc_domain`` unless the
        document also provides ``utc_domain``; the segment table must cover
        exactly that domain.  A different domain is rejected as an invalid
        revision.
        """

        if not isinstance(document, Mapping):
            raise InvalidCatalogue("revision document must be an object")
        name = document.get("revision", revision)
        if not isinstance(name, str) or not name:
            raise InvalidCatalogue("revision document needs a non-empty revision")

        next_zones = dict(self.active.zones)
        if "zones" in document:
            candidate = ClockCatalogue.from_document(document)
            if candidate.revision != name:
                raise InvalidCatalogue("revision name mismatch")
            next_zones = dict(candidate.zones)
        else:
            zone_id = document.get("zone_id")
            if not isinstance(zone_id, str) or not zone_id:
                raise InvalidCatalogue("replacement revision needs a zone_id")
            if zone_id not in next_zones:
                raise UnknownZone(f"no zone {zone_id!r} to replace")
            current = next_zones[zone_id]
            raw_segments = document.get("replace_segments")
            if (
                not isinstance(raw_segments, Sequence)
                or isinstance(raw_segments, (str, bytes))
            ):
                raise InvalidCatalogue("replace_segments must be an array")
            if len(raw_segments) > 20:
                raise InvalidCatalogue("at most 20 segments are supported")
            domain = current.domain
            if "utc_domain" in document:
                raw_domain = document["utc_domain"]
                if (
                    not isinstance(raw_domain, Sequence)
                    or isinstance(raw_domain, (str, bytes))
                    or len(raw_domain) != 2
                ):
                    raise InvalidCatalogue("utc_domain must be [start, end]")
                domain = (
                    _as_int(raw_domain[0], "utc_domain start"),
                    _as_int(raw_domain[1], "utc_domain end"),
                )
            segments = validate_segments(raw_segments, domain)
            next_zones[zone_id] = Zone(zone_id, domain, segments)

        return ClockCatalogue(name, next_zones)

    def install(self, catalogue: ClockCatalogue) -> ClockCatalogue:
        """Store and activate a catalogue that was already validated."""

        if catalogue.revision in self._by_revision:
            existing = self._by_revision[catalogue.revision]
            if existing.to_json() != catalogue.to_json():
                raise RevisionConflict(
                    f"revision {catalogue.revision!r} already exists with "
                    f"different content"
                )
            self._active = catalogue.revision
            return existing
        self._by_revision[catalogue.revision] = catalogue
        self._active = catalogue.revision
        return catalogue

    def apply_revision(
        self, document: Mapping[str, Any], revision: str | None = None
    ) -> ClockCatalogue:
        return self.install(self.preview_revision(document, revision))
