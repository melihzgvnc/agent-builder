"""scripts/rehearse.py: pure helpers on synthetic events plus one keyless --spec subprocess run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from builder.pipeline import STAGE_BUDGETS, STAGE_ORDER, TOTAL_BUDGET_S
from tests.conftest import FIXTURES_DIR, REPO_ROOT

sys.dont_write_bytecode = True


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rehearse", REPO_ROOT / "scripts" / "rehearse.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rehearse"] = mod
    spec.loader.exec_module(mod)
    return mod


rh = _load()


def _events(overrides: dict[str, float] | None = None) -> list[dict[str, object]]:
    events: list[dict[str, object]] = [{"kind": "start"}, {"kind": "warning", "message": "x"}]
    for name in STAGE_ORDER:
        secs = (overrides or {}).get(name, 0.5)
        events.append({"kind": "stage", "name": name, "status": "ok", "detail": "d", "duration_s": secs})
    return events


def test_parse_stages_keeps_only_stage_events() -> None:
    rows = rh.parse_stages(_events())
    assert [r.name for r in rows] == list(STAGE_ORDER)
    assert rows[0].seconds == 0.5 and rows[0].status == "ok"


def test_within_budget_is_ok() -> None:
    ev = rh.evaluate(rh.parse_stages(_events()), 12.0)
    assert ev.ok and not ev.warnings


def test_stage_over_budget_fails() -> None:
    rows = rh.parse_stages(_events({"lint": STAGE_BUDGETS["lint"] + 1}))
    ev = rh.evaluate(rows, 40.0)
    assert not ev.ok
    assert any("lint" in f for f in ev.failures)


def test_total_over_budget_fails() -> None:
    ev = rh.evaluate(rh.parse_stages(_events()), TOTAL_BUDGET_S + 1)
    assert not ev.ok and any("total" in f for f in ev.failures)


def test_missing_workflow_lint_error_kill_and_exit_code_fail() -> None:
    rows = rh.parse_stages(_events())
    assert any("workflow.json" in f for f in rh.evaluate(rows, 1, workflow_exists=False).failures)
    assert not rh.evaluate(rows, 1, lint_errors=["ERROR r: p: m"]).ok
    assert not rh.evaluate(rows, 1, killed=True).ok
    assert not rh.evaluate(rows, 1, exit_code=1).ok


def test_extraction_fallback_is_warn_unless_required() -> None:
    rows = rh.parse_stages(_events())
    ev = rh.evaluate(rows, 1, fallback=True)
    assert ev.ok and ev.warnings
    assert not rh.evaluate(rows, 1, fallback=True, require_extraction=True).ok


def test_render_table_lists_every_stage_and_total() -> None:
    rows = rh.parse_stages(_events({"lint": STAGE_BUDGETS["lint"] + 1}))
    table = rh.render_table(rows, 9.5)
    for name in STAGE_ORDER:
        assert name in table
    assert "TOTAL" in table and "OVER" in table
    assert table.splitlines()[0].split() == ["stage", "status", "seconds", "budget", "verdict"]


def test_subprocess_run_with_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = tmp_path / "tiny-brief.md"
    brief.write_text("# Tiny\n\nA short placeholder brief.\n", encoding="utf-8")
    runs = REPO_ROOT / "runs"
    before = {p.name for p in runs.iterdir()} if runs.is_dir() else set()
    code = rh.main([str(brief), "--spec", str(FIXTURES_DIR / "spec_brief_a.json"), "--budget", "600"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "REHEARSAL OK" in out
    for name in ("compile_workflow", "offline_checks", "TOTAL"):
        assert name in out
    after = {p.name for p in runs.iterdir()} if runs.is_dir() else set()
    assert after == before
