"""scripts/make_submission.py: bundle contents, manifest, missing export, no git."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT


def _load():
    spec = importlib.util.spec_from_file_location("make_submission", REPO_ROOT / "scripts" / "make_submission.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_submission"] = mod
    spec.loader.exec_module(mod)
    return mod


ms = _load()


def make_run(tmp: Path, with_export: bool = True, with_optional: bool = True) -> Path:
    run = tmp / "runs" / "slot-x-20300101-000000"
    run.mkdir(parents=True)
    (run / "trace.log").write_text("== START ==\n", encoding="utf-8")
    events = [
        {"kind": "inputs", "slot": "slot-x"},
        {"kind": "artifact", "action": "saved", "workflow_id": "wf_9", "agent_id": "ag_9", "version": 4},
    ]
    (run / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if with_export:
        (run / "slot-x.workflow.json").write_text('{"workflow": {}}', encoding="utf-8")
    if with_optional:
        (run / "compile_report.json").write_text("{}", encoding="utf-8")
        (run / "spec.json").write_text("{}", encoding="utf-8")
    return run


def test_bundle_copies_files_and_manifest_has_hashes(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    out = tmp_path / "bundle"
    assert ms.make_submission(run, out) == 0
    names = {p.name for p in out.iterdir()}
    assert names == {"slot-x.workflow.json", "trace.log", "trace.jsonl", "compile_report.json", "spec.json", "MANIFEST.md"}
    manifest = (out / "MANIFEST.md").read_text(encoding="utf-8")
    for n in names - {"MANIFEST.md"}:
        assert hashlib.sha256((out / n).read_bytes()).hexdigest() in manifest
    assert "wf_9" in manifest and "ag_9" in manifest and "git add -f" in manifest
    assert not (out / ".git").exists() and not (run / ".git").exists()


def test_missing_optional_files_still_succeed(tmp_path: Path, capsys) -> None:
    run = make_run(tmp_path, with_optional=False)
    assert ms.make_submission(run, tmp_path / "b") == 0
    assert "WARNING" in capsys.readouterr().err


def test_missing_export_without_exporter_exits_2(tmp_path: Path, capsys) -> None:
    run = make_run(tmp_path, with_export=False)
    assert ms.make_submission(run, tmp_path / "b", export=None) == 2
    assert "ERROR" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def test_missing_export_uses_exporter(tmp_path: Path) -> None:
    run = make_run(tmp_path, with_export=False)
    seen: list[str] = []

    def fake(workflow_id: str, path: str) -> str:
        seen.append(workflow_id)
        Path(path).write_text("{}", encoding="utf-8")
        return path

    assert ms.make_submission(run, tmp_path / "b", export=fake) == 0
    assert seen == ["wf_9"] and (tmp_path / "b" / "slot-x.workflow.json").is_file()


def test_missing_trace_log_exits_2(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    (run / "trace.log").unlink()
    assert ms.make_submission(run, tmp_path / "b") == 2


def test_stage_export_writes_workflow_file_in_run_dir(tmp_path: Path) -> None:
    from tests.online_fakes import FakeBank
    from tests.test_pipeline_online import StageFreya, build, make_world

    world = make_world(tmp_path)
    code, run_dir = build(tmp_path, StageFreya(world), FakeBank(world))
    assert code == 0 and (run_dir / "slot-a.workflow.json").is_file()
