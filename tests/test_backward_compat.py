"""Golden backward-compatibility test for the date/amount engine.

tests/fixtures/golden_facts.json was written by `python -m tests.test_backward_compat --write` from the
engine as it was BEFORE the account-based deadline / percent floor / per-weekday window work. The
structured facts (everything except the text blocks date_facts and product_facts, which change by
design) of the five extracted fixture specs must stay identical: same bands, deadlines, floors,
business days, callback dates, attempt offers and aggregation.

Never regenerate the golden file after an engine change: a diff here is a regression, not a refresh.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import builder.engine as engine
from tests.conftest import FIXTURES_DIR, load_fixture

GOLDEN_PATH: Path = FIXTURES_DIR / "golden_facts.json"
SPEC_NAMES: tuple[str, ...] = (
    "spec_brief_a",
    "spec_brief_ab",
    "spec_variant_1",
    "spec_variant_2",
    "spec_variant_3",
)
NOWS: tuple[str, ...] = ("2026-10-14T11:00", "2026-10-17T20:30")
TEXT_BLOCKS: tuple[str, ...] = ("date_facts", "product_facts")
IO: dict[str, str] = {
    "products_var": "account_products",
    "products_flat_prefix": "get_account_response.data.products",
    "out_prefix": "",
}
FULL_VALUE: float = 1234.56
FLOOR_VALUE: float = 400.25
UNMAPPED_TYPE: str = "unmapped_type"


def _types(spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for pn in (spec.get("debt_statement") or {}).get("product_names") or []:
        if pn.get("product_type") not in out:
            out.append(pn["product_type"])
    for t in ((spec.get("amounts") or {}).get("floor_fields") or {}):
        if t not in out:
            out.append(t)
    return out + [UNMAPPED_TYPE]


def _day_samples(spec: dict[str, Any]) -> list[tuple[str, int]]:
    """(band name, days late) for the min, mid and max of every band."""
    out: list[tuple[str, int]] = []
    for b in spec.get("bands") or []:
        lo = b.get("days_late_min")
        hi = b.get("days_late_max")
        lo = 0 if lo is None else int(lo)
        hi = lo + 10 if hi is None else int(hi)
        for d in sorted({lo, (lo + hi) // 2, hi}):
            out.append((str(b.get("name")), d))
    return out


def _floor_fields(spec: dict[str, Any]) -> list[str]:
    am = spec.get("amounts") or {}
    names = list((am.get("floor_fields") or {}).values())
    if am.get("floor_default_field"):
        names.append(am["floor_default_field"])
    return sorted(set(names))


def _products(spec: dict[str, Any]) -> list[dict[str, Any]]:
    af = spec["account_fields"]
    full_field = spec["amounts"]["full_amount_field"]
    out: list[dict[str, Any]] = []
    for ptype in _types(spec):
        for _band, days in _day_samples(spec):
            for with_floor in (True, False):
                p: dict[str, Any] = {
                    af["product_id"]: "pid",
                    af["product_type"]: ptype,
                    af["days_late"]: days,
                    full_field: FULL_VALUE,
                }
                if with_floor:
                    for f in _floor_fields(spec):
                        p[f] = FLOOR_VALUE
                out.append(p)
    return out


def _structured(facts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in facts.items() if k not in TEXT_BLOCKS}


def _round(v: Any) -> Any:
    return round(v, 6) if isinstance(v, float) else v


def _facts(spec: dict[str, Any], now: str, products: list[dict[str, Any]]) -> dict[str, Any]:
    vars_in = {spec["call_variables"]["now"]: now, IO["products_var"]: products}
    return _structured(engine.compute_facts(vars_in, spec, IO))


def _promise_samples(spec: dict[str, Any], now: str, product: dict[str, Any]) -> list[Any]:
    today = engine.parse_now(now).date()
    cal = engine.make_calendar(spec)
    deadline = engine.deadline_for_days_late(product[spec["account_fields"]["days_late"]], today, spec)
    out: list[Any] = []
    for offset in range(-1, 40):
        proposed = (today + timedelta(days=offset)).isoformat()
        r = engine.check_promise_date(proposed, today, deadline, cal)
        out.append([proposed, r["status"], r["date"], r["deadline"]])
    out.append(["not a date", engine.check_promise_date("not a date", today, deadline, cal)["status"]])
    return out


def _amount_samples(spec: dict[str, Any], product: dict[str, Any]) -> list[Any]:
    full = engine.full_for(product, spec)
    floor = engine.floor_for(product, spec)
    values: list[Any] = [full, floor, 0, -5, "abc", None, 1.0, 399.99, 400.25, 800.5, 1234.56, 1234.57, "700"]
    return [[repr(v), engine.classify_amount(v, full, floor)] for v in values]


def _callback_samples(spec: dict[str, Any], now: str) -> list[Any]:
    cal = engine.make_calendar(spec)
    n = engine.parse_now(now)
    out: list[Any] = []
    for offset in range(0, 6):
        day = n.date() + timedelta(days=offset)
        for hm in ("08:00", "09:00", "10:00", "12:30", "16:59", "17:00", "19:00", "19:01", "23:00"):
            proposed = day.isoformat() + "T" + hm
            out.append([proposed, engine.check_callback(proposed, now, spec, cal)["status"]])
    out.append(["garbage", engine.check_callback("garbage", now, spec, cal)["status"]])
    return out


def _aggregation_samples(spec: dict[str, Any]) -> list[Any]:
    reasons = [None, "paid", "refused", "hardship", "dispute", "service_error", "other"]
    out: list[Any] = []
    for promise in (False, True):
        for r1 in reasons:
            for r2 in reasons:
                states = [
                    {"product_id": "a", "promise_recorded": promise, "note_reason": r1},
                    {"product_id": "b", "promise_recorded": False, "note_reason": r2},
                ]
                for transfer in (None, "reason_x"):
                    out.append([promise, r1, r2, transfer, engine.aggregate_outcome(states, spec, transfer)])
    out.append(engine.aggregate_outcome([], spec, None))
    return out


def _one_date_samples(spec: dict[str, Any], now: str, products: list[dict[str, Any]]) -> list[Any]:
    today = engine.parse_now(now).date()
    cal = engine.make_calendar(spec)
    out: list[Any] = []
    for offset in (0, 3, 8, 13, 25, 60):
        proposed = (today + timedelta(days=offset)).isoformat()
        out.append([proposed, engine.one_date_for_all(proposed, products, today, spec, cal)])
    return out


def build_snapshot() -> dict[str, Any]:
    """Structured facts and rule samples for the five fixture specs, JSON-serialisable."""
    snap: dict[str, Any] = {}
    for name in SPEC_NAMES:
        spec = load_fixture(name + ".json")
        per_spec: dict[str, Any] = {}
        products = _products(spec)
        for now in NOWS:
            cases: list[Any] = []
            for i, prod in enumerate(products):
                cases.append(
                    {
                        "product": prod,
                        "facts": _facts(spec, now, [prod]),
                        "promise": _promise_samples(spec, now, prod) if i % 3 == 0 else None,
                        "amounts": _amount_samples(spec, prod) if i % 2 == 0 else None,
                    }
                )
            slots = max(1, int((spec.get("multi_product") or {}).get("max_products", 1)))
            combined = products[:: max(1, len(products) // (slots + 1))][: slots + 1]
            per_spec[now] = {
                "cases": cases,
                "combined": {"products": combined, "facts": _facts(spec, now, combined)},
                "callback": _callback_samples(spec, now),
                "one_date": _one_date_samples(spec, now, combined),
            }
        per_spec["aggregation"] = _aggregation_samples(spec)
        snap[name] = per_spec
    return json.loads(json.dumps(snap, sort_keys=True))


def _write() -> None:
    GOLDEN_PATH.write_text(json.dumps(build_snapshot(), indent=1, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return build_snapshot()


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_structured_facts_unchanged(name: str, golden: dict[str, Any], current: dict[str, Any]) -> None:
    for now in NOWS:
        old, new = golden[name][now], current[name][now]
        assert len(old["cases"]) == len(new["cases"])
        for a, b in zip(old["cases"], new["cases"]):
            assert a["product"] == b["product"]
            assert a["facts"] == b["facts"], a["product"]
        assert old["combined"] == new["combined"]


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_rule_samples_unchanged(name: str, golden: dict[str, Any], current: dict[str, Any]) -> None:
    for now in NOWS:
        old, new = golden[name][now], current[name][now]
        for a, b in zip(old["cases"], new["cases"]):
            assert a["promise"] == b["promise"], a["product"]
            assert a["amounts"] == b["amounts"], a["product"]
        assert old["callback"] == new["callback"]
        assert old["one_date"] == new["one_date"]
    assert golden[name]["aggregation"] == current[name]["aggregation"]


def test_golden_covers_every_band_edge_and_both_nows(golden: dict[str, Any]) -> None:
    for name in SPEC_NAMES:
        spec = load_fixture(name + ".json")
        assert set(golden[name]) >= set(NOWS) | {"aggregation"}
        days = {c["product"][spec["account_fields"]["days_late"]] for c in golden[name][NOWS[0]]["cases"]}
        for b in spec["bands"]:
            if b.get("days_late_min") is not None:
                assert int(b["days_late_min"]) in days
            if b.get("days_late_max") is not None:
                assert int(b["days_late_max"]) in days


def test_golden_has_the_documented_fact_names(golden: dict[str, Any]) -> None:
    facts = golden["spec_brief_a"][NOWS[0]]["cases"][0]["facts"]
    for suffix in ("id", "type", "name", "full", "floor", "band", "deadline", "attempts", "attempt1_date", "attempt1_amount"):
        assert "p1_" + suffix in facts
    assert "business_days" in facts and "callback_dates" in facts
    assert "date_facts" not in facts and "product_facts" not in facts


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        _write()
        print("wrote", GOLDEN_PATH)
    else:
        print("usage: python -m tests.test_backward_compat --write")
