import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.compiler import compile_template
from sql_param.errors import BatchError, PlanStoreError
from sql_param.executor import MAX_ITEMS_PER_BATCH, BatchExecutor
from sql_param.plan_store import MAX_TEMPLATES, PlanStore

SCHEMA = "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"


class LimitsTests(unittest.TestCase):
    def test_template_limit_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PlanStore(Path(tmp) / "store")
            for i in range(MAX_TEMPLATES):
                store.publish(f"t{i:03d}", 1, "SELECT :id")
            self.assertEqual(len(store._template_dirs()), MAX_TEMPLATES)
            with self.assertRaisesRegex(PlanStoreError, "template limit"):
                store.publish("one_more", 1, "SELECT :id")

    def test_batch_item_limit_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            with sqlite3.connect(db) as conn:
                conn.executescript(SCHEMA)
            store = PlanStore(root / "store")
            store.publish("ins", 1, "INSERT INTO t(id, v) VALUES(:id, :v)")
            executor = BatchExecutor(store, db, root / "receipts")

            ok_items = [
                {"template_id": "ins", "revision": 1,
                 "bindings": {"id": i, "v": "x"}}
                for i in range(MAX_ITEMS_PER_BATCH)
            ]
            receipt = executor.execute_batch("exactly-100", ok_items)
            self.assertEqual(receipt["status"], "committed")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 100)

            too_many = [
                {"template_id": "ins", "revision": 1,
                 "bindings": {"id": 1000 + i, "v": "y"}}
                for i in range(MAX_ITEMS_PER_BATCH + 1)
            ]
            with self.assertRaisesRegex(BatchError, "item limit"):
                executor.execute_batch("101-items", too_many)
            # 超限批次不产生回执、不写数据。
            self.assertFalse((root / "receipts" / "101-items.json").exists())

    def test_empty_batch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = BatchExecutor(PlanStore(root / "s"), root / "db", root / "r")
            with self.assertRaises(BatchError):
                executor.execute_batch("empty", [])


class UnicodeAndPersistenceTests(unittest.TestCase):
    def test_non_ascii_colon_rejected_but_text_kept(self):
        # 名字只允许 ASCII 字母开头：“:用户”的冒号后不是合法 name，必须明确拒绝。
        from sql_param.errors import TemplateCompileError
        with self.assertRaises(TemplateCompileError):
            compile_template("SELECT :用户", "u2", 1)

        # 中文字符在正常区域只是普通文本；字符串里的冒号逐字保留，
        # 后续参数位置按 Unicode 代码点计算。
        source = "SELECT '中文:冒号' AS s, :id FROM t"
        compiled = compile_template(source, "u", 1)
        (param,) = compiled.parameters
        self.assertEqual(source[param.start:param.end], ":id")
        self.assertEqual(param.start, len("SELECT '中文:冒号' AS s, "))
        self.assertIn("'中文:冒号'", compiled.sql)

    def test_unicode_offsets_before_parameter(self):
        source2 = "SELECT '中文' AS s, :id FROM t"
        c2 = compile_template(source2, "u3", 1)
        self.assertEqual(
            source2[c2.parameters[0].start:c2.parameters[0].end], ":id"
        )
        self.assertEqual(c2.parameters[0].start, len("SELECT '中文' AS s, "))

    def test_crlf_line_comment_and_eof(self):
        compiled = compile_template("SELECT :a -- c\r\nFROM t", "c", 1)
        self.assertEqual(compiled.sql, "SELECT ? -- c\r\nFROM t")
        self.assertEqual([p.name for p in compiled.parameters], ["a"])

    def test_plan_json_roundtrip_preserves_source_bytes(self):
        source = "SELECT '甲:ignored' AS \"a\"\"b\", :id"
        with tempfile.TemporaryDirectory() as tmp:
            store = PlanStore(Path(tmp) / "store")
            plan = store.publish("q", 1, source)
            text = (store._plan_path("q", 1)).read_text(encoding="utf-8")
            reloaded = json.loads(text)
            self.assertEqual(reloaded, plan)
            self.assertEqual(reloaded["source"], source)  # 无归一化
            self.assertIn("甲", text)                      # 原文中文直接落盘

    def test_receipt_reload_after_executor_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            with sqlite3.connect(db) as conn:
                conn.executescript(SCHEMA)
            store = PlanStore(root / "store")
            store.publish("ins", 1, "INSERT INTO t(id, v) VALUES(:id, :v)")
            BatchExecutor(store, db, root / "receipts").execute_batch(
                "b", [{"template_id": "ins", "revision": 1,
                       "bindings": {"id": 1, "v": "x"}}],
            )
            # 全新执行器重开后仍能回读完整回执（含计划与原文）。
            reopened = BatchExecutor(
                PlanStore(root / "store"), db, root / "receipts"
            )
            receipt = reopened.read_batch("b")
            self.assertEqual(receipt["status"], "committed")
            self.assertEqual(receipt["items"][0]["plan"]["source"],
                             "INSERT INTO t(id, v) VALUES(:id, :v)")


if __name__ == "__main__":
    unittest.main()
