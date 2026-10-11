from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from builder import engine, online_cases
from builder.online_cases import generate_cases, load_carriers, wrong_answer
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture

SPECS = ["spec_brief_a.json", "spec_brief_ab.json", "spec_variant_1.json", "spec_variant_2.json", "spec_variant_3.json"]
CASES_FILE = REPO_ROOT / "public-test-set.json"


@pytest.fixture(scope="module")
def carriers() -> list[online_cases.Carrier]:
    return load_carriers(CASES_FILE)


def _table(spec: dict) -> set[tuple[str, object]]:
    rows = {(r["outcome"], r.get("reason") or None) for r in spec["closing"]["outcome_table"]}
    agg = spec["closing"].get("aggregation") or {}
    default = agg.get("default") or {}
    if default.get("outcome"):
        rows.add((default["outcome"], default.get("reason") or None))
    return rows


def test_load_carriers_is_tolerant(tmp_path: Path) -> None:
    assert load_carriers(tmp_path / "missing.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert load_carriers(bad) == []
    bad.write_text('{"a": 1}', encoding="utf-8")
    assert load_carriers(bad) == []
    bad.write_text(json.dumps([1, {"id": "x"}, {"id": "y", "variables": {}, "identity": {}, "products": []},
                               {"id": "z", "variables": {}, "identity": {}, "products": [{"a": 1}], "expected": {"promises": [{"product_id": "q1"}]}}]),
                   encoding="utf-8")
    got = load_carriers(bad)
    assert [c.case_id for c in got] == ["z"] and got[0].product_ids == ["q1"]


def test_carriers_get_bank_product_ids(carriers: list[online_cases.Carrier]) -> None:
    assert len(carriers) == 13
    three = [c for c in carriers if len(c.products) == 3]
    assert three and len(three[0].product_ids) == 3 and len(set(three[0].product_ids)) == 3


@pytest.mark.parametrize("name", SPECS)
def test_expectations_follow_the_spec_and_engine(name: str, carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture(name)
    cal = engine.make_calendar(spec)
    cases, _skipped = generate_cases(spec, carriers)
    assert cases and len({c.id for c in cases}) == len(cases)
    table = _table(spec)
    by_carrier = {c.case_id: c for c in carriers}
    for c in cases:
        exp = c.expected
        assert (exp["outcome"], exp["reason"]) in table, c.id
        assert set(exp) == {"outcome", "reason", "promises", "notes", "callbacks", "transfers"}
        today = engine.parse_now(c.variables[spec["call_variables"]["now"]]).date()
        for row in exp["promises"]:
            p = c.products[c.product_ids.index(row["product_id"])]
            band = engine.band_for(p[spec["account_fields"]["days_late"]], spec)
            deadline = engine.deadline_for(band, today, cal)
            d = engine.parse_date(row["date"])
            assert engine.is_business_day(d, cal) and today <= d <= deadline, c.id
            assert row["amount"] in (engine.full_for(p, spec), engine.floor_for(p, spec)), c.id
        for cb in exp["callbacks"]:
            now = c.variables[spec["call_variables"]["now"]]
            assert engine.check_callback(f"{cb['date']}T{cb['time']}", now, spec, cal)["status"] == "ok"
        assert c.products == by_carrier[c.carrier_id].products


@pytest.mark.parametrize("name", SPECS)
def test_wrong_identity_answers_differ_and_match_the_limit(name: str, carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture(name)
    case = next((c for c in generate_cases(spec, carriers)[0] if c.kind == "identity_lockout"), None)
    assert case is not None
    assert len(case.wrong_answers) == spec["identity"]["max_wrong_total"]
    for field_name, wrong in case.wrong_answers:
        assert wrong != case.identity[field_name]
    on_failure = spec["identity"]["on_failure"]["outcome"]
    assert (case.expected["outcome"], case.expected["reason"]) == (on_failure["outcome"], on_failure.get("reason") or None)
    assert not case.expected["promises"]


def test_wrong_answer_shapes() -> None:
    assert wrong_answer("1988-03-14", "date") == "1987-03-14"
    assert wrong_answer("4471", "digits") == "5582"
    assert wrong_answer("AB1 2CD", "text") == "AB1 2CDx"


def test_cross_check_against_the_organisers_cases(carriers: list[online_cases.Carrier]) -> None:
    public = {c["id"]: c for c in json.loads(CASES_FILE.read_text(encoding="utf-8"))}
    for name in ("spec_brief_a.json", "spec_brief_ab.json"):
        cases = {c.kind: c for c in generate_cases(load_fixture(name), carriers)[0]}
        # these pairs describe the same scenario as the public case; the others (clamping, callback,
        # one date for all) use the customer's own date choice, so the public expectation differs on purpose
        assert cases["promise_full"].expected == public["PUB-01"]["expected"]
        assert cases["identity_lockout"].expected == public["PUB-03"]["expected"]
        assert cases["intent_payment_plan_request"].expected["outcome"] == public["PUB-09"]["expected"]["outcome"]
        assert cases["intent_payment_plan_request"].expected["transfers"] == public["PUB-09"]["expected"]["transfers"]
        assert cases["third_party_callback"].expected["reason"] == public["PUB-02"]["expected"]["reason"]


def test_multi_product_only_when_the_spec_has_it(carriers: list[online_cases.Carrier]) -> None:
    kinds_a, why_a = generate_cases(load_fixture("spec_brief_a.json"), carriers)
    kinds_ab, _ = generate_cases(load_fixture("spec_brief_ab.json"), carriers)
    assert "multi_one_date" not in {c.kind for c in kinds_a} and any("multi-product" in w for w in why_a)
    multi = next(c for c in kinds_ab if c.kind == "multi_one_date")
    assert len(multi.expected["promises"]) >= 2
    assert all(len(c.products) <= 1 for c in kinds_a)


def test_skipped_kinds_carry_a_reason(carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture("spec_brief_a.json")
    spec["third_party"].pop("callback_window")
    spec["amounts"].pop("below_floor")
    spec["amounts"].pop("too_much")
    spec["promise_rules"]["on_after_deadline"] = "reject"
    cases, why = generate_cases(spec, carriers)
    kinds = {c.kind for c in cases}
    assert not kinds & {"third_party_callback", "amount_floor", "date_over_limit"}
    assert any("callback" in w for w in why) and any("amounts" in w for w in why) and any("clamping" in w for w in why)


def test_tool_failure_needs_a_fault_within_the_retry_limit(carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture("spec_brief_a.json")
    case = next(c for c in generate_cases(spec, carriers)[0] if c.kind == "tool_failure")
    assert case.faults and case.faults[0]["tool"] in spec["tools"]
    spec["promise_rules"]["retries_on_failure"] = 0
    assert not any(c.kind == "tool_failure" for c in generate_cases(spec, carriers)[0])


def test_notes_expected_for_multi_product_intents(carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture("spec_brief_ab.json")
    hardship = next(c for c in generate_cases(spec, carriers)[0] if c.kind == "intent_hardship")
    assert hardship.expected["notes"] and hardship.expected["notes"][0]["reason"] == "hardship"
    assert (hardship.expected["outcome"], hardship.expected["reason"]) in _table(spec)


def test_generation_is_deterministic_and_limited(carriers: list[online_cases.Carrier]) -> None:
    spec = load_fixture("spec_brief_ab.json")
    a, b = generate_cases(spec, carriers), generate_cases(copy.deepcopy(spec), carriers)
    assert a == b
    assert [c.kind for c in a[0]][:3] == ["identity_lockout", "promise_full", "date_over_limit"]
    limited, _ = generate_cases(spec, carriers, limit=3)
    assert [c.id for c in limited] == [c.id for c in a[0]][:3]
    assert len(a[0]) <= online_cases.MAX_CASES


def test_no_carriers_means_no_cases() -> None:
    cases, why = generate_cases(load_fixture("spec_brief_a.json"), [])
    assert cases == [] and why


def test_module_is_offline_and_standalone() -> None:
    src = (REPO_ROOT / "builder" / "online_cases.py").read_text(encoding="utf-8")
    for banned in ("builder.pipeline", "import urllib", "import socket", "import requests", "freya"):
        assert banned not in src
