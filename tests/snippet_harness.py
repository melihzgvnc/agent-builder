"""Run a code-node snippet the way the platform might: exec with a `vars` dict."""

from __future__ import annotations

import builtins
from typing import Any

ALLOWED_IMPORTS: frozenset[str] = frozenset(("json", "re", "datetime"))
# __build_class__ is removed too: a sandbox without it cannot run a class statement.
REMOVED_BUILTINS: tuple[str, ...] = ("open", "eval", "exec", "compile", "input", "__build_class__")
LAYOUTS: tuple[str, ...] = ("single", "split")


def restricted_builtins() -> dict[str, Any]:
    """Copy of builtins without file/eval/class access whose __import__ allows only json/re/datetime."""
    real_import = builtins.__import__
    table = dict(builtins.__dict__)
    for name in REMOVED_BUILTINS:
        table.pop(name, None)

    def guarded_import(
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level != 0 or name.split(".")[0] not in ALLOWED_IMPORTS:
            raise ImportError(f"import of {name!r} is not allowed in a code node")
        return real_import(name, globals_, locals_, fromlist, level)

    table["__import__"] = guarded_import
    return table


def run_snippet(
    source: str, vars_in: dict[str, Any], restricted: bool = False, layout: str = "single"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exec source with `vars` bound to vars_in; returns (vars_in, namespace of top-level names).

    layout 'single': exec(src, {'vars': v}) (one namespace).
    layout 'split': exec(src, {}, {'vars': v}) (separate globals and locals, vars only in locals).
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}")
    code = compile(source, "<code_node>", "exec")
    globals_ns: dict[str, Any] = {}
    if restricted:
        globals_ns["__builtins__"] = restricted_builtins()
    if layout == "single":
        globals_ns["vars"] = vars_in
        exec(code, globals_ns)
        return vars_in, globals_ns
    locals_ns: dict[str, Any] = {"vars": vars_in}
    exec(code, globals_ns, locals_ns)
    return vars_in, locals_ns
