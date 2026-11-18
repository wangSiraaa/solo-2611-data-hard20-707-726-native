"""Load a saved public plan and bind values without compiling or executing SQL."""
import json
import re
import sys
from pathlib import Path


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


def decode_value(value):
    if value is None or type(value) is str:
        return value
    if type(value) is int and -(2**63) <= value < 2**63:
        return value
    if type(value) is dict and set(value) == {"$bytes"} and type(value["$bytes"]) is str:
        encoded = value["$bytes"]
        if len(encoded) % 2 or re.fullmatch(r"[0-9A-Fa-f]*", encoded) is None:
            raise ValueError("invalid byte encoding")
        return bytes.fromhex(encoded)
    raise ValueError("unsupported binding value")


def load_bound_plan(plan_path, request_path):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    if plan.get("format") != "sql-param-plan/v1":
        raise ValueError("unsupported plan format")
    if type(plan.get("revision")) is not int or plan["revision"] < 1:
        raise ValueError("invalid plan revision")
    if (plan["template_id"], plan["revision"]) != (request["template_id"], request["revision"]):
        raise ValueError("request refers to another plan")
    if type(plan["source"]) is not str or type(plan["sql"]) is not str:
        raise ValueError("invalid template text")
    names = []
    last_end = 0
    for item in plan["parameters"]:
        name, start, end = item["name"], item["start"], item["end"]
        if type(name) is not str or NAME.fullmatch(name) is None:
            raise ValueError("invalid parameter name")
        if type(start) is not int or type(end) is not int or not (last_end <= start < end <= len(plan["source"])):
            raise ValueError("invalid source character position")
        if plan["source"][start:end] != ":" + name:
            raise ValueError("position does not name source token")
        last_end = end
        names.append(name)
    bindings = request["bindings"]
    if type(bindings) is not dict or set(bindings) != set(names):
        raise ValueError("missing or extra binding keys")
    decoded = {key: decode_value(value) for key, value in bindings.items()}
    return plan["sql"], tuple(decoded[name] for name in names)


def main():
    sql, values = load_bound_plan(sys.argv[1], sys.argv[2])
    encoded = [{"$bytes": x.hex()} if type(x) is bytes else x for x in values]
    print(json.dumps({"sql": sql, "ordered_values": encoded}, ensure_ascii=False))


if __name__ == "__main__":
    main()
