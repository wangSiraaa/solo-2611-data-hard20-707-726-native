# Fictional questionnaire catalog & scoring repository

A small, source-complete reference implementation of two independent stores:

* **scale catalog** (`catalog.py`, `catalog.sqlite3`) — stable question IDs and
  allowed integer ranges, plus immutable per-scale versions.
* **scoring repository** (`scoring_repo.py`, `scoring.sqlite3`) — respondents,
  answer revisions, and immutable scored products with a per-question trace.

`scoring_client.py` is the upgraded consumer that reads **both** the delivered
legacy format and the new `scoring-product/2` format and keeps their scoring
contracts visibly distinct. `demo.py` runs the whole story on the provided
`materials/724` files with hand-checkable arithmetic. `tests/` holds contract
tests (including the 50-question / 10-scale / 500-respondent scale test).

All data here is fictional questionnaire data. There is **no** reliability
estimation, norm conversion, statistical imputation, or psychological
diagnosis anywhere in this project.

## Requirements

Python 3.11, standard library only (`sqlite3`, `fractions`, `hashlib`, `json`).
No third-party packages.

## Quick start

```bash
python3 demo.py                 # full demonstration with assertions
python3 -m unittest discover -s tests -v
python3 scoring_client.py materials/724/legacy_scores.json \
        --catalog materials/724/legacy_catalog.json
```

CLI:

```bash
python3 cli.py import-legacy-catalog --catalog materials/724/legacy_catalog.json
python3 cli.py import-history        --history  materials/724/legacy_scores.json
python3 cli.py operations            --file     materials/724/operations.jsonl
python3 cli.py show-scale --scale A
python3 cli.py list-reports
python3 cli.py report --report-id SCORE_U1R2_AV2
python3 cli.py lookup --report-id SCORE_U1R2_AV2 --respondent U1 \
        --scale A --version 2 --revision 2
```

Each command can point at separate databases with `--catalog-db` / `--repo-db`.

## The core correction

The legacy catalog stores an authoritative `entries` list per scale **and** a
top-level `direction_cache_by_question` blob. The blob is wrong: a question can
be forward in one scale and reversed in another.

| question | A entry | B entry | C entry | legacy cache |
|----------|---------|---------|---------|--------------|
| Q1       | forward | **reversed** | forward | forward (wrong for B) |
| Q2       | reversed | — | **forward** | reversed (wrong for C) |

`import_legacy_catalog` therefore:

1. imports each scale's `entries` as the sole authority;
2. stores the cache in an `ignored_direction_cache` table that **no scoring code
   reads**, and records every disagreement in `cache_conflicts` as evidence;
3. never writes a per-question direction back into the catalog.

Scoring reads `scale_entries` only, and applies reversal to a local copy of the
answer: `contribution = (min + max - answer) * weight`. The question in the
catalog is never flipped. For the same answer `Q1 = 5`:

* A (forward, w=2) → `5 * 2 = 10`
* B (reversed, w=1) → `(1 + 5 - 5) * 1 = 1`

The delivered `HIST_SCORE_001` reports B = 5. That is the old shared-direction
result, preserved verbatim and never presented as the current score.

## Scoring rules

Let `K` be a scale version's `missing_weight_limit`.

* Each entry answers with an integer in `[min, max]` or `MISSING`.
  Reversal uses that entry's copied `min`/`max`.
* `weighted_sum` = Σ contribution over answered entries.
  `answered_weight` = Σ weight over answered entries.
* `missing_weight` = Σ weight over missing entries.
* **Eligible** iff `missing_weight <= K`.
* For each missing entry, its *possible contribution* spans its own legal
  range: `min*weight .. max*weight` (or the reversed equivalents). These ranges
  are summed into `min_total` / `max_total`; no mean is imputed into a missing
  slot and no zero is assumed.
* `mean` is exact and reduced: `weighted_sum / answered_weight` as `a/b`, or
  `undefined` when `answered_weight == 0`. All-missing within `K` yields sum 0,
  answered weight 0, `mean = undefined`, and often a **non-zero** `min_total`
  (e.g. scale C: `2..12`).
* Ineligible products store no official `weighted_sum`/`min_total`/`max_total`/
  `mean` (all `null`) with an explicit `ineligible_reason`. Per-entry contributions
  and possible ranges remain for transparency.

## Repository contract

* Answers are validated as a whole before any write. Unknown question,
  out-of-range value, boolean, float or non-`MISSING` string rejects the entire
  submission; nothing is stored (`submit_answers`).
* Answer revisions are append-only. Re-submitting identical content is
  `duplicate_identical` (no-op); different content under the same revision is
  rejected; a revision lower than the latest is rejected.
