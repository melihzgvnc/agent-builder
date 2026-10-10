"""Tool definitions generated from an OpenAPI document.

Pure functions over dicts. Everything about a tool (name, url, method, body fields, types,
enums, required list) comes from the openapi document passed in; nothing here lists tool names
or fields. The output has the shape documented in docs/tools-and-code.md ("Tool function
shape"): every body field is a leaf under body.properties.<field> with its own `default`.
"""

from __future__ import annotations

import copy
from typing import Any

API_CALL_HANDLER: str = "__function__:api_call"
END_CALL_HANDLER: str = "__function__:end_call"
METHODS: tuple[str, ...] = ("post", "put", "patch", "delete", "get")
LIFECYCLE_POINTS: frozenset[str] = frozenset(("start_of_call", "end_of_call"))
DEFAULT_DESCRIPTION_PREFIX: str = "CALL it when the instructions say so: "
LEAF_KEYS: tuple[str, ...] = ("type", "description", "enum", "format")
OPEN_BRACES: str = "{" + "{"
CLOSE_BRACES: str = "}" + "}"


class ToolGenError(ValueError):
    """The openapi document cannot produce the requested tool or hook."""


def placeholder(name: str) -> str:
    """The template text for a variable: two opening braces, the name, two closing braces."""
    return OPEN_BRACES + name + CLOSE_BRACES


def has_double_braces(source: str) -> bool:
    """True when the text contains a doubled opening or closing brace."""
    return OPEN_BRACES in source or CLOSE_BRACES in source


def tool_name_for_path(path: str) -> str:
    """Last non-empty segment of an operation path."""
    parts = [p for p in str(path).split("/") if p]
    if not parts:
        raise ToolGenError(f"cannot derive a tool name from path {path!r}")
    return parts[-1]


def operation_paths(openapi: dict) -> list[str]:
    """Every path of the document that has at least one operation, in document order."""
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        return []
    return [p for p, item in paths.items() if isinstance(item, dict) and _first_method(item)]


def tool_names(openapi: dict) -> list[str]:
    """Tool names of every operation in the document, in document order."""
    return [tool_name_for_path(p) for p in operation_paths(openapi)]


def path_for_tool(openapi: dict, name: str) -> str:
    """The operation path whose last segment is `name`."""
    for p in operation_paths(openapi):
        if tool_name_for_path(p) == name:
            return p
    raise ToolGenError(f"openapi has no operation for tool {name!r}")


def _first_method(path_item: dict) -> str | None:
    for m in METHODS:
        if isinstance(path_item.get(m), dict):
            return m
    return None


def _resolve_ref(openapi: dict, schema: Any) -> Any:
    """Follow a local '#/...' $ref (a few levels); anything else is returned unchanged."""
    for _ in range(8):
        if not isinstance(schema, dict) or not isinstance(schema.get("$ref"), str):
            return schema
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return schema
        node: Any = openapi
        for part in ref[2:].split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        schema = node
    return schema


def body_schema(openapi: dict, operation: dict) -> dict:
    """JSON schema of the operation's JSON request body ({} when it has none)."""
    body = _resolve_ref(openapi, operation.get("requestBody"))
    content = body.get("content") if isinstance(body, dict) else None
    media = content.get("application/json") if isinstance(content, dict) else None
    schema = _resolve_ref(openapi, media.get("schema") if isinstance(media, dict) else None)
    return schema if isinstance(schema, dict) else {}


def _leaf(openapi: dict, name: str, field_schema: Any, default: str) -> dict:
    spec = _resolve_ref(openapi, field_schema)
    spec = spec if isinstance(spec, dict) else {}
    leaf: dict[str, Any] = {"type": spec.get("type") if isinstance(spec.get("type"), str) else "string"}
    for key in LEAF_KEYS[1:]:
        if key in spec:
            leaf[key] = copy.deepcopy(spec[key])
    leaf["default"] = default
    return leaf


def build_tool(
    openapi: dict,
    path: str,
    baked: dict[str, str] | None = None,
    max_retries: int = 0,
    timeout_ms: int = 10000,
    description: str | None = None,
) -> dict:
    """One Freya api_call tool for the operation at `path`.

    baked maps body field name -> variable placeholder text for fields the workflow fills; keys
    that are not body fields of this operation are ignored. Every other body field is
    model-filled: its default is a placeholder named after the field. description replaces the
    operation summary; without it the summary is used behind DEFAULT_DESCRIPTION_PREFIX.
    """
    paths = openapi.get("paths")
    item = paths.get(path) if isinstance(paths, dict) else None
    method = _first_method(item) if isinstance(item, dict) else None
    if item is None or method is None:
        raise ToolGenError(f"openapi has no operation at path {path!r}")
    servers = openapi.get("servers")
    base = servers[0].get("url") if isinstance(servers, list) and servers and isinstance(servers[0], dict) else None
    if not isinstance(base, str) or not base:
        raise ToolGenError("openapi has no servers[0].url")
    operation = item[method]
    schema = body_schema(openapi, operation)
    fields = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    baked = baked or {}
    props = {
        name: _leaf(openapi, name, spec, baked[name] if name in baked else placeholder(name))
        for name, spec in fields.items()
    }
    required = [r for r in (schema.get("required") or []) if isinstance(r, str) and r in props]
    if description is None:
        summary = operation.get("summary") or operation.get("description") or ""
        description = DEFAULT_DESCRIPTION_PREFIX + str(summary).strip()
    return {
        "type": "function",
        "config": {"timeoutMs": timeout_ms, "maxRetries": max_retries},
        "function": {
            "name": tool_name_for_path(path),
            "handler": API_CALL_HANDLER,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": base.rstrip("/") + "/" + path.lstrip("/")},
                    "method": {"type": "string", "default": method.upper()},
                    "headers": {
                        "type": "object",
                        "properties": {"Content-Type": {"type": "string", "default": "application/json"}},
                    },
                    "body": {"type": "object", "properties": props, "required": required},
                },
            },
        },
    }


