"""Engine tests: exact cases E1-E41 (public test set + brief arithmetic) and synthetic S1-S6."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from typing import Any

import pytest

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
