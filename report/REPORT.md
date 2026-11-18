# Synthetic wall-clock catalogue and UTC settlement ledger — report

All values below were produced by executing the code in this repository against `materials/727`. No real time-zone database, calendar or network was consulted; the integer UTC axis, offsets and domain are the only clock rules used.

## 1. Hand-computed anchors (r1)

Material anchor: local 90 → UTC 30; local 200 → no solution; local 330 → UTC 210 and 270.

| local | status | candidates (UTC, source segment) |
|---:|---|---|
| 90 | `unique` | 30 (segment 0, offset +60) |
| 200 | `no_candidate` | — |
| 330 | `ambiguous` | 210 (segment 1, offset +120), 270 (segment 2, offset +60) |

Candidate detail for local 330 — both occurrences are enumerated and each carries the UTC segment that produced it:

| candidate | UTC | segment | segment UTC interval | offset | local interval |
|---:|---:|---:|---|---:|---|
| 0 | 210 | 1 | [120, 240) | +120 | [240, 360) |
| 1 | 270 | 2 | [240, 600) | +60 | [300, 660) |

`local 200` is a wall-clock gap. The resolver returns an empty candidate list and the evaluation rejects it; it is never shifted to a nearby offset.

## 2. r1 evaluation of J1

**E1: settled under `r1`**

- wall-clock input: local 300 → 330 (30 minutes on the wall clock)
- real UTC: 180 → 270 = **90 minutes**
- rate 2/UTC-minute, **cost 180**
- start candidates: [180, 240], chosen 180 (segment 1)
- end candidates: [210, 270], chosen 270 (segment 2)

| segment | UTC interval | offset | minutes billed | local interval |
|---:|---|---:|---:|---|
| 1 | [180, 240) | +120 | 60 | [300, 360) |
| 2 | [240, 270) | +60 | 30 | [300, 330) |

The wall clock advances 300 → 330, i.e. **30 minutes**, yet real UTC advances 180 → 270, i.e. **90 minutes**: offset +120 is left for +60 at UTC 240. Duration and pay come from the UTC difference, not the wall-clock difference.

## 3. Clock revision r2 and re-evaluation

`apply_clock_revision` moved the active revision `r1` → `r2`; r2 changes only the middle segment's offset from +120 to +90.

Point resolution under r2:

| local | r1 candidates | r2 candidates | r2 status |
|---:|---|---|---|
| 90 | [30] | [30] | `unique` |
| 200 | [] | [] | `no_candidate` |
| 330 | [210, 270] | [270] | `unique` |

Local 300 under r2 now has candidates 210 and 240 (was 180 and 240). Selecting `earlier` therefore starts at 210.

**E2: settled under `r2`**

- wall-clock input: local 300 → 330 (30 minutes on the wall clock)
- real UTC: 210 → 270 = **60 minutes**
- rate 2/UTC-minute, **cost 120**
- start candidates: [210, 240], chosen 210 (segment 1)
- end candidates: [270], chosen 270 (segment 2)

| segment | UTC interval | offset | minutes billed | local interval |
|---:|---|---:|---:|---|
| 1 | [210, 240) | +90 | 30 | [300, 330) |
| 2 | [240, 270) | +60 | 30 | [300, 330) |

Duration changed from **90 minutes / 180** (r1) to **60 minutes / 120** (r2), while the wall-clock input stayed identical at 300 → 330.

## 4. Old evaluation is frozen

`read_evaluation E1` returns revision `r1`, stored duration **90**, stored cost **180**, even though r2 is active. Its embedded catalogue snapshot is `r1` with offsets [60, 120, 60]. The amount is read from the stored row, not recomputed under current rules.

Both facts coexist in the ledger:

| evaluation | revision | real UTC | minutes | cost |
|---|---|---|---:|---:|
| E1 | r1 | [180, 270) | 90 | 180 |
| E2 | r2 | [210, 270) | 60 | 120 |

## 5. Rejection leaves no pay

**J_GAP (r1): rejected** — `no_local_candidate`

> start local 200 is produced by no segment (wall-clock gap); refusing to shift it into range

Endpoint: `start`. No UTC interval, duration or cost is stored.

The rejected evaluation has `NULL` UTC bounds, duration, rate and cost in the `evaluation` row and is excluded from every summary by the `status = 'ok'` filter. A rejection cannot leave behind a successful timesheet entry or fee.

## 6. Half-open endpoints and adjacent-segment jumps

Segments are half-open on both axes because `local = UTC + offset` is a translation. Under r1:

| local | candidates | why |
|---:|---|---|
| 59 | [] | below the first local range [60, 180) |
| 60 | [0] | first minute of segment 0 |
| 179 | [119] | last minute of segment 0's local range [60, 180) |
| 180 | [] | segment 0 stops at 180; segment 1 only starts producing 240 |
| 239 | [] | between segment 0's local end (180) and segment 1's local start (240) |
| 240 | [120] | first minute of segment 1 |
| 299 | [179] | last minute before segment 2's local range starts |
| 300 | [180, 240] | overlap of segment 1 [240, 360) and segment 2 [300, 600) |
| 359 | [239, 299] | both segments still cover local 359 (segment 1 ends at 360) |
| 360 | [300] | segment 1 ends; segment 2 has [300, 600) |

