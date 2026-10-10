"""Restricted exec of compiled code, the way the platform might run it.

Used by the in-build self-checks (builder/selfcheck.py). The rules mirror the test harnesses
(tests/snippet_harness.py and tests/hook_harness.py), which stay as they are:
- code-node snippets may import json, re and datetime only;
- hooks may not import anything (the builtins' __import__ always raises);
- open, eval, exec, compile, input and __build_class__ are removed from the builtins;
- two exec layouts: 'single' (one namespace) and 'split' (separate globals and locals, the
  names the platform provides live only in the locals).
"""

from __future__ import annotations

import builtins
from typing import Any

SNIPPET_IMPORTS: frozenset[str] = frozenset(("json", "re", "datetime"))
# __build_class__ is removed too: a sandbox without it cannot run a class statement.
REMOVED_BUILTINS: tuple[str, ...] = ("open", "eval", "exec", "compile", "input", "__build_class__")
LAYOUTS: tuple[str, ...] = ("single", "split")


def snippet_builtins() -> dict[str, Any]:
    """Builtins without file/eval/class access whose __import__ allows only json/re/datetime."""
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
        if level != 0 or name.split(".")[0] not in SNIPPET_IMPORTS:
            raise ImportError(f"import of {name!r} is not allowed in a code node")
        return real_import(name, globals_, locals_, fromlist, level)

    table["__import__"] = guarded_import
    return table


def hook_builtins() -> dict[str, Any]:
    """Builtins without file/eval/class access whose __import__ always raises."""
    table = dict(builtins.__dict__)
    for name in REMOVED_BUILTINS:
        table.pop(name, None)

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        raise ImportError("import of " + repr(name) + " is not allowed in a hook")

    table["__import__"] = refuse
    return table


def _check_layout(layout: str) -> None:
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}")


def run_snippet(
    source: str, vars_in: dict[str, Any], restricted: bool = True, layout: str = "single"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exec a code-node source with `vars` bound to vars_in; returns (vars_in, top-level names).

    layout 'single': one namespace. layout 'split': separate globals and locals, vars only in
    the locals. restricted=True applies snippet_builtins().
    """
    _check_layout(layout)
    code = compile(source, "<code_node>", "exec")
    globals_ns: dict[str, Any] = {}
    if restricted:
        globals_ns["__builtins__"] = snippet_builtins()
    if layout == "single":
        globals_ns["vars"] = vars_in
        exec(code, globals_ns)
        return vars_in, globals_ns
    locals_ns: dict[str, Any] = {"vars": vars_in}
    exec(code, globals_ns, locals_ns)
    return vars_in, locals_ns


def run_hook(
    source: str,
    vars_in: dict[str, Any],
    config: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    layout: str = "single",
    restricted: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Exec hook source with `vars` (and `config` / `result` when given); returns the three objects.

    Pre-hooks get config, post-hooks get result. restricted=True applies hook_builtins().
    """
    _check_layout(layout)
    names: dict[str, Any] = {"vars": vars_in}
    if config is not None:
        names["config"] = config
    if result is not None:
        names["result"] = result
    code = compile(source, "<hook>", "exec")
    globals_ns: dict[str, Any] = {}
    if restricted:
        globals_ns["__builtins__"] = hook_builtins()
    if layout == "single":
        globals_ns.update(names)
        exec(code, globals_ns)
    else:
        exec(code, globals_ns, names)
    return vars_in, config, result
