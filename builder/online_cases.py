"""Spec-driven test cases for the online test stage (pure: no network, no pipeline import).

Cases are generated from the PolicySpec dict alone. The bank mock only knows a closed set of
accounts, so every case is bound to a "carrier": an account entry (products, identity answers,
variables, faults) read at run time from a cases file. The generated part is the customer's
behaviour (script) and the expected result, which the engine computes from the spec and the
carrier's own account. Nothing here is specific to one brief.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from builder import engine
from builder.node_prompts import note_reason_for_close
from builder.spec import load_spec

MAX_CASES: int = 9
CASE_LABEL: str = "online"
MONTHS: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass
class Carrier:
    """One account the bank can serve (borrowed from a cases file)."""

    case_id: str
    variables: dict[str, Any]
    identity: dict[str, Any]
    products: list[dict[str, Any]]
    faults: list[dict[str, Any]] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)


@dataclass
class GenCase:
    """One generated test case. `as_case()` has the shape tools/run_cases.py and score.py read."""

    id: str
    kind: str
    carrier_id: str
    title: str
    family: str
    level: str
    variables: dict[str, Any]
    identity: dict[str, Any]
    products: list[dict[str, Any]]
    script: str
    checks: list[str]
    expected: dict[str, Any]
    third_party: bool = False
    faults: list[dict[str, Any]] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    wrong_answers: list[tuple[str, str]] = field(default_factory=list)

    def as_case(self) -> dict[str, Any]:
        return {
            "id": self.id, "brief": CASE_LABEL, "family": self.family, "level": self.level,
            "title": self.title, "variables": self.variables, "identity": self.identity,
            "products": self.products, "script": self.script, "checks": self.checks,
            "expected": self.expected, "third_party": self.third_party, "faults": self.faults,
        }


def load_carriers(path: Any) -> list[Carrier]:
    """Read carriers from a cases file. Tolerant: never raises, skips unusable entries."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    carriers: list[Carrier] = []
    for e in data:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            continue
        variables, identity, products = e.get("variables"), e.get("identity"), e.get("products")
        if not isinstance(variables, dict) or not isinstance(identity, dict):
            continue
        if not isinstance(products, list) or not products or not all(isinstance(p, dict) for p in products):
            continue
        faults = e.get("faults")
        carriers.append(Carrier(
            e["id"], dict(variables), dict(identity), [dict(p) for p in products],
            [f for f in faults if isinstance(f, dict)] if isinstance(faults, list) else [],
        ))
    ids: set[str] = set()
    for e in data:
        exp = e.get("expected") if isinstance(e, dict) else None
        if not isinstance(exp, dict):
            continue
        for key in ("promises", "notes"):
            for row in exp.get(key) or []:
                if isinstance(row, dict) and isinstance(row.get("product_id"), str):
                    ids.add(row["product_id"])
    known = sorted(ids, key=lambda s: (len(s), s))
    for c in carriers:
        if len(known) >= len(c.products):
            c.product_ids = known[: len(c.products)]
    return carriers


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def spoken_date(d: datetime.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def wrong_answer(truth: Any, kind: str) -> str:
    """A value of the same shape as the truth that is guaranteed to differ from it."""
    text = str(truth)
    d = engine.parse_date(text)
    if d is not None and (kind == "date" or len(text) == 10):
        try:
            return d.replace(year=d.year - 1).isoformat()
        except ValueError:
            return (d - datetime.timedelta(days=366)).isoformat()
    if kind == "digits" or text.isdigit():
        return "".join(str((int(ch) + 1) % 10) if ch.isdigit() else ch for ch in text)
    return text + "x"


def _speak_value(value: str) -> str:
    d = engine.parse_date(value) if len(value) == 10 else None
    return spoken_date(d) if d is not None else value


def _reason(value: Any) -> Any:
    return value if value else None


def _outcome_pair(ref: Any) -> tuple[str, Any] | None:
    if isinstance(ref, dict) and ref.get("outcome"):
        return str(ref["outcome"]), _reason(ref.get("reason"))
    return None


def _empty_expected(outcome: str, reason: Any) -> dict[str, Any]:
    return {"outcome": outcome, "reason": reason, "promises": [], "notes": [], "callbacks": [], "transfers": []}


def _now_name(spec: dict) -> str:
    return str((spec.get("call_variables") or {}).get("now") or "now")


def _now_for(spec: dict, variables: dict) -> datetime.datetime | None:
    return engine.parse_now(variables.get(_now_name(spec)))


def _max_products(spec: dict) -> int:
    mp = spec.get("multi_product") or {}
    if mp.get("enabled"):
        n = engine.to_number(mp.get("max_products"))
        return max(1, int(n)) if n else 1
    return 1


def _product_ready(p: dict, today: datetime.date, spec: dict, cal: dict) -> bool:
    band = engine.band_for(p.get(str((spec.get("account_fields") or {}).get("days_late") or "")), spec)
    if band is None or engine.full_for(p, spec) is None:
        return False
    return engine.deadline_for(band, today, cal) is not None


def _deadline(p: dict, today: datetime.date, spec: dict, cal: dict) -> datetime.date:
    band = engine.band_for(p.get(str((spec.get("account_fields") or {}).get("days_late") or "")), spec)
    d = engine.deadline_for(band, today, cal) if band is not None else None
    assert d is not None
    return d


def _pick(spec: dict, carriers: list[Carrier], pred: Any, allow_faults: bool = False) -> Carrier | None:
    """Smallest sufficient carrier (fewest products, then id) that satisfies pred."""
    best = None
    for c in sorted(carriers, key=lambda c: (len(c.products), c.case_id)):
        if c.faults and not allow_faults:
            continue
        if not c.product_ids or len(c.products) > _max_products(spec):
            continue
        now = _now_for(spec, c.variables)
        if now is None:
            continue
        try:
            if pred(c, now.date()):
                best = c
                break
        except (KeyError, TypeError, ValueError, AssertionError):
            continue
    return best


def _base(kind: str, c: Carrier, title: str, family: str, level: str, script: str,
          checks: list[str], expected: dict, **kw: Any) -> GenCase:
    return GenCase(kind, kind, c.case_id, title, family, level,
                   c.variables, c.identity, c.products, script, checks, expected,
                   faults=c.faults, product_ids=c.product_ids, **kw)


def _promise_checks(spec: dict) -> list[str]:
    checks = ["identity was verified before the account was read"]
    pr = spec.get("promise_rules") or {}
    if pr.get("say_recorded_only_after_ok"):
        checks.append("the agent does not say the promise is recorded before the tool call succeeded")
    if pr.get("success_sentence"):
        checks.append(f"after the recorded promise the agent says: {pr['success_sentence']}")
    forbidden = (spec.get("opening") or {}).get("forbidden_before_verification")
    if forbidden:
        checks.append("before verification the agent does not say any of: " + ", ".join(map(str, forbidden)))
    return checks


def _agg(spec: dict, states: list[dict]) -> tuple[str, Any] | None:
    r = engine.aggregate_outcome(states, spec)
    if r.get("outcome"):
        return r["outcome"], _reason(r.get("reason"))
    if any(s.get("promise_recorded") for s in states):
        # no aggregation rule: the outcome-table row without a reason that describes a recorded promise
        for row in (spec.get("closing") or {}).get("outcome_table") or []:
            if isinstance(row, dict) and row.get("outcome") and not row.get("reason") and "promise" in str(row.get("when", "")).lower():
                return str(row["outcome"]), None
    return None


def _promise_expected(spec: dict, c: Carrier, rows: list[tuple[int, float, str]]) -> dict | None:
    states = [{"product_id": c.product_ids[i], "promise_recorded": True} for i, _a, _d in rows]
    pair = _agg(spec, states)
    if pair is None:
        return None
    exp = _empty_expected(*pair)
    exp["promises"] = [{"product_id": c.product_ids[i], "amount": a, "date": d} for i, a, d in rows]
    return exp


def _promise_date(p: dict, today: datetime.date, spec: dict, cal: dict) -> str | None:
    r = engine.check_promise_date(today, today, _deadline(p, today, spec, cal), cal)
    return r["date"] if r["status"] in ("ok", "moved_to_next_business_day", "after_deadline") else None


# ---------------------------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------------------------


def _identity_lockout(spec: dict, carriers: list[Carrier]) -> tuple[GenCase | None, str]:
    ident = spec.get("identity") or {}
    n = engine.to_number(ident.get("max_wrong_total"))
    pair = _outcome_pair((ident.get("on_failure") or {}).get("outcome"))
    qs = [q for q in ident.get("questions") or [] if isinstance(q, dict) and q.get("field")]
    if not n or n < 1 or pair is None or not qs:
        return None, "identity lockout: the spec has no wrong-answer limit with a failure outcome"
    n = int(n)
    c = _pick(spec, carriers, lambda c, t: all(str(q["field"]) in c.identity for q in qs))
    if c is None:
        return None, "identity lockout: no carrier account holds the answers to every security question"
    wrong: list[tuple[str, str]] = []
    for i in range(n):
        q = qs[i % len(qs)]
        f = str(q["field"])
        kind = str((q.get("answer_format") or {}).get("kind") or "")
        wrong.append((f, wrong_answer(c.identity[f], kind)))
    script = "Takes the call and agrees to the security questions, but gives only wrong answers, in this order, one per question asked: " + "; ".join(
        f"{i + 1}) {f.replace('_', ' ')}: {_speak_value(w)}" for i, (f, w) in enumerate(wrong)
    ) + ". Never gives a correct answer."
    of = ident.get("on_failure") or {}
    checks = [f"the agent stops asking security questions after {n} wrong answers in total", "the account was never read"]
    if of.get("say"):
        checks.append(f"the agent says: {of['say']}")
    if of.get("disclose_debt") is False:
        checks.append("the agent never mentions a debt, an amount or a product")
    return _base("identity_lockout", c, "Wrong security answers up to the limit", "Identity", "medium", script,
                 checks, _empty_expected(*pair), wrong_answers=wrong), ""


def _promise_full(spec: dict, carriers: list[Carrier], cal: dict, kind: str = "promise_full",
                  need_fault: bool = False) -> tuple[GenCase | None, str]:
    def ok(c: Carrier, today: datetime.date) -> bool:
        if need_fault and not _fault_ok(spec, c):
            return False
        if not need_fault and c.faults:
            return False
        p = c.products[0]
        return _product_ready(p, today, spec, cal) and _promise_date(p, today, spec, cal) is not None

    c = _pick(spec, carriers, ok, allow_faults=need_fault)
    if c is None:
        return None, f"{kind}: no usable carrier account"
    today = _now_for(spec, c.variables).date()  # type: ignore[union-attr]
    p = c.products[0]
    full, date = engine.full_for(p, spec), _promise_date(p, today, spec, cal)
    exp = _promise_expected(spec, c, [(0, full, date)])  # type: ignore[list-item]
    if exp is None:
        return None, f"{kind}: the outcome table has no outcome for a recorded promise"
    script = ("Takes the call, answers the security questions correctly, says the full overdue amount can be paid today, "
              "accepts the date the caller proposes and confirms the read-back.")
    title = "Pays the full amount on the first possible day"
    if kind == "tool_failure":
        title = "Same payment while the promise tool fails first"
        script += " The caller may need a second try; stays patient."
    checks = _promise_checks(spec)
    return _base(kind, c, title, "Tool failure" if kind == "tool_failure" else "Happy path", "easy", script, checks, exp), ""


def _fault_ok(spec: dict, c: Carrier) -> bool:
    tools = spec.get("tools") or {}
    promise_tools = {n for n, t in tools.items() if isinstance(t, dict) and {"amount", "date"} <= set((t.get("field_map") or {}))}
    retries = engine.to_number((spec.get("promise_rules") or {}).get("retries_on_failure")) or 0
    for f in c.faults:
        n = engine.to_number(f.get("fail_first_n"))
        if f.get("tool") in promise_tools and n is not None and 0 < n <= retries:
            return True
    return False


def _date_over_limit(spec: dict, carriers: list[Carrier], cal: dict) -> tuple[GenCase | None, str]:
    if (spec.get("promise_rules") or {}).get("on_after_deadline") != "offer_deadline":
        return None, "date clamping: the spec does not offer the deadline for a date beyond it"

    def ok(c: Carrier, today: datetime.date) -> bool:
        return _product_ready(c.products[0], today, spec, cal)

    c = _pick(spec, carriers, ok)
    if c is None:
        return None, "date clamping: no usable carrier account"
    today = _now_for(spec, c.variables).date()  # type: ignore[union-attr]
    p = c.products[0]
    dl = _deadline(p, today, spec, cal)
    if not engine.is_business_day(dl, cal):
        return None, "date clamping: the deadline itself is not a business day, so no promise could be recorded on it"
    beyond = engine.next_business_day_after(dl, cal)
    r = engine.check_promise_date(beyond, today, dl, cal)
    if r["status"] != "after_deadline":
        return None, "date clamping: the engine does not clamp the chosen date"
    exp = _promise_expected(spec, c, [(0, engine.full_for(p, spec), r["date"])])  # type: ignore[list-item]
    if exp is None:
        return None, "date clamping: the outcome table has no outcome for a recorded promise"
    script = (f"Takes the call, answers the security questions correctly and says the full overdue amount can be paid on "
              f"{spoken_date(beyond)}. When told the latest possible day, accepts that day for the full amount and confirms the read-back.")
    checks = _promise_checks(spec) + ["the agent does not record a date after the latest possible day"]
    return _base("date_over_limit", c, "Asks for a day beyond the deadline", "Dates", "medium", script, checks, exp), ""


def _amount_floor(spec: dict, carriers: list[Carrier], cal: dict) -> tuple[GenCase | None, str]:
    am = spec.get("amounts") or {}
    below, too_much = am.get("below_floor"), am.get("too_much")

    def floor_ok(c: Carrier, today: datetime.date) -> bool:
        p = c.products[0]
        full, floor = engine.full_for(p, spec), engine.floor_for(p, spec)
        return (_product_ready(p, today, spec, cal) and floor is not None and full is not None
                and floor < full and _promise_date(p, today, spec, cal) is not None)

    if isinstance(below, dict) and (engine.to_number(below.get("offer_floor_times")) or 0) >= 1:
        c = _pick(spec, carriers, floor_ok)
        if c is not None:
            today = _now_for(spec, c.variables).date()  # type: ignore[union-attr]
            p = c.products[0]
            floor = engine.floor_for(p, spec)
            half = round(floor / 2, int(engine.to_number(am.get("rounding_decimals", 2)) or 0))  # type: ignore[operator]
            if engine.classify_amount(half, engine.full_for(p, spec), floor) == "below_floor":
                exp = _promise_expected(spec, c, [(0, floor, _promise_date(p, today, spec, cal))])  # type: ignore[list-item]
                if exp is not None:
                    script = (f"Takes the call, answers the security questions correctly and says {half} can be paid today. "
                              f"When the caller refuses that amount and offers {floor}, accepts {floor} on the date proposed and confirms the read-back.")
                    checks = _promise_checks(spec) + ["the agent never records an amount below the minimum"]
                    if below.get("say"):
                        checks.append(f"the agent says: {below['say']}")
                    return _base("amount_floor", c, "Offers less than the minimum", "Amounts", "medium", script, checks, exp), ""
    if isinstance(too_much, dict) and (engine.to_number(too_much.get("offer_floor_times")) or 0) >= 1:
        c = _pick(spec, carriers, floor_ok)
        if c is not None:
            today = _now_for(spec, c.variables).date()  # type: ignore[union-attr]
            p = c.products[0]
            offer = engine.attempt_offer(1, p, today, spec, cal, full_too_much=True)
            if offer and offer.get("amount") is not None and offer.get("date"):
                exp = _promise_expected(spec, c, [(0, offer["amount"], offer["date"])])
                if exp is not None:
                    script = ("Takes the call, answers the security questions correctly and says the full overdue amount is too much right now. "
                              "When a lower amount and a day are offered, accepts both and confirms the read-back.")
                    checks = _promise_checks(spec) + ["the agent offers the lower amount from the account facts"]
                    return _base("amount_floor", c, "Says the full amount is too much", "Amounts", "medium", script, checks, exp), ""
    return None, "amounts: the spec has no usable below-minimum or too-much rule, or no carrier fits"


def _third_party_callback(spec: dict, carriers: list[Carrier], cal: dict) -> tuple[GenCase | None, str]:
    tp = spec.get("third_party") or {}
    pair = _outcome_pair(tp.get("on_callback_scheduled"))
    if pair is None or not tp.get("callback_window") or engine.callback_window_gaps(spec):
        return None, "callback: the spec has no fully defined callback window with an outcome"
    c = _pick(spec, carriers, lambda c, t: True)
    if c is None:
        return None, "callback: no usable carrier account"
    now = _now_for(spec, c.variables)
    start = engine.parse_hhmm((tp.get("callback_window") or {}).get("hour_start"))
    if now is None or start is None:
        return None, "callback: window start or current time unreadable"
    for d in engine.allowed_callback_dates(now.date(), spec, cal):
        for extra in (60, 120, 30):
            minutes = start[0] * 60 + start[1] + extra
            stamp = f"{d.isoformat()}T{minutes // 60:02d}:{minutes % 60:02d}"
            res = engine.check_callback(stamp, now.isoformat(timespec="minutes"), spec, cal)
            if res["status"] == "ok":
                exp = _empty_expected(*pair)
                exp["callbacks"] = [{"date": res["date"], "time": res["time"]}]
                who = f"{c.variables.get('first_name', '')} {c.variables.get('last_name', '')}".strip()
                script = (f"Is not {who}; answered the phone and says {who} is not available now. Does not know the security answers. "
                          f"When asked for a good time to call back, says {spoken_date(d)} at {res['time']}.")
                checks = ["the agent says nothing about the account or any debt to this person",
                          "the agent asks for a callback time", "the agent does not ask this person for security answers"]
                return _base("third_party_callback", c, "Someone else answers and names a callback time", "Third party", "medium",
                             script, checks, exp, third_party=True), ""
    return None, "callback: no callback time inside the window passes the engine check"


def _walk(actions: list) -> tuple[tuple[str, Any] | None, str | None, str]:
    """(close pair, transfer queue, spoken customer move) for the path where the customer declines offers."""
    close, queue, move = None, None, ""
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        t = a.get("type")
        if t == "transfer":
            queue = a.get("queue")
        elif t == "close":
            close = _outcome_pair(a)
        elif t == "offer_transfer":
            c2, q2, _m = _walk(a.get("on_decline") or [])
            close, queue = c2 or close, q2 or queue
            move = "When asked whether to be connected to another team, says no."
    return close, queue, move


def _intent_cases(spec: dict, carriers: list[Carrier], cal: dict, spec_obj: Any) -> tuple[list[GenCase], list[str]]:
    out: list[GenCase] = []
    skipped: list[str] = []
    intents = [i for i in spec.get("intents") or [] if isinstance(i, dict) and not i.get("applies_before_verification")]
    intents.sort(key=lambda i: engine.to_number(i.get("precedence")) or 0)
    mp = spec.get("multi_product") or {}
    uses_notes = bool(mp.get("enabled") and (mp.get("notes") or {}).get("for_products_without_promise"))
    for it in intents:
        if len(out) >= 2:
            break
        name = str(it.get("name") or "")
        triggers = [t for t in it.get("triggers") or [] if isinstance(t, str) and t.strip()]
        close, queue, move = _walk(it.get("actions") or [])
        if not triggers or close is None:
            skipped.append(f"intent {name}: no trigger phrase or no closing action")
            continue
        c = _pick(spec, carriers, lambda c, t: True)
        if c is None:
            skipped.append(f"intent {name}: no usable carrier account")
            continue
        exp = _empty_expected(*close)
        reason = note_reason_for_close(spec_obj, uses_notes, close[0], close[1] or "") if spec_obj is not None else ""
        if reason and queue is None:
            exp["notes"] = [{"product_id": pid, "reason": reason} for pid in c.product_ids]
            pair = _agg(spec, [{"product_id": pid, "promise_recorded": False, "note_reason": reason} for pid in c.product_ids])
            if pair is None:
                skipped.append(f"intent {name}: no aggregate outcome for the notes")
                continue
            exp["outcome"], exp["reason"] = pair
        if queue:
            exp["transfers"] = [{"queue": str(queue)}]
        script = (f"Takes the call and answers the security questions correctly. When the payment is discussed, says: \"{triggers[0]}\" "
                  f"and offers no payment. {move}").strip()
        checks = ["identity was verified before the account was read", "the agent does not record a promise"]
        out.append(_base(f"intent_{name}", c, f"Customer raises: {name.replace('_', ' ')}", "Intent", "medium", script, checks, exp))
    if not out and not skipped:
        skipped.append("intents: the spec has no intent usable after verification")
    return out, skipped


def _multi_one_date(spec: dict, carriers: list[Carrier], cal: dict) -> tuple[GenCase | None, str]:
    mp = spec.get("multi_product") or {}
    if not mp.get("enabled") or mp.get("one_date_for_all") != "cap_each_to_own_deadline":
        return None, "multi-product: not enabled or no date-capping rule in the spec"

    def ok(c: Carrier, today: datetime.date) -> bool:
        if len(c.products) < 2 or not all(_product_ready(p, today, spec, cal) for p in c.products):
            return False
        return len({_deadline(p, today, spec, cal) for p in c.products}) > 1

    c = _pick(spec, carriers, ok)
    if c is None:
        return None, "multi-product: no carrier account has products with different deadlines"
    today = _now_for(spec, c.variables).date()  # type: ignore[union-attr]
    latest = max(_deadline(p, today, spec, cal) for p in c.products)
    res = engine.one_date_for_all(latest.isoformat(), c.products, today, spec, cal)
    if len(res) != len(c.products) or any(not r["date"] for r in res):
        return None, "multi-product: the engine gives no date for some product"
    rows = [(i, engine.full_for(p, spec), r["date"]) for i, (p, r) in enumerate(zip(c.products, res))]
    exp = _promise_expected(spec, c, rows)  # type: ignore[arg-type]
    if exp is None:
        return None, "multi-product: the outcome table has no outcome for recorded promises"
    script = (f"Takes the call, answers the security questions correctly and says every overdue amount can be paid in full on "
              f"{spoken_date(latest)}, one date for all. Accepts the earlier day the caller names for any product and confirms the read-back.")
    checks = _promise_checks(spec) + ["every product gets its own promise", "no promise is later than that product's deadline"]
    return _base("multi_one_date", c, "One date for several products", "Multi-product", "hard", script, checks, exp), ""


def generate_cases(spec: dict, carriers: list[Carrier], limit: int = MAX_CASES) -> tuple[list[GenCase], list[str]]:
    """Deterministic. Returns (cases in priority order, reasons for the kinds that were skipped)."""
    cases: list[GenCase] = []
    skipped: list[str] = []
    cal = engine.make_calendar(spec)
    try:
        spec_obj = load_spec(spec)
    except Exception:  # noqa: BLE001 - notes expectations are then simply not derived
        spec_obj = None

    def add(res: tuple[GenCase | None, str]) -> None:
        if res[0] is not None:
            cases.append(res[0])
        elif res[1]:
            skipped.append(res[1])

    add(_identity_lockout(spec, carriers))
    add(_promise_full(spec, carriers, cal))
    add(_date_over_limit(spec, carriers, cal))
    add(_amount_floor(spec, carriers, cal))
    add(_third_party_callback(spec, carriers, cal))
    intents, why = _intent_cases(spec, carriers, cal, spec_obj)
    cases.extend(intents)
    skipped.extend(why)
    add(_multi_one_date(spec, carriers, cal))
    add(_promise_full(spec, carriers, cal, kind="tool_failure", need_fault=True))
    return cases[: max(0, limit)], skipped
