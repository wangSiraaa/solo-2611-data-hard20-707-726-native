"""Independent scale catalog: question bank, versioned scales and scoring maths.

Authority rules
---------------
* ``questions`` is the stable question bank (stable id + allowed integer range).
* A scale version's ``entries`` (question id, positive integer weight, reverse
  flag) are the ONLY authority for direction.  Direction is per scale entry,
  never per question, so the same question may be forward in one scale and
  reversed in another.
* The legacy ``direction_cache_by_question`` blob is recorded for provenance
  and explicitly ignored for scoring.  It is never imported as authority.

The ``score`` methods are pure: they read the stored catalog and never mutate
it.  Reversing is applied to the answer copy used for one scoring call, so a
question is never "permanently" flipped by scoring.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from common import (ContractError, canonical_json, content_hash, fraction_string,
                    is_bool, is_int, require_bool, require_int, transaction,
                    utc_now, validate_id)

CATALOG_SCHEMA_VERSION = 1
RESERVED_FIELDS = frozenset({"question_id", "weight", "reverse"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Current question bank.
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    min_value INTEGER NOT NULL,
    max_value INTEGER NOT NULL,
    CHECK (min_value <= max_value)
);
-- One row per immutable scale version.
CREATE TABLE IF NOT EXISTS scale_versions (
    scale_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    missing_weight_limit INTEGER NOT NULL,
    version_content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (scale_id, version),
    CHECK (version >= 1),
    CHECK (missing_weight_limit >= 0),
    CHECK (status IN ('active', 'superseded'))
);
-- One row per entry of a scale version.  question min/max are copied at
-- publish time so historical versions remain scorable after bank range edits.
CREATE TABLE IF NOT EXISTS scale_entries (
    scale_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    weight INTEGER NOT NULL,
    reverse INTEGER NOT NULL,
    min_value INTEGER NOT NULL,
    max_value INTEGER NOT NULL,
    PRIMARY KEY (scale_id, version, ordinal),
    UNIQUE (scale_id, version, question_id),
    FOREIGN KEY (scale_id, version) REFERENCES scale_versions(scale_id, version),
    CHECK (ordinal >= 0),
    CHECK (weight > 0),
    CHECK (reverse IN (0, 1)),
    CHECK (min_value <= max_value)
);
-- Provenance-only record of a legacy shared-direction cache.  Never read by
-- scoring code.
CREATE TABLE IF NOT EXISTS ignored_direction_cache (
    question_id TEXT PRIMARY KEY,
    reverse INTEGER NOT NULL,
    source TEXT NOT NULL,
    CHECK (reverse IN (0, 1))
);
-- Evidence that an ignored legacy cache actually disagrees with the current
-- per-scale authority.
CREATE TABLE IF NOT EXISTS cache_conflicts (
    question_id TEXT NOT NULL,
    scale_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    cache_reverse INTEGER NOT NULL,
    authority_reverse INTEGER NOT NULL,
    PRIMARY KEY (question_id, scale_id, version),
    CHECK (cache_reverse IN (0, 1)),
    CHECK (authority_reverse IN (0, 1)),
    CHECK (cache_reverse <> authority_reverse)
);
"""


@dataclass(frozen=True)
class Entry:
    question_id: str
    weight: int
    reverse: bool
    min_value: int
    max_value: int

    @property
    def effective_min(self) -> int:
        """Smallest contribution this entry can make over its legal range."""
        return self.min_value if self.reverse else self.min_value * self.weight

    @property
    def effective_max(self) -> int:
        return (self.min_value + self.max_value) * self.weight if self.reverse \
            else self.max_value * self.weight


@dataclass(frozen=True)
class ScaleVersion:
    scale_id: str
    version: int
    missing_weight_limit: int
    entries: tuple[Entry, ...]
    status: str = "active"
    origin: str = "catalog"
    published_at: str = ""
    version_content_hash: str = ""

    def entry(self, question_id: str) -> Entry | None:
        for entry in self.entries:
            if entry.question_id == question_id:
                return entry
        return None

    @property
    def total_min(self) -> int:
        return sum(entry.effective_min for entry in self.entries)

    @property
    def total_max(self) -> int:
        return sum(entry.effective_max for entry in self.entries)

    @property
    def total_weight(self) -> int:
        return sum(entry.weight for entry in self.entries)


