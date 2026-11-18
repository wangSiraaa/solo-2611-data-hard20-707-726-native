# 743 — Signed multiset source catalog + replayable LEFT JOIN derived store

Python 3.11, standard library only (SQLite via `sqlite3`). No SQL optimizer,
no arbitrary SQL and no join reordering are involved.

## Layout

```
multiset_join/
  model.py    tuple validation, signed-delta merging, canonical digests
  catalog.py  independent durable source catalog (source_history intervals)
  derive.py   pure LEFT JOIN + derived store (outputs + provenance)
  runner.py   operation-file executor
  __main__.py CLI
tests/        unittest suite (43 tests)
```

## Semantic points

**The tuple is the identity, the multiplicity is a count.** Both source tables
store complete tuples (`region, sku, quantity` / `sku, supplier, price`) with a
positive integer count. Equal complete tuples are merged by *addition*; equal
business content is never deduplicated to one.

**Batch acceptance.** A batch is first merged per `(source, complete tuple)`
and zero net deltas are dropped; only then is the final count checked. So
`+1/-1` in any input order describes the same canonical batch, and a
non-negative result is accepted. If *any* final count is negative the whole
batch is rejected atomically (transaction rollback; no version bump, no batch
record).

**Replay.** Each accepted batch stores a SHA-256 of its canonical merged
changes. Re-submitting the same `batch_id` with the same canonical content is
a pure replay: it returns the original `source_version` and applies nothing.
The same id with different content is rejected.

**Versions / slices.** Version 0 is the initial import; each accepted batch
bumps the version by one. Source state is kept as version intervals in
`source_history`, so `initial` at v0 still reads correctly after b3. The
source catalog is a standalone SQLite file and is usable without any derived
store.

**Derived store.** It owns no source copy. Each result is pinned to
`(source_version, source_fingerprint)` and stores the output rows plus the
provenance rows (left/right source tuple and both multipliers) that produced
them.

`R LEFT JOIN S USING(sku)` on multisets:

* matching left/right multiplicities contribute `mR * mS`;
* an unmatched left tuple contributes `mR` to one `(NULL, NULL)` output;
* contributions from distinct origins that land on the same output fields are
  added, never collapsed to one.

Per region the store records derived row count, quoted row count, unquoted
row count and `quoted_amount = Σ multiplicity × quantity × price`; unquoted
rows add count but no amount.

## Rebuild policy and why

`derive.py` uses **full rebuild from one immutable source slice**. At the
stated scale (10k distinct tuples per table, 500 changes/batch) a rebuild is a
linear scan (~0.1 s in the scale check) and makes the three classic
incremental bugs structurally impossible:

* **left-NULL transition** — when a sku gains a match, the synthetic NULL row
  simply isn't produced by the rebuild, so it cannot linger beside the new
  matched rows;
* **both sides in one batch** — R and S net counts come from the same accepted
  version, so cross terms are never missed;
* **zero-count deletion** — tuples whose final count is 0 are absent from the
  slice, so they cannot contribute anything.

An affected-sku incremental path would be cheaper per batch but must
explicitly evict old rows, delete/re-add the NULL row on match-set
transitions, and recompute both sides of each changed sku. That logic is where
the described bugs live; the full rebuild trades it for a linear scan. Reads
are still indexed per result/region/sku, so query cost is independent of
rebuild cost. Old results are never mutated: a new batch or a new derive does
not touch `initial`, `after-b2`, etc. Reusing a `result_id` for a different
source version is a conflict unless `replace=True`, which swaps atomically in
one transaction.

## Sample data (`materials/743/input.json`)

| result | derived rows | quoted | unquoted | amount |
|---|---|---|---|---|
| initial (v0) | 3 | 2 | 1 | 12 |
| after-b2 (v1) | 4 | 3 | 1 | 24 |
| after-b3 (v2) | 4 | 0 | 4 | 0 |

b2 deletes `A/v/3`, adds `A/w/4` and raises R's A count in the same batch, so
A's output replaces the old `v` quote with `w` and never keeps an old NULL row
for A. b3 removes `A/w/4`, so A and Q are both unquoted (4 rows, amount 0).
Replaying b2 returns v1 while the catalog is at v2.

## Usage

```bash
# Full operation file: import, apply b2/b3, derive, read, replay
python3 -m multiset_join run materials/743/input.json \
    --source-db src.db --derived-db derived.db

# Source catalog alone
python3 -m multiset_join import materials/743/input.json --source-db src.db
python3 -m multiset_join apply  materials/743/input.json --source-db src.db --batch-id b2
python3 -m multiset_join replay materials/743/input.json --source-db src.db --batch-id b2
python3 -m multiset_join read-source  --source-db src.db R --version 0
python3 -m multiset_join tuple-count  --source-db src.db S '["A","v",3]' --version 1

# Derived store (consumes the pinned source slice)
python3 -m multiset_join derive materials/743/input.json \
    --source-db src.db --derived-db derived.db --result-id after-b2 --at-batch b2
python3 -m multiset_join read-result --source-db src.db --derived-db derived.db \
    --result-id after-b2 --rows
python3 -m multiset_join explain --source-db src.db --derived-db derived.db \
    --result-id after-b2 --sku A
```

Every operation reopens the SQLite files it needs; no state is held in memory
between operations. Errors are emitted as JSON with a non-zero exit code.

Tests:

```bash
python3 -m unittest discover -s tests -v
```
