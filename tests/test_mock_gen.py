"""Tests for mock/gen_mock.py and the generated Worker (run offline through node)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "mock" / "gen_mock.py"
FLOW = ROOT / "mock" / "test" / "run_flow.mjs"
CONTRACT = ROOT / "tools" / "openapi.json"
CASES = ROOT / "public-test-set.json"

sys.path.insert(0, str(ROOT / "tools"))
import score  # noqa: E402  (stdlib-only organiser scorer)

_spec = importlib.util.spec_from_file_location("gen_mock", GEN)
gen_mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_mock)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    assert gen_mock.main(["--contract", str(CONTRACT), "--customers", str(CASES), "--out", str(out)]) == 0
    (out / "package.json").write_text('{"type": "module"}\n')
    return out


def run_flow(dist: Path, steps: list[dict]) -> dict:
    proc = subprocess.run(
        ["node", str(FLOW), str(dist)], input=json.dumps(steps), capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def cases() -> dict:
    return {c["id"]: c for c in json.loads(CASES.read_text())}


def tool(name: str, body: dict) -> dict:
    return {"method": "POST", "path": f"/v1/{name}", "body": {"customer_id": "$customer_id", **body}}


def session(case_id: str) -> dict:
    return {"method": "POST", "path": "/v1/practice/sessions", "body": {"case": case_id}}


def verify_steps(case: dict) -> list[dict]:
    return [tool("verify_identity", {"field": k, "answer": v}) for k, v in case["identity"].items()]


def test_generator_outputs(dist):
    for name in ("worker.js", "config.json", "data.json", "wrangler.toml"):
        assert (dist / name).is_file()
    cfg = json.loads((dist / "config.json").read_text())
    assert cfg["roles"] == {
        "close": "close_call", "transfer": "transfer_to_agent", "callback": "schedule_callback",
        "promise": "create_promise", "note": "add_note", "verify": "verify_identity", "account": "get_account",
    }
    assert set(cfg["gated_tools"]) == {"get_account", "create_promise"}
    assert cfg["base_path"] == "/v1"
    toml = (dist / "wrangler.toml").read_text()
    assert 'name = "' in toml and "compatibility_date" in toml and "# [[kv_namespaces]]" in toml


def test_all_customers_in_data(dist):
    data = json.loads((dist / "data.json").read_text())
    assert {c["id"] for c in data["customers"]} == set(cases())
    pub10 = next(c for c in data["customers"] if c["id"] == "PUB-10")
    assert pub10["faults"] == [{"tool": "create_promise", "fail_first_n": 1}]
    assert [p["product_id"] for p in pub10["products"]] == ["p1"]


def test_unknown_contract_warns_and_exits_zero(tmp_path, capsys):
    bad = tmp_path / "contract.txt"
    bad.write_text("this is not a contract")
    rc = gen_mock.main(["--contract", str(bad), "--customers", str(CASES), "--out", str(tmp_path / "o")])
    assert rc == 0
    assert "WARNING (loud)" in capsys.readouterr().err
    assert (tmp_path / "o" / "worker.js").is_file()


@needs_node
def test_sessions_for_every_customer(dist):
    ids = list(cases())
    out = run_flow(dist, [session(i) for i in ids])
    assert len(out["steps"]) == len(ids)
    for res in out["steps"]:
        assert res["status"] == 200 and res["body"]["variables"]["customer_id"]


@needs_node
def test_pub01_happy_path_scores(dist):
    case = cases()["PUB-01"]
    steps = [session("PUB-01"), *verify_steps(case), tool("get_account", {})]
    steps += [tool("create_promise", p) for p in case["expected"]["promises"]]
    steps.append(tool("close_call", {"outcome": case["expected"]["outcome"]}))
    out = run_flow(dist, steps)
    assert all(s["status"] == 200 for s in out["steps"])
    assert out["steps"][-3]["body"]["ok"] is True
    assert out["steps"][3]["body"]["products"][0]["product_id"] == "p1"
    assert score.score_ledger(case, out["ledger"]) == {"pass": True, "failed": []}


@needs_node
def test_pub10_fault_then_retry_passes(dist):
    case = cases()["PUB-10"]
    promise = case["expected"]["promises"][0]
    steps = [session("PUB-10"), *verify_steps(case), tool("get_account", {}),
             tool("create_promise", promise), tool("create_promise", promise),
             tool("close_call", {"outcome": "promise"})]
    out = run_flow(dist, steps)
    first, second = out["steps"][-3], out["steps"][-2]
    assert first["status"] == 500 and first["body"]["ok"] is False
    assert second["status"] == 200 and second["body"]["ok"] is True
    assert len(out["ledger"]["failures"]) == 1
    assert score.score_ledger(case, out["ledger"])["pass"] is True


@needs_node
def test_identity_gate_error_string(dist):
    out = run_flow(dist, [session("PUB-01"), tool("get_account", {})])
    assert out["steps"][-1]["body"] == {"ok": False, "error": "identity not verified"}
    assert out["ledger"]["calls"][-1]["result"]["error"] == "identity not verified"


@needs_node
def test_validation_and_bad_json(dist):
    out = run_flow(dist, [
        session("PUB-01"),
        tool("close_call", {"outcome": "bogus"}),
        {"method": "POST", "path": "/v1/close_call", "body": "{not json"},
        {"method": "GET", "path": "/v1/health"},
    ])
    assert out["steps"][1]["body"]["ok"] is False and "outcome" in out["steps"][1]["body"]["error"]
    assert out["steps"][2]["status"] == 400
    assert out["steps"][3]["body"]["storage"] == "kv"
