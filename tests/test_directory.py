"""Directory, conversion and revision tests (no database)."""
from __future__ import annotations

import json
from fractions import Fraction

import pytest

from qualitylib.directory import parse_directory, revise
from qualitylib.errors import (
    ConversionError,
    DisconnectedError,
    KindMismatch,
    RevisionError,
    UnknownUnit,
    ValidationError,
)

F = Fraction
MATERIALS = "materials/708"


@pytest.fixture
def r1():
    with open(f"{MATERIALS}/directory.json", encoding="utf-8") as handle:
        return parse_directory(json.load(handle))


def _rev(doc):
    return json.loads(json.dumps(doc))  # deep copy


def _edge(edge_id, src, dst, a, b):
    def ratio(pair):
        n, d = pair
        return {"n": n, "d": d}

    return {"edge_id": edge_id, "from": src, "to": dst, "a": ratio(a), "b": ratio(b)}


# ---------------------------------------------------------------------------
# Basic conversion / arithmetic
# ---------------------------------------------------------------------------
def test_absolute_and_difference_values(r1):
    assert r1.convert(F(50), "C", "F", "absolute") == F(122)
    assert r1.convert(F(10), "C", "F", "difference") == F(18)


def test_difference_ignores_offset(r1):
    # Same factor as absolute, but the +32 offset must not be applied.
    absolute = r1.conversion("C", "F", "absolute")
    difference = r1.conversion("C", "F", "difference")
    assert absolute.affine.a == difference.affine.a == F(9, 5)
    assert absolute.affine.b == F(32)
    assert difference.affine.b == F(0)


def test_reverse_conversion_is_exact(r1):
    forward = r1.conversion("C", "F", "absolute")
    reverse = r1.conversion("F", "C", "absolute")
    for value in (F(-40), F(0), F(50), F(212), F(37, 3)):
        assert reverse.apply(forward.apply(value)) == value
    # Explicit expected reverse: F=122 -> C=50
    assert reverse.apply(F(122)) == F(50)
    assert reverse.path[0].direction == "reverse"


def test_reverse_difference(r1):
    assert r1.convert(F(18), "F", "C", "difference") == F(10)


def test_kelvin_edges_consistent(r1):
    # K->C and K->F must agree with composing through C.
    kf = r1.conversion("K", "F", "absolute").affine
    via_c = r1.conversion("C", "F", "absolute").affine.compose_into(
        r1.conversion("K", "C", "absolute").affine
    )
    assert kf.a == via_c.a and kf.b == via_c.b
    assert r1.convert(F(27315, 100), "K", "C", "absolute") == F(0)
    assert r1.convert(F(27315, 100), "K", "F", "absolute") == F(32)


def test_no_float_tolerance_is_used(r1):
    # Exact equality, not "close enough": 1/3 stays 1/3.
    x = r1.convert(F(1, 3), "C", "F", "difference")
    assert x == F(3, 5)
    assert x.denominator == 5


# ---------------------------------------------------------------------------
# Category / quantity separation
# ---------------------------------------------------------------------------
def test_cannot_mix_categories(r1):
    # A difference result must never silently become an absolute value.
    diff = r1.convert(F(10), "C", "F", "difference")
    absolute_of_same_input = r1.convert(F(10), "C", "F", "absolute")
    assert diff != absolute_of_same_input


def test_disallowed_kind_rejected():
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["absolute"]}],
        "units": [{"unit_id": "a", "quantity_id": "T"},
                  {"unit_id": "b", "quantity_id": "T"}],
        "edges": [_edge("e", "a", "b", (2, 1), (0, 1))],
    }
    directory = parse_directory(doc)
    assert directory.convert(F(3), "a", "b", "absolute") == F(6)
    with pytest.raises(KindMismatch):
        directory.convert(F(3), "a", "b", "difference")


def test_cross_quantity_conversion_rejected():
    doc = {
        "revision": "x",
        "quantities": [
            {"quantity_id": "T", "allowed_kinds": ["absolute", "difference"]},
            {"quantity_id": "L", "allowed_kinds": ["absolute", "difference"]},
        ],
        "units": [{"unit_id": "c", "quantity_id": "T"},
                  {"unit_id": "m", "quantity_id": "L"}],
        "edges": [],
    }
    directory = parse_directory(doc)
    with pytest.raises(ConversionError):
        directory.conversion("c", "m", "absolute")


def test_unknown_unit(r1):
    with pytest.raises(UnknownUnit):
        r1.conversion("Z", "C", "absolute")