An adjacent jump where the wall clock runs forward 121 minutes but real time advances 61:

**JUMP (r1): local 179 → 300, end='earlier': settled under `r1`**

- wall-clock input: local 179 → 300 (121 minutes on the wall clock)
- real UTC: 119 → 180 = **61 minutes**
- rate 2/UTC-minute, **cost 122**
- start candidates: [119], chosen 119 (segment 0)
- end candidates: [180, 240], chosen 180 (segment 1)

| segment | UTC interval | offset | minutes billed | local interval |
|---:|---|---:|---:|---|
| 0 | [119, 120) | +60 | 1 | [179, 180) |
| 1 | [120, 180) | +120 | 60 | [240, 300) |

## 7. Multiple fall-backs (three candidates)

In a synthetic zone `M` with offsets +300, +150 and 0, local 550 is produced by all three segments. Every candidate is enumerated:

| candidate | UTC | segment | segment UTC interval | offset | local interval |
|---:|---:|---:|---|---:|---|
| 0 | 250 | 0 | [0, 300) | +300 | [300, 600) |
| 1 | 400 | 1 | [300, 500) | +150 | [450, 650) |
| 2 | 550 | 2 | [500, 600) | +0 | [500, 600) |

- `occurrence='earlier'` → 250 (segment 0)
- `occurrence='later'` → 550 (segment 2)
- no selection → `ambiguous` (rejected if used as a session endpoint)

## 8. Missing selection is ambiguous, never guessed

**AMB (r1): local 300 → 330, no selectors: rejected** — `ambiguous_local`

> start local 300 has 2 candidate UTC instants and no earlier/later selection

Endpoint: `start`. No UTC interval, duration or cost is stored.


## 9. Reversed real time

With r1, local 300 ('later') is UTC 240 and local 350 ('earlier') is UTC 230. The wall clock still reads 300 → 350, but real time runs backwards:

**REV (r1): 300 later → 350 earlier: rejected** — `non_positive_duration`

> actual UTC end must be strictly after actual UTC start: 240 -> 230

No UTC interval, duration or cost is stored.


Rejection is on the *actual UTC* ordering, not on the wall-clock ordering. Equal real times are rejected the same way (`non_positive_duration`).

## 10. Settlement summary over real UTC intersections

Query `[180, 200)` — squarely inside E1's real interval:

| evaluation | session interval | overlap | minutes | cost |
|---|---|---|---:|---:|
| E1 | [180, 270) | [180, 200) | 20 | 40 |

Total: **20 minutes / 40** — not the whole 90-minute session, and not the session attributed to its start minute.

Query `[200, 250)` cuts the same session in the middle:

Total: **50 minutes / 100**.

Full-domain settlement with `latest_per_session=False`, so every stored fact is visible:

| revision | minutes | cost | evaluations |
|---|---:|---:|---:|
| r1 | 90 | 180 | 1 |
| r2 | 60 | 120 | 1 |

The default settlement view (`latest_per_session=True`) instead bills each session once, using its newest evaluation — here the r2 evaluation — for **60 minutes / 120**, so re-evaluating does not double-pay the same work. Old facts remain individually readable.

## 11. Persistence across re-open

The database was closed and re-opened before the summary and the E1/E2 reads above. Revision history, stored evaluations and amounts all survived; nothing was recomputed from the current table.

## 12. Scale

`tests/test_operations.py::WorkloadScaleTests` builds 10 zones × 20 segments and settles 500 sessions, then intersects a 100-minute window over all 500. The full suite is 78 unittest cases.

## 13. Cost trade-offs

- **Local→UTC is a lazy per-zone scan** over at most 20 segments, built on first local query and cached in the immutable `Zone`. Cost: O(segments) per point, O(segments) memory. Nothing to persist or invalidate: a revision creates a new `Zone` object with a fresh cache, and old objects keep serving old answers. This is the choice made here.
- **Pre-built local interval index** would sort each zone's segments by local start (O(n log n) once) and binary-search the overlap band, then scan only that band. Faster for large n, but it is a derived structure that must be rebuilt on every revision; at n ≤ 20 the difference is noise, and the cached scan avoids a stale-index class of bugs.
- **Per-segment inversion without an index** (scan the raw table each call) is the simplest possible option and what a fallback path would use; identical asymptotics at this scale.
- **Multiple solutions** cost the same as one: the scan visits every segment either way, which is why full enumeration is not more expensive than taking the first hit.
- **Fixed evaluations** store the result, the full candidate lists, the segment chain and a catalogue snapshot (a few hundred bytes each). Reading them is O(1) and never re-derives under a newer revision; the price is storage proportional to evaluations, not to zones.

## 14. Operation trace

1. `resolve_points` → `ok`
2. `evaluate` → `ok`
3. `apply_clock_revision` → `ok`
4. `evaluate` → `ok`
5. `read_evaluation` → `ok`

The complete per-step JSON is available from `python3 -m clocklab run --db <db> --ops materials/727/operations.jsonl`.
