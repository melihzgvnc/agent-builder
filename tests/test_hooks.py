"""Hook and code-node source generators: executed with an import-free harness."""

from __future__ import annotations

import copy
import random
from datetime import date, timedelta
from typing import Any, Callable

import pytest

import builder.engine as engine
from builder import hooks
from tests.hook_harness import LAYOUTS, run_hook
from tests.test_engine import IO, NOW, PUB_PRODUCTS, SPEC_A, SPEC_AB, TODAY

SPECS: list[tuple[str, dict[str, Any]]] = [("A", SPEC_A), ("AB", SPEC_AB)]
FRIDAY_NOW = "2026-10-16T15:00"
FINALIZE = "transition_to_finalize"
GOODBYE = "Thank you for your time. Goodbye."


# ---------------------------------------------------------------------------------------------
# cfg builders (what the compiler will do from a spec; written here independently)
# ---------------------------------------------------------------------------------------------


def verify_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    ident = spec["identity"]
    return {
        "resp": {"ok": "ok", "verified": "verified", "all_verified": "all_verified",
                 "hint": "hint", "wrong_left": "wrong_answers_left"},
        "max_wrong": ident["max_wrong_total"],
        "questions": len(ident["questions"]),
        "hints": ident.get("hints", []),
    }


def callback_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    w = spec["third_party"]["callback_window"]
    return {
        "fields": {"date": "date", "time": "time"},
        "hour_start": w["hour_start"], "hour_end": w["hour_end"],
        "inclusive": w.get("inclusive"), "must_be_after_now": w.get("must_be_after_now"),
        "gaps": bool(engine.callback_window_gaps(spec)),
        "ok_key": "ok",
        "reasks": spec["third_party"].get("reasks_when_outside"),
        "exit_scheduled": "transition_to_exit_cb", "exit_no_time": "transition_to_exit_none",
    }


def promise_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    mp = spec["multi_product"]
    pr = spec["promise_rules"]
    return {
        "fields": {"product": "product_id", "amount": "amount", "date": "date"},
        "n_products": mp["max_products"] if mp["enabled"] else 1,
        "multi": mp["enabled"], "decimals": spec["amounts"].get("rounding_decimals", 2),
        "ok_key": "ok", "retries": pr.get("retries_on_failure"),
        "note_reason": (mp.get("on_promise_failure") or {}).get("note_reason"),
        "success_sentence": pr.get("success_sentence"),
        "goodbye": spec["closing"]["goodbye_before_close"]["text"],
        "finalize_transition": FINALIZE,
    }


def note_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    cfg = promise_cfg(spec)
    mp = spec["multi_product"]
    cfg.update({
        "fields": {"product": "product_id", "reason": "reason"},
        "allowed_reasons": ["refused", "hardship", "dispute", "paid", "service_error"],
        "fallback_reason": cfg["note_reason"],
        "reasons": (mp.get("notes") or {}).get("reasons", []),
    })
    return cfg


def transfer_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    cfg = promise_cfg(spec)
    mp = spec["multi_product"]
    cfg.update({
        "fields": {"queue": "queue"},
        "queue_reasons": [["restructuring", "restructuring"], ["support", "support"], ["general", "human"]],
        "no_notes": bool((mp.get("on_transfer") or {}).get("add_no_notes")),
    })
    return cfg


def final_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    rows = spec["closing"]["outcome_table"]
    with_reason = {r["outcome"] for r in rows if r.get("reason")}
    mp = spec["multi_product"]
    return {
        "fields": {"outcome": "outcome", "reason": "reason"},
        "aggregation": spec["closing"].get("aggregation") or {},
        "n_products": mp["max_products"] if mp["enabled"] else 1,
        "fallback_pre": {"outcome": "not_verified", "reason": "declined"},
        "fallback_post": {"outcome": "no_promise", "reason": "refused"},
        "reasonless": sorted({r["outcome"] for r in rows} - with_reason),
    }


def all_sources(spec: dict[str, Any]) -> dict[str, str]:
    return {
        "verify_post": hooks.verify_post_hook(verify_cfg(spec)),
        "callback_pre": hooks.callback_pre_hook(callback_cfg(spec)),
        "callback_post": hooks.callback_post_hook(callback_cfg(spec)),
        "promise_pre": hooks.promise_pre_hook(promise_cfg(spec)),
        "promise_post": hooks.promise_post_hook(promise_cfg(spec)),
        "note_pre": hooks.note_pre_hook(note_cfg(spec)),
        "note_post": hooks.note_post_hook(note_cfg(spec)),
        "transfer_pre": hooks.transfer_pre_hook(transfer_cfg(spec)),
        "transfer_post": hooks.transfer_post_hook(transfer_cfg(spec)),
        "close_pre": hooks.close_pre_hook(final_cfg(spec)),
        "finalize": hooks.finalize_source(final_cfg(spec), force=False),
        "finalize_forced": hooks.finalize_source(final_cfg(spec), force=True),
        "exit": hooks.exit_source("not_reached", "callback"),
        "exit_no_reason": hooks.exit_source("paid", None),
        "exit_gated": hooks.exit_source("not_reached", "callback", require=("callback_state", "scheduled")),
    }


def facts_vars(products: list[dict[str, Any]], spec: dict[str, Any], now: str = NOW) -> dict[str, Any]:
    """Facts-node outputs for the products plus the call variables, as the hooks see them."""
    base: dict[str, Any] = {"now": now}
    if products:
        base[IO["products_var"]] = copy.deepcopy(products)
    out = dict(base)
    out.update(engine.compute_facts(base, spec, IO))
    return out


def run(source: str, vars_in: dict[str, Any], config: Any = None, result: Any = None,
        layout: str = "single") -> tuple[dict[str, Any], Any, Any]:
    return run_hook(source, vars_in, config, result, layout=layout)


# ---------------------------------------------------------------------------------------------
# literal, check_source, generated source rules
# ---------------------------------------------------------------------------------------------


def test_literal_nested_dicts_have_no_doubled_brace() -> None:
    obj = {"a": {"b": {"c": [1, 2.5, None, True, "x"]}}, "d": {}, "e": [{"f": {"g": 1}}], "h": "}}{{ x {{{ }}}"}
    text = hooks.literal(obj)
    assert "{{" not in text and "}}" not in text
    assert eval(text) == obj  # noqa: S307 - literal() output of plain data


def test_literal_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        hooks.literal({1, 2})


def test_check_source_rejects_import_brace_and_syntax() -> None:
    assert hooks.check_source("x = 1\n") == "x = 1\n"
    for bad in ("import os\n", "from os import path\n", "x = {{1}}\n", "def (:\n"):
        with pytest.raises(ValueError):
            hooks.check_source(bad)


def test_harness_refuses_imports() -> None:
    with pytest.raises(ImportError):
        run_hook("import json\n", {})


