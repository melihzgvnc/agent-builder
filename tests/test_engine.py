"""Engine tests: exact cases E1-E41 (public test set + brief arithmetic) and synthetic S1-S6."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from typing import Any

import pytest

import builder.engine as engine
from builder.engine import (
    add_business_days,
    aggregate_outcome,
    attempt_offer,
    band_for,
    check_callback,
    check_promise_date,
    classify_amount,
    deadline_for,
    deadline_for_days_late,
    floor_for,
    full_for,
    is_business_day,
    last_bd_this_week_or_next,
    make_calendar,
    next_business_day_after,
    next_business_day_on_or_after,
    normalize_products,
    allowed_callback_dates,
    callback_window_gaps,
    compute_facts,
    fill_template,
    one_date_for_all,
    output_names,
    resolve_weekday,
    spoken_product_name,
)
from tests.conftest import load_fixture

SPEC_A: dict[str, Any] = load_fixture("spec_brief_a.json")
SPEC_AB: dict[str, Any] = load_fixture("spec_brief_ab.json")
CAL: dict[str, Any] = make_calendar(SPEC_A)
NOW: str = "2026-10-14T11:00"
TODAY: date = date(2026, 10, 14)
IO: dict[str, str] = {
    "products_var": "account_products",
    "products_flat_prefix": "get_account_response.data.products",
    "out_prefix": "",
}


def D(s: str) -> date:
    return date.fromisoformat(s)


def with_ids(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of the products with product_id p1..pN first (account order)."""
    return [dict(product_id=f"p{i}", **p) for i, p in enumerate(products, start=1)]


