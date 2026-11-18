"""railctl — command line for the ordered-marshalling and unload services."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import RailwayStore
from .db import connect
from .material import load_material, material_summary, replay_operations
from .model import ValidationError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _store(args) -> RailwayStore:
    if args.db == ":memory:":
        raise SystemExit("use a file-backed --db for CLI commands")
    return RailwayStore(args.db)


def cmd_init(args) -> None:
    path = Path(args.db)
    if path.exists():
        raise SystemExit(f"{path} already exists; refusing to overwrite")
    conn = connect(str(path))
    try:
        summary = load_material(conn, args.material)
    finally:
        conn.close()
    _print({"db": str(path), "loaded": summary})


def cmd_replay(args) -> None:
    material = Path(args.material)
    with _store(args) as store:
        summary = material_summary(material)
        if args.window:
            start, end = args.window.split(",", 1)
            summary["platform_window"] = [
                {"n": int(start), "d": 1},
                {"n": int(end), "d": 1},
            ]
        for item in replay_operations(store, summary):
            _print(item)


def cmd_composition(args) -> None:
    with _store(args) as store:
        _print(store.composition.composition(args.train_id))


def cmd_coordinate(args) -> None:
    with _store(args) as store:
        _print(store.composition.car_coordinate(args.train_id, args.car_id, args.local))


def cmd_event(args) -> None:
    with _store(args) as store:
        _print(store.composition.apply_event(json.loads(Path(args.file).read_text())))


def cmd_scan(args) -> None:
    window = [args.start, args.end]
    with _store(args) as store:
        report = store.platform.scan_platform(
            train_id=args.train_id,
            report_id=args.report_id,
            window=window,
            composition_version=args.version,
        )
        if args.out:
            store.platform.write_report(args.report_id, args.out)
        _print(report)


def cmd_read_report(args) -> None:
    with _store(args) as store:
        _print(store.platform.get_report(args.report_id))


def cmd_prepare(args) -> None:
    with _store(args) as store:
        _print(
            store.unload.prepare_unload(
                job_id=args.job_id,
                train_id=args.train_id,
                composition_version=args.version,
                window=[args.start, args.end],
                cargo_ids=args.cargo,
            )
        )


def cmd_confirm(args) -> None:
    with _store(args) as store:
        _print(store.unload.confirm_unload(args.job_id))


def cmd_cancel(args) -> None:
    with _store(args) as store:
        _print(store.unload.cancel_unload(args.job_id, args.reason))


def cmd_jobs(args) -> None:
    with _store(args) as store:
        _print(store.unload.list_jobs())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="railctl", description=__doc__)
    p.add_argument("--db", default="rail.db")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="load material into a new database")
    s.add_argument("--material", default="materials/734")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("replay", help="replay the material operation log")
    s.add_argument("--material", default="materials/734")
    s.add_argument("--window", help="override platform window as start,end")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("composition", help="show order/orientations/version")
    s.add_argument("train_id")
    s.set_defaults(func=cmd_composition)

    s = sub.add_parser("coordinate", help="map a car-local point to train x")
    s.add_argument("train_id")
    s.add_argument("car_id")
    s.add_argument("local")
    s.set_defaults(func=cmd_coordinate)

    s = sub.add_parser("event", help="apply a couple/split/reverse JSON request")
    s.add_argument("file")
    s.set_defaults(func=cmd_event)

    s = sub.add_parser("scan", help="platform scan -> platform-scan/v1 report")
    s.add_argument("train_id")
    s.add_argument("report_id")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--version")
    s.add_argument("--out")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("read-report", help="read a saved report")
    s.add_argument("report_id")
    s.set_defaults(func=cmd_read_report)

    s = sub.add_parser("prepare", help="prepare an unload job")
    s.add_argument("job_id")
    s.add_argument("train_id")
    s.add_argument("--version", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--cargo", nargs="+", required=True)
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("confirm", help="confirm an unload job")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_confirm)

    s = sub.add_parser("cancel", help="cancel a prepared unload job")
    s.add_argument("job_id")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("jobs", help="list unload jobs")
    s.set_defaults(func=cmd_jobs)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValidationError, LookupError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # domain refusals from model/unload
        from .model import ConflictError, InactiveError, UnloadError

        if isinstance(exc, (ConflictError, InactiveError, UnloadError)):
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
