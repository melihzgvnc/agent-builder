"""In-build offline self-checks of a compiled workflow.

run_selfchecks() simulates calls over the compiled code (the facts node, the tool hooks and the
code nodes) through builder.sandbox and compares the results with builder.engine, which is the
oracle. Every scenario is derived from the spec (bands, calendar, amounts, callback window,
closing table, openapi enums); a feature the spec does not have is skipped, never failed.

Checks (feature in brackets):
  structure   lint has no errors, no null, no agent_config, every code source passes its rules
  facts       facts node per band in both exec layouts, and at calendar edges (weekend / holiday)
  promise     promise pre-hook date scenarios against engine.check_promise_date
  amount      promise pre-hook amount scenarios per floor product type against engine.classify_amount
  callback    callback pre-hook scenarios (window edges, days, now) against engine.check_callback
  notes       note pre-hook / post-hook scenarios and note-driven aggregation
  outcomes    every code path that writes a final outcome, aggregation against engine.aggregate_outcome,
              reachability of every closing.outcome_table row (a row only the model can pick is a warning)
  no_none     no variable or request value written by compiled code is None
  layout      hook results do not depend on the exec layout

A failed check never raises; the caller decides what to do (see REPAIRS and repair_spec()).
"""

from __future__ import annotations

import copy
import datetime
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from builder import compiler, engine, hooks, lint, sandbox, snippets, tools_gen

SEVERITY_ERROR: str = "error"
SEVERITY_WARNING: str = "warning"

# Neutral calendar anchor used when the spec lists no holiday (not taken from any document).
NEUTRAL_ANCHOR: tuple[int, int, int] = (2030, 1, 7)
HOLIDAY_LEAD_DAYS: int = 10  # today is this many days before the first listed holiday
NEUTRAL_MINUTES: int = 9 * 60 + 30  # time of day when the spec has no callback window
FULL_AMOUNT: float = 1000.0  # neutral amounts for synthetic products
FLOOR_BASE: float = 400.0
FLOOR_STEP: float = 20.0
CUSTOMER_VALUE: str = "c1"
WRONG_PRODUCT_VALUE: str = "zz"

FEATURE_STRUCTURE: str = "structure"
FEATURE_FACTS: str = "facts"
FEATURE_PROMISE: str = "promise"
FEATURE_AMOUNT: str = "amount"
FEATURE_CALLBACK: str = "callback"
FEATURE_NOTES: str = "notes"
FEATURE_OUTCOMES: str = "outcomes"
FEATURE_NO_NONE: str = "no_none"
FEATURE_LAYOUT: str = "layout"


@dataclass
class Check:
    """One scenario result."""

    name: str
    ok: bool
    severity: str = SEVERITY_ERROR
    detail: str = ""
    feature: str = ""

    def format(self) -> str:
        text = f"{self.name} [{self.feature}]"
        return f"{text}: {self.detail}" if self.detail else text


@dataclass
class SelfCheckResult:
    """All checks that ran, the scenarios that were skipped, and the repairs that were applied."""

    checks: list[Check] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    ran: int = 0
    skipped: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ran:
            self.ran = len(self.checks)

    @property
    def failed(self) -> list[Check]:
        """Failed checks of severity error."""
        return [c for c in self.checks if not c.ok and c.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Check]:
        """Failed checks of severity warning."""
        return [c for c in self.checks if not c.ok and c.severity == SEVERITY_WARNING]

    def failed_features(self) -> set[str]:
        return {c.feature for c in self.failed}


class _Unusable(Exception):
    """A scenario cannot run because something it builds on is broken (reported elsewhere)."""


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _iso(d: datetime.date | None) -> str:
    return d.isoformat() if d is not None else ""