@pytest.mark.parametrize(("label", "spec"), SPECS, ids=[x for x, _ in SPECS])
def test_every_source_is_clean_and_runs(label: str, spec: dict[str, Any]) -> None:
    for name, src in all_sources(spec).items():
        hooks.check_source(src)
        assert "{{" not in src and "}}" not in src, name
        for layout in LAYOUTS:
            v = facts_vars(PUB_PRODUCTS["PUB-07"], spec)
            run(src, v, config={"body": {}}, result={"data": {}, "header": {"a": 1}}, layout=layout)


def test_text_with_doubled_braces_is_escaped() -> None:
    cfg = promise_cfg(SPEC_A)
    cfg["success_sentence"] = "Thanks {{name}} }}"
    src = hooks.promise_post_hook(cfg)
    assert "{{" not in src and "}}" not in src
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v.update(promise_idx=1, promise_sent_date="2026-10-15", promise_sent_amount=540.0, promise_changed="no")
    _, _, res = run(src, v, result={"data": {"ok": True}})
    assert "Thanks {{name}} }}" in res["data"]["what_to_do_next"]


def test_hook_source_exception_safety() -> None:
    """Garbage inputs never raise out of a hook."""
    for name, src in all_sources(SPEC_AB).items():
        for v, cfg, res in (({}, {}, {}), ({}, {"body": 5}, {"data": 5}), ({"p1_id": ["x"]}, {"body": None}, None)):
            run(src, dict(v), config=copy.deepcopy(cfg), result=copy.deepcopy(res))


# ---------------------------------------------------------------------------------------------
# verify_post_hook
# ---------------------------------------------------------------------------------------------

VERIFY = hooks.verify_post_hook(verify_cfg(SPEC_AB))


def verify(vars_in: dict[str, Any], data: dict[str, Any], layout: str = "single") -> tuple[dict[str, Any], dict[str, Any]]:
    v, _, res = run(VERIFY, dict(vars_in), result={"data": dict(data), "header": {"x": 1}}, layout=layout)
    assert "header" not in res
    return v, res["data"]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("all_v", [True, "true", "True"])
def test_verify_all_verified_forms(all_v: object, layout: str) -> None:
    v, data = verify({}, {"ok": True, "verified": True, "all_verified": all_v, "wrong_answers_left": 3}, layout)
    assert v["identity_state"] == "verified" and v["identity_wrong"] == 0
    assert "what_to_do_next" in data


def test_verify_one_question_at_a_time_then_verified() -> None:
    v, data = verify({}, {"ok": True, "verified": True, "all_verified": False})
    assert v["identity_state"] == "pending" and v["identity_verified_count"] == 1
    assert "next security question" in data["what_to_do_next"]
    v, _ = verify(v, {"ok": True, "verified": True, "all_verified": True})
    assert v["identity_state"] == "verified"


def test_verify_count_fallback_without_all_verified() -> None:
    v, _ = verify({}, {"ok": True, "verified": True})
    assert v["identity_state"] == "pending"
    v, _ = verify(v, {"ok": True, "verified": True})
    assert v["identity_state"] == "verified"  # two questions, two verified answers


def test_verify_not_ok_is_not_counted() -> None:
    v, data = verify({}, {"ok": False, "error": "unreadable"})
    assert v["identity_wrong"] == 0 and v["identity_state"] == "pending"
    assert "not counted" in data["what_to_do_next"]


def test_verify_hint_not_counted_and_unknown_hint_counted() -> None:
    v, data = verify({}, {"ok": True, "verified": False, "all_verified": False, "hint": "day_month_swapped",
                          "wrong_answers_left": 3})
    assert v["identity_wrong"] == 0
    assert data["what_to_do_next"] == "Ask the customer to say the month as a word."
    v, _ = verify({}, {"ok": True, "verified": False, "all_verified": False, "hint": "other", "wrong_answers_left": 2})
    assert v["identity_wrong"] == 1
    cfg = verify_cfg(SPEC_AB)
    cfg["hints"] = [dict(cfg["hints"][0], counts_as_wrong=True)]
    v, _, res = run(hooks.verify_post_hook(cfg), {}, result={"data": {
        "ok": True, "verified": False, "all_verified": False, "hint": "day_month_swapped"}})
    assert v["identity_wrong"] == 1
    assert "month as a word" in res["data"]["what_to_do_next"]


def test_verify_third_wrong_fails_and_never_downgrades() -> None:
    v: dict[str, Any] = {}
    for i in range(3):
        v, data = verify(v, {"ok": True, "verified": False, "all_verified": False})
        assert v["identity_wrong"] == i + 1
    assert v["identity_state"] == "failed"
    assert "Verification failed" in data["what_to_do_next"]
    vv, _ = verify({"identity_state": "verified", "identity_wrong": 5}, {"ok": True, "verified": False, "all_verified": False})
    assert vv["identity_state"] == "verified"


def test_verify_wrong_answers_left_zero_fails() -> None:
    v, _ = verify({}, {"ok": True, "verified": False, "all_verified": False, "wrong_answers_left": 0})
    assert v["identity_state"] == "failed"
    # Not ok: the field is ignored.
    v, _ = verify({}, {"ok": False, "wrong_answers_left": 0})
    assert v["identity_state"] == "pending"


def test_verify_missing_features_are_off() -> None:
    cfg = {"resp": {"ok": "ok", "verified": "verified"}, "max_wrong": None, "questions": 1}
    src = hooks.verify_post_hook(cfg)
    v, _, _ = run(src, {}, result={"data": {"ok": True, "verified": True}})
    assert v["identity_state"] == "verified"
    v, _, _ = run(src, {}, result={"data": {"ok": True, "verified": False}})
    assert v["identity_wrong"] == 1 and v["identity_state"] == "pending"


# ---------------------------------------------------------------------------------------------
# callback hooks, agreement with engine.check_callback
# ---------------------------------------------------------------------------------------------

TIMES = ["00:00", "08:59", "09:59", "10:00", "10:01", "10:30", "11:00", "11:30", "12:00", "15:00", "15:01",
         "18:59", "19:00", "19:01", "19:30", "20:00", "23:59", "25:00", "9:30", "abc", "", "10:00:00", "19:00:30",
         "11:00:30", "10:30:15"]


def window_variants() -> list[tuple[str, dict[str, Any]]]:
    out = [("A", SPEC_A)]
    for incl in (True, False):
        for after in (True, False):
            s = copy.deepcopy(SPEC_A)
            w = s["third_party"]["callback_window"]
            w["inclusive"], w["must_be_after_now"] = incl, after
            out.append((f"incl{incl}-after{after}", s))
    wide = copy.deepcopy(SPEC_A)
    wide["third_party"]["callback_window"]["days"]["n"] = 3
    out.append(("n3", wide))
    return out


