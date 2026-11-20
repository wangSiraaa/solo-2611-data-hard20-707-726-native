import unittest

from index_service import Catalogue, Phrase, find_matches, find_matches_for_cut


def add(catalogue, document_id, revision, title, body):
    catalogue.add_revision(
        {"id": document_id, "revision": revision, "title": title, "body": body}
    )


class PhraseTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue()

    def tearDown(self):
        self.catalogue.close()

    def matches(self, field, words, g):
        return find_matches(self.catalogue, Phrase(field, tuple(words), g))

    def test_gap_exactly_at_limit(self):
        add(self.catalogue, "D1", 1, ["amber bolt gear"], [])
        # amber=0, gear=2 -> one intervening word; g=1 allows it.
        self.assertEqual(len(self.matches("title", ["amber", "gear"], 1)), 1)
        # g=0 would require adjacency.
        self.assertEqual(self.matches("title", ["amber", "gear"], 0), [])

    def test_gap_one_too_large(self):
        add(self.catalogue, "D1", 1, ["amber x y gear"], [])
        # amber=0, gear=3 -> two intervening words; g=1 rejects, g=2 allows.
        self.assertEqual(self.matches("title", ["amber", "gear"], 1), [])
        self.assertEqual(len(self.matches("title", ["amber", "gear"], 2)), 1)

    def test_phrase_cannot_cross_segment_boundary(self):
        add(self.catalogue, "D2", 1, ["amber", "gear"], [])
        for g in (0, 1, 5, 100):
            self.assertEqual(self.matches("title", ["amber", "gear"], g), [])

    def test_phrase_requires_same_field(self):
        add(self.catalogue, "D1", 1, ["amber"], ["gear"])
        self.assertEqual(self.matches("title", ["amber", "gear"], 5), [])
        self.assertEqual(self.matches("body", ["amber", "gear"], 5), [])

    def test_order_matters(self):
        add(self.catalogue, "D1", 1, ["gear amber"], [])
        self.assertEqual(self.matches("title", ["amber", "gear"], 5), [])
        self.assertEqual(len(self.matches("title", ["gear", "amber"], 5)), 1)

    def test_repeated_words_need_distinct_positions(self):
        add(self.catalogue, "D1", 1, ["amber amber"], [])
        matches = self.matches("title", ["amber", "amber"], 0)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].positions, (0, 1))

    def test_repeated_words_insufficient_occurrences(self):
        add(self.catalogue, "D1", 1, ["amber"], [])
        self.assertEqual(self.matches("title", ["amber", "amber"], 5), [])

    def test_returned_positions_are_a_legal_chain(self):
        add(self.catalogue, "D1", 1, ["amber bolt gear amber gear"], [])
        matches = self.matches("title", ["amber", "gear"], 1)
        self.assertEqual(len(matches), 1)
        positions = matches[0].positions
        self.assertEqual(len(positions), 2)
        self.assertTrue(0 <= positions[0] < positions[1] <= 4)
        self.assertLessEqual(positions[1] - positions[0], 2)

    def test_cut_restricts_to_old_revision(self):
        add(self.catalogue, "D1", 1, ["amber gear"], [])
        add(self.catalogue, "D1", 2, ["amber"], [])
        all_matches = self.matches("title", ["amber", "gear"], 0)
        self.assertEqual({m.revision for m in all_matches}, {1})
        old = find_matches_for_cut(
            self.catalogue, Phrase("title", ("amber", "gear"), 0), [("D1", 1)]
        )
        self.assertEqual([m.revision for m in old], [1])
        new = find_matches_for_cut(
            self.catalogue, Phrase("title", ("amber", "gear"), 0), [("D1", 2)]
        )
        self.assertEqual(new, [])

    def test_punctuation_does_not_create_gap(self):
        add(self.catalogue, "D1", 1, ["amber-bolt-gear"], [])
        # tokens are amber, bolt, gear at positions 0,1,2.
        self.assertEqual(len(self.matches("title", ["amber", "gear"], 1)), 1)

    def test_absent_word_matches_nothing(self):
        add(self.catalogue, "D1", 1, ["amber gear"], [])
        self.assertEqual(self.matches("title", ["amber", "zzz"], 5), [])

    def test_all_candidates_are_examined_not_greedy_only(self):
        # A greedy earliest-first walk would pair amber(0) with gear(3)
        # and miss the tighter amber(2),gear(3).  Existence must still hold.
        add(self.catalogue, "D1", 1, ["amber x y gear amber gear"], [])
        matches = self.matches("title", ["amber", "gear"], 0)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].positions, (4, 5))

    def test_backtracking_required(self):
        # word A appears at 0 and 2; word B only at 1.
        # A at 0 cannot reach B at 1 in order for "b a" but A at 2 works.
        add(self.catalogue, "D1", 1, ["a b c a d"], [])
        # b(1) -> a(3) fits g=1 (gap 1 word 'c').
        self.assertEqual(len(self.matches("title", ["b", "a"], 1)), 1)
        # a(0)->b(1) fits g=0.
        self.assertEqual(len(self.matches("title", ["a", "b"], 0)), 1)


if __name__ == "__main__":
    unittest.main()
