"""Deterministic date and amount engine.

Pure functions over a PolicySpec dict (same shape as builder/spec_schema.json). Every rule
parameter (calendar, bands, amount fields, persuasion attempts, callback window, aggregation)
is read from the spec argument or the io argument; nothing here encodes a brief.

The code-node snippet (builder/snippets.py) embeds this file's source, so the same code runs
in the builder and on the platform. Constraints that keep the snippet valid:
- stdlib only: datetime, json, re;
- no nested dict/set literals and no brace-in-brace f-strings (the snippet may not contain
  a doubled brace);
- templates come from the spec and are filled by fill_template (missing slots become '');
- no class statements and no `global` statements (the snippet wraps this source in one
  function, so module-level names become closure variables);
- no fallback value for a policy parameter: a missing parameter is reported as unresolved;
- every loop is capped.
"""

from __future__ import annotations

import datetime
import json
import re

WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

DAY_CAP: int = 3660
ATTEMPT_CAP: int = 20
LISTED_BUSINESS_DAYS: int = 10
LISTED_HOLIDAYS: int = 20

_DATE_RE = re.compile(r"\s*(\d\d\d\d)-(\d\d)-(\d\d)")
_NOW_RE = re.compile(r"\s*(\d\d\d\d)-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?")
_HHMM_RE = re.compile(r"\s*(\d\d?):(\d\d)\s*$")
_NUMBER_RE = re.compile(r"\s*[-+]?(\d+(\.\d*)?|\.\d+)\s*$")


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------


def parse_date(s: object) -> datetime.date | None:
    """ISO YYYY-MM-DD (or the first 10 characters of a datetime string) -> date."""
    if isinstance(s, datetime.datetime):
        return s.date()
    if isinstance(s, datetime.date):
        return s
    if not isinstance(s, str):
        return None
    m = _DATE_RE.match(s)
    if m is None:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_now(s: object) -> datetime.datetime | None:
    """YYYY-MM-DDTHH:MM[:SS] (space separator allowed; trailing text ignored) -> datetime."""
    if isinstance(s, datetime.datetime):
        return s
    if not isinstance(s, str):
        return None
    m = _NOW_RE.match(s)
    if m is None:
        return None
    try:
        return datetime.datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6) or 0),
        )
    except ValueError:
        return None


def parse_hhmm(s: object) -> tuple[int, int] | None:
    """'HH:MM' -> (hour, minute)."""
    if not isinstance(s, str):
        return None
    m = _HHMM_RE.match(s)
    if m is None:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return (h, mi)


