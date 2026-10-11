from __future__ import annotations

import json
from pathlib import Path

import build
from builder import flow_pipeline
from builder.llm import FakeLLM, LLMConfigError
from tests.flow_fakes import STAGE_A, policy


def _folder(tmp_path: Path) -> Path:
    f = tmp_path / "case"
    f.mkdir(exist_ok=True)
    (f / "2030-01-01-contract.md").write_text("Clinic contract. Services: pets.list, visit.book.")
    (f / "2030-01-03-recap.txt").write_text("Date: 2030-01-03\nDecision: visits are booked after a yes.")
    return f


def test_dry_build_writes_artifacts(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(STAGE_A), json.dumps(policy())])
    code = flow_pipeline.run_flow_build(Path.cwd(), _folder(tmp_path), "t-slot", True, "https://mock.example", "forced", runs_dir=tmp_path / "runs", llm=llm)
    run = next((tmp_path / "runs").iterdir())
    assert code == 0
    for name in ("workflow.json", "trace.log", "trace.jsonl", "compile_report.json", "spec.json", "conflicts_report.json", "stage_a_tools.json"):
        assert (run / name).is_file(), name
    log = (run / "trace.log").read_text()
    assert "mode: flow because forced" in log and "end_to_human->handover_to_human" in log
    report = json.loads((run / "compile_report.json").read_text())
    assert report["tools"][0]["url"] == "https://mock.example/api/v2/pets.list"
    assert "SECRETTOPIC" not in json.loads((run / "workflow.json").read_text())["system_prompt"]


def test_missing_llm_key_still_leaves_workflow(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise LLMConfigError("no key")
    monkeypatch.setattr(flow_pipeline, "LLMClient", boom)
    code = flow_pipeline.run_flow_build(Path.cwd(), _folder(tmp_path), "t-slot", True, None, "forced", runs_dir=tmp_path / "runs")
    run = next((tmp_path / "runs").iterdir())
    wf = json.loads((run / "workflow.json").read_text())
    assert code != 0 and wf["initial_node"] == "main" and wf["tools"] == []
    assert "DEGRADED" in (run / "trace.log").read_text()


def test_cli_picks_flow_without_contract(tmp_path: Path) -> None:
    args = build.parse_args(["--folder", str(_folder(tmp_path)), "--slot", "x", "--server-url", "https://mock.example"])
    assert args.mode_resolved == "flow" and "no OpenAPI" in args.mode_why
    forced = build.parse_args(["--folder", str(_folder(tmp_path)), "--slot", "x", "--mode", "policy"])
    assert forced.mode_resolved == "policy"
