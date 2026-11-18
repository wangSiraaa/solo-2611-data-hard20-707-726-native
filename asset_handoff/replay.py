"""Replay driver for the synthetic scenario in ``materials/755``.

It loads the synthetic initial forest + custodians, then executes the
operations in ``operations.jsonl`` against the two independent stores:

* ``prepare_and_commit_handoff`` -> warehouse prepare + atomic commit,
  exporting an ``asset-handoff/1`` JSON artifact per handoff;
* ``revise_membership`` -> catalog-only physical move (custody untouched);
* ``read_handoff`` / ``read_current_custodians`` -> reads back frozen history
  and live responsibility.

The reader supplied with the materials is read-only and needs no database:
when its path is given, the runner shells out to it so the artifact format is
checked by the actual client, not just by our own code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import AssetCatalog
from .errors import AssetHandoffError
from .warehouse import HandoffWarehouse


def _bootstrap_catalog(catalog: AssetCatalog, state: dict) -> None:
    """Load the synthetic initial forest without advancing the revision.

    ``assets.json`` itself declares ``membership_revision: 1``; bootstrap is
    loading that state, not a mutating batch, so the catalog stays at rev 1
    and the first real ``revise_membership`` produces rev 2, matching the
    revision numbers carried by the operations file.
    """
    expected_revision = int(state.get("membership_revision", 1))
    rows = [
        (asset["asset_id"], asset["type"]) for asset in state["assets"]
    ]
    with catalog.conn:
        catalog.conn.executemany(
            "INSERT INTO assets(asset_id, type) VALUES (?, ?)", rows
        )
        for child_id, parent_id in state.get("parent_by_child", {}).items():
            catalog.conn.execute(
                "INSERT INTO edges(child_id, parent_id) VALUES (?, ?)",
                (child_id, parent_id),
            )
        catalog.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'membership_revision'",
            (expected_revision,),
        )


def _bootstrap_custody(warehouse: HandoffWarehouse, state: dict) -> None:
    """Load initial custodians as one bulk custody write (one version bump)."""
    mapping = {
        asset["asset_id"]: asset["custodian"]
        for asset in state["assets"]
        if "custodian" in asset
    }
    warehouse.set_custodians(mapping)


def run_operations(
    assets_path: str | Path,
    operations_path: str | Path,
    catalog_db: str | Path,
    warehouse_db: str | Path,
    artifact_dir: str | Path,
    reader_path: str | Path | None = None,
) -> dict:
    """Execute the full scenario and return a structured replay report."""
    import subprocess

    assets_path = Path(assets_path)
    operations_path = Path(operations_path)
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Fresh stores: the scenario file describes one deterministic timeline.
    for db_path in (catalog_db, warehouse_db):
        Path(db_path).unlink(missing_ok=True)

    state = json.loads(assets_path.read_text(encoding="utf-8"))
    catalog = AssetCatalog(str(catalog_db))
    warehouse = HandoffWarehouse(str(warehouse_db), catalog)
    report: dict = {"operations": [], "artifacts": {}, "reader_checks": []}

    _bootstrap_catalog(catalog, state)
    _bootstrap_custody(warehouse, state)
    report["initial_revision"] = catalog.revision

    try:
        for line in operations_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            op = json.loads(line)
            kind = op["operation"]

            if kind == "prepare_and_commit_handoff":
                record = warehouse.prepare_and_commit(
                    op["request_id"], op["roots"], op["from"], op["to"]
                )
                artifact_path = artifact_dir / f"{op['request_id']}.json"
                warehouse.export_artifact(op["request_id"], str(artifact_path))
                report["artifacts"][op["request_id"]] = str(artifact_path)
                report["operations"].append(
                    {
                        "operation": kind,
                        "request_id": op["request_id"],
                        "members": [m["asset_id"] for m in record["members"]],
                        "member_count": len(record["members"]),
                        "membership_revision": record["catalog_revision"],
                        "artifact": str(artifact_path),
                    }
                )
                if reader_path is not None:
                    proc = subprocess.run(
                        ["python3", str(reader_path), str(artifact_path)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    report["reader_checks"].append(
                        {
                            "request_id": op["request_id"],
                            "stdout": proc.stdout.splitlines(),
                        }
                    )

            elif kind == "revise_membership":
                revision = catalog.revise_membership(
                    moves=op.get("moves", ()),
                    attaches=op.get("attaches", ()),
                    detaches=op.get("detaches", ()),
                )
                if revision != int(op["revision"]):
                    raise AssertionError(
                        f"expected revision {op['revision']} after "
                        f"{kind}, got {revision}"
                    )
                report["operations"].append(
                    {
                        "operation": kind,
                        "revision": revision,
                        "moves": op.get("moves", []),
                        "custody_after_move": warehouse.current_custodians(),
                    }
                )

            elif kind == "read_handoff":
                record = warehouse.get_handoff(op["request_id"])
                report["operations"].append(
                    {
                        "operation": kind,
                        "request_id": op["request_id"],
                        "status": record["status"],
                        "frozen_members": [
                            m["asset_id"] for m in record["members"]
                        ],
                        "frozen_member_count": len(record["members"]),
                        "membership_revision": record["catalog_revision"],
                    }
                )

            elif kind == "read_current_custodians":
                report["operations"].append(
                    {
                        "operation": kind,
                        "custodians": warehouse.current_custodians(),
                    }
                )

            else:
                raise ValueError(f"unknown operation {kind!r}")
    finally:
        catalog.close()
        warehouse.close()

    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--assets",
        default="materials/755/assets.json",
        help="synthetic initial state (default: materials/755/assets.json)",
    )
    ap.add_argument(
        "--operations",
        default="materials/755/operations.jsonl",
        help="operation timeline (default: materials/755/operations.jsonl)",
    )
    ap.add_argument(
        "--out-dir",
        default="runtime",
        help="directory for SQLite files and handoff artifacts",
    )
    ap.add_argument(
        "--reader",
        default="materials/755/handoff_reader.py",
        help="read-only asset-handoff/1 client used to verify artifacts",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = run_operations(
            args.assets,
            args.operations,
            out_dir / "catalog.sqlite",
            out_dir / "warehouse.sqlite",
            out_dir / "artifacts",
            reader_path=args.reader,
        )
    except AssetHandoffError as exc:
        raise SystemExit(f"scenario failed: {exc}") from exc

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
