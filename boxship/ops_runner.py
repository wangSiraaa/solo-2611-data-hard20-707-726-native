"""按 materials/711/operations.jsonl 顺序驱动接收服务, 并附演示扩展场景.

- 交接客户端以子进程方式独立运行 (只读外部文件, 生成 shipment-input/v1)。
- 接收服务通过 CLI 以子进程独立运行, 读取持久库。
- 额外场景演示: 跨箱移动失败、目录修订、重复发运、拆封/重开,
  以及 A-A 数量约束与同单分箱。

用法:
  python -m boxship.ops_runner --materials ../../materials/711 --workdir run
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(module: str, *args: str, expect_ok: bool | None = None) -> tuple[int, dict | list]:
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=ROOT, text=True, capture_output=True,
    )
    out = proc.stdout.strip()
    try:
        parsed = json.loads(out) if out else {}
    except json.JSONDecodeError:
        parsed = {"raw_stdout": out, "raw_stderr": proc.stderr}
    if expect_ok is not None and (proc.returncode == 0) != expect_ok:
        raise SystemExit(
            f"unexpected outcome for {module} {args}: rc={proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.returncode, parsed


def store(db: Path, *args: str, expect_ok: bool | None = None):
    return _run("boxship.store_cli", "--db", str(db), *args, expect_ok=expect_ok)


def receiver(db: Path, input_path: Path, expect_ok: bool | None = None):
    return _run("boxship.receiver_cli", "--db", str(db), "--input", str(input_path),
                expect_ok=expect_ok)


def handoff(request: Path, out: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "handoff_client.py", request.name],
        cwd=request.parent, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"handoff failed: {proc.stderr}")
    payload = json.loads(proc.stdout)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    return payload


def log(step: str, obj) -> None:
    print(f"\n### {step}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boxship.ops_runner")
    p.add_argument("--materials", default="materials/711")
    p.add_argument("--workdir", default="run")
    args = p.parse_args(argv)

    materials = Path(args.materials).resolve()
    work = Path(args.workdir).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    db = work / "store.db"

    log("导入初始事实", store(db, "init", "--seed", str(materials / "initial_store.json"),
                              expect_ok=True)[1])
    log("初始目录", store(db, "catalog", expect_ok=True)[1])

    # operations.jsonl 顺序执行; 这里把 receive 拆成“客户端外部读取 -> 接收入口”
    handoff_out = work / "shipment_input.json"
    for line in (materials / "operations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        op = json.loads(line)
        kind = op["op"]
        if kind == "receive":
            request = materials / op["file"]
            input_path = work / (request.stem + "_input.json")
            payload = handoff(request, input_path)
            rc, result = receiver(db, input_path)
            log(f"receive {op['file']} (client -> receiver)", {
                "client_emitted": {"schema": payload["schema"],
                                   "shipment_id": payload["shipment_id"],
                                   "boxes": [m["box_id"] for m in payload["manifests"]]},
                "receiver": result,
            })
            if op["file"] == "changed_shipment.json" and rc == 0:
                raise SystemExit("a_changed must be rejected")
            if op["file"] == "shipment.json" and rc != 0:
                raise SystemExit("valid split shipment must be accepted")
        elif kind == "seal":
            rc, result = store(db, "seal", "--box", op["box_id"])
            expected = op.get("expect")
            if expected == "reject_A_A" and rc == 0:
                raise SystemExit("A-A seal must be rejected before revision")
            log(f"seal {op['box_id']} (expect={expected})", result)
        elif kind == "apply_revision":
            log(f"apply_revision {op['file']}",
                store(db, "apply-revision", "--file", str(materials / op["file"]),
                      expect_ok=True)[1])
        elif kind == "unseal":
            rc, result = store(db, "unseal", "--box", op["box_id"])
            if op.get("expect") == "reject_shipped" and rc == 0:
                raise SystemExit("unseal of shipped box must be rejected")
            log(f"unseal {op['box_id']} (expect={op.get('expect')})", result)
        else:
            raise SystemExit(f"unknown op {kind}")

    # ---------------- 扩展演示 ----------------
    print("\n\n========== 扩展场景 ==========")
    log("发运后状态", store(db, "summary", expect_ok=True)[1])
    log("BOX_M 诊断 (封箱时规则 r2, 按当时规则复核)",
        store(db, "diagnose", "--box", "BOX_M", expect_ok=True)[1])

    a = json.loads((materials / "handoff/a.json").read_text(encoding="utf-8"))

    # 1) 重复发运: 同一规范内容 -> 复读; 异文 -> 拒绝; 文件路径不属于业务身份
    dup = work / "ship_1_same_again.json"
    dup.write_text(json.dumps({
        "schema": "shipment-input/v1",
        "shipment_id": "SHIP_1",
        # 顺序反转、键序不同: 规范化后仍应视为同一内容
        "manifests": [
            json.loads((materials / "handoff/b.json").read_text(encoding="utf-8")),
            json.loads((materials / "handoff/a.json").read_text(encoding="utf-8")),
        ],
    }), encoding="utf-8")
    log("SHIP_1 同内容复读 (顺序/键序不同, 路径不同)",
        receiver(db, dup, expect_ok=True)[1])
    dup2 = work / "ship_1_conflict.json"
    changed_a = json.loads((materials / "handoff/a.json").read_text(encoding="utf-8"))
    changed_a["capacity"] = 99
    dup2.write_text(json.dumps({
        "schema": "shipment-input/v1", "shipment_id": "SHIP_1",
        "manifests": [changed_a],
    }), encoding="utf-8")
    log("SHIP_1 异文复用身份 -> 拒绝", receiver(db, dup2, expect_ok=False)[1])

    # 2) 同箱在一次申请中重复出现 -> 拒绝
    twice = work / "dup_box.json"
    twice.write_text(json.dumps({
        "schema": "shipment-input/v1", "shipment_id": "SHIP_DUP",
        "manifests": [a, a],
    }), encoding="utf-8")
    log("同申请重复箱 BOX_A x2 -> 拒绝", receiver(db, twice, expect_ok=False)[1])

    # 3) 已发运箱再次发运 -> 拒绝
    ship_a_again = work / "ship_a_again.json"
    ship_a_again.write_text(json.dumps({
        "schema": "shipment-input/v1", "shipment_id": "SHIP_AGAIN",
        "manifests": [a],
    }), encoding="utf-8")
    log("已发运 BOX_A 再次发运 -> 拒绝", receiver(db, ship_a_again, expect_ok=False)[1])

    # 4) 失败不占用成功身份: SHIP_AGAIN 与 SHIP_DUP 失败后均未入库
    log("失败后的发运列表 (只应有 SHIP_1)", store(db, "summary", expect_ok=True)[1])

    # ---- 全新演示库: 跨箱移动失败 / 修订 / 重开 ----
    print("\n(以下用独立演示库展示移动原子性、修订与重开)")
    demo_db = work / "demo.db"
    store(demo_db, "init", "--seed", str(materials / "initial_store.json"), expect_ok=True)

    # 5) 跨箱移动失败: 目标容量不足 -> 整体不变
    log("新建 BOX_TIGHT capacity=1",
        store(demo_db, "create-box", "--box", "BOX_TIGHT", "--capacity", "1",
              expect_ok=True)[1])
    log("尝试把 I2(重3) 从 BOX_M 移到 BOX_TIGHT -> 容量拒绝",
        store(demo_db, "move", "--item", "I2", "--to", "BOX_TIGHT",
              expect_ok=False)[1])
    log("失败后 I2 仍在 BOX_M (原子性)",
        store(demo_db, "items", expect_ok=True)[1])

    # 6) 已封箱不能直接搬走, 必须先拆封
    log("尝试把 I1 从已封 BOX_A 移出 -> 拒绝",
        store(demo_db, "move", "--item", "I1", "--to", "BOX_TIGHT",
              expect_ok=False)[1])

    # 7) 拆封使旧封箱身份失效; 再封使用当前规则且不复用旧 seal_id
    old_a = store(demo_db, "export", "--box", "BOX_A", expect_ok=True)[1]
    log("拆封 BOX_A", store(demo_db, "unseal", "--box", "BOX_A", expect_ok=True)[1])
    log("拆封后导出旧封箱 -> 失败", store(demo_db, "export", "--box", "BOX_A",
                                         expect_ok=False)[1])
    new_a = store(demo_db, "seal", "--box", "BOX_A", expect_ok=True)[1]
    log("重封 BOX_A (新 seal_id, 复用 BOX_M 无关)",
        {"old_seal": old_a["seal_id"], "new": new_a})
    if new_a["seal_id"] == old_a["seal_id"]:
        raise SystemExit("seal_id must not be reused")

    # 8) 旧封箱工件在重封后应被拒绝 (身份失效)
    stale_input = work / "stale_a.json"
    stale_input.write_text(json.dumps({
        "schema": "shipment-input/v1", "shipment_id": "SHIP_STALE",
        "manifests": [old_a],
    }), encoding="utf-8")
    log("用旧 SEAL_A_1 工件发运 -> 拒绝 (当前 seal 已变)",
        receiver(demo_db, stale_input, expect_ok=False)[1])

    # 9) 目录修订影响下一次封箱, 不追溯改写已封箱事实
    #    r1 有 A-A; 先把两件 A 放同一开放箱 BOX_AA
    store(demo_db, "create-box", "--box", "BOX_AA", "--capacity", "10", expect_ok=True)
    store(demo_db, "move", "--item", "I2", "--to", "BOX_AA", expect_ok=True)
    store(demo_db, "move", "--item", "I4", "--to", "BOX_AA", expect_ok=True)
    log("r1 下封含两件 A 的 BOX_AA -> 拒绝 (A-A 至少两件)",
        store(demo_db, "seal", "--box", "BOX_AA", expect_ok=False)[1])
    log("失败后 BOX_AA 仍开放且内容完整",
        store(demo_db, "diagnose", "--box", "BOX_AA", expect_ok=True)[1])
    store(demo_db, "apply-revision", "--file", str(materials / "revision.json"),
          expect_ok=True)
    log("应用 r2 (去掉 A-A) 后封 BOX_AA -> 成功",
        store(demo_db, "seal", "--box", "BOX_AA", expect_ok=True)[1])
    log("r2 不追溯: 已封 BOX_A/BOX_B 仍保留 r1 事实",
        store(demo_db, "diagnose", "--box", "BOX_A", expect_ok=True)[1])

    # 10) 重开: 拆封后改变内容, 使用当前规则再封; 恢复 A-A 后拒绝
    store(demo_db, "unseal", "--box", "BOX_AA", expect_ok=True)
    rev3 = work / "rev3.json"
    rev3.write_text(json.dumps({"revision": "r3",
                                "incompatible_pairs": [["A", "B"], ["A", "A"]]}),
                    encoding="utf-8")
    store(demo_db, "apply-revision", "--file", str(rev3), expect_ok=True)
    log("重开后 r3 恢复 A-A, 再封两件 A 的 BOX_AA -> 拒绝",
        store(demo_db, "seal", "--box", "BOX_AA", expect_ok=False)[1])
    # 移除一件后可按 r3 封箱
    store(demo_db, "move", "--item", "I4", "--to", "BOX_TIGHT", expect_ok=True)
    log("移走 I4 后按 r3 再封 BOX_AA -> 成功",
        store(demo_db, "seal", "--box", "BOX_AA", expect_ok=True)[1])

    print("\n全部演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
