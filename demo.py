"""End-to-end demonstration against materials/734.

Run: ``python3 demo.py``

Everything is synthetic; no real train is controlled.  Each section prints what
it did and the assertions it proves, so the transcript doubles as a report.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from rail import RailwayStore
from rail.material import load_material, material_summary, replay_operations
from rail.model import ConflictError, InactiveError, UnloadError, ValidationError

ROOT = Path(__file__).resolve().parent
MATERIAL = ROOT / "materials" / "734"
REPORTS = ROOT / "reports"


def show(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def fmt_x(frac):
    f = Fraction(frac["n"], frac["d"])
    return str(f)


def table(store, report):
    print(f"{report['report_id']}  train={report['train_id']}  "
          f"version={report['composition_version'][:12]}…  "
          f"window=[{fmt_x(report['window']['start'])},{fmt_x(report['window']['end'])})")
    for p in report["cargo_positions"]:
        mark = "IN " if p["in_window"] else "out"
        print(f"  {p['cargo_id']:>3}  car={p['car_id']:>3}  x={fmt_x(p['x']):>4}  {mark}"
              f"  status={p['cargo_status']}")


def composition_table(store, train_id):
    view = store.composition_view(train_id)
    cells = ", ".join(
        f"{c['car_id']}{'+' if c['orientation'] == 1 else '-'}"
        for c in view["cars"]
    )
    print(f"{train_id}: [{cells}]  version={view['composition_version'][:12]}…")
    return view


def fresh_material_store(path):
    store = RailwayStore(str(path))
    load_material(store.conn, MATERIAL)
    return store


def main():
    workdir = Path(tempfile.mkdtemp(prefix="rail-demo-"))
    db_path = workdir / "rail.db"
    REPORTS.mkdir(exist_ok=True)
    try:
        # --------------------------------------------------------------
        show("1. 载入材料并重放 operations.jsonl (E1 尾接尾, R1, E2 调头, R2)")
        store = fresh_material_store(db_path)
        summary = material_summary(MATERIAL)
        for item in replay_operations(store, summary):
            op = item["op"]
            res = item["result"]
            if op == "couple":
                print(f"E1 couple tail<->tail -> rule={res['layout_rule']}")
            elif op == "reverse":
                print(f"E2 reverse -> {res['composition_version'][:12]}…")
            elif op == "scan_platform":
                table(store, res)
        v_tail = store.composition_view("T")["composition_version"]
        composition_table(store, "T")
        for report_id in ("R1", "R2"):
            path = REPORTS / f"{report_id}.json"
            store.platform.write_report(report_id, path)
            print(f"wrote {path.relative_to(ROOT)}")

        # --------------------------------------------------------------
        show("2. 尾接尾 + 调头后的真实站台命中 (platform-scan/v1)")
        r1 = store.platform.get_report("R1")
        r2 = store.platform.get_report("R2")
        table(store, r1)
        table(store, r2)
        assert [p["cargo_id"] for p in r1["cargo_positions"] if p["in_window"]] == ["C1"]
        assert [p["cargo_id"] for p in r2["cargo_positions"] if p["in_window"]] == ["C3"]
        x1 = {p["cargo_id"]: fmt_x(p["x"]) for p in r1["cargo_positions"]}
        x2 = {p["cargo_id"]: fmt_x(p["x"]) for p in r2["cargo_positions"]}
        print("tail-tail  x:", x1, "  expected C1/C2/C3 = 2/12/28")
        print("after flip x:", x2, "  expected C3/C2/C1 = 2/18/28")

        # --------------------------------------------------------------
        show("3. 调头断链: U1 准备合法 -> reverse -> 旧 U1 必须拒绝")
        s3 = fresh_material_store(workdir / "chain.db")
        s3.apply_event(
            {
                "op": "couple",
                "event_id": "E1",
                "target": "T",
                "target_end": "tail",
                "other": "S",
                "other_end": "tail",
            }
        )
        r1_chain = s3.platform.scan_platform("T", "R1", [0, 5])
        v_tail = r1_chain["composition_version"]
        table(s3, r1_chain)
        u1 = s3.prepare_unload("U1", "T", v_tail, [0, 5], ["C1"])
        print("U1 prepared while tail-tail:", u1["state"], "cargo:", u1["cargo_ids"])
        s3.apply_event({"op": "reverse", "event_id": "E2", "train_id": "T"})
        composition_table(s3, "T")
        try:
            s3.confirm_unload("U1")
            raise AssertionError("U1 should have been refused")
        except UnloadError as exc:
            print("U1 confirm refused after reverse:", exc)
        print("C1 status:", s3.get_cargo("C1")["status"])
        assert s3.get_cargo("C1")["status"] == "LOADED"

        v_rev = s3.composition_view("T")["composition_version"]
        r2_chain = s3.platform.scan_platform("T", "R2", [0, 5])
        table(s3, r2_chain)
        u2 = s3.prepare_unload("U2", "T", v_rev, [0, 5], ["C3"])
        print("U2 prepared for C3 at x =", fmt_x(u2["items"][0]["x"]))
        done = s3.confirm_unload("U2")
        print("U2 confirmed:", done["state"], "receipt:", done["receipt"]["receipt_id"])
        print("C3 status:", s3.get_cargo("C3")["status"])

        try:
            s3.prepare_unload("U3", "T", v_rev, [0, 5], ["C3"])
            raise AssertionError("U3 should have been refused")
        except UnloadError as exc:
            print("U3 refused (geometry still hits, cargo UNLOADED):", exc)
        print("R1/R2 reports remain readable:",
              [r["report_id"] for r in s3.platform.list_reports()])
        print("U2 receipt remains readable:",
              s3.unload.get_receipt("U2")["receipt_id"])
        s3.close()

        # --------------------------------------------------------------
        show("4. 四种接端 (each on a fresh store)")
        for target_end, other_end, label in [
            ("tail", "head", "尾接头 -> target+other"),
            ("tail", "tail", "尾接尾 -> target+reverse(other)"),
            ("head", "tail", "头接尾 -> other+target"),
            ("head", "head", "头接头 -> reverse(other)+target"),
        ]:
            s = RailwayStore(":memory:")
            for cid in ("V1", "V2", "V3"):
                s.add_car(cid, 10)
            s.create_train("T", [{"car_id": "V1"}, {"car_id": "V2"}])
            s.create_train("S", [{"car_id": "V3"}])
            s.apply_event(
                {
                    "op": "couple",
                    "event_id": f"E-{target_end}-{other_end}",
                    "target": "T",
                    "target_end": target_end,
                    "other": "S",
                    "other_end": other_end,
                }
            )
            cells = ", ".join(
                f"{c['car_id']}{'+' if c['orientation'] == 1 else '-'}"
                for c in s.composition_view("T")["cars"]
            )
            print(f"  {label:<42} -> [{cells}]")
            s.close()

        # --------------------------------------------------------------
        show("5. 头/尾拆出 (split n, 拆出序列保持拆前方向)")
        s = fresh_material_store(workdir / "split.db")
        replay_operations(s, material_summary(MATERIAL))
        head = s.apply_event(
            {
                "op": "split",
                "event_id": "SP-HEAD",
                "train_id": "T",
                "end": "head",
                "n": 2,
                "new_train_id": "T-HEAD",
            }
        )
        print("split head 2 ->", [c["car_id"] + ("+" if c["orientation"] == 1 else "-") for c in head["new_cars"]],
              "remaining", [c["car_id"] + ("+" if c["orientation"] == 1 else "-") for c in head["cars"]])
        s.close()

        s = fresh_material_store(workdir / "split2.db")
        replay_operations(s, material_summary(MATERIAL))
        tail = s.apply_event(
            {
                "op": "split",
                "event_id": "SP-TAIL",
                "train_id": "T",
                "end": "tail",
                "n": 1,
                "new_train_id": "T-TAIL",
            }
        )
        print("split tail 1 ->", [c["car_id"] + ("+" if c["orientation"] == 1 else "-") for c in tail["new_cars"]],
              "remaining", [c["car_id"] + ("+" if c["orientation"] == 1 else "-") for c in tail["cars"]])
        try:
            s.apply_event(
                {"op": "split", "event_id": "SP-BAD", "train_id": "T-TAIL",
                 "end": "head", "n": 1, "new_train_id": "T-TAIL2"}
            )
        except ValidationError as exc:
            print("split n >= total refused:", exc)
        s.close()

        # --------------------------------------------------------------
        show("6. 重复事件 / 异文拒绝 / 非法自接 / 共享车厢 / 重开")
        s = RailwayStore(str(workdir / "events.db"))
        for cid in ("V1", "V2", "V3"):
            s.add_car(cid, 10)
        s.create_train("T", [{"car_id": "V1"}, {"car_id": "V2"}])
        s.create_train("S", [{"car_id": "V3"}])
        for cid, car_id in (("C1", "V1"), ("C2", "V2"), ("C3", "V3")):
            s.add_cargo(cid, car_id, 2)
        req = {"op": "couple", "event_id": "E1", "target": "T",
               "target_end": "tail", "other": "S", "other_end": "tail"}
        first = s.apply_event(req)
        again = s.apply_event(dict(req))
        print("same event_id + same content -> identical result:", first == again)
        try:
            s.apply_event({**req, "other_end": "head"})
        except ConflictError as exc:
            print("same event_id + different content refused:", exc)
        try:
            s.apply_event({"op": "couple", "event_id": "SELF", "target": "T",
                           "target_end": "tail", "other": "T", "other_end": "tail"})
        except ValidationError as exc:
            print("self-couple refused:", exc)
        try:
            s.create_train("X", [{"car_id": "V1"}])
        except ValidationError as exc:
            print("shared car refused:", exc)
        try:
            s.apply_event({"op": "reverse", "event_id": "E1", "train_id": "T"})
        except ConflictError as exc:
            print("event_id reused for another op refused:", exc)

        version = s.composition_view("T")["composition_version"]
        s.close()

        s = RailwayStore(str(workdir / "events.db"))
        print("reopen -> T version preserved:",
              s.composition_view("T")["composition_version"] == version)
        print("reopen -> event replay identical:",
              s.apply_event(dict(req)) == first)
        print("reopen -> cargo bindings:",
              [(c["cargo_id"], c["car_id"], fmt_x(c["local_position"]), c["status"])
               for c in s.unload.list_cargo()])
        print("reopen -> T=[V1+,V2+,V3-], cargo x:",
              {cid: fmt_x(s.cargo_coordinate("T", cid)["x"])
               for cid in ("C1", "C2", "C3")})
        s.close()

        # --------------------------------------------------------------
        show("7. 终态概览")
        s = fresh_material_store(workdir / "final.db")
        replay_operations(s, material_summary(MATERIAL))
        print("trains:", [(t["train_id"], t["active"]) for t in s.composition.list_trains()])
        print("cargo :", [(c["cargo_id"], c["status"]) for c in s.unload.list_cargo()])
        print("reports:", [(r["report_id"], r["composition_version"][:12] + "…")
                           for r in s.platform.list_reports()])
        s.close()
        print("\nAll demonstrations completed without assertion failures.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
