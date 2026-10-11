from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from builder.pipeline import run_build
from tests.conftest import FIXTURES_DIR, REPO_ROOT


class FakeFreya:
    def __init__(self, fail_save: bool = False, healthy: bool = True, stored_extra: bool = True) -> None:
        self.calls: list[str] = []
        self.fail_save = fail_save
        self.healthy = healthy
        self.stored: dict[str, Any] = {}

    def validate_workflow(self, name: str, workflow: dict) -> dict:
        self.calls.append("dry_run")
        return {"valid": True}

    def save_workflow(self, slot: str, workflow: dict, state_file: str = "") -> dict:
        self.calls.append("save")
        if self.fail_save:
            raise RuntimeError("server error")
        self.stored = {k: v for k, v in workflow.items() if k != "system_prompt"}
        return {"workflow_id": "wf_1", "agent_id": "ag_1", "version": 2}

    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        if path.startswith("/api/v2/workflow/"):
            self.calls.append("read_back")
            return {"workflow": {"id": "wf_1", "workflow": self.stored}}
        if path.endswith("/chat/message"):
            self.calls.append("smoke_test")
            if not self.healthy:
                raise RuntimeError("500 backend")
            return {"response": "Hello", "callId": "c1"}
        if path.endswith("/chat/end"):
            self.calls.append("chat_end")
            return {}
        raise AssertionError(path)

    def workspace_id(self) -> str:
        return "ws"

    def export_workflow(self, workflow_id: str, path: str) -> str:
        self.calls.append("export")
        Path(path).write_text("{}", encoding="utf-8")
        return path


def _build(tmp_path: Path, client: FakeFreya, dry: bool) -> tuple[int, Path]:
    dirs: list[Path] = []
    code = run_build(
        root=tmp_path,
        briefs=[REPO_ROOT / "briefs" / "brief-a.md"],
        openapi=REPO_ROOT / "tools" / "openapi.json",
        slot="slot-a",
        dry=dry,
        cases=None,
        runs_dir=tmp_path / "runs",
        on_run_dir=dirs.append,
        spec_path=FIXTURES_DIR / "spec_brief_a.json",
        client=client,
    )
    return code, dirs[0]


def test_dry_makes_no_client_calls_and_writes_files(tmp_path: Path) -> None:
    c = FakeFreya()
    code, run_dir = _build(tmp_path, c, dry=True)
    assert code == 0
    assert c.calls == []
    wf = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
    assert wf["nodes"]
    report = json.loads((run_dir / "compile_report.json").read_text(encoding="utf-8"))
    assert "warnings" in report and "to_verify" in report
    assert (run_dir / "trace.log").is_file()


def test_network_stage_order(tmp_path: Path) -> None:
    c = FakeFreya()
    code, run_dir = _build(tmp_path, c, dry=False)
    assert code == 0
    order = [x for x in c.calls if x != "chat_end"]
    assert order == ["dry_run", "save", "read_back", "export", "smoke_test"]
    assert c.calls[-1] == "chat_end"
    assert (run_dir / "slot-a.workflow.json").is_file()


def test_failing_save_is_contained(tmp_path: Path) -> None:
    c = FakeFreya(fail_save=True)
    code, run_dir = _build(tmp_path, c, dry=False)
    assert code == 1
    assert c.calls == ["dry_run", "save"]
    text = (run_dir / "trace.log").read_text(encoding="utf-8")
    assert "stage save crashed" in text
    assert "read_back: skipped: no saved workflow" in text
    assert "smoke_test: skipped" in text


def test_unhealthy_probe_logged_and_skipped(tmp_path: Path) -> None:
    c = FakeFreya(healthy=False)
    code, run_dir = _build(tmp_path, c, dry=False)
    assert code == 0
    text = (run_dir / "trace.log").read_text(encoding="utf-8")
    assert "model backend unhealthy" in text
    assert "smoke_test: skipped" in text


def test_read_back_differences_reach_warnings(tmp_path: Path) -> None:
    c = FakeFreya()
    _, run_dir = _build(tmp_path, c, dry=False)
    text = (run_dir / "trace.log").read_text(encoding="utf-8")
    assert "read_back: stripped system_prompt" in text


def test_rehearsal_spec_writes_degraded_line_and_list(tmp_path: Path) -> None:
    dirs: list[Path] = []
    code = run_build(
        root=tmp_path, briefs=[REPO_ROOT / "briefs" / "brief-a.md"], openapi=REPO_ROOT / "tools" / "openapi.json",
        slot="slot-r", dry=True, cases=None, runs_dir=tmp_path / "runs", on_run_dir=dirs.append,
        spec_path=FIXTURES_DIR / "spec_rehearsal.json", client=FakeFreya(),
    )
    assert code == 0
    assert "DEGRADED: compile: unstructured rule (deadline_formula)" in (dirs[0] / "trace.log").read_text(encoding="utf-8")
    report = json.loads((dirs[0] / "compile_report.json").read_text(encoding="utf-8"))
    assert len(report["degraded"]) >= 4
