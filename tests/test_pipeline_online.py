from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder import online_repair, pipeline
from builder.online_cases import generate_cases, load_carriers
from builder.pipeline import run_build
from builder.spec import load_spec, spec_to_dict
from tests.conftest import FIXTURES_DIR, REPO_ROOT
from tests.online_fakes import FakeBank, FakeSimFreya, World


class StageFreya(FakeSimFreya):
    """FakeSimFreya plus the calls the earlier stages make."""

    def __init__(self, world: World, healthy: bool = True, **kw: Any) -> None:
        super().__init__(world, **kw)
        self.healthy = healthy

    def validate_workflow(self, name: str, workflow: dict) -> dict:
        self.calls.append("dry_run")
        return {"valid": True}

    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        if path.startswith("/api/v2/workflow/"):
            return {"workflow": {"id": "wf_1", "workflow": {k: v for k, v in self.world.live.items() if k != "system_prompt"}}}
        if path.endswith("/chat/message"):
            if not self.healthy:
                raise RuntimeError("500 backend")
            return {"response": "Hello", "callId": "c1"}
        if path.endswith("/chat/end"):
            return {}
        raise AssertionError(path)

    def workspace_id(self) -> str:
        return "ws"


SPEC_FILE = FIXTURES_DIR / "spec_brief_a.json"


def make_world(root: Path) -> World:
    shutil.copy(REPO_ROOT / "public-test-set.json", root / "public-test-set.json")
    spec = spec_to_dict(load_spec(json.loads(SPEC_FILE.read_text(encoding="utf-8"))))
    cases = generate_cases(spec, load_carriers(root / "public-test-set.json"))[0]
    return World(cases)


def build(root: Path, client: Any, bank: Any = None, dry: bool = False, **kw: Any) -> tuple[int, Path]:
    dirs: list[Path] = []
    code = run_build(root=root, briefs=[REPO_ROOT / "briefs" / "brief-a.md"], openapi=REPO_ROOT / "tools" / "openapi.json",
                     slot="slot-a", dry=dry, cases=kw.pop("cases", None), runs_dir=root / "runs", on_run_dir=dirs.append,
                     spec_path=SPEC_FILE, client=client, bank=bank, **kw)
    return code, dirs[0]


def trace(run_dir: Path) -> str:
    return (run_dir / "trace.log").read_text(encoding="utf-8")


def case_id(world: World, k: str) -> str:
    return next(c.id for c in world.cases.values() if c.kind == k)


def test_full_run_writes_the_report(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, FakeBank(world))
    assert code == 0
    assert "online_test: ok" in trace(run_dir)
    report = json.loads((run_dir / "online_test.json").read_text(encoding="utf-8"))
    assert report["passed"] == report["scored"] == len(world.cases) and report["decision"]["kept"] == "baseline"
    assert {c["id"] for c in report["cases"]} == set(world.cases) and report["skipped_kinds"]
    assert client.sim_count == 2 and client.calls.count("save") == 1
    assert (tmp_path / ".personas.json").is_file()


def test_dry_skips_with_zero_client_calls(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, FakeBank(world), dry=True)
    assert code == 0 and client.calls == []
    assert "online_test: skipped: --dry" in trace(run_dir)


def test_no_carrier_file_skips_loudly_without_client_calls(tmp_path: Path) -> None:
    world = World([])
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client)
    assert code == 0
    assert "DEGRADED: online_test skipped: no carrier accounts" in trace(run_dir)
    assert "create_personas" not in client.calls and "run_simulation" not in client.calls


def test_unhealthy_backend_skips_the_stage(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world, healthy=False)
    code, run_dir = build(tmp_path, client, FakeBank(world))
    assert code == 0 and "online_test: skipped: model backend unhealthy" in trace(run_dir)
    assert "run_simulation" not in client.calls


def test_missing_bank_key_skips_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREYA_BANK_TEAM_KEY", raising=False)
    world = make_world(tmp_path)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client)
    assert code == 0 and "DEGRADED: online_test skipped: scoring tools or FREYA_BANK_TEAM_KEY missing" in trace(run_dir)
    assert "run_simulation" not in client.calls


def test_improving_repair_reaches_workflow_json_and_the_export(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    bad = case_id(world, "date_over_limit")
    world.modes[bad] = "behavior"
    world.fixed_by_marker.add(bad)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, FakeBank(world))
    assert code == 0
    assert online_repair.MARKER in (run_dir / "workflow.json").read_text(encoding="utf-8")
    assert online_repair.MARKER in (run_dir / "slot-a.workflow.json").read_text(encoding="utf-8")
    report = json.loads((run_dir / "online_test.json").read_text(encoding="utf-8"))
    assert report["decision"]["kept"] == "repaired" and report["repairs_tried"] == ["reminder:date_over_limit"]
    assert client.calls.count("export") == 2