@pytest.mark.parametrize(("label", "spec"), window_variants(), ids=[x for x, _ in window_variants()])
@pytest.mark.parametrize("now", [NOW, FRIDAY_NOW, "2026-10-14T10:30", "2026-10-14T20:00"])
def test_callback_pre_agrees_with_engine(label: str, spec: dict[str, Any], now: str) -> None:
    src = hooks.callback_pre_hook(callback_cfg(spec))
    cal = engine.make_calendar(spec)
    now_dt = engine.parse_now(now)
    assert now_dt is not None
    base = facts_vars([], spec, now)
    assert base["callback_dates"] != ""
    for k in range(-1, 6):
        d = (now_dt.date() + timedelta(days=k)).isoformat()
        for t in TIMES:
            body = {"customer_id": "c1", "date": d, "time": t}
            v, cfg, _ = run(src, dict(base), config={"body": dict(body)})
            proposed = engine.parse_now(d + "T" + t) if t.count(":") >= 1 and len(t.split(":")[0]) == 2 else None
            expected_ok = proposed is not None and engine.check_callback(proposed, now_dt, spec, cal)["status"] == "ok"
            blanked = cfg["body"]["date"] == "" and cfg["body"]["time"] == ""
            assert blanked == (not expected_ok), (d, t, now)
            if expected_ok:
                assert cfg["body"] == body and v["callback_state"] == ""
            else:
                assert v["callback_state"] == "invalid" and cfg["body"]["customer_id"] == "c1"


def test_callback_pre_counts_invalid_and_empty_dates_blank() -> None:
    src = hooks.callback_pre_hook(callback_cfg(SPEC_A))
    v = facts_vars([], SPEC_A)
    v, cfg, _ = run(src, v, config={"body": {"date": "2026-10-20", "time": "11:00"}})
    v, cfg, _ = run(src, v, config={"body": {"date": "2026-10-20", "time": "11:00"}})
    assert v["callback_invalid_count"] == 2
    # now unknown: callback_dates is empty, a defined window -> blank
    unknown = facts_vars([], SPEC_A, now="garbage")
    assert unknown["callback_dates"] == ""
    _, cfg, _ = run(src, dict(unknown), config={"body": {"date": "2026-10-15", "time": "11:00"}})
    assert cfg["body"] == {"date": "", "time": ""}


def test_callback_pre_window_gaps_leave_request_unchanged() -> None:
    spec = copy.deepcopy(SPEC_A)
    del spec["third_party"]["callback_window"]["inclusive"]
    cfg = callback_cfg(spec)
    cfg["inclusive"] = None
    src = hooks.callback_pre_hook(cfg)
    body = {"date": "2030-01-01", "time": "03:00"}
    v, out, _ = run(src, facts_vars([], spec), config={"body": dict(body)})
    assert out["body"] == body
    assert v["callback_guard"] == "skipped"


def test_callback_post_flow() -> None:
    cfg = callback_cfg(SPEC_A)
    pre = hooks.callback_pre_hook(cfg)
    post = hooks.callback_post_hook(cfg)
    v = facts_vars([], SPEC_A)
    # valid -> scheduled
    v, _, _ = run(pre, v, config={"body": {"date": "2026-10-15", "time": "11:00"}})
    v, _, res = run(post, v, result={"data": {"ok": True}, "header": {}})
    assert v["callback_state"] == "scheduled"
    assert "2026-10-15 at 11:00" in res["data"]["what_to_do_next"] and "transition_to_exit_cb" in res["data"]["what_to_do_next"]
    assert "header" not in res
    # invalid once -> re-ask, forced not ok
    v = facts_vars([], SPEC_A)
    v, _, _ = run(pre, v, config={"body": {"date": "2026-10-20", "time": "11:00"}})
    v, _, res = run(post, v, result={"data": {"ok": True}})
    assert res["data"]["ok"] is False and v["callback_state"] == "invalid"
    assert "outside the callback window" in res["data"]["what_to_do_next"]
    assert "2026-10-14,2026-10-15" in res["data"]["what_to_do_next"]
    # invalid again (reasks = 1) -> no valid time
    v, _, _ = run(pre, v, config={"body": {"date": "2026-10-20", "time": "11:00"}})
    v, _, res = run(post, v, result={"data": {"ok": False}})
    assert v["callback_state"] == "no_valid_time" and res["data"]["ok"] is False
    assert "transition_to_exit_none" in res["data"]["what_to_do_next"]
    # a valid call that the API rejects
    v = facts_vars([], SPEC_A)
    v, _, _ = run(pre, v, config={"body": {"date": "2026-10-15", "time": "11:00"}})
    v, _, res = run(post, v, result={"data": {"ok": False}})
    assert v["callback_state"] == "" and "not recorded" in res["data"]["what_to_do_next"]


def test_callback_post_without_reask_policy_never_moves_on() -> None:
    cfg = callback_cfg(SPEC_A)
    cfg["reasks"] = None
    post = hooks.callback_post_hook(cfg)
    v, _, res = run(post, {"callback_state": "invalid", "callback_invalid_count": 9}, result={"data": {"ok": True}})
    assert v["callback_state"] == "invalid" and res["data"]["ok"] is False


# ---------------------------------------------------------------------------------------------
# promise_pre_hook: date clamp agrees with engine.check_promise_date; amount clamp with classify
# ---------------------------------------------------------------------------------------------

PROMISE_PRE_A = hooks.promise_pre_hook(promise_cfg(SPEC_A))
PROMISE_PRE_AB = hooks.promise_pre_hook(promise_cfg(SPEC_AB))


def promise_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"customer_id": "c1", "product_id": "model-id", "amount": 100.0, "date": "2026-10-15"}
    body.update(kw)
    return body


def assert_rejected(v: dict[str, Any], body: dict[str, Any], *fields: str) -> None:
    assert v["promise_guard"] == "rejected" and v["promise_changed"] == "rejected"
    assert body["product_id"] == ""
    for name in fields:
        assert body[name] in ("", 0)


@pytest.mark.parametrize(("case", "spec"), [("PUB-07", SPEC_A), ("PUB-04", SPEC_A), ("PUB-13", SPEC_AB)])
def test_promise_date_is_kept_or_rejected_like_engine(case: str, spec: dict[str, Any]) -> None:
    src = hooks.promise_pre_hook(promise_cfg(spec))
    cal = engine.make_calendar(spec)
    base = facts_vars(PUB_PRODUCTS[case], spec)
    for idx in range(1, len(PUB_PRODUCTS[case]) + 1):
        deadline = date.fromisoformat(base[f"p{idx}_deadline"])
        d = TODAY - timedelta(days=4)
        while d <= TODAY + timedelta(days=30):
            body = promise_body(product_id=base[f"p{idx}_id"], date=d.isoformat(), amount=base[f"p{idx}_full"])
            v, cfg, _ = run(src, dict(base), config={"body": body})
            status = engine.check_promise_date(d, TODAY, deadline, cal)["status"]
            if status == "ok":
                assert cfg["body"]["date"] == d.isoformat() and v["promise_guard"] == ""
                assert cfg["body"]["product_id"] == base[f"p{idx}_id"]
            else:
                assert_rejected(v, cfg["body"], "date")
                assert v["promise_limit_date"] <= deadline.isoformat()
                assert v["promise_limit_date"] in base["business_days"].split(",")
            d += timedelta(days=1)


