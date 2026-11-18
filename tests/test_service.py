import json
import os
from fractions import Fraction

import pytest

from obs_service.api import ObservationService
from obs_service.semantics import RevConflict, SemanticsError

HERE = os.path.dirname(os.path.abspath(__file__))
MATERIALS = os.path.join(os.path.dirname(HERE), "materials", "715")


def load(name):
    with open(os.path.join(MATERIALS, name), encoding="utf-8") as f:
        return json.load(f)


def fresh():
    svc = ObservationService(":memory:")
    svc.import_dataset(load("observations.json"))
    return svc


# --------------------------------------------------------------------------
# 715 reference scenario
# --------------------------------------------------------------------------
def test_reports_match_brief():
    svc = fresh()
    r1 = svc.create_report("r1", "b1", "p", 1)
    assert r1["counts"] == {
        "total_samples": 4, "valid_samples": 3, "definitely_exceeds": 1,
        "possibly_exceeds": 1, "uncertain": 0, "missing": 1,
    }
    a1 = r1["average"]
    assert (a1["lower"], a1["upper"], a1["upper_unbounded"]) == ("10/3", None, True)
    assert a1["range_display"] == "(10/3,+inf)"
    assert a1["lower_attainable"] is False and a1["upper_attainable"] is False
    assert a1["unique"] is False

    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "7"}}])
    svc.register_threshold("p", 2, "6")
    r2 = svc.create_report("r2", "b1", "p", 2)
    assert r2["counts"]["definitely_exceeds"] == 1
    assert r2["counts"]["possibly_exceeds"] == 1
    assert r2["counts"]["missing"] == 1
    a2 = r2["average"]
    assert a2["range_display"] == "[4,17/3)"
    assert a2["lower_attainable"] is True and a2["upper_attainable"] is False
    assert a2["unique"] is False

    # Old report unchanged and bound to its own threshold.
    r1_again = svc.read_report("r1")
    assert r1_again["average"]["range_display"] == "(10/3,+inf)"
    assert r1_again["threshold"] == {"revision": 1, "value": "5"}
    assert r1_again["superseded"] is True
    # s4 is still MISSING in both.
    for rep in (r1_again, r2):
        s4 = [c for c in rep["contributions"] if c["sample_id"] == "s4"][0]
        assert s4["is_missing"] and s4["denominator"] is None


def test_illegal_batch_rejected_atomically():
    svc = fresh()
    with pytest.raises(SemanticsError):
        svc.import_measurements("b1", "p", [
            {"sample_id": "s1", "project": "p", "revision": 2,
             "observation": {"kind": "LT", "upper": "0"}},   # illegal/empty
            {"sample_id": "s2", "project": "p", "revision": 2,
             "observation": {"kind": "EXACT", "value": "1"}},  # otherwise valid
        ])
    # s2 must NOT be partially updated.
    hist = svc.revision_history("b1", "p", "s2")
    assert [h["revision"] for h in hist] == [1]
    assert hist[0]["observation"] == {"kind": "EXACT", "value": "5"}
    # A report still consumes s2 rev1.
    r = svc.create_report("rcheck", "b1", "p", 1)
    s2 = [c for c in r["contributions"] if c["sample_id"] == "s2"][0]
    assert s2["adopted_revision"] == 1


# --------------------------------------------------------------------------
# Replay / conflict / adoption semantics
# --------------------------------------------------------------------------
def test_replay_vs_conflict():
    svc = fresh()
    replay = svc.import_measurements("b1", "p", [
        {"sample_id": "s1", "project": "p", "revision": 1,
         "observation": {"kind": "LT", "upper": "5"}}])
    assert replay == {"batch_id": "b1", "project": "p", "inserted": 0, "replayed": 1}
    # Same kind but different literal spelling -> same semantics -> replay.
    replay2 = svc.import_measurements("b1", "p", [
        {"sample_id": "s1", "project": "p", "revision": 1,
         "observation": {"kind": "LT", "upper": "10/2"}}])
    assert replay2["replayed"] == 1
    # Different semantics -> conflict.
    with pytest.raises(RevConflict):
        svc.import_measurements("b1", "p", [
            {"sample_id": "s1", "project": "p", "revision": 1,
             "observation": {"kind": "LT", "upper": "4"}}])


def test_missing_differs_from_any_range():
    svc = fresh()
    with pytest.raises(RevConflict):
        svc.import_measurements("b1", "p", [
            {"sample_id": "s4", "project": "p", "revision": 1,
             "observation": {"kind": "EXACT", "value": "0"}}])


