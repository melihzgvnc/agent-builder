#!/usr/bin/env python3
"""Generate a deployable mock-bank Worker from a service contract and customers.

    python mock/gen_mock.py --contract PATH --customers PATH [--out DIR] [--name NAME]

Writes worker.js, config.json, data.json and wrangler.toml into DIR
(default: mock/dist). Nothing is deployed. The contract is read tolerantly:
an OpenAPI 3 document is the expected shape; anything else produces a loud
warning on stderr, a best-effort config, and exit code 0.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# role -> keywords matched (lowercase substring) against the tool name first,
# then against its summary/description.
ROLE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("close", ("close", "end_call", "hangup", "hang_up")),
    ("transfer", ("transfer", "handoff", "hand_off", "escalat")),
    ("callback", ("callback", "call_back", "reschedule")),
    ("promise", ("promise", "commit", "arrangement")),
    ("note", ("note", "remark", "comment")),
    ("verify", ("verify", "identity", "authenticat", "challenge")),
    ("account", ("account", "balance", "lookup", "products")),
]
GATED_ROLES = ("account", "promise")


def warn(msg: str) -> None:
    print(f"WARNING (loud): {msg}", file=sys.stderr)


def tool_name(path: str) -> str:
    parts = [p for p in str(path).split("/") if p]
    return parts[-1] if parts else ""


def load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"cannot read {what} {path}: {exc}")
        return None


def operation_schema(op: dict) -> dict:
    try:
        return op["requestBody"]["content"]["application/json"]["schema"] or {}
    except (KeyError, TypeError):
        return {}


def response_props(op: dict) -> dict:
    try:
        return op["responses"]["200"]["content"]["application/json"]["schema"].get("properties", {})
    except (KeyError, TypeError, AttributeError):
        return {}


def parse_contract(doc) -> tuple[dict, dict]:
    """Return (tools, extras). tools: name -> {path, summary, required, props}."""
    tools: dict[str, dict] = {}
    extras: dict = {"base_path": "/v1", "response_props": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        warn("contract is not an OpenAPI document with `paths`; guessing the standard tool set")
        return tools, extras
    servers = doc.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        from urllib.parse import urlparse
        base = urlparse(str(servers[0].get("url", ""))).path.rstrip("/")
        if base:
            extras["base_path"] = base
    for path, item in doc["paths"].items():
        op = item.get("post") if isinstance(item, dict) else None
        if not isinstance(op, dict):
            continue
        name = tool_name(path)
        if not name:
            continue
        schema = operation_schema(op)
        props = {}
        for key, spec in (schema.get("properties") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            entry = {"type": spec.get("type", "string")}
            if isinstance(spec.get("enum"), list):
                entry["enum"] = spec["enum"]
            fmt = spec.get("format")
            if fmt in ("date", "time"):
                entry["format"] = fmt
            elif "HH:MM" in str(spec.get("description", "")):
                entry["format"] = "time"
            props[key] = entry
        tools[name] = {
            "path": "/" + name,
            "summary": str(op.get("summary") or op.get("description") or ""),
            "required": [k for k in (schema.get("required") or []) if isinstance(k, str)],
            "props": props,
        }
        extras["response_props"][name] = response_props(op)
    if not tools:
        warn("contract has no POST operations; the mock will only serve sessions and ledgers")
    return tools, extras


def detect_roles(tools: dict) -> dict:
    roles: dict[str, str] = {}
    for by_name in (True, False):
        for name, tool in tools.items():
            if name in roles.values():
                continue
            hay = name.lower() if by_name else tool["summary"].lower()
            for role, words in ROLE_KEYWORDS:
                if role not in roles and any(w in hay for w in words):
                    roles[role] = name
                    break
    return roles


def pick(tool: dict | None, pred) -> str | None:
    if not tool:
        return None
    for key, spec in tool["props"].items():
        if pred(key, spec):
            return key
    return None


def detect_fields(tools: dict, roles: dict) -> dict:
    fields: dict[str, str] = {}
    customer = None
    for tool in tools.values():
        customer = customer or pick(tool, lambda k, s: "customer" in k.lower())
    fields["customer_id"] = customer or "customer_id"
    cid = fields["customer_id"]
    verify = tools.get(roles.get("verify", ""))
    key = pick(verify, lambda k, s: "enum" in s)
    ans = pick(verify, lambda k, s: k != cid and k != key and s.get("type") == "string")
    if key:
        fields["verify_key"] = key
    if ans:
        fields["verify_answer"] = ans
    close = tools.get(roles.get("close", ""))
    outcome = pick(close, lambda k, s: "enum" in s)
    reason = pick(close, lambda k, s: k not in (cid, outcome) and s.get("type") == "string")
    if outcome:
        fields["close_outcome"] = outcome
    if reason:
        fields["close_reason"] = reason
    return fields


def detect_hint(extras: dict, roles: dict):
    props = extras["response_props"].get(roles.get("verify", ""), {})
    for key, spec in props.items():
        if isinstance(spec, dict) and key.lower() == "hint" and spec.get("enum"):
            return {"key": key, "value": spec["enum"][0]}
    return None


def load_customers(raw) -> list[dict]:
    """Normalise public-test-set shape and the simpler {id|customer_id,...} shape."""
    if isinstance(raw, dict):
        raw = raw.get("customers") or raw.get("cases") or []
    out: list[dict] = []
    if not isinstance(raw, list):
        warn("customers file is not a list; no customers written")
        return out
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        cid = item.get("id") or item.get("customer_id")
        if not cid:
            warn(f"customer #{i} has no id/customer_id; skipped")
            continue
        products = []
        for n, p in enumerate(item.get("products") or [], start=1):
            if isinstance(p, dict):
                # Heuristic: product ids default to p1..pN by position.
                products.append({"product_id": p.get("product_id", f"p{n}"), **{k: v for k, v in p.items() if k != "product_id"}})
        out.append({
            "id": str(cid),
            "variables": item.get("variables") or {},
            "identity": item.get("identity") or {},
            "products": products,
            "faults": [f for f in (item.get("faults") or []) if isinstance(f, dict) and f.get("tool")],
        })
    return out


WRANGLER = """\
# Generated by mock/gen_mock.py. Edit the placeholders before deploying.
name = "{name}"
main = "worker.js"
# Pick a recent date; it pins the Workers runtime behaviour.
compatibility_date = "2025-01-01"

