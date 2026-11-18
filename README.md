# clocklab — synthetic wall-clock catalogue and UTC settlement ledger

Self-contained Python 3.11 implementation for the `materials/727` task. It uses
only the integer-minute UTC axis, offsets and domains declared in the material
files; no real time-zone database, calendar, network or system clock is read.

## Layout

```
clocklab/
  clock.py        immutable zones/segments, validation, local->UTC candidates
  evaluate.py     endpoint resolution, UTC duration/cost, rejection codes
  storage.py      SQLite facts: revisions, sessions, evaluations, summaries
  operations.py   JSONL/JSON operation runner
  __main__.py     CLI (`python -m clocklab ...`)
tests/            78 unittest cases (no third-party dependencies)
report/REPORT.md  generated evidence report with every segment used
report/generate_report.py
```

## Run

```bash
# apply the material operation file to a fresh database
python3 -m clocklab load-clock --db /tmp/ledger.sqlite --file materials/727/clock.json
python3 -m clocklab run --db /tmp/ledger.sqlite --ops materials/727/operations.jsonl

# independent clock query (no session needed)
python3 -m clocklab point --db /tmp/ledger.sqlite --local 330 --zone Z
python3 -m clocklab point --db /tmp/ledger.sqlite --local 330 --zone Z --occurrence later
python3 -m clocklab point --db /tmp/ledger.sqlite --local 200 --zone Z --revision r1

# settlement over a real UTC interval (intersection minutes only)
python3 -m clocklab summary --db /tmp/ledger.sqlite --from 180 --to 200 --revision r1

# stored facts
python3 -m clocklab evaluation --db /tmp/ledger.sqlite --evaluation E1
python3 -m clocklab session --db /tmp/ledger.sqlite --session-id J1
python3 -m clocklab revisions --db /tmp/ledger.sqlite

# regenerate the report
python3 report/generate_report.py
```

Tests:

```bash
python3 -m unittest discover -s tests -t tests
```

## Operations

`run` accepts a JSONL file (or a JSON array). Supported ops:

| op | fields |
|---|---|
| `load_clock` | `file` (a full clock document) |
| `resolve_points` | `file` (a requests document: `point_checks` and `sessions`); `revision`/`zone_id` optional |
| `register_sessions` | `file` (a document with `sessions`) |
| `evaluate` | `session_id`, `evaluation_id` (uses the active revision) |
| `apply_clock_revision` | `file` (full document or `replace_segments` form) |
| `read_evaluation` | `evaluation_id` |
| `get_session` | `session_id` |
| `list_sessions` | — |
| `summarize` | `utc_from`, `utc_to`, optional `zone_id`, `session_id`, `revision`, `group_by`, `latest_per_session` |

`resolve_points` also registers any `sessions` embedded in the requests file
(idempotently, and without letting a conflict overwrite an existing
definition), so the material file's later `evaluate` steps have their sessions.
A failing step is recorded as `{"status": "error", ...}` and the run continues.

## Core rules implemented

* A zone table must cover its domain exactly: segments are half-open,
  contiguous and non-overlapping, with integer endpoints and offsets. An
  invalid revision is rejected as a whole and the active revision is unchanged.
* UTC→local is unique. local→UTC returns **every** covering segment as a
  candidate (zero, one or many), each with its source segment, offset and
  segment UTC span. `earlier`/`later` select the earliest/latest candidate
  **by UTC**; a single candidate is used for either selector; an empty list is
  rejected without shifting; several candidates with no selector are
  ambiguous and rejected.
* Sessions are interpreted against one immutable catalogue version for both
  endpoints. Duration and cost use the real UTC difference. End must be
  strictly later in UTC; wall-clock order is irrelevant.
* A rejection persists no UTC bounds, duration, rate or cost, and is excluded
  from settlement.
* Evaluations are immutable facts. `evaluation_id` is idempotent for identical
  content and refuses different content. Old evaluations keep their stored
  revision, candidate lists, segment chain and amount; they are never
  re-interpreted under a newer table.
* Summaries bill only the intersection of each real UTC interval with the
  query window. By default each session is settled once (its newest
  evaluation, so a newer rejection voids older pay); `latest_per_session=False`
  exposes every stored fact; `revision=` pins one clock version.

## Cost choices

local→UTC uses a lazy per-zone cache of segments ordered by local start and a
linear scan (≤ 20 segments). A revision builds a fresh `Zone`, so nothing needs
invalidating, and old objects keep serving old answers. A pre-built local
interval index would speed up very large tables but is a derived structure that
must be rebuilt on every revision; at this scale it is unnecessary. Multiple
candidates cost the same as one because the scan visits every segment anyway.
Storing full evaluation snapshots makes reads O(1) and independent of current
rules, at the cost of storage per evaluation.
