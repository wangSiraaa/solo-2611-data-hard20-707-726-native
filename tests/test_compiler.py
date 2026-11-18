import unittest

from sql_param.compiler import compile_template
from sql_param.errors import TemplateCompileError


class CompilerTests(unittest.TestCase):
    def test_material_templates(self):
        put = compile_template(
            "INSERT INTO notes(id, message, payload) VALUES(:id, :text || ':literal', :blob) /* :ignored ? ; */",
            "put", 1,
        )
        self.assertEqual(
            put.sql,
            "INSERT INTO notes(id, message, payload) VALUES(?, ? || ':literal', ?) /* :ignored ? ; */",
        )
        self.assertEqual([p.name for p in put.parameters], ["id", "text", "blob"])
        self.assertEqual([(p.start, p.end) for p in put.parameters],
                         [(47, 50), (52, 57), (73, 78)])
        for param in put.parameters:
            self.assertEqual(put.source[param.start:param.end], ":" + param.name)
        self.assertEqual(put.statement, "insert")
        self.assertFalse(put.has_order_by)

        get = compile_template(
            "SELECT id, message, payload, :id AS echo FROM notes WHERE id=:id ORDER BY id -- :ignored",
            "get", 1,
        )
        self.assertEqual(
            get.sql,
            "SELECT id, message, payload, ? AS echo FROM notes WHERE id=? ORDER BY id -- :ignored",
        )
        self.assertEqual([p.name for p in get.parameters], ["id", "id"])
        self.assertEqual([(p.start, p.end) for p in get.parameters], [(29, 32), (61, 64)])
        self.assertEqual(get.statement, "select")
        self.assertTrue(get.has_order_by)

    def test_unicode_positions_are_code_points(self):
        # “甲”是一个代码点但占 3 个 UTF-8 字节；位置必须按字符计。
        source = "SELECT '甲:ignored' AS note, :id AS x -- :ignored"
        compiled = compile_template(source, "u", 1)
        (param,) = compiled.parameters
        self.assertEqual(source[param.start:param.end], ":id")
        self.assertEqual(param.start, 28)
        self.assertEqual(param.end, 31)

    def test_double_quote_escape_and_duplicate_name(self):
        source = 'SELECT \'甲:ignored\' AS "a""b", :id AS first, :id AS second'
        compiled = compile_template(source, "quoted", 1)
        self.assertEqual(
            compiled.sql,
            'SELECT \'甲:ignored\' AS "a""b", ? AS first, ? AS second',
        )
        self.assertEqual([p.name for p in compiled.parameters], ["id", "id"])
        self.assertEqual(compiled.distinct_names, ("id",))
        # 双引号内的双引号转义完整保留，且不会提前结束标识符。
        self.assertIn('"a""b"', compiled.sql)

    def test_escaped_single_quotes_keep_colon_literal(self):
        compiled = compile_template(
            "SELECT 'it''s :x ; ?' AS s, :y", "q", 1
        )
        self.assertEqual(compiled.sql, "SELECT 'it''s :x ; ?' AS s, ?")
        self.assertEqual([p.name for p in compiled.parameters], ["y"])

    def test_line_comment_terminates_at_eof(self):
        compiled = compile_template("SELECT :a -- comment :b ? ;\n", "c", 1)
        self.assertEqual(compiled.sql, "SELECT ? -- comment :b ? ;\n")
        compiled2 = compile_template("SELECT :a -- comment to eof", "c", 1)
        self.assertEqual(compiled2.sql, "SELECT ? -- comment to eof")

    def test_block_comment_preserved(self):
        compiled = compile_template(
            "UPDATE t SET x=:a /* :b ? ; */ WHERE y=:c", "u", 1
        )
        self.assertEqual(
            compiled.sql, "UPDATE t SET x=? /* :b ? ; */ WHERE y=?"
        )
        self.assertEqual([p.name for p in compiled.parameters], ["a", "c"])
        self.assertEqual(compiled.statement, "update")

    def test_rejected_inputs(self):
        cases = {
            "unterminated string": "SELECT 'abc",
            "unterminated identifier": 'SELECT "abc',
            "unterminated block comment": "SELECT 1 /* x",
            "semicolon normal": "SELECT 1;",
            "semicolon middle": "INSERT INTO t VALUES(:a); DELETE FROM t",
            "question mark": "SELECT * FROM t WHERE x=?",
            "bare colon": "SELECT * FROM t WHERE x=:",
            "colon digit": "SELECT :1",
            "colon space": "SELECT : name FROM t",
            "empty": "  -- only comment\n",
            "delete statement": "DELETE FROM t WHERE id=:id",
            "pragma statement": "PRAGMA table_info(t)",
        }
        for label, source in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(TemplateCompileError):
                    compile_template(source, "t", 1)

    def test_question_mark_inside_string_is_kept(self):
        compiled = compile_template("SELECT '?' AS q, ':ok' AS c, :id", "t", 1)
        self.assertEqual(compiled.sql, "SELECT '?' AS q, ':ok' AS c, ?")
        self.assertEqual([p.name for p in compiled.parameters], ["id"])

    def test_underscore_and_digit_names(self):
        compiled = compile_template("SELECT :a_1, :Z9", "t", 1)
        self.assertEqual([p.name for p in compiled.parameters], ["a_1", "Z9"])
        self.assertEqual(compiled.sql, "SELECT ?, ?")

    def test_no_parameters_plan(self):
        compiled = compile_template("SELECT 1 ORDER BY 1", "t", 1)
        self.assertEqual(compiled.parameters, ())
        self.assertEqual(compiled.distinct_names, ())

    def test_order_by_detection_ignores_comments_and_strings(self):
        self.assertTrue(compile_template("select :a from t order by 1", "t", 1).has_order_by)
        self.assertFalse(compile_template("SELECT * FROM t WHERE x=:x", "t", 2).has_order_by)
        self.assertFalse(
            compile_template("SELECT :a /* ORDER BY x */ FROM t", "t", 3).has_order_by
        )
        self.assertFalse(
            compile_template("SELECT 'ORDER BY x' AS s, :a FROM t", "t", 4).has_order_by
        )

    def test_parity_with_reader_example_plan(self):
        import json
        from pathlib import Path
        sample = json.loads(
            (Path(__file__).resolve().parents[1]
             / "materials" / "717" / "reader-example-plan.json").read_text(encoding="utf-8")
        )
        compiled = compile_template(sample["source"], sample["template_id"], sample["revision"])
        self.assertEqual(compiled.sql, sample["sql"])
        self.assertEqual(
            [(p.name, p.start, p.end) for p in compiled.parameters],
            [(p["name"], p["start"], p["end"]) for p in sample["parameters"]],
        )


if __name__ == "__main__":
    unittest.main()
