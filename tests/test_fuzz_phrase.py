import itertools
import random
import unittest

from index_service import Catalogue, Phrase, find_matches


def brute_force_positions(token_lists, words, g):
    """Reference matcher: enumerate every strictly-increasing chain."""

    k = len(words)
    pools = [token_lists.get(w, []) for w in words]
    if any(not p for p in pools):
        return None
    for chain in itertools.product(*pools):
        if any(chain[i] >= chain[i + 1] for i in range(k - 1)):
            continue
        if any(chain[i + 1] - chain[i] > g + 1 for i in range(k - 1)):
            continue
        return chain
    return None


class FuzzPhraseTests(unittest.TestCase):
    def test_dp_matches_brute_force(self):
        rng = random.Random(1234)
        alphabet = ["a", "b", "c"]
        for trial in range(400):
            length = rng.randint(0, 8)
            tokens = [rng.choice(alphabet) for _ in range(length)]
            k = rng.randint(1, 4)
            words = [rng.choice(alphabet) for _ in range(k)]
            g = rng.randint(0, 3)

            pools = {}
            for pos, token in enumerate(tokens):
                pools.setdefault(token, []).append(pos)
            expected = brute_force_positions(pools, words, g)

            catalogue = Catalogue()
            try:
                catalogue.add_revision(
                    {
                        "id": "D",
                        "revision": 1,
                        "title": [" ".join(tokens)],
                        "body": [],
                    }
                )
                matches = find_matches(catalogue, Phrase("title", tuple(words), g))
            finally:
                catalogue.close()

            if expected is None:
                self.assertEqual(
                    matches, [], msg=f"tokens={tokens} words={words} g={g}"
                )
            else:
                self.assertEqual(
                    len(matches), 1, msg=f"tokens={tokens} words={words} g={g}"
                )
                positions = matches[0].positions
                self.assertTrue(
                    all(positions[i] < positions[i + 1] for i in range(len(positions) - 1))
                )
                self.assertTrue(
                    all(
                        positions[i + 1] - positions[i] <= g + 1
                        for i in range(len(positions) - 1)
                    )
                )
                for word, pos in zip(words, positions):
                    self.assertEqual(tokens[pos], word)


if __name__ == "__main__":
    unittest.main()
