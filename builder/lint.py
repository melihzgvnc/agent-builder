"""Workflow linter: the checklist of docs/tools-and-code.md and docs/workflow-format.md.

lint(workflow) -> list[Finding]. Pure: it reads the workflow dict and nothing else.
CLI: python -m builder.lint <workflow.json>  (bare workflow, or an export with a 'workflow' key)
prints one line per finding and exits 1 when any finding is an error (2 when unreadable).
"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

API_CALL: str = "__function__:api_call"
CODE_EXEC: str = "__function__:code_exec"
END_CALL: str = "__function__:end_call"
OPEN_BRACES: str = "{" + "{"
CLOSE_BRACES: str = "}" + "}"
BODY_TOP_KEYS: frozenset[str] = frozenset(("url", "method", "headers", "body", "connect_retries"))
PREVIOUS: str = "__previous__"
TRUE_SPELLINGS: tuple[str, str] = ("true", "True")
DATE_NAME_RE: re.Pattern[str] = re.compile(r"(^|_)(date|dob|birthday)($|_)", re.IGNORECASE)
DATE_TEXT_RE: re.Pattern[str] = re.compile(r"\b(date|dates|birthday)\b", re.IGNORECASE)
VARS_WRITE_RE: re.Pattern[str] = re.compile(r"""vars\[\s*['"]([^'"]+)['"]\s*\]\s*=(?!=)""")


COUNT_WORDS: dict[str, int] = {"once": 1, "twice": 2, "thrice": 3}
COUNT_RE: re.Pattern[str] = re.compile(r"\b(once|twice|thrice)\b|\b(\d+) times\b", re.IGNORECASE)
SENTENCE_SPLIT_RE: re.Pattern[str] = re.compile(r"(?<=[.!?:;])\s+|\n+")
STOPWORDS: frozenset[str] = frozenset((
    "the", "and", "for", "with", "that", "this", "from", "then", "than", "when", "only", "not", "are", "was", "has",
    "have", "his", "her", "any", "all", "one", "per", "each", "after", "before", "call", "ask", "say", "once", "twice",
    "thrice", "times", "most", "least", "more", "same", "also", "into", "its", "your", "you", "can", "may", "must",
))


@dataclass(frozen=True)
class Finding:
    """One lint result. severity is 'error' or 'warn'."""

    severity: str
    path: str
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.severity.upper():5} {self.rule}: {self.path}: {self.message}"


# ---------------------------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------------------------


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def iter_nodes(wf: dict) -> Iterator[tuple[str, dict]]:
    """(path, node) for every node and global node."""
    for section in ("nodes", "global_nodes"):
        for key, node in _dict(wf.get(section)).items():
            if isinstance(node, dict):
                yield f"{section}.{key}", node


def iter_tools(wf: dict) -> Iterator[tuple[str, dict, str]]:
    """(path, tool, where) for workflow tools, node tools and pre_actions entries.

    where is 'workflow', 'tools' or 'pre_actions'.
    """
    for i, tool in enumerate(_list(wf.get("tools"))):
        if isinstance(tool, dict):
            yield f"tools[{i}]", tool, "workflow"
    for npath, node in iter_nodes(wf):
        for field in ("tools", "pre_actions"):
            for i, tool in enumerate(_list(node.get(field))):
                if isinstance(tool, dict):
                    yield f"{npath}.{field}[{i}]", tool, field


def _handler(tool: dict) -> object:
    return _dict(tool.get("function")).get("handler")


def _tool_name(tool: dict) -> object:
    return _dict(tool.get("function")).get("name")


def _params(tool: dict) -> dict:
    return _dict(_dict(tool.get("function")).get("parameters"))


def _walk(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def _has_braces(text: str) -> bool:
    return OPEN_BRACES in text or CLOSE_BRACES in text


def _code_sources(tool: dict, path: str) -> Iterator[tuple[str, str]]:
    """(path, source) for hook sources and the code default of a code_exec tool."""
    for key in ("pre_process_code", "post_process_code"):
        if isinstance(tool.get(key), str):
            yield f"{path}.{key}", tool[key]
    if _handler(tool) == CODE_EXEC:
        code = _dict(_dict(_params(tool).get("properties")).get("code")).get("default")
        if isinstance(code, str):
            yield f"{path}.function.parameters.properties.code.default", code


# ---------------------------------------------------------------------------------------------
# Rules. Each takes the workflow and appends to `out`.
# ---------------------------------------------------------------------------------------------


def _add(out: list[Finding], severity: str, path: str, rule: str, message: str) -> None:
    out.append(Finding(severity, path, rule, message))


def rule_no_const(wf: dict, out: list[Finding]) -> None:
    """1: `const` does not bake a value; it may not appear in tool parameter schemas."""

    def scan(value: Any, path: str, in_properties: bool) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "const" and not in_properties:
                    _add(out, "error", f"{path}.const", "no_const", "const does not bake a value; use default")
                scan(v, f"{path}.{k}", k == "properties" and not in_properties)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                scan(v, f"{path}[{i}]", False)

    for path, tool, _ in iter_tools(wf):
        scan(_params(tool), f"{path}.function.parameters", False)


def rule_placement(wf: dict, out: list[Finding]) -> None:
    """2: tools only on conversation nodes; pre_actions only on api_request/code nodes."""
    for path, node in iter_nodes(wf):
        node_type = node.get("node_type")
        if node.get("tools") and node_type != "conversation":
            _add(out, "error", f"{path}.tools", "placement", f"tools on a {node_type} node are stripped silently")
        if node.get("pre_actions") and node_type not in ("api_request", "code"):
            _add(out, "error", f"{path}.pre_actions", "placement", f"pre_actions on a {node_type} node are stripped silently")


def rule_response_mapping(wf: dict, out: list[Finding]) -> None:
    """3: mapping placement and 'data.' paths."""
    for path, tool, where in iter_tools(wf):
        if where == "pre_actions" and "response_mapping" in tool:
            _add(out, "error", f"{path}.response_mapping", "response_mapping",
                 "response_mapping inside a pre_actions entry is ignored; put it on the node")
    places = [(f"{p}.response_mapping", t.get("response_mapping")) for p, t, _ in iter_tools(wf)]
    places += [(f"{p}.response_mapping", n.get("response_mapping")) for p, n in iter_nodes(wf)]
    for path, mapping in places:
        for i, entry in enumerate(_list(mapping)):
            entry_path = _dict(entry).get("path")
            if not isinstance(entry_path, str) or not entry_path.startswith("data."):
                _add(out, "error", f"{path}[{i}].path", "response_mapping", f"mapping path {entry_path!r} must start with 'data.'")


def _condition_groups(condition: object) -> Iterator[tuple[str, dict]]:
    """Every group (dict with 'conditions') in a condition tree, plus a bare leaf as a group."""
    cond = _dict(condition)
    if "conditions" in cond:
        yield "group", cond
        for child in _list(cond.get("conditions")):
            if isinstance(child, dict) and "conditions" in child:
                yield from _condition_groups(child)
    elif "variable" in cond:
        yield "leaf", cond


def _condition_roots(wf: dict) -> Iterator[tuple[str, object]]:
    for path, node in iter_nodes(wf):
        for i, tr in enumerate(_list(node.get("transitions"))):
            yield f"{path}.transitions[{i}].condition", _dict(tr).get("condition")
        if "condition" in node:
            yield f"{path}.condition", node["condition"]


def _true_test(leaf: object) -> tuple[str, str] | None:
    d = _dict(leaf)
    if d.get("operator") == "equals" and d.get("operand") in TRUE_SPELLINGS:
        return str(d.get("variable")), str(d["operand"])
    return None


def rule_bool_spellings(wf: dict, out: list[Finding]) -> None:
    """4: a test for 'true' or 'True' must test both spellings in the same OR group."""
    for path, root in _condition_roots(wf):
        for kind, group in _condition_groups(root):
            if kind == "leaf":
                if _true_test(group):
                    _add(out, "error", path, "bool_spellings", "single true/True test; use an OR group with both spellings")
                continue
            seen: dict[str, set[str]] = {}
            for child in _list(group.get("conditions")):
                hit = _true_test(child)
                if hit:
                    seen.setdefault(hit[0], set()).add(hit[1])
            for variable, spellings in seen.items():
                if group.get("logic") == "and":
                    _add(out, "error", path, "bool_spellings", f"{variable}: true test inside an AND group; nest an OR group with both spellings")
                elif len(spellings) < 2:
                    missing = [s for s in TRUE_SPELLINGS if s not in spellings][0]
                    _add(out, "error", path, "bool_spellings", f"{variable}: tests {sorted(spellings)[0]!r} but not {missing!r} in the same OR group")


def rule_passthrough(wf: dict, out: list[Finding]) -> None:
    """5: passthrough nodes have a default transition and end with an `always` transition."""
    for path, node in iter_nodes(wf):
        if not node.get("passthrough"):
            continue
        if not node.get("passthrough_default_transition"):
            _add(out, "error", path, "passthrough", "passthrough node has no passthrough_default_transition")
        transitions = _list(node.get("transitions"))
        last = _dict(_dict(transitions[-1]).get("condition")) if transitions else {}
        if last.get("operator") != "always":
            _add(out, "error", f"{path}.transitions", "passthrough", "passthrough node must end its transitions with an always transition")


def rule_code_braces(wf: dict, out: list[Finding]) -> None:
    """6: no doubled braces in code defaults or hook sources."""
    for path, tool, _ in iter_tools(wf):
        for src_path, source in _code_sources(tool, path):
            if _has_braces(source):
                _add(out, "error", src_path, "code_braces", "code contains a doubled brace; read values from vars")


def rule_no_null(wf: dict, out: list[Finding]) -> None:
    """7: the schema is strict; no null anywhere."""
    for path, value in _walk(wf, "$"):
        if value is None:
            _add(out, "error", path, "no_null", "null value; omit the field or use an empty string")


def rule_extraction(wf: dict, out: list[Finding]) -> None:
    """8: min_confidence on every entry; date-like fields typed as date."""
    for path, node in iter_nodes(wf):
        for i, entry in enumerate(_list(node.get("extraction_schema"))):
            e = _dict(entry)
            epath = f"{path}.extraction_schema[{i}]"
            if "min_confidence" not in e:
                _add(out, "error", epath, "extraction", f"{e.get('name')!r} has no min_confidence (default 0.95 discards most values)")
            looks_like_date = bool(DATE_NAME_RE.search(str(e.get("name", "")))) or bool(DATE_TEXT_RE.search(str(e.get("description", ""))))
            if looks_like_date and not e.get("enum_values") and e.get("type") not in ("date", "datetime"):
                _add(out, "warn", epath, "extraction", f"{e.get('name')!r} looks like a date but has type {e.get('type')!r}")


def _edges(node: dict) -> list[str]:
    targets = [_dict(t).get("target_node") for t in _list(node.get("transitions"))]
    targets += [node.get("passthrough_default_transition"), node.get("max_turn_transition")]
    return [t for t in targets if isinstance(t, str) and t]


def rule_graph(wf: dict, out: list[Finding]) -> None:
    """9: initial node and every target exist; every node can reach an end_call node."""
    nodes = {k: v for k, v in _dict(wf.get("nodes")).items() if isinstance(v, dict)}
    known = set(nodes) | set(_dict(wf.get("global_nodes")))
    if wf.get("initial_node") not in nodes:
        _add(out, "error", "initial_node", "graph", f"initial_node {wf.get('initial_node')!r} is not a node")
    for path, node in iter_nodes(wf):
        for target in _edges(node):
            if target != PREVIOUS and target not in known:
                _add(out, "error", path, "graph", f"transition target {target!r} does not exist")
    ends = {k for k, v in nodes.items() if v.get("node_type") == "end_call"}
    if not ends:
        _add(out, "error", "nodes", "graph", "no end_call node")
        return
    reverse: dict[str, set[str]] = {}
    for key, node in nodes.items():
        for target in _edges(node):
            reverse.setdefault(target, set()).add(key)
    reach, queue = set(ends), deque(ends)
    while queue:
        for src in reverse.get(queue.popleft(), ()):
            if src not in reach:
                reach.add(src)
                queue.append(src)
    for key in nodes:
        if key not in reach:
            _add(out, "error", f"nodes.{key}", "graph", "node cannot reach an end_call node")


def rule_agent_config(wf: dict, out: list[Finding]) -> None:
    """10: agent_config is left to the platform."""
    if "agent_config" in wf:
        _add(out, "error", "agent_config", "agent_config", "omit agent_config; the platform fills in defaults")


def rule_end_call(wf: dict, out: list[Finding]) -> None:
    """11: end_call nodes have the end_call terminal and no transitions."""
    for path, node in iter_nodes(wf):
        if node.get("node_type") != "end_call":
            continue
        if _dict(_dict(node.get("terminal")).get("function")).get("handler") != END_CALL:
            _add(out, "error", f"{path}.terminal", "end_call", f"terminal handler must be {END_CALL}")
        if node.get("transitions"):
            _add(out, "error", f"{path}.transitions", "end_call", "end_call node must have no transitions")


def rule_hooks_handler(wf: dict, out: list[Finding]) -> None:
    """12: hooks only on api_call tools (the platform removes them elsewhere)."""
    for path, tool, _ in iter_tools(wf):
        for key in ("pre_process_code", "post_process_code"):
            if isinstance(tool.get(key), str) and tool[key].strip() and _handler(tool) != API_CALL:
                _add(out, "error", f"{path}.{key}", "hooks_handler", f"hooks are removed from {_handler(tool)!r} tools; api_call only")


def _is_template(spec: object) -> bool:
    d = _dict(spec)
    return "default" in d and "properties" not in d


def rule_body_fields(wf: dict, out: list[Finding]) -> None:
    """13: api_call body fields are leaves under body.properties, each with its own default."""
    for path, tool, _ in iter_tools(wf):
        if _handler(tool) != API_CALL:
            continue
        props = _dict(_params(tool).get("properties"))
        body = props.get("body")
        template = _is_template(body)
        for name in props:
            if name not in BODY_TOP_KEYS:
                if template:
                    _add(out, "error", f"{path}.function.parameters.properties.{name}", "body_fields",
                         "field spec at the top level next to a body default template; the model sees no enum or description")
                else:
                    _add(out, "warn", f"{path}.function.parameters.properties.{name}", "body_fields",
                         "top-level field on an api_call tool; body fields belong under body.properties")
        if template:
            _add(out, "error", f"{path}.function.parameters.properties.body", "body_fields",
                 "body is only a default template; declare each field under body.properties")
        for name, leaf in _dict(_dict(body).get("properties")).items():
            if "default" not in _dict(leaf):
                _add(out, "error", f"{path}.function.parameters.properties.body.properties.{name}", "body_fields", "body field has no default")


def rule_tool_prompt(wf: dict, out: list[Finding]) -> None:
    """14: a conversation node with tools should mention them in its task_message."""
    for path, node in iter_nodes(wf):
        tools = [t for t in _list(node.get("tools")) if isinstance(t, dict)]
        if node.get("node_type") != "conversation" or not tools:
            continue
        text = str(node.get("task_message", "")).lower()
        names = [str(_tool_name(t)).lower() for t in tools]
        if "tool" not in text and not any(n and n in text for n in names):
            _add(out, "warn", f"{path}.task_message", "tool_prompt", "node has tools but its task_message never mentions a tool")


def rule_turn_cap(wf: dict, out: list[Finding]) -> None:
    """15: max_node_turns without max_turn_transition changes nothing."""
    for path, node in iter_nodes(wf):
        if "max_node_turns" in node and not node.get("max_turn_transition"):
            _add(out, "warn", path, "turn_cap", "max_node_turns is set without max_turn_transition")


def rule_tool_scope(wf: dict, out: list[Finding]) -> None:
    """16: unique tool names per node; lifecycle tools at workflow level with a non-empty list."""
    for path, node in iter_nodes(wf):
        names = [_tool_name(t) for t in _list(node.get("tools")) if isinstance(t, dict)]
        for name in sorted({str(n) for n in names if names.count(n) > 1}):
            _add(out, "error", f"{path}.tools", "tool_scope", f"duplicate tool name {name!r}")
    top_names = [_tool_name(t) for t in _list(wf.get("tools")) if isinstance(t, dict)]
    for name in sorted({str(n) for n in top_names if top_names.count(n) > 1}):
        _add(out, "error", "tools", "tool_scope", f"duplicate workflow tool name {name!r}")
    for path, tool, where in iter_tools(wf):
        config = _dict(tool.get("config"))
        if "alwaysRunsAt" in config:
            runs = config["alwaysRunsAt"]
            if not isinstance(runs, list) or not runs:
                _add(out, "error", f"{path}.config.alwaysRunsAt", "tool_scope", "alwaysRunsAt must be a non-empty list")
            elif where != "workflow":
                _add(out, "warn", f"{path}.config.alwaysRunsAt", "tool_scope", "lifecycle tool inside a node; put it in the workflow-level tools")
            elif "each_turn" in runs:
                _add(out, "warn", f"{path}.config.alwaysRunsAt", "tool_scope", "each_turn is accepted but never fires")
        elif where == "workflow":
            _add(out, "error", path, "tool_scope", "workflow-level tool without alwaysRunsAt never runs")


def rule_placeholder_collision(wf: dict, out: list[Finding]) -> None:
    """17: model-filled body fields named like a variable a code node writes get baked."""
    written: set[str] = set()
    for path, tool, _ in iter_tools(wf):
        for _src_path, source in _code_sources(tool, path):
            written.update(VARS_WRITE_RE.findall(source))
    if not written:
        return
    for path, tool, _ in iter_tools(wf):
        if _handler(tool) != API_CALL:
            continue
        body = _dict(_dict(_params(tool).get("properties")).get("body"))
        for name, leaf in _dict(body.get("properties")).items():
            if _dict(leaf).get("default") == OPEN_BRACES + name + CLOSE_BRACES and name in written:
                _add(out, "warn", f"{path}.function.parameters.properties.body.properties.{name}", "placeholder_collision",
                     f"model-filled field {name!r} shares its name with a variable a code node sets; it would be baked")


def _sentence_counts(text: str) -> list[tuple[str, int, frozenset[str]]]:
    """(sentence, repeat count, topic words) for every sentence that states a repeat count."""
    out: list[tuple[str, int, frozenset[str]]] = []
    for sentence in SENTENCE_SPLIT_RE.split(text):
        m = COUNT_RE.search(sentence)
        if not m:
            continue
        count = COUNT_WORDS[m.group(1).lower()] if m.group(1) else int(m.group(2))
        words = frozenset(w for w in re.findall(r"[a-z]+", sentence.lower()) if len(w) >= 3 and w not in STOPWORDS)
        out.append((sentence.strip(), count, words))
    return out


def rule_repeat_counts(wf: dict, out: list[Finding]) -> None:
    """16: two sentences of one task about the same topic that give different repeat counts contradict each other."""
    for path, node in iter_nodes(wf):
        if node.get("node_type") != "conversation":
            continue
        items = _sentence_counts(str(node.get("task_message", "")))
        done = False
        for i, (a, ca, wa) in enumerate(items):
            for b, cb, wb in items[i + 1:]:
                if ca != cb and len(wa & wb) >= 2:
                    _add(out, "warn", f"{path}.task_message", "repeat_counts", f"{a[:80]!r} vs {b[:80]!r}")
                    done = True
                    break
            if done:
                break


RULES = (
    rule_no_const,
    rule_placement,
    rule_response_mapping,
    rule_bool_spellings,
    rule_passthrough,
    rule_code_braces,
    rule_no_null,
    rule_extraction,
    rule_graph,
    rule_agent_config,
    rule_end_call,
    rule_hooks_handler,
    rule_body_fields,
    rule_tool_prompt,
    rule_turn_cap,
    rule_tool_scope,
    rule_placeholder_collision,
    rule_repeat_counts,
)


def lint(workflow: dict) -> list[Finding]:
    """Run every rule. A rule that crashes is reported as an error finding, never raised."""
    out: list[Finding] = []
    if not isinstance(workflow, dict):
        return [Finding("error", "$", "structure", "workflow is not a JSON object")]
    for rule in RULES:
        try:
            rule(workflow, out)
        except Exception as e:  # noqa: BLE001 - a malformed workflow must not crash the linter
            _add(out, "error", "$", rule.__name__, f"rule crashed: {type(e).__name__}: {e}")
    return out


def main(argv: list[str]) -> int:
    """CLI entry. Exit 0 (no errors), 1 (errors), 2 (unreadable input)."""
    if len(argv) != 1:
        print("usage: python -m builder.lint <workflow.json>", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read {argv[0]}: {e}", file=sys.stderr)
        return 2
    workflow = data["workflow"] if isinstance(data, dict) and isinstance(data.get("workflow"), dict) else data
    findings = lint(workflow)
    for f in findings:
        print(f.format())
    errors = sum(1 for f in findings if f.severity == "error")
    print(f"{errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
