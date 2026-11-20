import tempfile
import unittest
from pathlib import Path

from index_service import Catalogue, RevisionConflict


def doc(document_id, revision, title, body):
    return {"id": document_id, "revision": revision, "title": title, "body": body}


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue()

    def tearDown(self):
        self.catalogue.close()

    def test_positions_are_per_segment(self):
        self.catalogue.add_revision(doc("D1", 1, ["Amber bolt gear"], ["stop blue tag"]))
        postings = self.catalogue.postings_for_token("bolt")
        self.assertEqual(len(postings), 1)
        p = postings[0]
        self.assertEqual((p.document_id, p.revision, p.field, p.segment, p.position),
                         ("D1", 1, "title", 0, 1))

    def test_segment_indices_are_independent(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber", "gear"], []))
        amber = self.catalogue.postings_for_token("amber")[0]
        gear = self.catalogue.postings_for_token("gear")[0]
        self.assertEqual((amber.segment, amber.position), (0, 0))
        self.assertEqual((gear.segment, gear.position), (1, 0))

    def test_original_text_is_retained(self):
        self.catalogue.add_revision(doc("D1", 1, ["Amber bolt gear"], ["stop blue tag"]))
        stored = self.catalogue.document("D1", 1)
        self.assertEqual(stored.title, ("Amber bolt gear",))
        self.assertEqual(stored.body, ("stop blue tag",))

    def test_correction_preserves_old_revision_and_postings(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber gear"], ["clean"]))
        self.catalogue.add_revision(doc("D1", 2, ["amber"], ["stop tag"]))
        self.assertEqual(self.catalogue.revisions("D1"), [1, 2])
        self.assertEqual(self.catalogue.current_revision("D1"), 2)
        # Old revision is still queryable through its postings.
        old = self.catalogue.postings_for_token("gear")
        self.assertEqual([(p.revision, p.field) for p in old], [(1, "title")])
        self.assertEqual(self.catalogue.document("D1", 1).title, ("amber gear",))

    def test_replay_is_idempotent(self):
        payload = doc("D1", 1, ["amber gear"], ["clean"])
        self.assertTrue(self.catalogue.add_revision(payload))
        self.assertFalse(self.catalogue.add_revision(payload))
        # No duplicated postings.
        self.assertEqual(len(self.catalogue.postings_for_token("amber")), 1)

    def test_replay_requires_same_segment_order(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber", "gear"], []))
        with self.assertRaises(RevisionConflict):
            self.catalogue.add_revision(doc("D1", 1, ["gear", "amber"], []))

    def test_replay_requires_same_text(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber gear"], []))
        with self.assertRaises(RevisionConflict):
            self.catalogue.add_revision(doc("D1", 1, ["amber"], []))

    def test_batch_rejection_is_atomic(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber gear"], []))
        with self.assertRaises(RevisionConflict):
            self.catalogue.add_revisions(
                [
                    doc("D2", 1, ["clean"], []),
                    doc("D1", 1, ["different text"], []),
                ]
            )
        # D2 must not have been stored by the rejected batch.
        self.assertEqual(self.catalogue.revisions("D2"), [])

    def test_snapshot_is_canonical_cut(self):
        self.catalogue.add_revision(doc("D1", 1, ["a"], []))
        self.catalogue.add_revision(doc("D2", 1, ["b"], []))
        self.catalogue.add_revision(doc("D1", 2, ["c"], []))
        self.assertEqual(self.catalogue.snapshot(), (("D1", 2), ("D2", 1)))

    def test_postings_can_be_restricted_to_cut(self):
        self.catalogue.add_revision(doc("D1", 1, ["amber gear"], []))
        self.catalogue.add_revision(doc("D1", 2, ["amber"], []))
        old = self.catalogue.postings_for_token("gear", documents=[("D1", 1)])
        self.assertEqual(len(old), 1)
        new = self.catalogue.postings_for_token("gear", documents=[("D1", 2)])
        self.assertEqual(new, [])

    def test_persisted_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "catalogue.sqlite")
            with Catalogue(path) as cat:
                cat.add_revision(doc("D1", 1, ["amber gear"], []))
            with Catalogue(path) as cat:
                self.assertEqual(cat.current_revision("D1"), 1)
                self.assertEqual(
                    [p.position for p in cat.postings_for_token("gear")], [1]
                )


if __name__ == "__main__":
    unittest.main()
