"""Gap guard (rules that exist only as prose) and the derived closing.aggregation.default."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from builder import engine, gap_guard
from builder.compiler import _Build, compile_workflow
from builder.spec import load_spec
from tests.conftest import REPO_ROOT, load_fixture

OPENAPI: dict[str, Any] = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))


def kinds(name: str) -> list[str]:
    return [g.kind for g in gap_guard.find_unstructured(load_fixture(name))]


@pytest.mark.parametrize("name", ["spec_brief_a.json", "spec_brief_ab.json"])
def test_clean_fixtures_have_no_gap(name: str) -> None:
    assert kinds(name) == []


@pytest.mark.parametrize("name", ["spec_variant_1.json", "spec_variant_2.json", "spec_variant_3.json"])
def test_variants_report_the_percent_floor(name: str) -> None:
    assert "amount_floor" in kinds(name)


def test_rehearsal_spec_reports_four_kinds_and_compiles_degraded() -> None:
    assert kinds("spec_rehearsal.json") == ["deadline_formula", "amount_floor", "callback_window", "persuasion_date"]
    _wf, report = compile_workflow(load_spec(load_fixture("spec_rehearsal.json")), OPENAPI)
    assert len(report.degraded) >= 4
    assert any(g.startswith("unstructured rule (deadline_formula)") for g in report.gaps)
    assert report.to_dict()["degraded"] == report.degraded


def test_gap_kind_tag_and_structure_present() -> None:
    spec = copy.deepcopy(load_fixture("spec_brief_ab.json"))
    spec["verbatim_rules"].append({"stage": "negotiate", "text": "Some callback rule.", "gap_kind": "callback_window"})
    assert [g.kind for g in gap_guard.find_unstructured(spec)] == ["callback_window"]
    spec["third_party"]["callback_window"]["per_weekday"] = [
        {"weekdays": ["monday"], "hour_start": "09:00", "hour_end": "10:00"}]
    assert gap_guard.find_unstructured(spec) == []


def test_warning_without_admission_cue_is_ignored() -> None:
    spec = copy.deepcopy(load_fixture("spec_brief_ab.json"))
    spec["warnings"] = ["The floor is 30% of the amount."]
    assert gap_guard.find_unstructured(spec) == []


def build_obj(spec: dict) -> _Build:
    b = _Build(load_spec(spec), OPENAPI)
    b.build()
    return b


def test_aggregation_default_is_derived_from_the_spec() -> None:
    b = build_obj(load_fixture("spec_rehearsal.json"))
    agg = b.final_cfg()["aggregation"]
    assert agg["default"] == {"outcome": "no_promise", "reason": "refused"}
    assert any("closing.aggregation.default derived" in w for w in b.report.warnings)
    out = engine.aggregate_outcome([{"product_id": "x", "promise_recorded": False, "note_reason": None}],
                                   {"closing": {"aggregation": agg}})
    assert (out["outcome"], out["reason"]) == ("no_promise", "refused")


def test_aggregation_default_from_table_when_no_other_source() -> None:
    spec = copy.deepcopy(load_fixture("spec_rehearsal.json"))
    spec["persuasion"].pop("on_exhausted")
    spec["promise_rules"].pop("on_failure")
    spec["multi_product"]["notes"]["reasons"] = ["hardship", "paid"]
    b = build_obj(spec)
    assert b.final_cfg()["aggregation"]["default"] == {"outcome": "no_promise", "reason": "hardship"}


def test_aggregation_default_impossible_is_a_gap() -> None:
    spec = copy.deepcopy(load_fixture("spec_rehearsal.json"))
    spec["persuasion"].pop("on_exhausted")
    spec["promise_rules"].pop("on_failure")
    spec["closing"]["aggregation"].pop("first_note_not_equal")
    b = build_obj(spec)
    assert "closing.aggregation.default: no safe default derivable" in b.report.gaps
    assert "closing.aggregation.default: no safe default derivable" in b.report.degraded


def test_explicit_default_wins() -> None:
    spec = copy.deepcopy(load_fixture("spec_rehearsal.json"))
    spec["closing"]["aggregation"]["default"] = {"outcome": "no_promise", "reason": "hardship"}
    b = build_obj(spec)
    assert b.final_cfg()["fallback_post"] == {"outcome": "no_promise", "reason": "hardship"}
    assert not any("derived" in w for w in b.report.warnings)
