"""Test suite for the gradaudit library.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    RevisionConflictError,
    Store,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
    solve,
    verify_plan,
    verify_plan_document,
)
from gradaudit.errors import ConfirmationStaleError, NotPreparedError

MAT = os.path.join(ROOT, "materials", "725")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def cat1():
    return parse_catalog(load("catalog.json"))


def cat2():
    return parse_catalog(load("catalog_revision2.json"))


def recs():
    return parse_records(load("records.json"))


def reqs():
    return parse_requirements(load("requirements.json"))


# =========================================================================== #
# parsing / catalog
# =========================================================================== #
class TestCatalog(unittest.TestCase):
    def test_candidates_are_explicit_not_guessed(self):
        c = cat1()
        self.assertEqual(c.candidates("X"), ("LC_CORE", "LC_OPT"))
        self.assertEqual(c.candidates("Z"), ("LC_BOTH",))
        # an external course absent from equivalences has no candidates
        self.assertEqual(c.candidates("NOT_IN_CATALOG"), ())
        # a bare suffix in the name does not conjure a candidate
        self.assertEqual(c.candidates("LC_CORE"), ())

    def test_candidate_categories_listed(self):
        c = cat1()
        out = c.candidates_with_categories("Z")
        self.assertEqual(out, [{"course_id": "LC_BOTH", "categories": ["CORE", "OPT"]}])

    def test_records_parse_rejects_bad_points(self):
        from gradaudit.errors import SchemaError

        bad = copy.deepcopy(load("records.json"))
        bad["records"][0]["points"] = 0
        with self.assertRaises(SchemaError):
            parse_records(bad)
        bad["records"][0]["points"] = True
        with self.assertRaises(SchemaError):
            parse_records(bad)

    def test_requirements_reject_too_many_groups(self):
        from gradaudit.errors import SchemaError

        bad = copy.deepcopy(load("requirements.json"))
        bad["groups"] = bad["groups"] * 2  # 4 groups
        with self.assertRaises(SchemaError):
            parse_requirements(bad)


# =========================================================================== #
# independent verification (the public contract)
# =========================================================================== #
class TestVerify(unittest.TestCase):
    def test_plan_a_and_b_both_accepted(self):
        for name in ("plan_a.json", "plan_b.json"):
            with self.subTest(plan=name):
                result = verify_plan_document(load(name), cat1(), recs(), reqs())
                self.assertTrue(result.accepted, result.reasons)
                self.assertEqual(result.group_totals, {"G_CORE": 3, "G_OPT": 3})
                self.assertEqual(set(result.used_records), {"R1", "R2"})

    def test_invalid_reuse_rejected_and_points_at_r1(self):
        result = verify_plan_document(load("plan_invalid_reuse.json"), cat1(), recs(), reqs())
        self.assertFalse(result.accepted)
        self.assertTrue(any(r.get("location") == "R1" for r in result.reasons), result.reasons)

    def test_unknown_record(self):
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_CORE"},
                {"record_id": "GHOST", "course_id": "LC_OPT", "group_id": "G_OPT"},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("unknown_record", {r["code"] for r in res.reasons})

    def test_non_candidate_course_rejected(self):
        # R1 (external X) cannot map to LC_BOTH (only Z can).
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_BOTH", "group_id": "G_CORE"},
                {"record_id": "R2", "course_id": "LC_OPT", "group_id": "G_OPT"},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("course_not_candidate", {r["code"] for r in res.reasons})

    def test_category_mismatch_rejected(self):
        # R1 -> LC_CORE assigned to the OPT group: course is a candidate but
        # its categories do not hit G_OPT.
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_OPT"},
                {"record_id": "R2", "course_id": "LC_CORE", "group_id": "G_CORE"},
                {"record_id": "R3", "course_id": "LC_BOTH", "group_id": "G_OPT"},
            ],
        }
        # G_CORE=3 ok; G_OPT gets only R3 (2) because R1 miscategorised
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        codes = {r["code"] for r in res.reasons}
        self.assertIn("category_mismatch", codes)
        self.assertIn("group_underfilled", codes)

    def test_points_read_from_records_not_plan(self):
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            # A roguish extra points field must be ignored.
            "assignments": [
                {"record_id": "R3", "course_id": "LC_BOTH", "group_id": "G_OPT", "points": 99},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)  # R3 is only 2 points -> underfilled
        self.assertEqual(res.group_totals["G_OPT"], 2)
        self.assertTrue(any(r.get("location") == "G_OPT" for r in res.reasons))

    def test_unused_records_are_fine(self):
        # plan_a leaves R3 unused and is accepted.
        res = verify_plan_document(load("plan_a.json"), cat1(), recs(), reqs())
        self.assertNotIn("R3", res.used_records)

    def test_extra_plan_fields_ignored(self):
        plan = load("plan_a.json")
        plan["comment"] = "extra fields are allowed"
        plan["assignments"][0]["note"] = "still fine"
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertTrue(res.accepted, res.reasons)

    def test_revision_mismatch(self):
        res = verify_plan_document(load("plan_a.json"), cat2(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("revision_mismatch", {r["code"] for r in res.reasons})

    def test_any_contract_satisfying_plan_accepted_not_just_solver_output(self):
        # plan_b differs from the solver's choice but must still be accepted.
        solver_plan = solve(cat1(), recs(), reqs()).as_plan_document()
        plan_b = load("plan_b.json")
        self.assertNotEqual(solver_plan["assignments"], plan_b["assignments"])
        self.assertTrue(verify_plan_document(plan_b, cat1(), recs(), reqs()).accepted)


# =========================================================================== #
# solver
# =========================================================================== #
class TestSolver(unittest.TestCase):
    def test_rev1_feasible(self):
        result = solve(cat1(), recs(), reqs())
        self.assertTrue(result.feasible)
        self.assertEqual(result.group_totals, {"G_CORE": 3, "G_OPT": 3})
        # the emitted plan must pass the independent verifier
        self.assertTrue(verify_plan_document(result.as_plan_document(), cat1(), recs(), reqs()).accepted)

    def test_rev2_infeasible_with_group_maxima(self):
        result = solve(cat2(), recs(), reqs())
        self.assertFalse(result.feasible)
        maxima = {g.group_id: g.max_ignoring_other_groups for g in result.groups}
        self.assertEqual(maxima["G_OPT"], 2)
        self.assertEqual(maxima["G_CORE"], 8)

    def test_single_group_reachable_but_overall_not(self):
        # One record, 3 points, usable in either group; both groups need 3.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "C1", "categories": ["A"]},
                {"course_id": "C2", "categories": ["B"]},
                {"course_id": "CB", "categories": ["A", "B"]},
            ],
            "equivalences": {"E": ["CB"]},
        }
        records = {"student_id": "S", "revision": 1,
                   "records": [{"record_id": "R1", "external_course": "E", "points": 3}]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 3},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 3},
        ]}
        res = solve(parse_catalog(catalog), parse_records(records), parse_requirements(requirements))
        self.assertFalse(res.feasible)
        for g in res.groups:
            self.assertEqual(g.max_ignoring_other_groups, 3)
            self.assertTrue(g.satisfied)  # each alone is enough
        self.assertIn("does not make the request feasible", res.note)

    def test_multi_candidate_and_unused(self):
        # Two groups, several candidates; solver must pick an exact allocation
        # and may leave records unused.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "A1", "categories": ["A"]},
                {"course_id": "A2", "categories": ["A"]},
                {"course_id": "B1", "categories": ["B"]},
                {"course_id": "AB", "categories": ["A", "B"]},
            ],
            "equivalences": {"EA": ["A1", "A2", "AB"], "EB": ["B1", "AB"], "EX": ["A1"]},
        }
        records = {"student_id": "S", "revision": 1, "records": [
            {"record_id": "R1", "external_course": "EA", "points": 2},
            {"record_id": "R2", "external_course": "EB", "points": 2},
            {"record_id": "R3", "external_course": "EA", "points": 2},
            {"record_id": "R4", "external_course": "EX", "points": 2},
        ]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 4},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 2},
        ]}
        c, r, q = parse_catalog(catalog), parse_records(records), parse_requirements(requirements)
        res = solve(c, r, q)
        self.assertTrue(res.feasible)
        self.assertTrue(verify_plan_document(res.as_plan_document(), c, r, q).accepted)
        self.assertEqual(res.group_totals["GA"], 4)
        self.assertEqual(res.group_totals["GB"], 2)
        # R4 can only ever feed GA; it is used here. But no record may repeat.
        used = [a.record_id for a in res.assignments]
        self.assertEqual(len(used), len(set(used)))

    def test_exact_infeasibility_due_to_shared_record(self):
        # Same as above but GB minimum cannot coexist: only CB is shared and
        # each group needs a *separate* record.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "CA", "categories": ["A"]},
                {"course_id": "CB_", "categories": ["B"]},
                {"course_id": "CAB", "categories": ["A", "B"]},
            ],
            "equivalences": {"SHARED": ["CAB"]},
        }
        records = {"student_id": "S", "revision": 1,
                   "records": [{"record_id": "R1", "external_course": "SHARED", "points": 3}]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 3},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 3},
        ]}
        res = solve(parse_catalog(catalog), parse_records(records), parse_requirements(requirements))
        self.assertFalse(res.feasible)


# =========================================================================== #
# SQLite library: revisions, conflict, prepare/confirm/reopen
# =========================================================================== #
class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _import_all(self):
        self.store.import_catalog(load("catalog.json"))
        self.store.import_records(load("records.json"))
        self.store.import_requirements(load("requirements.json"))

    def test_same_revision_different_content_rejected(self):
        self.store.import_catalog(load("catalog.json"))
        # revision 1 again but changed content
        changed = copy.deepcopy(load("catalog.json"))
        changed["equivalences"]["X"] = ["LC_CORE"]
        with self.assertRaises(RevisionConflictError):
            self.store.import_catalog(changed)
        # identical re-import is idempotent
        self.store.import_catalog(load("catalog.json"))

    def test_old_revision_remains_queryable(self):
        self.store.import_catalog(load("catalog.json"))
        self.store.import_catalog(load("catalog_revision2.json"))
        self.assertEqual(self.store.list_candidates(1, "X"), [
            {"course_id": "LC_CORE", "categories": ["CORE"]},
            {"course_id": "LC_OPT", "categories": ["OPT"]},
        ])
        self.assertEqual(self.store.list_candidates(2, "X"), [
            {"course_id": "LC_CORE", "categories": ["CORE"]},
        ])

    def test_prepare_confirm_status_flow(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertTrue(prep["accepted"])
        pts = self.store.preparation_points(prep["preparation_id"])
        self.assertEqual(
            {(p["record_id"], p["course_id"], p["group_id"], p["points"]) for p in pts},
            {("R1", "LC_CORE", "G_CORE", 3), ("R2", "LC_OPT", "G_OPT", 3)},
        )
        conf = self.store.confirm_audit(request_id="REQ1")
        self.assertIn("confirmation_id", conf)
        status = self.store.confirmation_status("REQ1")
        self.assertTrue(status["confirmed"])
        self.assertIsNone(status["stale"])

    def test_rejected_preparation_cannot_be_confirmed(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_invalid_reuse.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertFalse(prep["accepted"])
        from gradaudit.errors import ConfirmationError
        with self.assertRaises(ConfirmationError):
            self.store.confirm_audit(request_id="REQ1")

    def test_catalog_change_makes_confirmation_stale_and_blocks_reconfirm(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        conf = self.store.confirm_audit(request_id="REQ1")
        # Mapping revision to rev2, re-registered against REQ1.
        self.store.import_catalog(load("catalog_revision2.json"))
        self.store.register_request(
            request_id="REQ1", catalog_revision=2,
            records_revision=1, requirements_revision=1,
        )
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIsNotNone(status["stale"])
        # old confirmation must not be re-confirmable against new revisions
        with self.assertRaises(ConfirmationStaleError):
            self.store.confirm_audit(request_id="REQ1", preparation_id=prep["preparation_id"])
        # but history keeps the original confirmation intact
        confs = self.store.list_confirmations("REQ1")
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0]["confirmation_id"], conf["confirmation_id"])

    def test_mapping_revision_really_changes_next_feasibility(self):
        self._import_all()
        self.store.import_catalog(load("catalog_revision2.json"))
        self.store.register_request(
            request_id="REQ1", catalog_revision=1,
            records_revision=1, requirements_revision=1,
        )
        # rev1 can satisfy both groups
        from gradaudit.solver import solve as _solve
        r1 = _solve(self.store.get_catalog(1), self.store.get_records(1), self.store.get_requirements(1))
        self.assertTrue(r1.feasible)
        # rev2 cannot
        r2 = _solve(self.store.get_catalog(2), self.store.get_records(1), self.store.get_requirements(1))
        self.assertFalse(r2.feasible)

    def test_reopen_keeps_history(self):
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        res = self.store.reopen_audit(request_id="REQ1", reason="student appealed")
        self.assertTrue(res["reopened"])
        self.assertFalse(self.store.confirmation_status("REQ1")["confirmed"])
        # history retains the confirmation and events
        hist = self.store.history("REQ1")
        kinds = [h["kind"] for h in hist]
        self.assertIn("confirmed", kinds)
        self.assertIn("reopened", kinds)
        self.assertEqual(len(self.store.list_confirmations("REQ1")), 1)

    def test_external_plan_uses_same_identity_and_confirmation_boundary(self):
        # A plan with a wrong records_revision for the *current* preparation
        # is rejected at prepare time by the independent verifier.
        self._import_all()
        plan = load("plan_b.json")
        plan["records_revision"] = 2  # not the supplied records rev
        prep = self.store.prepare_audit(
            plan, request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertFalse(prep["accepted"])

    def test_prepare_rejects_mismatched_request_id(self):
        self._import_all()
        with self.assertRaises(Exception):
            self.store.prepare_audit(
                load("plan_a.json"), request_id="OTHER",
                catalog_revision=1, records_revision=1, requirements_revision=1,
            )

    def test_importing_newer_revision_alone_stales_confirmation(self):
        # No explicit register_request: merely importing a newer catalog
        # revision must invalidate the previously confirmed audit.
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        self.assertTrue(self.store.confirmation_status("REQ1")["confirmed"])
        self.store.import_catalog(load("catalog_revision2.json"))
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIn("catalog_revision", status["stale"])

    def test_importing_newer_records_alone_stales_confirmation(self):
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        newer = copy.deepcopy(load("records.json"))
        newer["revision"] = 2
        newer["records"][2]["points"] = 4  # R3 changed
        self.store.import_records(newer)
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIn("records_revision", status["stale"])


class TestSolverExactness(unittest.TestCase):
    """Brute-force cross-check that the DP's feasible/infeasible answers agree
    with exhaustive enumeration on many small random instances."""

    def _brute_force(self, catalog, records, requirements):
        import itertools

        rec_list = sorted(records.records.values(), key=lambda r: r.record_id)
        group_list = list(requirements.groups)
        # per record: list of (None) skip or (course, group_index) options
        per_record_options = []
        for rec in rec_list:
            opts = [None]
            cands = catalog.candidates(rec.external_course)
            for g_idx, g in enumerate(group_list):
                for cid in cands:
                    if set(catalog.courses[cid].categories) & set(g.allowed_categories):
                        opts.append((cid, g_idx))
            per_record_options.append(opts)
        for combo in itertools.product(*per_record_options):
            totals = [0] * len(group_list)
            for rec, choice in zip(rec_list, combo):
                if choice is None:
                    continue
                _, g_idx = choice
                totals[g_idx] += rec.points
            if all(totals[g] >= group_list[g].minimum_points for g in range(len(group_list))):
                return True
        return False

    def test_random_small_instances(self):
        import random

        rng = random.Random(20240101)
        for trial in range(200):
            n_courses = rng.randint(2, 4)
            cats = ["A", "B", "C"]
            courses = []
            for i in range(n_courses):
                chosen = rng.sample(cats, rng.randint(1, 2))
                courses.append({"course_id": f"C{i}", "categories": chosen})
            course_ids = [c["course_id"] for c in courses]
            ext_pool = ["E0", "E1", "E2", "E3"]
            equivalences = {}
            for ext in ext_pool:
                k = rng.randint(1, min(3, n_courses))
                equivalences[ext] = rng.sample(course_ids, k)
            catalog_doc = {"format": "course-catalog/1", "revision": 1,
                           "courses": courses, "equivalences": equivalences}
            n_records = rng.randint(0, 5)
            recs_doc = {"student_id": "S", "revision": 1, "records": [
                {"record_id": f"R{i}", "external_course": rng.choice(ext_pool),
                 "points": rng.randint(1, 6)}
                for i in range(n_records)
            ]}
            n_groups = rng.randint(1, 3)
            groups = []
            for gi in range(n_groups):
                groups.append({
                    "group_id": f"G{gi}",
                    "allowed_categories": rng.sample(cats, rng.randint(1, 2)),
                    "minimum_points": rng.randint(1, 6),
                })
            req_doc = {"request_id": "Q", "revision": 1, "groups": groups}

            c = parse_catalog(catalog_doc)
            r = parse_records(recs_doc)
            q = parse_requirements(req_doc)
            got = solve(c, r, q)
            expected = self._brute_force(c, r, q)
            with self.subTest(trial=trial):
                self.assertEqual(got.feasible, expected)
                if got.feasible:
                    # every solver answer must itself verify
                    self.assertTrue(
                        verify_plan_document(got.as_plan_document(), c, r, q).accepted
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
