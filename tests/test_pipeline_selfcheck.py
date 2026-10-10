"""The offline_checks pipeline stage: reporting, the one-shot repair, containment. No network, no LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import builder.pipeline as pipeline
from builder import lint, selfcheck
from builder.pipeline import BuildContext, run_build
from tests.conftest import FIXTURES_DIR, REPO_ROOT

BRIEF = REPO_ROOT / "briefs" / "brief-a.md"
OPENAPI = REPO_ROOT / "tools" / "openapi.json"
REAL_RUN = selfcheck.run_selfchecks
REAL_COMPILE = pipeline.compile_workflow


@pytest.fixture(autouse=True)
def _no_freya(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: BuildContext) -> Any:
        raise AssertionError("the Freya API must not be used")

    monkeypatch.setattr(pipeline, "_freya", boom)


def _build(tmp_path: Path, spec: str | None = "spec_brief_a.json", **kwargs: Any) -> tuple[int, Path, str]:
    runs = tmp_path / "runs"
    params: dict[str, Any] = dict(
        root=REPO_ROOT, briefs=[BRIEF], openapi=OPENAPI, slot="t", dry=True, cases=None, runs_dir=runs,
        spec_path=(FIXTURES_DIR / spec) if spec else None,
    )
    params.update(kwargs)
    before = set(runs.iterdir()) if runs.exists() else set()
    code = run_build(**params)
    new = [p for p in runs.iterdir() if p not in before and p.is_dir()]
    assert len(new) == 1, new
    return code, new[0], (new[0] / "trace.log").read_text(encoding="utf-8")


def _json(run_dir: Path, name: str) -> Any:
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _failing(feature: str, name: str = "injected") -> selfcheck.Check:
    return selfcheck.Check(name, False, selfcheck.SEVERITY_ERROR, "injected failure", feature)


def test_stage_is_registered_after_lint_and_is_not_a_network_stage() -> None:
    order = list(pipeline.STAGE_ORDER)
    assert order[order.index("lint") + 1] == "offline_checks"
    assert "offline_checks" not in pipeline.NETWORK_STAGES
    assert "offline_checks" in pipeline.ESSENTIAL_STAGES
    assert pipeline.STAGE_BUDGETS["offline_checks"] == 120.0
    assert pipeline.REQUIRES["offline_checks"] == ("workflow", "spec")
    assert pipeline.DEFAULT_STAGES["offline_checks"] is pipeline.stage_offline_checks


@pytest.mark.parametrize("spec", ["spec_brief_a.json", "spec_brief_ab.json"])
def test_fixture_specs_pass_the_offline_checks(tmp_path: Path, spec: str) -> None:
    code, run_dir, text = _build(tmp_path, spec)
    assert code == 0, text
    assert "\noffline_checks: ok" in text and "selfcheck FAILED" not in text
    block = _json(run_dir, "compile_report.json")["selfcheck"]
    assert block["failed"] == [] and block["repairs"] == [] and block["checks"] > 20
    assert set(block) >= {"checks", "failed", "warnings", "repairs"}
    # --dry does not skip it: it is not a network stage
    assert "\ndry_run: skipped" in text


def test_repairable_callback_failure_is_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake(workflow: dict, spec: dict, openapi: dict, context: dict) -> selfcheck.SelfCheckResult:
        calls.append(1)
        result = REAL_RUN(workflow, spec, openapi, context)
        if len(calls) == 1:
            result.checks.append(_failing(selfcheck.FEATURE_CALLBACK, "callback[injected]"))
        return result

    monkeypatch.setattr(selfcheck, "run_selfchecks", fake)
    code, run_dir, text = _build(tmp_path)
    assert code == 0, text
    assert len(calls) == 2
    assert "REPAIRED" in text and "selfcheck FAILED" not in text
    wf = _json(run_dir, "workflow.json")
    assert not any(t.get("function", {}).get("name") == "schedule_callback" for _p, t, _w in lint.iter_tools(wf))
    assert [f for f in lint.lint(wf) if f.severity == "error"] == []
    report = _json(run_dir, "compile_report.json")
    assert report["selfcheck"]["repairs"] == ["drop_callback_window"] and report["selfcheck"]["failed"] == []
    assert report["fallback_workflow"] is False
    spec_on_disk = _json(run_dir, "spec.json")
    assert "callback_window" in spec_on_disk["third_party"]  # spec.json is never rewritten


def test_repair_that_does_not_help_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _json(_build(tmp_path / "base")[1], "workflow.json")

    def always(workflow: dict, spec: dict, openapi: dict, context: dict) -> selfcheck.SelfCheckResult:
        result = REAL_RUN(workflow, spec, openapi, context)
        result.checks.append(_failing(selfcheck.FEATURE_CALLBACK, "callback[injected]"))
        return result

    monkeypatch.setattr(selfcheck, "run_selfchecks", always)
    code, run_dir, text = _build(tmp_path / "bad")
    assert code == 0, text
    assert "selfcheck FAILED: callback[injected]" in text
    assert "rejected" in text and "original workflow kept" in text and "REPAIRED" not in text
    assert _json(run_dir, "workflow.json") == baseline
    block = _json(run_dir, "compile_report.json")["selfcheck"]
    assert len(block["failed"]) == 1 and block["repairs"] == []


def test_non_repairable_failure_is_reported_and_workflow_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _json(_build(tmp_path / "base")[1], "workflow.json")

    def always(workflow: dict, spec: dict, openapi: dict, context: dict) -> selfcheck.SelfCheckResult:
        result = REAL_RUN(workflow, spec, openapi, context)
        result.checks.append(_failing(selfcheck.FEATURE_FACTS, "facts[injected]"))
        return result

    monkeypatch.setattr(selfcheck, "run_selfchecks", always)
    code, run_dir, text = _build(tmp_path / "bad")
    assert code == 0, text
    assert "selfcheck FAILED: facts[injected]" in text and "no deterministic repair applies" in text
    assert "\noffline_checks: ok" in text
    assert _json(run_dir, "workflow.json") == baseline
    assert _json(run_dir, "compile_report.json")["selfcheck"]["failed"]


def test_a_really_broken_hook_is_found_and_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(spec: Any, openapi: dict) -> Any:
        workflow, report = REAL_COMPILE(spec, openapi)
        for _path, tool, _where in lint.iter_tools(workflow):
            if tool.get("function", {}).get("name") == "schedule_callback":
                assert "mins < HS" in tool["pre_process_code"]
                tool["pre_process_code"] = tool["pre_process_code"].replace("mins < HS", "mins < HS - 120", 1)
        return workflow, report

    monkeypatch.setattr(pipeline, "compile_workflow", broken)
    code, run_dir, text = _build(tmp_path)
    assert code == 0, text
    assert "REPAIRED" in text and _json(run_dir, "compile_report.json")["selfcheck"]["failed"] == []
    assert _json(run_dir, "compile_report.json")["selfcheck"]["repairs"] == ["drop_callback_window"]


def test_crashing_selfcheck_is_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(workflow: dict, spec: dict, openapi: dict, context: dict) -> selfcheck.SelfCheckResult:
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(selfcheck, "run_selfchecks", boom)
    code, run_dir, text = _build(tmp_path)
    assert code == 1
    assert "stage offline_checks crashed" in text and "checker exploded" in text
    assert (run_dir / "workflow.json").is_file()
    assert [f for f in lint.lint(_json(run_dir, "workflow.json")) if f.severity == "error"] == []


def test_fallback_workflow_skips_the_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builder.extract.make_llm", lambda trace=None: None)
    code, run_dir, text = _build(tmp_path, spec=None)
    assert code == 0, text
    assert "FALLBACK WORKFLOW" in text
    assert "\noffline_checks: skipped" in text and "fallback workflow" in text
    assert "selfcheck" not in _json(run_dir, "compile_report.json")


def test_failed_scenario_does_not_change_the_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        selfcheck, "run_selfchecks",
        lambda *a, **k: selfcheck.SelfCheckResult(checks=[_failing(selfcheck.FEATURE_OUTCOMES, "outcomes[x]")]),
    )
    code, _run_dir, text = _build(tmp_path)
    assert code == 0 and "selfcheck FAILED: outcomes[x]" in text
