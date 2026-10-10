"""builder.sandbox: restricted exec helpers for the in-build self-checks."""

from __future__ import annotations

import pytest

from builder import sandbox


@pytest.mark.parametrize("layout", sandbox.LAYOUTS)
def test_snippet_mutates_vars_in_both_layouts(layout: str) -> None:
    vars_in: dict = {"a": 2}
    out, names = sandbox.run_snippet("vars['b'] = vars['a'] * 3\nx = 1\n", vars_in, layout=layout)
    assert out is vars_in and vars_in["b"] == 6
    assert names["x"] == 1


def test_split_layout_keeps_vars_out_of_globals() -> None:
    src = "def f():\n    return vars['a']\ntry:\n    vars['r'] = f()\nexcept Exception:\n    vars['r'] = 'no globals'\n"
    single: dict = {"a": 1}
    sandbox.run_snippet(src, single, layout="single")
    split: dict = {"a": 1}
    sandbox.run_snippet(src, split, layout="split")
    assert single["r"] == 1 and split["r"] == "no globals"


def test_snippet_may_import_json_re_datetime_only() -> None:
    v: dict = {}
    sandbox.run_snippet("import json\nimport re\nimport datetime\nvars['j'] = json.dumps([1])\n", v)
    assert v["j"] == "[1]"
    with pytest.raises(ImportError):
        sandbox.run_snippet("import os\n", {})
    with pytest.raises(ImportError):
        sandbox.run_snippet("import time\n", {})


def test_snippet_builtins_are_restricted() -> None:
    for src in ("open('x')", "eval('1')", "exec('1')", "compile('1', 'x', 'exec')", "input()"):
        with pytest.raises(NameError):
            sandbox.run_snippet(src + "\n", {})
    with pytest.raises(NameError):
        sandbox.run_snippet("class A:\n    pass\n", {})


def test_unrestricted_snippet_may_import_anything() -> None:
    v: dict = {}
    sandbox.run_snippet("import os\nvars['n'] = os.sep\n", v, restricted=False)
    assert v["n"]


@pytest.mark.parametrize("layout", sandbox.LAYOUTS)
def test_hook_sees_vars_config_result(layout: str) -> None:
    vars_in: dict = {"a": 1}
    config: dict = {"body": {"x": 1}}
    result: dict = {"data": {}}
    src = "vars['b'] = 2\nconfig['body']['x'] = 5\nresult['data']['ok'] = True\n"
    out = sandbox.run_hook(src, vars_in, config=config, result=result, layout=layout)
    assert out == (vars_in, config, result)
    assert vars_in["b"] == 2 and config["body"]["x"] == 5 and result["data"]["ok"] is True


def test_hook_import_is_blocked_even_for_json() -> None:
    with pytest.raises(ImportError):
        sandbox.run_hook("import json\n", {})
    with pytest.raises(ImportError):
        sandbox.run_hook("def f():\n    import re\nf()\n", {})


def test_hook_builtins_are_restricted() -> None:
    with pytest.raises(NameError):
        sandbox.run_hook("open('x')\n", {})
    with pytest.raises(NameError):
        sandbox.run_hook("class A:\n    pass\n", {})


def test_unknown_layout_is_rejected() -> None:
    with pytest.raises(ValueError):
        sandbox.run_snippet("pass\n", {}, layout="nested")
    with pytest.raises(ValueError):
        sandbox.run_hook("pass\n", {}, layout="nested")