def test_disconnected_component_is_unconvertible(r1):
    # U exists in r1 as a TEMP unit but has no edges.
    assert not r1.is_connected("U", "C")
    with pytest.raises(DisconnectedError):
        r1.conversion("U", "C", "absolute")
    with pytest.raises(DisconnectedError):
        r1.conversion("C", "U", "absolute")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_path_lists_actual_edges(r1):
    conversion = r1.conversion("K", "F", "absolute")
    assert conversion.path, "a multi-hop conversion must expose its path"
    for step in conversion.path:
        assert step.edge_id in r1.edges
    # Chained endpoints connect source to target.
    assert conversion.path[0].from_unit == "K"
    assert conversion.path[-1].to_unit == "F"


def test_multiple_equivalent_paths_agree():
    # Two routes a->c: direct, and a->b->c. Both must reduce identically.
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["absolute", "difference"]}],
        "units": [{"unit_id": u, "quantity_id": "T"} for u in "abc"],
        "edges": [
            _edge("ab", "a", "b", (2, 1), (1, 1)),
            _edge("bc", "b", "c", (3, 1), (2, 1)),
            _edge("ac", "a", "c", (6, 1), (5, 1)),  # 3*(2x+1)+2 = 6x+5
        ],
    }
    directory = parse_directory(doc)
    direct = directory.conversion("a", "c", "absolute")
    via = directory.conversion("a", "b", "absolute")
    assert direct.affine == via.affine.compose_into(directory.conversion("b", "c", "absolute").affine)
    assert directory.convert(F(4), "a", "c", "absolute") == F(29)


def test_inconsistent_cycle_rejected():
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["absolute", "difference"]}],
        "units": [{"unit_id": u, "quantity_id": "T"} for u in "ab"],
        "edges": [
            _edge("ab", "a", "b", (2, 1), (0, 1)),
            _edge("ba", "b", "a", (2, 1), (0, 1)),  # should be 1/2
        ],
    }
    with pytest.raises(RevisionError):
        parse_directory(doc)


def test_inconsistent_cycle_in_difference_only():
    # Same scale but a spurious offset in a difference-only quantity: the
    # offset is irrelevant for differences, so the cycle is consistent.
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["difference"]}],
        "units": [{"unit_id": u, "quantity_id": "T"} for u in "ab"],
        "edges": [
            _edge("ab", "a", "b", (2, 1), (100, 1)),
            _edge("ba", "b", "a", (1, 2), (0, 1)),
        ],
    }
    directory = parse_directory(doc)
    assert directory.convert(F(3), "a", "b", "difference") == F(6)
    # But the absolute interpretation would be inconsistent, so it is disallowed.
    with pytest.raises(KindMismatch):
        directory.conversion("a", "b", "absolute")


# ---------------------------------------------------------------------------
# Revision semantics
# ---------------------------------------------------------------------------
def _load(name):
    with open(f"{MATERIALS}/{name}", encoding="utf-8") as handle:
        return json.load(handle)


def test_r2_adopts_final_consistent_graph(r1):
    r2 = revise(r1, _load("revision.json"))
    assert r2.revision == "r2"
    assert set(r2.edges) == {"E_CF", "E_KC", "E_KF2"}
    assert r2.convert(F(50), "C", "F", "absolute") == F(120)
    assert r2.convert(F(10), "C", "F", "difference") == F(18)
    # E_KF (old) is gone; K->F is still reachable (E_KF2 directly, or K->C->F)
    # and every route must agree with the new calibration.
    kf = r2.conversion("K", "F", "absolute")
    assert all(step.edge_id != "E_KF" for step in kf.path)
    # The whole r2 calibration shifted the absolute offset by -2 F.
    assert r2.convert(F(27315, 100), "K", "F", "absolute") == F(30)


def test_deleted_edge_does_not_participate(r1):
    # Deleting only E_CF leaves C<->F reachable via K; the deleted edge must not
    # appear in the chosen path and the value must match the remaining route.
    doc = {"revision": "r-del", "operations": [{"op": "delete", "edge_id": "E_CF"}]}
    revised = revise(r1, doc)
    assert "E_CF" not in revised.edges
    conversion = revised.conversion("C", "F", "absolute")
    assert all(step.edge_id != "E_CF" for step in conversion.path)
    via_k = r1.conversion("C", "K", "absolute").affine
    expected = r1.conversion("K", "F", "absolute").affine.compose_into(via_k)
    assert conversion.affine == expected

    # Removing both C->F routes truly disconnects C from F.
    doc2 = {
        "revision": "r-cut",
        "operations": [
            {"op": "delete", "edge_id": "E_CF"},
            {"op": "delete", "edge_id": "E_KF"},
        ],
    }
    cut = revise(r1, doc2)
    with pytest.raises(DisconnectedError):
        cut.conversion("C", "F", "absolute")


