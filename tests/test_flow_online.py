from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder import flow_docs, flow_online, flow_tools
from builder.llm import FakeLLM
from builder.trace import Trace
from tests.flow_fakes import STAGE_A, policy

PERSONAS = [{"id": f"p{i}", "kind": "happy", "name": f"n{i}", "prompt": "script", "successCriteria": ["ok"],
             "variables": {"ownerId": "1", "bogus": "x"}} for i in range(3)]
PATCH = {"diagnosis": "d", "ops": [{"op": "append", "path": "never", "value": "Never shout"}]}


class FakeClient:
    """sim_script: one dict {persona index: pass?} per simulation, in order."""

    def __init__(self, sim_script: list[dict[int, bool]], boom: bool = False) -> None:
        self.script, self.boom, self.saves, self.sims = list(sim_script), boom, [], []
        self._runs: dict[str, list[dict]] = {}

    def create_personas(self, personas: list[dict]) -> dict:
        return {"personas": [{"id": f"pid{i}"} for i, _ in enumerate(personas)]}

    def run_simulation(self, agent_id: str, name: str, variables: dict, max_seconds: int = 0) -> tuple:
        if self.boom:
            raise RuntimeError("POST /api/v2/simulations -> 504: gateway timeout")
        verdict = self.script.pop(0)
        self.sims.append(sorted(variables))
        self._runs[str(len(self.sims))] = [{"personaId": pid, "outcome": "pass" if verdict.get(int(pid[3:]), True) else "fail",
                                            "reason": "r", "callId": "c" + pid} for pid in variables]
        return str(len(self.sims)), "it", "completed"

    def get_iteration(self, sim_id: str, it_id: str) -> dict:
        return {"runs": self._runs[sim_id]}

    def get_call(self, call_id: str) -> dict:
        return {"messages": [{"role": "user", "message": "hi"}, {"role": "assistant", "message": "hello"}]}

    def save_workflow(self, slot: str, wf: dict, state_file: str = "") -> dict:
        self.saves.append(wf)
        return {"workflow_id": "w", "agent_id": "a", "version": len(self.saves)}

    def validate_workflow(self, name: str, wf: dict) -> dict:
        return {}


def make_ctx(tmp_path: Path, client: FakeClient, llm: FakeLLM) -> SimpleNamespace:
    plan = flow_tools.normalise(STAGE_A, "https://mock.example")
    from builder import flow_assemble
    wf, _ = flow_assemble.assemble(policy(), plan)
    docset = flow_docs.DocSet(test_data_summary="rows")
    run_dir = tmp_path / "run"
    return SimpleNamespace(client=client, slot="s", root=tmp_path, run_dir=run_dir, trace=Trace(run_dir, Path.cwd()),
                           saved={"workflow_id": "w", "agent_id": "a"}, workflow=wf, policy=policy(), plan=plan,
                           docset=docset, llm=llm, server_url="https://mock.example")


def run(ctx: SimpleNamespace, healthy: bool = True) -> str:
    exports: list[int] = []
    out = flow_online.run_online(ctx, lambda url: (healthy, "health"), lambda c: exports.append(1) or "x")
    assert exports or not healthy
    return out


def personas_json() -> str:
    return json.dumps(PERSONAS)


def test_all_pass_stops_early(tmp_path: Path) -> None:
    client = FakeClient([{}])
    ctx = make_ctx(tmp_path, client, FakeLLM([personas_json()]))
    assert "best 3/3" in run(ctx)
    assert len(client.sims) == 1 and client.saves == []
    log = json.loads((ctx.run_dir / "online_test.json").read_text())
    assert "bogus" not in log["personas"][0]["variables"]


def test_improving_patch_is_kept_and_only_failed_rerun(tmp_path: Path) -> None:
    client = FakeClient([{1: False}, {}, {}])
    ctx = make_ctx(tmp_path, client, FakeLLM([personas_json(), json.dumps(PATCH)]))
    assert "best 3/3" in run(ctx)
    assert client.sims[1] == ["pid1"] and len(client.sims[2]) == 3
    assert len(client.saves) == 1 and "Never shout" in json.dumps(client.saves[0])
    assert "Never shout" in json.dumps(ctx.workflow)


def test_regression_rolls_back_and_resaves_best(tmp_path: Path) -> None:
    client = FakeClient([{1: False}, {}, {0: False, 2: False}])
    ctx = make_ctx(tmp_path, client, FakeLLM([personas_json(), json.dumps(PATCH)]))
    original = json.dumps(ctx.workflow)
    run(ctx)
    assert len(client.saves) == 2 and json.dumps(client.saves[1]) == original
    assert json.dumps(ctx.workflow) == original
    log = json.loads((ctx.run_dir / "online_test.json").read_text())
    assert "rolled back" in log["patches"][0]["decision"]


def test_no_improvement_rolls_back(tmp_path: Path) -> None:
    client = FakeClient([{1: False}, {1: False}])
    ctx = make_ctx(tmp_path, client, FakeLLM([personas_json(), json.dumps(PATCH)]))
    run(ctx)
    assert len(client.saves) == 2 and "Never shout" not in json.dumps(ctx.workflow)


def test_mock_unreachable_skips(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeClient([]), FakeLLM([]))
    with pytest.raises(flow_online.Skipped):
        flow_online.run_online(ctx, lambda url: (False, "down"), lambda c: "x")
    assert any("DEGRADED" in w for w in ctx.trace.warnings)


def test_platform_5xx_degrades_without_raising(tmp_path: Path) -> None:
    client = FakeClient([], boom=True)
    ctx = make_ctx(tmp_path, client, FakeLLM([personas_json()]))
    out = flow_online.run_online(ctx, lambda url: (True, "ok"), lambda c: "x")
    assert "DEGRADED" in out and client.saves == []
    assert any("DEGRADED" in w for w in ctx.trace.warnings)
