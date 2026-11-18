"""End-to-end tests of the operation-file runner and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from multiset_join.runner import run_document_path, run_operations

MATERIALS = os.path.join(ROOT, "materials", "743", "input.json")


def by_op(steps, op):
    return [step for step in steps if step.get("op") == op]


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src.db")
        self.drv = os.path.join(self.tmp.name, "derived.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_operation_document(self) -> None:
        run = run_document_path(MATERIALS, self.src, self.drv)
        self.assertIsNone(run["error"])
        steps = run["steps"]

        derives = {step["result_id"]: step for step in by_op(steps, "derive")}
        self.assertEqual(derives["initial"]["source_version"], 0)
        self.assertEqual(derives["initial"]["totals"]["derived_row_count"], 3)
        self.assertEqual(derives["initial"]["totals"]["quoted_amount"], 12)
        self.assertEqual(derives["initial"]["totals"]["unquoted_row_count"], 1)

        self.assertEqual(derives["after-b2"]["totals"]["derived_row_count"], 4)
        self.assertEqual(derives["after-b2"]["totals"]["quoted_amount"], 24)
        self.assertEqual(derives["after-b2"]["totals"]["unquoted_row_count"], 1)

        self.assertEqual(derives["after-b3"]["totals"]["derived_row_count"], 4)
        self.assertEqual(derives["after-b3"]["totals"]["quoted_amount"], 0)
        self.assertEqual(derives["after-b3"]["totals"]["unquoted_row_count"], 4)

        # Historical read survived b3.
        read = by_op(steps, "read_result")[0]
        self.assertEqual(read["result_id"], "initial")
        self.assertEqual(read["totals"]["quoted_amount"], 12)
        self.assertEqual(read["source_version"], 0)

        # Replay reports b2's original slice, without re-applying.
        replay = by_op(steps, "replay")[0]
        self.assertEqual(replay["source_version"], 1)
        self.assertEqual(replay["current_source_version"], 2)
        self.assertEqual(replay["slice"]["version"], 1)
        self.assertEqual(
            replay["slice"]["S"], [{"tuple": ["A", "w", 4], "multiplicity": 1}]
        )
        self.assertEqual(
            replay["slice"]["R"],
            [
                {"tuple": ["east", "A", 2], "multiplicity": 3},
                {"tuple": ["east", "Q", 1], "multiplicity": 1},
            ],
        )

    def test_apply_steps_report_versions(self) -> None:
        run = run_document_path(MATERIALS, self.src, self.drv)
        applies = {step["batch_id"]: step for step in by_op(run["steps"], "apply")}
        self.assertEqual(applies["b2"]["source_version"], 1)
        self.assertEqual(applies["b2"]["status"], "applied")
        self.assertEqual(applies["b3"]["source_version"], 2)
        self.assertEqual(
            sorted((c["source"], tuple(c["tuple"]), c["delta"]) for c in applies["b2"]["changes"]),
            [
                ("R", ("east", "A", 2), 1),
                ("S", ("A", "v", 3), -1),
                ("S", ("A", "w", 4), 1),
            ],
        )

    def test_rerun_operation_document_is_idempotent(self) -> None:
        first = run_document_path(MATERIALS, self.src, self.drv)
        second = run_document_path(MATERIALS, self.src, self.drv)
        self.assertIsNone(second["error"])
        self.assertEqual(
            {step["result_id"]: step["totals"] for step in by_op(second["steps"], "derive")},
            {step["result_id"]: step["totals"] for step in by_op(first["steps"], "derive")},
        )
        applies = by_op(second["steps"], "apply")
        self.assertTrue(all(step["status"] == "replayed" for step in applies))

    def test_operations_can_pin_versions_and_filter_reads(self) -> None:
        document = {
            "initial": {
                "R": [{"tuple": ["east", "A", 2], "multiplicity": 2}],
                "S": [{"tuple": ["A", "v", 3], "multiplicity": 1}],
            },
            "batches": [
                {
                    "batch_id": "b",
                    "changes": [
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -1},
                        {"source": "S", "tuple": ["A", "w", 4], "delta": 1},
                    ],
                }
            ],
            "operations": [
                {"op": "apply", "batch_id": "b"},
                {"op": "derive", "result_id": "at-b", "version": 1},
                {"op": "derive", "result_id": "at-b-by-batch", "at_batch": "b"},
                {"op": "read_slice", "version": 0},
                {"op": "read_source", "source": "S", "version": 1},
                {"op": "tuple_count", "source": "S", "tuple": ["A", "w", 4]},
                {"op": "read_result", "result_id": "at-b", "include_rows": True},
                {"op": "explain", "result_id": "at-b", "region": "east"},
            ],
        }
        run = run_operations(document, self.src, self.drv)
        self.assertIsNone(run["error"])
        steps = run["steps"]
        same = {s["result_id"]: s["source_version"] for s in by_op(steps, "derive")}
        self.assertEqual(same, {"at-b": 1, "at-b-by-batch": 1})
        slice0 = by_op(steps, "read_slice")[0]
        self.assertEqual(slice0["S"], [{"tuple": ["A", "v", 3], "multiplicity": 1}])
        read_source = by_op(steps, "read_source")[0]
        self.assertEqual(read_source["rows"], [{"tuple": ["A", "w", 4], "multiplicity": 1}])
        count = by_op(steps, "tuple_count")[0]
        self.assertEqual(count["multiplicity"], 1)
        read = by_op(steps, "read_result")[0]
        self.assertEqual(read["rows"][0]["supplier"], "w")
        explain = by_op(steps, "explain")[0]
        self.assertEqual(explain["contributions"][0]["contribution"], 2)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src.db")
        self.drv = os.path.join(self.tmp.name, "derived.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "multiset_join", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, expect, proc.stderr or proc.stdout)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def test_cli_roundtrip(self) -> None:
        run = self.cli(
            "run", MATERIALS, "--source-db", self.src, "--derived-db", self.drv
        )
        self.assertIsNone(run["error"])

        result = self.cli(
            "read-result",
            "--source-db", self.src,
            "--derived-db", self.drv,
            "--result-id", "after-b2",
            "--rows",
        )
        self.assertEqual(result["totals"]["quoted_amount"], 24)
        self.assertEqual(result["rows"][0]["supplier"], "w")

        explain = self.cli(
            "explain",
            "--source-db", self.src,
            "--derived-db", self.drv,
            "--result-id", "after-b2",
            "--sku", "A",
        )
        self.assertEqual(explain["contributions"][0]["formula"], "3 x 1 = 3")

        count = self.cli(
            "tuple-count",
            "--source-db", self.src,
            "R", '["east","A",2]',
            "--version", "1",
        )
        self.assertEqual(count["multiplicity"], 3)

        replay = self.cli(
            "replay", MATERIALS, "--source-db", self.src, "--batch-id", "b2"
        )
        self.assertEqual(replay["source_version"], 1)
        self.assertEqual(replay["current_source_version"], 2)

    def test_cli_error_is_json(self) -> None:
        self.cli("run", MATERIALS, "--source-db", self.src, "--derived-db", self.drv)
        bad = self.cli(
            "tuple-count", "--source-db", self.src, "S", '["A","v",-1]',
            expect=2,
        )
        self.assertEqual(bad["error"], "ValidationError")

    def test_cli_batch_is_atomic(self) -> None:
        self.cli("import", MATERIALS, "--source-db", self.src)
        bad_doc = {
            "batches": [
                {
                    "batch_id": "neg",
                    "changes": [
                        {"source": "S", "tuple": ["A", "v", 3], "delta": -5}
                    ],
                }
            ]
        }
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(bad_doc, handle)
        error = self.cli(
            "apply", path, "--source-db", self.src, "--batch-id", "neg", expect=2
        )
        self.assertEqual(error["error"], "NegativeCountError")
        count = self.cli(
            "tuple-count", "--source-db", self.src, "S", '["A","v",3]'
        )
        self.assertEqual(count["multiplicity"], 1)


if __name__ == "__main__":
    unittest.main()