def to_number(x: object) -> float | None:
    """int/float/numeric string -> float; bool, NaN, infinity and anything else -> None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str) and _NUMBER_RE.match(x):
        v = float(x.strip())
    else:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _int_or_float(v: float) -> int | float:
    return int(v) if v == int(v) else v


# ---------------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------------


def make_calendar(spec: dict) -> dict:
    """Calendar dict: weekend weekday ints, holiday dates, week start int, weekday semantics."""
    c = spec.get("calendar") or {}
    weekend = set()
    for name in c.get("weekend_days") or []:
        if isinstance(name, str) and name.lower() in WEEKDAYS:
            weekend.add(WEEKDAYS.index(name.lower()))
    holidays = set()
    for h in c.get("holidays") or []:
        d = parse_date(h)
        if d is not None:
            holidays.add(d)
    ws = str(c.get("week_starts_on", "monday")).lower()
    week_start = WEEKDAYS.index(ws) if ws in WEEKDAYS else 0
    sem = c.get("weekday_semantics") or {}
    return {
        "weekend": weekend,
        "holidays": holidays,
        "week_start": week_start,
        "next": sem.get("next"),
        "this": sem.get("this"),
    }


def is_business_day(d: datetime.date, cal: dict) -> bool:
    return d.weekday() not in cal["weekend"] and d not in cal["holidays"]


def next_business_day_on_or_after(d: datetime.date, cal: dict) -> datetime.date:
    x = d
    for _ in range(DAY_CAP):
        if is_business_day(x, cal):
            return x
        x = x + datetime.timedelta(days=1)
    return d


def next_business_day_after(d: datetime.date, cal: dict) -> datetime.date:
    return next_business_day_on_or_after(d + datetime.timedelta(days=1), cal)


def previous_business_day_on_or_before(d: datetime.date, cal: dict) -> datetime.date:
    x = d
    for _ in range(DAY_CAP):
        if is_business_day(x, cal):
            return x
        x = x - datetime.timedelta(days=1)
    return d


def add_business_days(start: datetime.date, n: int, cal: dict) -> datetime.date:
    """The n-th business day strictly after start (n <= 0 -> start)."""
    if n <= 0:
        return start
    x = start
    count = 0
    for _ in range(DAY_CAP):
        x = x + datetime.timedelta(days=1)
        if is_business_day(x, cal):
            count += 1
            if count >= n:
                return x
    return x


def week_start_date(d: datetime.date, cal: dict) -> datetime.date:
    return d - datetime.timedelta(days=(d.weekday() - cal["week_start"]) % 7)


def last_business_day_of_week(d: datetime.date, cal: dict) -> datetime.date | None:
    ws = week_start_date(d, cal)
    for i in range(6, -1, -1):
        x = ws + datetime.timedelta(days=i)
        if is_business_day(x, cal):
            return x
    return None


def last_bd_this_week_or_next(today: datetime.date, cal: dict) -> datetime.date:
    """Last business day of this week; next week's if this week's is today or already past."""
    x = last_business_day_of_week(today, cal)
    if x is not None and x > today:
        return x
    ws = week_start_date(today, cal)
    for k in range(1, 530):
        x = last_business_day_of_week(ws + datetime.timedelta(days=7 * k), cal)
        if x is not None:
            return x
    return next_business_day_after(today, cal)


def resolve_weekday(
    today: datetime.date, weekday: str, qualifier: str, cal: dict
) -> datetime.date | None:
    """Resolve '<qualifier> <weekday>' with the calendar's semantics for that qualifier.

    qualifier 'next' / 'this' looks up the calendar semantics; any other value is taken as a
    semantics name. Unknown or missing semantics -> None.
    """
    w = str(weekday).lower()
    if w not in WEEKDAYS:
        return None
    idx = WEEKDAYS.index(w)
    sem = cal.get(qualifier) if qualifier in ("next", "this") else qualifier
    if sem == "following_calendar_week":
        ws = week_start_date(today, cal) + datetime.timedelta(days=7)
        return ws + datetime.timedelta(days=(idx - cal["week_start"]) % 7)
    if sem == "current_calendar_week":
        ws = week_start_date(today, cal)
        return ws + datetime.timedelta(days=(idx - cal["week_start"]) % 7)
    if sem == "next_occurrence":
        delta = (idx - today.weekday()) % 7
        return today + datetime.timedelta(days=delta or 7)
    if sem == "next_occurrence_inclusive":
        return today + datetime.timedelta(days=(idx - today.weekday()) % 7)
    return None


# ---------------------------------------------------------------------------------------------
# Bands and deadlines
# ---------------------------------------------------------------------------------------------


def band_for(days_late: object, spec: dict) -> dict | None:
    """First band whose inclusive [days_late_min, days_late_max] holds days_late."""
    n = to_number(days_late)
    if n is None:
        return None
    for b in spec.get("bands") or []:
        if not isinstance(b, dict):
            continue
        lo = to_number(b.get("days_late_min"))
        hi = to_number(b.get("days_late_max"))
        if lo is not None and n < lo:
            continue
        if hi is not None and n > hi:
            continue
        return b
    return None


def deadline_for(band: dict, today: datetime.date, cal: dict) -> datetime.date | None:
    dl = band.get("deadline") or {}
    n = int(to_number(dl.get("n")) or 0)
    kind = dl.get("kind")
    if kind == "business_days_after_today":
        return add_business_days(today, n, cal)
    if kind == "calendar_days_after_today":
        d = today + datetime.timedelta(days=n)
        roll = dl.get("roll", "none")
        if roll == "previous_business_day":
            return previous_business_day_on_or_before(d, cal)
        if roll == "next_business_day":
            return next_business_day_on_or_after(d, cal)
        return d
    return None


def deadline_for_days_late(
    days_late: object, today: datetime.date, spec: dict
) -> datetime.date | None:
    band = band_for(days_late, spec)
    if band is None:
        return None
    return deadline_for(band, today, make_calendar(spec))


# ---------------------------------------------------------------------------------------------
# Promise date
# ---------------------------------------------------------------------------------------------


def _iso(d: datetime.date | None) -> str:
    return d.isoformat() if d is not None else ""


def check_promise_date(
    proposed: object, today: datetime.date, deadline: datetime.date | None, cal: dict
) -> dict:
    """Status order: past -> deadline_unknown -> after_deadline -> moved_to_next_business_day -> ok.

    after_deadline returns the deadline as date. Unparsable input -> status 'invalid'. A None
    deadline (the policy gave none) -> status 'deadline_unknown' with date '': never 'ok'.
    """
    d = parse_date(proposed)
    if d is None:
        return {"status": "invalid", "date": "", "deadline": _iso(deadline)}
    if d < today:
        return {"status": "past", "date": d.isoformat(), "deadline": _iso(deadline)}
    if deadline is None:
        return {"status": "deadline_unknown", "date": "", "deadline": ""}
    if d > deadline:
        return {"status": "after_deadline", "date": deadline.isoformat(), "deadline": _iso(deadline)}
    if not is_business_day(d, cal):
        m = next_business_day_on_or_after(d, cal)
        if deadline is not None and m > deadline:
            return {
                "status": "after_deadline",
                "date": deadline.isoformat(),
                "deadline": _iso(deadline),
            }
        return {
            "status": "moved_to_next_business_day",
            "date": m.isoformat(),
            "deadline": _iso(deadline),
        }
    return {"status": "ok", "date": d.isoformat(), "deadline": _iso(deadline)}


# ---------------------------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------------------------


def _callback_window(spec: dict) -> dict:
    return (spec.get("third_party") or {}).get("callback_window") or {}


def callback_window_gaps(spec: dict) -> list[str]:
    """Callback-window keys the policy leaves undefined; [] when the window is fully defined.

    There is no fallback for any of them: a missing key means the policy did not say.
    """
    cw = _callback_window(spec)
    days = cw.get("days") or {}
    gaps = []
    if days.get("kind") != "today_or_next_business_days":
        gaps.append("days.kind")
    n = days.get("n")
    if isinstance(n, bool) or not isinstance(n, int):
        gaps.append("days.n")
    for key in ("inclusive", "must_be_after_now"):
        if not isinstance(cw.get(key), bool):
            gaps.append(key)
    return gaps


def allowed_callback_dates(today: datetime.date, spec: dict, cal: dict) -> list[datetime.date]:
    """Today plus the next n business days (window kind today_or_next_business_days).

    [] when the window kind or n is not defined by the policy.
    """
    cw = _callback_window(spec)
    days = cw.get("days") or {}
    if days.get("kind") != "today_or_next_business_days":
        return []
    n = days.get("n")
    if isinstance(n, bool) or not isinstance(n, int):
        return []
    out = [today]
    x = today
    for _ in range(min(max(n, 0), DAY_CAP)):
        x = next_business_day_after(x, cal)
        out.append(x)
    return out


def _hhmm(t: datetime.datetime) -> str:
    """HH:MM without strftime (strftime imports the time module, which a sandbox may block)."""
    return str(t.hour).zfill(2) + ":" + str(t.minute).zfill(2)


def _minutes(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def check_callback(proposed: object, now: object, spec: dict, cal: dict) -> dict:
    """Status order: invalid -> window_unspecified -> day_not_allowed -> outside_hours ->
    not_after_now -> ok. window_unspecified: the policy leaves part of the window undefined
    (see callback_window_gaps), so no time can be judged."""
    p = parse_now(proposed)
    n = parse_now(now)
    if p is None or n is None:
        return {"status": "invalid", "date": "", "time": ""}
    date_s = p.date().isoformat()
    time_s = _hhmm(p)
    if callback_window_gaps(spec):
        return {"status": "window_unspecified", "date": date_s, "time": time_s}
    if p.date() not in allowed_callback_dates(n.date(), spec, cal):
        return {"status": "day_not_allowed", "date": date_s, "time": time_s}
    cw = _callback_window(spec)
    start = parse_hhmm(cw.get("hour_start"))
    end = parse_hhmm(cw.get("hour_end"))
    t = p.hour * 60 + p.minute
    if start is not None and t < _minutes(start):
        return {"status": "outside_hours", "date": date_s, "time": time_s}
    if end is not None:
        inclusive = cw.get("inclusive")
        too_late = t > _minutes(end) if inclusive else t >= _minutes(end)
        if inclusive and t == _minutes(end) and p.second > 0:
            too_late = True
        if too_late:
            return {"status": "outside_hours", "date": date_s, "time": time_s}
    if cw.get("must_be_after_now") and p <= n:
        return {"status": "not_after_now", "date": date_s, "time": time_s}
    return {"status": "ok", "date": date_s, "time": time_s}


# ---------------------------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------------------------


def to_cents(x: object) -> int | None:
    v = to_number(x)
    if v is None:
        return None
    return int(round(v * 100))


def _amounts(spec: dict) -> dict:
    return spec.get("amounts") or {}


def _decimals(spec: dict) -> int:
    return int(to_number(_amounts(spec).get("rounding_decimals", 2)) or 0)


def _product_type(product: dict, spec: dict) -> object:
    field = (spec.get("account_fields") or {}).get("product_type")
    return product.get(field) if field else None


def full_for(product: dict, spec: dict) -> float | None:
    field = _amounts(spec).get("full_amount_field")
    if not field:
        return None
    v = to_number(product.get(field))
    return round(v, _decimals(spec)) if v is not None else None


def floor_for(product: dict, spec: dict) -> float | None:
    am = _amounts(spec)
    ptype = _product_type(product, spec)
    floor_fields = am.get("floor_fields") or {}
    field = floor_fields.get(ptype) if isinstance(ptype, str) else None
    if not field:
        field = am.get("floor_default_field")
    if not field:
        return None
    v = to_number(product.get(field))
    return round(v, _decimals(spec)) if v is not None else None


def classify_amount(amount: object, full: object, floor: object = None) -> str:
    """full | in_range (floor <= a < full) | below_floor | above_full | floor_unknown | invalid.

    Cent-exact. A missing floor (None) with an amount below full -> 'floor_unknown': the
    policy gave no floor, so the amount is never accepted as in_range.
    """
    a = to_cents(amount)
    f = to_cents(full)
    if a is None or a <= 0 or f is None:
        return "invalid"
    if a == f:
        return "full"
    if a > f:
        return "above_full"
    fl = to_cents(floor)
    if fl is None:
        return "floor_unknown"
    if a < fl:
        return "below_floor"
    return "in_range"


# ---------------------------------------------------------------------------------------------
# Persuasion
# ---------------------------------------------------------------------------------------------


def _days_late(product: dict, spec: dict) -> object:
    field = (spec.get("account_fields") or {}).get("days_late")
    return product.get(field) if field else None


def max_attempts(spec: dict) -> int:
    best = 0
    for b in spec.get("bands") or []:
        if isinstance(b, dict):
            v = to_number(b.get("persuasion_attempts"))
            if v is not None and v > best:
                best = int(v)
    return min(best, ATTEMPT_CAP)


def attempt_offer(
    n: int,
    product: dict,
    today: datetime.date,
    spec: dict,
    cal: dict,
    full_too_much: bool = False,
) -> dict | None:
    """Offer for persuasion attempt n; None when the product's band has no such attempt."""
    band = band_for(_days_late(product, spec), spec)
    if band is None:
        return None
    allowed = to_number(band.get("persuasion_attempts"))
    if allowed is None or n < 1 or n > allowed:
        return None
    attempt = None
    for a in (spec.get("persuasion") or {}).get("attempts") or []:
        if isinstance(a, dict) and to_number(a.get("index")) == n:
            attempt = a
            break
    if attempt is None:
        return None
    bands = attempt.get("bands")
    if isinstance(bands, list) and band.get("name") not in bands:
        return None
    deadline = deadline_for(band, today, cal)
    date_rule = attempt.get("date_rule")
    d = None
    if date_rule == "last_business_day_this_week_or_next_if_today":
        d = last_bd_this_week_or_next(today, cal)
    elif date_rule == "deadline":
        d = deadline
    elif date_rule == "today":
        d = today
    elif date_rule == "next_business_day":
        d = next_business_day_after(today, cal)
    if d is not None and deadline is not None and d > deadline:
        d = deadline
    amount_rule = attempt.get("amount_rule")
    full = full_for(product, spec)
    floor = floor_for(product, spec)
    amount = None
    if amount_rule == "full":
        amount = full
    elif amount_rule == "floor":
        amount = floor
    elif amount_rule == "full_or_floor_if_too_much":
        amount = floor if (full_too_much and floor is not None) else full
    return {
        "attempt": n,
        "date": _iso(d),
        "amount": amount,
        "date_rule": date_rule or "",
        "amount_rule": amount_rule or "",
    }