@dataclass(frozen=True)
class EntryContribution:
    question_id: str
    min_value: int
    max_value: int
    weight: int
    reverse: bool
    answer: int | None
    contribution: int | None
    possible_min: int
    possible_max: int

    @property
    def is_missing(self) -> bool:
        return self.answer is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "min": self.min_value,
            "max": self.max_value,
            "weight": self.weight,
            "reverse": self.reverse,
            "answer": "MISSING" if self.answer is None else self.answer,
            "contribution": self.contribution,
            "possible_contribution_min": self.possible_min,
            "possible_contribution_max": self.possible_max,
        }


@dataclass
class ScoreResult:
    scale_id: str
    version: int
    missing_weight_limit: int
    eligible: bool
    ineligible_reason: str | None
    missing_weight: int
    answered_weight: int
    weighted_sum: int
    min_total: int
    max_total: int
    mean_numerator: int
    mean_denominator: int
    contributions: list[EntryContribution] = field(default_factory=list)

    @property
    def mean(self) -> str:
        return fraction_string(self.mean_numerator, self.mean_denominator)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale_id": self.scale_id,
            "scale_version": self.version,
            "eligible": self.eligible,
            "ineligible_reason": self.ineligible_reason,
            "missing_weight_limit": self.missing_weight_limit,
            "missing_weight": self.missing_weight,
            "answered_weight": self.answered_weight,
            "weighted_sum": self.weighted_sum,
            "min_total": self.min_total,
            "max_total": self.max_total,
            "mean": self.mean,
            "entries": [contribution.to_dict() for contribution in self.contributions],
        }


