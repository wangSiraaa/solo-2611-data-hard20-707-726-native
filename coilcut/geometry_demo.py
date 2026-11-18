"""Geometry-only demos: show where a local cut lands on the root axis."""

from __future__ import annotations

from fractions import Fraction

from .geometry import PieceGeometry, cut_geometry, flip_geometry


def demo_731() -> None:
    root = PieceGeometry("P0", "ROOT", 0, 100, 1)
    c1 = cut_geometry(root, 30, 2, "L1", "R1")
    print("C1 parent P0 [0,100) u=30 kerf=2")
    print(f"  L1 root [{c1.left.root_lo},{c1.left.root_hi}) orient={c1.left.orientation}")
    print(f"  R1 root [{c1.right.root_lo},{c1.right.root_hi}) orient={c1.right.orientation}")
    print(f"  kerf [{c1.kerf_interval.lo},{c1.kerf_interval.hi})")

    r1_flipped = flip_geometry(c1.right)
    c2 = cut_geometry(r1_flipped, 20, 2, "L2", "R2")
    print("C2 parent R1 flipped, local u=20 from the -1 start (root hi)")
    print(f"  L2 root [{c2.left.root_lo},{c2.left.root_hi}) orient={c2.left.orientation} len={c2.left.length}")
    print(f"  R2 root [{c2.right.root_lo},{c2.right.root_hi}) orient={c2.right.orientation} len={c2.right.length}")
    print(f"  kerf [{c2.kerf_interval.lo},{c2.kerf_interval.hi})")
    assert c2.left.root_lo == Fraction(80)
    assert c2.left.root_hi == Fraction(100)
    assert c2.left.orientation == -1
    assert c2.right.root_lo == Fraction(32)
    assert c2.right.root_hi == Fraction(78)
    assert c2.kerf_interval.lo == Fraction(78)
    assert c2.kerf_interval.hi == Fraction(80)


def demo_rational() -> None:
    root = PieceGeometry("P0", "ROOT", 0, Fraction(1), 1)
    cut = cut_geometry(root, Fraction(1, 3), Fraction(1, 100), "A", "B")
    print("Rational cut:")
    print(f"  A [{cut.left.root_lo}, {cut.left.root_hi})")
    print(f"  kerf [{cut.kerf_interval.lo}, {cut.kerf_interval.hi})")
    print(f"  B [{cut.right.root_lo}, {cut.right.root_hi})")


if __name__ == "__main__":  # pragma: no cover
    demo_731()
    demo_rational()