def test_lower_revision_ignored_higher_adopted():
    svc = fresh()
    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "7"}}])
    # Register an older (revision 1) still valid record - already present via dataset.
    # Higher revision 3 wins:
    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 3,
         "observation": {"kind": "EXACT", "value": "9"}}])
    r = svc.create_report("r", "b1", "p", 1)
    s3 = [c for c in r["contributions"] if c["sample_id"] == "s3"][0]
    assert s3["adopted_revision"] == 3
    hist = svc.revision_history("b1", "p", "s3")
    assert [h["revision"] for h in hist] == [1, 2, 3]


def test_correction_to_missing_keeps_history():
    svc = fresh()
    svc.import_measurements("b1", "p", [
        {"sample_id": "s2", "project": "p", "revision": 2,
         "observation": {"kind": "MISSING"}}])
    r = svc.create_report("r", "b1", "p", 1)
    counts = r["counts"]
    assert counts["valid_samples"] == 2 and counts["missing"] == 2
    s2 = [c for c in r["contributions"] if c["sample_id"] == "s2"][0]
    assert s2["is_missing"] and s2["adopted_revision"] == 2
    assert [h["revision"] for h in svc.revision_history("b1", "p", "s2")] == [1, 2]


def test_threshold_conflict_and_replay():
    svc = fresh()
    assert svc.register_threshold("p", 1, "5")["replayed"] is True
    assert svc.register_threshold("p", 1, "10/2")["replayed"] is True
    with pytest.raises(RevConflict):
        svc.register_threshold("p", 1, "4")


# --------------------------------------------------------------------------
# Immutability / snapshot isolation
# --------------------------------------------------------------------------
def test_report_immutable_after_new_imports_and_thresholds():
    svc = fresh()
    r1 = svc.create_report("r1", "b1", "p", 1)
    snapshot = json.dumps(r1, sort_keys=True)
    # Change measurements and threshold.
    svc.import_measurements("b1", "p", [
        {"sample_id": "s1", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "100"}}])
    svc.register_threshold("p", 2, "1000")
    r1_again = svc.read_report("r1")
    assert json.dumps(r1_again, sort_keys=True) == snapshot
    with pytest.raises(SemanticsError):
        svc.create_report("r1", "b1", "p", 1)  # cannot overwrite


def test_snapshot_survives_reopen(tmp_path):
    db = str(tmp_path / "obs.db")
    svc = ObservationService(db)
    svc.import_dataset(load("observations.json"))
    svc.create_report("r1", "b1", "p", 1)
    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "7"}}])
    svc.register_threshold("p", 2, "6")
    svc.create_report("r2", "b1", "p", 2)
    svc.close()

    svc2 = ObservationService(db)
    r1 = svc2.read_report("r1")
    r2 = svc2.read_report("r2")
    assert r1["average"]["range_display"] == "(10/3,+inf)"
    assert r1["threshold"]["value"] == "5"
    assert r2["average"]["range_display"] == "[4,17/3)"
    assert r2["threshold"]["value"] == "6"
    svc2.close()


def test_report_binds_threshold_revision_not_current():
    svc = fresh()
    r1 = svc.create_report("r1", "b1", "p", 1)
    # Threshold rev 2 = 4 would make EXACT(5) still definitely exceed but
    # s1 [0,5) would now be *possibly* exceeding (values in (4,5)).
    svc.register_threshold("p", 2, "4")
    assert r1["counts"]["uncertain"] == 0
    r2 = svc.create_report("r2", "b1", "p", 2)
    assert r2["counts"]["uncertain"] == 1   # s1 is [0,5) vs T=4
    assert r2["counts"]["definitely_exceeds"] == 2   # s2 EXACT(5), s3 (5,inf)
    assert r2["counts"]["possibly_exceeds"] == 3


# --------------------------------------------------------------------------
# Counts classification edge cases
# --------------------------------------------------------------------------
def test_possibly_includes_definite_and_missing_excluded():
    svc = fresh()
    r = svc.create_report("r", "b1", "p", 1)
    c = r["counts"]
    assert c["possibly_exceeds"] == c["definitely_exceeds"] + c["uncertain"]
    assert c["valid_samples"] + c["missing"] == c["total_samples"]


def test_undefined_average_when_all_missing():
    svc = ObservationService(":memory:")
    svc.import_dataset({
        "batch_id": "bz", "threshold": {"project": "p", "revision": 1, "value": "5"},
        "measurements": [
            {"sample_id": "s1", "project": "p", "revision": 1,
             "observation": {"kind": "MISSING"}},
        ]})
    r = svc.create_report("rz", "bz", "p", 1)
    assert r["average"]["defined"] is False
    assert r["counts"]["valid_samples"] == 0
    assert r["counts"]["missing"] == 1