# ---------------------------------------------------------------------------------------------
# Multi-product
# ---------------------------------------------------------------------------------------------


def _product_id(product: dict, spec: dict) -> object:
    field = (spec.get("account_fields") or {}).get("product_id")
    return product.get(field) if field else None


def one_date_for_all(
    proposed: object, products: list, today: datetime.date, spec: dict, cal: dict
) -> list[dict]:
    """Apply one proposed date to every product, each against its own deadline.

    Mode cap_each_to_own_deadline: a date after a product's deadline becomes that deadline
    (capped True). Mode reject_if_any_after_deadline: such a product gets date '' and, when
    any product is after its deadline, every other product with a date is blanked too with
    status 'rejected_other_product_after_deadline'. No mode in the policy: such a product gets date '' and status 'unresolved' (there is no fallback).
    """
    mode = (spec.get("multi_product") or {}).get("one_date_for_all")
    out = []
    for p in products:
        if not isinstance(p, dict):
            continue
        band = band_for(_days_late(p, spec), spec)
        deadline = deadline_for(band, today, cal) if band is not None else None
        r = check_promise_date(proposed, today, deadline, cal)
        status = r["status"]
        capped = False
        d = r["date"]
        if status == "after_deadline":
            if mode == "cap_each_to_own_deadline":
                capped = True
            elif mode == "reject_if_any_after_deadline":
                d = ""
            else:
                d = ""
                status = "unresolved"
        elif status in ("past", "invalid"):
            d = ""
        out.append({"product_id": _product_id(p, spec), "date": d, "capped": capped, "status": status})
    if mode == "reject_if_any_after_deadline" and any(x["status"] == "after_deadline" for x in out):
        for x in out:
            if x["status"] != "after_deadline" and x["date"] != "":
                x["date"] = ""
                x["status"] = "rejected_other_product_after_deadline"
    return out