@pytest.mark.parametrize("bad", ["garbage", "", "2026-13-45x", "15/10/2026", None, 5])
def test_promise_invalid_date_is_rejected(bad: object) -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, cfg, _ = run(PROMISE_PRE_A, v, config={"body": promise_body(date=bad, amount=v["p1_full"])})
    assert_rejected(v, cfg["body"], "date")
    assert v["promise_reject"] == "date_invalid"


AMOUNT_CASES: list[tuple[object, str]] = [
    (2125.1, "full"), (3000, "above_full"), (637.5, "in_range"), (1000, "in_range"), (2125.0, "in_range"),
    (500, "below_floor"), (0, "invalid"), (-5, "invalid"), ("abc", "invalid"), ("1000", "in_range"),
    (None, "invalid"), (637.49, "below_floor"), (2125.11, "above_full"),
]


@pytest.mark.parametrize(("amount", "kind"), AMOUNT_CASES)
def test_promise_amount_is_kept_or_rejected_like_classify(amount: object, kind: str) -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-06"], SPEC_A)
    full, floor = v["p1_full"], v["p1_floor"]
    assert (full, floor) == (2125.1, 637.5)
    assert engine.classify_amount(amount, full, floor) == kind
    v, cfg, _ = run(PROMISE_PRE_A, v, config={"body": promise_body(amount=amount, date="2026-10-19")})
    if kind in ("full", "in_range"):
        assert cfg["body"]["amount"] == amount and v["promise_guard"] == ""
    else:
        assert_rejected(v, cfg["body"], "amount")
        assert cfg["body"]["date"] == "2026-10-19"
        limit = {"above_full": full, "below_floor": floor}.get(kind, "")
        assert v["promise_limit_amount"] == limit


def test_promise_unknown_facts_pass_through_unchanged() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-06"], SPEC_A)
    for name in ("p1_floor", "p1_full", "p1_deadline", "business_days"):
        v[name] = ""
    body = promise_body(amount=100.0, date="2026-10-17")
    v2, cfg, _ = run(PROMISE_PRE_A, dict(v), config={"body": dict(body, product_id="p1")})
    assert cfg["body"] == dict(body, product_id="p1")
    assert v2["promise_guard"] == "" and v2["promise_unchecked"] == "date,amount"
    assert "unknown" not in str(cfg["body"])
    garbage = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A, now="garbage")
    assert garbage["business_days"] == "" and garbage["p1_deadline"] == ""
    _, cfg, _ = run(PROMISE_PRE_A, garbage, config={"body": promise_body(date="2026-10-17")})
    assert cfg["body"]["date"] == "2026-10-17"


def test_promise_body_is_asked_or_blank_property() -> None:
    rng = random.Random(7)
    for _ in range(300):
        v = facts_vars(PUB_PRODUCTS["PUB-06"], SPEC_A)
        for name in rng.sample(["p1_floor", "p1_full", "p1_deadline", "business_days", "today"], rng.randint(0, 3)):
            v[name] = ""
        asked_date = (TODAY + timedelta(days=rng.randint(-5, 25))).isoformat()
        asked_amt = rng.choice([0, -1, 100.0, 637.5, 900.25, 2125.1, 4000.0, "x"])
        body = promise_body(date=asked_date, amount=asked_amt, product_id="p1")
        v, cfg, _ = run(PROMISE_PRE_A, v, config={"body": dict(body)})
        out = cfg["body"]
        assert "unknown" not in str(out)
        if v["promise_guard"] == "rejected":
            assert out["product_id"] == "" and out["date"] in ("", asked_date) and out["amount"] in (0, asked_amt)
        else:
            assert out == body


def test_promise_rounding_decimals_honoured_in_limit_text() -> None:
    spec = copy.deepcopy(SPEC_A)
    spec["amounts"]["rounding_decimals"] = 0
    cfg = promise_cfg(spec)
    v = facts_vars(PUB_PRODUCTS["PUB-06"], spec)
    v, _, _ = run(hooks.promise_pre_hook(cfg), v, config={"body": promise_body(amount=10.0, date="2026-10-19")})
    v, _, res = run(hooks.promise_post_hook(cfg), v, result={"data": {"ok": True}})
    assert "at least" not in res["data"]["what_to_do_next"] and v["p1_promise_rejects"] == 1
    assert f"{round(v['promise_limit_amount']):d}" in res["data"]["what_to_do_next"]


def test_promise_multi_product_id_forcing() -> None:
    base = facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB)
    BIG = base["p1_full"]
    # the model names a real product: kept
    v, cfg, _ = run(PROMISE_PRE_AB, dict(base), config={"body": promise_body(product_id="p2", amount=base["p2_full"])})
    assert cfg["body"]["product_id"] == "p2" and v["promise_idx"] == 2
    # unknown id: first product still pending
    v, cfg, _ = run(PROMISE_PRE_AB, dict(base), config={"body": promise_body(product_id="zzz", amount=BIG)})
    assert cfg["body"]["product_id"] == "p1" and v["promise_idx"] == 1
    # p1 recorded, p2 noted: p3
    pending = dict(base, p1_promise="recorded", p2_note="refused")
    v, cfg, _ = run(PROMISE_PRE_AB, pending, config={"body": promise_body(product_id="zzz", amount=base["p3_full"])})
    assert cfg["body"]["product_id"] == "p3" and v["promise_idx"] == 3
    # everything handled: nothing to choose, request unchanged
    done = dict(base, p1_promise="recorded", p2_note="refused", p3_promise="recorded")
    v, cfg, _ = run(PROMISE_PRE_AB, done, config={"body": promise_body(product_id="zzz")})
    assert cfg["body"]["product_id"] == "zzz" and v["promise_guard"] == "skipped"
    # single product: always product 1
    single = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, cfg, _ = run(PROMISE_PRE_A, single, config={"body": promise_body(product_id="anything", amount=single["p1_full"])})
    assert cfg["body"]["product_id"] == "p1"