def test_transient_conflict_is_not_a_rejection(r1):
    # Order: replace E_CF, then delete E_KF. Mid-batch the graph is
    # inconsistent, but the final graph is consistent -> accepted.
    doc = {
        "revision": "r-order",
        "operations": [
            {"op": "upsert", "edge": _edge("E_CF", "C", "F", (9, 5), (30, 1))},
            {"op": "delete", "edge_id": "E_KF"},
            {"op": "upsert", "edge": _edge("E_KF2", "K", "F", (9, 5), (-46167, 100))},
        ],
    }
    revised = revise(r1, doc)
    assert revised.revision == "r-order"
    assert revised.convert(F(50), "C", "F", "absolute") == F(120)


def test_bad_batch_rejected_and_current_unchanged(r1):
    with pytest.raises(RevisionError):
        revise(r1, _load("invalid_revision.json"))
    # r1 object is untouched and still converts the old way.
    assert r1.revision == "r1"
    assert r1.convert(F(50), "C", "F", "absolute") == F(122)


def test_duplicate_edge_id_in_batch_rejected(r1):
    doc = {
        "revision": "dup",
        "operations": [
            {"op": "delete", "edge_id": "E_CF"},
            {"op": "upsert", "edge": _edge("E_CF", "C", "F", (9, 5), (32, 1))},
        ],
    }
    with pytest.raises(RevisionError):
        revise(r1, doc)


def test_delete_missing_edge_rejected(r1):
    doc = {"revision": "d", "operations": [{"op": "delete", "edge_id": "NOPE"}]}
    with pytest.raises(RevisionError):
        revise(r1, doc)


def test_replace_missing_edge_rejected(r1):
    doc = {
        "revision": "r",
        "operations": [
            {"op": "replace", "edge": _edge("NOPE", "C", "F", (1, 1), (0, 1))}
        ],
    }
    with pytest.raises(RevisionError):
        revise(r1, doc)


def test_negative_or_zero_scale_rejected():
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["absolute", "difference"]}],
        "units": [{"unit_id": u, "quantity_id": "T"} for u in "ab"],
        "edges": [_edge("ab", "a", "b", (0, 1), (0, 1))],
    }
    with pytest.raises(ValidationError):
        parse_directory(doc)


def test_noninteger_fraction_input_rejected():
    doc = {
        "revision": "x",
        "quantities": [{"quantity_id": "T", "allowed_kinds": ["absolute"]}],
        "units": [{"unit_id": u, "quantity_id": "T"} for u in "ab"],
        "edges": [{"edge_id": "e", "from": "a", "to": "b",
                   "a": {"n": 1.5, "d": 1}, "b": {"n": 0, "d": 1}}],
    }
    with pytest.raises(ValidationError):
        parse_directory(doc)


def test_replace_can_change_endpoints_and_coefficients(r1):
    # Re-point E_KC to K->F (identical calibration) and delete the duplicate
    # E_KF; the final graph is still consistent.
    doc = {
        "revision": "r-repoint",
        "operations": [
            {"op": "replace", "edge": _edge("E_KC", "K", "F", (9, 5), (-45967, 100))},
            {"op": "delete", "edge_id": "E_KF"},
        ],
    }
    revised = revise(r1, doc)
    assert revised.edges["E_KC"].from_unit == "K"
    assert revised.edges["E_KC"].to_unit == "F"
    assert "E_KF" not in revised.edges
    assert revised.convert(F(27315, 100), "K", "F", "absolute") == F(32)


def test_revision_label_reuse_rejected(r1):
    doc = {"revision": "r1", "operations": []}
    with pytest.raises(RevisionError):
        revise(r1, doc)


def test_directory_stands_alone(r1):
    # Validation + conversion need no catalog/samples at all.
    text = json.dumps(r1.to_json())
    reparsed = parse_directory(json.loads(text))
    assert reparsed.fingerprint == r1.fingerprint
    assert reparsed.convert(F(50), "C", "F", "absolute") == F(122)