# ---------------------------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------------------------


def aggregate_outcome(states: list, spec: dict, transfer_reason: str | None = None) -> dict:
    """Final outcome from per-product states, following closing.aggregation.precedence.

    states: [{"product_id", "promise_recorded": bool, "note_reason": str | None}] in account
    order. Reason '' means none.
    """
    agg = (spec.get("closing") or {}).get("aggregation") or {}
    rows = [s for s in states if isinstance(s, dict)]
    for step in agg.get("precedence") or []:
        if step == "transfer":
            if transfer_reason and agg.get("transfer_outcome"):
                return {"outcome": agg.get("transfer_outcome"), "reason": transfer_reason}
        elif step == "any_promise":
            if agg.get("promise_outcome") and any(s.get("promise_recorded") for s in rows):
                return {"outcome": agg.get("promise_outcome"), "reason": ""}
        elif step == "all_notes_equal":
            cfg = agg.get("all_notes_equal") or {}
            want = cfg.get("note_reason")
            if rows and want and all(s.get("note_reason") == want for s in rows):
                return {"outcome": cfg.get("outcome", ""), "reason": ""}
        elif step == "first_note_not_equal":
            cfg = agg.get("first_note_not_equal") or {}
            excluded = cfg.get("excluded_reason")
            for s in rows:
                r = s.get("note_reason")
                if r and r != excluded:
                    return {"outcome": cfg.get("outcome", ""), "reason": r}
    default = agg.get("default") or {}
    return {"outcome": default.get("outcome", ""), "reason": default.get("reason", "")}