# Optional durable ledgers. Create with `npx wrangler kv namespace create LEDGER_KV`
# and paste the returned id, then uncomment. Without it ledgers are memory only.
# [[kv_namespaces]]
# binding = "LEDGER_KV"
# id = "REPLACE_WITH_KV_NAMESPACE_ID"
"""


def generate(contract: Path, customers: Path, out: Path, name: str = "bank-mock-REPLACE-ME") -> int:
    doc = load_json(contract, "contract")
    tools, extras = parse_contract(doc)
    roles = detect_roles(tools)
    for role, _ in ROLE_KEYWORDS:
        if role not in roles and tools:
            warn(f"no tool recognised for role '{role}'; such calls are recorded generically")
    config = {
        "name": name,
        "base_path": extras["base_path"],
        "error_status": 200,
        "fault_status": 500,
        "team_header": "x-team-key",
        "tools": tools,
        "roles": roles,
        "gated_tools": [roles[r] for r in GATED_ROLES if r in roles],
        "fields": detect_fields(tools, roles),
    }
    hint = detect_hint(extras, roles)
    if hint:
        config["verify_hint"] = hint
    raw_customers = load_json(customers, "customers")
    data = {"customers": load_customers(raw_customers)}
    if not data["customers"]:
        warn("no customers were loaded; sessions will fail with 'unknown case'")

    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "worker.js", out / "worker.js")
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (out / "data.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (out / "wrangler.toml").write_text(WRANGLER.format(name=name), encoding="utf-8")
    print(f"wrote worker.js config.json data.json wrangler.toml to {out}")
    print(f"tools: {', '.join(tools) or '(none)'}; roles: {roles}; customers: {len(data['customers'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contract", required=True, type=Path)
    ap.add_argument("--customers", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=HERE / "dist")
    ap.add_argument("--name", default="bank-mock-REPLACE-ME")
    a = ap.parse_args(argv)
    return generate(a.contract, a.customers, a.out, a.name)


if __name__ == "__main__":
    sys.exit(main())
