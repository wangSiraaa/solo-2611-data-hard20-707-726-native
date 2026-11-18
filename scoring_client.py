#!/usr/bin/env python3
"""Upgraded consumer that reads both legacy and new scored products.

Design rules enforced here
--------------------------
* Dispatch is by the ``format`` field only.
* A ``published-scores/1`` document is a historical delivery.  It is read
  verbatim: original report id, original values, original
  ``legacy-shared-direction`` contract.  The current catalog is NEVER applied to
  it and nothing is recomputed.
* A ``scoring-product/2`` document must use the ``per-scale-entry`` contract and
  is shown with its full per-entry trace.  Its integrity hash is verified.
* The two contracts are surfaced distinctly so a caller can never mistake a
  historical number for a current score.

The module can be used as a library (``read_document``, ``read_from_repo``) or
as a CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from common import ContractError, content_hash, load_json_file

LEGACY_FORMAT = "published-scores/1"
NEW_FORMAT = "scoring-product/2"
LEGACY_CONTRACT = "legacy-shared-direction"
NEW_CONTRACT = "per-scale-entry"


def read_document(document: Any) -> dict[str, Any]:
    """Classify and read one already-published document without re-scoring."""
    if not isinstance(document, dict):
        raise ContractError("invalid_product", "product must be a JSON object")
    fmt = document.get("format")
    if fmt == LEGACY_FORMAT:
        return _read_legacy(document)
    if fmt == NEW_FORMAT:
        return _read_new(document)
    raise ContractError("unsupported_format", "unknown product format", format=fmt)


def _read_legacy(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("scoring_contract") != LEGACY_CONTRACT:
        # We only know how to interpret the contract that produced the file.
        raise ContractError(
            "unknown_legacy_contract",
            "legacy product uses an unexpected scoring contract",
            contract=document.get("scoring_contract"))
    scores = []
    for score in document.get("scores", []):
        scores.append({
            "respondent_id": score.get("respondent_id"),
            "answer_revision": score.get("answer_revision"),
            "scale_id": score.get("scale_id"),
            "scale_version": score.get("scale_version"),
            "eligible": score.get("eligible"),
            "weighted_sum": score.get("weighted_sum"),
            "answered_weight": score.get("answered_weight"),
            "min_total": score.get("min_total"),
            "max_total": score.get("max_total"),
            "mean": score.get("mean"),
        })
    return {
        "kind": "historical",
        "format": LEGACY_FORMAT,
        "report_id": document.get("report_id"),
        "scoring_contract": LEGACY_CONTRACT,
        "rescored": False,
        "note": ("Historical delivery read verbatim. Values reflect the old "
                 "shared-direction rule and are not current scores. The current "
                 "catalog was not applied."),
        "scores": scores,
    }


def _read_new(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("scoring_contract") != NEW_CONTRACT:
        raise ContractError("unknown_new_contract",
                            "new product uses an unexpected scoring contract",
                            contract=document.get("scoring_contract"))
    if document.get("direction_source") not in (None, "per-scale-entry"):
        raise ContractError("invalid_direction_source",
                            "new product must score from per-scale entries",
                            direction_source=document.get("direction_source"))
    reported_hash = document.get("report_hash")
    if reported_hash is not None:
        recomputed = content_hash({k: v for k, v in document.items()
                                   if k not in ("report_hash", "status")})
        if recomputed != reported_hash:
            raise ContractError("report_hash_mismatch",
                                "new product hash does not match its content",
                                report_id=document.get("report_id"))
    return {
        "kind": "current",
        "format": NEW_FORMAT,
        "report_id": document.get("report_id"),
        "scoring_contract": NEW_CONTRACT,
        "direction_source": "per-scale-entry",
        "catalog_version": document.get("catalog_version"),
        "generated_at": document.get("generated_at"),
        "verified": reported_hash is not None,
        "scores": document.get("scores", []),
    }


def read_from_repo(repo: Any, report_id: str) -> dict[str, Any]:
    document = repo.get_report(report_id)
    if document is None:
        raise ContractError("unknown_report", "no such report", report_id=report_id)
    return read_document(document)


def format_human(result: dict[str, Any]) -> str:
    lines = []
    if result["kind"] == "historical":
        lines.append(f"HISTORICAL {result['report_id']} "
                     f"[{result['format']} / {result['scoring_contract']}]")
        lines.append("  read verbatim; current catalog NOT applied; rescored=False")
        for score in result["scores"]:
            lines.append(
                f"  {score['respondent_id']} rev{score['answer_revision']} "
                f"{score['scale_id']}v{score['scale_version']} "
                f"eligible={score['eligible']} sum={score['weighted_sum']} "
                f"aw={score['answered_weight']} "
                f"total=[{score['min_total']},{score['max_total']}] "
                f"mean={score['mean']}")
        return "\n".join(lines)

    lines.append(f"CURRENT {result['report_id']} "
                 f"[{result['format']} / {result['scoring_contract']}] "
                 f"hash_verified={result['verified']}")
    for score in result["scores"]:
        lines.append(
            f"  {score['respondent_id']} rev{score['answer_revision']} "
            f"{score['scale_id']}v{score['scale_version']} "
            f"eligible={score['eligible']} "
            f"missing_weight={score['missing_weight']}/"
            f"{score['missing_weight_limit']} "
            f"sum={score['weighted_sum']} aw={score['answered_weight']} "
            f"total=[{score['min_total']},{score['max_total']}] "
            f"mean={score['mean']}"
            + (f" reason={score['ineligible_reason']}"
               if not score["eligible"] else ""))
        if result["verified"]:
            for entry in score["entries"]:
                lines.append(
                    f"      {entry['question_id']} "
                    f"min={entry['min']} max={entry['max']} "
                    f"w={entry['weight']} reverse={entry['reverse']} "
                    f"answer={entry['answer']} contrib={entry['contribution']} "
                    f"possible=[{entry['possible_contribution_min']},"
                    f"{entry['possible_contribution_max']}]")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("product", help="path to a published product JSON file")
    parser.add_argument("--catalog", help="optional catalog JSON, shown for context only")
    parser.add_argument("--json", action="store_true", help="emit machine JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_json_file(args.product)
        if args.catalog:
            # Printed only as context.  It is never used to re-score history.
            catalog = load_json_file(args.catalog)
            print("# catalog supplied for context only; not used to re-score",
                  file=sys.stderr)
            if catalog.get("format") == "scale-catalog-legacy/1":
                for scale in catalog.get("scales", []):
                    print("# raw", scale.get("scale_id"), scale.get("version"),
                          [(e.get("question_id"), e.get("weight"), e.get("reverse"))
                           for e in scale.get("entries", [])], file=sys.stderr)
                print("# legacy shared cache (not authoritative)",
                      catalog.get("direction_cache_by_question"), file=sys.stderr)
        result = read_document(document)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