# ---------------------------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------------------------


TEMPLATE_SLOT_CAP: int = 50


def fill_template(template: str, values: dict) -> str:
    """str.format_map where a missing slot becomes ''. No class statement (a sandbox may lack
    __build_class__): each missing key is added as '' and the format is retried, capped."""
    table = dict(values)
    for _ in range(TEMPLATE_SLOT_CAP):
        try:
            return template.format_map(table)
        except KeyError as e:
            key = e.args[0] if e.args else None
            if key in table:
                raise
            table[key] = ""
    return template.format_map(table)


def spoken_product_name(product: dict, spec: dict) -> str:
    """debt_statement.product_names template for the product type; fallback: type, '_' -> ' '."""
    ptype = _product_type(product, spec)
    for pn in (spec.get("debt_statement") or {}).get("product_names") or []:
        if isinstance(pn, dict) and pn.get("product_type") == ptype:
            values = dict()
            for k, v in product.items():
                values[str(k)] = "" if v is None else str(v)
            try:
                return fill_template(str(pn.get("template", "")), values).strip()
            except Exception:
                break
    if ptype is None:
        return ""
    return str(ptype).replace("_", " ")


# ---------------------------------------------------------------------------------------------
# Defensive input
# ---------------------------------------------------------------------------------------------


def _numeric_fields(spec: dict) -> tuple[list, str]:
    am = _amounts(spec)
    amount_fields = []
    for f in [am.get("full_amount_field"), am.get("floor_default_field")] + list(
        (am.get("floor_fields") or {}).values()
    ):
        if isinstance(f, str) and f and f not in amount_fields:
            amount_fields.append(f)
    days = (spec.get("account_fields") or {}).get("days_late") or ""
    return amount_fields, days


