# 委托时点授权与分件签收齐套确认

Two independent, file-backed services (Python 3.11, SQLite), plus a thin
integration adapter and CLIs. All grants, receipts, shipments and people are
synthetic; there is no real identity, cryptographic signature or legal effect.

```
delegation/     委托核查服务  (grants, originals, chains, reports)
delivery/       分件交付确认仓 (shipments, supplements, coverage, confirmations)
tools/          integration adapter + CLIs + scenario/scale drivers
inputs/         756 originals + the published S1 / supplements / R4 originals
data/scale/     generated 30/100/200/50x10 dataset
tests/          63 unit + integration tests
```

The two packages never import each other. The delivery depot depends only on
`delivery/ports.py::AuthorityPort`; `tools/authority_adapter.py` (integration
layer) implements it over the delegation store, read-only.

## Run everything

```bash
bash tools/verify_all.sh
```

Individual entry points:

```bash
python3 -m unittest discover -s tests -t .          # tests
python3 tools/run_scenario.py --reset               # materials/756 + S1 scenario
python3 tools/generate_scale.py                     # regenerate scale inputs
python3 tools/run_scale.py --reset                  # scale load + review sweep
```

Read artefacts back (the reopen requirement):

```bash
python3 tools/delegation_cli.py --db db/delegation.db read-report REPORT1 --verify
python3 tools/delegation_cli.py --db db/delegation.db read-report REPORT2 --verify
python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
    confirmation DEL1
python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
    status S1
```

## Delegation side (委托核查服务)

**Data.** `grants(grant_id, revision)` are immutable revisions; `receipts` keep
the original verbatim plus a canonical content hash. Same id + same content is
an idempotent replay; same id + different content is rejected (异文拒绝).

**Ingest validation (whole batch, atomic).** A single malformed grant or receipt
rejects the entire batch with nothing written. Structure enforced at ingest:

* at most two levels; a child names its parent explicitly;
* child issuer = parent agent; parent `can_redelegate` must be true;
* child nominal `[start,end)` and region/type sets must be subsets of the parent;
* `revoked_from` must lie inside the nominal interval.

**Assessment** (`delegation/authority.py`) follows *only* the claimed chain — no
substitution of another grant, no identity inference from a name:

* self-signed receipt (signer = consignee) may carry an empty chain;
* delegated receipt must claim a complete root→leaf path, root issuer = consignee,
  and the actual signer must be the final claimed agent;
* per level: effective at `signed_day` (nominal window, and not at/after
  revocation) and within region/type scope;
* **timing:** reached revocation day ⇒ invalid. A parent revocation truncates
  the whole path; later levels are reported `not_checked`, never repaired;
* **intake day is never used** for validity (R1 signed 10, intaken 18 is valid).

New assessments resolve each claimed id to its *newest* revision, so a nominal or
revocation correction changes today's verdict. Published reports are immutable
snapshots: `REPORT1` keeps its original inputs and conclusion even after the
correction; `verify_report` re-hashes on read and reports whether it is still
current. The delegation side stores no shipments, parts, coverage or
confirmation state.

## Delivery side (分件交付确认仓)

**Data.** Shipments (consignee/region/type/required parts); receipt supplements
(the original's content + a fixed `shipment_id`/`part_ids` binding); part
bindings; immutable confirmations; and a separate mutable shipment state pointer.

Binding rules: parts must belong to the shipment; region/type must match; a
receipt cannot be re-pointed at another shipment or different parts (binding is a
supplement fixed once); same id + same content replays, different content is
rejected.

**Coverage.** Only an original whose delegation-side content matches and whose
assessment is valid — and whose consignee/region/type match the shipment — can
cover its explicitly bound parts. Parts are unioned by `part_id`; duplicate
originals never add quantity. Any missing part ⇒ not complete; a single valid
evidence boolean is never treated as whole-shipment completion.

**Confirmation lifecycle.**

* `prepare` snapshots complete evidence + authority fingerprints (pinned).
* `confirm` requires a fresh preparation whose evidence **and** authority hashes
  still match; confirmation records the fixed parts, evidence and authority
  sources atomically with the state pointer. Stale preparation is refused.
* Confirmations are immutable (`DEL1`, `DEL2`, ...) and linked
  (`previous_confirmation_id`).
* When an authority correction regresses coverage, the shipment moves to
  `NEEDS_REVIEW` — the original confirmation identity is retained unchanged and
  no longer claims current completeness. New evidence that restores completeness
  can be prepared and confirmed, producing `DEL2` linked to `DEL1`.

The delivery depot never recomputes delegation authority; it records the
authority verdicts/fingerprints it was given.

## Scenario (materials/756 + published shipment)

`inputs/` holds the 756 grants/receipts/operations plus the published
`shipments.jsonl`, `receipt_supplements.jsonl` (R1→BX1/BX2, R2→BX2, R3 unbindable
— omitted), and the new `R4` original and supplement. `tools/run_scenario.py`
replays, in order:

1. initial assessment: R1 valid, R2 (day 12 = revocation boundary) and R3
   (region S) invalid → `REPORT1` (valid=1);
2. S1 coverage from R1 alone (even though intaken day 18) → prepare → `DEL1`;
   re-importing R1 adds no parts;
3. revise G1 revocation 12 → 9 → `REPORT2` (valid=0); `REPORT1` unchanged;
4. reconcile → S1 `NEEDS_REVIEW`, `DEL1` untouched;
5. ingest `R4` (self-signed P0, day 10, intaken 19, BX1+BX2) on both sides →
   coverage complete → prepare → `DEL2` (previous = `DEL1`), S1 `CONFIRMED`.

## Scale

`tools/generate_scale.py` produces 30 people, 100 grants (two-level families,
some revoked), 200 originals, 50 shipments × 10 parts, 100 supplements.
`tools/run_scale.py` loads them through both services, confirms complete
shipments, applies an authority correction, and asserts the required counts
while swapping 30 confirmed shipments into `NEEDS_REVIEW`.

## Deliberate scope limits

No multi-person approval chains, no generic permission platform. The review
sweep is a single operational action; identity inference, real signatures and
legal effect are out of scope. Optional point-in-time traversal is represented
by immutable report/confirmation snapshots rather than a full temporal index.
