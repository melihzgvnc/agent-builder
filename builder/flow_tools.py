"""Stage A: tool list extracted from the contract text -> normalised plan -> Freya api_call tool dicts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from builder import tools_gen
from builder.tools_gen import API_CALL_HANDLER, placeholder

TOOL_TIMEOUT_MS = 30_000
NAME_OK = re.compile(r"[^A-Za-z0-9_]+")
FIELD_TYPES = {"string", "number", "integer", "boolean"}


@dataclass
class ToolPlan:
    call_variables: list[dict[str, str]] = field(default_factory=list)
    base_address: str = ""
    wrapper_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    response_convention: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    server_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_variables": self.call_variables, "base_address": self.base_address,
            "request_envelope": {"wrapper_key": self.wrapper_key, "extra": self.extra},
            "response_convention": self.response_convention, "tools": self.tools,
            "warnings": self.warnings, "server_url": self.server_url,
        }

    def names(self) -> list[str]:
        return [t["name"] for t in self.tools]

    def var_names(self) -> list[str]:
        return [v["name"] for v in self.call_variables]


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def origin_of(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"invalid server url {url!r}: expected http(s)://host")
    return f"{p.scheme}://{p.netloc}"


def tool_url(base_address: str, path: str, server_url: str | None) -> str:
    """Contract path under the contract base path; the origin is replaced by --server-url when given."""
    base = urlparse(base_address) if base_address else urlparse("")
    base_path = base.path.rstrip("/")
    if path.startswith(("http://", "https://")):
        pp = urlparse(path)
        full_path, contract_origin = pp.path, f"{pp.scheme}://{pp.netloc}"
    else:
        rel = path.strip()
        if rel.startswith("/") and (not base_path or rel.startswith(base_path + "/")):
            full_path = rel
        else:
            full_path = base_path + "/" + rel.lstrip("/")
        contract_origin = f"{base.scheme}://{base.netloc}" if base.netloc else ""
    origin = origin_of(server_url) if server_url else contract_origin
    return origin + full_path


def normalise(raw: Any, server_url: str | None = None) -> ToolPlan:
    """Pure clean-up of the stage A answer. Never raises on odd shapes; records warnings."""
    plan = ToolPlan(server_url=server_url)
    raw = raw if isinstance(raw, dict) else {}
    for v in raw.get("call_variables") or []:
        if isinstance(v, dict) and _s(v.get("name")):
            plan.call_variables.append({"name": _s(v["name"]), "description": _s(v.get("description")),
                                        "example_from_docs": _s(str(v.get("example_from_docs") or ""))})
        elif isinstance(v, str) and v.strip():
            plan.call_variables.append({"name": v.strip(), "description": "", "example_from_docs": ""})
    plan.base_address = _s(raw.get("base_address"))
    env = raw.get("request_envelope") if isinstance(raw.get("request_envelope"), dict) else {}
    plan.wrapper_key = _s(env.get("wrapper_key")) or None
    plan.extra = env.get("extra") if isinstance(env.get("extra"), dict) else {}
    plan.response_convention = raw.get("response_convention") if isinstance(raw.get("response_convention"), dict) else {}
    vars_ = set(plan.var_names())
    seen: set[str] = set()
    for t in raw.get("tools") or []:
        if not isinstance(t, dict):
            continue
        path = _s(t.get("path"))
        name_src = _s(t.get("name")) or path.rstrip("/").rsplit("/", 1)[-1]
        name = NAME_OK.sub("_", name_src).strip("_")
        if not path or not name:
            plan.warnings.append(f"dropped a tool without a name or path: {t.get('name')!r}")
            continue
        if name in seen:
            plan.warnings.append(f"dropped duplicate tool {name}")
            continue
        seen.add(name)
        fields: list[dict[str, Any]] = []
        for f in t.get("fields") or []:
            if not isinstance(f, dict) or not _s(f.get("name")):
                continue
            src = _s(f.get("source")) or "model"
            if src.startswith("call_variable:"):
                if src.split(":", 1)[1].strip() not in vars_:
                    src = "model"
            elif not (src.startswith("constant:") or src in ("model", "previous_tool_output")):
                src = "model"
            ftype = _s(f.get("type")).lower()
            enum = [x for x in (f.get("enum") or []) if isinstance(x, (str, int, float))] if isinstance(f.get("enum"), list) else []
            fields.append({"name": _s(f["name"]), "type": ftype if ftype in FIELD_TYPES else "string",
                           "description": _s(f.get("description")), "source": src,
                           "required": f.get("required") is not False, "enum": enum})
        plan.tools.append({
            "name": name, "description_for_model": _s(t.get("description_for_model")),
            "method": (_s(t.get("method")) or "POST").upper(), "path": path, "fields": fields,
            "url": tool_url(plan.base_address, path, server_url),
            "response_fields_that_matter": [x for x in (t.get("response_fields_that_matter") or []) if isinstance(x, str)],
            "errors": [e for e in (t.get("errors") or []) if isinstance(e, dict)],
            "side_effects": _s(t.get("side_effects")) or "none", "notes": _s(t.get("notes")),
        })
    if not server_url:
        plan.warnings.append("WARNING: no --server-url given; tools call the contract's own address " + plan.base_address)
    return plan


def py_literal(obj: Any) -> str:
    """Python source for a JSON value that never contains a doubled brace."""
    if isinstance(obj, dict):
        return "{ " + ", ".join(f"{py_literal(str(k))}: {py_literal(v)}" for k, v in obj.items()) + " }" if obj else "dict()"
    if isinstance(obj, list):
        return "[" + ", ".join(py_literal(v) for v in obj) + "]"
    if isinstance(obj, str):
        return repr(obj.replace("{{", "{\u200b{").replace("}}", "}\u200b}"))
    return repr(obj)


def envelope_hook(wrapper_key: str, extra: dict[str, Any]) -> str:
    return "\n".join([
        "try:",
        "    _body = config.get('body')",
        f"    if isinstance(_body, dict) and {py_literal(wrapper_key)} not in _body:",
        f"        _wrapped = {{ {py_literal(wrapper_key)}: _body }}",
        f"        _wrapped.update({py_literal(extra)})",
        "        config['body'] = _wrapped",
        "except Exception:",
        "    pass",
        "",
    ])


def make_tool(t: dict[str, Any], plan: ToolPlan) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in t["fields"]:
        leaf: dict[str, Any] = {"type": f["type"]}
        if f["description"]:
            leaf["description"] = f["description"]
        if f["enum"]:
            leaf["enum"] = f["enum"]
        src = f["source"]
        if src.startswith("call_variable:"):
            leaf["default"] = placeholder(src.split(":", 1)[1].strip())
        elif src.startswith("constant:"):
            leaf["default"] = src.split(":", 1)[1]
        else:
            leaf["default"] = placeholder(f["name"])
        props[f["name"]] = leaf
        if f["required"]:
            required.append(f["name"])
    read_only = t["side_effects"].lower().startswith("none")
    desc = t["description_for_model"] or "calls the service " + t["name"]
    tool = {
        "type": "function",
        "config": {"timeoutMs": TOOL_TIMEOUT_MS, "maxRetries": 1 if read_only else 0},
        "function": {
            "name": t["name"], "handler": API_CALL_HANDLER,
            "description": tools_gen.DEFAULT_DESCRIPTION_PREFIX + desc,
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "default": t["url"]},
                "method": {"type": "string", "default": t["method"]},
                "headers": {"type": "object", "properties": {
                    "Content-Type": {"type": "string", "default": "application/json"},
                    "Accept": {"type": "string", "default": "application/json"}}},
                "body": {"type": "object", "properties": props, "required": required},
            }},
        },
    }
    if plan.wrapper_key:
        tools_gen.attach_hooks(tool, pre=envelope_hook(plan.wrapper_key, plan.extra))
    return tool


def make_tools(plan: ToolPlan) -> list[dict[str, Any]]:
    out = [make_tool(t, plan) for t in plan.tools]
    for tool, t in zip(out, plan.tools):
        hits = tools_gen.check_default_collisions(tool, set(plan.var_names()))
        if hits:
            plan.warnings.append(f"tool {t['name']}: model-filled fields share a call-variable name: {hits}")
    return out


def finalize_descriptions(tools: list[dict[str, Any]], policy: dict[str, Any]) -> None:
    """Append 'Only after: ...' preconditions from the policy flow steps to each tool description."""
    pre: dict[str, list[str]] = {}
    for step in policy.get("flow_steps") or []:
        if isinstance(step, dict) and isinstance(step.get("call_tool"), str):
            pre.setdefault(step["call_tool"], []).extend(p for p in (step.get("preconditions") or []) if isinstance(p, str))
    for tool in tools:
        fn = tool["function"]
        base = fn["description"].split(" Only after: ")[0]
        items = pre.get(fn["name"])
        fn["description"] = base + (" Only after: " + "; ".join(items) + "." if items else "")