def _coerce(product: dict, spec: dict) -> dict:
    amount_fields, days = _numeric_fields(spec)
    out = dict(product)
    for f in amount_fields:
        if f in out:
            v = to_number(out[f])
            if v is not None:
                out[f] = v
    if days and days in out:
        v = to_number(out[days])
        if v is not None:
            out[days] = _int_or_float(v)
    return out


def _maybe_json(raw: object) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def normalize_products(raw: object, flat: dict, prefix: str, spec: dict) -> list[dict]:
    """Product list from a list, a JSON string, a dict holding the products key, or flattened
    '<prefix>.<i>.<field>' keys in flat. Non-dict items are dropped; account order is kept."""
    key = (spec.get("account_fields") or {}).get("products_key")
    value = _maybe_json(raw)
    if isinstance(value, dict) and key:
        value = _maybe_json(value.get(key))
    items = []
    if isinstance(value, list):
        items = [x for x in value[:1000] if isinstance(x, dict)]
    if not items and isinstance(flat, dict) and prefix:
        head = prefix + "."
        by_index = dict()
        for k, v in list(flat.items())[:100000]:
            if not isinstance(k, str) or not k.startswith(head):
                continue
            parts = k[len(head) :].split(".", 1)
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
                continue
            i = int(parts[0])
            if i not in by_index:
                by_index[i] = dict()
            by_index[i][parts[1]] = v
        items = [by_index[i] for i in sorted(by_index)]
    return [_coerce(p, spec) for p in items]


# ---------------------------------------------------------------------------------------------
# Facts (the single function the builder and the code node both use)
# ---------------------------------------------------------------------------------------------


def _slot_count(spec: dict) -> int:
    mp = spec.get("multi_product") or {}
    if not mp.get("enabled", False):
        return 1
    n = to_number(mp.get("max_products", 1))
    return max(1, min(int(n or 1), ATTEMPT_CAP))


