"""Snippet tests: platform rules, agreement with builder.engine, never raising, speed."""

from __future__ import annotations

import ast
import copy
import json
import time
from datetime import datetime
from typing import Any, Callable

import pytest

import builder.engine as engine
from builder.snippets import SnippetError, check_snippet, facts_snippet, spec_subset
from tests.snippet_harness import run_snippet
from tests.test_engine import (
    D,
    E30_CASES,
    IO,
    NOW,
    PUB_PRODUCTS,
    S1_SPEC,
    S3_SPEC,
    S4_SPEC,
    SPEC_A,
    SPEC_AB,
    TODAY,
    flatten,
    state,
)

FLAG_VALUES: frozenset[str] = frozenset(("known", "unknown", "ok", "error"))
FLAG_NAMES: tuple[str, ...] = ("facts_status", "now_state", "products_state")

# (restricted builtins, exec layout): one namespace, and separate globals/locals with vars
# only in locals; each with and without restricted builtins (no class statements, no open).
EXEC_MODES: list[tuple[bool, str]] = [(False, "single"), (True, "single"), (False, "split"), (True, "split")]
EXEC_MODE_IDS: list[str] = ["normal", "restricted", "split", "split-restricted"]

# Brief A cases run on the A spec, Brief B cases on the layered spec.
MATCH_CASES: list[tuple[str, dict[str, Any]]] = [
    ("PUB-01", SPEC_A),
    ("PUB-04", SPEC_A),
    ("PUB-06", SPEC_A),
    ("PUB-07", SPEC_A),
    ("PUB-12", SPEC_AB),
    ("PUB-13", SPEC_AB),
]


