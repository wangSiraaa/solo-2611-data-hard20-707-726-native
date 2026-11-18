#!/usr/bin/env bash
# End-to-end demo on the synthetic 754 scenario.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MAT="$HERE/materials/754"
OUT="$HERE/out"
DB="$OUT/demo.db"

rm -rf "$OUT"
mkdir -p "$OUT"

python3 -m cumulative_billing.cli replay \
  --db "$DB" \
  --contract "$MAT/contract.json" \
  --usage "$MAT/usage.jsonl" \
  --bills "$MAT/issued_bills.jsonl" \
  --operations "$MAT/operations.jsonl"

python3 -m cumulative_billing.cli export \
  --db "$DB" --out "$OUT/bills_final.jsonl" --account AC1 --year 2025

echo
echo "--- read-only client over exported period-bill/1 ---"
python3 "$MAT/bill_reader.py" "$OUT/bills_final.jsonl"

echo
echo "--- current cumulative statement (fee / positions / marginal) ---"
python3 -m cumulative_billing.cli reassess --db "$DB" --account AC1 --year 2025 \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); print("complete_through", s["complete_through"]); [print(p["period"], p["quantity"], p["cumulative_start"], p["cumulative_end"], p["fee_cents"]) for p in s["periods"][:3]]'