def test_le0_singleton_not_missing():
    svc = ObservationService(":memory:")
    svc.import_dataset({
        "batch_id": "b", "threshold": {"project": "p", "revision": 1, "value": "0"},
        "measurements": [
            {"sample_id": "s1", "project": "p", "revision": 1,
             "observation": {"kind": "LE", "upper": "0"}},
        ]})
    r = svc.create_report("r", "b", "p", 1)
    assert r["counts"]["valid_samples"] == 1
    assert r["counts"]["definitely_exceeds"] == 0   # {0} > 0 is false
    assert r["average"]["range_display"] == "[0,0]"  # singleton 0, not undefined


# --------------------------------------------------------------------------
# Contribution traceability / explanation
# --------------------------------------------------------------------------
def test_contributions_explain_average():
    svc = fresh()
    r1 = svc.create_report("r1", "b1", "p", 1)
    ex = svc.explain("r1")
    assert ex["derivation"]["denominator_n"] == 3
    assert ex["derivation"]["lower_sum"] == "10"
    assert ex["derivation"]["upper_sum"] == "+inf"
    assert ex["derivation"]["lower_attainable"] is False
    # Each valid sample records its range and contribution.
    by_id = {c["sample_id"]: c for c in r1["contributions"]}
    assert by_id["s1"]["range_display"] == "[0,5)"
    assert by_id["s2"]["range_display"] == "[5,5]"
    assert by_id["s3"]["range_display"] == "(5,+inf)"
    assert by_id["s3"]["status"] == "true"
    assert by_id["s4"]["is_missing"]

    # After correction, r2's contribution uses rev 2 and EXACT(7).
    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "7"}}])
    svc.register_threshold("p", 2, "6")
    r2 = svc.create_report("r2", "b1", "p", 2)
    s3 = [c for c in r2["contributions"] if c["sample_id"] == "s3"][0]
    assert s3["adopted_revision"] == 2
    assert s3["range_display"] == "[7,7]"
    ex2 = svc.explain("r2")
    assert ex2["derivation"]["lower_sum"] == "12"
    assert ex2["derivation"]["upper_sum"] == "17"
    assert ex2["derivation"]["lower_attainable"] is True
    # r1's explanation still shows the old bound.
    assert svc.explain("r1")["derivation"]["lower_sum"] == "10"


def test_explain_sample_and_missing():
    svc = fresh()
    svc.create_report("r1", "b1", "p", 1)
    sample = svc.explain("r1", "s4")["sample"]
    assert sample["is_missing"] is True
    with pytest.raises(KeyError):
        svc.explain("r1", "nope")


# --------------------------------------------------------------------------
# Semantic service entry points
# --------------------------------------------------------------------------
def test_service_normalize_intersect_check():
    svc = ObservationService(":memory:")
    norm = svc.normalize({"kind": "LT", "upper": "10/3"})
    assert norm["range_display"] == "[0,10/3)"
    inter = svc.intersect({"kind": "GT", "lower": "5"},
                          {"kind": "LT", "upper": "6"})
    assert inter["range_display"] == "(5,6)"
    empty = svc.intersect({"kind": "LT", "upper": "5"},
                          {"kind": "GT", "lower": "5"})
    assert empty["empty"] is True
    chk = svc.threshold_check({"kind": "GE", "lower": "5"}, "5")
    assert chk["status"] == "uncertain"
    missing = svc.threshold_check({"kind": "MISSING"}, "5")
    assert missing["status"] == "undefined"


# --------------------------------------------------------------------------
# Scale: 1000 samples, exact, no statistical shortcuts
# --------------------------------------------------------------------------
def test_1000_samples_exact():
    svc = ObservationService(":memory:")
    svc.register_threshold("p", 1, "5")
    recs = []
    expected_lower_num = 0
    for i in range(1000):
        # alternate LT(5) [0,5) and EXACT(5) [5,5]; all non-missing
        if i % 2 == 0:
            recs.append({"sample_id": f"s{i}", "project": "p", "revision": 1,
                         "observation": {"kind": "LT", "upper": "5"}})
            expected_lower_num += 0
        else:
            recs.append({"sample_id": f"s{i}", "project": "p", "revision": 1,
                         "observation": {"kind": "EXACT", "value": "5"}})
            expected_lower_num += 5
    svc.import_measurements("b", "p", recs)
    r = svc.create_report("r", "b", "p", 1)
    assert r["counts"]["valid_samples"] == 1000
    # lower = 500*5 / 1000 = 5/2 ; upper = (500*5 + 500*5)/1000 = 5 but open.
    a = r["average"]
    assert Fraction(a["lower"]) == Fraction(5, 2)
    assert Fraction(a["upper"]) == Fraction(5)
    assert a["lower_attainable"] is True   # all lower ends closed
    assert a["upper_attainable"] is False  # LT upper ends open
    # Contributions stored for all samples.
    assert len(r["contributions"]) == 1000
