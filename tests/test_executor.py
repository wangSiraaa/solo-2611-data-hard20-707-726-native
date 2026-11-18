import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.errors import BatchError
from sql_param.executor import BatchExecutor
from sql_param.plan_store import PlanStore

SCHEMA = "CREATE TABLE notes(id INTEGER PRIMARY KEY, message TEXT, payload BLOB);"


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "data.db"
        self.store = PlanStore(root / "store")
        self.executor = BatchExecutor(self.store, self.db_path, root / "receipts")
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

        self.put_plan = self.store.publish(
            "put", 1,
            "INSERT INTO notes(id, message, payload) VALUES(:id, :text || ':literal', :blob) /* :ignored ? ; */",
        )
        self.get_plan = self.store.publish(
            "get", 1,
            "SELECT id, message, payload, :id AS echo FROM notes WHERE id=:id ORDER BY id -- :ignored",
        )

    def all_rows(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT id, message, payload FROM notes ORDER BY id").fetchall()

    def test_normal_batch_commit_and_select_order(self):
        receipt = self.executor.execute_batch("b-ok", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "text": "hello", "blob": {"$bytes": "00ff"}}},
            {"template_id": "get", "revision": 1, "bindings": {"id": 1}},
        ])
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(self.all_rows(), [(1, "hello:literal", b"\x00\xff")])

        select_item = receipt["items"][1]
        self.assertTrue(select_item["result"]["order_by"])
        self.assertEqual(select_item["result"]["columns"], ["id", "message", "payload", "echo"])
        self.assertEqual(select_item["result"]["rows"],
                         [[1, "hello:literal", {"$bytes": "00ff"}, 1]])
        # 回执内嵌计划快照，且 ordered_values 按参数出现顺序（get 里 id 出现两次）。
        self.assertEqual(select_item["plan"]["source"], self.get_plan["source"])
        self.assertEqual(select_item["ordered_values"], [1, 1])

    def test_constraint_failure_rolls_back_prior_writes(self):
        # 预置 id=1，使失败批的第二项触发主键冲突（与材料 b-rollback 场景一致）。
        self.executor.execute_batch("seed", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "text": "seed", "blob": None}},
        ])
        receipt = self.executor.execute_batch("b-rollback", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 2, "text": None, "blob": {"$bytes": ""}}},
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "text": "duplicate", "blob": None}},
        ])
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(receipt["items"][0]["status"], "ok")
        self.assertEqual(receipt["items"][1]["status"], "failed")
        self.assertIn("IntegrityError", receipt["items"][1]["error"])
        # 第一条插入必须随回滚消失；预置的 id=1 仍在，id=2 不得落库。
        self.assertEqual([row[0] for row in self.all_rows()], [1])
        # 失败回执已落盘，重开可读，且不标成功。
        reread = self.executor.read_batch("b-rollback")
        self.assertEqual(reread["status"], "rolled_back")

    def test_preflight_rejects_before_touching_database(self):
        with self.assertRaises(BatchError):
            self.executor.execute_batch("bad", [
                {"template_id": "put", "revision": 1,
                 "bindings": {"id": 9, "text": "x"}},  # 缺 blob
            ])
        with self.assertRaises(BatchError):
            self.executor.execute_batch("bad2", [
                {"template_id": "put", "revision": 1,
                 "bindings": {"id": 9, "text": "x", "blob": None, "extra": 1}},
            ])
        with self.assertRaises(BatchError):
            self.executor.execute_batch("bad3", [
                {"template_id": "missing", "revision": 1, "bindings": {}},
            ])
        with self.assertRaises(BatchError):
            self.executor.execute_batch("bad4", [
                {"template_id": "put", "revision": 1,
                 "bindings": {"id": True, "text": "x", "blob": None}},
            ])
        self.assertEqual(self.all_rows(), [])
        # 预检失败不产生回执文件。
        self.assertEqual(list(self.executor.receipts_dir.iterdir()), [])

    def test_revision_publish_changes_only_later_batches(self):
        self.executor.execute_batch("old", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "text": "hello", "blob": {"$bytes": "00ff"}}},
        ])
        self.store.publish(
            "put", 2,
            "INSERT INTO notes(id, message, payload) VALUES(:id, :text || ':revised', :blob)",
        )
        self.executor.execute_batch("new", [
            {"template_id": "put", "revision": 2,
             "bindings": {"id": 3, "text": "new", "blob": None}},
        ])
        rows = dict((r[0], r[1]) for r in self.all_rows())
        self.assertEqual(rows[1], "hello:literal")
        self.assertEqual(rows[3], "new:revised")

        old_receipt = self.executor.read_batch("old")
        self.assertEqual(old_receipt["items"][0]["revision"], 1)
        self.assertIn(":literal", old_receipt["items"][0]["plan"]["source"])

    def test_batch_id_is_immutable(self):
        item = {"template_id": "put", "revision": 1,
                "bindings": {"id": 1, "text": "a", "blob": None}}
        self.executor.execute_batch("once", [item])
        with self.assertRaises(BatchError):
            self.executor.execute_batch("once", [item])

    def test_executor_uses_saved_plan_not_source_scan(self):
        # 篡改磁盘上的计划 sql（source 保持不动）：执行侧必须照保存的 sql 走，
        # 证明它不重新扫描 source 编译。
        self.executor.execute_batch("seed", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 5, "text": "x", "blob": None}},
        ])
        plan_path = self.store._plan_path("put", 1)
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        saved["sql"] = "INSERT INTO notes(id, message, payload) VALUES(?, 'tampered', ?)"
        # 去掉一个参数位以匹配两个绑定（id, blob），手工保持位置合法。
        saved["parameters"] = [saved["parameters"][0], saved["parameters"][2]]
        plan_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        receipt = self.executor.execute_batch("tampered", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 6, "blob": None}},
        ])
        self.assertEqual(receipt["status"], "committed")
        with sqlite3.connect(self.db_path) as conn:
            message = conn.execute(
                "SELECT message FROM notes WHERE id=6"
            ).fetchone()[0]
        self.assertEqual(message, "tampered")


if __name__ == "__main__":
    unittest.main()
