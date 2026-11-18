#!/usr/bin/env python3
"""Contract tests for the catalog and scoring repository.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scoring_client
from catalog import Catalog
from common import ContractError
from scoring_repo import ScoringRepository

MATERIALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "materials", "724")


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = Catalog.open(os.path.join(self.tmp.name, "catalog.sqlite3"))
        self.repo = ScoringRepository.open(os.path.join(self.tmp.name, "scoring.sqlite3"),
                                           self.catalog)
        self.addCleanup(self._close)
        with open(os.path.join(MATERIALS, "legacy_catalog.json"), encoding="utf-8") as fh:
            self.catalog.import_legacy_catalog(json.load(fh))

    def _close(self) -> None:
        self.repo.close()
        self.catalog.close()

    def assertCode(self, code: str, fn, *args, **kwargs):
        with self.assertRaises(ContractError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception


class LegacyImportTests(BaseCase):
    def test_entries_are_authority_and_cache_is_recorded_only(self) -> None:
        exported = self.catalog.exported_document()
        directions = {(s["scale_id"], e["question_id"]): e["reverse"]
                      for s in exported["scales"] for e in s["entries"]}
        self.assertEqual(directions[("A", "Q1")], False)
        self.assertEqual(directions[("B", "Q1")], True)
        self.assertEqual(directions[("C", "Q2")], False)
        conflicts = {(c["question_id"], c["scale_id"]) for c in exported["cache_conflicts"]}
        self.assertIn(("Q1", "B"), conflicts)
        self.assertIn(("Q2", "C"), conflicts)
        # The ignored cache is stored but flagged as ignored, never as authority.
        self.assertTrue(all(c["source"] == "legacy-catalog"
                            for c in exported["ignored_direction_cache"]))

    def test_same_question_opposite_directions_differ(self) -> None:
        answers = {"Q1": 5, "Q2": "MISSING"}
        a = self.catalog.score("A", 1, answers)
        b = self.catalog.score("B", 1, answers)
        self.assertEqual(a.weighted_sum, 10)
        self.assertEqual(b.weighted_sum, 1)
        self.assertNotEqual(a.weighted_sum, b.weighted_sum)

    def test_scoring_does_not_mutate_catalog(self) -> None:
        before = [(e.question_id, e.weight, e.reverse) for e in
                  self.catalog.require_scale("A", 1).entries]
        self.catalog.score("A", 1, {"Q1": 4, "Q2": 2})
        after = [(e.question_id, e.weight, e.reverse) for e in
                 self.catalog.require_scale("A", 1).entries]
        self.assertEqual(before, after)

    def test_scale_cannot_reference_question_twice(self) -> None:
        self.assertCode("duplicate_entry", self.catalog.publish_scale, "D", 1,
                        [{"question_id": "Q1", "weight": 1, "reverse": False},
                         {"question_id": "Q1", "weight": 2, "reverse": True}], 0)

    def test_entries_must_use_known_questions(self) -> None:
        self.assertCode("unknown_question", self.catalog.publish_scale, "D", 1,
                        [{"question_id": "NOPE", "weight": 1, "reverse": False}], 0)

    def test_weight_must_be_positive_integer_not_bool(self) -> None:
        for weight in (0, -1, True, 1.5, "2"):
            self.assertCode("invalid_weight", self.catalog.publish_scale, "D", 1,
                            [{"question_id": "Q1", "weight": weight,
                              "reverse": False}], 0)

    def test_reverse_must_be_boolean(self) -> None:
        self.assertCode("invalid_boolean", self.catalog.publish_scale, "D", 1,
                        [{"question_id": "Q1", "weight": 1, "reverse": 1}], 0)

    def test_scale_version_is_immutable(self) -> None:
        self.assertCode("scale_version_immutable", self.catalog.publish_scale, "A", 1,
                        [{"question_id": "Q1", "weight": 2, "reverse": True},
                         {"question_id": "Q2", "weight": 1, "reverse": False}], 1)

    def test_identical_version_reassertion_is_noop(self) -> None:
        result = self.catalog.publish_scale(
            "A", 1, [{"question_id": "Q1", "weight": 2, "reverse": False},
                     {"question_id": "Q2", "weight": 1, "reverse": True}], 1)
        self.assertEqual(result["status"], "already_published")

    def test_new_version_supersedes_but_old_still_scores(self) -> None:
        self.catalog.publish_scale(
            "A", 2, [{"question_id": "Q1", "weight": 2, "reverse": True},
                     {"question_id": "Q2", "weight": 1, "reverse": False}], 1)
        self.assertEqual(self.catalog.require_scale("A", 1).status, "superseded")
        self.assertEqual(self.catalog.require_scale("A", 2).status, "active")
        self.assertEqual(self.catalog.score("A", 1, {"Q1": 4}).weighted_sum, 8)
        self.assertEqual(self.catalog.score("A", 2, {"Q1": 4}).weighted_sum, 4)


class ScoreMathTests(BaseCase):
    def test_missing_uses_own_range_and_nonzero_lower_bound(self) -> None:
        score = self.catalog.score("C", 1, {"Q1": "MISSING", "Q2": "MISSING"})
        self.assertTrue(score.eligible)
        self.assertEqual(score.missing_weight, 3)
        self.assertEqual(score.answered_weight, 0)
        self.assertEqual(score.weighted_sum, 0)
        self.assertEqual((score.min_total, score.max_total), (2, 12))
        self.assertEqual(score.mean, "undefined")

    def test_ineligible_above_threshold(self) -> None:
        score = self.catalog.score("A", 1, {"Q1": "MISSING", "Q2": "MISSING"})
        self.assertFalse(score.eligible)
        self.assertIn("exceeds limit", score.ineligible_reason)
        # Bounds are still computed for transparency but no official aggregate.
        self.assertEqual((score.min_total, score.max_total), (2, 12))

    def test_missing_absorbs_legal_range_not_mean(self) -> None:
        # A v1 forward Q1 answered 2 w2 = 4; missing Q2 reversed range 0..2 w1.
        score = self.catalog.score("A", 1, {"Q1": 2, "Q2": "MISSING"})
        self.assertEqual(score.weighted_sum, 4)
        self.assertEqual((score.min_total, score.max_total), (4, 6))
        self.assertEqual(score.mean, "2/1")

    def test_exact_rational_mean_reduced(self) -> None:
        # C: Q1 answered 2 w2 = 4, Q2 answered 1 w1 = 1 -> 5/3.
        score = self.catalog.score("C", 1, {"Q1": 2, "Q2": 1})
        self.assertEqual(score.mean, "5/3")

    def test_reverse_uses_entry_min_and_max(self) -> None:
        # B: Q1 min1 max5 reverse, answer 3 -> 1+5-3 = 3, w1.
        score = self.catalog.score("B", 1, {"Q1": 3})
        self.assertEqual(score.weighted_sum, 3)
        # A: Q2 min0 max2 reverse, answer 0 -> 2, w1.
        score = self.catalog.score("A", 1, {"Q1": 1, "Q2": 0})
        self.assertEqual(score.contributions[1].contribution, 2)

    def test_out_of_range_answer_rejected_in_scoring(self) -> None:
        self.assertCode("answer_out_of_range", self.catalog.score, "B", 1, {"Q1": 6})


class RepositoryTests(BaseCase):
    def test_whole_submission_rejected_without_partial_state(self) -> None:
        for payload, code in (
                ({"Q1": 1, "QX": 2}, "unknown_question"),
                ({"Q1": True}, "boolean_answer"),
                ({"Q1": 9}, "answer_out_of_range"),
                ({"Q1": 2.0}, "invalid_answer"),
                ({"Q1": "missing"}, "invalid_answer")):
            self.assertCode(code, self.repo.submit_answers, "U9", 1, payload)
        self.assertEqual(self.repo.list_answer_revisions("U9"), [])

    def test_identical_replay_and_conflicting_replay(self) -> None:
        self.assertEqual(self.repo.submit_answers("U1", 1, {"Q1": 5})["status"], "stored")
        self.assertEqual(self.repo.submit_answers("U1", 1, {"Q1": 5})["status"],
                         "duplicate_identical")
        self.assertCode("answer_revision_conflict", self.repo.submit_answers,
                        "U1", 1, {"Q1": 4})

    def test_revision_rollback_rejected(self) -> None:
        self.repo.submit_answers("U1", 2, {"Q1": 5})
        self.assertCode("answer_revision_rollback", self.repo.submit_answers,
                        "U1", 1, {"Q1": 3})

    def test_publish_is_atomic_across_selections(self) -> None:
        self.repo.submit_answers("U1", 1, {"Q1": 5, "Q2": "MISSING"})
        before = len(self.repo.list_reports())
        self.assertCode("unknown_scale_version", self.repo.publish_scores, [
            {"respondent_id": "U1", "answer_revision": 1, "scale_id": "A",
             "scale_version": 1},
            {"respondent_id": "U1", "answer_revision": 1, "scale_id": "ZZ",
             "scale_version": 1}])
        self.assertEqual(len(self.repo.list_reports()), before)

    def test_ineligible_publish_has_no_official_numbers(self) -> None:
        self.repo.submit_answers("U2", 1, {"Q1": "MISSING", "Q2": "MISSING"})
        doc = self.repo.publish_scores([{"respondent_id": "U2", "answer_revision": 1,
                                         "scale_id": "A", "scale_version": 1}])
        score = doc["scores"][0]
        self.assertFalse(score["eligible"])
        self.assertIsNone(score["weighted_sum"])
        self.assertIsNone(score["mean"])
        self.assertIsNone(score["min_total"])
        self.assertTrue(score["ineligible_reason"])

    def test_published_product_traces_every_entry(self) -> None:
        self.repo.submit_answers("U1", 1, {"Q1": 5, "Q2": "MISSING"})
        doc = self.repo.publish_scores([{"respondent_id": "U1", "answer_revision": 1,
                                         "scale_id": "A", "scale_version": 1}])
        score = doc["scores"][0]
        self.assertEqual(score["missing_weight"], 1)
        self.assertEqual(score["entries"][0]["contribution"], 10)
        self.assertEqual(score["entries"][1]["answer"], "MISSING")
        self.assertIsNone(score["entries"][1]["contribution"])
        self.assertEqual(score["entries"][1]["possible_contribution_min"], 0)
        self.assertEqual(score["entries"][1]["possible_contribution_max"], 2)

    def test_different_publish_for_same_locked_id_rejected(self) -> None:
        self.repo.submit_answers("U1", 1, {"Q1": 5})
        first = self.repo.publish_scores([{"respondent_id": "U1", "answer_revision": 1,
                                           "scale_id": "A", "scale_version": 1}],
                                         report_id="R1")
        again = self.repo.publish_scores([{"respondent_id": "U1", "answer_revision": 1,
                                           "scale_id": "A", "scale_version": 1}],
                                         report_id="R1")
        self.assertEqual(again["report_id"], first["report_id"])
        self.repo.submit_answers("U1", 2, {"Q1": 4})
        self.assertCode("publication_content_conflict", self.repo.publish_scores,
                        [{"respondent_id": "U1", "answer_revision": 2,
                          "scale_id": "A", "scale_version": 1}], report_id="R1")


class HistoryTests(BaseCase):
    def test_history_preserved_and_not_recomputed(self) -> None:
        with open(os.path.join(MATERIALS, "legacy_scores.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.repo.import_history_document(payload)
        view = scoring_client.read_from_repo(self.repo, "HIST_SCORE_001")
        self.assertEqual(view["kind"], "historical")
        self.assertEqual(view["report_id"], "HIST_SCORE_001")
        self.assertFalse(view["rescored"])
        b = next(s for s in view["scores"] if s["scale_id"] == "B")
        self.assertEqual(b["weighted_sum"], 5)
        self.assertEqual(b["mean"], "5/1")

    def test_new_product_cannot_overwrite_history_id(self) -> None:
        with open(os.path.join(MATERIALS, "legacy_scores.json"), encoding="utf-8") as fh:
            self.repo.import_history_document(json.load(fh))
        self.repo.submit_answers("U1", 1, {"Q1": 5})
        self.assertCode("report_id_conflict", self.repo.publish_scores,
                        [{"respondent_id": "U1", "answer_revision": 1,
                          "scale_id": "A", "scale_version": 1}],
                        report_id="HIST_SCORE_001")

    def test_history_import_rejects_new_format(self) -> None:
        self.assertCode("unsupported_history_format",
                        self.repo.import_history_document,
                        {"format": "scoring-product/2", "report_id": "X"})

    def test_client_distinguishes_contracts(self) -> None:
        with open(os.path.join(MATERIALS, "legacy_scores.json"), encoding="utf-8") as fh:
            legacy = scoring_client.read_document(json.load(fh))
        self.repo.submit_answers("U1", 1, {"Q1": 5})
        new_doc = self.repo.publish_scores([{"respondent_id": "U1", "answer_revision": 1,
                                             "scale_id": "A", "scale_version": 1}])
        new = scoring_client.read_document(new_doc)
        self.assertEqual(legacy["kind"], "historical")
        self.assertEqual(new["kind"], "current")
        self.assertNotEqual(legacy["scoring_contract"], new["scoring_contract"])

    def test_tampered_new_product_fails_hash(self) -> None:
        self.repo.submit_answers("U1", 1, {"Q1": 5})
        doc = self.repo.publish_scores([{"respondent_id": "U1", "answer_revision": 1,
                                         "scale_id": "A", "scale_version": 1}])
        doc["scores"][0]["weighted_sum"] = 999
        self.assertCode("report_hash_mismatch", scoring_client.read_document, doc)


class ScaleAndConcurrencyTests(BaseCase):
    def test_large_catalog_and_many_respondents(self) -> None:
        """50 questions, 10 scales, 500 answer submissions and publications."""
        # Build 48 extra questions on top of Q1, Q2 -> 50 total.
        questions = []
        for index in range(3, 51):
            questions.append({"question_id": f"Q{index}", "min": 1 + index % 3,
                              "max": 5 + index % 4})
        for question in questions:
            self.catalog.upsert_question(question["question_id"], question["min"],
                                         question["max"])

        # 7 more scales (D..J) on top of A, B, C -> 10 total.
        all_question_ids = [f"Q{i}" for i in range(1, 51)]
        for scale_offset in range(7):
            scale_id = chr(ord("D") + scale_offset)
            entries = []
            for position in range(10):
                question_id = all_question_ids[(position * 5 + scale_offset) % 50]
                entries.append({"question_id": question_id,
                                "weight": 1 + (position + scale_offset) % 3,
                                "reverse": bool((position + scale_offset) % 2)})
            self.catalog.publish_scale(scale_id, 1, entries, 2)

        self.assertEqual(len(all_question_ids), 50)
        self.assertEqual(len(self.catalog.list_scale_versions()), 10)

        # 500 respondents, each with 50 answers -> 500 stored revisions.
        scale_ids = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        for respondent_index in range(500):
            answers = {}
            for question_index, question_id in enumerate(all_question_ids):
                # A mix of answers and missing values keeps totals non-trivial.
                if (respondent_index + question_index) % 17 == 0:
                    answers[question_id] = "MISSING"
                else:
                    question = self.catalog.get_question(question_id)
                    answers[question_id] = question["min"] + \
                        (respondent_index + question_index) % (question["max"] -
                                                               question["min"] + 1)
            self.repo.submit_answers(f"R{respondent_index}", 1, answers)

        self.assertEqual(len(self.repo.list_answer_revisions()), 500)

        selections = []
        for respondent_index in range(500):
            for scale_id in scale_ids:
                version = self.catalog.latest_version(scale_id)
                selections.append({"respondent_id": f"R{respondent_index}",
                                   "answer_revision": 1, "scale_id": scale_id,
                                   "scale_version": version})
        document = self.repo.publish_scores(selections)
        self.assertEqual(len(document["scores"]), 500 * 10)
        stored = self.repo.lookup_score(document["report_id"], "R42", "J", 1, 1)
        self.assertIsNotNone(stored)
        # Recompute that one score directly and compare.
        direct = self.repo.score_answers("R42", 1, "J", 1)
        self.assertEqual(stored["weighted_sum"], direct["weighted_sum"])
        self.assertEqual(stored["min_total"], direct["min_total"])

    def test_scale_revision_changes_score_new_scale_same_answers(self) -> None:
        self.repo.submit_answers("U1", 1, {"Q1": 4, "Q2": "MISSING"})
        v1 = self.repo.score_answers("U1", 1, "A", 1)
        self.catalog.publish_scale(
            "A", 2, [{"question_id": "Q1", "weight": 2, "reverse": True},
                     {"question_id": "Q2", "weight": 1, "reverse": False}], 1)
        v2 = self.repo.score_answers("U1", 1, "A", 2)
        self.assertNotEqual(v1["weighted_sum"], v2["weighted_sum"])
        self.assertEqual(v1["weighted_sum"], 8)
        # v2 Q1 reversed: (1+5-4)*2 = 4; Q2 missing own range 0..2.
        self.assertEqual(v2["weighted_sum"], 4)

    def test_bank_range_edit_cannot_break_published_scale(self) -> None:
        self.assertCode("range_breaks_published_scale", self.catalog.upsert_question,
                        "Q1", 2, 5)
        # The failed edit must not have persisted.
        self.assertEqual(self.catalog.get_question("Q1"), {"question_id": "Q1",
                                                           "min": 1, "max": 5})

    def test_operations_journal_is_atomic_and_replay_safe(self) -> None:
        from cli import _apply_operation
        from common import transaction
        journal = []
        with open(os.path.join(MATERIALS, "operations.jsonl"), encoding="utf-8") as fh:
            journal = [json.loads(line) for line in fh if line.strip()]

        def run_journal() -> list[str]:
            statuses = []
            with transaction(self.catalog.conn):
                with transaction(self.repo.conn):
                    for operation in journal:
                        statuses.append(_apply_operation(self.catalog, self.repo,
                                                         operation)
                                        .get("status", "report"))
            return statuses

        first = run_journal()
        self.assertEqual(first[0], "published")
        report_ids = [r["report_id"] for r in self.repo.list_reports()]
        # A replay must not duplicate products.
        second = run_journal()
        self.assertEqual(second[0], "already_published")
        self.assertEqual([r["report_id"] for r in self.repo.list_reports()],
                         report_ids)
        self.assertEqual(len(self.repo.list_reports()), 2)


if __name__ == "__main__":
    unittest.main()
