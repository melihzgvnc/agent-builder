"""Run generated hook source the way the platform might: exec with vars/config/result bound.

Imports are forbidden: the builtins' __import__ always raises, which proves a hook never needs one.
"""

from __future__ import annotations

import builtins
from typing import Any

# __build_class__ is removed too: a sandbox without it cannot run a class statement.
REMOVED_BUILTINS: tuple[str, ...] = ("open", "eval", "exec", "compile", "input", "__build_class__")
LAYOUTS: tuple[str, ...] = ("single", "split")


def no_import_builtins() -> dict[str, Any]:
    """Copy of builtins without file/eval/class access whose __import__ always raises."""
    table = dict(builtins.__dict__)
    for name in REMOVED_BUILTINS:
        table.pop(name, None)

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        raise ImportError("import of " + repr(name) + " is not allowed in a hook")

    table["__import__"] = refuse
    return table


def run_hook(
    source: str,
    vars_in: dict[str, Any],
    config: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    layout: str = "single",
    restricted: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Exec source with `vars` (and `config` / `result` when given); returns the three objects.

    layout 'single': exec(src, namespace). layout 'split': exec(src, {}, namespace) with the
    names only in locals (functions then cannot see them as globals).
    """
    if layout not in LAYOUTS:
        raise ValueError("unknown layout " + repr(layout))
    names: dict[str, Any] = {"vars": vars_in}
    if config is not None:
        names["config"] = config
    if result is not None:
        names["result"] = result
    code = compile(source, "<hook>", "exec")
    globals_ns: dict[str, Any] = {}
    if restricted:
        globals_ns["__builtins__"] = no_import_builtins()
    if layout == "single":
        globals_ns.update(names)
        exec(code, globals_ns)
    else:
        exec(code, globals_ns, names)
    return vars_in, config, result
