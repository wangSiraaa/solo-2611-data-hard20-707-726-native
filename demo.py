#!/usr/bin/env python3
"""End-to-end demonstration on the provided materials/724 files.

The demo is intentionally explicit about every arithmetic step so the printed
output can be checked by hand.  It also asserts each expected value, so a
regression fails loudly instead of printing a wrong number.

Run:  python3 demo.py [--workdir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import scoring_client
from catalog import Catalog
from common import ContractError, load_json_file
from scoring_repo import ScoringRepository

MATERIALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "materials", "724")


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  OK {label} = {actual!r}")


def summary(score: dict) -> dict:
    keys = ("eligible", "missing_weight", "answered_weight", "weighted_sum",
            "min_total", "max_total", "mean", "ineligible_reason")
    return {key: score[key] for key in keys}


def run(workdir: str) -> None:
    catalog_path = os.path.join(workdir, "catalog.sqlite3")
    repo_path = os.path.join(workdir, "scoring.sqlite3")

    catalog = Catalog.open(catalog_path)
    repo = ScoringRepository.open(repo_path, catalog)

    # ---------------------------------------------------------------- import
    hr("1. Import the legacy catalog: entries are authority, cache is not")
    legacy_catalog = load_json_file(os.path.join(MATERIALS, "legacy_catalog.json"))
    report = catalog.import_legacy_catalog(legacy_catalog)
    show("questions imported", report["imported_questions"])
    show("scales imported", [(s["scale_id"], s["version"]) for s in report["scales"]])
    print("  ignored legacy direction_cache_by_question (NOT authority):")
    for conflict in report["ignored_direction_cache"]["conflicts"]:
        print(f"    {conflict['question_id']} in {conflict['scale_id']}v"
              f"{conflict['version']}: cache reverse={conflict['cache_reverse']} "
              f"but the scale entry says reverse={conflict['authority_reverse']}")
    print("  => the same question has different directions in different scales,")
    print("     so a single per-question cache cannot be correct.")

    hr("2. Import the delivered legacy product verbatim (no re-scoring)")
    history = load_json_file(os.path.join(MATERIALS, "legacy_scores.json"))
    show("history import", repo.import_history_document(history, catalog_version="legacy-pre-fix"))
    legacy_view = scoring_client.read_from_repo(repo, "HIST_SCORE_001")
    print(scoring_client.format_human(legacy_view))
    print("  => preserved exactly: B was reported as 5 under the old shared-direction")
    print("     rule. That is a historical number, not the current answer.")

    # --------------------------------------------------------------- answers
    hr("3. Submit answers (whole-submission validation)")
    for line in open(os.path.join(MATERIALS, "answers.jsonl"), encoding="utf-8"):
        entry = json.loads(line)
        result = repo.submit_answers(entry["respondent_id"], entry["revision"],
                                     entry["answers"])
        show(f"{entry['respondent_id']} rev{entry['revision']}", result["status"])

    # ------------------------------------------------------- independent A/B
    hr("4. Independent catalog scoring: same answer, opposite directions")
    print("  U1 revision 1: Q1=5, Q2=MISSING")
    a1 = repo.score_answers("U1", 1, "A", 1)
    b1 = repo.score_answers("U1", 1, "B", 1)
    print("  A (Q1 forward w=2, Q2 reversed w=1):")
    show("Q1 contribution", "5 * 2 = 10")
    show("Q2 missing -> own range 0..2 * w1 = 0..2", "contribution [0, 2]")
    assert_equal(summary(a1), {"eligible": True, "missing_weight": 1,
                               "answered_weight": 2, "weighted_sum": 10,
                               "min_total": 10, "max_total": 12, "mean": "5/1",
                               "ineligible_reason": None}, "A v1 rev1")
    print("  B (Q1 reversed w=1): Q1 contribution = (min+max-answer) * w")
    show("Q1 contribution", f"(1+5-5) * 1 = 1")
    assert_equal(summary(b1), {"eligible": True, "missing_weight": 0,
                               "answered_weight": 1, "weighted_sum": 1,
                               "min_total": 1, "max_total": 1, "mean": "1/1",
                               "ineligible_reason": None}, "B v1 rev1")
    print("  => A gives 10, B gives 1 for the same Q1=5. They genuinely differ.")
    print("     (The historical file said B=5 because the old cache forced Q1")
    print("      forward in every scale. New scoring does not use the cache.)")
    show("catalog unchanged after scoring",
         [(e.question_id, e.weight, e.reverse) for e in catalog.require_scale("A", 1).entries])
    assert_equal([(e.question_id, e.reverse) for e in catalog.require_scale("A", 1).entries],
                 [("Q1", False), ("Q2", True)], "A v1 entries after scoring")

    # ------------------------------------------------------ missing behaviour
    hr("5. Missing threshold, all-missing and non-zero lower bound")
    print("  U2 revision 1: Q1=MISSING, Q2=MISSING")
    c1 = repo.score_answers("U2", 1, "C", 1)
    print("  C limit K=3, both missing weight 2+1=3 <= 3 -> eligible")
    show("Q1 range 1..5 * w2 -> contribution 2..10", "[2, 10]")
    show("Q2 range 0..2 * w1 -> contribution 0..2", "[0, 2]")
    show("total bounds", "2+0 .. 10+2 = 2 .. 12 (non-zero minimum)")
    show("answered weight 0 -> mean undefined", "undefined")
    assert_equal(summary(c1), {"eligible": True, "missing_weight": 3,
                               "answered_weight": 0, "weighted_sum": 0,
                               "min_total": 2, "max_total": 12, "mean": "undefined",
                               "ineligible_reason": None}, "C v1 all missing")
    a2 = repo.score_answers("U2", 1, "A", 1)
    print("  A limit K=1, missing weight 2+1=3 > 1 -> not eligible")
    assert_equal((a2["eligible"], a2["weighted_sum"], a2["mean"]),
                 (False, None, None), "A v1 all missing ineligible")

    # -------------------------------------------------- no partial on reject
    hr("6. Rejection is whole-submission: nothing partially stored")
    for payload, expected_code in (
            ({"Q1": 1, "QX": 2}, "unknown_question"),
            ({"Q1": True}, "boolean_answer"),
            ({"Q1": 9}, "answer_out_of_range"),
            ({"Q1": 2.0}, "invalid_answer")):
        try:
            repo.submit_answers("UREJECT", 1, payload)
        except ContractError as exc:
            assert_equal(exc.code, expected_code, f"reject {payload}")
    assert_equal(repo.list_answer_revisions("UREJECT"), [], "UREJECT stored revisions")
    try:
        repo.submit_answers("U1", 1, {"Q1": 4, "Q2": "MISSING"})
    except ContractError as exc:
        assert_equal(exc.code, "answer_revision_conflict", "same revision, different text")
    assert_equal(repo.submit_answers("U1", 1, {"Q1": 5, "Q2": "MISSING"})["status"],
                 "duplicate_identical", "same revision, identical replay")

    # -------------------------------------------------------- operations file
    hr("7. Apply the operations journal atomically")
    operations = [json.loads(line) for line in
                  open(os.path.join(MATERIALS, "operations.jsonl"), encoding="utf-8")]
    from cli import _apply_operation
    from common import transaction
    applied = []
    with transaction(catalog.conn):
        with transaction(repo.conn):
            for index, operation in enumerate(operations, start=1):
                applied.append((index, operation["operation"],
                                _apply_operation(catalog, repo, operation)))
    for index, kind, result in applied:
        status = result.get("status") or result.get("report_id")
        show(f"{index}. {kind}", status)
    assert_equal(catalog.require_scale("A", 1).status, "superseded", "A v1 status")
    assert_equal(catalog.require_scale("A", 2).status, "active", "A v2 status")

    # --------------------------------------------- revised answers and scale
    hr("8. Answer revision + scale revision genuinely change the new score")
    print("  U1 revision 2: Q1=4, Q2=MISSING")
    print("  A version 2: Q1 is now REVERSED (w=2), Q2 forward (w=1)")
    a2v2 = repo.score_answers("U1", 2, "A", 2)
    show("Q1 contribution", "(1+5-4) * 2 = 4")
    show("Q2 missing own range 0..2 * w1 = 0..2", "contribution [0, 2]")
    assert_equal(summary(a2v2), {"eligible": True, "missing_weight": 1,
                                 "answered_weight": 2, "weighted_sum": 4,
                                 "min_total": 4, "max_total": 6, "mean": "2/1",
                                 "ineligible_reason": None}, "A v2 rev2")
    b2 = repo.score_answers("U1", 2, "B", 1)
    print("  B version 1 (unchanged): Q1 reversed (1+5-4)*1 = 2, no missing")
    assert_equal(summary(b2), {"eligible": True, "missing_weight": 0,
                               "answered_weight": 1, "weighted_sum": 2,
                               "min_total": 2, "max_total": 2, "mean": "2/1",
                               "ineligible_reason": None}, "B v1 rev2")
    # Version pinning: A v1 still scores the same old answers with old direction.
    a_old = repo.score_answers("U1", 2, "A", 1)
    show("A v1 still scores rev2 Q1 forward", f"4*2 = {a_old['weighted_sum']}")
    assert_equal(a_old["weighted_sum"], 8, "A v1 rev2 unchanged by A v2 publication")

    # ------------------------------------------------------------- publish
    hr("9. Publish scoring-product/2 (one atomic report per selection)")
    published = []
    for report_id, selection in (
            ("SCORE_U1R2_AV2", {"respondent_id": "U1", "answer_revision": 2,
                                "scale_id": "A", "scale_version": 2}),
            ("SCORE_U1R2_BV1", {"respondent_id": "U1", "answer_revision": 2,
                                "scale_id": "B", "scale_version": 1})):
        try:
            document = repo.publish_scores([selection], catalog_version="catalog-after-ops",
                                           report_id=report_id)
        except ContractError as exc:
            if exc.code != "publication_content_conflict":
                raise
            document = repo.get_report(report_id)
        published.append(document)
        show(report_id, f"format={document['format']} contract={document['scoring_contract']}")
    # Re-publishing identical content is a no-op, not an overwrite.
    again = repo.publish_scores([{"respondent_id": "U1", "answer_revision": 2,
                                  "scale_id": "A", "scale_version": 2}],
                                catalog_version="catalog-after-ops",
                                report_id="SCORE_U1R2_AV2")
    assert_equal(again["status"], "already_published", "identical republish")

    # --------------------------------------------------------- client reads
    hr("10. Upgraded client reads history and new product, distinguished")
    print(scoring_client.format_human(scoring_client.read_from_repo(repo, "HIST_SCORE_001")))
    print()
    new_view = scoring_client.read_from_repo(repo, "SCORE_U1R2_AV2")
    print(scoring_client.format_human(new_view))
    assert_equal(new_view["kind"], "current", "new product kind")
    assert_equal(new_view["verified"], True, "new product hash verified")
    assert_equal(legacy_view["kind"], "historical", "legacy product kind")
    assert_equal(legacy_view["rescored"], False, "legacy not rescored")
    # Historical values are untouched even though the catalog moved on.
    legacy_b = next(s for s in legacy_view["scores"] if s["scale_id"] == "B")
    assert_equal(legacy_b["weighted_sum"], 5, "historical B value preserved")
    assert_equal(legacy_b["mean"], "5/1", "historical B mean preserved")

    # ------------------------------------------------- independent lookup
    hr("11. Independent query surfaces: catalog vs repository")
    scale = catalog.require_scale("B", 1)
    show("catalog.require_scale('B',1).entries",
         [(e.question_id, e.weight, e.reverse, e.min_value, e.max_value)
          for e in scale.entries])
    stored = repo.lookup_score("SCORE_U1R2_AV2", "U1", "A", 2, 2)
    show("repository.lookup_score(...) weighted_sum", stored["weighted_sum"])
    show("repository per-entry trace",
         [(e["question_id"], e["answer"], e["reverse"], e["contribution"],
           (e["possible_contribution_min"], e["possible_contribution_max"]))
          for e in stored["entries"]])
    assert_equal(stored["weighted_sum"], 4, "stored weighted_sum")
    assert_equal(stored["entries"][0]["contribution"], 4, "stored Q1 contribution")

    # ------------------------------------------------------- reopen test
    hr("12. Reopen both stores from disk; everything persists")
    repo.close()
    catalog.close()
    catalog = Catalog.open(catalog_path)
    repo = ScoringRepository.open(repo_path, catalog)
    reopened = repo.lookup_score("SCORE_U1R2_AV2", "U1", "A", 2, 2)
    assert_equal(reopened["mean"], "2/1", "reopened mean")
    assert_equal(reopened["entries"][0]["reverse"], True, "reopened direction")
    assert_equal(catalog.require_scale("A", 1).status, "superseded", "reopened A v1 status")
    assert_equal(len(repo.list_reports()), 5, "reopened report count")
    print("  reports:", [r["report_id"] for r in repo.list_reports()])

    # ------------------------------------------------- snapshot & close
    hr("13. Snapshot keeps a historical copy without changing current products")
    snapshot_path = os.path.join(workdir, "snapshot.json")
    info = repo.write_snapshot(snapshot_path)
    show("snapshot", info)
    snap = load_json_file(snapshot_path)
    show("snapshot catalog versions",
         [(s["scale_id"], s["version"], s["status"]) for s in snap["catalog"]["scales"]])
    show("snapshot report ids", [r["report_id"] for r in snap["reports"]])

    repo.close()
    catalog.close()
    print()
    print("All demonstrated assertions passed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default="demo_run")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if os.path.isdir(args.workdir) and not args.keep:
        shutil.rmtree(args.workdir)
    os.makedirs(args.workdir, exist_ok=True)
    for name in ("catalog.sqlite3", "scoring.sqlite3", "snapshot.json"):
        path = os.path.join(args.workdir, name)
        if os.path.exists(path):
            os.unlink(path)
    try:
        run(args.workdir)
    except AssertionError as exc:
        print(f"DEMO ASSERTION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
