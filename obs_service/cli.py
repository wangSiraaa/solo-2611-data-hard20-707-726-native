"""Command-line entry points.

Examples::

    python -m obs_service.cli import --db obs.db --file materials/715/observations.json
    python -m obs_service.cli report --db obs.db --id r1 --batch b1 --project p --threshold 1
    python -m obs_service.cli show   --db obs.db --id r1
    python -m obs_service.cli explain --db obs.db --id r1            # average derivation
    python -m obs_service.cli explain --db obs.db --id r1 --sample s3
    python -m obs_service.cli ops    --db obs.db --file materials/715/operations.json \
                                     --import-file materials/715/observations.json
    python -m obs_service.cli normalize --obs '{"kind":"LT","upper":"5"}'
    python -m obs_service.cli intersect --a '{"kind":"LT","upper":"5"}' \
                                        --b '{"kind":"GE","lower":"4"}'
    python -m obs_service.cli check --obs '{"kind":"GT","lower":"5"}' --threshold 5
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import ObservationService


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="obs_service", description=__doc__)
    p.add_argument("--db", default="obs.db", help="SQLite path (default obs.db; ':memory:' allowed)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("import", help="import a dataset object")
    pi.add_argument("--file", required=True)

    pm = sub.add_parser("import-measurements", help="import measurement records")
    pm.add_argument("--batch", required=True)
    pm.add_argument("--project", required=True)
    pm.add_argument("--file", required=True, help="JSON list or {records:[...]}")

    pt = sub.add_parser("threshold", help="register a threshold revision")
    pt.add_argument("--project", required=True)
    pt.add_argument("--revision", type=int, required=True)
    pt.add_argument("--value", required=True)

    pr = sub.add_parser("report", help="create an immutable report")
    pr.add_argument("--id", required=True)
    pr.add_argument("--batch", required=True)
    pr.add_argument("--project", required=True)
    pr.add_argument("--threshold", type=int, required=True, dest="threshold_revision")

    ps = sub.add_parser("show", help="read a stored report")
    ps.add_argument("--id", required=True)

    pl = sub.add_parser("list", help="list reports")
    pl.add_argument("--batch", default=None)

    pe = sub.add_parser("explain", help="explain a report average or one sample")
    pe.add_argument("--id", required=True)
    pe.add_argument("--sample", default=None)
    pe.add_argument("--focus", default="average")

    ph = sub.add_parser("history", help="revision history of one sample")
    ph.add_argument("--batch", required=True)
    ph.add_argument("--project", required=True)
    ph.add_argument("--sample", required=True)

    po = sub.add_parser("ops", help="run an operations.json file")
    po.add_argument("--file", required=True)
    po.add_argument("--import-file", default=None, help="dataset to import first")

    pn = sub.add_parser("normalize", help="normalize an observation JSON")
    pn.add_argument("--obs", required=True)

    px = sub.add_parser("intersect", help="intersect two observation JSONs")
    px.add_argument("--a", required=True)
    px.add_argument("--b", required=True)

    pc = sub.add_parser("check", help="classify x > threshold for an observation")
    pc.add_argument("--obs", required=True)
    pc.add_argument("--threshold", required=True)

    args = p.parse_args(argv)
    try:
        svc = ObservationService(args.db)
    except Exception as exc:  # pragma: no cover - startup errors
        print(f"error: cannot open db: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "import":
            with open(args.file, encoding="utf-8") as f:
                _print(svc.import_dataset(json.load(f)))

        elif args.cmd == "import-measurements":
            with open(args.file, encoding="utf-8") as f:
                payload = json.load(f)
            records = payload.get("records", payload) if isinstance(payload, dict) else payload
            _print(svc.import_measurements(args.batch, args.project, records))

        elif args.cmd == "threshold":
            _print(svc.register_threshold(args.project, args.revision, args.value))

        elif args.cmd == "report":
            _print(svc.create_report(args.id, args.batch, args.project, args.threshold_revision))

        elif args.cmd == "show":
            rep = svc.read_report(args.id)
            if rep is None:
                print(f"error: no report {args.id!r}", file=sys.stderr)
                return 1
            _print(rep)

        elif args.cmd == "list":
            _print(svc.list_reports(args.batch))

        elif args.cmd == "explain":
            _print(svc.explain(args.id, args.sample, args.focus))

        elif args.cmd == "history":
            _print(svc.revision_history(args.batch, args.project, args.sample))

        elif args.cmd == "ops":
            payload = {}
            if args.import_file:
                with open(args.import_file, encoding="utf-8") as f:
                    payload["import"] = svc.import_dataset(json.load(f))
            with open(args.file, encoding="utf-8") as f:
                ops = json.load(f)
            payload["ops"] = svc.run_operations(ops)
            _print(payload)

        elif args.cmd == "normalize":
            _print(svc.normalize(json.loads(args.obs)))

        elif args.cmd == "intersect":
            _print(svc.intersect(json.loads(args.a), json.loads(args.b)))

        elif args.cmd == "check":
            _print(svc.threshold_check(json.loads(args.obs), args.threshold))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
