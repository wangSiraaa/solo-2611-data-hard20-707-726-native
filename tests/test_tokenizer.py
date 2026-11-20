import unittest

from index_service.tokenizer import tokenize_segment


class TokenizerTests(unittest.TestCase):
    def test_basic_lowercasing(self):
        self.assertEqual(tokenize_segment("Amber BOLT gear"), ["amber", "bolt", "gear"])

    def test_punctuation_is_separator(self):
        self.assertEqual(tokenize_segment("stop-tag"), ["stop", "tag"])
        self.assertEqual(tokenize_segment("a.b,c;d"), ["a", "b", "c", "d"])

    def test_underscores_are_not_tokens(self):
        self.assertEqual(tokenize_segment("___"), [])
        self.assertEqual(tokenize_segment("a__b___c"), ["a", "b", "c"])

    def test_digits_are_tokens(self):
        self.assertEqual(tokenize_segment("v1 x86 3d"), ["v1", "x86", "3d"])

    def test_non_ascii_is_separator(self):
        self.assertEqual(tokenize_segment("amber中gear"), ["amber", "gear"])

    def test_empty_segment(self):
        self.assertEqual(tokenize_segment(""), [])

    def test_positions_ignore_separators(self):
        # "a - b" -> positions a=0, b=1, regardless of the dash.
        self.assertEqual(tokenize_segment("a - b"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
