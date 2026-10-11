from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from builder import flow_assemble, flow_docs, flow_llm, flow_mode, flow_policy, flow_tools, folder_input
from builder.llm import FakeLLM, LLMError
from builder.trace import Trace
from tests.flow_fakes import STAGE_A, policy

SERVER = "https://mock.example:8443/ignored"


def test_decide_mode_variants() -> None:
    assert flow_mode.decide_mode(None, "flow")[0] == "flow"
    assert flow_mode.decide_mode(None, "policy")[0] == "policy"
    assert flow_mode.decide_mode(None, "auto")[0] == "policy"
    no_contract = folder_input.FolderInputs()
    mode, why = flow_mode.decide_mode(no_contract, "auto")
    assert mode == "flow" and "no OpenAPI" in why
    with_contract = folder_input.FolderInputs(openapi_path=Path("x.json"))
    mode, why = flow_mode.decide_mode(with_contract, "auto", {"paths": {"/a/only_one": {"post": {}}}})
    assert mode == "flow" and "verify_identity" in why
    full = json.loads(Path("tools/openapi.json").read_text(encoding="utf-8"))
    assert flow_mode.decide_mode(with_contract, "auto", full)[0] == "policy"


def test_docs_order_priority_and_trim(tmp_path: Path) -> None:
    (tmp_path / "2030-01-05-note.txt").write_text("middle " * 10)
    (tmp_path / "2030-01-09-decisions.txt").write_text("newest " * 10)
    (tmp_path / "plain.md").write_text("Date: 2029-12-31\nold")
    (tmp_path / "undated.md").write_text("x" * 60_000)
    (tmp_path / "customers.json").write_text(json.dumps({"rows": [{"id": f"c{i}", "name": "n", "items": [1, 2]} for i in range(20)]}))
    (tmp_path / "f.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    ds = flow_docs.collect(tmp_path, budget_chars=30_000)
    names = [d.rel for d in ds.docs]
    assert names == ["undated.md", "plain.md", "2030-01-05-note.txt", "2030-01-09-decisions.txt"]
    assert ds.docs[-1].priority == "highest"
    assert all(len(d.text) <= flow_docs.PER_DOC_CHARS + 20 for d in ds.docs)
    assert "[trimmed]" in next(d for d in ds.docs if d.rel == "undated.md").text
    assert "customers.json" not in " ".join(names) and "20 records" in ds.test_data_summary
    assert any("pdf skipped" in n for n in ds.notes)
    assert "newest overrides all" in flow_docs.render(ds)


def test_normalise_and_url_rewrite() -> None:
    plan = flow_tools.normalise(STAGE_A, SERVER)
    assert plan.names() == ["pets_list", "visit_book"]
    assert plan.tools[0]["url"] == "https://mock.example:8443/api/v2/pets.list"
    assert plan.tools[1]["url"] == "https://mock.example:8443/api/v2/visit.book"
    assert plan.tools[1]["fields"][1]["source"] == "model"
    assert plan.tools[0]["method"] == "POST"
    assert not any(w.startswith("WARNING") for w in plan.warnings)
    local = flow_tools.normalise(STAGE_A, None)
    assert local.tools[0]["url"] == "https://clinic.example/api/v2/pets.list"
    assert any(w.startswith("WARNING") for w in local.warnings)
    with pytest.raises(ValueError):
        flow_tools.normalise(STAGE_A, "not-a-url")


def test_tool_json_and_envelope_hook() -> None:
    plan = flow_tools.normalise(STAGE_A, SERVER)
    tools = flow_tools.make_tools(plan)
    t0, t1 = tools
    props = t0["function"]["parameters"]["properties"]["body"]["properties"]
    assert props["owner"]["default"] == "{{ownerId}}" and props["flow"]["default"] == "Visit"
    assert t0["config"]["timeoutMs"] >= 30000 and t1["config"]["maxRetries"] == 0
    assert t1["function"]["parameters"]["properties"]["body"]["properties"]["petId"]["default"] == "{{petId}}"
    code = t0["pre_process_code"]
    ast.parse(code)
    assert "{{" not in code and "}}" not in code
    cfg = {"body": {"owner": "7"}}
    exec(code, {"config": cfg, "vars": {}})
    assert cfg["body"] == {"data": {"owner": "7"}, "call": {"lang": "xx"}}


def test_policy_checks_patch_and_assembly() -> None:
    p = policy()
    names = ["pets_list", "visit_book"]
    assert flow_policy.check_policy(p, names) == []
    bad = dict(p, flow_steps=[{"id": "x", "call_tool": "nope"}])
    assert any("unknown tool" in e for e in flow_policy.check_policy(bad, names))
    assert any("missing key" in e for e in flow_policy.check_policy({"language": "x"}, names))
    patched = flow_policy.apply_patch(p, [{"op": "set", "path": "rules[0].text", "value": "Be kind"},
                                          {"op": "append", "path": "never", "value": "Never shout"},
                                          {"op": "remove", "path": "reading_rules[0]"}])
    assert patched["rules"][0]["text"] == "Be kind" and patched["never"][-1] == "Never shout" and not patched["reading_rules"]
    assert p["rules"][0]["text"] != "Be kind"
    out = flow_policy.assemble_prompts(policy(), ["ownerId", "stamp"], names)
    text = out["system_prompt"] + out["task_message"]
    assert "NEVER:" in text and "Bye now." in text and "Passing you to a person." in text
    assert "only when: the owner is known" in text and "SECRETTOPIC" not in text
    assert "{{ownerId}}" in text and "{{junk}}" not in text
    assert text.replace("{{ownerId}}", "").replace("{{stamp}}", "").count("{{") == 0
    assert {h["id"] for h in out["handovers"]} == {"to_human", "silence_end", "tries_end"}


def test_assemble_workflow_shape() -> None:
    plan = flow_tools.normalise(STAGE_A, SERVER)
    wf, info = flow_assemble.assemble(policy(), plan)
    main = wf["nodes"]["main"]
    assert wf["initial_node"] == "main" and main["node_type"] == "conversation"
    assert main["on_enter"] == {"type": "tts", "text": "Welcome to the clinic."}
    assert main["max_node_turns"] and main["max_turn_transition"] == "end_limit"
    tr = {t["target_node"]: t for t in main["transitions"]}
    assert tr["end_to_human"]["is_tool_call"] is True
    end = wf["nodes"]["end_to_human"]
    assert end["node_type"] == "end_call"
    assert end["terminal"]["function"]["name"] == "handover_to_human" != end["name"]
    assert wf["test_variables"]["ownerId"] == "123"
    assert flow_assemble.local_checks(wf, plan) == []
    assert info["system_prompt_chars"] > 0


class FakeFreya:
    def __init__(self, fail: int) -> None:
        self.fail, self.calls = fail, 0

    def validate_workflow(self, name: str, wf: dict) -> dict:
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("POST /api/v2/workflow -> 400: bad thing")
        return {"ok": True}


def _trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "run", Path.cwd())