def test_regressing_repair_is_restored_and_exported(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    bad, victim = case_id(world, "date_over_limit"), case_id(world, "promise_full")
    world.modes[bad] = "behavior"
    world.fixed_by_marker.add(bad)
    world.broken_by_marker.add(victim)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, FakeBank(world))
    assert code == 0
    assert online_repair.MARKER not in (run_dir / "workflow.json").read_text(encoding="utf-8")
    assert online_repair.MARKER not in json.dumps(world.live)
    assert client.calls.count("save") == 3  # build save, repair, restore
    report = json.loads((run_dir / "online_test.json").read_text(encoding="utf-8"))
    assert report["decision"]["kept"] == "baseline" and report["decision"]["rolled_back"]


def test_a_crash_in_the_stage_is_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> None:
        raise RuntimeError("controller bug")

    monkeypatch.setattr(online_repair, "run_loop", boom)
    world = make_world(tmp_path)
    code, run_dir = build(tmp_path, StageFreya(world), FakeBank(world))
    assert code == 1 and "stage online_test crashed" in trace(run_dir)
    assert (run_dir / "workflow.json").is_file()


def test_a_slow_stage_is_abandoned_and_the_build_still_ends(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world, sim_delay=3.0)
    code, run_dir = build(tmp_path, client, FakeBank(world), stage_timeouts={"online_test": 0.3})
    assert code == 1 and "stage online_test exceeded" in trace(run_dir)
    assert (run_dir / "workflow.json").is_file()


def test_total_budget_gone_skips_the_stage_with_a_degraded_line(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, FakeBank(world), budget_s=0.0)
    assert "DEGRADED: stage online_test skipped: total time budget exhausted" in trace(run_dir)
    assert "run_simulation" not in client.calls


def test_spec_repair_gate_rejects_lint_errors_and_accepts_clean_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    openapi = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
    ctx = SimpleNamespace(results={"selfcheck": {"failed": ["x", "y"]}}, openapi_doc=openapi)
    gate = pipeline._online_recompile(ctx)  # type: ignore[arg-type]
    assert gate(spec) is not None
    monkeypatch.setattr(pipeline, "lint", lambda wf: [SimpleNamespace(severity="error")])
    assert gate(spec) is None
    monkeypatch.undo()
    assert gate({"not": "a spec"}) is None


def _mock_setup(monkeypatch: pytest.MonkeyPatch, world: World, **fake_kw: Any) -> list[Any]:
    """Replace the mock bank with a FakeBank so no network is used; records the constructed sources."""
    made: list[Any] = []

    class Spy(FakeBank):
        def __init__(self, source: Any) -> None:
            super().__init__(world, **fake_kw)
            made.append(source)

    monkeypatch.setattr(pipeline.online_runner, "MockBank", Spy)
    return made


def test_server_url_selects_the_mock_bank_without_a_team_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREYA_BANK_TEAM_KEY", raising=False)
    world = make_world(tmp_path)
    made = _mock_setup(monkeypatch, world)
    code, run_dir = build(tmp_path, StageFreya(world), server_url="https://mock.example/v1")
    assert code == 0 and "online_test: ok" in trace(run_dir)
    assert len(made) == 1 and made[0].origin == "https://mock.example"


def test_no_server_url_keeps_the_organizer_bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREYA_BANK_TEAM_KEY", raising=False)
    world = make_world(tmp_path)
    made = _mock_setup(monkeypatch, world)
    code, run_dir = build(tmp_path, StageFreya(world))
    assert code == 0 and not made and "scoring tools or FREYA_BANK_TEAM_KEY missing" in trace(run_dir)


def test_unreachable_ledger_degrades_to_infra_and_keeps_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def down(*a: Any, **kw: Any) -> Any:
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("urllib.request.urlopen", down)
    world = make_world(tmp_path)
    client = StageFreya(world)
    code, run_dir = build(tmp_path, client, server_url="https://mock.invalid/v1")
    assert code == 0 and (run_dir / "workflow.json").is_file()
    report = json.loads((run_dir / "online_test.json").read_text(encoding="utf-8"))
    assert report["scored"] == 0 and report["excluded"] > 0 and report["decision"]["kept"] == "baseline"
    assert client.calls.count("save") == 1 and "run_simulation" not in client.calls
    assert "DEGRADED: online_test" in trace(run_dir)


def test_folder_customers_file_supplies_the_carriers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.loads((REPO_ROOT / "public-test-set.json").read_text(encoding="utf-8"))
    folder = tmp_path / "folder"
    folder.mkdir()
    renamed = [{**e, "id": "ZZ-" + e["id"]} for e in data]
    (folder / "people.json").write_text(json.dumps(renamed), encoding="utf-8")
    spec = spec_to_dict(load_spec(json.loads(SPEC_FILE.read_text(encoding="utf-8"))))
    mine = World(generate_cases(spec, load_carriers(folder / "people.json"))[0])
    code, run_dir = build(tmp_path, StageFreya(mine), FakeBank(mine), cases=folder / "people.json")
    assert code == 0
    report = json.loads((run_dir / "online_test.json").read_text(encoding="utf-8"))
    assert report["carriers_file"].endswith("people.json")
    assert report["cases"] and all(c["carrier"].startswith("ZZ-") for c in report["cases"])