def test_promise_changed_flag() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, cfg, _ = run(PROMISE_PRE_A, v, config={"body": promise_body(amount=540.0, date="2026-10-15")})
    assert v["promise_changed"] == "no" and v["promise_reject"] == ""
    assert (v["promise_asked_date"], v["promise_asked_amount"]) == ("2026-10-15", 540.0)
    v, cfg, _ = run(PROMISE_PRE_A, v, config={"body": promise_body(amount=540.0, date="2026-10-17")})
    assert v["promise_changed"] == "rejected" and cfg["body"]["date"] == ""
    assert v["promise_reject"] == "date_not_business_day" and v["promise_limit_date"] == "2026-10-19"


def test_promise_multi_product_per_product_limits() -> None:
    base = facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB)
    late = max(base[f"p{i}_deadline"] for i in (1, 2, 3))
    early = min(base[f"p{i}_deadline"] for i in (1, 2, 3))
    k = [i for i in (1, 2, 3) if base[f"p{i}_deadline"] == early][0]
    if early != late:
        body = promise_body(product_id=f"p{k}", date=late, amount=base[f"p{k}_full"])
        v, cfg, _ = run(PROMISE_PRE_AB, dict(base), config={"body": body})
        assert v["promise_idx"] == k and v["promise_limit_date"] <= early


# ---------------------------------------------------------------------------------------------
# promise_post_hook / note hooks / transfer hooks
# ---------------------------------------------------------------------------------------------

PROMISE_POST_A = hooks.promise_post_hook(promise_cfg(SPEC_A))
PROMISE_POST_AB = hooks.promise_post_hook(promise_cfg(SPEC_AB))
NOTE_PRE = hooks.note_pre_hook(note_cfg(SPEC_AB))
NOTE_POST = hooks.note_post_hook(note_cfg(SPEC_AB))


def promise_round(src_pre: str, src_post: str, v: dict[str, Any], body: dict[str, Any], ok: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    v, _, _ = run(src_pre, v, config={"body": body})
    v, _, res = run(src_post, v, result={"data": {"ok": ok}, "header": {}})
    assert "header" not in res
    return v, res["data"]


def test_promise_post_single_recorded() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=540.0, date="2026-10-15"), True)
    assert v["p1_promise"] == "recorded" and v["closing_ready"] == "yes"
    assert (v["p1_promise_date"], v["p1_promise_amount"]) == ("2026-10-15", 540.0)
    text = data["what_to_do_next"]
    assert "540.00 on 2026-10-15" in text
    assert "You will receive a text message confirming this promise." in text
    assert FINALIZE in text and GOODBYE in text
    assert "differ" not in text


def test_promise_post_rejected_asks_for_new_readback_and_keeps_retries() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-06"], SPEC_A)
    for n in (1, 2):
        v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=100.0, date="2026-10-19"), True)
        assert data["ok"] is False and v["p1_promise_rejects"] == n
        assert "637.50" in data["what_to_do_next"] and "read-back" in data["what_to_do_next"]
        assert v.get("p1_promise", "") == "" and v.get("p1_promise_fails", 0) == 0
    v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=100.0, date="2026-10-19"), True)
    assert v["promise_state"] == "write_failed" and "read-back" not in data["what_to_do_next"]
    # a confirmed in-range value afterwards is recorded exactly as asked
    v = facts_vars(PUB_PRODUCTS["PUB-06"], SPEC_A)
    v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=700.0, date="2026-10-19"), True)
    assert (v["p1_promise_date"], v["p1_promise_amount"]) == ("2026-10-19", 700.0)
    assert "differ" not in data["what_to_do_next"]


def test_promise_post_failure_paths_single() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=540.0), False)
    assert v["p1_promise_fails"] == 1 and v.get("promise_state") is None
    assert "same values" in data["what_to_do_next"]
    assert v.get("p1_promise", "") == ""
    v, data = promise_round(PROMISE_PRE_A, PROMISE_POST_A, v, promise_body(amount=540.0), False)
    assert v["promise_state"] == "write_failed" and v["p1_promise_fails"] == 2
    assert "moves on by itself" in data["what_to_do_next"]
    assert v.get("closing_ready", "") != "yes"


def test_promise_post_multi_flow_with_failure_and_note() -> None:
    base = facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB)
    v, data = promise_round(PROMISE_PRE_AB, PROMISE_POST_AB, dict(base), promise_body(product_id="p1", amount=510.0), True)
    assert v["p1_promise"] == "recorded" and v.get("closing_ready", "") == ""
    assert base["p2_name"] in data["what_to_do_next"] and "(id p2)" in data["what_to_do_next"]
    assert FINALIZE not in data["what_to_do_next"]
    # product 2 fails twice -> failed + note reason
    body = promise_body(product_id="p2", amount=1320.0)
    v, data = promise_round(PROMISE_PRE_AB, PROMISE_POST_AB, v, body, False)
    assert "same values" in data["what_to_do_next"]
    v, data = promise_round(PROMISE_PRE_AB, PROMISE_POST_AB, v, body, False)
    assert v["p2_promise"] == "failed" and "service_error" in data["what_to_do_next"]
    assert v.get("promise_state") is None
    # the note tool takes the forced reason for the failed product; bad reason replaced
    v, cfg, _ = run(NOTE_PRE, v, config={"body": {"customer_id": "c1", "product_id": "p2", "reason": "bogus"}})
    assert cfg["body"]["reason"] == "service_error" and v["note_sent_reason"] == "service_error"
    v, _, res = run(NOTE_POST, v, result={"data": {"ok": True}, "header": {}})
    assert v["p2_note"] == "service_error" and v.get("closing_ready", "") == ""
    assert base["p3_name"] in res["data"]["what_to_do_next"]
    # product 3 gets a note: everything handled
    v, cfg, _ = run(NOTE_PRE, v, config={"body": {"customer_id": "c1", "product_id": "zzz", "reason": "refused"}})
    assert cfg["body"] == {"customer_id": "c1", "product_id": "p3", "reason": "refused"}
    v, _, res = run(NOTE_POST, v, result={"data": {"ok": True}})
    assert v["p3_note"] == "refused" and v["closing_ready"] == "yes"
    assert FINALIZE in res["data"]["what_to_do_next"] and GOODBYE in res["data"]["what_to_do_next"]


def test_note_pre_reason_fallbacks_and_blocked_flag() -> None:
    base = facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB)
    v, cfg, _ = run(NOTE_PRE, dict(base, notes_blocked="yes"), config={"body": {"product_id": "p1", "reason": "refused"}})
    assert v["note_blocked_hit"] == "yes" and cfg["body"]["reason"] == "refused"
    no_fallback = note_cfg(SPEC_AB)
    no_fallback["fallback_reason"] = None
    _, cfg, _ = run(hooks.note_pre_hook(no_fallback), dict(base), config={"body": {"product_id": "p1", "reason": "bogus"}})
    assert cfg["body"]["reason"] == "refused"  # first of multi_product.notes.reasons


