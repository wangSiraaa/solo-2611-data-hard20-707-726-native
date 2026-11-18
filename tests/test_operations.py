"""Operation-file and CLI tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clocklab import ClockCatalogue, Ledger
from clocklab.operations import OperationRunner, load_operations

from support import MATERIALS, fresh_ledger, session

ROOT = Path(__file__).resolve().parents[1]


class MaterialOperationsTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fresh_ledger()
        self.runner = OperationRunner(self.ledger, MATERIALS)

    def test_material_operation_file(self):
        records = self.runner.run(load_operations(MATERIALS / "operations.jsonl"))
        self.assertEqual(
            [r["op"] for r in records],
            ["resolve_points", "evaluate", "apply_clock_revision",
             "evaluate", "read_evaluation"],
        )
        self.assertTrue(all(r["result"]["status"] != "error" for r in records))

        points = {p["local"]: p for p in records[0]["result"]["points"]}
        self.assertEqual([c["utc"] for c in points[90]["candidates"]], [30])
        self.assertEqual(points[200]["candidates"], [])
        self.assertEqual([c["utc"] for c in points[330]["candidates"]], [210, 270])
        self.assertEqual(
            records[0]["result"]["registered_sessions"], ["J1", "J_GAP"]
        )

        e1 = records[1]["result"]
        self.assertEqual(
            (e1["outcome"]["revision"], e1["outcome"]["utc_start"],
             e1["outcome"]["utc_end"], e1["outcome"]["duration_minutes"],
             e1["outcome"]["cost"]),
            ("r1", 180, 270, 90, 180),
        )
        self.assertEqual(records[2]["result"]["previous"], "r1")
        self.assertEqual(records[2]["result"]["revision"], "r2")
        e2 = records[3]["result"]
        self.assertEqual(
            (e2["outcome"]["revision"], e2["outcome"]["utc_start"],
             e2["outcome"]["utc_end"], e2["outcome"]["duration_minutes"],
             e2["outcome"]["cost"]),
            ("r2", 210, 270, 60, 120),
        )
        old = records[4]["result"]
        self.assertEqual(old["revision"], "r1")
        self.assertEqual(old["stored_duration_minutes"], 90)
        self.assertEqual(old["stored_cost"], 180)

    def test_operation_errors_do_not_stop_the_run(self):
        operations = [
            {"op": "evaluate", "session_id": "missing", "evaluation_id": "X"},
            {"op": "read_evaluation", "evaluation_id": "nope"},
            {"op": "unknown"},
            {"op": "resolve_points", "file": "requests.json"},
        ]
        records = self.runner.run(operations)
        self.assertEqual(records[0]["result"]["status"], "error")
        self.assertEqual(records[1]["result"]["status"], "error")
        self.assertEqual(records[2]["result"]["status"], "error")
        self.assertEqual(records[3]["result"]["status"], "ok")

    def test_file_escape_is_refused(self):
        records = self.runner.run(
            [{"op": "resolve_points", "file": "../../etc/passwd"}]
        )
        self.assertEqual(records[0]["result"]["status"], "error")

    def test_replaying_the_whole_file_is_refused_not_double_billed(self):
        ops = load_operations(MATERIALS / "operations.jsonl")
        self.runner.run(ops)
        # The ledger is now on r2.  Replaying step 2 (E1) would settle the
        # same evaluation_id with different content, so it is refused rather
        # than silently rewriting or duplicating the stored r1 fact.
        second = self.runner.run(ops)
        self.assertEqual(second[0]["result"]["status"], "ok")  # points: same
        self.assertEqual(second[1]["result"]["status"], "error")
        self.assertEqual(second[1]["result"]["error_type"], "ConflictingEvaluation")
        self.assertEqual(second[3]["result"]["status"], "ok")
        self.assertTrue(second[3]["result"]["replayed"])
        self.assertEqual(
            self.ledger.read_evaluation("E1")["stored_cost"], 180
        )
        self.assertEqual(
            self.ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM evaluation"
            ).fetchone()["n"],
            2,  # E1 and E2, unchanged by the replay
        )


class WorkloadScaleTests(unittest.TestCase):
    """10 zones, 20 segments each, 500 sessions."""

    def test_scale(self):
        zones = []
        for z in range(10):
            segments = []
            for i in range(20):
                start = i * 100
                # Alternate between two offsets so every zone has overlaps.
                offset = 50 if i % 2 == 0 else 0
                segments.append(
                    {"utc_start": start, "utc_end": start + 100, "offset": offset}
                )
            zones.append(
                {"zone_id": f"Z{z}", "utc_domain": [0, 2000], "segments": segments}
            )
        ledger = fresh_ledger({"revision": "scale", "zones": zones})
        for n in range(500):
            zone = f"Z{n % 10}"
            # local 150 is always produced by segment 0 (offset 50 -> UTC 100)
            # and segment 1 (offset 0 -> UTC 150): choose explicitly.
            ledger.register_session(
                session(
                    f"S{n}",
                    local_start=1100,
                    local_end=1300,
                    rate=1 + (n % 3),
                    start_occurrence="earlier",
                    end_occurrence="later",
                    zone_id=zone,
                )
            )
            result = ledger.evaluate(f"S{n}", f"E{n}")
            self.assertEqual(result["status"], "ok")
        summary = ledger.summarize(1150, 1250)
        self.assertEqual(len(summary["intersections"]), 500)
        self.assertEqual(summary["total_minutes"], 500 * 100)


class CliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "clocklab", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ledger.sqlite")
            result = self._run(
                "load-clock", "--db", db, "--file",
                str(MATERIALS / "clock.json"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["active_revision"], "r1")

            result = self._run(
                "run", "--db", db, "--ops", str(MATERIALS / "operations.jsonl")
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            steps = json.loads(result.stdout)["steps"]
            self.assertEqual(len(steps), 5)

            result = self._run("point", "--db", db, "--local", "330", "--zone", "Z")
            self.assertEqual(result.returncode, 0, result.stderr)
            point = json.loads(result.stdout)
            self.assertEqual([c["utc"] for c in point["candidates"]], [270])

            result = self._run(
                "summary", "--db", db, "--from", "180", "--to", "200",
                "--revision", "r1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["total_minutes"], 20)
            self.assertEqual(summary["total_cost"], 40)

            result = self._run("evaluation", "--db", db, "--evaluation", "E1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["stored_cost"], 180)

            result = self._run("session", "--db", db, "--session-id", "J1")
            self.assertEqual(result.returncode, 0, result.stderr)
            defined = json.loads(result.stdout)
            self.assertEqual(
                [e["evaluation_id"] for e in defined["evaluations"]], ["E1", "E2"]
            )

            result = self._run("point", "--db", db, "--local", "200", "--zone", "Z")
            self.assertEqual(json.loads(result.stdout)["status"], "no_candidate")


if __name__ == "__main__":
    unittest.main()