# Product lists copied from public-test-set.json.
PUB_PRODUCTS: dict[str, list[dict[str, Any]]] = {
    "PUB-01": with_ids(
        [{"type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6, "minimum_payment": 126.0}]
    ),
    "PUB-02": with_ids(
        [{"type": "personal_loan", "past_due": 310.5, "days_late": 11, "instalment_amount": 103.5}]
    ),
    "PUB-04": with_ids(
        [{"type": "car_loan", "past_due": 1250.0, "days_late": 14, "instalment_amount": 400.0}]
    ),
    "PUB-06": with_ids(
        [{"type": "credit_card", "card_last4": "1903", "past_due": 2125.1, "days_late": 12, "minimum_payment": 637.5}]
    ),
    "PUB-07": with_ids(
        [{"type": "personal_loan", "past_due": 540.0, "days_late": 5, "instalment_amount": 180.0}]
    ),
    "PUB-10": with_ids(
        [{"type": "credit_card", "card_last4": "7740", "past_due": 365.0, "days_late": 8, "minimum_payment": 109.5}]
    ),
    "PUB-11": with_ids(
        [
            {"type": "credit_card", "card_last4": "2288", "past_due": 220.0, "days_late": 9, "minimum_payment": 66.0},
            {"type": "personal_loan", "past_due": 640.0, "days_late": 9, "instalment_amount": 213.0},
            {"type": "car_loan", "past_due": 75.0, "days_late": 3, "instalment_amount": 25.0},
        ]
    ),
    "PUB-12": with_ids(
        [
            {"type": "credit_card", "card_last4": "9015", "past_due": 510.0, "days_late": 16, "minimum_payment": 153.0},
            {"type": "personal_loan", "past_due": 1320.0, "days_late": 16, "instalment_amount": 440.0},
            {"type": "car_loan", "past_due": 140.0, "days_late": 7, "instalment_amount": 46.0},
        ]
    ),
    "PUB-13": with_ids(
        [
            {"type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0},
            {"type": "personal_loan", "past_due": 900.0, "days_late": 20, "instalment_amount": 300.0},
        ]
    ),
}


def state(pid: str, promise: bool = False, note: str | None = None) -> dict[str, Any]:
    return {"product_id": pid, "promise_recorded": promise, "note_reason": note}


def offer_pair(offer: dict[str, Any] | None) -> tuple[str, float] | None:
    return None if offer is None else (offer["date"], offer["amount"])


# ---------------------------------------------------------------------------------------------
# E1-E41
# ---------------------------------------------------------------------------------------------


def test_e1_is_business_day() -> None:
    assert is_business_day(D("2026-10-14"), CAL) is True
    assert is_business_day(D("2026-10-17"), CAL) is False
    assert is_business_day(D("2026-10-26"), CAL) is False


@pytest.mark.parametrize("days_late", [14, 20, 8, 45])
def test_e2_late_deadline(days_late: int) -> None:
    assert deadline_for_days_late(days_late, TODAY, SPEC_A) == D("2026-10-23")


@pytest.mark.parametrize("days_late", [4, 5, 1, 7])
def test_e3_early_deadline_skips_holiday(days_late: int) -> None:
    assert deadline_for_days_late(days_late, TODAY, SPEC_A) == D("2026-11-02")


def test_e4_band_for() -> None:
    names = [None if b is None else b["name"] for b in (band_for(x, SPEC_A) for x in (0, 1, 7, 8, 45, 46, "12"))]
    assert names == [None, "early", "early", "late", "late", None, "late"]


def test_e5_promise_inside_deadline() -> None:
    r = check_promise_date(D("2026-10-30"), TODAY, D("2026-11-02"), CAL)
    assert (r["status"], r["date"]) == ("ok", "2026-10-30")


def test_e6_holiday_moves_to_next_business_day() -> None:
    r = check_promise_date(D("2026-10-26"), TODAY, D("2026-11-02"), CAL)
    assert (r["status"], r["date"]) == ("moved_to_next_business_day", "2026-10-27")


def test_e7_after_deadline_gives_deadline() -> None:
    r = check_promise_date(D("2026-11-06"), TODAY, D("2026-10-23"), CAL)
    assert (r["status"], r["date"], r["deadline"]) == ("after_deadline", "2026-10-23", "2026-10-23")


def test_e8_past() -> None:
    assert check_promise_date(D("2026-10-13"), TODAY, D("2026-10-23"), CAL)["status"] == "past"


def test_e9_saturday_inside_deadline() -> None:
    r = check_promise_date(D("2026-10-17"), TODAY, D("2026-10-23"), CAL)
    assert (r["status"], r["date"]) == ("moved_to_next_business_day", "2026-10-19")


def test_e10_saturday_after_deadline() -> None:
    r = check_promise_date(D("2026-10-24"), TODAY, D("2026-10-23"), CAL)
    assert (r["status"], r["date"]) == ("after_deadline", "2026-10-23")


def test_e11_last_bd_this_week() -> None:
    assert last_bd_this_week_or_next(TODAY, CAL) == D("2026-10-16")


def test_e12_last_bd_on_friday_is_next_week() -> None:
    assert last_bd_this_week_or_next(D("2026-10-16"), CAL) == D("2026-10-23")


def test_e13_last_bd_with_holidays() -> None:
    assert last_bd_this_week_or_next(D("2026-12-24"), CAL) == D("2026-12-31")


def test_e14_tomorrow() -> None:
    tomorrow = next_business_day_after(TODAY, CAL)
    assert tomorrow == D("2026-10-15")
    assert check_promise_date(tomorrow, TODAY, D("2026-10-23"), CAL)["status"] == "ok"


def test_e15_weekend_skips() -> None:
    assert next_business_day_after(D("2026-10-16"), CAL) == D("2026-10-19")
    assert next_business_day_on_or_after(D("2026-10-17"), CAL) == D("2026-10-19")


def test_e16_weekend_plus_holiday() -> None:
    assert next_business_day_on_or_after(D("2026-10-24"), CAL) == D("2026-10-27")
    assert next_business_day_after(D("2026-11-10"), CAL) == D("2026-11-12")


def test_e17_this_friday() -> None:
    assert resolve_weekday(TODAY, "friday", "this", CAL) == D("2026-10-16")


def test_e18_next_friday_said_on_friday() -> None:
    assert resolve_weekday(D("2026-10-16"), "friday", "next", CAL) == D("2026-10-23")


def test_e19_next_weekday_following_week() -> None:
    assert resolve_weekday(TODAY, "friday", "next", CAL) == D("2026-10-23")
    assert resolve_weekday(TODAY, "monday", "next", CAL) == D("2026-10-19")
    assert resolve_weekday(TODAY, "wednesday", "next", CAL) == D("2026-10-21")


def test_e20_this_monday_is_past() -> None:
    d = resolve_weekday(TODAY, "monday", "this", CAL)
    assert d == D("2026-10-12")
    assert check_promise_date(d, TODAY, D("2026-10-23"), CAL)["status"] == "past"


def cb(proposed: str, now: str = NOW, spec: dict[str, Any] = SPEC_A) -> str:
    return check_callback(datetime.fromisoformat(proposed), datetime.fromisoformat(now), spec, CAL)["status"]


def test_e21_callback_next_day_ok() -> None:
    r = check_callback(datetime(2026, 10, 15, 11, 0), datetime(2026, 10, 14, 11, 0), SPEC_A, CAL)
    assert r == {"status": "ok", "date": "2026-10-15", "time": "11:00"}


def test_e22_callback_before_hours() -> None:
    assert cb("2026-10-14T09:00") == "outside_hours"


def test_e23_callback_not_after_now() -> None:
    assert cb("2026-10-14T10:30") == "not_after_now"


def test_e24_callback_hour_edges() -> None:
    assert [cb(f"2026-10-15T{t}") for t in ("19:00", "19:30", "10:00", "09:59")] == [
        "ok",
        "outside_hours",
        "ok",
        "outside_hours",
    ]


def test_e25_callback_second_business_day() -> None:
    assert cb("2026-10-16T11:00") == "day_not_allowed"


def test_e26_callback_from_friday() -> None:
    now = "2026-10-16T15:00"
    assert cb("2026-10-17T12:00", now) == "day_not_allowed"
    assert cb("2026-10-19T12:00", now) == "ok"
    assert cb("2026-10-16T16:00", now) == "ok"


def test_e27_one_date_capped_per_product() -> None:
    r = one_date_for_all(D("2026-10-30"), PUB_PRODUCTS["PUB-13"], TODAY, SPEC_AB, make_calendar(SPEC_AB))
    assert [(x["product_id"], x["date"], x["capped"]) for x in r] == [
        ("p1", "2026-10-30", False),
        ("p2", "2026-10-23", True),
    ]


def test_e28_one_date_for_three() -> None:
    r = one_date_for_all(D("2026-10-16"), PUB_PRODUCTS["PUB-11"], TODAY, SPEC_AB, make_calendar(SPEC_AB))
    assert [(x["date"], x["capped"], x["status"]) for x in r] == [("2026-10-16", False, "ok")] * 3


def test_e29_floor_for() -> None:
    assert floor_for(PUB_PRODUCTS["PUB-06"][0], SPEC_A) == 637.5
    assert floor_for(PUB_PRODUCTS["PUB-02"][0], SPEC_A) == 103.5
    assert floor_for(PUB_PRODUCTS["PUB-04"][0], SPEC_A) == 400.0
    assert full_for(PUB_PRODUCTS["PUB-06"][0], SPEC_A) == 2125.1


E30_CASES: list[tuple[object, str]] = [
    (2125.1, "full"),
    ("2125.10", "full"),
    (637.5, "in_range"),
    (1000, "in_range"),
    (637.49, "below_floor"),
    (600, "below_floor"),
    (2200, "above_full"),
    ("abc", "invalid"),
]


@pytest.mark.parametrize(("amount", "expected"), E30_CASES)
def test_e30_classify_amount(amount: object, expected: str) -> None:
    assert classify_amount(amount, 2125.1, 637.5) == expected


def test_e31_attempt1_floor_when_too_much() -> None:
    o = attempt_offer(1, PUB_PRODUCTS["PUB-06"][0], TODAY, SPEC_A, CAL, full_too_much=True)
    assert offer_pair(o) == ("2026-10-16", 637.5)
    assert offer_pair(attempt_offer(1, PUB_PRODUCTS["PUB-06"][0], TODAY, SPEC_A, CAL)) == ("2026-10-16", 2125.1)


def test_e32_attempts_per_band() -> None:
    card, loan, car = PUB_PRODUCTS["PUB-12"]
    cal = make_calendar(SPEC_AB)
    got = [offer_pair(attempt_offer(n, p, TODAY, SPEC_AB, cal)) for p in (card, loan, car) for n in (1, 2)]
    assert got == [
        ("2026-10-16", 510.0),
        ("2026-10-23", 153.0),
        ("2026-10-16", 1320.0),
        ("2026-10-23", 440.0),
        ("2026-10-16", 140.0),
        None,
    ]


def test_e33_pub10_attempt1() -> None:
    assert offer_pair(attempt_offer(1, PUB_PRODUCTS["PUB-10"][0], TODAY, SPEC_A, CAL)) == ("2026-10-16", 365.0)


def test_e34_aggregate_promise_with_refusals() -> None:
    states = [state("p1", promise=True), state("p2", note="refused"), state("p3", note="refused")]
    assert aggregate_outcome(states, SPEC_AB) == {"outcome": "promise", "reason": ""}


def test_e35_aggregate_all_paid() -> None:
    states = [state("p1", note="paid"), state("p2", note="paid")]
    assert aggregate_outcome(states, SPEC_AB) == {"outcome": "paid", "reason": ""}


def test_e36_aggregate_first_non_paid_note() -> None:
    states = [state("p1", note="paid"), state("p2", note="dispute"), state("p3", note="refused")]
    assert aggregate_outcome(states, SPEC_AB) == {"outcome": "no_promise", "reason": "dispute"}


def test_e37_aggregate_transfer_after_promise() -> None:
    states = [state("p1", promise=True), state("p2")]
    assert aggregate_outcome(states, SPEC_AB, transfer_reason="restructuring") == {
        "outcome": "transferred",
        "reason": "restructuring",
    }


def test_e38_aggregate_service_error() -> None:
    assert aggregate_outcome([state("p1", note="service_error")], SPEC_AB) == {
        "outcome": "no_promise",
        "reason": "service_error",
    }


def test_e39_aggregate_single_product_transfer() -> None:
    assert aggregate_outcome([state("p1", promise=True)], SPEC_A, transfer_reason="support") == {
        "outcome": "transferred",
        "reason": "support",
    }


def test_e40_spoken_product_name() -> None:
    assert spoken_product_name(PUB_PRODUCTS["PUB-01"][0], SPEC_A) == "credit card ending 4821"
    assert spoken_product_name(PUB_PRODUCTS["PUB-02"][0], SPEC_A) == "personal loan"
    assert spoken_product_name(PUB_PRODUCTS["PUB-04"][0], SPEC_A) == "car loan"


def flatten(products: list[dict[str, Any]], prefix: str) -> dict[str, str]:
    """Flattened '<prefix>.<i>.<field>' keys with string values, as the platform may map them."""
    out: dict[str, str] = {}
    for i, p in enumerate(products):
        for k, v in p.items():
            out[f"{prefix}.{i}.{k}"] = str(v)
    return out


def test_e41_normalize_products_forms() -> None:
    products = PUB_PRODUCTS["PUB-06"] + PUB_PRODUCTS["PUB-12"]
    prefix = IO["products_flat_prefix"]
    as_list = normalize_products(copy.deepcopy(products), {}, prefix, SPEC_A)
    as_json = normalize_products(json.dumps(products), {}, prefix, SPEC_A)
    as_wrapped = normalize_products(json.dumps({"products": products}), {}, prefix, SPEC_A)
    as_flat = normalize_products(None, flatten(products, prefix), prefix, SPEC_A)
    assert as_list == products
    assert as_json == products
    assert as_wrapped == products
    assert as_flat == products
    first = as_flat[0]
    assert first["past_due"] == 2125.1 and isinstance(first["past_due"], float)
    assert first["days_late"] == 12 and isinstance(first["days_late"], int)
    assert first["minimum_payment"] == 637.5
    assert first["card_last4"] == "1903"


def test_e41_normalize_flat_keeps_index_order() -> None:
    prefix = IO["products_flat_prefix"]
    flat = flatten(PUB_PRODUCTS["PUB-12"], prefix)
    flat = dict(reversed(list(flat.items())))
    flat["unrelated"] = "x"
    got = normalize_products("", flat, prefix, SPEC_A)
    assert [p["product_id"] for p in got] == ["p1", "p2", "p3"]


# ---------------------------------------------------------------------------------------------
# Synthetic generality cases S1-S6 (specs built here, not from fixtures)
# ---------------------------------------------------------------------------------------------


def synthetic_spec(**sections: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "call_variables": {"customer_id": "cid", "now": "clock"},
        "account_fields": {"product_id": "ref", "product_type": "kind", "days_late": "late_days"},
        "calendar": {"weekend_days": ["saturday", "sunday"], "holidays": []},
        "bands": [],
        "amounts": {"full_amount_field": "owed"},
    }
    spec.update(sections)
    return spec


S1_SPEC: dict[str, Any] = synthetic_spec(
    calendar={"weekend_days": ["friday", "saturday"], "holidays": ["2026-10-15"], "week_starts_on": "sunday"}
)
S3_SPEC: dict[str, Any] = synthetic_spec(
    bands=[
        {"name": "a", "days_late_min": 1, "days_late_max": 10, "persuasion_attempts": 1,
         "deadline": {"kind": "business_days_after_today", "n": 3}},
        {"name": "b", "days_late_min": 11, "days_late_max": 30, "persuasion_attempts": 2,
         "deadline": {"kind": "business_days_after_today", "n": 5}},
        {"name": "c", "days_late_min": 31, "days_late_max": 90, "persuasion_attempts": 3,
         "deadline": {"kind": "calendar_days_after_today", "n": 10, "roll": "previous_business_day"}},
    ],
    persuasion={
        "attempts": [
            {"index": 1, "date_rule": "today", "amount_rule": "full"},
            {"index": 2, "date_rule": "next_business_day", "amount_rule": "full"},
            {"index": 3, "date_rule": "deadline", "amount_rule": "full"},
        ]
    },
)
S4_SPEC: dict[str, Any] = synthetic_spec(
    calendar={
        "weekend_days": ["saturday", "sunday"],
        "holidays": [],
        "weekday_semantics": {"next": "next_occurrence", "this": "next_occurrence_inclusive"},
    }
)


def test_s1_friday_saturday_weekend_sunday_week() -> None:
    cal = make_calendar(S1_SPEC)
    assert next_business_day_after(TODAY, cal) == D("2026-10-18")
    assert add_business_days(TODAY, 3, cal) == D("2026-10-20")
    assert last_bd_this_week_or_next(TODAY, cal) == D("2026-10-22")


def test_s2_other_holidays() -> None:
    cal = make_calendar(synthetic_spec(calendar={"weekend_days": ["saturday", "sunday"], "holidays": ["2026-10-16"]}))
    assert last_bd_this_week_or_next(TODAY, cal) == D("2026-10-15")
    assert add_business_days(TODAY, 7, cal) == D("2026-10-26")
    assert add_business_days(TODAY, 0, cal) == TODAY


def test_s3_three_bands_with_calendar_days() -> None:
    cal = make_calendar(S3_SPEC)
    deadlines = [deadline_for(b, TODAY, cal) for b in S3_SPEC["bands"]]
    assert deadlines == [D("2026-10-19"), D("2026-10-21"), D("2026-10-23")]
    assert band_for(0, S3_SPEC) is None
    assert band_for(95, S3_SPEC) is None
    assert band_for(31, S3_SPEC)["name"] == "c"
    band_c = {"ref": "x1", "kind": "k", "owed": 80.0, "late_days": 40}
    band_a = {"ref": "x2", "kind": "k", "owed": 20.0, "late_days": 5}
    third = attempt_offer(3, band_c, TODAY, S3_SPEC, cal)
    assert third is not None and offer_pair(third) == ("2026-10-23", 80.0)
    assert attempt_offer(3, band_a, TODAY, S3_SPEC, cal) is None
    assert offer_pair(attempt_offer(2, band_c, TODAY, S3_SPEC, cal)) == ("2026-10-15", 80.0)


def test_s4_next_occurrence_semantics() -> None:
    cal = make_calendar(S4_SPEC)
    assert resolve_weekday(TODAY, "friday", "next", cal) == D("2026-10-16")
    assert resolve_weekday(D("2026-10-16"), "friday", "next", cal) == D("2026-10-23")
    assert resolve_weekday(TODAY, "wednesday", "this", cal) == D("2026-10-14")
    no_semantics = make_calendar(synthetic_spec())
    assert resolve_weekday(TODAY, "friday", "next", no_semantics) is None
    assert resolve_weekday(TODAY, "friday", "this", no_semantics) is None


def test_s5_renamed_fields_and_types() -> None:
    spec = synthetic_spec(
        account_fields={"product_id": "ref", "product_type": "kind", "days_late": "late_days", "products_key": "items"},
        amounts={"full_amount_field": "owed", "floor_fields": {"gold": "lowest"}, "floor_default_field": "base_min"},
        debt_statement={"product_names": [{"product_type": "gold", "template": "gold account {ref}{missing}"}]},
    )
    raw = json.dumps({"items": [
        {"ref": "G7", "kind": "gold", "owed": "50.00", "lowest": "10", "base_min": "5", "late_days": "3"},
        {"ref": "S2", "kind": "store_card", "owed": 30, "base_min": 7.5, "late_days": 2},
    ]})
    gold, store = normalize_products(raw, {}, "unused", spec)
    assert gold["owed"] == 50.0 and gold["lowest"] == 10.0 and gold["late_days"] == 3
    assert full_for(gold, spec) == 50.0
    assert floor_for(gold, spec) == 10.0
    assert floor_for(store, spec) == 7.5
    assert classify_amount(30, full_for(gold, spec), floor_for(gold, spec)) == "in_range"
    assert classify_amount(9.99, full_for(gold, spec), floor_for(gold, spec)) == "below_floor"
    assert spoken_product_name(gold, spec) == "gold account G7"
    assert spoken_product_name(store, spec) == "store card"


def test_s6_reordered_aggregation() -> None:
    spec = synthetic_spec(
        closing={
            "aggregation": {
                "precedence": ["first_note_not_equal", "any_promise", "transfer"],
                "promise_outcome": "won",
                "transfer_outcome": "moved",
                "first_note_not_equal": {"excluded_reason": "settled", "outcome": "lost"},
                "default": {"outcome": "nothing", "reason": "none_given"},
            }
        }
    )
    states = [state("a", promise=True), state("b", note="settled"), state("c", note="angry")]
    assert aggregate_outcome(states, spec, transfer_reason="desk") == {"outcome": "lost", "reason": "angry"}
    assert aggregate_outcome(states[:2], spec, transfer_reason="desk") == {"outcome": "won", "reason": ""}
    assert aggregate_outcome([state("b", note="settled")], spec, transfer_reason="desk") == {
        "outcome": "moved",
        "reason": "desk",
    }
    assert aggregate_outcome([], spec) == {"outcome": "nothing", "reason": "none_given"}


# A window and a multi-product section that leave the policy-bearing keys out: the engine
# must report them as unresolved instead of applying any brief's rule.
GAP_SPEC: dict[str, Any] = synthetic_spec(
    bands=[
        {"name": "a", "days_late_min": 1, "days_late_max": 10, "persuasion_attempts": 1,
         "deadline": {"kind": "business_days_after_today", "n": 3}},
        {"name": "b", "days_late_min": 11, "days_late_max": 30, "persuasion_attempts": 1,
         "deadline": {"kind": "business_days_after_today", "n": 5}},
    ],
    third_party={"callback_window": {"days": {"kind": "today_or_next_business_days"},
                                     "hour_start": "09:00", "hour_end": "17:00"}},
    multi_product={"enabled": True, "max_products": 2},
)
GAP_PRODUCTS: list[dict[str, Any]] = [
    {"ref": "x1", "kind": "k", "owed": 20.0, "late_days": 5},
    {"ref": "x2", "kind": "k", "owed": 30.0, "late_days": 20},
]


def test_missing_callback_window_keys_are_unresolved() -> None:
    cal = make_calendar(GAP_SPEC)
    assert callback_window_gaps(GAP_SPEC) == ["days.n", "inclusive", "must_be_after_now"]
    assert allowed_callback_dates(TODAY, GAP_SPEC, cal) == []
    r = check_callback(datetime.fromisoformat("2026-10-15T12:00"), datetime.fromisoformat(NOW), GAP_SPEC, cal)
    assert r == {"status": "window_unspecified", "date": "2026-10-15", "time": "12:00"}
    facts = compute_facts({"clock": NOW}, GAP_SPEC, IO)
    assert (
        "Callback window: not fully defined by the policy (missing: days.n, inclusive, must_be_after_now)."
        in facts["date_facts"]
    )
    assert "Callback dates allowed" not in facts["date_facts"]

    explicit = copy.deepcopy(GAP_SPEC)
    window = explicit["third_party"]["callback_window"]
    window["days"]["n"] = 1
    window["inclusive"] = False
    window["must_be_after_now"] = False
    assert callback_window_gaps(explicit) == []
    now = datetime.fromisoformat(NOW)
    assert check_callback(datetime.fromisoformat("2026-10-15T17:00"), now, explicit, cal)["status"] == "outside_hours"
    assert check_callback(datetime.fromisoformat("2026-10-14T10:00"), now, explicit, cal)["status"] == "ok"
    assert "hours 09:00 to 17:00 (end excluded)" in compute_facts({"clock": NOW}, explicit, IO)["date_facts"]


def test_missing_one_date_mode_is_unresolved() -> None:
    cal = make_calendar(GAP_SPEC)
    r = one_date_for_all(D("2026-10-20"), GAP_PRODUCTS, TODAY, GAP_SPEC, cal)
    assert r == [
        {"product_id": "x1", "date": "", "capped": False, "status": "unresolved"},
        {"product_id": "x2", "date": "2026-10-20", "capped": False, "status": "ok"},
    ]
    capped = copy.deepcopy(GAP_SPEC)
    capped["multi_product"]["one_date_for_all"] = "cap_each_to_own_deadline"
    assert one_date_for_all(D("2026-10-20"), GAP_PRODUCTS, TODAY, capped, cal)[0] == {
        "product_id": "x1", "date": "2026-10-19", "capped": True, "status": "after_deadline",
    }


def test_fill_template_missing_slots() -> None:
    assert fill_template("a {x} b {y}", {"x": "1"}) == "a 1 b "
    assert fill_template("{{lit}} {x}", {}) == "{lit} "


def test_reject_if_any_after_deadline_blanks_every_product() -> None:
    cal = make_calendar(GAP_SPEC)
    rejecting = copy.deepcopy(GAP_SPEC)
    rejecting["multi_product"]["one_date_for_all"] = "reject_if_any_after_deadline"
    r = one_date_for_all(D("2026-10-20"), GAP_PRODUCTS, TODAY, rejecting, cal)
    assert [(x["product_id"], x["date"], x["status"], x["capped"]) for x in r] == [
        ("x1", "", "after_deadline", False),
        ("x2", "", "rejected_other_product_after_deadline", False),
    ]
    # Nobody is over the deadline: dates stay.
    ok = one_date_for_all(D("2026-10-16"), GAP_PRODUCTS, TODAY, rejecting, cal)
    assert [(x["date"], x["status"]) for x in ok] == [("2026-10-16", "ok")] * 2
    # The cap mode is unchanged: the other product keeps its date.
    capped = copy.deepcopy(GAP_SPEC)
    capped["multi_product"]["one_date_for_all"] = "cap_each_to_own_deadline"
    r = one_date_for_all(D("2026-10-20"), GAP_PRODUCTS, TODAY, capped, cal)
    assert [(x["date"], x["status"]) for x in r] == [("2026-10-19", "after_deadline"), ("2026-10-20", "ok")]


def test_classify_amount_floor_unknown() -> None:
    assert classify_amount(100, 500, None) == "floor_unknown"
    assert classify_amount(100, 500) == "floor_unknown"
    assert classify_amount(500, 500) == "full"
    assert classify_amount(900, 500) == "above_full"
    assert classify_amount(0, 500) == "invalid"


def test_check_promise_date_deadline_unknown() -> None:
    r = check_promise_date(D("2026-10-20"), TODAY, None, CAL)
    assert r == {"status": "deadline_unknown", "date": "", "deadline": ""}
    assert check_promise_date(D("2026-10-13"), TODAY, None, CAL)["status"] == "past"
    assert check_promise_date("nope", TODAY, None, CAL)["status"] == "invalid"


def test_business_days_through() -> None:
    from builder.engine import business_days_through

    days = business_days_through(TODAY, D("2026-10-27"), CAL)
    assert [d.isoformat() for d in days] == [
        "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
        "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-27",
    ]
    assert business_days_through(D("2026-10-17"), D("2026-10-18"), CAL) == []
    assert business_days_through(D("2026-10-20"), D("2026-10-14"), CAL) == []
    assert business_days_through(D("2026-10-17"), D("2026-10-19"), CAL) == [D("2026-10-19")]


def test_facts_business_days_and_callback_dates() -> None:
    # Single early-band product: deadline 2026-11-02, so the list ends there.
    facts = compute_facts({"now": NOW, IO["products_var"]: PUB_PRODUCTS["PUB-07"]}, SPEC_A, IO)
    days = facts["business_days"].split(",")
    assert days[0] == "2026-10-14" and days[-1] == "2026-11-02"
    assert "2026-10-17" not in days and "2026-10-26" not in days
    assert facts["callback_dates"] == "2026-10-14,2026-10-15"
    # Several products: through the latest deadline among them.
    multi = compute_facts({"now": NOW, IO["products_var"]: PUB_PRODUCTS["PUB-13"]}, SPEC_AB, IO)
    assert multi["business_days"].split(",")[-1] == "2026-11-02"
    # Unknown now: both empty.
    unknown = compute_facts({IO["products_var"]: PUB_PRODUCTS["PUB-07"]}, SPEC_A, IO)
    assert unknown["business_days"] == "" and unknown["callback_dates"] == ""
    # No products: no deadline, so no business days (callback dates only need today).
    none = compute_facts({"now": NOW}, SPEC_A, IO)
    assert none["business_days"] == "" and none["callback_dates"] == "2026-10-14,2026-10-15"
    # Band missing for the product: no deadline.
    odd = compute_facts({"now": NOW, IO["products_var"]: [dict(PUB_PRODUCTS["PUB-07"][0], days_late=99)]}, SPEC_A, IO)
    assert odd["business_days"] == ""
    # Callback window with gaps: no dates at all.
    gaps = compute_facts({"clock": NOW}, GAP_SPEC, IO)
    assert gaps["callback_dates"] == ""
    assert {"business_days", "callback_dates"} <= set(output_names(SPEC_A, IO))


def _all_product_text(spec: dict[str, Any]) -> list[str]:
    out = []
    for prods in PUB_PRODUCTS.values():
        for i, p in enumerate(prods, start=1):
            out.append(engine._product_block(i, p, TODAY, spec, CAL))
    return out


def test_product_block_marks_repeat_floor_offer_only_when_floor_is_offered_once() -> None:
    marker = "a repeat of the floor offer"
    texts = _all_product_text(SPEC_A)
    attempt2 = [ln for t in texts for ln in t.splitlines() if ln.startswith("- attempt 2 offer")]
    assert attempt2 and any(marker in ln for ln in attempt2)
    assert not any(marker in ln for t in texts for ln in t.splitlines() if ln.startswith("- attempt 1 offer"))
    for value in (None, 2):
        spec = copy.deepcopy(SPEC_A)
        spec["amounts"]["too_much"]["offer_floor_times"] = value
        assert not any(marker in t for t in _all_product_text(spec))


# ---------------------------------------------------------------------------------------------
# Account-based deadlines, percent floors, per-weekday callback windows, attempt date_n,
# weekday-name calendar summary, allowed promise dates and the one-date table.
# ---------------------------------------------------------------------------------------------


def new_spec(**sections: Any) -> dict[str, Any]:
    """Copy of the layered spec without holidays; each keyword replaces or extends a section."""
    spec = copy.deepcopy(SPEC_AB)
    spec["calendar"]["holidays"] = []
    spec["account_fields"]["due_date"] = "due_on"
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key].update(value)
        else:
            spec[key] = value
    return spec


def with_deadline(kind: str, n: int, base: str | None = None, roll: str | None = None) -> dict[str, Any]:
    spec = new_spec()
    dl: dict[str, Any] = {"kind": kind, "n": n}
    if base:
        dl["base"] = base
    if roll:
        dl["roll"] = roll
    spec["bands"] = [dict(spec["bands"][0], days_late_min=1, days_late_max=45, deadline=dl)]
    return spec


def prod_d(due: str | None = "2026-10-12", late: Any = 4, **extra: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"product_id": "p1", "type": "credit_card", "days_late": late, "past_due": 1000.0}
    if due is not None:
        p["due_on"] = due
    p.update(extra)
    return p


@pytest.mark.parametrize(
    ("kind", "n", "base", "roll", "product", "want"),
    [
        ("business_days_after_base", 3, "due_date_field", None, prod_d("2026-10-12"), "2026-10-15"),
        ("business_days_after_base", 0, "due_date_field", None, prod_d("2026-10-12"), "2026-10-12"),
        ("business_days_after_base", 1, "due_date_field", None, prod_d("2026-10-16"), "2026-10-19"),
        ("calendar_days_after_base", 5, "due_date_field", None, prod_d("2026-10-12"), "2026-10-17"),
        ("calendar_days_after_base", 5, "due_date_field", "next_business_day", prod_d("2026-10-12"), "2026-10-19"),
        ("calendar_days_after_base", 5, "due_date_field", "previous_business_day", prod_d("2026-10-12"), "2026-10-16"),
        ("business_days_after_base", 2, "days_late_field", None, prod_d(late=4), "2026-10-13"),
        ("business_days_after_base", 2, "days_late_field", None, prod_d(None, late="4"), "2026-10-13"),
        ("calendar_days_after_base", 3, "days_late_field", None, prod_d(late=4), "2026-10-13"),
        ("calendar_days_after_base", 1, "days_late_field", "next_business_day", prod_d(late=4), "2026-10-12"),
    ],
)
def test_base_deadlines(kind: str, n: int, base: str, roll: str | None, product: dict[str, Any], want: str) -> None:
    spec = with_deadline(kind, n, base, roll)
    cal = make_calendar(spec)
    prods = normalize_products([product], {}, "", spec)
    assert engine.product_deadline(prods[0], TODAY, spec, cal) == D(want)
    assert engine.product_deadline(prods[0], TODAY, spec) == D(want)


@pytest.mark.parametrize(
    "product",
    [prod_d(None), prod_d("not a date"), prod_d(""), prod_d(12345)],
    ids=["missing", "garbage", "empty", "number"],
)
def test_unresolved_due_date_gives_no_deadline(product: dict[str, Any]) -> None:
    spec = with_deadline("business_days_after_base", 3, "due_date_field")
    assert engine.product_deadline(product, TODAY, spec) is None
    facts = compute_facts({"now": NOW, "account_products": [product]}, spec, IO)
    assert facts["p1_deadline"] == "" and facts["business_days"] == ""
    assert "latest promise date: " in facts["product_facts"]
    assert "allowed promise dates" not in facts["product_facts"]


def test_unresolved_base_cases() -> None:
    spec = with_deadline("business_days_after_base", 3, "due_date_field")
    band = spec["bands"][0]
    cal = make_calendar(spec)
    assert deadline_for(band, TODAY, cal) is None  # no product, no base
    assert deadline_for(band, TODAY, cal, prod_d()) is None  # no spec
    unmapped = copy.deepcopy(spec)
    del unmapped["account_fields"]["due_date"]
    assert engine.product_deadline(prod_d(), TODAY, unmapped) is None
    late = with_deadline("business_days_after_base", 2, "days_late_field")
    assert engine.base_date_for(late["bands"][0]["deadline"], prod_d(late="x"), TODAY, late) is None
    assert engine.base_date_for(late["bands"][0]["deadline"], {}, TODAY, late) is None
    assert engine.base_date_for({"base": "elsewhere"}, prod_d(), TODAY, late) is None
    assert engine.base_date_for({}, prod_d(), TODAY, late) is None
    assert engine.base_date_for(late["bands"][0]["deadline"], None, TODAY, late) is None  # type: ignore[arg-type]
    assert engine.product_deadline(prod_d(late=999), TODAY, late) is None  # no band


def test_base_date_for_values() -> None:
    spec = with_deadline("business_days_after_base", 1, "due_date_field")
    assert engine.base_date_for({"base": "due_date_field"}, prod_d("2026-10-12"), TODAY, spec) == D("2026-10-12")
    assert engine.base_date_for({"base": "days_late_field"}, prod_d(late=6), TODAY, spec) == D("2026-10-08")
    assert engine.base_date_for({"base": "days_late_field"}, prod_d(late="6"), TODAY, spec) == D("2026-10-08")


def test_product_deadline_equals_deadline_for_days_late_for_old_kinds() -> None:
    for spec in (SPEC_A, SPEC_AB, S3_SPEC):
        cal = make_calendar(spec)
        late_field = spec["account_fields"]["days_late"]
        for days in range(0, 100):
            prod = {late_field: days}
            assert engine.product_deadline(prod, TODAY, spec, cal) == deadline_for_days_late(days, TODAY, spec)
            assert engine.product_deadline(prod, TODAY, spec) == deadline_for_days_late(days, TODAY, spec)


def test_old_deadline_for_signature_still_works() -> None:
    band = SPEC_A["bands"][0]
    assert deadline_for(band, TODAY, CAL) == deadline_for(band, TODAY, CAL, {"x": 1}, SPEC_A)


def test_one_date_for_all_with_account_based_deadlines() -> None:
    spec = new_spec()
    spec["bands"] = [
        dict(spec["bands"][0], days_late_min=1, days_late_max=45,
             deadline={"kind": "business_days_after_base", "n": 2, "base": "due_date_field"})
    ]
    prods = [
        {"product_id": "a", "type": "credit_card", "days_late": 3, "past_due": 100.0, "due_on": "2026-10-12"},
        {"product_id": "b", "type": "credit_card", "days_late": 3, "past_due": 100.0, "due_on": "2026-10-14"},
        {"product_id": "c", "type": "credit_card", "days_late": 3, "past_due": 100.0},
    ]
    res = one_date_for_all("2026-10-16", prods, TODAY, spec, make_calendar(spec))
    assert [(r["product_id"], r["date"], r["capped"], r["status"]) for r in res] == [
        ("a", "2026-10-14", True, "after_deadline"),
        ("b", "2026-10-16", False, "ok"),
        ("c", "", False, "deadline_unknown"),
    ]


# ---- percent floors --------------------------------------------------------------------------


def pct_spec(mode: str | None, percent: float = 12.5, decimals: int | None = None) -> dict[str, Any]:
    spec = new_spec()
    am = spec["amounts"]
    am.pop("floor_fields")
    am["floor_default_percent"] = {"percent": percent, "of_field": "bal"}
    am.pop("rounding_mode", None)
    if mode:
        am["rounding_mode"] = mode
    if decimals is not None:
        am["rounding_decimals"] = decimals
    return spec


def floor_of(spec: dict[str, Any], bal: Any) -> float | None:
    return floor_for({"product_id": "p1", "type": "credit_card", "bal": bal}, spec)


@pytest.mark.parametrize(
    ("bal", "percent", "decimals", "half_up", "half_even", "up", "down"),
    [
        (1.0, 12.5, 2, 0.13, 0.12, 0.13, 0.12),  # exact tie, lower digit even
        (3.0, 12.5, 2, 0.38, 0.38, 0.38, 0.37),  # exact tie, lower digit odd
        (100.05, 10, 2, 10.01, 10.0, 10.01, 10.0),  # tie 10.005
        (200.0, 10, 2, 20.0, 20.0, 20.0, 20.0),  # exact, nothing to round
        (100.01, 33.3, 2, 33.3, 33.3, 33.31, 33.3),  # 33.30333
        (100.0, 33.35, 2, 33.35, 33.35, 33.35, 33.35),  # percent with two decimals stays exact
        (25.0, 10, 0, 3.0, 2.0, 3.0, 2.0),  # 2.5 at zero decimals
        (35.0, 10, 0, 4.0, 4.0, 4.0, 3.0),  # 3.5 at zero decimals
        (0.01, 50, 2, 0.01, 0.0, 0.01, 0.0),  # 0.005
        (1234.56, 7.25, 2, 89.51, 89.51, 89.51, 89.5),  # 89.5056
    ],
)
def test_percent_floor_rounding_modes(
    bal: float, percent: float, decimals: int, half_up: float, half_even: float, up: float, down: float
) -> None:
    for mode, want in (("half_up", half_up), ("half_even", half_even), ("up", up), ("down", down)):
        assert floor_of(pct_spec(mode, percent, decimals), bal) == want, (mode, bal, percent)


def test_percent_floor_without_rounding_mode_uses_round() -> None:
    spec = pct_spec(None)
    assert floor_of(spec, 100.0) == 12.5
    assert floor_of(spec, 1.0) == round(0.125, 2)
    assert floor_of(spec, 1234.56) == round(1234.56 * 12.5 / 100, 2)
    assert floor_of(pct_spec("bogus"), 100.0) == 12.5  # not an enum value: same as absent


def test_percent_floor_is_exact_in_integer_arithmetic() -> None:
    spec = pct_spec("half_up", 0.1, 2)
    for cents in range(0, 20001, 7):
        bal = cents / 100
        want = (2 * cents * 100 + 100000) // 200000  # percent 0.1 -> 100 milli-percent
        assert floor_of(spec, bal) == want / 100


def test_percent_floor_precedence() -> None:
    spec = new_spec()
    spec["amounts"].update(
        floor_fields={"credit_card": "field_a"},
        floor_percent={"personal_loan": {"percent": 50, "of_field": "bal"}, "credit_card": {"percent": 40, "of_field": "bal"}},
        floor_default_field="field_d",
        floor_default_percent={"percent": 10, "of_field": "bal"},
        rounding_mode="half_up",
    )
    base = {"product_id": "p1", "bal": 200.0, "field_a": 11.0, "field_d": 22.0}
    assert floor_for(dict(base, type="credit_card"), spec) == 11.0  # floor_fields first
    assert floor_for(dict(base, type="personal_loan"), spec) == 100.0  # then floor_percent[type]
    assert floor_for(dict(base, type="car_loan"), spec) == 22.0  # then floor_default_field
    del spec["amounts"]["floor_default_field"]
    assert floor_for(dict(base, type="car_loan"), spec) == 20.0  # then floor_default_percent
    del spec["amounts"]["floor_default_percent"]
    assert floor_for(dict(base, type="car_loan"), spec) is None


def test_percent_floor_unknown_input_never_falls_through() -> None:
    spec = new_spec()
    spec["amounts"].update(
        floor_fields={"credit_card": "field_a"},
        floor_percent={"personal_loan": {"percent": 50, "of_field": "bal"}},
        floor_default_field="field_d",
    )
    assert floor_for({"type": "credit_card", "field_d": 5.0}, spec) is None  # field_a missing, default not used
    assert floor_for({"type": "personal_loan", "field_d": 5.0}, spec) is None  # bal missing, default not used
    assert floor_for({"type": "personal_loan", "bal": "abc", "field_d": 5.0}, spec) is None
    assert floor_for({"type": "personal_loan", "bal": None}, spec) is None
    assert floor_for({"type": "personal_loan", "bal": True}, spec) is None
    assert floor_for({"type": "personal_loan", "bal": -10}, spec) is None
    assert floor_for({"type": "car_loan", "field_d": 5.0}, spec) == 5.0


def test_percent_floor_numeric_string_of_field() -> None:
    spec = pct_spec("half_up", 25, 2)
    assert floor_of(spec, "200.50") == 50.13
    products = normalize_products([{"product_id": "p1", "type": "credit_card", "bal": "200.50", "days_late": "4"}], {}, "", spec)
    assert products[0]["bal"] == 200.5 and products[0]["days_late"] == 4
    assert floor_for(products[0], spec) == 50.13
    flat = {"acc.0.product_id": "p1", "acc.0.type": "credit_card", "acc.0.bal": "80", "acc.0.days_late": "4"}
    assert floor_for(normalize_products(None, flat, "acc", spec)[0], spec) == 20.0


def test_percent_floor_in_facts_and_classify() -> None:
    spec = pct_spec("half_up", 30, 2)
    prod = {"product_id": "p1", "type": "credit_card", "days_late": 10, "past_due": 500.0, "bal": "250.00"}
    facts = compute_facts({"now": NOW, "account_products": [prod]}, spec, IO)
    assert facts["p1_floor"] == 75.0
    assert "- floor amount: 75.00" in facts["product_facts"]
    assert facts["p1_attempt2_amount"] == 75.0
    floor = floor_for(normalize_products([prod], {}, "", spec)[0], spec)
    assert classify_amount(74.99, 500.0, floor) == "below_floor"
    assert classify_amount(75.0, 500.0, floor) == "in_range"
    unresolved = compute_facts({"now": NOW, "account_products": [dict(prod, bal=None)]}, spec, IO)
    assert unresolved["p1_floor"] == ""
    assert "- floor amount: unknown" in unresolved["product_facts"]
    assert classify_amount(100.0, 500.0, None) == "floor_unknown"


def test_missing_of_field_value_gives_none_and_numeric_fields_include_of_field() -> None:
    spec = pct_spec("down", 30)
    assert floor_of(spec, None) is None
    fields, _days = engine._numeric_fields(spec)
    assert "bal" in fields
    spec["amounts"]["floor_percent"] = {"car_loan": {"percent": 5, "of_field": "other_bal"}}
    fields, _days = engine._numeric_fields(spec)
    assert "bal" in fields and "other_bal" in fields


# ---- per-weekday callback windows --------------------------------------------------------------


def cb_spec(per_weekday: Any = "default") -> dict[str, Any]:
    spec = new_spec()
    spec["third_party"]["callback_window"] = {
        "days": {"kind": "today_or_next_business_days", "n": 1},
        "hour_start": "10:00",
        "hour_end": "19:00",
        "inclusive": True,
        "must_be_after_now": True,
    }
    if per_weekday == "default":
        per_weekday = [{"weekdays": ["friday"], "hour_start": "09:00", "hour_end": "13:00", "inclusive": False}]
    if per_weekday is not None:
        spec["third_party"]["callback_window"]["per_weekday"] = per_weekday
    return spec


THURSDAY_NOW = "2026-10-15T08:00"  # allowed dates: Thursday 2026-10-15 and Friday 2026-10-16


@pytest.mark.parametrize(
    ("proposed", "status"),
    [
        ("2026-10-16T09:00", "ok"),  # Friday window start
        ("2026-10-16T12:59", "ok"),
        ("2026-10-16T13:00", "outside_hours"),  # end excluded on Friday
        ("2026-10-16T14:00", "outside_hours"),  # inside the single window, outside Friday's
        ("2026-10-16T08:59", "outside_hours"),
        ("2026-10-15T14:00", "ok"),  # Thursday uses the single window
        ("2026-10-15T09:30", "outside_hours"),  # inside Friday's window, not Thursday's
        ("2026-10-15T19:00", "ok"),  # single window end included
        ("2026-10-17T11:00", "day_not_allowed"),
    ],
)
def test_check_callback_per_weekday(proposed: str, status: str) -> None:
    spec = cb_spec()
    assert check_callback(proposed, THURSDAY_NOW, spec, make_calendar(spec))["status"] == status


def test_per_weekday_entry_inherits_inclusive_from_the_single_window() -> None:
    spec = cb_spec([{"weekdays": ["friday"], "hour_start": "09:00", "hour_end": "13:00"}])
    cal = make_calendar(spec)
    assert check_callback("2026-10-16T13:00", THURSDAY_NOW, spec, cal)["status"] == "ok"
    assert check_callback("2026-10-16T13:01", THURSDAY_NOW, spec, cal)["status"] == "outside_hours"


def test_callback_window_for() -> None:
    spec = cb_spec()
    friday = engine.callback_window_for(D("2026-10-16"), spec)
    assert (friday["hour_start"], friday["hour_end"], friday["inclusive"]) == ("09:00", "13:00", False)
    assert friday["must_be_after_now"] is True and "per_weekday" not in friday
    thursday = engine.callback_window_for(D("2026-10-15"), spec)
    assert (thursday["hour_start"], thursday["hour_end"], thursday["inclusive"]) == ("10:00", "19:00", True)
    plain = engine.callback_window_for(D("2026-10-16"), cb_spec(None))
    assert (plain["hour_start"], plain["hour_end"]) == ("10:00", "19:00")
    first = cb_spec([
        {"weekdays": ["Friday"], "hour_start": "09:00", "hour_end": "10:00"},
        {"weekdays": ["friday"], "hour_start": "11:00", "hour_end": "12:00"},
    ])
    assert engine.callback_window_for(D("2026-10-16"), first)["hour_start"] == "09:00"


@pytest.mark.parametrize(
    ("per_weekday", "gap"),
    [
        ([{"weekdays": ["friday"], "hour_start": "9am", "hour_end": "13:00"}], "per_weekday[0].hours"),
        ([{"weekdays": [], "hour_start": "09:00", "hour_end": "13:00"}], "per_weekday[0].hours"),
        ([{"weekdays": ["funday"], "hour_start": "09:00", "hour_end": "13:00"}], "per_weekday[0].hours"),
        ([{"weekdays": ["monday"], "hour_start": "09:00", "hour_end": "13:00"}, "junk"], "per_weekday[1].hours"),
        ([{"weekdays": ["friday"], "hour_start": "09:00", "hour_end": "13:00", "inclusive": "yes"}], "per_weekday[0].hours"),
        ([], "per_weekday"),
        ("friday", "per_weekday"),
    ],
)
def test_malformed_per_weekday_is_reported_as_a_gap(per_weekday: Any, gap: str) -> None:
    spec = cb_spec(per_weekday)
    assert gap in callback_window_gaps(spec)
    cal = make_calendar(spec)
    assert check_callback("2026-10-15T14:00", THURSDAY_NOW, spec, cal)["status"] == "window_unspecified"
    facts = compute_facts({"now": THURSDAY_NOW, "account_products": []}, spec, IO)
    assert facts["callback_dates"] == ""
    assert "not fully defined" in facts["date_facts"] and gap in facts["date_facts"]


def test_callback_gaps_unchanged_without_per_weekday() -> None:
    assert callback_window_gaps(cb_spec(None)) == []
    assert callback_window_gaps(cb_spec()) == []


def test_callback_hours_text_and_date_facts() -> None:
    spec = cb_spec()
    cal = make_calendar(spec)
    dates = allowed_callback_dates(D("2026-10-15"), spec, cal)
    assert engine.callback_hours_text(dates, spec).splitlines() == [
        "Thursday 2026-10-15: 10:00 to 19:00 (both included)",
        "Friday 2026-10-16: 09:00 to 13:00 (end excluded)",
    ]
    facts = compute_facts({"now": THURSDAY_NOW, "account_products": []}, spec, IO)
    text = facts["date_facts"]
    assert "Callback dates allowed: Thursday 2026-10-15, Friday 2026-10-16; the hours depend on the date:" in text
    assert "Friday 2026-10-16: 09:00 to 13:00 (end excluded)" in text
    assert "The time must be later than now (08:00 today)." in text
    plain = compute_facts({"now": THURSDAY_NOW, "account_products": []}, cb_spec(None), IO)["date_facts"]
    assert "; hours 10:00 to 19:00 (both included); the time must be later than now (08:00 today)." in plain
    assert engine.callback_hours_text([], spec) == ""


# ---- attempt date rule -----------------------------------------------------------------------


def attempt_spec(rule: str, date_n: Any = 3, deadline_n: int = 12) -> dict[str, Any]:
    spec = new_spec()
    spec["bands"] = [dict(spec["bands"][0], days_late_min=1, days_late_max=45, persuasion_attempts=2,
                          deadline={"kind": "business_days_after_today", "n": deadline_n})]
    att = {"index": 1, "date_rule": rule, "amount_rule": "full"}
    if date_n is not None:
        att["date_n"] = date_n
    spec["persuasion"]["attempts"] = [att]
    return spec


def test_attempt_offer_business_days_after_today() -> None:
    spec = attempt_spec("business_days_after_today", 3)
    cal = make_calendar(spec)
    prod = {"product_id": "p1", "type": "credit_card", "days_late": 4, "past_due": 500.0}
    offer = attempt_offer(1, prod, TODAY, spec, cal)
    assert offer is not None and offer["date"] == "2026-10-19" and offer["date_rule"] == "business_days_after_today"
    assert attempt_offer(1, prod, D("2026-10-16"), spec, cal)["date"] == "2026-10-21"  # Friday: weekend skipped
    zero = attempt_spec("business_days_after_today", 0)
    assert attempt_offer(1, prod, TODAY, zero, cal)["date"] == "2026-10-14"


def test_attempt_offer_business_days_after_today_is_capped_by_the_deadline() -> None:
    spec = attempt_spec("business_days_after_today", 9, deadline_n=2)
    prod = {"product_id": "p1", "type": "credit_card", "days_late": 4, "past_due": 500.0}
    assert attempt_offer(1, prod, TODAY, spec, make_calendar(spec))["date"] == "2026-10-16"


def test_attempt_offer_business_days_after_today_without_date_n_has_no_date() -> None:
    spec = attempt_spec("business_days_after_today", None)
    prod = {"product_id": "p1", "type": "credit_card", "days_late": 4, "past_due": 500.0}
    assert attempt_offer(1, prod, TODAY, spec, make_calendar(spec))["date"] == ""


def test_attempt_offer_with_account_based_deadline_cap() -> None:
    spec = attempt_spec("deadline")
    spec["bands"][0]["deadline"] = {"kind": "business_days_after_base", "n": 2, "base": "due_date_field"}
    prod = {"product_id": "p1", "type": "credit_card", "days_late": 4, "past_due": 500.0, "due_on": "2026-10-12"}
    assert attempt_offer(1, prod, TODAY, spec, make_calendar(spec))["date"] == "2026-10-14"
    assert attempt_offer(1, dict(prod, due_on=""), TODAY, spec, make_calendar(spec))["date"] == ""


# ---- business week in words, allowed dates, one-date table -------------------------------------


def calendar_of(weekend: list[str], week_starts_on: str) -> dict[str, Any]:
    spec = new_spec()
    spec["calendar"]["weekend_days"] = weekend
    spec["calendar"]["week_starts_on"] = week_starts_on
    return make_calendar(spec)


def test_calendar_summary_sunday_to_thursday() -> None:
    cal = calendar_of(["friday", "saturday"], "sunday")
    assert engine.calendar_summary(cal) == (
        "Business days of the week: Sunday, Monday, Tuesday, Wednesday, Thursday. "
        "Weekend days (never business days): Friday, Saturday."
    )


def test_calendar_summary_monday_to_friday() -> None:
    cal = calendar_of(["saturday", "sunday"], "monday")
    assert engine.calendar_summary(cal) == (
        "Business days of the week: Monday, Tuesday, Wednesday, Thursday, Friday. "
        "Weekend days (never business days): Saturday, Sunday."
    )


def test_calendar_summary_edge_calendars() -> None:
    assert "Weekend days (never business days): none." in engine.calendar_summary(calendar_of([], "monday"))
    assert engine.calendar_summary(calendar_of(["saturday", "sunday"], "wednesday")).startswith(
        "Business days of the week: Wednesday, Thursday, Friday, Monday, Tuesday."
    )


def test_date_facts_sunday_calendar_lists_sunday_as_business_day() -> None:
    spec = new_spec()
    spec["calendar"]["weekend_days"] = ["friday", "saturday"]
    spec["calendar"]["week_starts_on"] = "sunday"
    facts = compute_facts({"now": NOW, "account_products": []}, spec, IO)
    text = facts["date_facts"]
    assert text.splitlines()[-1] == (
        "Business days of the week: Sunday, Monday, Tuesday, Wednesday, Thursday. "
        "Weekend days (never business days): Friday, Saturday."
    )
    business = next(line for line in text.splitlines() if line.startswith("Next 10 business days"))
    assert "Sunday 2026-10-18" in business and "Friday" not in business and "Saturday" not in business
    mon_fri = compute_facts({"now": NOW, "account_products": []}, new_spec(), IO)["date_facts"]
    assert mon_fri.splitlines()[-1].startswith("Business days of the week: Monday, Tuesday")
    assert "Sunday" not in next(line for line in mon_fri.splitlines() if line.startswith("Next 10 business days"))


def test_allowed_promise_dates_line() -> None:
    spec = with_deadline("business_days_after_today", 4)
    facts = compute_facts({"now": NOW, "account_products": [prod_d()]}, spec, IO)
    lines = facts["product_facts"].splitlines()
    i = next(k for k, line in enumerate(lines) if line.startswith("- latest promise date:"))
    assert lines[i] == "- latest promise date: 2026-10-20"
    assert lines[i + 1] == (
        "- allowed promise dates (every business day up to the latest promise date): "
        "Wednesday 2026-10-14, Thursday 2026-10-15, Friday 2026-10-16, Monday 2026-10-19, Tuesday 2026-10-20"
    )
    assert facts["business_days"] == "2026-10-14,2026-10-15,2026-10-16,2026-10-19,2026-10-20"


def test_allowed_promise_dates_are_capped() -> None:
    spec = with_deadline("business_days_after_today", 30)
    facts = compute_facts({"now": NOW, "account_products": [prod_d()]}, spec, IO)
    line = next(x for x in facts["product_facts"].splitlines() if x.startswith("- allowed promise dates"))
    assert line.count("20") >= engine.LISTED_PROMISE_DATES  # 15 dates plus the last one
    assert ", ... last: Wednesday 2026-11-25" in line
    listed = line.split(": ", 1)[1].split(", ... last:")[0].split(", ")
    assert len(listed) == engine.LISTED_PROMISE_DATES
    assert listed[0] == "Wednesday 2026-10-14"


def test_allowed_promise_dates_when_today_is_not_a_business_day() -> None:
    spec = with_deadline("business_days_after_today", 1)
    facts = compute_facts({"now": "2026-10-17T11:00", "account_products": [prod_d()]}, spec, IO)
    line = next(x for x in facts["product_facts"].splitlines() if x.startswith("- allowed promise dates"))
    assert line.endswith(": Monday 2026-10-19")


def two_deadline_products() -> list[dict[str, Any]]:
    return [
        {"product_id": "a", "type": "credit_card", "days_late": 4, "past_due": 100.0},  # 12 business days
        {"product_id": "b", "type": "personal_loan", "days_late": 10, "past_due": 100.0},  # 7 business days
    ]


def test_one_date_table_for_different_deadlines() -> None:
    cal = make_calendar(SPEC_AB)
    prods = normalize_products(two_deadline_products(), {}, "", SPEC_AB)
    table = engine.one_date_table(prods, TODAY, SPEC_AB, cal)
    lines = table.splitlines()
    assert lines[0].startswith("One date for all products")
    assert len(lines) == 1 + engine.LISTED_BUSINESS_DAYS + 1
    assert lines[1] == (
        "If the customer names Wednesday 2026-10-14 for all products: "
        "a -> Wednesday 2026-10-14, b -> Wednesday 2026-10-14"
    )
    assert "b -> Friday 2026-10-23" in lines[engine.LISTED_BUSINESS_DAYS]  # capped at b's own deadline
    assert "a -> " in lines[-1] and lines[-1].startswith("If the customer names a later date, up to Monday 2026-11-02")
    facts = compute_facts({"now": NOW, "account_products": two_deadline_products()}, SPEC_AB, IO)
    assert facts["product_facts"].endswith(table)


def test_one_date_table_not_built_when_not_needed() -> None:
    cal = make_calendar(SPEC_AB)
    same = normalize_products(two_deadline_products()[:1] * 2, {}, "", SPEC_AB)
    assert engine.one_date_table(same, TODAY, SPEC_AB, cal) == ""
    assert engine.one_date_table(same[:1], TODAY, SPEC_AB, cal) == ""
    reject = copy.deepcopy(SPEC_AB)
    reject["multi_product"]["one_date_for_all"] = "reject_if_any_after_deadline"
    prods = normalize_products(two_deadline_products(), {}, "", reject)
    assert engine.one_date_table(prods, TODAY, reject, cal) == ""
    absent = copy.deepcopy(SPEC_AB)
    del absent["multi_product"]["one_date_for_all"]
    assert engine.one_date_table(prods, TODAY, absent, cal) == ""
    assert "One date for all products" not in compute_facts(
        {"now": NOW, "account_products": two_deadline_products()}, reject, IO)["product_facts"]


def test_one_date_table_with_account_based_deadlines() -> None:
    spec = new_spec()
    spec["bands"] = [dict(spec["bands"][0], days_late_min=1, days_late_max=45,
                          deadline={"kind": "business_days_after_base", "n": 2, "base": "due_date_field"})]
    prods = [
        {"product_id": "a", "type": "credit_card", "days_late": 3, "past_due": 100.0, "due_on": "2026-10-12"},
        {"product_id": "b", "type": "credit_card", "days_late": 3, "past_due": 100.0, "due_on": "2026-10-14"},
    ]
    table = engine.one_date_table(prods, TODAY, spec, make_calendar(spec))
    assert table.splitlines() == [
        "One date for all products (each product is capped to its own latest promise date):",
        "If the customer names Wednesday 2026-10-14 for all products: a -> Wednesday 2026-10-14, b -> Wednesday 2026-10-14",
        "If the customer names Thursday 2026-10-15 for all products: a -> Wednesday 2026-10-14, b -> Thursday 2026-10-15",
        "If the customer names Friday 2026-10-16 for all products: a -> Wednesday 2026-10-14, b -> Friday 2026-10-16",
    ]


def test_new_spec_keys_survive_in_the_snippet_subset() -> None:
    from builder.snippets import spec_subset

    spec = pct_spec("half_even", 20)
    spec["amounts"]["floor_percent"] = {"car_loan": {"percent": 5, "of_field": "bal"}}
    sub = spec_subset(spec)["amounts"]
    assert sub["floor_default_percent"] == {"percent": 20, "of_field": "bal"}
    assert sub["rounding_mode"] == "half_even" and sub["floor_percent"]["car_loan"]["percent"] == 5
