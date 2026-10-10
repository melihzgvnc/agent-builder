"""Pipeline with the extractor wired in: FakeLLM only, no network, never the Freya API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import builder.extract
from builder import lint, pipeline
from builder.llm import FakeLLM, LLMError
from builder.pipeline import DEFAULT_STAGES, BuildContext, run_build
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture

BRIEF = REPO_ROOT / "briefs" / "brief-a.md"
OPENAPI = REPO_ROOT / "tools" / "openapi.json"


@pytest.fixture(autouse=True)
def _no_freya(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: BuildContext) -> Any:
        raise AssertionError("the Freya API must not be used")

    monkeypatch.setattr(pipeline, "_freya", boom)


def _happy_llm() -> FakeLLM:
    return FakeLLM(
        [
            {"tool_inputs": [load_fixture("spec_brief_a.json")]},
            {"tool_inputs": [{"items": []}]},
            {"tool_inputs": [{"mappings": []}]},
        ]
    )


def _build(tmp_path: Path, **kwargs: Any) -> tuple[int, Path, str]:
    params: dict[str, Any] = dict(
        root=REPO_ROOT, briefs=[BRIEF], openapi=OPENAPI, slot="t", dry=True, cases=None,
        runs_dir=tmp_path / "runs",
    )
    params.update(kwargs)
    code = run_build(**params)
    dirs = [p for p in (tmp_path / "runs").iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return code, dirs[0], (dirs[0] / "trace.log").read_text(encoding="utf-8")


def _lint_errors(run_dir: Path) -> list[str]:
    wf = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
    return [f.format() for f in lint.lint(wf) if f.severity == "error"]


def test_raw_brief_with_fake_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _happy_llm()
    monkeypatch.setattr("builder.extract.make_llm", lambda trace=None: llm)
    code, run_dir, text = _build(tmp_path)
    assert code == 0, text
    for name in ("workflow.json", "compile_report.json", "spec.json", "extraction_notes.md"):
        assert (run_dir / name).is_file(), name
    assert "\nextract_spec: ok" in text and "\ncompile_workflow: ok" in text
    assert llm.requests[0]["purpose"] == "extract_spec"
    assert _lint_errors(run_dir) == []
    report = json.loads((run_dir / "compile_report.json").read_text(encoding="utf-8"))
    assert report["fallback_workflow"] is False


@pytest.mark.parametrize("mode", ["llm_error", "no_llm"])
def test_extraction_fallback_still_gives_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    llm = FakeLLM([LLMError("down", 500)] * 6) if mode == "llm_error" else None
    monkeypatch.setattr("builder.extract.make_llm", lambda trace=None: llm)
    code, run_dir, text = _build(tmp_path)
    assert code == 0, text
    assert "EXTRACTION FELL BACK" in text
    assert "DEGRADED" in text
    assert "FALLBACK WORKFLOW" in text
    assert _lint_errors(run_dir) == []
    report = json.loads((run_dir / "compile_report.json").read_text(encoding="utf-8"))
    assert report["fallback_workflow"] is True and report["fallback_reason"]


def test_crashing_extract_stage_keeps_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: Any) -> str:
        raise ValueError("extractor exploded")

    monkeypatch.setitem(DEFAULT_STAGES, "extract_spec", boom)
    code, run_dir, text = _build(tmp_path)
    assert code == 1
    assert "stage extract_spec crashed" in text and "extractor exploded" in text
    assert (run_dir / "workflow.json").is_file()
    assert _lint_errors(run_dir) == []
    assert "FALLBACK WORKFLOW" in text


def test_invalid_spec_gives_fallback_workflow(tmp_path: Path) -> None:
    spec = load_fixture("spec_brief_a.json")
    del spec["bands"]

    def inject(ctx: BuildContext) -> str:
        ctx.spec = spec
        return "injected"

    code, run_dir, text = _build(tmp_path, stages={"extract_spec": inject})
    assert code == 1
    assert not (run_dir / "spec.json").exists()
    assert (run_dir / "workflow.json").is_file()
    assert "FALLBACK WORKFLOW" in text
    assert _lint_errors(run_dir) == []


def test_spec_option_never_builds_an_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(trace: Any = None) -> Any:
        raise AssertionError("make_llm must not be called with --spec")

    monkeypatch.setattr("builder.extract.make_llm", forbidden)
    code, run_dir, text = _build(tmp_path, spec_path=FIXTURES_DIR / "spec_brief_a.json")
    assert code == 0, text
    report = json.loads((run_dir / "compile_report.json").read_text(encoding="utf-8"))
    assert report["fallback_workflow"] is False
    assert _lint_errors(run_dir) == []


def test_hung_stage_is_abandoned(tmp_path: Path) -> None:
    release = threading.Event()

    def sleeper(ctx: BuildContext) -> str:
        release.wait(10)
        ctx.spec = {"late": True}
        return "too late"

    t0 = time.monotonic()
    try:
        code, run_dir, text = _build(
            tmp_path, stages={"extract_spec": sleeper}, stage_timeouts={"extract_spec": 0.3}
        )
    finally:
        release.set()
    assert time.monotonic() - t0 < 5
    assert code == 1
    assert "exceeded" in text
    assert (run_dir / "workflow.json").is_file()
    assert _lint_errors(run_dir) == []


def test_exhausted_budget_skips_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(trace: Any = None) -> Any:
        raise AssertionError("extraction must be skipped")

    monkeypatch.setattr("builder.extract.make_llm", forbidden)
    code, run_dir, text = _build(tmp_path, budget_s=0.01)
    assert code == 0, text
    assert "total time budget" in text
    assert (run_dir / "workflow.json").is_file()


def test_default_extract_stage_is_the_extractor() -> None:
    assert DEFAULT_STAGES["extract_spec"] is builder.extract.stage_extract
    assert not hasattr(pipeline, "stage_extract_spec")


def test_ensure_workflow_on_disk_writes_emergency_workflow(tmp_path: Path) -> None:
    from builder.trace import Trace

    run_dir = tmp_path / "run"
    trace = Trace(run_dir, REPO_ROOT)
    kind = pipeline.ensure_workflow_on_disk(run_dir, [tmp_path / "missing.md"], tmp_path / "nope.json", trace)
    assert kind == "fallback"
    assert _lint_errors(run_dir) == []
    assert pipeline.ensure_workflow_on_disk(run_dir, [], OPENAPI, trace) == "present"


def test_cli_keyless_dry_build() -> None:
    runs = REPO_ROOT / "runs"
    before = {p for p in runs.iterdir()} if runs.is_dir() else set()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    proc = subprocess.run(
        [sys.executable, "build.py", "briefs/brief-a.md", "--dry"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    new = ({p for p in runs.iterdir()} if runs.is_dir() else set()) - before
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(new) == 1, new
        run_dir = next(iter(new))
        assert (run_dir / "workflow.json").is_file()
        assert "DEGRADED" in (run_dir / "trace.log").read_text(encoding="utf-8")
        assert "degraded build" in proc.stderr
    finally:
        for d in new:
            shutil.rmtree(d)