class Catalog:
    """Scale catalog with an independent query surface and scoring maths."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ open
    @classmethod
    def open(cls, path: str) -> "Catalog":
        connection = sqlite3.connect(path, isolation_level=None)
        catalog = cls(connection)
        catalog.conn.executescript(SCHEMA)
        catalog._set_meta("catalog_schema", str(CATALOG_SCHEMA_VERSION))
        return catalog

    def close(self) -> None:
        self.conn.close()

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    # --------------------------------------------------------------- bank
    def upsert_question(self, question_id: Any, min_value: Any, max_value: Any) -> None:
        validate_id(question_id, "question_id")
        require_int(min_value, "min", code="invalid_range")
        require_int(max_value, "max", code="invalid_range")
        if min_value > max_value:
            raise ContractError("invalid_range", "min must be <= max",
                                question_id=question_id, min=min_value, max=max_value)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO questions(question_id, min_value, max_value) VALUES(?,?,?) "
                "ON CONFLICT(question_id) DO UPDATE SET "
                "min_value = excluded.min_value, max_value = excluded.max_value",
                (question_id, min_value, max_value))
            # Editing a bank range would silently change totals of already
            # published versions.  Refuse if a stored version would fall outside
            # the new range; the transaction rolls the update back.
            clash = self.conn.execute(
                "SELECT e.scale_id, e.version FROM scale_entries e "
                "WHERE e.question_id = ? AND (e.min_value < ? OR e.max_value > ?) LIMIT 1",
                (question_id, min_value, max_value)).fetchone()
            if clash is not None:
                raise ContractError(
                    "range_breaks_published_scale",
                    "new range does not contain a published entry's range",
                    question_id=question_id, scale_id=clash["scale_id"],
                    version=clash["version"])

    def get_question(self, question_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT question_id, min_value, max_value FROM questions WHERE question_id = ?",
            (question_id,)).fetchone()
        if row is None:
            return None
        return {"question_id": row["question_id"], "min": row["min_value"],
                "max": row["max_value"]}

    def list_questions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT question_id, min_value, max_value FROM questions ORDER BY question_id")
        return [{"question_id": r["question_id"], "min": r["min_value"],
                 "max": r["max_value"]} for r in rows]

    # ------------------------------------------------------------- legacy
    def record_ignored_direction_cache(self, cache: Any, source: str) -> dict[str, Any]:
        """Store, but never use, a legacy shared-direction cache.

        Returns the conflicts found against the per-scale authority so callers
        can prove the cache is not authoritative.
        """
        if not isinstance(cache, dict):
            raise ContractError("invalid_cache", "direction cache must be an object")
        for question_id, reverse in cache.items():
            validate_id(question_id, "cache question_id")
            require_bool(reverse, f"cache[{question_id}]")
            self.conn.execute(
                "INSERT INTO ignored_direction_cache(question_id, reverse, source) "
                "VALUES(?,?,?) ON CONFLICT(question_id) DO UPDATE SET "
                "reverse = excluded.reverse, source = excluded.source",
                (question_id, 1 if reverse else 0, source))
        conflicts = self.conn.execute(
            "SELECT e.question_id, e.scale_id, e.version, c.reverse AS cache_reverse, "
            "e.reverse AS authority_reverse "
            "FROM scale_entries e JOIN ignored_direction_cache c "
            "ON c.question_id = e.question_id "
            "WHERE c.reverse <> e.reverse ORDER BY e.scale_id, e.version, e.question_id"
        ).fetchall()
        for row in conflicts:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache_conflicts(question_id, scale_id, version, "
                "cache_reverse, authority_reverse) VALUES(?,?,?,?,?)",
                (row["question_id"], row["scale_id"], row["version"],
                 row["cache_reverse"], row["authority_reverse"]))
        return {
            "source": source,
            "stored_questions": sorted(cache.keys()),
            "conflict_count": len(conflicts),
            "conflicts": [
                {"question_id": r["question_id"], "scale_id": r["scale_id"],
                 "version": r["version"], "cache_reverse": bool(r["cache_reverse"]),
                 "authority_reverse": bool(r["authority_reverse"])}
                for r in conflicts
            ],
        }

    # -------------------------------------------------------------- scales
    def normalize_entries(self, entries: Any) -> list[dict[str, Any]]:
        """Validate a requested entry list and resolve current bank ranges."""
        if not isinstance(entries, list) or not entries:
            raise ContractError("invalid_entries", "entries must be a non-empty list")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ContractError("invalid_entry", "each entry must be an object")
            unknown_fields = set(raw) - RESERVED_FIELDS
            if unknown_fields:
                raise ContractError("invalid_entry", "unknown entry fields",
                                    fields=sorted(unknown_fields))
            question_id = validate_id(raw.get("question_id"), "question_id")
            for field_name in RESERVED_FIELDS - {"question_id"}:
                if field_name not in raw:
                    raise ContractError("invalid_entry", "missing entry field",
                                        field=field_name, question_id=question_id)
            weight = require_int(raw["weight"], "weight", minimum=1,
                                 code="invalid_weight")
            reverse = require_bool(raw["reverse"], "reverse")
            if question_id in seen:
                raise ContractError("duplicate_entry",
                                    "a scale version may reference a question only once",
                                    question_id=question_id)
            seen.add(question_id)
            question = self.get_question(question_id)
            if question is None:
                raise ContractError("unknown_question",
                                    "entry references a question outside the bank",
                                    question_id=question_id)
            normalized.append({
                "question_id": question_id,
                "weight": weight,
                "reverse": reverse,
                "min_value": question["min"],
                "max_value": question["max"],
            })
        return normalized

    def publish_scale(self, scale_id: Any, version: Any, entries: Any,
                      missing_weight_limit: Any, origin: str = "catalog",
                      published_at: str | None = None,
                      *, replace: bool = False) -> dict[str, Any]:
        """Publish an immutable scale version inside one transaction.

        A different version number is always allowed (it is a new version).  The
        same version number may only be re-asserted with identical content; any
        content change is rejected so history is never overwritten.
        """
        validate_id(scale_id, "scale_id")
        require_int(version, "version", minimum=1, code="invalid_version")
        require_int(missing_weight_limit, "missing_weight_limit", minimum=0,
                    code="invalid_missing_weight_limit")
        if not isinstance(origin, str) or not origin:
            raise ContractError("invalid_origin", "origin must be a non-empty string")
        normalized = self.normalize_entries(entries)
        body = {
            "scale_id": scale_id,
            "version": version,
            "missing_weight_limit": missing_weight_limit,
            "entries": normalized,
        }
        version_hash = content_hash(body)

        from common import utc_now
        stamp = published_at or utc_now()

        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT version_content_hash, missing_weight_limit FROM scale_versions "
                "WHERE scale_id = ? AND version = ?", (scale_id, version)).fetchone()
            if existing is not None:
                if existing["version_content_hash"] != version_hash:
                    raise ContractError(
                        "scale_version_immutable",
                        "scale version already exists with different content",
                        scale_id=scale_id, version=version)
                return {"status": "already_published", "scale_id": scale_id,
                        "version": version, "version_content_hash": version_hash,
                        "superseded_versions": []}
            if not replace:
                latest = self.conn.execute(
                    "SELECT MAX(version) AS v FROM scale_versions WHERE scale_id = ?",
                    (scale_id,)).fetchone()
                if latest["v"] is not None and version <= latest["v"]:
                    raise ContractError(
                        "stale_version",
                        "new version must be greater than the latest version",
                        scale_id=scale_id, version=version, latest=latest["v"])
            self.conn.execute(
                "INSERT INTO scale_versions(scale_id, version, missing_weight_limit, "
                "version_content_hash, status, origin, published_at) "
                "VALUES(?,?,?,?, 'active', ?, ?)",
                (scale_id, version, missing_weight_limit, version_hash, origin, stamp))
            for ordinal, entry in enumerate(normalized):
                self.conn.execute(
                    "INSERT INTO scale_entries(scale_id, version, ordinal, question_id, "
                    "weight, reverse, min_value, max_value) VALUES(?,?,?,?,?,?,?,?)",
                    (scale_id, version, ordinal, entry["question_id"], entry["weight"],
                     1 if entry["reverse"] else 0, entry["min_value"], entry["max_value"]))
            rows = self.conn.execute(
                "SELECT version FROM scale_versions WHERE scale_id = ? AND version < ? "
                "AND status = 'active'", (scale_id, version)).fetchall()
            self.conn.execute(
                "UPDATE scale_versions SET status = 'superseded' "
                "WHERE scale_id = ? AND version < ? AND status = 'active'",
                (scale_id, version))
        return {"status": "published", "scale_id": scale_id, "version": version,
                "version_content_hash": version_hash,
                "superseded_versions": sorted(r["version"] for r in rows)}

    def import_legacy_catalog(self, legacy: Any) -> dict[str, Any]:
        """Import a legacy ``scale-catalog-legacy/1`` document.

        ``entries`` are taken as authority; ``direction_cache_by_question`` is
        recorded as ignored.  The whole import is one transaction.
        """
        if not isinstance(legacy, dict) or legacy.get("format") != "scale-catalog-legacy/1":
            raise ContractError("unsupported_catalog_format",
                                "expected scale-catalog-legacy/1")
        questions = legacy.get("questions")
        scales = legacy.get("scales")
        if not isinstance(questions, list) or not isinstance(scales, list):
            raise ContractError("invalid_catalog", "questions and scales must be lists")

        # Validate in memory before writing anything.
        prepared_questions: list[dict[str, Any]] = []
        bank: dict[str, tuple[int, int]] = {}
        for raw in questions:
            if not isinstance(raw, dict):
                raise ContractError("invalid_question", "question must be an object")
            question_id = validate_id(raw.get("question_id"), "question_id")
            min_value = require_int(raw.get("min"), "min", code="invalid_range")
            max_value = require_int(raw.get("max"), "max", code="invalid_range")
            if min_value > max_value:
                raise ContractError("invalid_range", "min must be <= max",
                                    question_id=question_id)
            if question_id in bank:
                raise ContractError("duplicate_question", "duplicate question id",
                                    question_id=question_id)
            bank[question_id] = (min_value, max_value)
            prepared_questions.append({"question_id": question_id,
                                       "min_value": min_value, "max_value": max_value})

        prepared_scales: list[dict[str, Any]] = []
        for raw in scales:
            if not isinstance(raw, dict):
                raise ContractError("invalid_scale", "scale must be an object")
            scale_id = validate_id(raw.get("scale_id"), "scale_id")
            version = require_int(raw.get("version"), "version", minimum=1,
                                  code="invalid_version")
            limit = require_int(raw.get("missing_weight_limit"),
                                "missing_weight_limit", minimum=0,
                                code="invalid_missing_weight_limit")
            entries = raw.get("entries")
            if not isinstance(entries, list) or not entries:
                raise ContractError("invalid_entries", "entries must be a non-empty list")
            seen: set[str] = set()
            normalized: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ContractError("invalid_entry", "each entry must be an object")
                unknown_fields = set(entry) - RESERVED_FIELDS
                if unknown_fields:
                    raise ContractError("invalid_entry", "unknown entry fields",
                                        fields=sorted(unknown_fields))
                question_id = validate_id(entry.get("question_id"), "question_id")
                weight = require_int(entry.get("weight"), "weight", minimum=1,
                                     code="invalid_weight")
                reverse = require_bool(entry.get("reverse"), "reverse")
                if question_id in seen:
                    raise ContractError("duplicate_entry",
                                        "a scale version may reference a question only once",
                                        question_id=question_id)
                seen.add(question_id)
                if question_id not in bank:
                    raise ContractError("unknown_question",
                                        "entry references an unknown question",
                                        question_id=question_id)
                min_value, max_value = bank[question_id]
                normalized.append({"question_id": question_id, "weight": weight,
                                   "reverse": reverse, "min_value": min_value,
                                   "max_value": max_value})
            prepared_scales.append({"scale_id": scale_id, "version": version,
                                    "missing_weight_limit": limit,
                                    "entries": normalized})

        cache = legacy.get("direction_cache_by_question", {})
        if not isinstance(cache, dict):
            raise ContractError("invalid_cache", "direction cache must be an object")
        for question_id, reverse in cache.items():
            validate_id(question_id, "cache question_id")
            require_bool(reverse, f"cache[{question_id}]")

        imported: list[dict[str, Any]] = []
        with transaction(self.conn):
            for question in prepared_questions:
                self.upsert_question(question["question_id"], question["min_value"],
                                     question["max_value"])
            for scale in prepared_scales:
                raw_entries = [{"question_id": e["question_id"], "weight": e["weight"],
                                "reverse": e["reverse"]} for e in scale["entries"]]
                result = self.publish_scale(
                    scale["scale_id"], scale["version"], raw_entries,
                    scale["missing_weight_limit"], origin="legacy-catalog",
                    replace=True)
                imported.append({"scale_id": scale["scale_id"],
                                 "version": scale["version"], "status": result["status"]})
            cache_report = self.record_ignored_direction_cache(cache, "legacy-catalog")
        return {"imported_questions": len(prepared_questions), "scales": imported,
                "ignored_direction_cache": cache_report}

    def get_scale(self, scale_id: Any, version: Any) -> ScaleVersion | None:
        validate_id(scale_id, "scale_id")
        require_int(version, "version", minimum=1)
        row = self.conn.execute(
            "SELECT scale_id, version, missing_weight_limit, version_content_hash, "
            "status, origin, published_at FROM scale_versions "
            "WHERE scale_id = ? AND version = ?", (scale_id, version)).fetchone()
        if row is None:
            return None
        rows = self.conn.execute(
            "SELECT question_id, weight, reverse, min_value, max_value FROM scale_entries "
            "WHERE scale_id = ? AND version = ? ORDER BY ordinal",
            (scale_id, version)).fetchall()
        entries = tuple(Entry(r["question_id"], r["weight"], bool(r["reverse"]),
                              r["min_value"], r["max_value"]) for r in rows)
        return ScaleVersion(row["scale_id"], row["version"],
                            row["missing_weight_limit"], entries, row["status"],
                            row["origin"], row["published_at"],
                            row["version_content_hash"])

    def require_scale(self, scale_id: Any, version: Any) -> ScaleVersion:
        scale = self.get_scale(scale_id, version)
        if scale is None:
            raise ContractError("unknown_scale_version",
                                "no such scale version", scale_id=scale_id,
                                version=version)
        return scale

    def list_scale_versions(self, scale_id: str | None = None) -> list[dict[str, Any]]:
        if scale_id is None:
            rows = self.conn.execute(
                "SELECT scale_id, version, missing_weight_limit, status, origin, "
                "version_content_hash FROM scale_versions ORDER BY scale_id, version")
        else:
            validate_id(scale_id, "scale_id")
            rows = self.conn.execute(
                "SELECT scale_id, version, missing_weight_limit, status, origin, "
                "version_content_hash FROM scale_versions WHERE scale_id = ? "
                "ORDER BY version", (scale_id,))
        return [{"scale_id": r["scale_id"], "version": r["version"],
                 "missing_weight_limit": r["missing_weight_limit"],
                 "status": r["status"], "origin": r["origin"],
                 "version_content_hash": r["version_content_hash"]} for r in rows]

    def latest_version(self, scale_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM scale_versions WHERE scale_id = ?",
            (validate_id(scale_id, "scale_id"),)).fetchone()
        return row["v"]

    def exported_document(self) -> dict[str, Any]:
        """Lossless export that keeps per-entry direction (not a shared cache)."""
        scales = []
        for summary in self.list_scale_versions():
            scale = self.require_scale(summary["scale_id"], summary["version"])
            scales.append({
                "scale_id": scale.scale_id,
                "version": scale.version,
                "missing_weight_limit": scale.missing_weight_limit,
                "status": scale.status,
                "origin": scale.origin,
                "published_at": scale.published_at,
                "version_content_hash": scale.version_content_hash,
                "entries": [{"question_id": e.question_id, "weight": e.weight,
                             "reverse": e.reverse, "min": e.min_value,
                             "max": e.max_value} for e in scale.entries],
            })
        cache = [{"question_id": r["question_id"], "reverse": bool(r["reverse"]),
                  "source": r["source"]}
                 for r in self.conn.execute(
                     "SELECT question_id, reverse, source FROM ignored_direction_cache "
                     "ORDER BY question_id")]
        conflicts = [{"question_id": r["question_id"], "scale_id": r["scale_id"],
                      "version": r["version"],
                      "cache_reverse": bool(r["cache_reverse"]),
                      "authority_reverse": bool(r["authority_reverse"])}
                     for r in self.conn.execute(
                         "SELECT question_id, scale_id, version, cache_reverse, "
                         "authority_reverse FROM cache_conflicts "
                         "ORDER BY scale_id, version, question_id")]
        return {
            "format": "scale-catalog/2",
            "direction_contract": "per-scale-entry",
            "questions": self.list_questions(),
            "scales": scales,
            "ignored_direction_cache": cache,
            "cache_conflicts": conflicts,
        }

    # ------------------------------------------------------------- scoring
    def score(self, scale_id: Any, version: Any,
              answers: dict[str, Any]) -> ScoreResult:
        """Score one answer map against one scale version.

        Pure: the catalog and the answer map are not modified.  ``answers`` must
        already be validated by the caller (unknown ids and out-of-range values
        are rejected there); this method still enforces the range defensively.
        """
        scale = self.require_scale(scale_id, version)
        if not isinstance(answers, dict):
            raise ContractError("invalid_answers", "answers must be an object")

        contributions: list[EntryContribution] = []
        missing_weight = 0
        answered_weight = 0
        weighted_sum = 0
        missing_ids: list[str] = []

        for entry in scale.entries:
            raw = answers.get(entry.question_id, "MISSING")
            if raw == "MISSING":
                missing_ids.append(entry.question_id)
                missing_weight += entry.weight
                contributions.append(EntryContribution(
                    entry.question_id, entry.min_value, entry.max_value, entry.weight,
                    entry.reverse, None, None, entry.effective_min, entry.effective_max))
                continue
            if not is_int(raw) or is_bool(raw):
                raise ContractError("invalid_answer",
                                    "answer must be an integer or MISSING",
                                    question_id=entry.question_id, value=repr(raw))
            if not (entry.min_value <= raw <= entry.max_value):
                raise ContractError("answer_out_of_range",
                                    "answer outside the question's allowed range",
                                    question_id=entry.question_id, answer=raw,
                                    min=entry.min_value, max=entry.max_value)
            # Reverse is applied to a local copy only.
            effective = (entry.min_value + entry.max_value - raw) if entry.reverse else raw
            contribution = effective * entry.weight
            answered_weight += entry.weight
            weighted_sum += contribution
            contributions.append(EntryContribution(
                entry.question_id, entry.min_value, entry.max_value, entry.weight,
                entry.reverse, raw, contribution, entry.effective_min,
                entry.effective_max))

        eligible = missing_weight <= scale.missing_weight_limit
        reason = None
        if not eligible:
            reason = (f"missing weight {missing_weight} exceeds limit "
                      f"{scale.missing_weight_limit}")
        min_total = weighted_sum + sum(
            c.possible_min for c in contributions if c.is_missing)
        max_total = weighted_sum + sum(
            c.possible_max for c in contributions if c.is_missing)

        return ScoreResult(
            scale_id=scale.scale_id,
            version=scale.version,
            missing_weight_limit=scale.missing_weight_limit,
            eligible=eligible,
            ineligible_reason=reason,
            missing_weight=missing_weight,
            answered_weight=answered_weight,
            weighted_sum=weighted_sum,
            min_total=min_total,
            max_total=max_total,
            mean_numerator=weighted_sum,
            mean_denominator=answered_weight,
            contributions=contributions,
        )
