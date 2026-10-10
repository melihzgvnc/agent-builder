"""scripts/check_contamination.py: unit tests on tmp trees plus the scan of the real repo."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import REPO_ROOT

sys.dont_write_bytecode = True


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_contamination", REPO_ROOT / "scripts" / "check_contamination.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_contamination"] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load()

BRIEF = (
    "Call on 2031-05-06 only.\n"
    "Value `alpha_beta` is special.\n"
    'Say "settle the open balance" first.\n'
    "Mention `plain` too.\n"
)


class Tree:
    """A tmp tree with a brief, an empty fixture and a src/ folder to scan."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        (tmp / "brief.md").write_text(BRIEF, encoding="utf-8")
        (tmp / "fixture.json").write_text('{"k": 5}', encoding="utf-8")
        (tmp / "src").mkdir()
        self.allow = tmp / "allow.json"

    def write(self, rel: str, text: str) -> Path:
        p = self.tmp / "src" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run(self, *extra: str) -> int:
        return cc.main(
            [
                "--briefs", str(self.tmp / "brief.md"),
                "--fixtures", str(self.tmp / "fixture.json"),
                "--roots", str(self.tmp / "src"),
                "--openapi", str(self.tmp / "none.json"),
                "--schema", str(self.tmp / "none-schema.json"),
                "--allow", str(self.allow),
                *extra,
            ]
        )


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def test_derived_tiers(tree: Tree) -> None:
    tokens = {t.key: t for t in cc.derive_tokens([tree.tmp / "brief.md"], [tree.tmp / "fixture.json"], tree.tmp / "x", tree.tmp / "y")}
    assert tokens["2031-05-06"].kind == "date" and tokens["2031-05-06"].tier == "strict"
    assert tokens["alpha_beta"].tier == "strict"
    assert tokens["settle the open balance"].tier == "strict"
    assert tokens["plain"].tier == "advisory"


def test_strict_hits_fail_with_lines(tree: Tree, capsys: pytest.CaptureFixture[str]) -> None:
    tree.write(
        "bad.py",
        "x = 1\nd = '2031-05-06'\nname = 'Alpha_Beta'\n# please SETTLE   the open balance\nplain = 1\n",
    )
    assert tree.run() == 1
    out = capsys.readouterr().out
    assert "bad.py:2: 2031-05-06" in out
    assert "bad.py:3: alpha_beta" in out
    assert "bad.py:4: settle the open balance" in out
    assert "[identifier, brief.md]" in out
    assert "bad.py:5" not in out.split("advisory")[0]


def test_identifier_boundary(tree: Tree) -> None:
    tree.write("ok.py", "on_alpha_beta_x = 1\nalpha_beta_x = 2\nxalpha_beta = 3\n")
    assert tree.run() == 0


def test_advisory_never_fails(tree: Tree, capsys: pytest.CaptureFixture[str]) -> None:
    tree.write("ok.py", "plain = 1\nplain = 2\n")
    assert tree.run() == 0
    assert "advisory" in capsys.readouterr().out


def test_allowlist_needs_reason(tree: Tree, capsys: pytest.CaptureFixture[str]) -> None:
    tree.write("bad.py", "n = 'alpha_beta'\n")
    tree.allow.write_text(json.dumps([{"token": "alpha_beta"}]), encoding="utf-8")
    assert tree.run() == 1
    assert "reason is required" in capsys.readouterr().out
    tree.allow.write_text(json.dumps([{"token": "alpha_beta", "reason": "  "}]), encoding="utf-8")
    assert tree.run() == 1


def test_allowlist_with_reason_suppresses(tree: Tree) -> None:
    tree.write("bad.py", "n = 'alpha_beta'\n")
    tree.allow.write_text(
        json.dumps([{"token": "ALPHA_BETA", "reason": "generic test vocabulary"}]), encoding="utf-8"
    )
    assert tree.run() == 0


def test_unused_allowlist_entry_fails(tree: Tree, capsys: pytest.CaptureFixture[str]) -> None:
    tree.write("ok.py", "x = 1\n")
    tree.allow.write_text(
        json.dumps([{"token": "alpha_beta", "reason": "stale entry"}]), encoding="utf-8"
    )
    assert tree.run() == 1
    assert "unused allowlist entry" in capsys.readouterr().out


def test_spikes_excluded_unless_flag(tree: Tree, capsys: pytest.CaptureFixture[str]) -> None:
    tree.write("spikes/s.py", "n = 'alpha_beta'\n")
    assert tree.run() == 0
    assert "NOTE" not in capsys.readouterr().out
    assert tree.run("--include-spikes") == 0
    out = capsys.readouterr().out
    assert "NOTE" in out and "spikes/s.py:1: alpha_beta" in out


def test_tool_names_and_schema_vocabulary_are_not_tokens(tree: Tree) -> None:
    (tree.tmp / "brief.md").write_text("Call `get_thing` and `my_field` and `my_field.deep_one`.\n", encoding="utf-8")
    (tree.tmp / "api.json").write_text(json.dumps({"paths": {"/get_thing": {}}}), encoding="utf-8")
    (tree.tmp / "schema.json").write_text(
        json.dumps({"properties": {"my_field": {"properties": {"deep_one": {"enum": ["pick_me"]}}}}}),
        encoding="utf-8",
    )
    keys = {t.key for t in cc.derive_tokens([tree.tmp / "brief.md"], [], tree.tmp / "api.json", tree.tmp / "schema.json")}
    assert not keys & {"get_thing", "my_field", "my_field.deep_one"}


def test_variant_files_are_refused(tree: Tree) -> None:
    with pytest.raises(SystemExit):
        cc.derive_tokens([tree.tmp / "brief_variant_9.md"], [], tree.tmp / "x", tree.tmp / "y")


def test_real_repo_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    code = cc.main([])
    assert code == 0, capsys.readouterr().out
