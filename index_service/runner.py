"""Scenario runner for the ``materials/<id>/input.json`` fixture format.

The library itself does not depend on this format; the runner only exists
so a material set can be replayed from a JSON description and so the
documented worked examples can be exercised end to end.

Fixture format
--------------
``documents``
    Initial revisions to ingest (a list of document payloads).
``revision`` / ``revisions``
    Optional whole-document corrections.  These are *pending* corrections:
    a correction only takes effect once applied, exactly like a real
    ``apply_revision`` call.  In the shorthand used by the material sets,
    pending corrections are applied after the first ``publish`` operation,
    which is what lets ``v1`` be published against the old cut and ``v2``
    against the corrected cut.  An explicit
    ``{"op": "apply_revision", ...}`` operation disables the shorthand and
    applies the correction precisely where it appears.
``query``
    Default publication query used by ``publish`` operations that do not
    carry their own ``query``.
``operations``
    Ordered list of ``publish`` / ``read`` / ``query`` / ``apply_revision``
    operations.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .publisher import PublicationConflict, PublicationError, PublicationRejected
from .service import Service


def _coerce_revision_list(material: dict) -> list[dict]:
    revisions: list[dict] = []
    single = material.get("revision")
    if isinstance(single, dict):
        revisions.append(single)
    elif single is not None:
        raise ValueError("'revision' must be a document payload object")
    plural = material.get("revisions")
    if plural is not None:
        if not isinstance(plural, list):
            raise ValueError("'revisions' must be a list of document payloads")
        revisions.extend(plural)
    return revisions


def run_materials(material: dict) -> dict[str, Any]:
    """Replay a material fixture and return a structured report."""

    if not isinstance(material, dict):
        raise ValueError("material must be an object")

    documents = material.get("documents", [])
    if not isinstance(documents, list):
        raise ValueError("'documents' must be a list")

    pending = _coerce_revision_list(material)
    operations = material.get("operations", [])
    if not isinstance(operations, list):
        raise ValueError("'operations' must be a list")
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError(f"operation must be an object: {op!r}")
    default_query = material.get("query")

    has_explicit_apply = any(op.get("op") == "apply_revision" for op in operations)
    auto_apply_pending = not has_explicit_apply

    report: dict[str, Any] = {"operations": [], "publications": {}}

    with Service() as service:
        ingested = service.load_documents(documents)
        report["ingest"] = {"documents": len(documents), "stored": ingested}

        published_any = False
        for op in operations:
            if not isinstance(op, dict):
                raise ValueError(f"operation must be an object: {op!r}")
            kind = op.get("op")
            op_id = op.get("id")

            if kind == "apply_revision":
                payload = op.get("document", op.get("revision"))
                if payload is True or payload is None:
                    if not pending:
                        raise ValueError("no pending revision to apply")
                    payload = pending.pop(0)
                stored = service.apply_revision(payload)
                report["operations"].append(
                    {
                        "op": "apply_revision",
                        "id": op_id,
                        "revision": payload.get("revision"),
                        "document_id": payload.get("id", payload.get("document_id")),
                        "stored": stored,
                    }
                )
                continue

            if kind == "publish":
                query = op.get("query", default_query)
                if query is None:
                    raise ValueError(f"publish operation {op_id} has no query")
                version = op.get("version")
                cut = op.get("cut")
                cut_pairs = (
                    [(str(c[0]), int(c[1])) for c in cut] if cut is not None else None
                )
                try:
                    artifact = service.publish(
                        publication_id=op_id,
                        version=version,
                        query=query,
                        cut=cut_pairs,
                    )
                except PublicationRejected as exc:
                    report["operations"].append(
                        {
                            "op": "publish",
                            "id": op_id,
                            "version": version,
                            "status": "rejected",
                            "reason": str(exc),
                            "minimum_documents": exc.minimum,
                            "selected": exc.selected,
                        }
                    )
                except PublicationConflict as exc:
                    report["operations"].append(
                        {
                            "op": "publish",
                            "id": op_id,
                            "version": version,
                            "status": "conflict",
                            "reason": str(exc),
                        }
                    )
                else:
                    report["operations"].append(
                        {
                            "op": "publish",
                            "id": op_id,
                            "version": version,
                            "status": "published",
                            "selected_ids": [s["document_id"] for s in artifact["selected"]],
                        }
                    )
                    report["publications"][op_id] = copy.deepcopy(artifact)
                published_any = True

                if auto_apply_pending and published_any and pending:
                    # Shorthand: the first publication captures the
                    # pre-correction cut; queued corrections then advance
                    # the catalogue for later publications.
                    for payload in pending:
                        service.apply_revision(payload)
                    report.setdefault("auto_applied_revisions", []).append(
                        {
                            "op": "apply_revision",
                            "auto": True,
                            "documents": [
                                payload.get("id", payload.get("document_id"))
                                for payload in pending
                            ],
                        }
                    )
                    pending.clear()
                continue

            if kind == "read":
                try:
                    artifact = service.read(op_id)
                except PublicationError as exc:
                    report["operations"].append(
                        {
                            "op": "read",
                            "id": op_id,
                            "status": "error",
                            "reason": str(exc),
                        }
                    )
                else:
                    report["operations"].append(
                        {
                            "op": "read",
                            "id": op_id,
                            "status": "ok",
                            "artifact": copy.deepcopy(artifact),
                        }
                    )
                continue

            if kind in ("query", "positions", "phrase"):
                spec = op.get("phrase", op.get("include"))
                if spec is None:
                    raise ValueError(f"query operation {op_id} has no phrase")
                cut = op.get("cut")
                cut_pairs = (
                    [(str(c[0]), int(c[1])) for c in cut] if cut is not None else None
                )
                matches = service.query_positions(spec, cut=cut_pairs)
                report["operations"].append(
                    {
                        "op": kind,
                        "id": op_id,
                        "status": "ok",
                        "matches": matches,
                    }
                )
                continue

            raise ValueError(f"unknown operation type: {kind!r}")

    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("material", help="path to a material input.json")
    parser.add_argument("-o", "--output", help="write report JSON here")
    args = parser.parse_args(argv)

    with open(args.material, "r", encoding="utf-8") as handle:
        material = json.load(handle)
    report = run_materials(material)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
