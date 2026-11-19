# scale_calibration

Exact integer-code interpretation and cross-section common-shift calibration.
Python 3.11, standard library only (`fractions`, `sqlite3`).

Two independent layers:

| Layer | Module | Responsibility |
|---|---|---|
| Conversion catalog | `catalog.py` | Immutable scale contracts + frozen read interpretations, exact rationals |
| Section calibration | `calibration.py` | Choose one read per point + one common shift, complete feasible set |

Persistence is SQLite (`store.py`); interval algebra is in `rationals.py`; the
CLI is `__main__.py`.

## Exact arithmetic

A code `x` is interpreted as `base + x*p/q` with `fractions.Fraction` and is
never rounded. Inputs are `int`, `Fraction`, or strings (`"-3"`, `"3/4"`,
`"0.75"`). Floats and bools are rejected on purpose — accepting a float
reintroduces binary rounding into the one thing this tool exists to get right.
All reported positions/shifts are canonical `n/d` strings.

## Run the bundled example

```bash
python -m scale_calibration --db cal.db demo materials/759/input.json
```

Expected results:

| Request | Outcome |
|---|---|
| `G1` | **Infeasible.** A (code 6→3) window `[3/4,5/4]`, B@1 (14→7) window `[7/4,9/4]`; both individually fixable, no common `s`. |
| `G2` | **Feasible** `s ∈ [3/4,5/4]`, witness `s=1` → corrected 4 and 9 with B@2 (14→8). |
| `G3` | **Infeasible.** Same legal reads as G2, but allowed shift `[0,1/2]` excludes `[3/4,5/4]`. |
| `G4` | **Feasible** `s ∈ [3/4,5/4] ∪ [7/4,9/4]`, using exactly 1 backup; `3/2` is *not* feasible. |

## Independent entry points

```bash
# Code-value lookup, independent of any stored read
python -m scale_calibration --db cal.db interpret K 2 14      # -> position 8/1

# One read revision, frozen with its contract provenance
python -m scale_calibration --db cal.db read B 1              # -> position 7/1, scale_revision 1

# Full revision history of a read id (old values still queryable)
python -m scale_calibration --db cal.db read-history B

# Calibration intake (from a payload or an inline/file request)
python -m scale_calibration --db cal.db calibrate --file materials/759/input.json --request-id G2

# Stored products and corrected samples (history read)
python -m scale_calibration --db cal.db products
python -m scale_calibration --db cal.db product G2
python -m scale_calibration --db cal.db samples G2
```

Exit codes: `0` success, `2` input error, `3` infeasible.

## Design decisions

### Interpretation is frozen at read insertion, not recomputed

A read stores its resolved `position` string plus the `scale_id` /
`scale_revision` it used. Publishing a new contract revision or a new read
revision never rewrites an old read. This is what makes "new revision may
switch contract; old reads and old values remain queryable" hold literally, and
it is why a stored calibration product is insulated from later catalog changes.

### Idempotency and conflicts

- Contracts and reads: same natural key + same content → return stored row
  (`created: false`); different content → `ConflictError`.
- Calibration: identity is a SHA-256 fingerprint of the *submitted* request
  content only (no catalog lookup). Identical resubmission is served from
  storage **before** any catalog access, so later revisions cannot perturb it.
  Different content under the same `request_id` is rejected.

### Feasibility: sweep endpoints, not selections

With 20 points × 6 candidates there are `6^20` selections. Coverage of a point
across shifts changes only at candidate window endpoints, so we sweep the sorted
endpoints and one midpoint per gap. At a shift `s`, the minimum backup count is
exactly the number of points covered at `s` that have **no PRIMARY** covering
`s` — each such point is served independently by one backup. That makes the
whole problem linear in the number of endpoints instead of exponential.

### Closed endpoints and separated feasible ranges

- Interval constraints are **closed** (`|pos + s − ref| ≤ ε`), so a feasible
  open segment is published as a closed interval.
- Closedness cuts the other way at a single point: a shift can be feasible while
  both neighbouring open segments are not (e.g. `[0,1]` and `[1,2]` meet only at
  `1`). We therefore **also evaluate every event point on its own** and emit a
  zero-width interval when feasible. The output is an ordered union of disjoint
  closed intervals — possibly a single point, possibly several parts — never a
  heuristic envelope.
- **Why not intersect widest per-point envelopes:** a point's candidates may be
  far apart (near 0 and near 10). The envelope `[−10.4, 0.4]` overlaps another
  point's window near −5, fabricating a shift that no actual candidate reaches.
  The endpoint sweep per candidate forbids this; `EnvelopeTrapTests` pins it.

### Interpretation evidence in the result

Every sample carries `position`, the chosen `shift`, `corrected`, exact
`residual`, and a `source` block with `scale_id`, `scale_revision`, `code`. The
product also embeds, per point, each candidate's raw and in-bounds shift window,
so the decision is auditable from the product alone.

### Failure diagnostics are layered

Infeasibility is reported by the first layer that fails, so the reason names the
real blocker:

1. raw candidate windows, no backup cap, no shift bounds → "individual windows
   are disjoint" (G1);
2. windows clipped to the allowed shift interval → "none lies in the allowed
   shift interval" (G3);
3. budget only → "requires at least N BACKUP read(s) exceeding budget".

On any failure no row is written: the request/product/sample insert is a single
`BEGIN IMMEDIATE … COMMIT`.

## Out of scope

No slope fitting, no GPS/georeferencing, no field-construction judgement. The
tool only interprets codes exactly and decides whether one common shift plus a
per-point candidate choice satisfies the stated closed constraints.

## Tests

```bash
python -m unittest discover -s tests -v
```

37 tests: exact-rational parsing and interval algebra, catalog idempotency and
conflict, all four bundled cases, replay across connections and later
revisions, input errors, no-product-on-failure, isolated feasible points, exact
non-terminating windows, the envelope trap, and a 20-point × 6-candidate × 100
request scale run.
