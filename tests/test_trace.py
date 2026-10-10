from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from builder.pipeline import STAGE_ORDER, BuildContext, make_slot, run_build
from builder.trace import REQUIRED_SECTIONS, Trace, builder_source_hash
from tests.conftest import REPO_ROOT, copy_builder_tree, load_fixture

OPENAPI = REPO_ROOT / "tools" / "openapi.json"


def _assert_sections_in_order(text: str) -> None:
    pos = -1
    for name in REQUIRED_SECTIONS:
        idx = text.find(f"== {name} ==")
        assert idx > pos, f"section {name} missing or out of order"
        pos = idx


def _section(text: str, name: str) -> str:
    start = text.index(f"== {name} ==")
    nxt = [text.find(f"== {n} ==", start + 1) for n in REQUIRED_SECTIONS]
    ends = [i for i in nxt if i > start]
    return text[start : min(ends) if ends else len(text)]


def _only_run_dir(runs: Path) -> Path:
    dirs = [p for p in runs.iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return dirs[0]


def test_trace_writes_every_section(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    brief = tmp_path / "input.md"
    brief.write_text("# a test brief\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    t = Trace(run_dir, root)
    t.set_inputs([brief], OPENAPI, None, "slot1", {"dry": True})
    t.log_stage("load_inputs", "ok", "read 1 brief(s)", 0.01)
    t.log_model_call("model-x", "extract spec", 1.2, 100, 50)
    t.log_artifact("created", "wf_1", "ag_1", 3)
    t.log_test("smoke", "ag_1", "1/1 passed", 1, 1)
    t.warn("a warning")
    assert t.finish(0) == 0

    text = (run_dir / "trace.log").read_text(encoding="utf-8")
    _assert_sections_in_order(text)
    assert hashlib.sha256(brief.read_bytes()).hexdigest() in text
    assert f"brief[0]: {brief.resolve()}" in text
    assert "openapi: " in text and hashlib.sha256(OPENAPI.read_bytes()).hexdigest() in text
    assert "model-x | extract spec | 1.20s | in=100 out=50" in text
    assert "created workflow_id=wf_1 agent_id=ag_1 version=3" in text
    assert "smoke ag_1: 1/1 passed passed=1/1" in text
    assert "a warning" in _section(text, "WARNINGS")
    assert f"start: {t.start_time}" in text
    assert f"end: {t.end_time}" in text and t.end_time
    assert "git_commit: no-commit" in text
    assert "source_sha256_start: " in text and "source_sha256_end: " in text
    assert "exit_code: 0" in text and "result: ok" in text

    lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert all(isinstance(e, dict) for e in events)
    kinds = [e["kind"] for e in events]
    assert kinds == ["start", "inputs", "stage", "model_call", "artifact", "test", "warning", "end"]


def test_empty_sections_say_none(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    t = Trace(tmp_path / "run", root)
    assert t.finish(0) == 0
    text = (tmp_path / "run" / "trace.log").read_text(encoding="utf-8")
    _assert_sections_in_order(text)
    for name in ("INPUTS", "STAGES", "MODEL CALLS", "FREYA ARTIFACTS", "TESTS ON AGENT", "WARNINGS", "ERRORS"):
        assert "(none)" in _section(text, name), name
    # second finish rewrites the same data
    assert t.finish(0) == 0
    assert (tmp_path / "run" / "trace.log").read_text(encoding="utf-8") == text


def test_source_hash_detects_change(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    h0 = builder_source_hash(root)
    pycache = root / "builder" / "__pycache__"
    pycache.mkdir(exist_ok=True)
    (pycache / "junk.py").write_text("x = 1\n", encoding="utf-8")
    assert builder_source_hash(root) == h0

    with open(root / "builder" / "trace.py", "a", encoding="utf-8") as f:
        f.write("# edited\n")
    h1 = builder_source_hash(root)
    assert h1 != h0

    (root / "builder" / "added.py").write_text("y = 2\n", encoding="utf-8")
    assert builder_source_hash(root) != h1


def test_finish_flags_source_change(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    brief = tmp_path / "input.md"
    brief.write_text("# a test brief\n", encoding="utf-8")

    def edit_source(ctx: BuildContext) -> str:
        with open(root / "builder" / "pipeline.py", "a", encoding="utf-8") as f:
            f.write("# changed during run\n")
        return "ok"

    code = run_build(
        root=root,
        briefs=[brief],
        openapi=OPENAPI,
        slot="t",
        dry=True,
        cases=None,
        runs_dir=tmp_path / "runs",
        stages={"extract_spec": edit_source},
    )
    assert code == 1
    text = (_only_run_dir(tmp_path / "runs") / "trace.log").read_text(encoding="utf-8")
    assert "builder source changed during run" in text
    assert "result: source-changed" in text


def test_stage_crash_is_contained(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    brief = tmp_path / "input.md"
    brief.write_text("# a test brief\n", encoding="utf-8")

    def fake_extract(ctx: BuildContext) -> str:
        ctx.spec = {"schema_version": "1"}
        return "fake spec"

    def boom(ctx: BuildContext) -> str:
        raise ValueError("boom")

    code = run_build(
        root=root,
        briefs=[brief],
        openapi=OPENAPI,
        slot="t",
        dry=True,
        cases=None,
        runs_dir=tmp_path / "runs",
        stages={"extract_spec": fake_extract, "compile_workflow": boom},
    )
    assert code == 1
    run_dir = _only_run_dir(tmp_path / "runs")
    text = (run_dir / "trace.log").read_text(encoding="utf-8")
    assert "failed: stage compile_workflow crashed on input" in text
    assert "ValueError: boom" in text
    stages = _section(text, "STAGES")
    for name in STAGE_ORDER:
        assert f"\n{name}: " in stages, name
    assert "== END ==" in text and "exit_code: 1" in text
    events = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    errors = [e for e in events if e["kind"] == "error"]
    assert errors and "Traceback" in errors[0]["traceback"]


def _run_with_injected_spec(tmp_path: Path, spec: dict[str, object]) -> tuple[int, Path, str]:
    brief = tmp_path / "input.md"
    brief.write_text("# a test brief\n", encoding="utf-8")

    def inject_spec(ctx: BuildContext) -> str:
        ctx.spec = spec
        return "injected spec"

    code = run_build(
        root=REPO_ROOT,
        briefs=[brief],
        openapi=OPENAPI,
        slot="t",
        dry=True,
        cases=None,
        runs_dir=tmp_path / "runs",
        stages={"extract_spec": inject_spec},
    )
    run_dir = _only_run_dir(tmp_path / "runs")
    return code, run_dir, (run_dir / "trace.log").read_text(encoding="utf-8")


def test_validate_stage_with_injected_spec(tmp_path: Path) -> None:
    spec = load_fixture("spec_brief_a.json")
    code, run_dir, text = _run_with_injected_spec(tmp_path, spec)
    assert code == 0
    assert "\nvalidate_spec: ok: spec valid" in _section(text, "STAGES")
    written = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    assert written == spec


def test_validate_stage_with_invalid_spec(tmp_path: Path) -> None:
    spec = load_fixture("spec_brief_a.json")
    del spec["bands"]
    code, run_dir, text = _run_with_injected_spec(tmp_path, spec)
    assert code == 1
    stages = _section(text, "STAGES")
    assert "\nvalidate_spec: failed: " in stages
    assert "spec invalid: 1 errors" in stages
    assert "$.bands: required" in _section(text, "WARNINGS")
    assert not (run_dir / "spec.json").exists()
    for name in STAGE_ORDER:
        assert f"\n{name}: " in stages, name
    assert "== END ==" in text


def test_slot_default_is_last_brief_stem() -> None:
    assert make_slot([Path("x/a.md"), Path("y/b.md")], None) == "b"
    assert make_slot([Path("x/a.md")], "custom_1.v2") == "custom_1.v2"
    assert make_slot([Path("x/we ird.md")], None) == "we_ird"
    with pytest.raises(ValueError):
        make_slot([Path("x/a.md")], "bad/slot")
    with pytest.raises(ValueError):
        make_slot([Path("x/a.md")], "")
