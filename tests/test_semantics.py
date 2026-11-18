from fractions import Fraction

import pytest

from obs_service.semantics import (
    FALSE,
    TRUE,
    UNCERTAIN,
    Range,
    RevConflict,
    SemanticsError,
    average_of_ranges,
    parse_observation,
    parse_rational,
    threshold_status,
)


# --------------------------------------------------------------------------
# Rational parsing (exact, no floats)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (5, Fraction(5)),
    ("5", Fraction(5)),
    ("10/3", Fraction(10, 3)),
    ("0", Fraction(0)),
    ("7/2", Fraction(7, 2)),
])
def test_parse_rational_exact(raw, expected):
    assert parse_rational(raw) == expected


@pytest.mark.parametrize("bad", [-1, "-3/2", 1.5, 0.1, True, "", "abc", "1/0"])
def test_parse_rational_rejects(bad):
    with pytest.raises(SemanticsError):
        parse_rational(bad)


# --------------------------------------------------------------------------
# Observation parsing -> canonical non-empty ranges
# --------------------------------------------------------------------------
def test_exact_singleton():
    obs = parse_observation({"kind": "EXACT", "value": "5"})
    assert obs.range == Range.singleton(Fraction(5))
    assert repr(obs.range) == "[5,5]"


def test_lt_lt0_le0():
    assert repr(parse_observation({"kind": "LT", "upper": "5"}).range) == "[0,5)"
    with pytest.raises(SemanticsError):
        parse_observation({"kind": "LT", "upper": "0"})  # LT(0) is empty
    obs = parse_observation({"kind": "LE", "upper": "0"})
    assert obs.range == Range.singleton(Fraction(0))  # LE(0) == {0}


def test_gt_ge_unbounded():
    assert repr(parse_observation({"kind": "GT", "lower": "5"}).range) == "(5,+inf)"
    assert repr(parse_observation({"kind": "GE", "lower": "5"}).range) == "[5,+inf)"


def test_between_open_endpoints():
    closed = parse_observation({"kind": "BETWEEN", "lower": "1", "upper": "2",
                                "lower_closed": True, "upper_closed": True})
    assert repr(closed.range) == "[1,2]"
    mixed = parse_observation({"kind": "BETWEEN", "lower": "1", "upper": "2",
                               "lower_closed": False, "upper_closed": False})
    assert repr(mixed.range) == "(1,2)"


def test_between_equal_endpoints_need_both_closed():
    ok = parse_observation({"kind": "BETWEEN", "lower": "2", "upper": "2",
                            "lower_closed": True, "upper_closed": True})
    assert ok.range == Range.singleton(Fraction(2))
    for lc, uc in [(True, False), (False, True), (False, False)]:
        with pytest.raises(SemanticsError):
            parse_observation({"kind": "BETWEEN", "lower": "2", "upper": "2",
                               "lower_closed": lc, "upper_closed": uc})


def test_between_inverted_rejected():
    with pytest.raises(SemanticsError):
        parse_observation({"kind": "BETWEEN", "lower": "3", "upper": "2"})


def test_missing_is_not_a_range():
    obs = parse_observation({"kind": "MISSING"})
    assert obs.is_missing and obs.range is None
    with pytest.raises(SemanticsError):
        parse_observation({"kind": "MISSING", "value": "0"})


# --------------------------------------------------------------------------
# Intersection (exact, continuous)
# --------------------------------------------------------------------------
def test_intersect_basic_and_empty():
    a = parse_observation({"kind": "LT", "upper": "5"}).range
    b = parse_observation({"kind": "GE", "lower": "4"}).range
    assert repr(a.intersect(b)) == "[4,5)"
    c = parse_observation({"kind": "GT", "lower": "5"}).range
    assert a.intersect(c) is None  # (5,inf) ∩ [0,5) = empty


def test_intersect_touching_open_ends_empty():
    a = Range.lt(Fraction(5))          # [0,5)
    b = Range.ge(Fraction(5))          # [5,inf)
    assert a.intersect(b) is None      # 5 excluded on the left
    c = Range.le(Fraction(5))          # [0,5]
    assert repr(c.intersect(b)) == "[5,5]"


# --------------------------------------------------------------------------
# Threshold predicate x > T
# --------------------------------------------------------------------------
def test_threshold_status_table():
    T = Fraction(5)
    assert threshold_status(Range.gt(T), T) == TRUE           # (5,inf)
    assert threshold_status(Range.ge(T), T) == UNCERTAIN      # [5,inf): x=5 fails
    assert threshold_status(Range.singleton(T), T) == FALSE   # {5}
    assert threshold_status(Range.lt(T), T) == FALSE          # [0,5)
    assert threshold_status(Range.le(T), T) == FALSE          # [0,5]
    assert threshold_status(Range.between(Fraction(4), Fraction(6), True, True), T) == UNCERTAIN
    assert threshold_status(Range.between(Fraction(6), Fraction(9), True, True), T) == TRUE
    # Upper open just above T still TRUE since all values eventually exceed T.
    assert threshold_status(Range.between(Fraction(5), Fraction(6), False, False), T) == TRUE


def test_threshold_with_fraction_threshold():
    r = parse_observation({"kind": "GT", "lower": "10/3"}).range
    assert threshold_status(r, Fraction(10, 3)) == TRUE
    assert threshold_status(r, Fraction(4)) == UNCERTAIN


# --------------------------------------------------------------------------
# Average of ranges
# --------------------------------------------------------------------------
def test_average_of_four_715_r1():
    ranges = [
        parse_observation({"kind": "LT", "upper": "5"}).range,
        parse_observation({"kind": "EXACT", "value": "5"}).range,
        parse_observation({"kind": "GT", "lower": "5"}).range,
    ]  # MISSING excluded from denominator
    avg = average_of_ranges(ranges)
    assert avg["n"] == 3
    assert avg["lo"] == Fraction(10, 3) and avg["hi"] is None
    assert avg["lo_closed"] is False          # s3's lower end is open
    assert avg["lower_attainable"] is False
    assert avg["upper_unbounded"] is True
    assert avg["upper_attainable"] is False


def test_average_715_r2():
    ranges = [
        parse_observation({"kind": "LT", "upper": "5"}).range,
        parse_observation({"kind": "EXACT", "value": "5"}).range,
        parse_observation({"kind": "EXACT", "value": "7"}).range,
    ]
    avg = average_of_ranges(ranges)
    assert avg["n"] == 3
    assert avg["lo"] == Fraction(4) and avg["lo_closed"] is True
    assert avg["hi"] == Fraction(17, 3) and avg["hi_closed"] is False
    assert avg["lower_attainable"] is True
    assert avg["upper_attainable"] is False
    assert avg["hi"] != avg["lo"]


def test_average_undefined_and_singleton():
    assert average_of_ranges([]) is None
    avg = average_of_ranges([Range.singleton(Fraction(3)),
                             Range.singleton(Fraction(5))])
    assert avg["lo"] == avg["hi"] == Fraction(4)
    assert avg["lower_attainable"] and avg["upper_attainable"]


def test_average_lower_zero_closed_when_all_lower_unbounded():
    # LT samples have lo None (0) closed; LE(0) is {0} closed.
    avg = average_of_ranges([Range.lt(Fraction(5)), Range.le(Fraction(0))])
    assert avg["lo"] == Fraction(0) and avg["lo_closed"] is True
    assert avg["lower_attainable"] is True


# --------------------------------------------------------------------------
# RevConflict type
# --------------------------------------------------------------------------
def test_rev_conflict_is_semantics_error():
    assert issubclass(RevConflict, SemanticsError)
