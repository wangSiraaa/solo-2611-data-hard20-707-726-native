#!/usr/bin/env bash
# Full verification: unit/integration tests, 756 scenario, scale dataset.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== tests =="
python3 -m unittest discover -s tests -t .

echo
echo "== 756 scenario (reset) =="
python3 tools/run_scenario.py --reset

echo
echo "== scale dataset =="
python3 tools/generate_scale.py
python3 tools/run_scale.py --reset

echo
echo "== reopen artefacts =="
python3 tools/delegation_cli.py --db db/delegation.db read-report REPORT1 --verify
python3 tools/delegation_cli.py --db db/delegation.db read-report REPORT2 --verify
python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
    confirmation DEL1 >/dev/null && echo "DEL1 readable"
python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
    confirmation DEL2 >/dev/null && echo "DEL2 readable"
python3 tools/delivery_cli.py --db db/delivery.db --delegation-db db/delegation.db \
    status S1 | python3 -c 'import json,sys; print("S1 status:", json.load(sys.stdin)["status"])'