def _slot_fields(spec: dict) -> list[str]:
    fields = ["id", "type", "name", "full", "floor", "band", "deadline", "attempts"]
    for k in range(1, max_attempts(spec) + 1):
        fields.append("attempt" + str(k) + "_date")
        fields.append("attempt" + str(k) + "_amount")
    return fields


def output_names(spec: dict, io: dict) -> list[str]:
    """Every variable name compute_facts can write."""
    p = io["out_prefix"]
    names = [
        "facts_status",
        "now_state",
        "today",
        "today_weekday",
        "now_time",
        "products_state",
        "product_count",
    ]
    for i in range(1, _slot_count(spec) + 1):
        for f in _slot_fields(spec):
            names.append("p" + str(i) + "_" + f)
    names.append("date_facts")
    names.append("product_facts")
    return [p + n for n in names]


def _money(v: object, spec: dict) -> str:
    x = to_number(v)
    if x is None:
        return "unknown"
    return format(x, "." + str(_decimals(spec)) + "f")


def _day_label(d: datetime.date) -> str:
    return WEEKDAYS[d.weekday()].capitalize() + " " + d.isoformat()


def _date_facts(now: datetime.datetime, spec: dict, cal: dict) -> str:
    today = now.date()
    lines = []
    lines.append("Today is " + _day_label(today) + ". The time now is " + _hhmm(now) + ".")
    if is_business_day(today, cal):
        lines.append("Today is a business day.")
    else:
        lines.append("Today is not a business day.")
    days = []
    x = today
    for _ in range(LISTED_BUSINESS_DAYS):
        x = next_business_day_after(x, cal)
        days.append(_day_label(x))
    lines.append("Next " + str(LISTED_BUSINESS_DAYS) + " business days: " + ", ".join(days) + ".")
    upcoming = [h for h in sorted(cal["holidays"]) if h >= today][:LISTED_HOLIDAYS]
    if upcoming:
        lines.append(
            "Upcoming holidays (not business days): " + ", ".join(_day_label(h) for h in upcoming) + "."
        )
    else:
        lines.append("Upcoming holidays (not business days): none.")
    for qualifier in ("this", "next"):
        parts = []
        for w in WEEKDAYS:
            d = resolve_weekday(today, w, qualifier, cal)
            if d is None:
                parts.append(qualifier + " " + w.capitalize() + " = not defined by the policy")
            else:
                note = ""
                if d < today:
                    note = " (in the past)"
                elif d == today:
                    note = " (today)"
                elif not is_business_day(d, cal):
                    note = " (not a business day)"
                parts.append(qualifier + " " + w.capitalize() + " = " + d.isoformat() + note)
        lines.append("Meaning of '" + qualifier + " <weekday>': " + "; ".join(parts) + ".")
    this_last = last_business_day_of_week(today, cal)
    next_last = last_business_day_of_week(week_start_date(today, cal) + datetime.timedelta(days=7), cal)
    lines.append(
        "Last business day of this week: "
        + (_day_label(this_last) if this_last is not None else "none")
        + ". Last business day of next week: "
        + (_day_label(next_last) if next_last is not None else "none")
        + "."
    )
    cw = _callback_window(spec)
    gaps = callback_window_gaps(spec)
    if cw and gaps:
        lines.append(
            "Callback window: not fully defined by the policy (missing: " + ", ".join(gaps) + ")."
        )
    elif cw:
        allowed = allowed_callback_dates(today, spec, cal)
        hours = str(cw.get("hour_start", "")) + " to " + str(cw.get("hour_end", ""))
        hours += " (both included)" if cw.get("inclusive") else " (end excluded)"
        text = "Callback dates allowed: " + ", ".join(_day_label(d) for d in allowed) + "; hours " + hours
        if cw.get("must_be_after_now"):
            text += "; the time must be later than now (" + _hhmm(now) + " today)"
        lines.append(text + ".")
    return "\n".join(lines)