def test_note_post_not_ok_retries_once_then_counts() -> None:
    v = dict(facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB), note_idx=1, note_sent_reason="dispute")
    v, _, res = run(NOTE_POST, v, result={"data": {"ok": False}})
    assert v.get("p1_note", "") == "" and "once more" in res["data"]["what_to_do_next"]
    v, _, res = run(NOTE_POST, v, result={"data": {"ok": False}})
    assert v["p1_note"] == "dispute" and v["p1_note_fails"] == 2


def test_transfer_hooks() -> None:
    cfg = transfer_cfg(SPEC_AB)
    pre = hooks.transfer_pre_hook(cfg)
    post = hooks.transfer_post_hook(cfg)
    v, out, _ = run(pre, {}, config={"body": {"customer_id": "c1", "queue": "general"}})
    assert out["body"] == {"customer_id": "c1", "queue": "general"} and v["transfer_sent_queue"] == "general"
    v, _, res = run(post, v, result={"data": {"ok": True}, "header": {}})
    assert v["transfer_state"] == "done" and v["transfer_reason"] == "human"
    assert v["closing_ready"] == "yes" and v["notes_blocked"] == "yes"
    assert FINALIZE in res["data"]["what_to_do_next"] and "connected" in res["data"]["what_to_do_next"]
    # failure: nothing is set
    v, _, res = run(post, {"transfer_sent_queue": "support"}, result={"data": {"ok": False}})
    assert "transfer_state" not in v and "closing_ready" not in v
    # single product spec: no notes_blocked; unmapped queue uses the queue name
    single = transfer_cfg(SPEC_A)
    single["queue_reasons"] = []
    v, _, _ = run(hooks.transfer_post_hook(single), {"transfer_sent_queue": "x"}, result={"data": {"ok": True}})
    assert v["transfer_reason"] == "x" and "notes_blocked" not in v


# ---------------------------------------------------------------------------------------------
# close_pre_hook / finalize_source / exit_source
# ---------------------------------------------------------------------------------------------

CLOSE_AB = hooks.close_pre_hook(final_cfg(SPEC_AB))
CLOSE_A = hooks.close_pre_hook(final_cfg(SPEC_A))


def multi_vars(promises: dict[int, str] | None = None, notes: dict[int, str] | None = None, **extra: Any) -> dict[str, Any]:
    v = facts_vars(PUB_PRODUCTS["PUB-12"], SPEC_AB)
    v["identity_state"] = "verified"
    for k, val in (promises or {}).items():
        v[f"p{k}_promise"] = val
    for k, val in (notes or {}).items():
        v[f"p{k}_note"] = val
    v.update(extra)
    return v


