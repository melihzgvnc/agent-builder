"""builder.selfcheck: spec-derived offline scenario checks over the compiled workflow."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from builder import compiler, lint, selfcheck
from builder.compiler import CompileReport, compile_workflow
from builder.spec import load_spec, spec_to_dict
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture

OPENAPI: dict[str, Any] = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
SPEC_FILES: dict[str, str] = {"A": "spec_brief_a.json", "AB": "spec_brief_ab.json"}


def build(key: str, spec_d: dict | None = None) -> tuple[dict, dict, CompileReport]:
    """(workflow, spec dict, report) for a fixture spec or an edited copy of one."""
    data = copy.deepcopy(spec_d if spec_d is not None else load_fixture(SPEC_FILES[key]))
    data.pop("coverage", None)
    spec = load_spec(data)
    wf, report = compile_workflow(spec, copy.deepcopy(OPENAPI))
    return wf, spec_to_dict(spec), report


def run(wf: dict, spec_d: dict, report: CompileReport) -> selfcheck.SelfCheckResult:
    return selfcheck.run_selfchecks(wf, spec_d, copy.deepcopy(OPENAPI), report.context)


def tool_of(wf: dict, name: str) -> dict:
    for _path, tool, _where in lint.iter_tools(wf):
        if tool.get("function", {}).get("name") == name:
            return tool
    raise AssertionError(f"no tool {name}")


def mutate(source: str, old: str, new: str) -> str:
    assert old in source, f"mutation target {old!r} not in the source"
    return source.replace(old, new, 1)


def mutate_code_node(wf: dict, node: str, old: str, new: str) -> None:
    default = wf["nodes"][node]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]
    default["default"] = mutate(default["default"], old, new)


def names(result: selfcheck.SelfCheckResult, prefix: str) -> list[str]:
    return [c.name for c in result.checks if c.name.startswith(prefix)]


# ---------------------------------------------------------------------------------------------
# the fixture specs pass
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["A", "AB"])
def test_fixture_specs_pass_every_scenario(key: str) -> None:
    wf, spec_d, report = build(key)
    result = run(wf, spec_d, report)
    assert [c.format() for c in result.failed] == []
    assert result.ran > 20 and result.ran == len(result.checks)
    # every closing.outcome_table row is reachable by compiled code (no warning at all)
    assert [c.format() for c in result.warnings] == []
    rows = {(r["outcome"], r.get("reason") or "") for r in spec_d["closing"]["outcome_table"]}
    reach = {c.name for c in result.checks if c.name.startswith("outcomes.reach[")}
    assert len(reach) == len(rows)
    for prefix in ("structure.", "facts.band[", "facts.calendar[", "promise.date[", "amount[", "callback[",
                   "outcomes.node[", "outcomes.aggregate[", "layout.", "no_none."):
        assert names(result, prefix), prefix


def test_notes_scenarios_only_with_the_note_tool() -> None:
    a = run(*build("A"))
    ab = run(*build("AB"))
    assert not names(a, "notes.") and any("notes" in s for s in a.skipped)
    assert len(names(ab, "notes.")) >= 3
    assert any(n.startswith("outcomes.aggregate[all_notes[") for n in names(ab, "outcomes.aggregate["))


# ---------------------------------------------------------------------------------------------
# mutations: a deliberately broken hook is found by the matching scenario
# ---------------------------------------------------------------------------------------------


def test_broken_callback_hour_is_caught() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_CALLBACK)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "mins < hs", "mins < hs - 120")
    result = run(wf, spec_d, report)
    assert result.failed_features() == {"callback"}
    assert any("before_start" in c.name for c in result.failed)


def test_broken_callback_inclusive_edge_is_caught() -> None:
    wf, spec_d, report = build("A")
    assert spec_d["third_party"]["callback_window"]["inclusive"] is True
    tool = tool_of(wf, compiler.ROLE_CALLBACK)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "late = mins > he or", "late = mins >= he or")
    result = run(wf, spec_d, report)
    assert any("end_edge" in c.name for c in result.failed)
    assert result.failed_features() == {"callback"}


def test_broken_amount_floor_is_caught() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_PROMISE)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "ac < int(round(floor * 100))", "ac < 0")
    result = run(wf, spec_d, report)
    assert result.failed_features() == {"amount"}
    assert all("below_floor" in c.name for c in result.failed)


def test_broken_promise_date_is_caught() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_PROMISE)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "asked_date not in bds", "asked_date in bds")
    result = run(wf, spec_d, report)
    assert "promise" in result.failed_features()
    assert any("promise.date[" in c.name for c in result.failed)


def test_broken_facts_node_is_caught_without_blaming_other_features() -> None:
    wf, spec_d, report = build("A")
    mutate_code_node(wf, compiler.NODE_FACTS, "compute_facts", "compute_factz")
    result = run(wf, spec_d, report)
    assert result.failed_features() == {"facts"}
    assert result.skipped  # the dependent scenarios were skipped, not failed


def test_broken_finalize_is_caught() -> None:
    wf, spec_d, report = build("AB")
    mutate_code_node(wf, compiler.NODE_FINALIZE, "vars['final_outcome'] = pair[0]", "vars['final_outcome'] = 'zz'")
    result = run(wf, spec_d, report)
    assert "outcomes" in result.failed_features()


def test_broken_note_hook_is_caught() -> None:
    wf, spec_d, report = build("AB")
    tool = tool_of(wf, compiler.ROLE_NOTE)
    tool["post_process_code"] = mutate(tool["post_process_code"], "vars[_key(k, 'note')] = reason", "pass")
    result = run(wf, spec_d, report)
    assert "notes" in result.failed_features()


def test_none_written_by_a_hook_is_caught() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_PROMISE)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "vars['promise_changed'] = 'no'", "vars['promise_changed'] = None")
    result = run(wf, spec_d, report)
    assert "no_none" in result.failed_features()


def test_import_in_a_hook_is_a_structure_failure() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_PROMISE)
    tool["post_process_code"] = "import os\n" + tool["post_process_code"]
    result = run(wf, spec_d, report)
    assert "structure" in result.failed_features()


def test_layout_difference_is_caught() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_PROMISE)
    # a hook that only works when `vars` is a global
    tool["pre_process_code"] = (
        "def _g():\n    return vars.get('today')\n"
        "config['body']['zz_marker'] = _g()\n" + tool["pre_process_code"]
    )
    result = run(wf, spec_d, report)
    assert "layout" in result.failed_features()


def test_a_dropped_outcome_row_path_is_a_warning() -> None:
    wf, spec_d, report = build("A")
    spec_d = copy.deepcopy(spec_d)
    spec_d["closing"]["outcome_table"].append({"outcome": "x_model_only", "reason": "y", "when": "never by code"})
    result = run(wf, spec_d, report)
    assert [c.name for c in result.warnings] == ["outcomes.reach[x_model_only/y]"]
    assert result.failed == []


# ---------------------------------------------------------------------------------------------
# skipped features and repairs
# ---------------------------------------------------------------------------------------------


def test_callback_checks_are_skipped_without_a_callback_window() -> None:
    spec_d = load_fixture(SPEC_FILES["A"])
    spec_d["third_party"].pop("callback_window")
    wf, sd, report = build("A", spec_d)
    result = run(wf, sd, report)
    assert result.failed == []
    assert not names(result, "callback[")
    assert any(s.startswith("callback") for s in result.skipped)


def test_repair_spec_drops_the_callback_window_and_recompiles_clean() -> None:
    wf, spec_d, report = build("A")
    tool = tool_of(wf, compiler.ROLE_CALLBACK)
    tool["pre_process_code"] = mutate(tool["pre_process_code"], "mins < hs", "mins < hs - 120")
    result = run(wf, spec_d, report)
    repaired = selfcheck.repair_spec(spec_d, result)
    assert repaired is not None
    fixed, names_ = repaired
    assert names_ == ["drop_callback_window"]
    assert "callback_window" not in fixed["third_party"] and "callback_window" in spec_d["third_party"]
    wf2, sd2, report2 = build("A", fixed)
    assert not [f for f in lint.lint(wf2) if f.severity == "error"]
    again = run(wf2, sd2, report2)
    assert again.failed == [] and not names(again, "callback[")


def test_repair_spec_disables_notes_and_recompiles_clean() -> None:
    wf, spec_d, report = build("AB")
    tool = tool_of(wf, compiler.ROLE_NOTE)
    tool["post_process_code"] = mutate(tool["post_process_code"], "vars[_key(k, 'note')] = reason", "pass")
    result = run(wf, spec_d, report)
    fixed, names_ = selfcheck.repair_spec(spec_d, result) or ({}, [])
    assert names_ == ["disable_notes"]
    assert fixed["multi_product"]["notes"]["for_products_without_promise"] is False
    assert spec_d["multi_product"]["notes"]["for_products_without_promise"] is True
    wf2, sd2, report2 = build("AB", fixed)
    assert not report2.context["has"][compiler.ROLE_NOTE]
    again = run(wf2, sd2, report2)
    assert again.failed == []


def test_repair_spec_returns_none_for_other_failures() -> None:
    wf, spec_d, report = build("A")
    mutate_code_node(wf, compiler.NODE_FACTS, "compute_facts", "compute_factz")
    assert selfcheck.repair_spec(spec_d, run(wf, spec_d, report)) is None
    wf, spec_d, report = build("A")
    assert selfcheck.repair_spec(spec_d, run(wf, spec_d, report)) is None


def test_repairs_table_is_exactly_the_two_entries() -> None:
    assert [r.name for r in selfcheck.REPAIRS] == ["drop_callback_window", "disable_notes"]


def test_report_context_is_not_in_to_dict() -> None:
    _wf, _sd, report = build("AB")
    assert set(report.to_dict()) == {"warnings", "gaps", "to_verify", "written_vars", "lint", "degraded"}
    assert set(report.context) == {"fields", "facts_io", "has", "n_products", "close_name", "resp"}


def test_variant_spec_is_exercised_read_only_when_valid() -> None:
    path = FIXTURES_DIR / "spec_variant_1.json"
    if not path.exists():
        pytest.skip("no spec_variant_1.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        wf, sd, report = build("", data)
    except Exception as e:  # noqa: BLE001 - another workflow owns this file; it may be unfinished
        pytest.skip(f"spec_variant_1.json does not compile yet: {type(e).__name__}")
    result = run(wf, sd, report)
    assert result.ran > 0
    assert not [c for c in result.failed if c.feature == "structure"]


def test_opening_order_and_closing_checks() -> None:
    wf, spec_d, report = build("A")
    result = run(wf, spec_d, report)
    assert {"opening.order", "closing.auto_close", "closing.end_nodes_speak", "closing.wrapup_cap"} <= set(c.name for c in result.checks)
    assert not [c for c in result.checks if not c.ok]
    # reordered first message, missing auto_close transition, silent end node
    wf2, spec_d, report = build("A")
    tts = wf2["nodes"]["identity"]["on_enter"]
    tts["text"] = " ".join(reversed(tts["text"].split(". ")))
    wf2["nodes"]["negotiate"]["transitions"] = [
        t for t in wf2["nodes"]["negotiate"]["transitions"] if "auto_close" not in json.dumps(t.get("condition"))
    ]
    wf2["nodes"]["end_final"]["terminal"]["function"]["parameters"]["properties"].pop("spoken_message")
    failed = {c.name for c in run(wf2, spec_d, report).checks if not c.ok}
    assert {"opening.order", "closing.auto_close", "closing.end_nodes_speak"} <= failed
