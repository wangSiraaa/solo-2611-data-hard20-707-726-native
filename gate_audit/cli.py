"""命令行驱动：按 operations.jsonl 的顺序对材料执行事实核查。

用法：
    python -m gate_audit.cli --materials materials/733 \
        --db out/gate_audit.sqlite --outdir out/reports

支持的操作行：
    {"op": "audit_routes", "file": "routes.json", "report_id": "R1"}
    {"op": "revise_gate", "file": "revision.json"}
    {"op": "read_report", "report_id": "R1"}
    {"op": "edge_query", "revision": "r1", "rooms": ["A", "B"]}
    {"op": "route_query", "revision": "r1", "route_id": "X", "points": [[...]]}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import Catalog, CatalogError
from .reports import AuditStore, ReportError


def _write(outdir: Path, name: str, payload: dict) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def run(materials: Path, db_path: Path, outdir: Path,
        operations_file: str = "operations.jsonl") -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()  # 重跑材料时保持确定：历史报告由测试单独验证持久化

    with Catalog(db_path) as catalog:
        store = AuditStore(catalog)
        version = catalog.load_plan_file(materials / "plan.json")
        current_revision = version.revision
        log: list[dict] = []

        ops = [json.loads(line) for line in
               (materials / operations_file).read_text(
                   encoding="utf-8").splitlines() if line.strip()]
        for seq, op in enumerate(ops, 1):
            kind = op["op"]
            if kind == "audit_routes":
                data = json.loads((materials / op["file"]).read_text("utf-8"))
                batch = store.audit_batch(
                    current_revision, data["routes"], op["report_id"])
                for r in batch["routes"]:
                    verdict = "LEGAL" if r["legal"] else "REJECTED"
                    first = r["first_violation"]
                    print(f"[{seq}] audit {r['route_id']} @ "
                          f"{current_revision} -> {verdict}"
                          + ("" if r["legal"] else
                             f" | first={first['reason']} @ {first['point']}"))
                fname = f"{op['report_id']}.json"
                _write(outdir, fname, batch)
                log.append({"op": kind, "report": fname,
                            "replayed": batch["replayed"]})

            elif kind == "revise_gate":
                version = catalog.apply_revision_file(
                    materials / op["file"], version)
                current_revision = version.revision
                print(f"[{seq}] revise_gate -> 新门版本 {current_revision} "
                      f"(base={version.base_revision})，旧版本保持冻结")
                log.append({"op": kind, "revision": current_revision})

            elif kind == "read_report":
                payload = store.read_report(op["report_id"])
                _write(outdir, f"read_{op['report_id']}.json", payload)
                print(f"[{seq}] read_report {op['report_id']}: "
                      f"平面版本={payload['plan_revision']} 复读成功，"
                      "门证据为旧版本快照")
                log.append({"op": kind, "report_id": op["report_id"]})

            elif kind == "edge_query":
                payload = store.edge_query(op["revision"], *op["rooms"])
                _write(outdir, "edge_query.json", payload)
                print(f"[{seq}] edge_query {op['rooms']} @ {op['revision']}")
                log.append({"op": kind})

            elif kind == "route_query":
                payload = store.audit_route(
                    op["revision"], op["route_id"], op["points"],
                    op["report_id"])
                _write(outdir, f"{op['report_id']}.json", payload)
                log.append({"op": kind, "report_id": op["report_id"]})

            else:
                raise CatalogError(f"未知操作: {kind}")

        _write(outdir, "operation_log.json", {"operations": log})
        print("\n已签发凭据:")
        for cred in store.list_credentials():
            print(" ", cred["credential_id"], cred["revision"],
                  cred["route_id"], "rooms=", cred["room_chain_json"])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="展馆门段与路线核查事实库")
    ap.add_argument("--materials", default="materials/733")
    ap.add_argument("--db", default="out/gate_audit.sqlite")
    ap.add_argument("--outdir", default="out/reports")
    args = ap.parse_args()
    try:
        raise SystemExit(run(Path(args.materials), Path(args.db),
                             Path(args.outdir)))
    except (CatalogError, ReportError) as exc:
        raise SystemExit(f"事实库拒绝: {exc}")


if __name__ == "__main__":
    main()
