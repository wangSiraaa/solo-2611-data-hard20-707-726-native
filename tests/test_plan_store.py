import json
import tempfile
import unittest
from pathlib import Path

from sql_param.errors import PlanStoreError
from sql_param.plan_store import PLAN_FORMAT, PlanStore, validate_plan


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlanStore(Path(self.tmp.name) / "plans")

    def test_publish_writes_public_plan_file(self):
        plan = self.store.publish("put", 1, "INSERT INTO t VALUES(:id)")
        self.assertEqual(plan["format"], PLAN_FORMAT)
        path = self.store._plan_path("put", 1)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, plan)
        # load 重新读盘并校验，不依赖发布时的内存对象。
        self.assertEqual(self.store.load("put", 1), plan)

    def test_replay_same_source_is_idempotent(self):
        source = "SELECT :id"
        first = self.store.publish("t", 1, source)
        second = self.store.publish("t", 1, source)
        self.assertEqual(first, second)

    def test_different_source_same_revision_rejected(self):
        self.store.publish("t", 1, "SELECT :id")
        with self.assertRaisesRegex(PlanStoreError, "different source"):
            self.store.publish("t", 1, "SELECT :id, :x")

    def test_new_revision_keeps_old_plan(self):
        self.store.publish("put", 1, "INSERT INTO notes VALUES(:id, ':v1')")
        self.store.publish("put", 2, "INSERT INTO notes VALUES(:id, ':v2')")
        self.assertEqual(
            self.store.load("put", 1)["source"],
            "INSERT INTO notes VALUES(:id, ':v1')",
        )
        self.assertEqual(
            self.store.load("put", 2)["sql"],
            "INSERT INTO notes VALUES(?, ':v2')",
        )

    def test_missing_plan_and_bad_request(self):
        with self.assertRaises(PlanStoreError):
            self.store.load("nope", 1)
        with self.assertRaises(PlanStoreError):
            self.store.load("../escape", 1)
        with self.assertRaises(PlanStoreError):
            self.store._plan_path("t", 0)

    def test_validate_plan_rejects_bad_positions(self):
        plan = self.store.publish("t", 1, "SELECT :id")
        good = json.loads(json.dumps(plan))
        validate_plan(good)
        bad = json.loads(json.dumps(plan))
        bad["parameters"][0]["start"] = 99
        with self.assertRaises(PlanStoreError):
            validate_plan(bad)
        bad2 = json.loads(json.dumps(plan))
        bad2["format"] = "other"
        with self.assertRaises(PlanStoreError):
            validate_plan(bad2)

    def test_store_reopened_sees_existing_plans(self):
        self.store.publish("t", 1, "SELECT :id")
        reopened = PlanStore(self.store.store_dir)
        self.assertEqual(reopened.load("t", 1)["sql"], "SELECT ?")


if __name__ == "__main__":
    unittest.main()