def build_tools(openapi: dict, options: dict[str, dict] | None = None) -> dict[str, dict]:
    """build_tool for every operation; options maps tool name -> keyword arguments."""
    options = options or {}
    out: dict[str, dict] = {}
    for path in operation_paths(openapi):
        name = tool_name_for_path(path)
        out[name] = build_tool(openapi, path, **options.get(name, {}))
    return out


def body_properties(tool: dict) -> dict:
    """The body.properties object of a tool ({} when missing)."""
    params = (tool.get("function") or {}).get("parameters") or {}
    body = (params.get("properties") or {}).get("body") or {}
    props = body.get("properties")
    return props if isinstance(props, dict) else {}


def check_default_collisions(tool: dict, reserved_vars: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Model-filled fields whose placeholder name is a variable the compiler sets.

    Such a field would be baked silently and the model would never see it. Returns the field
    names, in body order.
    """
    reserved = set(reserved_vars)
    hits = []
    for name, leaf in body_properties(tool).items():
        if isinstance(leaf, dict) and leaf.get("default") == placeholder(name) and name in reserved:
            hits.append(name)
    return hits


def _require_api_call(tool: dict, what: str) -> None:
    handler = (tool.get("function") or {}).get("handler")
    if handler != API_CALL_HANDLER:
        raise ToolGenError(f"{what} only works on {API_CALL_HANDLER} tools, not {handler!r}")


def attach_hooks(tool: dict, pre: str | None = None, post: str | None = None) -> dict:
    """Set pre_process_code / post_process_code on an api_call tool (same dict is returned)."""
    _require_api_call(tool, "hooks")
    for key, source in (("pre_process_code", pre), ("post_process_code", post)):
        if source is None or not source.strip():
            continue
        if has_double_braces(source):
            raise ToolGenError(f"{key} contains a doubled brace")
        tool[key] = source
    return tool


def attach_response_mapping(tool: dict, mappings: list[dict[str, str]]) -> dict:
    """Set the tool-level response_mapping; every path must start with 'data.'."""
    out = []
    for m in mappings:
        path, variable = m.get("path"), m.get("variable")
        if not isinstance(path, str) or not path.startswith("data.") or not variable:
            raise ToolGenError(f"bad response_mapping entry {m!r}: path must start with 'data.'")
        out.append({"path": path, "variable": variable})
    tool["response_mapping"] = out
    return tool


def lifecycle(tool: dict, when: list[str] | None = None) -> dict:
    """Make the tool a lifecycle tool (config.alwaysRunsAt); default: end of call."""
    points = list(when) if when is not None else ["end_of_call"]
    if not points or any(p not in LIFECYCLE_POINTS for p in points):
        raise ToolGenError(f"alwaysRunsAt must be a non-empty subset of {sorted(LIFECYCLE_POINTS)}")
    tool.setdefault("config", {})["alwaysRunsAt"] = points
    return tool


def post_hook_source(next_if_ok: str, next_if_failed: str) -> str:
    """Safe post-hook source: drop headers, add result['data']['what_to_do_next'].

    The sentence depends on whether result['data'].get('ok') is true. Both strings come from
    the caller. The code is wrapped in try/except and checks types before use.
    """
    source = "\n".join(
        [
            "try:",
            "    if isinstance(result, dict):",
            "        result.pop('header', None)",
            "        data = result.get('data')",
            "        if isinstance(data, dict):",
            "            ok = data.get('ok')",
            "            if ok is True or str(ok).lower() == 'true':",
            "                data['what_to_do_next'] = " + repr(str(next_if_ok)),
            "            else:",
            "                data['what_to_do_next'] = " + repr(str(next_if_failed)),
            "except Exception:",
            "    pass",
            "",
        ]
    )
    if has_double_braces(source):
        raise ToolGenError("post hook text contains a doubled brace")
    return source


def end_call_node(name: str, spoken_message: str | None = None, terminal_name: str | None = None) -> dict:
    """An end_call node. Silent hang-up unless spoken_message is given.

    terminal_name is the terminal function name (the call's ended reason); default: `name`.
    """
    props: dict[str, Any] = {"message_mode": {"type": "string", "default": "none"}}
    if spoken_message is not None:
        props["message_mode"]["default"] = "custom"
        props["spoken_message"] = {"type": "string", "default": spoken_message}
    return {
        "name": name,
        "node_type": "end_call",
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "transitions": [],
        "terminal": {
            "type": "function",
            "function": {
                "name": terminal_name or name,
                "handler": END_CALL_HANDLER,
                "parameters": {"type": "object", "properties": props},
            },
        },
    }
