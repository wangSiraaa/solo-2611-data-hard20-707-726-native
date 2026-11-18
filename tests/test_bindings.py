import unittest

from sql_param.bindings import bind_parameters, decode_value, encode_json_value
from sql_param.compiler import compile_template
from sql_param.errors import BindingError


class DecodeTests(unittest.TestCase):
    def test_supported_values(self):
        self.assertIsNone(decode_value(None))
        self.assertEqual(decode_value("héllo"), "héllo")
        self.assertEqual(decode_value(0), 0)
        self.assertEqual(decode_value(-(2**63)), -(2**63))
        self.assertEqual(decode_value(2**63 - 1), 2**63 - 1)
        self.assertEqual(decode_value({"$bytes": "00ff"}), b"\x00\xff")
        self.assertEqual(decode_value({"$bytes": ""}), b"")

    def test_rejected_values(self):
        for value in (True, False, 2**63, -(2**63) - 1, 1.5, [1], {"x": 1},
                      {"$bytes": "0"}, {"$bytes": "zz"}, {"$bytes": 1},
                      {"$bytes": "ab", "x": 1}):
            with self.subTest(value=value):
                with self.assertRaises(BindingError):
                    decode_value(value)

    def test_roundtrip_bytes(self):
        for raw in (b"", b"\x00\xff\x10", "字节".encode()):
            self.assertEqual(decode_value(encode_json_value(raw)), raw)


class BindTests(unittest.TestCase):
    def setUp(self):
        self.compiled = compile_template(
            "SELECT :id, :name, :id", "t", 1
        )

    def test_duplicate_name_copies_value_in_order(self):
        values = bind_parameters(self.compiled, {"id": 7, "name": "n"})
        self.assertEqual(values, (7, "n", 7))

    def test_missing_and_extra_keys(self):
        with self.assertRaisesRegex(BindingError, "missing"):
            bind_parameters(self.compiled, {"id": 1})
        with self.assertRaisesRegex(BindingError, "extra"):
            bind_parameters(self.compiled, {"id": 1, "name": "n", "extra": 2})
        with self.assertRaises(BindingError):
            bind_parameters(self.compiled, {"name": "n"})

    def test_null_and_bytes_via_compiled(self):
        compiled = compile_template(
            "INSERT INTO t VALUES(:a, :b, :c)", "t", 1
        )
        values = bind_parameters(
            compiled,
            {"a": None, "b": {"$bytes": "00ff"}, "c": "héllo"},
        )
        self.assertEqual(values, (None, b"\x00\xff", "héllo"))

    def test_bool_rejected_even_though_int(self):
        with self.assertRaises(BindingError):
            bind_parameters(self.compiled, {"id": True, "name": "n"})

    def test_empty_parameter_template(self):
        compiled = compile_template("SELECT 1", "t", 1)
        self.assertEqual(bind_parameters(compiled, {}), ())
        with self.assertRaisesRegex(BindingError, "extra"):
            bind_parameters(compiled, {"x": 1})


if __name__ == "__main__":
    unittest.main()
