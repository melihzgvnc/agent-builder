"""Extractor tests with FakeLLM: no network."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder import extract as ex
from builder.extract import deterministic_coverage, extract_spec, parse_json, stage_extract
from builder.llm import FakeLLM, LLMError
from builder.spec import validate_spec
from tests.conftest import REPO_ROOT, load_fixture

OPENAPI: dict[str, Any] = {
    "paths": {
        "/verify_identity": {"post": {"summary": "check", "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"field": {"type": "string", "enum": ["dob", "phone"]}}}}}}}},
        "/add_note": {"post": {"summary": "note", "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"reason": {"type": "string", "enum": ["refused", "hardship"]}}}}}}}},
        "/close_call": {"post": {"summary": "close", "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"outcome": {"type": "string", "enum": ["promise", "none"]}}}}}}}},
    }
}


def base_spec() -> dict[str, Any]:
    return load_fixture("spec_brief_a.json")


def small_spec() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "meta": {"bank_name": "B", "agent_identity": "bot"},
        "account_fields": {"product_id": "id"},
        "calendar": {"weekend_days": ["saturday", "sunday"]},
        "bands": [{"name": "x", "days_late_min": 1, "days_late_max": 9,
                   "deadline": {"kind": "calendar_days_after_today", "n": 3}, "persuasion_attempts": 2}],
        "closing": {"outcome_table": [{"outcome": "promise", "when": "promise made"}]},
    }


def tool(spec: dict[str, Any]) -> dict[str, Any]:
    return {"tool_inputs": [spec]}


def write_briefs(tmp_path: Path, *texts: str) -> list[Path]:
    paths = []
    for i, t in enumerate(texts):
        p = tmp_path / f"doc{i}.md"
        p.write_text(t, encoding="utf-8")
        paths.append(p)
    return paths


EMPTY_COVERAGE = {"tool_inputs": [{"items": []}]}


def run(paths: list[Path], script: list[Any], **kw: Any) -> tuple[dict[str, Any], list[str], FakeLLM]:
    llm = FakeLLM(script)
    spec, warns = extract_spec(paths, OPENAPI, llm, **kw)
    return spec, warns, llm


def test_scripted_spec_validates(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, "Plain brief text.")
    spec, warns, llm = run(paths, [tool(small_spec()), EMPTY_COVERAGE, {"tool_inputs": [{"mappings": []}]}])
    assert validate_spec(spec) == []
    assert spec["meta"]["sources"][0]["path"] == "doc0.md"
    assert llm.requests[0]["tools"][0]["name"] == "submit_spec"
    assert llm.requests[0]["tool_choice"] == {"type": "auto"}


def test_invalid_then_retry_carries_error_paths(tmp_path: Path) -> None:
    bad = small_spec()
    bad["bands"][0]["deadline"]["kind"] = "weeks"
    paths = write_briefs(tmp_path, "text")
    spec, _w, llm = run(paths, [tool(bad), tool(small_spec()), EMPTY_COVERAGE])
    assert validate_spec(spec) == []
    retry_user = llm.requests[1]["messages"][0]["content"]
    assert "bands[0].deadline.kind" in retry_user
    assert llm.requests[1]["purpose"] == "extract_repair_1"


def test_prose_wrapped_json_parses(tmp_path: Path) -> None:
    text = "Sure, here it is:\n```json\n" + json.dumps(small_spec()) + "\n```\nDone."
    assert parse_json(text)["schema_version"] == "1"
    assert parse_json('x {"a": "}", "b": [1]} y') == {"a": "}", "b": [1]}
    paths = write_briefs(tmp_path, "text")
    spec, _w, _l = run(paths, [{"text": text}, EMPTY_COVERAGE])
    assert validate_spec(spec) == []
    assert spec["bands"][0]["name"] == "x"


def test_forced_tool_rejected_falls_back_to_json_mode(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, "text")
    spec, _w, llm = run(paths, [LLMError("nope", 400), {"text": json.dumps(small_spec())}, EMPTY_COVERAGE])
    assert llm.requests[1]["tools"] is None
    assert "JSON Schema" in llm.requests[1]["system"]
    assert validate_spec(spec) == []


def test_coverage_pass_appends_missing_rules(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, 'Never say "the secret phrase here" to anyone.\n\nLimit is 17 percent.')
    cov = {"tool_inputs": [{"items": [
        {"text": 'Never say "the secret phrase here" to anyone.', "stage": "opening", "reason": "missing"},
        {"text": "odd", "stage": "bogus-stage", "reason": "x"},
    ]}]}
    trace = SimpleNamespace(warnings=[], warn=lambda m: None, log_model_call=lambda *a, **k: None)
    trace.warn = trace.warnings.append
    spec, warns, llm = run(paths, [tool(small_spec()), cov, {"tool_inputs": [{"mappings": []}]}], trace=trace)
    rules = spec["verbatim_rules"]
    assert any(r["stage"] == "opening" and r["source_ref"] == "coverage" for r in rules)
    assert any(r["stage"] == "global" and r["text"] == "odd" for r in rules)
    assert sum("coverage: added" in w for w in trace.warnings) == 2
    # the deterministic missing list is sent to the coverage call
    assert "17 percent" in llm.requests[1]["messages"][0]["content"]
    assert validate_spec(spec) == []


def test_coverage_dedupes_existing_rules(tmp_path: Path) -> None:
    s = small_spec()
    s["verbatim_rules"] = [{"stage": "global", "text": "Be polite always.", "source_ref": "r1"}]
    cov = {"tool_inputs": [{"items": [{"text": "be polite always", "stage": "global", "reason": "x"}]}]}
    spec, _w, _l = run(write_briefs(tmp_path, "Be polite always."), [tool(s), cov])
    assert len(spec["verbatim_rules"]) == 1


def test_grounding_repairs_mutated_enum(tmp_path: Path) -> None:
    s = small_spec()
    s["identity"] = {"questions": [{"field": "birth_date", "prompt": "p"}, {"field": "phone", "prompt": "q"}], "max_wrong_total": 3}
    s["closing"]["outcome_table"].append({"outcome": "no_deal", "when": "no promise"})
    def maps(req: dict[str, Any]) -> dict[str, Any]:
        items = json.loads(req["messages"][0]["content"].split("ITEMS:\n", 1)[1])
        want = {"birth_date": "dob", "no_deal": "none"}
        return {"tool_inputs": [{"mappings": [{"id": i["id"], "replacement": want.get(i["value"])} for i in items]}]}

    spec, warns, llm = run(write_briefs(tmp_path, "text"), [tool(s), EMPTY_COVERAGE, maps])
    assert [q["field"] for q in spec["identity"]["questions"]] == ["dob", "phone"]
    assert {r["outcome"] for r in spec["closing"]["outcome_table"]} == {"promise", "none"}
    assert any("birth_date" in w and "mapped" in w for w in warns)
    assert validate_spec(spec) == []


def test_grounding_unmatched_stays_with_warning(tmp_path: Path) -> None:
    s = small_spec()
    s["identity"] = {"questions": [{"field": "shoe_size", "prompt": "p"}], "max_wrong_total": 3}
    maps = {"tool_inputs": [{"mappings": [{"id": 0, "replacement": None}]}]}
    spec, warns, _l = run(write_briefs(tmp_path, "text"), [tool(s), EMPTY_COVERAGE, maps])
    assert spec["identity"]["questions"][0]["field"] == "shoe_size"
    assert any("shoe_size" in w and "unmatched" in w for w in warns)


def test_unknown_tool_name_warns(tmp_path: Path) -> None:
    s = small_spec()
    s["tools"] = {"get_account": {"purpose": "x"}}
    _spec, warns, _l = run(write_briefs(tmp_path, "text"), [tool(s), EMPTY_COVERAGE])
    assert any("get_account" in w and "no operation" in w for w in warns)


def test_deterministic_coverage_finds_absent_number() -> None:
    spec = small_spec()
    text = 'Pay within 3 days, up to 17 percent, on 2031-02-03 at 09:30, say "a long mandated sentence" and use `code_x`.'
    miss = {(m["kind"], m["token"]) for m in deterministic_coverage([text], spec)}
    kinds = {k for k, _ in miss}
    assert ("number", "17 percent") in miss
    assert ("date", "2031-02-03") in miss
    assert ("time", "09:30") in miss
    assert ("quote", "a long mandated sentence") in miss
    assert ("identifier", "code_x") in miss
    assert not any(t == "3 days" for _k, t in miss)  # 3 is in the spec (deadline n)
    assert "date" in kinds
    spec["verbatim_rules"] = [{"stage": "global", "text": "17 percent 2031-02-03 09:30 a long mandated sentence code_x", "source_ref": "r"}]
    assert deterministic_coverage([text], spec) == []


def test_budget_exhaustion_returns_valid_fallback(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, "Para one.\n\nPara two.\n\n| a | b |\n|---|---|")
    llm = FakeLLM([tool(small_spec())])
    spec, warns = extract_spec(paths, OPENAPI, llm, budget_s=0)
    assert validate_spec(spec) == []
    assert [r["text"] for r in spec["verbatim_rules"]][:2] == ["Para one.", "Para two."]
    assert all(r["stage"] == "global" for r in spec["verbatim_rules"])
    assert any("FELL BACK" in w for w in warns)
    assert {r["outcome"] for r in spec["closing"]["outcome_table"]} == {"promise", "none"}


def test_raising_llm_returns_fallback(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, "Only paragraph.")
    spec, warns, _l = run(paths, [LLMError("down", 503)])
    assert validate_spec(spec) == [] and spec["verbatim_rules"][0]["text"] == "Only paragraph."
    assert any("FELL BACK" in w for w in warns)


def test_no_llm_returns_fallback(tmp_path: Path) -> None:
    spec, warns = extract_spec(write_briefs(tmp_path, "x"), OPENAPI, None)
    assert validate_spec(spec) == [] and any("no LLM" in w for w in warns)


def test_validation_never_passes_falls_back(tmp_path: Path) -> None:
    bad = {"schema_version": "1"}
    spec, warns, llm = run(write_briefs(tmp_path, "text"), [tool(bad)] * 4)
    assert len(llm.requests) == 4  # first try + 3 repairs
    assert validate_spec(spec) == [] and any("FELL BACK" in w for w in warns)


def test_layered_ordering_reaches_prompt(tmp_path: Path) -> None:
    paths = write_briefs(tmp_path, "FIRST-DOC-TEXT", "SECOND-DOC-TEXT", "THIRD-DOC-TEXT")
    _s, _w, llm = run(paths, [tool(small_spec()), EMPTY_COVERAGE])
    user = llm.requests[0]["messages"][0]["content"]
    assert user.index("FIRST-DOC-TEXT") < user.index("SECOND-DOC-TEXT") < user.index("THIRD-DOC-TEXT")
    assert "DOCUMENT 3 of 3" in user
    assert "override" in llm.requests[0]["system"].lower()


def test_unreadable_brief_is_programmer_error(tmp_path: Path) -> None:
    with pytest.raises(ex.ExtractError, match="doesnotexist"):
        extract_spec([tmp_path / "doesnotexist.md"], OPENAPI, FakeLLM([]))


def test_prompt_files_exist_and_are_clean() -> None:
    from tests.test_contamination import BANNED_TOKENS

    files = sorted(ex.PROMPT_DIR.glob("*.md"))
    assert {f.stem for f in files} >= {"system", "sections", "coverage", "repair", "grounding"}
    for f in files:
        low = f.read_text(encoding="utf-8").lower()
        assert not [t for t in BANNED_TOKENS if t in low], f.name


def test_stage_extract_writes_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLM([tool(small_spec()), EMPTY_COVERAGE])
    monkeypatch.setattr(ex, "make_llm", lambda trace=None: llm)
    run_dir = tmp_path / "run"
    ctx = SimpleNamespace(
        briefs=[tmp_path / "b.md"], brief_texts=["Some brief."], openapi_doc=OPENAPI, run_dir=run_dir,
        trace=SimpleNamespace(warn=lambda m: None, log_model_call=lambda *a, **k: None), spec=None, results={},
    )
    detail = stage_extract(ctx)
    assert "spec extracted" in detail and validate_spec(ctx.spec) == []
    assert (run_dir / "spec.json").is_file() and (run_dir / "extraction_notes.md").is_file()
    assert "extraction" in ctx.results


def test_stage_extract_without_key_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "make_llm", lambda trace=None: None)
    ctx = SimpleNamespace(
        briefs=[tmp_path / "b.md"], brief_texts=["Some brief."], openapi_doc=OPENAPI, run_dir=tmp_path / "r",
        trace=SimpleNamespace(warn=lambda m: None), spec=None, results={},
    )
    assert "FALLBACK" in stage_extract(ctx) and validate_spec(ctx.spec) == []


def test_real_openapi_summary_lists_enums() -> None:
    doc = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
    assert "close_call" in ex.openapi_summary(doc)
    assert ex.openapi_enums(doc)["close_call"]["outcome"]
    assert copy.deepcopy(ex.minimal_spec([("a.md", "x")], doc)) and validate_spec(ex.minimal_spec([("a.md", "x")], doc)) == []


def test_grounding_does_not_borrow_enum_from_other_tool(tmp_path: Path) -> None:
    s = small_spec()
    s["closing"]["outcome_table"].append({"outcome": "none", "reason": "free_text_reason", "when": "x"})
    spec, warns, llm = run(write_briefs(tmp_path, "text"), [tool(s), EMPTY_COVERAGE])
    assert any(r.get("reason") == "free_text_reason" for r in spec["closing"]["outcome_table"])
    assert not any("free_text_reason" in w for w in warns)
