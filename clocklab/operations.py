"""Operation runner.

Reads an operation list (JSONL or JSON array) and applies it to a ``Ledger``
in order.  Every step is captured as a record so it can be printed or turned
into a report; a step that fails is recorded and the run continues, which
makes the report faithful instead of all-or-nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .storage import ConflictingSession, Ledger


class OperationError(Exception):
    """The operation list itself is malformed."""


def load_operations(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    operations: list[dict[str, Any]] = []
    stripped = text.strip()
    if not stripped:
        return operations
    if stripped[0] in "[{":
        # Ambiguous: a JSON object could be a single op, a JSON array a list.
        # Accept a single operation object or an array, but prefer JSONL when
        # the object is followed by more data.
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            operations = data
        elif isinstance(data, dict):
            operations = [data]
        else:
            operations = _parse_jsonl(text)
    else:
        operations = _parse_jsonl(text)
    for index, op in enumerate(operations):
        if not isinstance(op, dict) or "op" not in op:
            raise OperationError(f"operation {index} is not an object with 'op'")
    return operations


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    operations = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            operations.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise OperationError(f"line {line_no} is not valid JSON: {exc}") from None
    return operations


class OperationRunner:
    def __init__(self, ledger: Ledger, base_dir: str | Path) -> None:
        self.ledger = ledger
        self.base_dir = Path(base_dir)
        self.records: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- helpers
    def _load_json(self, name: str) -> Any:
        if not isinstance(name, str) or not name:
            raise OperationError("operation needs a 'file' string")
        path = (self.base_dir / name).resolve()
        base = self.base_dir.resolve()
        if base != path and base not in path.parents:
            raise OperationError(f"file {name!r} escapes the materials directory")
        return json.loads(path.read_text(encoding="utf-8"))

    def _record(self, index: int, op: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        self.records.append(
            {"step": index, "op": op.get("op"), "request": dict(op), "result": dict(result)}
        )

    # ------------------------------------------------------------------- run
    def run(self, operations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        start = len(self.records)
        for index, op in enumerate(operations, start=1):
            handler = getattr(self, f"_op_{op.get('op')}", None)
            if handler is None:
                self._record(index, op, {"status": "error", "error_type": "UnknownOperation",
                                         "message": f"unknown op {op.get('op')!r}"})
                continue
            try:
                result = handler(op)
            except Exception as exc:  # recorded, not fatal: one bad op must not
                # destroy the report of the ones before it.
                result = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            self._record(index, op, result)
        # Return only this run's records; self.records keeps the full history.
        return self.records[start:]

    # -------------------------------------------------------------- handlers
    def _op_register_sessions(self, op: Mapping[str, Any]) -> dict[str, Any]:
        document = self._load_json(op["file"])
        raw_sessions = document.get("sessions") if isinstance(document, dict) else None
        if not isinstance(raw_sessions, list):
            raise OperationError("sessions file needs a 'sessions' array")
        registered = []
        conflicts = []
        for raw in raw_sessions:
            try:
                session = self.ledger.register_session(raw)
                registered.append(session.to_json())
            except ConflictingSession as exc:
                conflicts.append({"session_id": raw.get("session_id"), "message": str(exc)})
        return {
            "status": "ok" if not conflicts else "partial",
            "registered": registered,
            "conflicts": conflicts,
            "file_revision": document.get("revision") if document else None,
        }

    def _op_load_clock(self, op: Mapping[str, Any]) -> dict[str, Any]:
        from .clock import ClockCatalogue

        document = self._load_json(op["file"])
        catalogue = ClockCatalogue.from_document(document)
        stored = self.ledger.install_catalogue(catalogue)
        self.ledger.log_event("load_clock", {"revision": stored.revision})
        return {"status": "ok", "revision": stored.revision,
                "zones": [zone.zone_id for zone in stored.zones.values()]}

    def _op_resolve_points(self, op: Mapping[str, Any]) -> dict[str, Any]:
        document = self._load_json(op["file"])
        if not isinstance(document, dict):
            raise OperationError("requests file must be an object")
        revision = op.get("revision")
        zone_id = op.get("zone_id", "Z")
        # A requests file may carry both point checks and session inputs; the
        # sessions become durable, idempotently, so a later evaluate step can
        # refer to them by session_id.
        registered = []
        conflicts = []
        for raw in document.get("sessions") or []:
            try:
                registered.append(self.ledger.register_session(raw).session_id)
            except ConflictingSession as exc:
                conflicts.append(
                    {"session_id": raw.get("session_id"), "message": str(exc)}
                )
        points = document.get("point_checks") or []
        resolved = []
        for point in points:
            if not isinstance(point, dict):
                raise OperationError("point check must be an object")
            resolved.append(
                self.ledger.resolve_point(
                    local=point["local"],
                    zone_id=point.get("zone_id", zone_id),
                    revision=revision,
                    occurrence=point.get("occurrence"),
                )
            )
        return {
            "status": "ok" if not conflicts else "partial",
            "revision": revision or self.ledger.active_revision(),
            "points": resolved,
            "registered_sessions": registered,
            "session_conflicts": conflicts,
        }

    def _op_apply_clock_revision(self, op: Mapping[str, Any]) -> dict[str, Any]:
        document = self._load_json(op["file"])
        result = self.ledger.apply_revision(document)
        result["status"] = "ok"
        return result

    def _op_evaluate(self, op: Mapping[str, Any]) -> dict[str, Any]:
        return self.ledger.evaluate(op["session_id"], op["evaluation_id"])

    def _op_read_evaluation(self, op: Mapping[str, Any]) -> dict[str, Any]:
        return self.ledger.read_evaluation(op["evaluation_id"])

    def _op_list_sessions(self, op: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "sessions": self.ledger.list_sessions()}

    def _op_get_session(self, op: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "ok", **self.ledger.get_session(op["session_id"])}

    def _op_summarize(self, op: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            **self.ledger.summarize(
                utc_from=op["utc_from"],
                utc_to=op["utc_to"],
                zone_id=op.get("zone_id"),
                session_id=op.get("session_id"),
                group_by=op.get("group_by", "session"),
                revision=op.get("revision"),
                latest_per_session=op.get("latest_per_session", True),
            ),
        }
