"""独立发运接收入口: 只接受 shipment-input/v1, 与持久库核对后才占用/发运.

它不导入 handoff_client, 也不读取 handoff-request; 交接客户端负责把外部
文件整理成 shipment-input/v1, 接收方负责可信性核验。

用法:
  python -m boxship.receiver_cli --db run/store.db --input run/ship_1_input.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import PackError, PackStore


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boxship.receiver_cli")
    p.add_argument("--db", required=True, help="可信持久库路径")
    p.add_argument("--input", required=True, help="shipment-input/v1 JSON 文件")
    args = p.parse_args(argv)

    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        with PackStore(args.db) as store:
            result = store.receive_shipment_input(payload)
    except PackError as e:
        print(json.dumps(e.as_dict(), ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": "bad_input", "message": str(e)},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