* `publish_scores` computes **all** selections first, then writes one report.
  Any failure rolls the whole publication back.
* Published products are immutable. Re-publishing identical content is
  `already_published`; different content for the same report id is rejected.
  Report ids never collide with history (a `published-scores/1` id cannot be
  reused by a `scoring-product/2` product, and vice versa).

## New product format (`scoring-product/2`)

```json
{
  "format": "scoring-product/2",
  "report_id": "SCORE_U1R2_AV2",
  "scoring_contract": "per-scale-entry",
  "direction_source": "per-scale-entry",
  "catalog_version": "catalog-after-ops",
  "report_hash": "<sha256 of content>",
  "scores": [{
    "respondent_id": "U1", "answer_revision": 2,
    "scale_id": "A", "scale_version": 2,
    "eligible": true, "ineligible_reason": null,
    "missing_weight_limit": 1, "missing_weight": 1,
    "answered_weight": 2, "weighted_sum": 4,
    "min_total": 4, "max_total": 6, "mean": "2/1",
    "entries": [{
      "question_id": "Q1", "min": 1, "max": 5,
      "weight": 2, "reverse": true,
      "answer": 4, "contribution": 4,
      "possible_contribution_min": 1, "possible_contribution_max": 12
    }]
  }]
}
```

The same data is normalized into `score_entries` for the repository's
independent `lookup_score` path, so the client can read the JSON document and
the query surface can read the rows; both are produced from the same commit.

The client verifies `report_hash` before trusting a new product. A tampered
document fails with `report_hash_mismatch`.

## History handling

`import_history_document` accepts **only** `published-scores/1` and stores it
byte-for-byte (plus an import timestamp). It never recomputes legacy values and
never applies the current catalog. `scoring_client.read_document` dispatches on
`format`:

* `published-scores/1` → `kind: "historical"`, `rescored: false`, values shown
  as delivered (B = 5, mean `5/1`).
* `scoring-product/2` → `kind: "current"`, `per-scale-entry`, hash verified,
  full entry trace.

The contracts are surfaced side by side but never merged, so a caller cannot
mistake the old number for a corrected one.

## Versions and atomic publication

* Scale versions are immutable and never overwritten. Publishing version `n+1`
  marks older versions `superseded` but keeps them scorable; new scores pin an
  explicit `scale_version`. Publishing an existing version number with
  different content is rejected.
* Answer revisions are immutable and append-only; `(respondent_id, revision)`
  pins the exact content hash used by every score.
* A new version and/or answer revision actually changes the new score
  (demo §8), while old products and the historical file keep their original
  version numbers and values.
* `publish_scores` is a single transaction in the repository; the catalog is
  untouched. The two SQLite files cannot share one ACID transaction, so the
  operations journal uses deterministic, content-derived report ids to make a
  replay after a crash an `already_published` no-op. A live multi-store
  transaction that spanned two database files is intentionally avoided; the
  journal is idempotent instead.

## Snapshot vs. current catalog

`write_snapshot` writes a standalone `scoring-snapshot/1` file containing the
catalog with all versions (each keeping its own entries, directions and status)
and every stored report. This is an explicit, separate artifact. A score
publish never mutates the snapshot, and restoring is an explicit import. So a
repository can hold a historical snapshot while publishing corrected current
products from the same catalog, with no partial new/old direction mixture in a
formal product.

## Compatibility trade-offs (summary)

| Concern | Decision | Rationale |
|---|---|---|
| Legacy cache | stored, ignored, conflicts recorded | cannot express per-scale direction; keeping it only as evidence |
| Legacy products | imported verbatim, labelled historical | fidelity of already-delivered numbers; no silent rewrite |
| New products | `scoring-product/2`, per-scale-entry, hashed | correct direction, traceable, current |
| Missing answers | own legal range, never zero/mean imputed | honest lower/upper bounds |
| Scale/answer versions | immutable, pinned | reproducible, no history overwrite |
| Cross-store publish | idempotent journal, deterministic ids | two files cannot be one transaction; no partial products |
| Snapshot | separate explicit file | history preserved without blocking current correction |

## Layout

```
common.py        hashing, validation, exact fractions, transactions
catalog.py       question bank, versioned scales, scoring maths, legacy import
scoring_repo.py  respondents, revisions, reports, per-entry trace, history, snapshot
scoring_client.py upgraded dual-format read-only client (+ CLI)
cli.py           command line entry points
demo.py          end-to-end demonstration on materials/724 with assertions
tests/test_contract.py   contract tests incl. 50q/10scale/500-respondent scale test
materials/724/   provided legacy catalog, answers, operations, legacy scores, reader
```