def _product_block(i: int, product: dict, today: datetime.date | None, spec: dict, cal: dict) -> str:
    lines = []
    pid = _product_id(product, spec)
    lines.append("Product " + str(i) + " (id " + str(pid if pid is not None else "unknown") + "):")
    lines.append("- spoken name: " + spoken_product_name(product, spec))
    lines.append("- full amount: " + _money(full_for(product, spec), spec))
    lines.append("- floor amount: " + _money(floor_for(product, spec), spec))
    dl = _days_late(product, spec)
    band = band_for(dl, spec)
    if band is None:
        lines.append("- band: none (days late " + str(dl) + ")")
        return "\n".join(lines)
    lines.append("- band: " + str(band.get("name", "")) + " (days late " + str(dl) + ")")
    lines.append("- persuasion attempts: " + str(band.get("persuasion_attempts", "")))
    if today is None:
        lines.append("- latest promise date: unknown (today's date is unknown)")
        return "\n".join(lines)
    lines.append("- latest promise date: " + _iso(deadline_for(band, today, cal)))
    for k in range(1, max_attempts(spec) + 1):
        offer = attempt_offer(k, product, today, spec, cal)
        if offer is None:
            continue
        text = "- attempt " + str(k) + " offer: " + _money(offer["amount"], spec) + " on " + offer["date"]
        alt = attempt_offer(k, product, today, spec, cal, True)
        if alt is not None and alt["amount"] != offer["amount"]:
            text += " (if the customer already said the full amount is too much: "
            text += _money(alt["amount"], spec) + " on " + alt["date"] + ")"
        lines.append(text)
    return "\n".join(lines)


def _plain(v: object) -> object:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def compute_facts(vars_in: dict, spec: dict, io: dict) -> dict[str, object]:
    """All facts for the code node. Values are strings or numbers only (no booleans).

    io keys (all required): products_var, products_flat_prefix, out_prefix.
    """
    p = io["out_prefix"]
    products_var = io["products_var"]
    flat_prefix = io["products_flat_prefix"]
    cal = make_calendar(spec)
    out = dict()
    for name in output_names(spec, io):
        out[name] = ""

    now_var = (spec.get("call_variables") or {}).get("now")
    now = parse_now(vars_in.get(now_var)) if now_var else None
    today = now.date() if now is not None else None
    if now is not None:
        out[p + "now_state"] = "known"
        out[p + "today"] = today.isoformat()
        out[p + "today_weekday"] = WEEKDAYS[today.weekday()]
        out[p + "now_time"] = _hhmm(now)
        out[p + "date_facts"] = _date_facts(now, spec, cal)
    else:
        out[p + "now_state"] = "unknown"
        out[p + "date_facts"] = "The current date and time are unknown."

    products = normalize_products(vars_in.get(products_var), vars_in, flat_prefix, spec)
    out[p + "products_state"] = "known" if products else "unknown"
    out[p + "product_count"] = len(products)
    blocks = []
    slots = _slot_count(spec)
    for i, prod in enumerate(products[:slots], start=1):
        s = p + "p" + str(i) + "_"
        band = band_for(_days_late(prod, spec), spec)
        out[s + "id"] = _plain(_product_id(prod, spec))
        out[s + "type"] = _plain(_product_type(prod, spec))
        out[s + "name"] = spoken_product_name(prod, spec)
        out[s + "full"] = _plain(full_for(prod, spec))
        out[s + "floor"] = _plain(floor_for(prod, spec))
        if band is not None:
            out[s + "band"] = _plain(band.get("name"))
            out[s + "attempts"] = _plain(band.get("persuasion_attempts"))
            if today is not None:
                out[s + "deadline"] = _iso(deadline_for(band, today, cal))
                for k in range(1, max_attempts(spec) + 1):
                    offer = attempt_offer(k, prod, today, spec, cal)
                    if offer is not None:
                        out[s + "attempt" + str(k) + "_date"] = offer["date"]
                        out[s + "attempt" + str(k) + "_amount"] = _plain(offer["amount"])
        blocks.append(_product_block(i, prod, today, spec, cal))
    if len(products) > slots:
        blocks.append(
            str(len(products) - slots) + " more product(s) on the account are beyond the "
            + str(slots) + " this call handles."
        )
    out[p + "product_facts"] = "\n\n".join(blocks) if blocks else "No product data is available."
    out[p + "facts_status"] = "ok"
    return out