def _hhmm(minutes: int) -> str:
    m = max(0, min(23 * 60 + 59, minutes))
    return str(m // 60).zfill(2) + ":" + str(m % 60).zfill(2)


def _minutes(value: Any) -> int | None:
    hm = engine.parse_hhmm(value)
    return hm[0] * 60 + hm[1] if hm is not None else None


def _same_number(a: Any, b: Any) -> bool:
    x, y = engine.to_number(a), engine.to_number(b)
    if x is None or y is None:
        return x is None and y is None and (a in ("", None)) == (b in ("", None))
    return abs(x - y) < 0.005


def _code_of(node: dict) -> str:
    return node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _code_sources(workflow: dict) -> list[tuple[str, str, bool]]:
    """(label, source, is_facts) for every code node and every hook of the workflow."""
    out: list[tuple[str, str, bool]] = []
    for name, node in _dict(workflow.get("nodes")).items():
        if isinstance(node, dict) and node.get("node_type") == "code":
            try:
                out.append((f"node {name}", _code_of(node), name == compiler.NODE_FACTS))
            except (KeyError, IndexError, TypeError):
                out.append((f"node {name}", "", False))
    for path, tool, _where in lint.iter_tools(workflow):
        for key in ("pre_process_code", "post_process_code"):
            if isinstance(tool.get(key), str):
                out.append((f"{path}.{key}", tool[key], False))
    return out


# ---------------------------------------------------------------------------------------------
# The scenario runner
# ---------------------------------------------------------------------------------------------


class _Runner:
    def __init__(self, workflow: dict, spec: dict, openapi: dict, context: dict) -> None:
        self.wf = workflow
        self.sd = spec
        self.openapi = openapi if isinstance(openapi, dict) else {}
        self.ctx = context if isinstance(context, dict) else {}
        self.cal = engine.make_calendar(spec)
        cv = _dict(spec.get("call_variables"))
        self.cust_var = str(cv.get("customer_id") or "customer_id")
        self.now_var = str(cv.get("now") or "now")
        self.af = _dict(spec.get("account_fields"))
        self.am = _dict(spec.get("amounts"))
        self.io = _dict(self.ctx.get("facts_io"))
        self.fields = _dict(self.ctx.get("fields"))
        self.has = _dict(self.ctx.get("has"))
        self.resp = _dict(self.ctx.get("resp"))
        self.n_products = max(1, int(self.ctx.get("n_products") or 1))
        self.decimals = int(engine.to_number(self.am.get("rounding_decimals", 2)) or 0)
        self.cw = _dict(_dict(spec.get("third_party")).get("callback_window"))
        self.checks: list[Check] = []
        self.skipped: list[str] = []
        self.none_hits: list[str] = []
        self._facts_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.facts_src = self._facts_source()
        self.floor_fields = self._floor_field_names()
        self.types: list[str] = [t for t in _dict(self.am.get("floor_fields")) if isinstance(t, str)]
        self.bands = self._band_cases()
        self.today = self._pick_today()
        start, end = _minutes(self.cw.get("hour_start")), _minutes(self.cw.get("hour_end"))
        self.hs, self.he = start, end
        if start is not None and end is not None and end > start:
            self.now_min = (start + end) // 2
        else:
            self.now_min = NEUTRAL_MINUTES

    # ---- plumbing ---------------------------------------------------------------------------

    def add(self, name: str, ok: bool, feature: str, detail: str = "", severity: str = SEVERITY_ERROR) -> None:
        self.checks.append(Check(name, ok, severity, "" if ok else detail, feature))

    def skip(self, text: str) -> None:
        self.skipped.append(text)

    def attempt(self, name: str, feature: str, fn: Callable[[], tuple[bool, str]],
                severity: str = SEVERITY_ERROR) -> None:
        """Run one scenario; a crash is a failed check, an unusable base is a skip."""
        try:
            ok, detail = fn()
        except _Unusable as e:
            self.skip(f"{name}: {e}")
            return
        except Exception as e:  # noqa: BLE001 - a broken workflow must not stop the other checks
            ok, detail = False, f"scenario crashed: {type(e).__name__}: {e}"
        self.add(name, ok, feature, detail, severity)

    def watch(self, label: str, *objects: Any) -> None:
        """Record every None value in vars / request bodies after a scenario (check no_none)."""
        for obj in objects:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if value is None:
                        self.none_hits.append(f"{label}: {key}")
                    elif isinstance(value, dict):
                        self.watch(f"{label}.{key}", value)

    def _facts_source(self) -> str | None:
        node = _dict(_dict(self.wf.get("nodes")).get(compiler.NODE_FACTS))
        try:
            return _code_of(node)
        except (KeyError, IndexError, TypeError):
            return None

    def tool(self, role: str) -> dict | None:
        for _path, tool, _where in lint.iter_tools(self.wf):
            if _dict(tool.get("function")).get("name") == role:
                return tool
        return None

    def ok_key(self, role: str) -> str:
        return str(_dict(self.resp.get(role)).get("ok") or "ok")

    def enum(self, role: str, field_name: str) -> list[str]:
        try:
            path = tools_gen.path_for_tool(self.openapi, role)
            item = self.openapi["paths"][path]
            schema = tools_gen.body_schema(self.openapi, item[tools_gen._first_method(item)])
            leaf = _dict(schema.get("properties")).get(field_name)
            values = _dict(leaf).get("enum")
            return [str(v) for v in values] if isinstance(values, list) else []
        except Exception:  # noqa: BLE001 - an openapi without the operation simply has no enum
            return []

    # ---- spec-derived scenario inputs -------------------------------------------------------

    def _floor_field_names(self) -> list[str]:
        names: list[str] = []
        for f in list(_dict(self.am.get("floor_fields")).values()) + [self.am.get("floor_default_field")]:
            if isinstance(f, str) and f and f not in names:
                names.append(f)
        return names

    def _band_cases(self) -> list[tuple[str, int]]:
        """(band name, days late) per distinct band, the days taken from the band's own range."""
        out: list[tuple[str, int]] = []
        for band in self.sd.get("bands") or []:
            if not isinstance(band, dict):
                continue
            lo = engine.to_number(band.get("days_late_min"))
            hi = engine.to_number(band.get("days_late_max"))
            if lo is not None and hi is not None:
                days = int((lo + hi) // 2)
            elif lo is not None or hi is not None:
                days = int(lo if lo is not None else hi or 0)
            else:
                continue
            found = engine.band_for(days, self.sd)
            if found is None:
                continue
            name = str(found.get("name", ""))
            if all(n != name for n, _ in out):
                out.append((name, days))
        return out

    def _pick_today(self) -> datetime.date:
        holidays = sorted(self.cal["holidays"])
        if holidays:
            anchor = holidays[0] - datetime.timedelta(days=HOLIDAY_LEAD_DAYS)
        else:
            anchor = datetime.date(*NEUTRAL_ANCHOR)
        return engine.next_business_day_on_or_after(anchor, self.cal)

    def can_build_products(self) -> bool:
        return bool(
            self.af.get("product_id") and self.af.get("days_late")
            and self.am.get("full_amount_field") and self.bands
        )

    def now_iso(self, day: datetime.date | None = None, minutes: int | None = None) -> str:
        d = day if day is not None else self.today
        return f"{d.isoformat()}T{_hhmm(self.now_min if minutes is None else minutes)}"

    def make_product(self, product_id: str, ptype: str | None, days_late: int) -> dict[str, Any]:
        prod: dict[str, Any] = {str(self.af["product_id"]): product_id, str(self.af["days_late"]): days_late}
        if ptype and self.af.get("product_type"):
            prod[str(self.af["product_type"])] = ptype
        prod[str(self.am["full_amount_field"])] = FULL_AMOUNT
        for i, name in enumerate(self.floor_fields):
            prod[name] = FLOOR_BASE + FLOOR_STEP * i
        return prod

    def products_for(self, count: int) -> list[dict[str, Any]]:
        out = []
        for i in range(count):
            _name, days = self.bands[i % len(self.bands)]
            ptype = self.types[i % len(self.types)] if self.types else None
            out.append(self.make_product(f"p{i + 1}", ptype, days))
        return out

    # ---- facts -------------------------------------------------------------------------------

    def run_facts(self, products: list[dict], now_iso: str, layout: str = "single") -> dict[str, Any]:
        """Vars after the compiled facts node ran over a synthetic account (cached)."""
        if self.facts_src is None:
            raise _Unusable("the workflow has no facts node")
        key = (json.dumps(products, sort_keys=True), now_iso, layout)
        if key not in self._facts_cache:
            v: dict[str, Any] = {self.cust_var: CUSTOMER_VALUE, self.now_var: now_iso}
            prefix = str(self.io.get("products_flat_prefix") or "")
            for i, prod in enumerate(products):
                for k, val in prod.items():
                    v[f"{prefix}.{i}.{k}"] = val
            sandbox.run_snippet(self.facts_src, v, restricted=True, layout=layout)
            self._facts_cache[key] = v
        return dict(self._facts_cache[key])

    def facts(self, products: list[dict], now_iso: str) -> dict[str, Any]:
        """Like run_facts but raises _Unusable when the node did not produce usable facts."""
        v = self.run_facts(products, now_iso)
        if v.get("facts_status") != "ok" or v.get("products_state") != "known":
            raise _Unusable("the facts node gave no usable facts (see the facts checks)")
        return v

    def expected_days(self, last: datetime.date | None, today: datetime.date) -> str:
        return ",".join(_iso(d) for d in engine.business_days_through(today, last, self.cal)) if last else ""

    def facts_expectations(self, v: dict, products: list[dict], today: datetime.date) -> list[str]:
        bad: list[str] = []
        pre = str(self.io.get("out_prefix") or "")
        if v.get(pre + "facts_status") != "ok":
            bad.append(f"facts_status={v.get(pre + 'facts_status')!r}")
        if v.get(pre + "products_state") != "known":
            bad.append(f"products_state={v.get(pre + 'products_state')!r}")
        if v.get(pre + "today") != _iso(today):
            bad.append(f"today={v.get(pre + 'today')!r} want {_iso(today)}")
        latest: datetime.date | None = None
        for i, prod in enumerate(products[: self.n_products], start=1):
            s = f"{pre}p{i}_"
            days = prod.get(str(self.af.get("days_late")))
            band = engine.band_for(days, self.sd)
            dl = engine.deadline_for_days_late(days, today, self.sd)
            if dl is not None and (latest is None or dl > latest):
                latest = dl
            if v.get(s + "deadline") != _iso(dl):
                bad.append(f"{s}deadline={v.get(s + 'deadline')!r} want {_iso(dl)}")
            if band is not None and v.get(s + "band") != band.get("name"):
                bad.append(f"{s}band={v.get(s + 'band')!r} want {band.get('name')!r}")
            if not _same_number(v.get(s + "full"), engine.full_for(prod, self.sd)):
                bad.append(f"{s}full={v.get(s + 'full')!r} want {engine.full_for(prod, self.sd)!r}")
            if not _same_number(v.get(s + "floor"), engine.floor_for(prod, self.sd)):
                bad.append(f"{s}floor={v.get(s + 'floor')!r} want {engine.floor_for(prod, self.sd)!r}")
            if v.get(s + "id") != prod.get(str(self.af.get("product_id"))):
                bad.append(f"{s}id={v.get(s + 'id')!r}")
        if v.get(pre + "business_days") != self.expected_days(latest, today):
            bad.append(f"business_days={v.get(pre + 'business_days')!r} want {self.expected_days(latest, today)!r}")
        if self.cw and not engine.callback_window_gaps(self.sd):
            want = ",".join(_iso(d) for d in engine.allowed_callback_dates(today, self.sd, self.cal))
            if v.get(pre + "callback_dates") != want:
                bad.append(f"callback_dates={v.get(pre + 'callback_dates')!r} want {want!r}")
        return bad

    def special_days(self) -> list[tuple[str, datetime.date]]:
        """Days to use as 'today': the first, the last business day before a weekend and before a holiday."""
        out: list[tuple[str, datetime.date]] = [("first", self.today)]
        x = self.today
        for _ in range(60):
            nxt = x + datetime.timedelta(days=1)
            if engine.is_business_day(x, self.cal) and not engine.is_business_day(nxt, self.cal):
                if nxt.weekday() in self.cal["weekend"]:
                    out.append(("before_weekend", x))
                    break
            x = nxt
        for h in sorted(self.cal["holidays"]):
            if h > self.today:
                prev = engine.previous_business_day_on_or_before(h - datetime.timedelta(days=1), self.cal)
                if prev >= self.today and all(prev != d for _, d in out):
                    out.append(("before_holiday", prev))
                break
        return out

    def check_facts(self) -> None:
        if not self.can_build_products():
            self.skip("facts: the spec lacks account_fields.product_id / days_late, amounts.full_amount_field or bands")
            return
        for name, days in self.bands:
            prod = self.make_product("p1", self.types[0] if self.types else None, days)
            for layout in sandbox.LAYOUTS:
                def one(prod: dict = prod, layout: str = layout) -> tuple[bool, str]:
                    v = self.run_facts([prod], self.now_iso(), layout)
                    self.watch(f"facts[{name},{layout}]", v)
                    bad = self.facts_expectations(v, [prod], self.today)
                    return not bad, "; ".join(bad)
                self.attempt(f"facts.band[{name},{layout}]", FEATURE_FACTS, one)
        prod0 = self.products_for(1)
        for label, day in self.special_days():
            def cal_edge(day: datetime.date = day) -> tuple[bool, str]:
                v = self.run_facts(prod0, self.now_iso(day))
                self.watch(f"facts.calendar[{label}]", v)
                bad = self.facts_expectations(v, prod0, day)
                return not bad, "; ".join(bad)
            self.attempt(f"facts.calendar[{label}]", FEATURE_FACTS, cal_edge)
        if self.n_products > 1:
            many = self.products_for(self.n_products)

            def multi() -> tuple[bool, str]:
                v = self.run_facts(many, self.now_iso())
                bad = self.facts_expectations(v, many, self.today)
                pre = str(self.io.get("out_prefix") or "")
                if v.get(pre + "product_count") != len(many):
                    bad.append(f"product_count={v.get(pre + 'product_count')!r}")
                return not bad, "; ".join(bad)
            self.attempt(f"facts.products[{self.n_products}]", FEATURE_FACTS, multi)

    # ---- hooks ------------------------------------------------------------------------------

    def pre(self, tool: dict, vars_: dict, body: dict, layout: str = "single") -> dict:
        config = {"body": dict(body)}
        sandbox.run_hook(tool["pre_process_code"], vars_, config=config, layout=layout)
        return config["body"]

    def post(self, tool: dict, vars_: dict, data: dict, layout: str = "single") -> dict:
        result = {"data": dict(data), "header": {}}
        sandbox.run_hook(tool["post_process_code"], vars_, result=result, layout=layout)
        return result["data"]

    def need_tool(self, role: str, feature: str) -> dict | None:
        tool = self.tool(role)
        if tool is None or not isinstance(tool.get("pre_process_code"), str):
            self.add(f"{feature}.tool[{role}]", False, feature, f"the workflow has no {role} tool with a pre-hook")
            return None
        return tool

    # ---- promise date -----------------------------------------------------------------------

    def expected_promise_date(self, asked: datetime.date, deadline: datetime.date) -> str | None:
        r = engine.check_promise_date(_iso(asked), self.today, deadline, self.cal)
        status = r["status"]
        if status in ("ok", "moved_to_next_business_day"):
            return str(r["date"])
        if status == "after_deadline":
            return _iso(engine.previous_business_day_on_or_before(deadline, self.cal))
        if status == "past":
            return _iso(engine.next_business_day_on_or_after(self.today, self.cal))
        return None

    def promise_dates(self, deadline: datetime.date) -> list[tuple[str, datetime.date]]:
        one = datetime.timedelta(days=1)
        bds = engine.business_days_through(self.today, deadline, self.cal)
        out: list[tuple[str, datetime.date]] = []
        if bds:
            out.append(("inside", bds[len(bds) // 2]))
            out.append(("last_business_day", bds[-1]))
        out.append(("after_deadline", deadline + 3 * one))
        x = self.today + one
        while x < deadline:
            if x.weekday() in self.cal["weekend"] and engine.next_business_day_after(x, self.cal) <= deadline:
                out.append(("weekend", x))
                break
            x += one
        for h in sorted(self.cal["holidays"]):
            if self.today < h <= deadline and h.weekday() not in self.cal["weekend"]:
                if engine.next_business_day_after(h, self.cal) <= deadline:
                    out.append(("holiday", h))
                    break
        out.append(("past", self.today - 3 * one))
        return out

    def check_promise_dates(self) -> None:
        if not self.can_build_products():
            return
        tool = self.need_tool(compiler.ROLE_PROMISE, FEATURE_PROMISE)
        if tool is None:
            return
        f = _dict(self.fields.get(compiler.ROLE_PROMISE))
        for name, days in self.bands:
            deadline = engine.deadline_for_days_late(days, self.today, self.sd)
            if deadline is None:
                self.skip(f"promise.date[{name}]: the band has no deadline")
                continue
            prod = self.make_product("p1", self.types[0] if self.types else None, days)
            for label, asked in self.promise_dates(deadline):
                def one(asked: datetime.date = asked) -> tuple[bool, str]:
                    want = self.expected_promise_date(asked, deadline)
                    if want is None:
                        raise _Unusable("the engine gives no expected date")
                    v = self.facts([prod], self.now_iso())
                    body = {f["product"]: WRONG_PRODUCT_VALUE, f["amount"]: FULL_AMOUNT, f["date"]: _iso(asked)}
                    out = self.pre(tool, v, body)
                    self.watch(f"promise.date[{name},{label}]", v, out)
                    bad = []
                    if out.get(f["date"]) != want:
                        bad.append(f"date {out.get(f['date'])!r} for asked {_iso(asked)} (deadline {_iso(deadline)}), want {want!r}")
                    if out.get(f["product"]) != "p1":
                        bad.append(f"product id {out.get(f['product'])!r} was not forced to p1")
                    return not bad, "; ".join(bad)
                self.attempt(f"promise.date[{name},{label}]", FEATURE_PROMISE, one)
        self.check_layout_parity(tool, f)

    def check_layout_parity(self, tool: dict, f: dict) -> None:
        if not self.can_build_products():
            return
        name, days = self.bands[0]
        prod = self.make_product("p1", self.types[0] if self.types else None, days)

        def parity() -> tuple[bool, str]:
            v1 = self.facts([prod], self.now_iso())
            v2 = dict(v1)
            body = {f["product"]: WRONG_PRODUCT_VALUE, f["amount"]: FULL_AMOUNT * 1.5,
                    f["date"]: _iso(self.today + datetime.timedelta(days=2))}
            b1 = self.pre(tool, v1, body, "single")
            b2 = self.pre(tool, v2, body, "split")
            return (b1 == b2 and v1 == v2), f"single {b1!r} differs from split {b2!r}"
        self.attempt("layout.promise_pre", FEATURE_LAYOUT, parity)

    # ---- amounts ----------------------------------------------------------------------------

    def check_amounts(self) -> None:
        if not self.can_build_products():
            return
        tool = self.tool(compiler.ROLE_PROMISE)
        if tool is None or not isinstance(tool.get("pre_process_code"), str):
            return
        f = _dict(self.fields.get(compiler.ROLE_PROMISE))
        types: list[str | None] = list(self.types) if self.types else [None]
        _name, days = self.bands[0]
        good_date = _iso(engine.next_business_day_on_or_after(self.today, self.cal))
        for ptype in types:
            prod = self.make_product("p1", ptype, days)
            full = engine.full_for(prod, self.sd)
            floor = engine.floor_for(prod, self.sd)
            if full is None:
                self.skip(f"amount[{ptype}]: no full amount for the synthetic product")
                continue
            asked: list[tuple[str, float]] = [("above_full", round(full * 1.5, 2)), ("exact_full", full)]
            if floor is not None:
                if floor > 1:
                    asked.append(("below_floor", round(floor - 1, 2)))
                asked.append(("at_floor", floor))
                asked.append(("between", round((full + floor) / 2, 2)))
            for label, amount in asked:
                def one(amount: float = amount) -> tuple[bool, str]:
                    cls = engine.classify_amount(amount, full, floor)
                    if cls in ("full", "above_full"):
                        want: float | None = full
                    elif cls == "below_floor":
                        want = floor
                    elif cls == "in_range":
                        want = amount
                    else:
                        raise _Unusable(f"the engine classifies {amount} as {cls}")
                    v = self.facts([prod], self.now_iso())
                    body = {f["product"]: WRONG_PRODUCT_VALUE, f["amount"]: amount, f["date"]: good_date}
                    out = self.pre(tool, v, body)
                    self.watch(f"amount[{ptype},{label}]", v, out)
                    got = out.get(f["amount"])
                    ok = engine.to_number(got) is not None and abs(float(got) - float(want or 0)) < 0.005
                    return ok, f"asked {amount} (full {full}, floor {floor}): sent {got!r}, want {want!r}"
                self.attempt(f"amount[{ptype},{label}]", FEATURE_AMOUNT, one)

    # ---- callback ---------------------------------------------------------------------------

    def callback_cases(self, day: datetime.date) -> list[tuple[str, datetime.date, str]]:
        one = datetime.timedelta(days=1)
        allowed = engine.allowed_callback_dates(day, self.sd, self.cal)
        last = allowed[-1] if allowed else day
        mid = _hhmm(self.now_min + 60 if self.hs is None else (self.hs + (self.he or self.hs)) // 2)
        cases: list[tuple[str, datetime.date, str]] = [("inside", last, mid)]
        if self.hs is not None:
            cases += [("start_edge", last, _hhmm(self.hs)), ("before_start", last, _hhmm(self.hs - 1))]
        if self.he is not None:
            cases += [("end_edge", last, _hhmm(self.he)), ("before_end", last, _hhmm(self.he - 1)),
                      ("after_end", last, _hhmm(self.he + 1))]
        cases += [
            ("beyond_days", engine.next_business_day_after(last, self.cal), mid),
            ("next_calendar_day", day + one, mid),
            ("today_earlier", day, _hhmm(self.now_min - 1)),
            ("today_equal", day, _hhmm(self.now_min)),
            ("today_later", day, _hhmm(self.now_min + 1)),
            ("bad_time", last, "xx:yy"),
        ]
        return cases

    def check_callbacks(self) -> None:
        if not self.cw:
            self.skip("callback: the spec has no callback window")
            return
        if not self.has.get(compiler.ROLE_CALLBACK):
            self.skip("callback: the workflow has no callback tool")
            return
        gaps = engine.callback_window_gaps(self.sd)
        if gaps:
            self.skip("callback: the window is not fully defined (" + ", ".join(gaps) + ")")
            return
        if not self.can_build_products():
            self.skip("callback: no synthetic account can be built from the spec")
            return
        tool = self.need_tool(compiler.ROLE_CALLBACK, FEATURE_CALLBACK)
        if tool is None:
            return
        f = _dict(self.fields.get(compiler.ROLE_CALLBACK))
        prod = self.products_for(1)
        for dlabel, day in self.special_days():
            now_iso = self.now_iso(day)
            for label, date, time_s in self.callback_cases(day):
                def one(date: datetime.date = date, time_s: str = time_s, day: datetime.date = day,
                        now_iso: str = now_iso) -> tuple[bool, str]:
                    want = engine.check_callback(f"{_iso(date)}T{time_s}", now_iso, self.sd, self.cal)["status"]
                    v = self.facts(prod, now_iso)
                    body = {f["date"]: _iso(date), f["time"]: time_s}
                    out = self.pre(tool, v, body)
                    self.watch(f"callback[{dlabel},{label}]", v, out)
                    state = v.get("callback_state")
                    accepted = state != "invalid" and out.get(f["date"]) == _iso(date) and out.get(f["time"]) == time_s
                    blanked = state == "invalid" and out.get(f["date"]) == "" and out.get(f["time"]) == ""
                    if want == "ok":
                        return accepted, f"engine accepts {_iso(date)} {time_s} (now {now_iso}) but the hook sent {out!r} state {state!r}"
                    return blanked, f"engine rejects {_iso(date)} {time_s} ({want}, now {now_iso}) but the hook sent {out!r} state {state!r}"
                self.attempt(f"callback[{dlabel},{label}]", FEATURE_CALLBACK, one)

    # ---- notes ------------------------------------------------------------------------------

    def notes_enabled(self) -> bool:
        return bool(self.has.get(compiler.ROLE_NOTE))

    def check_notes(self) -> None:
        if not self.notes_enabled():
            self.skip("notes: the workflow has no note tool")
            return
        if not self.can_build_products():
            self.skip("notes: no synthetic account can be built from the spec")
            return
        pre_tool = self.need_tool(compiler.ROLE_NOTE, FEATURE_NOTES)
        if pre_tool is None:
            return
        f = _dict(self.fields.get(compiler.ROLE_NOTE))
        enum = self.enum(compiler.ROLE_NOTE, str(f.get("reason") or ""))
        mp = _dict(self.sd.get("multi_product"))
        reasons = enum or [str(r) for r in _dict(mp.get("notes")).get("reasons") or []]
        count = self.n_products
        products = self.products_for(count)
        ok_key = self.ok_key(compiler.ROLE_NOTE)
        for reason in reasons[:1]:
            def kept(reason: str = reason) -> tuple[bool, str]:
                v = self.facts(products, self.now_iso())
                out = self.pre(pre_tool, v, {f["product"]: WRONG_PRODUCT_VALUE, f["reason"]: reason})
                self.watch(f"notes.reason[{reason}]", v, out)
                return (out.get(f["reason"]) == reason and out.get(f["product"]) == "p1",
                        f"reason {reason!r} became {out.get(f['reason'])!r}, product {out.get(f['product'])!r}")
            self.attempt(f"notes.pre_keeps_reason[{reason}]", FEATURE_NOTES, kept)
        if enum:
            def clamped() -> tuple[bool, str]:
                v = self.facts(products, self.now_iso())
                out = self.pre(pre_tool, v, {f["product"]: WRONG_PRODUCT_VALUE, f["reason"]: "zz_unknown"})
                return out.get(f["reason"]) in enum, f"reason outside the enum was sent as {out.get(f['reason'])!r}"
            self.attempt("notes.pre_clamps_reason", FEATURE_NOTES, clamped)
        if reasons:
            def all_noted() -> tuple[bool, str]:
                v = self.facts(products, self.now_iso())
                v["identity_state"] = "verified"
                for k in range(1, count + 1):
                    self.pre(pre_tool, v, {f["product"]: f"p{k}", f["reason"]: reasons[0]})
                    self.post(pre_tool, v, {ok_key: True})
                self.watch("notes.all_noted", v)
                notes = [v.get(f"p{k}_note") for k in range(1, count + 1)]
                ready = v.get("closing_ready") == "yes"
                return ready and all(n == reasons[0] for n in notes), (
                    f"after a note for each of {count} product(s): notes {notes!r}, closing_ready {v.get('closing_ready')!r}")
            self.attempt("notes.post_marks_ready", FEATURE_NOTES, all_noted)

    # ---- outcomes ---------------------------------------------------------------------------

    def table_rows(self) -> list[tuple[str, str]]:
        rows = _dict(self.sd.get("closing")).get("outcome_table") or []
        return [(str(r.get("outcome", "")), str(r.get("reason") or "")) for r in rows if isinstance(r, dict)]

    def _reasonless(self, rows: list[tuple[str, str]]) -> set[str]:
        return {o for o, _ in rows} - {o for o, r in rows if r}

    def expected_pair(self, states: list[dict], transfer_reason: str | None, rows: list[tuple[str, str]]) -> tuple[str, str] | None:
        res = engine.aggregate_outcome(states, self.sd, transfer_reason)
        outcome, reason = str(res.get("outcome", "")), str(res.get("reason", ""))
        if outcome == "":
            return None
        return (outcome, "" if outcome in self._reasonless(rows) else reason)

    def run_close(self, vars_: dict) -> tuple[str, str] | None:
        """Outcome and reason the close pre-hook writes into the request for these vars."""
        tools = self.wf.get("tools")
        tool = tools[0] if isinstance(tools, list) and tools and isinstance(tools[0], dict) else None
        if tool is None or not isinstance(tool.get("pre_process_code"), str):
            return None
        f = _dict(self.fields.get(compiler.ROLE_CLOSE))
        out_f, rsn_f = str(f.get("outcome") or ""), str(f.get("reason") or "")
        body = {out_f: "x"}
        if rsn_f:
            body[rsn_f] = "x"
        done = self.pre(tool, dict(vars_), body)
        return (str(done.get(out_f, "")), str(done.get(rsn_f, "")) if rsn_f else "")

    def check_outcomes(self) -> None:
        rows = self.table_rows()
        if not rows:
            self.skip("outcomes: the spec has no closing.outcome_table")
            return
        if not self.can_build_products():
            self.skip("outcomes: no synthetic account can be built from the spec")
            return
        reached: set[tuple[str, str]] = set()
        count = self.n_products
        base = self.run_facts(self.products_for(count), self.now_iso())
        if base.get("facts_status") != "ok":
            self.skip("outcomes: the facts node gave no usable facts (see the facts checks)")
            return
        # (a) every code node, with a few preset states
        presets: list[dict[str, Any]] = [
            {}, {"callback_state": "scheduled"},
            {"identity_state": "verified", "closing_ready": "yes"},
            {"identity_state": "pending", "closing_ready": "yes"},
        ]
        for name, node in _dict(self.wf.get("nodes")).items():
            if not isinstance(node, dict) or node.get("node_type") != "code" or name == compiler.NODE_FACTS:
                continue

            def node_run(name: str = name, node: dict = node) -> tuple[bool, str]:
                src = _code_of(node)
                bad: list[str] = []
                for preset in presets:
                    v = {**base, **preset}
                    sandbox.run_hook(src, v)
                    self.watch(f"outcomes.node[{name}]", v)
                    if v.get("final_outcome"):
                        pair = (str(v["final_outcome"]), str(v.get("final_reason") or ""))
                        reached.add(pair)
                        if pair not in rows:
                            bad.append(f"writes {pair[0]}/{pair[1]}, which is not a closing.outcome_table row")
                return not bad, "; ".join(bad)
            self.attempt(f"outcomes.node[{name}]", FEATURE_OUTCOMES, node_run, SEVERITY_WARNING)
        # (b) close pre-hook alone (nothing recorded), verified and not
        for state in ("verified", "pending"):
            def close_alone(state: str = state) -> tuple[bool, str]:
                pair = self.run_close({**base, "identity_state": state})
                if pair is None:
                    raise _Unusable("the workflow has no close tool with a pre-hook")
                reached.add(pair)
                return pair in rows, f"the close hook writes {pair[0]}/{pair[1]}, which is not a closing.outcome_table row"
            self.attempt(f"outcomes.close_fallback[{state}]", FEATURE_OUTCOMES, close_alone, SEVERITY_WARNING)
        # (c) aggregation through finalize + close, against the engine
        finalize = _dict(_dict(self.wf.get("nodes")).get(compiler.NODE_FINALIZE))
        if not finalize:
            self.skip("outcomes: the workflow has no finalize node")
        else:
            self.aggregation_scenarios(finalize, base, rows, reached)
        # (d) reachability of every row
        for outcome, reason in rows:
            self.add(
                f"outcomes.reach[{outcome}/{reason}]", (outcome, reason) in reached, FEATURE_OUTCOMES,
                f"outcome row {outcome}/{reason} is not reachable by any compiled code path (only the model could pick it)",
                SEVERITY_WARNING,
            )

    def aggregation_scenarios(self, finalize: dict, base: dict, rows: list[tuple[str, str]],
                              reached: set[tuple[str, str]]) -> None:
        count = self.n_products
        mp = _dict(self.sd.get("multi_product"))
        note_reasons: list[str] = []
        if self.notes_enabled():
            note_reasons = [str(r) for r in _dict(mp.get("notes")).get("reasons") or []]
        src = _code_of(finalize)
        id_field = str(self.af.get("product_id"))
        products = self.products_for(count)
        # label, per-product (promise recorded, note reason or ''), transfer queue, identity, uses notes
        scenarios: list[tuple[str, list[tuple[bool, str]], str | None, str, bool]] = [
            ("nothing", [(False, "")] * count, None, "verified", False),
            ("unverified", [(False, "")] * count, None, "pending", False),
            ("promise_first", [(True, "")] + [(False, note_reasons[0] if note_reasons else "")] * (count - 1), None, "verified", False),
        ]
        if count > 1:
            scenarios.append(("promise_last", [(False, note_reasons[0] if note_reasons else "")] * (count - 1) + [(True, "")], None, "verified", False))
        for r in note_reasons:
            scenarios.append((f"all_notes[{r}]", [(False, r)] * count, None, "verified", True))
        if len(note_reasons) >= 2:
            a, b = note_reasons[0], note_reasons[1]
            scenarios.append(("first_note_differs", [(False, b)] + [(False, a)] * (count - 1), None, "verified", True))
            scenarios.append(("later_note_differs", [(False, a)] + [(False, b)] * (count - 1), None, "verified", True))
        queue_field = str(_dict(self.fields.get(compiler.ROLE_TRANSFER)).get("queue") or "")
        queues = self.enum(compiler.ROLE_TRANSFER, queue_field) if self.has.get(compiler.ROLE_TRANSFER) else []
        for q in queues:
            scenarios.append((f"transfer[{q}]", [(False, "")] * count, q, "verified", False))
        for label, per_product, queue, identity, uses_notes in scenarios:
            feature = FEATURE_NOTES if uses_notes else FEATURE_OUTCOMES

            def one(per_product: list[tuple[bool, str]] = per_product, queue: str | None = queue,
                    identity: str = identity) -> tuple[bool, str]:
                v = dict(base)
                v["identity_state"] = identity
                for k, (promised, note) in enumerate(per_product, start=1):
                    if promised:
                        v[f"p{k}_promise"] = "recorded"
                    if note:
                        v[f"p{k}_note"] = note
                if queue is not None:
                    tool = self.need_tool(compiler.ROLE_TRANSFER, FEATURE_OUTCOMES)
                    if tool is None:
                        raise _Unusable("no transfer tool")
                    tf = _dict(self.fields.get(compiler.ROLE_TRANSFER))
                    self.pre(tool, v, {tf["queue"]: queue})
                    self.post(tool, v, {self.ok_key(compiler.ROLE_TRANSFER): True})
                v["closing_ready"] = "yes"
                sandbox.run_hook(src, v)
                self.watch(f"outcomes.aggregate[{label}]", v)
                got = (str(v.get("final_outcome") or ""), str(v.get("final_reason") or ""))
                reached.add(got)
                bad: list[str] = []
                transfer_reason = str(v.get("transfer_reason") or "") or None
                states = [
                    {"product_id": products[k - 1].get(id_field), "promise_recorded": promised, "note_reason": note or None}
                    for k, (promised, note) in enumerate(per_product, start=1)
                ]
                if identity == "verified" or transfer_reason is not None:
                    want = self.expected_pair(states, transfer_reason, rows)
                    if want is not None and got != want:
                        bad.append(f"finalize wrote {got[0]}/{got[1]}, the engine says {want[0]}/{want[1]}")
                closed = self.run_close(v)
                if closed is not None:
                    reached.add(closed)
                    # finalize decided nothing (spec gap): the close hook leaves the model's own values alone
                    if got[0] and closed != got:
                        bad.append(f"the close hook wrote {closed[0]}/{closed[1]} but finalize wrote {got[0]}/{got[1]}")
                return not bad, "; ".join(bad)
            self.attempt(f"outcomes.aggregate[{label}]", feature, one)

    # ---- structure and none ------------------------------------------------------------------

    def check_structure(self) -> None:
        wf = self.wf
        errors = [f.format() for f in lint.lint(wf) if f.severity == "error"]
        self.add("structure.lint", not errors, FEATURE_STRUCTURE, "; ".join(errors[:5]))
        nulls = [p for p, v in lint._walk(wf, "$") if v is None]
        self.add("structure.no_null", not nulls, FEATURE_STRUCTURE, "null at " + ", ".join(nulls[:5]))
        self.add("structure.no_agent_config", "agent_config" not in wf, FEATURE_STRUCTURE, "agent_config is in the workflow")
        bad: list[str] = []
        for label, source, is_facts in _code_sources(wf):
            try:
                if is_facts:
                    snippets.check_snippet(source)
                else:
                    hooks.check_source(source)
            except Exception as e:  # noqa: BLE001 - SnippetError / ValueError describe the broken rule
                bad.append(f"{label}: {e}")
        self.add("structure.code_sources", not bad, FEATURE_STRUCTURE, "; ".join(bad[:5]))

    def check_no_none(self) -> None:
        self.add("no_none.runtime", not self.none_hits, FEATURE_NO_NONE,
                 "None written by compiled code: " + "; ".join(self.none_hits[:8]))


def run_selfchecks(workflow: dict, spec_dict: dict, openapi: dict, context: dict) -> SelfCheckResult:
    """Run every offline check on a compiled workflow; returns the result, never raises for a failed check.

    spec_dict is the spec as a plain dict, openapi the tool API document, context the
    CompileReport.context of the compile that produced the workflow.
    """
    runner = _Runner(workflow, spec_dict, openapi, context)
    steps: list[tuple[str, Callable[[], None]]] = [
        ("structure", runner.check_structure),
        ("facts", runner.check_facts),
        ("promise", runner.check_promise_dates),
        ("amount", runner.check_amounts),
        ("callback", runner.check_callbacks),
        ("notes", runner.check_notes),
        ("outcomes", runner.check_outcomes),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - a crash in the checker itself is reported, not raised
            runner.add(f"{name}.runner", False, FEATURE_STRUCTURE, f"self-check step crashed: {type(e).__name__}: {e}")
    runner.check_no_none()
    return SelfCheckResult(checks=runner.checks, skipped=runner.skipped)


# ---------------------------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repair:
    """A deterministic change of the spec that removes one optional feature."""

    name: str
    features: tuple[str, ...]
    description: str
    applies: Callable[[dict], bool]
    apply: Callable[[dict], None]


def _has_callback_window(spec: dict) -> bool:
    return bool(_dict(_dict(spec.get("third_party")).get("callback_window")))


def _drop_callback_window(spec: dict) -> None:
    spec["third_party"].pop("callback_window", None)


def _has_notes(spec: dict) -> bool:
    return bool(_dict(_dict(spec.get("multi_product")).get("notes")).get("for_products_without_promise"))


def _disable_notes(spec: dict) -> None:
    spec["multi_product"]["notes"]["for_products_without_promise"] = False


REPAIRS: tuple[Repair, ...] = (
    Repair(
        "drop_callback_window", (FEATURE_CALLBACK,),
        "remove third_party.callback_window (the callback tool is dropped, the model handles the case)",
        _has_callback_window, _drop_callback_window,
    ),
    Repair(
        "disable_notes", (FEATURE_NOTES,),
        "set multi_product.notes.for_products_without_promise to false (no note tool)",
        _has_notes, _disable_notes,
    ),
)


def repair_spec(spec_dict: dict, result: SelfCheckResult) -> tuple[dict, list[str]] | None:
    """Apply every repair whose feature failed; returns (repaired copy, repair names) or None.

    The copy has no `coverage` section (its paths do not survive an edit); it is only used to
    recompile, never written as spec.json.
    """
    failed = result.failed_features()
    chosen = [r for r in REPAIRS if failed & set(r.features) and r.applies(spec_dict)]
    if not chosen:
        return None
    fixed = copy.deepcopy(spec_dict)
    fixed.pop("coverage", None)
    for repair in chosen:
        repair.apply(fixed)
    return fixed, [r.name for r in chosen]