def close_body(src: str, v: dict[str, Any], layout: str = "single") -> dict[str, Any]:
    _, cfg, _ = run(src, v, config={"body": {"customer_id": "c1", "outcome": "{{final_outcome}}", "reason": "{{final_reason}}"}},
                    layout=layout)
    return cfg["body"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_close_preset_outcome(layout: str) -> None:
    body = close_body(CLOSE_AB, {"final_outcome": "not_reached", "final_reason": "callback"}, layout)
    assert body == {"customer_id": "c1", "outcome": "not_reached", "reason": "callback"}
    # preset outcome without a reason: the reason key is removed
    body = close_body(CLOSE_AB, {"final_outcome": "paid", "final_reason": ""})
    assert body == {"customer_id": "c1", "outcome": "paid"}


@pytest.mark.parametrize(
    ("promises", "notes", "extra", "outcome", "reason"),
    [
        ({1: "recorded", 2: "recorded"}, {3: "refused"}, {}, "promise", None),
        ({}, {1: "paid", 2: "paid", 3: "paid"}, {}, "paid", None),
        ({}, {1: "refused", 2: "dispute", 3: "refused"}, {}, "no_promise", "refused"),
        ({}, {1: "paid", 2: "dispute", 3: "refused"}, {}, "no_promise", "dispute"),
        ({1: "recorded"}, {}, {"transfer_reason": "restructuring"}, "transferred", "restructuring"),
        ({}, {}, {"transfer_reason": "human"}, "transferred", "human"),
    ],
)
def test_close_aggregation(promises: dict[int, str], notes: dict[int, str], extra: dict[str, Any],
                           outcome: str, reason: str | None) -> None:
    body = close_body(CLOSE_AB, multi_vars(promises, notes, **extra))
    assert body["outcome"] == outcome
    assert body.get("reason") == reason


def test_close_single_product_and_fallbacks() -> None:
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v["identity_state"] = "verified"
    assert close_body(CLOSE_A, dict(v, p1_promise="recorded"))["outcome"] == "promise"
    assert close_body(CLOSE_A, dict(v, p1_promise="recorded", transfer_reason="support")) == {
        "customer_id": "c1", "outcome": "transferred", "reason": "support"}
    # nothing recorded, no default in the spec: post fallback
    assert close_body(CLOSE_A, dict(v)) == {"customer_id": "c1", "outcome": "no_promise", "reason": "refused"}
    # not verified: pre fallback, unless a transfer happened
    assert close_body(CLOSE_A, {"identity_state": "pending"}) == {
        "customer_id": "c1", "outcome": "not_verified", "reason": "declined"}
    assert close_body(CLOSE_A, {}) == {"customer_id": "c1", "outcome": "not_verified", "reason": "declined"}
    assert close_body(CLOSE_A, {"identity_state": "pending", "transfer_reason": "human"})["outcome"] == "transferred"


def test_close_exception_path_writes_fallback() -> None:
    class Exploding(dict):  # type: ignore[type-arg]
        def get(self, key: object, default: object = None) -> object:
            if key == "transfer_reason":
                raise RuntimeError("boom")
            return super().get(key, default)

    for state, expected in (("verified", "no_promise"), ("pending", "not_verified")):
        body = close_body(CLOSE_AB, Exploding(identity_state=state, final_outcome=""))
        assert body["outcome"] == expected and body["reason"] in ("refused", "declined")
    # a hook with no usable fallback leaves the body alone
    cfg = final_cfg(SPEC_AB)
    cfg["fallback_pre"] = cfg["fallback_post"] = None
    body = close_body(hooks.close_pre_hook(cfg), Exploding())
    assert body == {"customer_id": "c1", "outcome": "{{final_outcome}}", "reason": "{{final_reason}}"}


def test_close_aggregation_default_beats_fallback() -> None:
    spec = copy.deepcopy(SPEC_AB)
    spec["closing"]["aggregation"]["default"] = {"outcome": "no_promise", "reason": "hardship"}
    src = hooks.close_pre_hook(final_cfg(spec))
    v = multi_vars()
    assert close_body(src, v) == {"customer_id": "c1", "outcome": "no_promise", "reason": "hardship"}


def test_close_field_names_follow_cfg() -> None:
    cfg = final_cfg(SPEC_AB)
    cfg["fields"] = {"outcome": "result", "reason": "why"}
    _, out, _ = run(hooks.close_pre_hook(cfg), {"final_outcome": "no_promise", "final_reason": "refused"},
                    config={"body": {"result": "x", "why": "y"}})
    assert out["body"] == {"result": "no_promise", "why": "refused"}


FINALIZE_AB = hooks.finalize_source(final_cfg(SPEC_AB), force=False)
FINALIZE_FORCED_AB = hooks.finalize_source(final_cfg(SPEC_AB), force=True)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_finalize_gating(layout: str) -> None:
    v, _, _ = run(FINALIZE_AB, multi_vars({1: "recorded"}), layout=layout)
    assert "final_outcome" not in v and v.get("closing_ready", "") == ""
    v, _, _ = run(FINALIZE_AB, multi_vars({1: "recorded"}, closing_ready="yes"), layout=layout)
    assert (v["final_outcome"], v["final_reason"]) == ("promise", "")
    v, _, _ = run(FINALIZE_FORCED_AB, multi_vars({}, {1: "paid", 2: "paid", 3: "paid"}), layout=layout)
    assert v["closing_ready"] == "yes" and v["final_outcome"] == "paid"
    # a preset outcome is kept
    v, _, _ = run(FINALIZE_FORCED_AB, multi_vars({1: "recorded"}, final_outcome="not_reached", final_reason="callback"))
    assert (v["final_outcome"], v["final_reason"]) == ("not_reached", "callback")


def test_finalize_transfer_and_exception() -> None:
    v, _, _ = run(FINALIZE_AB, multi_vars({1: "recorded"}, closing_ready="yes", transfer_reason="restructuring"))
    assert (v["final_outcome"], v["final_reason"]) == ("transferred", "restructuring")

    class Exploding(dict):  # type: ignore[type-arg]
        def get(self, key: object, default: object = None) -> object:
            if key == "transfer_reason":
                raise RuntimeError("boom")
            return super().get(key, default)

    v, _, _ = run(FINALIZE_AB, Exploding(closing_ready="yes", identity_state="verified"))
    assert v["closing_ready"] == "yes" and v["final_outcome"] == "no_promise"


def test_exit_source() -> None:
    v, _, _ = run(hooks.exit_source("not_reached", "callback"), {})
    assert v == {"final_outcome": "not_reached", "final_reason": "callback"}
    v, _, _ = run(hooks.exit_source("paid", None), {})
    assert v == {"final_outcome": "paid"}
    gated = hooks.exit_source("not_reached", "callback", require=("callback_state", "scheduled"))
    v, _, _ = run(gated, {"callback_state": "invalid"})
    assert "final_outcome" not in v
    v, _, _ = run(gated, {"callback_state": "scheduled"})
    assert v["final_outcome"] == "not_reached"
    tricky = hooks.exit_source("a{{b", "c}}d")
    v, _, _ = run(tricky, {})
    assert v == {"final_outcome": "a{{b", "final_reason": "c}}d"}


# ---------------------------------------------------------------------------------------------
# embedded aggregate_outcome agrees with the engine
# ---------------------------------------------------------------------------------------------


def test_engine_function_source_is_import_free_and_agrees() -> None:
    src = hooks.engine_function_source("aggregate_outcome")
    hooks.check_source(src)
    assert '"""' not in src and "->" not in src
    ns: dict[str, Any] = {}
    exec(compile(src, "<agg>", "exec"), {"__builtins__": {"any": any, "all": all, "isinstance": isinstance,
                                                              "dict": dict, "list": list}}, ns)
    embedded: Callable[..., dict[str, Any]] = ns["aggregate_outcome"]
    rng = random.Random(7)
    reasons = [None, "paid", "refused", "dispute", "hardship", "service_error", ""]
    for spec in (SPEC_A, SPEC_AB, {}):
        for _ in range(400):
            states = [
                {"product_id": f"p{i}", "promise_recorded": rng.random() < 0.3, "note_reason": rng.choice(reasons)}
                for i in range(rng.randint(0, 3))
            ]
            tr = rng.choice([None, "", "restructuring", "human"])
            assert embedded(states, spec, tr) == engine.aggregate_outcome(states, spec, tr)
    with pytest.raises(KeyError):
        hooks.engine_function_source("nope")


def test_close_hook_matches_engine_on_random_states() -> None:
    rng = random.Random(11)
    src = hooks.close_pre_hook(final_cfg(SPEC_AB))
    reasons = ["", "paid", "refused", "dispute", "hardship", "service_error"]
    for _ in range(200):
        v = multi_vars()
        states = []
        for k in (1, 2, 3):
            promise = "recorded" if rng.random() < 0.3 else ""
            note = "" if promise else rng.choice(reasons)
            v[f"p{k}_promise"], v[f"p{k}_note"] = promise, note
            states.append({"product_id": f"p{k}", "promise_recorded": promise == "recorded", "note_reason": note or None})
        tr = rng.choice(["", "restructuring", "support"])
        v["transfer_reason"] = tr
        expected = engine.aggregate_outcome(states, SPEC_AB, tr or None)
        body = close_body(src, v)
        if expected["outcome"]:
            assert (body["outcome"], body.get("reason", "")) == (expected["outcome"], expected["reason"])
        else:
            assert (body["outcome"], body["reason"]) == ("no_promise", "refused")


def test_identity_state_is_checked_through_verify_then_close() -> None:
    """Failed identity before verification: the close hook uses the pre fallback."""
    v: dict[str, Any] = {}
    for _ in range(3):
        v, _ = verify(v, {"ok": True, "verified": False, "all_verified": False})
    assert v["identity_state"] == "failed"
    assert close_body(CLOSE_AB, v) == {"customer_id": "c1", "outcome": "not_verified", "reason": "declined"}


def test_sources_mention_no_imports() -> None:
    for src in all_sources(SPEC_AB).values():
        assert "import " not in src
        assert "datetime" not in src
def test_callback_pre_computes_calendar_when_facts_not_run() -> None:
    """A third-party caller never reaches the facts node: the hook derives the allowed dates itself."""
    cal = engine.make_calendar(SPEC_A)
    cfg = callback_cfg(SPEC_A)
    cfg.update({
        "now_var": SPEC_A["call_variables"]["now"],
        "weekend": sorted(cal["weekend"]),
        "holidays": sorted(d.isoformat() for d in cal["holidays"]),
        "days_n": SPEC_A["third_party"]["callback_window"]["days"]["n"],
    })
    src = hooks.callback_pre_hook(cfg)
    for now in (NOW, FRIDAY_NOW, "2026-10-23T10:30", "2026-12-31T10:30"):
        now_dt = engine.parse_now(now)
        assert now_dt is not None
        want = ",".join(d.isoformat() for d in engine.allowed_callback_dates(now_dt.date(), SPEC_A, cal))
        before = {SPEC_A["call_variables"]["now"]: now}
        v, out, _ = run(src, dict(before), config={"body": {"date": now_dt.date().isoformat(), "time": "23:00"}})
        assert v["callback_dates"] == want
        assert v["today"] == now_dt.date().isoformat()
        assert v["now_time"] == now[11:16]
    # a valid time on an allowed date passes without the facts node
    d = engine.allowed_callback_dates(engine.parse_now(NOW).date(), SPEC_A, cal)[-1].isoformat()
    v, out, _ = run(src, {SPEC_A["call_variables"]["now"]: NOW}, config={"body": {"date": d, "time": "11:00"}})
    assert out["body"] == {"date": d, "time": "11:00"} and v["callback_state"] == ""


# ---------------------------------------------------------------------------------------------
# identity gate and last-try warning
# ---------------------------------------------------------------------------------------------

WRONG = {"ok": True, "verified": False, "all_verified": False}


def test_verify_last_try_text_when_one_wrong_answer_is_left() -> None:
    v: dict[str, Any] = {}
    texts = []
    for _ in range(2):
        v, data = verify(v, dict(WRONG))
        texts.append(data["what_to_do_next"])
    assert "submit it to the tool first" not in texts[0]
    assert "submit it to the tool first" in texts[1]
    v, data = verify(v, dict(WRONG))
    assert v["identity_state"] == "failed" and "submit it to the tool first" not in data["what_to_do_next"]


def test_verify_last_try_text_follows_max_wrong_and_bank_count() -> None:
    cfg = verify_cfg(SPEC_AB)
    cfg["max_wrong"] = 2
    _, _, res = run(hooks.verify_post_hook(cfg), {}, result={"data": dict(WRONG)})
    assert "submit it to the tool first" in res["data"]["what_to_do_next"]
    cfg["max_wrong"] = None
    _, _, res = run(hooks.verify_post_hook(cfg), {}, result={"data": dict(WRONG, wrong_answers_left=1)})
    assert "submit it to the tool first" in res["data"]["what_to_do_next"]
    _, _, res = run(hooks.verify_post_hook(cfg), {}, result={"data": dict(WRONG, wrong_answers_left=2)})
    assert "submit it to the tool first" not in res["data"]["what_to_do_next"]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("spec", [SPEC_A, SPEC_AB])
def test_pre_hooks_block_when_identity_failed(spec: dict[str, Any], layout: str) -> None:
    base = {f"p1_{n}": x for n, x in (("id", "P1"), ("name", "n"), ("full", 100.0), ("floor", 50.0), ("deadline", "2026-11-02"))}
    base["business_days"] = "2026-10-14,2026-10-15"
    cases = [
        (hooks.promise_pre_hook(promise_cfg(spec)), {"product_id": "P1", "amount": 100, "date": "2026-10-15"}, "product_id", "promise_guard"),
        (hooks.note_pre_hook(note_cfg(spec)), {"product_id": "P1", "reason": "refused"}, "product_id", "note_guard"),
        (hooks.transfer_pre_hook(transfer_cfg(spec)), {"queue": "support"}, "queue", "transfer_guard"),
        (hooks.account_pre_hook({"fields": {"customer": "customer_id"}}), {"customer_id": "C1"}, "customer_id", "account_guard"),
    ]
    for src, body, field, guard in cases:
        v, cfg, _ = run(src, dict(base, identity_state="failed"), config={"body": dict(body)}, layout=layout)
        assert cfg["body"][field] == "" and v[guard] == "blocked"
        for state in ({"identity_state": "pending"}, {"identity_state": "verified"}, {}):
            v, cfg, _ = run(src, dict(base, **state), config={"body": dict(body)}, layout=layout)
            assert cfg["body"][field] != "" and v.get(guard, "") != "blocked"


def test_callback_per_weekday_hook_agrees_with_engine_and_hours_text() -> None:
    spec = copy.deepcopy(SPEC_A)
    w = spec["third_party"]["callback_window"]
    w["days"]["n"] = 4
    w["per_weekday"] = [{"weekdays": ["thursday"], "hour_start": "10:00", "hour_end": "12:00", "inclusive": False}]
    cfg = callback_cfg(spec)
    cfg["windows"] = [[engine.WEEKDAYS.index("thursday"), "10:00", "12:00", False]]
    cfg["weekday_names"] = list(engine.WEEKDAYS)
    src = hooks.callback_pre_hook(cfg)
    cal = engine.make_calendar(spec)
    now_dt = engine.parse_now(NOW)
    base = facts_vars([], spec)
    dates = engine.allowed_callback_dates(now_dt.date(), spec, cal)
    for d in dates:
        for t in ["09:30", "10:00", "11:59", "12:00", "15:00", "19:00"]:
            v, out, _ = run(src, dict(base), config={"body": {"date": d.isoformat(), "time": t}})
            proposed = engine.parse_now(d.isoformat() + "T" + t)
            ok = engine.check_callback(proposed, now_dt, spec, cal)["status"] == "ok"
            assert (out["body"]["date"] != "") == ok, (d, t)
    assert v["callback_hours"] == engine.callback_hours_text(dates, spec)


def test_promise_post_auto_close_sets_flag_and_skips_success_sentence() -> None:
    cfg = {**promise_cfg(SPEC_A), "auto_close": True, "farewell_auto": True}
    pre, post = hooks.promise_pre_hook(cfg), hooks.promise_post_hook(cfg)
    v = facts_vars(PUB_PRODUCTS["PUB-07"], SPEC_A)
    v, data = promise_round(pre, post, v, promise_body(amount=540.0, date="2026-10-15"), True)
    assert v["auto_close"] == "yes" and v["closing_ready"] == "yes"
    assert "automatically" in data["what_to_do_next"] and "text message" not in data["what_to_do_next"]
    assert GOODBYE not in data["what_to_do_next"]


def test_finalize_closing_variant() -> None:
    src = hooks.finalize_source({**final_cfg(SPEC_AB), "confirm_single": True, "confirm_plural": True}, force=False)
    v, _, _ = run(src, multi_vars({1: "recorded"}, closing_ready="yes"))
    assert v["closing_variant"] == "single"
    v, _, _ = run(src, multi_vars({1: "recorded", 2: "recorded"}, closing_ready="yes"))
    assert v["closing_variant"] == "plural"
    v, _, _ = run(src, multi_vars({1: "recorded"}, closing_ready="yes", transfer_reason="restructuring"))
    assert v["closing_variant"] == ""
    v, _, _ = run(src, multi_vars({}, {1: "paid", 2: "paid", 3: "paid"}, closing_ready="yes"))
    assert v["closing_variant"] == ""
    v, _, _ = run(FINALIZE_AB, multi_vars({1: "recorded"}, closing_ready="yes"))
    assert v["closing_variant"] == ""