def test_validation_repair_loop(tmp_path: Path) -> None:
    plan = flow_tools.normalise(STAGE_A, SERVER)
    llm = FakeLLM([json.dumps(policy()), json.dumps(policy())])
    freya = FakeFreya(fail=2)
    wf, pol, report = flow_assemble.validate_with_repair(policy(), plan, "s", llm, freya, _trace(tmp_path), dry=False)
    assert [r["ok"] for r in report["validation_rounds"]] == [False, False, True] and wf


def test_validation_always_failing_falls_back(tmp_path: Path) -> None:
    plan = flow_tools.normalise(STAGE_A, SERVER)
    llm = FakeLLM([json.dumps(policy())] * 3)
    tr = _trace(tmp_path)
    wf, _pol, report = flow_assemble.validate_with_repair(policy(), plan, "s", llm, FakeFreya(fail=99), tr, dry=False)
    assert len(report["validation_rounds"]) == 4 and wf["initial_node"] == "main"
    assert any("DEGRADED" in w for w in tr.warnings)


def test_ask_json_parsing_and_retry() -> None:
    assert flow_llm.extract_json('noise ```json\n{"a": [1]}\n``` tail') == {"a": [1]}
    llm = FakeLLM(["not json", '{"ok": true}'])
    assert flow_llm.ask_json(llm, "p", "s", "u") == {"ok": True}
    with pytest.raises(LLMError):
        flow_llm.ask_json(FakeLLM(["x", "y"]), "p", "s", "u")
