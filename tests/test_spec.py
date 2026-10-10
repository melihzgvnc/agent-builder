from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import jsonschema
import pytest

from builder.spec import (
    SpecError,
    load_schema,
    load_spec,
    load_spec_file,
    resolve_path,
    spec_to_dict,
    validate_spec,
)
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture

FIXTURES = ("spec_brief_a.json", "spec_brief_ab.json")
A_REFS = {f"A.{i}" for i in range(1, 48)}
B_REFS = {f"B.{i}" for i in range(1, 12)} | {"B.closing"}


def _contains_none(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_none(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_none(v) for v in value)
    return False


def test_schema_is_valid_draft_2020_12() -> None:
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name", FIXTURES)
def test_fixtures_validate(name: str) -> None:
    assert validate_spec(load_fixture(name)) == []
    assert load_spec_file(FIXTURES_DIR / name).schema_version == "1"


@pytest.mark.parametrize(("name", "required"), [
    ("spec_brief_a.json", A_REFS | {"A.sec2", "A.sec7"}),
    ("spec_brief_ab.json", A_REFS | B_REFS | {"A.sec2", "A.sec7"}),
])
def test_coverage_complete(name: str, required: set[str]) -> None:
    data = load_fixture(name)
    refs = {c["source_ref"] for c in data["coverage"]}
    assert required <= refs, sorted(required - refs)
    for entry in data["coverage"]:
        for path in entry["paths"]:
            resolve_path(data, path)


@pytest.mark.parametrize("name", FIXTURES)
def test_round_trip(name: str) -> None:
    data = load_fixture(name)
    spec = load_spec(data)
    dumped = spec_to_dict(spec)
    assert not _contains_none(dumped)
    assert validate_spec(dumped) == []
    assert load_spec(dumped) == spec


def test_ab_layers_on_a() -> None:
    a = load_spec(load_fixture("spec_brief_a.json"))
    ab = load_spec(load_fixture("spec_brief_ab.json"))
    assert [s.order for s in ab.meta.sources] == [1, 2]
    assert [s.role for s in ab.meta.sources] == ["base", "layer"]
    assert a.multi_product.enabled is False and ab.multi_product.enabled is True
    assert ab.multi_product.max_products == 3
    assert ab.closing.aggregation is not None
    assert ab.closing.aggregation.precedence == [
        "transfer", "any_promise", "all_notes_equal", "first_note_not_equal"
    ]
    assert not any("add_note" in r.text for r in ab.verbatim_rules)
    assert any("never uses add_note" in r.text for r in a.verbatim_rules)


def _set(path: list[Any], value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        target: Any = d
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(path: list[Any]) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        target: Any = d
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _close_with_unknown_outcome(d: dict[str, Any]) -> None:
    d["intents"][2]["actions"][2] = {"type": "close", "outcome": "nowhere"}


MALFORMED: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("missing deadline", _delete(["bands", 1, "deadline"]), "bands[1].deadline: required"),
    ("missing deadline n", _delete(["bands", 1, "deadline", "n"]), "bands[1].deadline.n: required"),
    ("bad weekday", _set(["calendar", "weekend_days"], ["funday"]),
     "calendar.weekend_days[0]: must be one of [monday, tuesday, wednesday, thursday, friday, saturday, sunday]"),
    ("string band max", _set(["bands", 0, "days_late_max"], "7"), "bands[0].days_late_max: expected integer"),
    ("extra key", _set(["bands", 0, "colour"], "red"), "bands[0].colour: unexpected property"),
    ("transfer without queue", _set(["intents", 0, "actions", 0], {"type": "transfer"}),
     "intents[0].actions[0].queue: required"),
    ("overlapping bands", _set(["bands", 1, "days_late_min"], 7), "bands[1]: overlaps bands[0]"),
    ("max below min", _set(["bands", 1, "days_late_max"], 5), "bands[1].days_late_max: below days_late_min"),
    ("unknown attempt band", _set(["persuasion", "attempts", 1, "bands"], ["nope"]),
     "persuasion.attempts[1].bands[0]: unknown band 'nope'"),
    ("duplicate attempt index", _set(["persuasion", "attempts", 1, "index"], 1),
     "persuasion.attempts[1].index: duplicate index 1 (also persuasion.attempts[0])"),
    ("callback end before start", _set(["third_party", "callback_window", "hour_end"], "09:00"),
     "third_party.callback_window.hour_end: before hour_start"),
    ("unknown close outcome", _close_with_unknown_outcome,
     "intents[2].actions[2]: outcome 'nowhere' not in closing.outcome_table"),
    ("unknown close reason", _set(["persuasion", "on_exhausted"], {"outcome": "no_promise", "reason": "bored"}),
     "persuasion.on_exhausted: outcome 'no_promise'/'bored' not in closing.outcome_table"),
    ("unresolved coverage path", _set(["coverage", 0, "paths"], ["no_such.path"]),
     "coverage[0].paths[0]: does not resolve"),
    ("missing meta", _delete(["meta"]), "$.meta: required"),
    ("null value", _set(["meta", "currency"], None), "meta.currency: expected string"),
    ("bad schema version", _set(["schema_version"], "2"), "schema_version: '1' was expected"),
]


@pytest.mark.parametrize(("label", "mutate", "expected"), MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_specs(label: str, mutate: Callable[[dict[str, Any]], None], expected: str) -> None:
    data = copy.deepcopy(load_fixture("spec_brief_a.json"))
    mutate(data)
    errors = validate_spec(data)
    assert expected in errors, errors
    with pytest.raises(SpecError) as info:
        load_spec(data)
    assert expected in info.value.errors


def test_bad_holiday_date() -> None:
    data = copy.deepcopy(load_fixture("spec_brief_a.json"))
    data["calendar"]["holidays"][0] = "2026-13-40"
    errors = validate_spec(data)
    assert any(e.startswith("calendar.holidays[0]: ") for e in errors), errors


def test_minimal_spec_defaults() -> None:
    minimal = {
        "schema_version": "1",
        "meta": {"bank_name": "Example Bank", "agent_identity": "Example assistant"},
        "account_fields": {"product_id": "id"},
        "calendar": {"weekend_days": ["saturday", "sunday"]},
        "bands": [{
            "name": "only",
            "days_late_min": 1,
            "days_late_max": 30,
            "deadline": {"kind": "calendar_days_after_today", "n": 5},
            "persuasion_attempts": 1,
        }],
        "closing": {"outcome_table": [{"outcome": "done", "when": "always"}]},
    }
    spec = load_spec(minimal)
    # Neutral structural defaults.
    assert spec.calendar.week_starts_on == "monday"
    assert spec.calendar.holidays == []
    assert spec.calendar.weekday_semantics is None
    assert spec.bands[0].deadline.roll == "none"
    assert spec.multi_product.enabled is False
    assert spec.multi_product.max_products == 1
    assert spec.intents == []
    assert spec.meta.language == "en"
    dumped = spec_to_dict(spec)
    assert not _contains_none(dumped)
    assert load_spec(dumped) == spec


def test_policy_fields_have_no_default() -> None:
    """A policy-bearing field the spec leaves out stays None: no brief's rule is inherited."""
    minimal = {
        "schema_version": "1",
        "meta": {"bank_name": "Example Bank", "agent_identity": "Example assistant"},
        "account_fields": {"product_id": "id"},
        "calendar": {"weekend_days": ["saturday", "sunday"], "vague_period": {}},
        "bands": [{
            "name": "only",
            "days_late_min": 1,
            "days_late_max": 30,
            "deadline": {"kind": "calendar_days_after_today", "n": 5},
            "persuasion_attempts": 1,
        }],
        "opening": {},
        "third_party": {"callback_window": {
            "days": {"kind": "today_or_next_business_days"}, "hour_start": "09:00", "hour_end": "17:00",
        }},
        "identity": {
            "questions": [{"field": "q", "prompt": "Q?"}], "max_wrong_total": 1,
            "on_failure": {}, "decline": {},
        },
        "amounts": {"full_amount_field": "owed", "too_much": {}, "below_floor": {}},
        "promise_rules": {"readback": {}},
        "persuasion": {},
        "closing": {"outcome_table": [{"outcome": "done", "when": "always"}], "goodbye_before_close": {}},
    }
    spec = load_spec(minimal)
    policy_values = {
        "opening": [spec.opening.applies_to_everyone, spec.opening.max_confirmation_asks,
                    spec.opening.answer_question_first],
        "third_party": [spec.third_party.say_nothing_beyond_opening, spec.third_party.reasks_when_outside,
                        spec.third_party.others_may_answer_security, spec.third_party.others_may_hear_account],
        "callback_window": [spec.third_party.callback_window.days.n, spec.third_party.callback_window.inclusive,
                            spec.third_party.callback_window.must_be_after_now],
        "identity": [spec.identity.one_at_a_time, spec.identity.wrong_may_be_reasked,
                     spec.identity.on_failure.disclose_debt, spec.identity.decline.max_refusals],
        "calendar": [spec.calendar.vague_period.ask_exact_day_times],
        "amounts": [spec.amounts.too_much.offer_floor_times, spec.amounts.below_floor.offer_floor_times,
                    spec.amounts.accept_between_floor_and_full],
        "promise_rules": [
            spec.promise_rules.earliest, spec.promise_rules.must_be_business_day, spec.promise_rules.latest,
            spec.promise_rules.on_non_business_day, spec.promise_rules.on_after_deadline,
            spec.promise_rules.readback.require_clear_yes, spec.promise_rules.readback.recheck_on_change,
            spec.promise_rules.one_promise_per_product, spec.promise_rules.allow_change_or_cancel,
            spec.promise_rules.retries_on_failure, spec.promise_rules.say_recorded_only_after_ok,
        ],
        "persuasion": [spec.persuasion.customer_proposal_is_never_refusal,
                       spec.persuasion.do_not_repeat_refused_offer],
        "closing": [spec.closing.close_tool_last, spec.closing.exactly_one_close,
                    spec.closing.goodbye_before_close.required],
        "multi_product": [spec.multi_product.one_date_for_all],
    }
    for section, values in policy_values.items():
        assert values == [None] * len(values), section
    dumped = spec_to_dict(spec)
    assert not _contains_none(dumped)
    assert load_spec(dumped) == spec


def _schema_defaults(node: object, path: str, out: dict[str, object]) -> None:
    if isinstance(node, dict):
        if "default" in node and not isinstance(node["default"], dict):
            out[path] = node["default"]
        for key, value in node.items():
            if key != "default" or isinstance(value, dict):
                _schema_defaults(value, f"{path}/{key}", out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _schema_defaults(value, f"{path}/{i}", out)


def test_schema_defaults_are_neutral_only() -> None:
    """Only structural defaults remain in the schema; policy-bearing fields have none."""
    found: dict[str, object] = {}
    _schema_defaults(load_schema(), "", found)
    assert found == {
        "/$defs/meta/properties/language": "en",
        "/$defs/calendar/properties/holidays": [],
        "/$defs/calendar/properties/week_starts_on": "monday",
        "/$defs/band/properties/deadline/properties/roll": "none",
        "/$defs/amounts/properties/above_full": "not_specified",
        "/$defs/amounts/properties/rounding_decimals": 2,
        "/$defs/intent/properties/precedence": 100,
        "/$defs/intent/properties/applies_before_verification": False,
        "/$defs/intent/properties/applies_after_verification": True,
        "/$defs/intent/properties/occurrences_before_action": 1,
        "/$defs/multi_product/properties/enabled": False,
        "/$defs/multi_product/properties/max_products": 1,
        "/$defs/multi_product/properties/order": "account_order",
    }


def test_resolve_path() -> None:
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert resolve_path(data, "a.b[1].c") == 2
    for bad in ("a.x", "a.b[5]", "a.b.c", "", "a..b"):
        with pytest.raises(KeyError):
            resolve_path(data, bad)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "builder.spec", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_validate(tmp_path: Path) -> None:
    ok = _run_cli("validate", str(FIXTURES_DIR / "spec_brief_a.json"))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout.strip() == "OK"

    data = copy.deepcopy(load_fixture("spec_brief_a.json"))
    del data["bands"][1]["deadline"]["n"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    res = _run_cli("validate", str(bad))
    assert res.returncode == 1
    assert "bands[1].deadline.n: required" in res.stdout.splitlines()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _run_cli("validate", str(broken)).returncode == 2
    missing = _run_cli("validate", str(tmp_path / "missing.json"))
    assert missing.returncode == 2 and missing.stderr
