"""Field-map derivation in the compiler and the compile-feedback repair loop (FakeLLM, no network)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder import extract as ex
from builder.compiler import CompileError, compile_workflow
from builder.extract import extract_spec
from builder.llm import FakeLLM, LLMError, LLMResult
from builder.spec import load_spec, validate_spec
from tests.conftest import REPO_ROOT, load_fixture

OPENAPI_PATH = REPO_ROOT / "tools" / "openapi.json"


def real_openapi() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def spec_with(verify_map: dict[str, str] | None, base: str = "spec_variant_1.json") -> dict[str, Any]:
    spec = load_fixture(base)
    spec.setdefault("tools", {}).setdefault("verify_identity", {})
    if verify_map is None:
        spec["tools"].pop("verify_identity")
    else:
        spec["tools"]["verify_identity"]["field_map"] = verify_map
    return spec


def compile_spec(spec: dict[str, Any], openapi: dict[str, Any] | None = None):
    return compile_workflow(load_spec(spec), openapi or real_openapi())


def derived(report: Any) -> list[str]:
    return [w for w in report.warnings if "derived field_map" in w]


# ---- compiler: field_map derivation ---------------------------------------------------------


def test_empty_field_map_is_derived_and_logged() -> None:
    _wf, report = compile_spec(spec_with({}))
    lines = [w for w in derived(report) if w.startswith("verify_identity")]
    assert any("'question' -> 'field'" in w for w in lines)


def test_no_tool_entry_at_all_is_derived() -> None:
    _wf, report = compile_spec(spec_with(None))
    assert any("verify_identity" in w and "'question' -> 'field'" in w for w in derived(report))


def test_alias_keys_are_accepted() -> None:
    _wf, report = compile_spec(spec_with({"field": "field", "answer": "answer"}))
    assert any("'question' -> 'field'" in w for w in derived(report))


def test_partial_map_keeps_explicit_entries() -> None:
    _wf, report = compile_spec(spec_with({"customer": "customer_id", "answer": "answer"}))
    lines = derived(report)
    assert any("verify_identity" in w and "'question' -> 'field'" in w for w in lines)
    assert not any("verify_identity" in w and "'answer'" in w for w in lines)


def test_explicit_entry_wins_over_cues() -> None:
    oa = real_openapi()
    props = oa["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    props["challenge"] = {"type": "string", "enum": ["a", "b"]}
    _wf, report = compile_spec(spec_with({"question": "challenge"}), oa)
    assert not any("verify_identity" in w and "'question'" in w for w in derived(report))


def test_ambiguous_raises_with_candidates() -> None:
    oa = real_openapi()
    props = oa["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    del props["field"]
    props["alpha"] = {"type": "string"}
    props["beta"] = {"type": "string"}
    with pytest.raises(CompileError) as e:
        compile_spec(spec_with({}), oa)
    msg = str(e.value)
    assert "cannot derive" in msg and "alpha" in msg and "beta" in msg and "question" in msg


def test_wrong_explicit_value_is_warned_and_derived() -> None:
    _wf, report = compile_spec(spec_with({"question": "nope"}))
    assert any("'nope'" in w and "does not have" in w for w in report.warnings)
    assert any("verify_identity" in w and "'question' -> 'field'" in w for w in derived(report))


def test_enum_taken_from_openapi_when_spec_omits_it() -> None:
    spec = spec_with({})
    spec["tools"]["verify_identity"].pop("allowed_values", None)
    wf, report = compile_spec(spec)
    assert any("allowed_values for 'field' taken from the openapi enum" in w for w in report.warnings)
    assert "date_of_birth" in json.dumps(wf)


def test_extra_allowed_values_warned() -> None:
    spec = spec_with({})
    spec["tools"]["verify_identity"]["allowed_values"] = {"field": ["date_of_birth", "shoe_size"]}
    _wf, report = compile_spec(spec)
    assert any("openapi enum lacks" in w for w in report.warnings)


# ---- extraction: _fill_tool_fields and compile-feedback loop --------------------------------


def test_fill_tool_fields_drops_non_api_values() -> None:
    spec = {"tools": {"verify_identity": {"field_map": {"question": "field", "answer": "nope"},
                                          "allowed_values": {"zzz": ["x"], "field": ["date_of_birth"]}}}}
    warns: list[str] = []
    ex._fill_tool_fields(spec, real_openapi(), warns)
    assert spec["tools"]["verify_identity"]["field_map"] == {"question": "field"}
    assert list(spec["tools"]["verify_identity"]["allowed_values"]) == ["field"]
    assert len(warns) == 2


def broken_spec() -> dict[str, Any]:
    spec = load_fixture("spec_variant_1.json")
    spec.pop("identity")
    assert validate_spec(spec) == []
    return spec


def brief(tmp_path: Path) -> list[Path]:
    p = tmp_path / "doc.md"
    p.write_text("Plain brief text.", encoding="utf-8")
    return [p]


def dispatcher(first: dict[str, Any], repairs: list[Any], log: list[str]):
    """Script item per call, keyed on purpose so the order of coverage/grounding calls does not matter."""
    queue = list(repairs)

    def item(request: dict[str, Any]) -> Any:
        purpose = request["purpose"]
        log.append(purpose)
        if purpose == "extract_spec":
            return {"tool_inputs": [first]}
        if purpose.startswith("extract_compile_repair"):
            if not queue:
                raise AssertionError("unexpected extra compile repair call")
            nxt = queue.pop(0)
            return nxt
        if purpose == "extract_coverage":
            return {"tool_inputs": [{"items": []}]}
        return {"tool_inputs": [{"mappings": []}]}

    return item


def run_loop(tmp_path: Path, first: dict[str, Any], repairs: list[Any], **kw: Any):
    log: list[str] = []
    llm = FakeLLM([dispatcher(first, repairs, log) for _ in range(12)])
    report: dict[str, Any] = {}
    spec, warns = extract_spec(brief(tmp_path), real_openapi(), llm, report=report, **kw)
    return spec, warns, llm, report, log


def test_compile_repair_fixes_spec(tmp_path: Path) -> None:
    good_identity = load_fixture("spec_variant_1.json")["identity"]
    spec, warns, llm, report, log = run_loop(
        tmp_path, broken_spec(), [{"tool_inputs": [{"sections": {"identity": good_identity}}]}]
    )
    reqs = [r for r in llm.requests if r["purpose"] == "extract_compile_repair_1"]
    assert len(reqs) == 1
    user = reqs[0]["messages"][0]["content"]
    assert "no identity section" in user and "verify_identity" in user
    assert report["compile_repairs"] == 1 and "compile_error" not in report
    compile_workflow(load_spec(spec), real_openapi())
    assert any("compile-feedback repair 1" in w for w in warns)


def test_compile_repair_invalid_patch_discarded_and_capped(tmp_path: Path) -> None:
    bad_patch = {"tool_inputs": [{"sections": {"identity": {"bogus": 1}}}]}
    spec, _w, _llm, report, log = run_loop(tmp_path, broken_spec(), [bad_patch, bad_patch])
    assert report["compile_repairs"] == 2
    assert "identity" not in spec
    assert "no identity section" in report["compile_error"]
    assert log.count("extract_compile_repair_1") + log.count("extract_compile_repair_2") == 2


def test_compile_repair_llm_error_keeps_spec(tmp_path: Path) -> None:
    spec, warns, _llm, report, _log = run_loop(tmp_path, broken_spec(), [LLMError("boom"), LLMError("boom")])
    assert "identity" not in spec
    assert any("compile-feedback repair 1 failed" in w for w in warns)
    assert report["compile_error"]


def test_no_repair_call_when_spec_compiles(tmp_path: Path) -> None:
    _spec, _w, _llm, report, log = run_loop(tmp_path, load_fixture("spec_variant_1.json"), [])
    assert not [p for p in log if p.startswith("extract_compile_repair")]
    assert report["compile_repairs"] == 0


def test_no_repair_call_when_budget_is_low(tmp_path: Path) -> None:
    _spec, _w, _llm, report, log = run_loop(tmp_path, broken_spec(), [], budget_s=25)
    assert not [p for p in log if p.startswith("extract_compile_repair")]
    assert report["compile_repairs"] == 0


def test_truncated_reply_retries_with_compact_hint(tmp_path: Path) -> None:
    cut = LLMResult(text='{"schema_version": "1", "meta": ', stop_reason="max_tokens")
    llm = FakeLLM([cut, {"tool_inputs": [load_fixture("spec_variant_1.json")]}] + [{"tool_inputs": [{"items": []}]}] * 4)
    extract_spec(brief(tmp_path), real_openapi(), llm)
    assert "cut off" in llm.requests[1]["messages"][0]["content"]
    assert "cut off" not in llm.requests[0]["messages"][0]["content"]
