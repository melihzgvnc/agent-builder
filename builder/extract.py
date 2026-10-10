"""Spec extractor: briefs (+ tool API document) -> PolicySpec dict, via an LLM.

Never drops input: if the model is unreachable, the key is missing, validation never passes or the
budget runs out, a minimal valid spec is built deterministically (all brief text as verbatim rules)
and a loud warning is returned, so the build can continue.
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from builder.llm import LLMConfigError, LLMError, LLMBudgetExceeded, LLMClient
from builder.spec import load_schema, validate_spec

PROMPT_DIR: Path = Path(__file__).with_name("extract_prompts")
STAGES: tuple[str, ...] = ("opening", "third_party", "identity", "debt", "negotiate", "closing", "global")
DEFAULT_BUDGET_S = 480.0
MAX_REPAIRS = 3
SPEC_MAX_TOKENS = 20000
SMALL_MAX_TOKENS = 6000
MODES: tuple[str, ...] = ("auto", "json")  # forced tool choice is rejected by some providers


class ExtractError(Exception):
    """Programmer error (bad arguments, unreadable input). Names the stage and the input."""


class _Degrade(Exception):
    """Internal: the LLM path cannot produce a valid spec; use the deterministic fallback."""


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _norm(text: str) -> str:
    t = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" .,;:!?\"'`")


def parse_json(text: str) -> Any:
    """Parse JSON from model text: strips code fences, finds the outermost object or array."""
    t = text.strip()
    fence = re.search(r"```(?:json|JSON)?\s*\n?(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    for start in sorted(starts):
        opener = t[start]
        closer = "}" if opener == "{" else "]"
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("no JSON object found in model output")


def _strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value if v is not None]
    return value


def _leaf_texts(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _leaf_texts(v, out)
    elif isinstance(value, list):
        for v in value:
            _leaf_texts(v, out)
    elif value is not None:
        out.append(str(value).lower() if isinstance(value, bool) else str(value))


def _spec_text(spec: Any) -> str:
    parts: list[str] = []
    _leaf_texts(spec, parts)
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------
# Tool API document helpers
# ---------------------------------------------------------------------------------------------


def _op_schemas(doc: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    """operation name -> (summary, request schema, response schema)."""
    ops: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    paths = doc.get("paths") if isinstance(doc, dict) else None
    if not isinstance(paths, dict):
        return ops
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in ("post", "put", "get", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            req: dict[str, Any] = {}
            body = (op.get("requestBody") or {}).get("content", {})
            for ct in body.values():
                req = ct.get("schema", {}) or {}
                break
            resp: dict[str, Any] = {}
            ok = (op.get("responses") or {}).get("200") or {}
            for ct in (ok.get("content") or {}).values():
                resp = ct.get("schema", {}) or {}
                break
            ops[str(path).strip("/")] = (str(op.get("summary") or op.get("description") or ""), req, resp)
            break
    return ops


def _flatten(schema: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """(dotted name, property schema) for every property, descending into objects and arrays."""
    out: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(schema, dict):
        return out
    if schema.get("type") == "array":
        return _flatten(schema.get("items"), prefix)
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            if not isinstance(sub, dict):
                continue
            full = f"{prefix}{name}"
            out.append((full, sub))
            out.extend(_flatten(sub, full + "."))
    return out


def openapi_summary(doc: dict[str, Any]) -> str:
    lines = ["TOOL API (operation: field (type) [allowed values] - note):"]
    for name, (summary, req, resp) in _op_schemas(doc).items():
        lines.append(f"- {name}: {summary}")
        for label, sch in (("request", req), ("response", resp)):
            for fname, sub in _flatten(sch):
                enum = sub.get("enum")
                desc = str(sub.get("description") or "")[:240]
                bit = f"    {label} {fname} ({sub.get('type', '?')})"
                if enum:
                    bit += " [" + ", ".join(str(e) for e in enum) + "]"
                if desc:
                    bit += f" - {desc}"
                lines.append(bit)
    return "\n".join(lines)


def openapi_enums(doc: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """operation -> field (last name part) -> allowed values, for request and response fields."""
    enums: dict[str, dict[str, list[str]]] = {}
    for name, (_s, req, resp) in _op_schemas(doc).items():
        for sch in (req, resp):
            for fname, sub in _flatten(sch):
                if isinstance(sub.get("enum"), list):
                    enums.setdefault(name, {})[fname.split(".")[-1]] = [str(e) for e in sub["enum"]]
    return enums


def openapi_field_names(doc: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for _n, (_s, req, resp) in _op_schemas(doc).items():
        for sch in (req, resp):
            names.update(f.split(".")[-1] for f, _ in _flatten(sch))
    return names


# ---------------------------------------------------------------------------------------------
# Deterministic coverage check
# ---------------------------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_UNITS = (
    r"percent|per cent|business days?|calendar days?|days?|hours?|hrs?|minutes?|mins?|seconds?|"
    r"times|attempts?|weeks?|months?|years?|digits?|characters?|asks?|tries|refusals?|products?"
)
_NUM_RE = re.compile(rf"(?<![\w.:-])(\d+(?:[.,]\d+)?)\s*(?:%|(?:{_UNITS})\b)", re.I)
_TIME_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?![\d:])")
_QUOTE_RE = re.compile(r'"([^"\n]+)"|\u201c([^\u201d\n]+)\u201d')
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_TICK_RE = re.compile(r"`([A-Za-z][\w-]{2,})`")


def _number_present(num: str, text: str) -> bool:
    n = num.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", num) else num.replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(n)}(?![\d]|\.\d)", text) is not None


def deterministic_coverage(texts: list[str], spec: Any) -> list[dict[str, str]]:
    """Tokens found in the brief text that appear nowhere in the spec. No LLM involved."""
    spec_text = _spec_text(spec)
    spec_norm = _norm(spec_text)
    spec_plain = spec_text.replace(",", "")
    seen: set[tuple[str, str]] = set()
    missing: list[dict[str, str]] = []

    def add(kind: str, token: str) -> None:
        if (kind, token) not in seen:
            seen.add((kind, token))
            missing.append({"kind": kind, "token": token})

    for doc in texts:
        for m in _DATE_RE.finditer(doc):
            if m.group(0) not in spec_text:
                add("date", m.group(0))
        masked = _DATE_RE.sub(" ", doc)
        for m in _NUM_RE.finditer(masked):
            if not _number_present(m.group(1), spec_plain):
                add("number", m.group(0).strip())
        for m in _TIME_RE.finditer(masked):
            hh, mm = m.group(0).split(":")
            if not (m.group(0) in spec_text or f"{int(hh):02d}:{mm}" in spec_text):
                add("time", m.group(0))
        for m in _QUOTE_RE.finditer(doc):
            q = m.group(1) or m.group(2)
            if len(q.split()) >= 2 or len(q) >= 15:
                if _norm(q) not in spec_norm:
                    add("quote", q.strip())
        for m in _SNAKE_RE.finditer(doc):
            if m.group(0) not in spec_text.lower():
                add("identifier", m.group(0))
        for m in _TICK_RE.finditer(doc):
            if m.group(1).lower() not in spec_text.lower():
                add("identifier", m.group(1))
    return missing


# ---------------------------------------------------------------------------------------------
# Structured calls
# ---------------------------------------------------------------------------------------------


class _Asker:
    """Gets one JSON object from the model: tool call first, plain JSON text as fallback."""

    def __init__(self, llm: Any, deadline: float, per_call_s: float = 240.0, mode: str = "auto") -> None:
        self.llm = llm
        self.deadline = deadline
        self.per_call_s = per_call_s
        self.mode = mode
        self.calls = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def ask(
        self,
        purpose: str,
        system: str,
        user: str,
        tool_name: str,
        schema: dict[str, Any],
        max_tokens: int = SMALL_MAX_TOKENS,
    ) -> dict[str, Any]:
        last_err = "no attempt"
        for _ in range(3):
            if self.remaining() <= 1:
                raise LLMBudgetExceeded(f"extraction budget exhausted before '{purpose}'")
            kwargs: dict[str, Any] = {}
            sys_text = system
            if self.mode == "json":
                sys_text += (
                    "\n\nReply with exactly one JSON object and nothing else. It must follow this JSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False)
                )
            else:
                kwargs["tools"] = [
                    {"name": tool_name, "description": "Submit the result.", "input_schema": schema}
                ]
                kwargs["tool_choice"] = {"type": "auto"}
                sys_text += f"\n\nReturn the result by calling the tool `{tool_name}` exactly once. Write no other text."
            try:
                res = self.llm.call(
                    purpose,
                    sys_text,
                    [{"role": "user", "content": user}],
                    max_tokens=max_tokens,
                    timeout_s=max(5.0, min(self.per_call_s, self.remaining())),
                    **kwargs,
                )
            except LLMError as e:
                if e.status == 400 and self.mode != "json" and not isinstance(e, LLMBudgetExceeded):
                    self.mode = "json"
                    last_err = f"tool mode rejected: {e}"
                    continue
                raise
            self.calls += 1
            if res.tool_inputs and res.tool_inputs[0]:
                return res.tool_inputs[0]
            try:
                parsed = parse_json(res.text)
            except ValueError:
                trunc = " (output truncated at max_tokens)" if res.stop_reason == "max_tokens" else ""
                last_err = f"no usable JSON in reply{trunc}"
                self.mode = "json"
                continue
            if isinstance(parsed, list):
                parsed = {"items": parsed}
            if isinstance(parsed, dict):
                return parsed
            last_err = "reply was not a JSON object"
        raise _Degrade(f"'{purpose}': {last_err}")


def _tool_schema() -> dict[str, Any]:
    schema = copy.deepcopy(load_schema())
    schema.pop("$schema", None)
    schema.pop("$id", None)
    return schema


def _normalize(raw: Any, sources: list[tuple[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
    """Mechanical clean-up only: stringified sub-objects, nulls, schema_version, sources."""
    if not isinstance(raw, dict):
        return {}
    spec = dict(raw)
    props = schema.get("properties", {})
    for key, val in list(spec.items()):
        want = props.get(key, {}).get("type") if isinstance(props.get(key), dict) else None
        if isinstance(val, str) and (want in ("object", "array") or "$ref" in props.get(key, {})):
            try:
                spec[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    spec = _strip_nulls(spec)
    spec.setdefault("schema_version", "1")
    meta = spec.get("meta")
    if isinstance(meta, dict) and "sources" not in meta:
        meta["sources"] = [
            {"path": name, "order": i + 1, "role": "base" if i == 0 else "layer"}
            for i, (name, _t) in enumerate(sources)
        ]
    return spec


def _docs_block(sources: list[tuple[str, str]]) -> str:
    n = len(sources)
    parts = [
        f"DOCUMENTS ({n}, in order; later documents build on and override earlier ones):"
    ]
    for i, (name, text) in enumerate(sources, 1):
        parts.append(f"\n=== DOCUMENT {i} of {n}: {name} ===\n{text.strip()}\n=== END OF DOCUMENT {i} ===")
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------
# Minimal fallback spec
# ---------------------------------------------------------------------------------------------


def minimal_spec(sources: list[tuple[str, str]], openapi: dict[str, Any] | None) -> dict[str, Any]:
    """Valid spec built without any model: all brief text as global verbatim rules."""
    rules: list[dict[str, str]] = []
    for name, text in sources:
        n = 0
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para:
                n += 1
                rules.append({"stage": "global", "text": para, "source_ref": f"{name}#p{n}"})
    enums = openapi_enums(openapi or {})
    outcomes: list[str] = []
    for tool in ("close_call",):
        outcomes = enums.get(tool, {}).get("outcome", [])
    if not outcomes:
        for fields in enums.values():
            if "outcome" in fields:
                outcomes = fields["outcome"]
                break
    table = [
        {"outcome": o, "when": "unknown: extraction fell back, see verbatim_rules"} for o in outcomes
    ] or [{"outcome": "unknown", "when": "unknown: extraction fell back, see verbatim_rules"}]
    names = openapi_field_names(openapi or {})
    return {
        "schema_version": "1",
        "meta": {
            "bank_name": "unknown",
            "agent_identity": "unknown",
            "sources": [
                {"path": name, "order": i + 1, "role": "base" if i == 0 else "layer"}
                for i, (name, _t) in enumerate(sources)
            ],
        },
        "call_variables": {"customer_id": "unknown", "now": "unknown"},
        "account_fields": {"product_id": "product_id" if "product_id" in names else "unknown"},
        "calendar": {"weekend_days": []},
        "bands": [
            {
                "name": "unknown",
                "days_late_min": 0,
                "days_late_max": 0,
                "deadline": {"kind": "calendar_days_after_today", "n": 0},
                "persuasion_attempts": 0,
            }
        ],
        "closing": {"outcome_table": table},
        "verbatim_rules": rules,
    }


# ---------------------------------------------------------------------------------------------
# openapi grounding
# ---------------------------------------------------------------------------------------------

_KIND_FIELD: dict[str, tuple[str, str]] = {
    "identity_field": ("verify_identity", "field"),
    "note_reason": ("add_note", "reason"),
    "queue": ("transfer_to_agent", "queue"),
    "close_outcome": ("close_call", "outcome"),
    "close_reason": ("close_call", "reason"),
}


def _allowed_for(
    enums: dict[str, dict[str, list[str]]], tool: str, fld: str, ops: set[str] | None = None
) -> list[str] | None:
    """Allowed values for tool.field; other tools are searched only when `tool` is not an operation."""
    if fld in enums.get(tool, {}):
        return enums[tool][fld]
    if ops is not None and tool in ops:
        return None
    for fields in enums.values():
        if fld in fields:
            return fields[fld]
    return None


Usage = tuple[tuple[Any, ...], str, str]  # (path, kind, value)


def _walk_usages(node: Any, path: tuple[Any, ...], out: list[Usage], ctx: dict[str, Any]) -> None:
    if isinstance(node, list):
        for i, v in enumerate(node):
            if path and path[-1] == "reasons" and isinstance(v, str):
                out.append(((*path, i), "note_reason", v))
            else:
                _walk_usages(v, (*path, i), out, ctx)
        return
    if not isinstance(node, dict):
        return
    top = path[0] if path else None
    if top in ("verbatim_rules", "mandated_sentences", "coverage", "warnings", "meta", "tools"):
        return
    atype = node.get("type") if isinstance(node.get("type"), str) else None
    for key, val in node.items():
        here = (*path, key)
        if isinstance(val, str):
            if key == "field" and len(path) >= 2 and path[-2] == "questions":
                out.append((here, "identity_field", val))
            elif key in ("outcome", "transfer_outcome", "promise_outcome"):
                out.append((here, "close_outcome", val))
            elif key == "reason":
                if atype == "add_note":
                    out.append((here, "note_reason", val))
                elif "outcome" in node or atype == "close":
                    out.append((here, "close_reason", val))
            elif key in ("note_reason", "excluded_reason"):
                out.append((here, "note_reason", val))
            elif key == "queue" and atype in ("transfer", "offer_transfer", None):
                out.append((here, "queue", val))
        else:
            _walk_usages(val, here, out, ctx)


def collect_bad_values(spec: dict[str, Any], doc: dict[str, Any]) -> tuple[list[Usage], dict[Usage, list[str]], list[str]]:
    """(bad usages, usage -> allowed list, warnings for names with no operation)."""
    enums = openapi_enums(doc)
    ops = set(_op_schemas(doc))
    usages: list[Usage] = []
    _walk_usages(spec, (), usages, {})
    bad: list[Usage] = []
    allowed_by: dict[Usage, list[str]] = {}
    for u in usages:
        tool, fld = _KIND_FIELD[u[1]]
        allowed = _allowed_for(enums, tool, fld, ops)
        if allowed is not None and u[2] not in allowed:
            bad.append(u)
            allowed_by[u] = allowed
    tools = spec.get("tools") if isinstance(spec.get("tools"), dict) else {}
    for tname, override in tools.items():
        if ops and tname not in ops:
            continue
        av = override.get("allowed_values") if isinstance(override, dict) else None
        if isinstance(av, dict):
            for fld, values in av.items():
                allowed = _allowed_for(enums, tname, fld, ops)
                if allowed is None or not isinstance(values, list):
                    continue
                for i, v in enumerate(values):
                    if isinstance(v, str) and v not in allowed:
                        u = (("tools", tname, "allowed_values", fld, i), "allowed_value", v)
                        bad.append(u)
                        allowed_by[u] = allowed
    names = openapi_field_names(doc)
    af = spec.get("account_fields") if isinstance(spec.get("account_fields"), dict) else {}
    for key, val in af.items():
        if isinstance(val, str) and names and val not in names:
            u = (("account_fields", key), "account_field", val)
            bad.append(u)
            allowed_by[u] = sorted(names)
    warns = []
    for tname in tools:
        if ops and tname not in ops:
            warns.append(f"tool '{tname}' in spec.tools has no operation in the tool API")
    return bad, allowed_by, warns


def _set_path(spec: Any, path: tuple[Any, ...], value: str) -> None:
    node = spec
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value


# ---------------------------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------------------------


def _add_warning(warnings: list[str], trace: Any, msg: str) -> None:
    if msg not in warnings:
        warnings.append(msg)
        if trace is not None:
            trace.warn(msg)


def _merge_spec_warnings(spec: dict[str, Any], warnings: list[str]) -> None:
    existing = [w for w in spec.get("warnings", []) if isinstance(w, str)]
    merged = list(existing)
    for w in warnings:
        if w not in merged:
            merged.append(w)
    spec["warnings"] = merged


def _repair_user(docs: str, spec: Any, errors: list[str]) -> str:
    shown = errors[:80]
    more = f"\n(and {len(errors) - 80} more)" if len(errors) > 80 else ""
    return (
        f"{load_prompt('repair')}\n\nVALIDATION ERRORS (exact paths in your previous output):\n"
        + "\n".join(f"- {e}" for e in shown)
        + more
        + f"\n\nPREVIOUS OUTPUT:\n{json.dumps(spec, ensure_ascii=False)}\n\n{docs}"
    )


def _primary(asker: _Asker, system: str, user: str, docs: str, sources: list[tuple[str, str]],
             schema: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    raw = asker.ask("extract_spec", system, user, "submit_spec", schema, SPEC_MAX_TOKENS)
    spec = _normalize(raw, sources, schema)
    errors = validate_spec(spec)
    attempt = 0
    while errors and attempt < MAX_REPAIRS:
        attempt += 1
        report.setdefault("validation_errors", []).append(list(errors))
        raw = asker.ask(
            f"extract_repair_{attempt}", system, _repair_user(docs, spec, errors), "submit_spec", schema,
            SPEC_MAX_TOKENS,
        )
        spec = _normalize(raw, sources, schema)
        errors = validate_spec(spec)
    report["repairs"] = attempt
    if errors:
        report.setdefault("validation_errors", []).append(list(errors))
        report["last_invalid_spec"] = spec
        raise _Degrade(f"spec still invalid after {MAX_REPAIRS} repairs: {errors[:5]}")
    return spec


def _add_verbatim(spec: dict[str, Any], items: list[Any], warnings: list[str], trace: Any) -> int:
    rules = spec.setdefault("verbatim_rules", [])
    known = [_norm(r.get("text", "")) for r in rules if isinstance(r, dict)]
    known += [_norm(m.get("text", "")) for m in spec.get("mandated_sentences", []) if isinstance(m, dict)]
    added = 0
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            continue
        text = it["text"].strip()
        n = _norm(text)
        if not n or any(n == k or (k and n in k) for k in known):
            continue
        stage = it.get("stage") if it.get("stage") in STAGES else "global"
        rules.append({"stage": stage, "text": text, "source_ref": "coverage"})
        known.append(n)
        added += 1
        why = str(it.get("reason") or "not represented")[:120]
        _add_warning(warnings, trace, f"coverage: added verbatim rule ({stage}): {text[:100]} [{why}]")
    return added


def _coverage_pass(asker: _Asker, system: str, docs: str, spec: dict[str, Any],
                   missing: list[dict[str, str]], warnings: list[str], trace: Any, report: dict[str, Any]) -> None:
    user = (
        f"{load_prompt('coverage')}\n\n{docs}\n\nPOLICYSPEC:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
        "TOKENS NOT FOUND BY THE MECHANICAL CHECK:\n"
        + json.dumps(missing[:120], ensure_ascii=False)
    )
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "stage": {"type": "string", "enum": list(STAGES)},
                        "reason": {"type": "string"},
                    },
                    "required": ["text", "stage", "reason"],
                },
            }
        },
        "required": ["items"],
    }
    out = asker.ask("extract_coverage", "You audit policy specifications for completeness.", user,
                    "report_gaps", schema, SPEC_MAX_TOKENS // 2)
    items = out.get("items")
    items = items if isinstance(items, list) else []
    candidate = copy.deepcopy(spec)
    report["coverage_added"] = _add_verbatim(candidate, items, warnings, trace)
    if validate_spec(candidate):
        _add_warning(warnings, trace, "coverage: additions made the spec invalid; additions discarded")
        report["coverage_added"] = 0
        return
    spec.clear()
    spec.update(candidate)


def _grounding(asker: _Asker | None, spec: dict[str, Any], doc: dict[str, Any],
               warnings: list[str], trace: Any, report: dict[str, Any]) -> None:
    bad, allowed_by, warns = collect_bad_values(spec, doc)
    for w in warns:
        _add_warning(warnings, trace, "grounding: " + w)
    if not bad:
        return
    groups: dict[tuple[str, str], list[Usage]] = {}
    for u in bad:
        groups.setdefault((u[1], u[2]), []).append(u)
    items = [
        {"id": i, "kind": k, "value": v, "allowed": allowed_by[us[0]]}
        for i, ((k, v), us) in enumerate(groups.items())
    ]
    report["grounding_bad"] = [(k, v) for (k, v) in groups]
    mapping: dict[int, str | None] = {}
    if asker is not None and asker.remaining() > 15:
        schema = {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "replacement": {"type": ["string", "null"]}},
                        "required": ["id", "replacement"],
                    },
                }
            },
            "required": ["mappings"],
        }
        try:
            out = asker.ask(
                "extract_grounding", "You map values to the nearest allowed value.",
                f"{load_prompt('grounding')}\n\nITEMS:\n{json.dumps(items, ensure_ascii=False)}",
                "submit_mappings", schema,
            )
            for m in out.get("mappings", []) if isinstance(out.get("mappings"), list) else []:
                if isinstance(m, dict) and isinstance(m.get("id"), int):
                    mapping[m["id"]] = m.get("replacement") if isinstance(m.get("replacement"), str) else None
        except (LLMError, _Degrade) as e:
            _add_warning(warnings, trace, f"grounding: repair call failed ({type(e).__name__}); values left as is")
    candidate = copy.deepcopy(spec)
    changed = 0
    for i, ((kind, value), us) in enumerate(groups.items()):
        rep = mapping.get(i)
        if rep and rep in allowed_by[us[0]]:
            for path, _k, _v in us:
                _set_path(candidate, path, rep)
            changed += 1
            _add_warning(warnings, trace, f"grounding: {kind} '{value}' mapped to API value '{rep}'")
        else:
            _add_warning(
                warnings, trace,
                f"grounding: {kind} '{value}' is not in the tool API allowed values {allowed_by[us[0]]}; left unmatched",
            )
    if changed:
        if validate_spec(candidate):
            _add_warning(warnings, trace, "grounding: mapped values made the spec invalid; mappings discarded")
        else:
            spec.clear()
            spec.update(candidate)


def extract_from_texts(
    sources: list[tuple[str, str]],
    openapi: dict[str, Any] | None,
    llm: Any,
    trace: Any = None,
    budget_s: float = DEFAULT_BUDGET_S,
    report: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Core extraction over already-loaded (name, text) documents."""
    if not sources:
        raise ExtractError("extract_spec: no brief documents given")
    report = report if report is not None else {}
    warnings: list[str] = []
    t0 = time.monotonic()
    doc = openapi if isinstance(openapi, dict) else {}
    report.update({"fallback": False, "mode": None})

    def fallback(reason: str) -> tuple[dict[str, Any], list[str]]:
        spec = minimal_spec(sources, doc)
        _add_warning(
            warnings, trace,
            f"EXTRACTION FELL BACK to a minimal spec (all brief text kept as verbatim rules): {reason}",
        )
        report["fallback"] = True
        report["seconds"] = time.monotonic() - t0
        _merge_spec_warnings(spec, warnings)
        errs = validate_spec(spec)
        if errs:  # programmer error: the fallback itself must always validate
            raise ExtractError(f"extract_spec: fallback spec invalid for inputs {[n for n, _ in sources]}: {errs[:5]}")
        return spec, warnings

    if llm is None:
        return fallback("no LLM available (missing key or configuration)")
    if hasattr(llm, "set_budget"):
        llm.set_budget(budget_s)
    asker = _Asker(llm, t0 + budget_s)
    schema = _tool_schema()
    system = load_prompt("system") + "\n\n" + load_prompt("sections")
    docs = _docs_block(sources)
    user = f"{openapi_summary(doc)}\n\n{docs}\n\nProduce the PolicySpec now."
    try:
        spec = _primary(asker, system, user, docs, sources, schema, report)
    except (LLMError, _Degrade) as e:
        report["mode"] = asker.mode
        return fallback(f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 - never drop input
        report["mode"] = asker.mode
        return fallback(f"unexpected {type(e).__name__}: {e}")

    # Steps below enrich a valid spec; a failure keeps the spec as it is.
    try:
        missing = deterministic_coverage([t for _n, t in sources], spec)
        report["missing_tokens"] = missing
        if asker.remaining() > 20:
            try:
                _coverage_pass(asker, system, docs, spec, missing, warnings, trace, report)
            except (LLMError, _Degrade) as e:
                _add_warning(warnings, trace, f"coverage: LLM pass failed ({type(e).__name__}: {e}); missing tokens: {len(missing)}")
        else:
            _add_warning(warnings, trace, "coverage: LLM pass skipped (budget)")
        left = deterministic_coverage([t for _n, t in sources], spec)
        report["missing_after"] = left
        if left:
            _add_warning(warnings, trace, f"coverage: {len(left)} brief token(s) still not found in the spec")
    except Exception as e:  # noqa: BLE001
        _add_warning(warnings, trace, f"coverage: check crashed ({type(e).__name__}: {e})")
    try:
        _grounding(asker, spec, doc, warnings, trace, report)
    except Exception as e:  # noqa: BLE001
        _add_warning(warnings, trace, f"grounding: check crashed ({type(e).__name__}: {e})")

    report["mode"] = asker.mode
    report["seconds"] = time.monotonic() - t0
    _merge_spec_warnings(spec, warnings)
    for w in spec.get("warnings", []):
        if w not in warnings:
            warnings.append(w)
            if trace is not None:
                trace.warn(f"spec: {w}")
    errs = validate_spec(spec)
    if errs:  # warnings merge cannot break validity; if it ever does, never drop input
        return fallback(f"final spec invalid: {errs[:3]}")
    return spec, warnings


def extract_spec(
    brief_paths: list[Path | str],
    openapi: dict[str, Any] | Path | str,
    llm: Any,
    trace: Any = None,
    budget_s: float = DEFAULT_BUDGET_S,
    report: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Read the brief files (layer order) and the tool API document, return (spec, warnings)."""
    if not brief_paths:
        raise ExtractError("extract_spec: at least one brief path is required")
    sources: list[tuple[str, str]] = []
    for p in brief_paths:
        try:
            sources.append((Path(p).name, Path(p).read_text(encoding="utf-8")))
        except OSError as e:
            raise ExtractError(f"extract_spec: cannot read brief {p}: {e}") from e
    if isinstance(openapi, (str, Path)):
        try:
            openapi = json.loads(Path(openapi).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ExtractError(f"extract_spec: cannot read tool API document {openapi}: {e}") from e
    return extract_from_texts(sources, openapi, llm, trace, budget_s, report)


def make_llm(trace: Any = None) -> Any:
    """Real client from the environment, or None when no key/model is configured."""
    try:
        return LLMClient(trace=trace)
    except LLMConfigError:
        return None


def _notes_text(spec: dict[str, Any], warnings: list[str], report: dict[str, Any], llm: Any) -> str:
    lines = ["# Extraction notes", ""]
    lines.append(f"fallback: {report.get('fallback')}")
    lines.append(f"mode: {report.get('mode')}")
    lines.append(f"repairs: {report.get('repairs', 0)}")
    lines.append(f"seconds: {report.get('seconds', 0):.1f}")
    lines.append(
        f"tokens: in={getattr(llm, 'total_input_tokens', 0)} out={getattr(llm, 'total_output_tokens', 0)} "
        f"calls={getattr(llm, 'calls', 0)}"
    )
    lines.append(f"verbatim_rules: {len(spec.get('verbatim_rules', []))}")
    lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings] if warnings else ["", "No warnings."]
    for key in ("missing_tokens", "missing_after"):
        items = report.get(key) or []
        lines += ["", f"## {key} ({len(items)})", ""] + [f"- {i['kind']}: {i['token']}" for i in items]
    return "\n".join(lines) + "\n"


def stage_extract(ctx: Any) -> str:
    """Pipeline stage: ctx brief paths and texts + ctx.openapi_doc -> ctx.spec (+ spec.json, notes)."""
    names = [Path(b).name for b in ctx.briefs]
    texts = list(ctx.brief_texts)
    if len(texts) != len(names):
        raise ExtractError(f"stage extract_spec: briefs not loaded for inputs {[str(b) for b in ctx.briefs]}")
    llm = make_llm(ctx.trace)
    report: dict[str, Any] = {}
    spec, warnings = extract_from_texts(
        list(zip(names, texts)), ctx.openapi_doc, llm, ctx.trace, DEFAULT_BUDGET_S, report
    )
    ctx.spec = spec
    ctx.results["extraction"] = {
        "fallback": report.get("fallback"),
        "mode": report.get("mode"),
        "warnings": len(warnings),
        "input_tokens": getattr(llm, "total_input_tokens", 0),
        "output_tokens": getattr(llm, "total_output_tokens", 0),
        "seconds": round(report.get("seconds", 0.0), 1),
    }
    Path(ctx.run_dir).mkdir(parents=True, exist_ok=True)
    (Path(ctx.run_dir) / "spec.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (Path(ctx.run_dir) / "extraction_notes.md").write_text(
        _notes_text(spec, warnings, report, llm), encoding="utf-8"
    )
    kind = "FALLBACK minimal spec" if report.get("fallback") else "spec extracted"
    return f"{kind}; {len(warnings)} warning(s); verbatim_rules={len(spec.get('verbatim_rules', []))}"
