"""Finite-grid sanity checks for the continuous-range results.

IMPORTANT: The brief is explicit that a finite enumeration can only *falsify*,
never prove, a claim about continuous ranges.  These tests therefore only
sample representative points inside each stored range and check they respect
the already-derived exact bound/attainability; they do not derive the bound.

For genuinely continuous questions (e.g. "is the infimum attained?") the
authoritative check is the exact-arithmetic unit tests in test_semantics.py.
"""

from fractions import Fraction

from obs_service.api import ObservationService


def sample_points(rng_dict, base=Fraction(1, 97), count=19):
    """Yield representative interior points of a stored range (non-exhaustive)."""
    lo = Fraction(rng_dict["lo"]) if rng_dict["lo"] is not None else Fraction(0)
    hi = Fraction(rng_dict["hi"]) if rng_dict["hi"] is not None else None
    # Include an included lower endpoint so boundary attainability is testable.
    if rng_dict["lo_closed"]:
        yield lo
    if hi is None:
        for k in range(1, count + 1):
            yield lo + k * base
    else:
        if hi == lo:
            yield lo
            return
        span = hi - lo
        for k in range(1, count + 1):
            yield lo + span * Fraction(k, count + 1)


def test_r1_points_within_upper_unbounded():
    svc = ObservationService(":memory:")
    from obs_service.demo import _load
    svc.import_dataset(_load("observations.json"))
    r1 = svc.create_report("r1", "b1", "p", 1)
    avg = r1["average"]
    lo = Fraction(avg["lower"])
    # Search for a counterexample to the *lower* bound: no mean below 10/3.
    # Because the mean is a product box, the minimum over all choices is the
    # lower-sum/n; any finite sample should therefore be >= lo.
    ranges = []
    for c in r1["contributions"]:
        if not c["is_missing"]:
            ranges.append(c["range"])
    import itertools
    choices = [list(sample_points(r)) for r in ranges]
    for combo in itertools.product(*choices):
        mean = sum(combo, Fraction(0)) / len(combo)
        assert mean >= lo, (combo, mean)   # falsification search
    # The upper end is unbounded: increasing one GT value raises the mean above
    # any fixed finite bound, which a finite grid can illustrate but not prove.
    s3 = next(r for r in ranges if r["hi"] is None)
    probe = sample_points(s3, base=Fraction(10_000))
    assert max(probe) / 3 > 1000


def test_r2_points_respect_bounds_and_open_upper():
    svc = ObservationService(":memory:")
    from obs_service.demo import _load
    svc.import_dataset(_load("observations.json"))
    svc.import_measurements("b1", "p", [
        {"sample_id": "s3", "project": "p", "revision": 2,
         "observation": {"kind": "EXACT", "value": "7"}}])
    svc.register_threshold("p", 2, "6")
    r2 = svc.create_report("r2", "b1", "p", 2)
    avg = r2["average"]
    lo, hi = Fraction(avg["lower"]), Fraction(avg["upper"])
    assert lo == 4 and hi == Fraction(17, 3)
    ranges = [c["range"] for c in r2["contributions"] if not c["is_missing"]]
    import itertools
    choices = [list(sample_points(r)) for r in ranges]
    seen_lower = False
    for combo in itertools.product(*choices):
        mean = sum(combo, Fraction(0)) / len(combo)
        assert lo <= mean <= hi, (combo, mean)
        if mean == lo:
            seen_lower = True
        # Mathematical (not grid) fact: mean < hi always, since s1 upper end is
        # open.  Grid points can only ever be strictly below hi.
        assert mean < hi
    assert seen_lower   # lower bound is genuinely attainable (s1=0, s2=5, s3=7)