def input_forms(products: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The three shapes get_account products may take inside vars."""
    return {
        "list": {"now": NOW, IO["products_var"]: copy.deepcopy(products)},
        "json": {"now": NOW, IO["products_var"]: json.dumps(products)},
        "flat": {"now": NOW, **flatten(products, IO["products_flat_prefix"])},
    }


def facts_only(vars_out: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    return {k: vars_out[k] for k in engine.output_names(spec, IO)}


@pytest.mark.parametrize("spec", [SPEC_A, SPEC_AB], ids=["A", "AB"])
def test_snippet_rules(spec: dict[str, Any]) -> None:
    src = facts_snippet(spec, IO)
    assert "{{" not in src and "}}" not in src
    tree = ast.parse(src)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules <= {"json", "re", "datetime"}
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Try)
    handlers = tree.body[0].handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].type, ast.Name) and handlers[0].type.id == "Exception"


def test_snippet_escapes_doubled_braces_in_spec_strings() -> None:
    spec = copy.deepcopy(SPEC_A)
    spec["debt_statement"]["product_names"][0]["template"] = "card {{x}} ending {card_last4}"
    spec["persuasion"]["attempts"][0]["note"] = "}}x{{ and {{{y}}}"
    src = facts_snippet(spec, IO)
    assert "{{" not in src and "}}" not in src
    _, ns = run_snippet(src, {"now": NOW})
    assert ns["ENGINE"]["SPEC"] == spec_subset(spec)
    assert ns["ENGINE"]["IO"] == IO
    assert ns["ENGINE"]["SPEC"]["debt_statement"]["product_names"][0]["template"] == "card {{x}} ending {card_last4}"


@pytest.mark.parametrize(("restricted", "layout"), EXEC_MODES, ids=EXEC_MODE_IDS)
@pytest.mark.parametrize(("case", "spec"), MATCH_CASES, ids=[c for c, _ in MATCH_CASES])
def test_snippet_matches_engine(case: str, spec: dict[str, Any], restricted: bool, layout: str) -> None:
    src = facts_snippet(spec, IO)
    products = PUB_PRODUCTS[case]
    results: dict[str, dict[str, Any]] = {}
    for form, vars_in in input_forms(products).items():
        expected = engine.compute_facts(copy.deepcopy(vars_in), spec, IO)
        got, _ = run_snippet(src, copy.deepcopy(vars_in), restricted=restricted, layout=layout)
        assert facts_only(got, spec) == expected, form
        results[form] = expected
    assert results["list"] == results["json"] == results["flat"]

    facts = results["list"]
    assert facts["facts_status"] == "ok"
    assert facts["now_state"] == "known"
    assert facts["products_state"] == "known"
    assert facts["product_count"] == len(products)
    assert facts["today"] == "2026-10-14"
    business_line = next(line for line in facts["date_facts"].splitlines() if line.startswith("Next 10 business days"))
    assert "2026-10-15" in business_line and "2026-10-29" in business_line
    assert "2026-10-16" in business_line and "2026-10-27" in business_line
    assert "2026-10-26" not in business_line
    if case == "PUB-13":
        assert facts["p1_deadline"] == "2026-11-02"
        assert facts["p2_deadline"] == "2026-10-23"
    if case == "PUB-06":
        assert facts["p1_floor"] == 637.5
        assert facts["p1_attempt1_date"] == "2026-10-16"
    if case == "PUB-12":
        assert facts["p3_attempt2_date"] == ""
        assert facts["p1_name"] == "credit card ending 9015"


def _agreement_calls() -> list[tuple[str, Callable[[dict[str, Any]], object]]]:
    """(label, fn(namespace) -> result); run once with the snippet's namespace, once with the engine."""
    cases: list[tuple[str, Callable[[dict[str, Any]], object]]] = []

    def cal(m: dict[str, Any], spec: dict[str, Any] = SPEC_A) -> dict[str, Any]:
        return m["make_calendar"](spec)

    def dl(m: dict[str, Any], days: int) -> object:
        band = m["band_for"](days, SPEC_A)
        return m["deadline_for"](band, TODAY, cal(m))

    for days in (14, 20, 8, 45, 4, 5, 1, 7):
        cases.append((f"E2/E3 deadline {days}", lambda m, d=days: dl(m, d)))
    cases.append(("E3 add_business_days 12", lambda m: m["add_business_days"](TODAY, 12, cal(m))))
    cases.append(("E6", lambda m: m["check_promise_date"](D("2026-10-26"), TODAY, D("2026-11-02"), cal(m))))
    cases.append(("E7", lambda m: m["check_promise_date"](D("2026-11-06"), TODAY, D("2026-10-23"), cal(m))))
    cases.append(("E11", lambda m: m["last_bd_this_week_or_next"](TODAY, cal(m))))
    cases.append(("E12", lambda m: m["last_bd_this_week_or_next"](D("2026-10-16"), cal(m))))
    cases.append(("E18", lambda m: m["resolve_weekday"](D("2026-10-16"), "friday", "next", cal(m))))
    for p in ("2026-10-15T11:00", "2026-10-14T09:00", "2026-10-14T10:30", "2026-10-15T19:00",
              "2026-10-15T19:30", "2026-10-15T10:00", "2026-10-15T09:59", "2026-10-16T11:00"):
        cases.append((f"E21-E25 callback {p}", lambda m, p=p: m["check_callback"](
            datetime.fromisoformat(p), datetime.fromisoformat(NOW), SPEC_A, cal(m))))
    cases.append(("E27", lambda m: m["one_date_for_all"](
        D("2026-10-30"), PUB_PRODUCTS["PUB-13"], TODAY, SPEC_AB, cal(m, SPEC_AB))))
    for amount, _ in E30_CASES:
        cases.append((f"E30 {amount!r}", lambda m, a=amount: m["classify_amount"](a, 2125.1, 637.5)))
    for i in range(3):
        for n in (1, 2):
            cases.append((f"E32 p{i + 1} attempt {n}", lambda m, i=i, n=n: m["attempt_offer"](
                n, PUB_PRODUCTS["PUB-12"][i], TODAY, SPEC_AB, cal(m, SPEC_AB))))
    agg_cases: list[tuple[list[dict[str, Any]], str | None]] = [
        ([state("p1", True), state("p2", note="refused"), state("p3", note="refused")], None),
        ([state("p1", note="paid"), state("p2", note="paid")], None),
        ([state("p1", note="paid"), state("p2", note="dispute"), state("p3", note="refused")], None),
        ([state("p1", True), state("p2")], "restructuring"),
    ]
    for k, (states, reason) in enumerate(agg_cases):
        cases.append((f"E34-E37 #{k}", lambda m, s=states, r=reason: m["aggregate_outcome"](s, SPEC_AB, r)))
    cases.append(("S1 next bd", lambda m: m["next_business_day_after"](TODAY, cal(m, S1_SPEC))))
    cases.append(("S1 add 3", lambda m: m["add_business_days"](TODAY, 3, cal(m, S1_SPEC))))
    cases.append(("S1 last bd", lambda m: m["last_bd_this_week_or_next"](TODAY, cal(m, S1_SPEC))))
    for b in S3_SPEC["bands"]:
        cases.append((f"S3 deadline {b['name']}", lambda m, b=b: m["deadline_for"](b, TODAY, cal(m, S3_SPEC))))
    cases.append(("S3 attempt 3", lambda m: m["attempt_offer"](
        3, {"ref": "x", "kind": "k", "owed": 80.0, "late_days": 40}, TODAY, S3_SPEC, cal(m, S3_SPEC))))
    for day, q in (("2026-10-14", "next"), ("2026-10-16", "next"), ("2026-10-14", "this")):
        cases.append((f"S4 {q} from {day}", lambda m, d=day, q=q: m["resolve_weekday"](
            D(d), "friday" if q == "next" else "wednesday", q, cal(m, S4_SPEC))))
    return cases


@pytest.mark.parametrize(("restricted", "layout"), EXEC_MODES, ids=EXEC_MODE_IDS)
def test_snippet_function_agreement(restricted: bool, layout: str) -> None:
    _, ns = run_snippet(facts_snippet(SPEC_AB, IO), {"now": NOW}, restricted=restricted, layout=layout)
    snippet_engine = ns["ENGINE"]
    mod = vars(engine)
    assert snippet_engine["add_business_days"] is not engine.add_business_days
    for label, call in _agreement_calls():
        assert call(snippet_engine) == call(mod), label


class _ExplodingVars(dict):
    """vars whose reads fail, to force the snippet's except branch."""

    def get(self, key: object, default: object = None) -> object:
        raise RuntimeError("boom")


NEVER_RAISE_INPUTS: list[tuple[str, dict[str, Any]]] = [
    ("empty", {}),
    ("garbage now", {"now": "garbage"}),
    ("products not json", {"now": NOW, "account_products": "not json"}),
    ("non-dict item", {"now": NOW, "account_products": [1, "x", None, {"product_id": "p1", "days_late": [3]}]}),
    ("odd values", {"now": NOW, "account_products": [{"type": {"a": 1}, "past_due": "lots", "days_late": "n/a"}]}),
]


@pytest.mark.parametrize(("restricted", "layout"), EXEC_MODES, ids=EXEC_MODE_IDS)
@pytest.mark.parametrize("spec", [SPEC_A, SPEC_AB], ids=["A", "AB"])
@pytest.mark.parametrize(("label", "vars_in"), NEVER_RAISE_INPUTS, ids=[x for x, _ in NEVER_RAISE_INPUTS])
def test_snippet_never_raises(
    label: str, vars_in: dict[str, Any], spec: dict[str, Any], restricted: bool, layout: str
) -> None:
    got, _ = run_snippet(facts_snippet(spec, IO), copy.deepcopy(vars_in), restricted=restricted, layout=layout)
    _assert_outputs_safe(got, spec)
    assert got["facts_status"] == "ok", label


@pytest.mark.parametrize("spec", [SPEC_A, SPEC_AB], ids=["A", "AB"])
def test_snippet_fallback_branch(spec: dict[str, Any]) -> None:
    got, _ = run_snippet(facts_snippet(spec, IO), _ExplodingVars(now=NOW))
    _assert_outputs_safe(got, spec)
    assert got["facts_status"] == "error"
    assert got["date_facts"] and got["product_facts"]


def _assert_outputs_safe(got: dict[str, Any], spec: dict[str, Any]) -> None:
    for name in engine.output_names(spec, IO):
        assert name in dict.keys(got), name
        value = dict.__getitem__(got, name)
        assert not isinstance(value, bool), name
        assert isinstance(value, (str, int, float)), name
    for name in FLAG_NAMES:
        assert dict.__getitem__(got, name) in FLAG_VALUES, name


def test_check_snippet_rejects_class_and_global() -> None:
    for stmt in ("class X:\n        pass", "global x"):
        with pytest.raises(SnippetError):
            check_snippet("try:\n    " + stmt + "\nexcept Exception:\n    pass\n")


def test_snippet_speed() -> None:
    src = facts_snippet(SPEC_AB, IO)
    vars_in = {"now": NOW, "account_products": copy.deepcopy(PUB_PRODUCTS["PUB-12"])}
    start = time.perf_counter()
    run_snippet(src, vars_in)
    assert time.perf_counter() - start < 0.5
