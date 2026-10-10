"""Code-node snippet emitter.

The facts snippet embeds the source of builder/engine.py (annotations, docstrings and imports
stripped) plus the subset of the spec the engine reads, so the platform runs the same code as
the builder. Platform rules enforced here: no doubled braces anywhere in the source, imports
limited to json/re/datetime, one top-level try/except Exception that writes a fallback for
every output variable, no class or global statements (the engine runs as closures inside one
function, so the snippet does not depend on how the sandbox lays out exec namespaces).
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

from builder.engine import output_names

ENGINE_PATH: Path = Path(__file__).with_name("engine.py")
ALLOWED_MODULES: frozenset[str] = frozenset(("json", "re", "datetime"))
UNAVAILABLE_SENTENCE: str = "These facts are unavailable right now; do not state any date or amount from memory."


class SnippetError(ValueError):
    """The generated snippet violates a platform rule."""


class _StripForSnippet(ast.NodeTransformer):
    """Drop docstrings and every annotation."""

    def _strip_body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for stmt in body:
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            out.append(stmt)
        return out or [ast.Pass()]

    def _strip_args(self, args: ast.arguments) -> None:
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            a.annotation = None
        if args.vararg is not None:
            args.vararg.annotation = None
        if args.kwarg is not None:
            args.kwarg.annotation = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        self._strip_args(node.args)
        node.returns = None
        node.body = self._strip_body(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        self._strip_args(node.args)
        node.returns = None
        node.body = self._strip_body(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._strip_body(node.body)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if node.value is None:
            return None
        return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)


def engine_source_for_snippet() -> str:
    """Engine source without __future__/module imports, docstrings and annotations."""
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    body = [s for s in tree.body if not isinstance(s, (ast.Import, ast.ImportFrom))]
    tree.body = body
    tree = _StripForSnippet().visit(tree)
    tree.body = [
        s
        for s in tree.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def embed_json(data: object) -> str:
    """Python string literal holding indented JSON with no doubled braces."""
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=True)
    text = text.replace("{{", "\\u007b\\u007b").replace("}}", "\\u007d\\u007d")
    return repr(text)


# Spec keys the engine reads: section -> sub-keys (None = the whole section).
ENGINE_SPEC_KEYS: dict[str, tuple[str, ...] | None] = {
    "call_variables": ("now",),
    "account_fields": None,
    "calendar": ("weekend_days", "holidays", "week_starts_on", "weekday_semantics"),
    "bands": None,
    "amounts": ("full_amount_field", "floor_fields", "floor_default_field", "rounding_decimals", "too_much"),
    "persuasion": ("attempts",),
    "debt_statement": ("product_names",),
    "multi_product": ("enabled", "max_products", "one_date_for_all"),
    "third_party": ("callback_window",),
    "closing": ("aggregation",),
}


def spec_subset(spec: dict) -> dict:
    """Only the parts of the spec the engine reads."""
    out: dict = {}
    for key, sub in ENGINE_SPEC_KEYS.items():
        if key not in spec:
            continue
        value = spec[key]
        if sub is None or not isinstance(value, dict):
            out[key] = value
            continue
        picked = {k: value[k] for k in sub if k in value}
        if picked:
            out[key] = picked
    return out


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def check_snippet(source: str) -> None:
    """Raise SnippetError unless the source satisfies the platform snippet rules."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SnippetError(f"snippet does not parse: {exc}") from exc
    if "{{" in source or "}}" in source:
        raise SnippetError("snippet contains a doubled brace")
    extra = _imported_modules(tree) - ALLOWED_MODULES
    if extra:
        raise SnippetError(f"snippet imports modules outside {sorted(ALLOWED_MODULES)}: {sorted(extra)}")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Try):
        raise SnippetError("snippet must be exactly one top-level try statement")
    if any(isinstance(node, (ast.ClassDef, ast.Global)) for node in ast.walk(tree)):
        raise SnippetError("snippet may not contain class or global statements")


def _top_level_names(source: str) -> list[str]:
    """Functions and simple assignments defined at the top level of source, in order."""
    names: list[str] = []
    for stmt in ast.parse(source).body:
        if isinstance(stmt, ast.FunctionDef):
            names.append(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.extend(t.id for t in stmt.targets if isinstance(t, ast.Name))
    return list(dict.fromkeys(names))


def facts_snippet(spec: dict, io: dict) -> str:
    """Code-node source that writes compute_facts(vars, SPEC, IO) into vars, never raising.

    The imports, the engine and the embedded spec live inside one function, so the engine's
    functions find each other as closures whatever exec layout the platform uses (one dict,
    separate globals and locals, vars only in locals). The function returns its names as a
    dict, bound to ENGINE at the top level.
    """
    prefix = io["out_prefix"]
    names = output_names(spec, io)
    engine_src = engine_source_for_snippet()
    exported = _top_level_names(engine_src) + ["SPEC", "IO"]
    factory = "\n".join(
        [
            "import json",
            "import re",
            "import datetime",
            engine_src,
            "SPEC = json.loads(" + embed_json(spec_subset(spec)) + ")",
            "IO = json.loads(" + embed_json(io) + ")",
            "return dict(" + ", ".join(n + "=" + n for n in exported) + ")",
        ]
    )
    body = "\n".join(
        [
            "def _facts_engine():",
            textwrap.indent(factory, "    "),
            "ENGINE = _facts_engine()",
            "vars.update(ENGINE['compute_facts'](vars, ENGINE['SPEC'], ENGINE['IO']))",
        ]
    )
    fallback = "\n".join(
        [
            "for _name in " + repr(names) + ":",
            "    vars[_name] = ''",
            "vars[" + repr(prefix + "facts_status") + "] = 'error'",
            "vars[" + repr(prefix + "now_state") + "] = 'unknown'",
            "vars[" + repr(prefix + "products_state") + "] = 'unknown'",
            "vars[" + repr(prefix + "date_facts") + "] = " + repr(UNAVAILABLE_SENTENCE),
            "vars[" + repr(prefix + "product_facts") + "] = " + repr(UNAVAILABLE_SENTENCE),
        ]
    )
    source = (
        "try:\n"
        + textwrap.indent(body, "    ")
        + "\nexcept Exception:\n"
        + textwrap.indent(fallback, "    ")
        + "\n"
    )
    check_snippet(source)
    return source
