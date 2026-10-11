"""Failure classification, deterministic repairs and the keep-best controller of the online test.

Classes (checked in this order): infra, expectation, hook, closing, behavior, conversation,
judge_noise. Only `behavior` and `conversation` get an automatic prompt repair (a spec-derived
reminder appended to the node that owns the stage); `hook` and `closing` failures are reported with
evidence, and only an optional feature (callback, notes) that failed them twice is dropped through
selfcheck's own repair. A repaired workflow is kept only when it is strictly better on the cases it
was re-tested on; otherwise the best earlier workflow is saved again. Does not import builder.pipeline.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from builder import engine, selfcheck
from builder.compiler import NODE_IDENTITY, NODE_NEGOTIATE
from builder.online_cases import GenCase
from builder.online_runner import BATCH_SIZE, Budget, CaseResult, next_estimate

MARKER: str = "LIVE-TEST REMINDER"
MAX_REMINDERS: int = 3
MAX_REPAIR_ROUNDS: int = 2
SENTINELS: int = 2

INFRA, EXPECTATION, HOOK, CLOSING, BEHAVIOR, CONVERSATION, NOISE = (
    "infra", "expectation", "hook", "closing", "behavior", "conversation", "judge_noise")


@dataclass
class Classification:
    klass: str
    evidence: str
    kind: str


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------


def _args(call: dict) -> dict:
    for k in ("args", "request", "input", "body", "params"):
        if isinstance(call.get(k), dict):
            return call[k]
    return {}


def _tools_with(spec: dict, names: set[str]) -> dict[str, dict]:
    out = {}
    for n, t in (spec.get("tools") or {}).items():
        fm = t.get("field_map") if isinstance(t, dict) else None
        if isinstance(fm, dict) and names <= set(fm):
            out[n] = fm
    return out


def call_violations(case: GenCase, spec: dict, calls: list) -> list[str]:
    """Rule breaks in the arguments of recorded promise/callback calls (what the engine would refuse)."""
    cal = engine.make_calendar(spec)
    now = engine.parse_now(case.variables.get(str((spec.get("call_variables") or {}).get("now") or "now")))
    if now is None:
        return []
    today = now.date()
    promise, callback = _tools_with(spec, {"amount", "date"}), _tools_with(spec, {"date", "time"})
    bad: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        tool, args = call.get("tool"), _args(call)
        if tool in promise:
            fm = promise[tool]
            pid, date, amount = args.get(fm.get("product", "product_id")), args.get(fm["date"]), args.get(fm["amount"])
            if pid not in case.product_ids:
                bad.append(f"{tool}: product {pid} is not in the account")
                continue
            p = case.products[case.product_ids.index(pid)]
            band = engine.band_for(p.get(str((spec.get("account_fields") or {}).get("days_late") or "")), spec)
            deadline = engine.deadline_for(band, today, cal) if band else None
            r = engine.check_promise_date(date, today, deadline, cal)
            if r["status"] != "ok":
                bad.append(f"{tool}: date {date} is {r['status']} (deadline {r['deadline'] or 'unknown'})")
            kind = engine.classify_amount(amount, engine.full_for(p, spec), engine.floor_for(p, spec))
            if kind not in ("full", "in_range"):
                bad.append(f"{tool}: amount {amount} is {kind}")
        elif tool in callback:
            fm = callback[tool]
            r = engine.check_callback(f"{args.get(fm['date'])}T{args.get(fm['time'])}", now.isoformat(timespec="minutes"), spec, cal)
            if r["status"] != "ok":
                bad.append(f"{tool}: callback {args.get(fm['date'])} {args.get(fm['time'])} is {r['status']}")
    return bad


def _rejected(call: dict) -> bool:
    res = call.get("result")
    return isinstance(res, dict) and (res.get("ok") is False or bool(res.get("error")))


def _same_rows(led: dict, exp: dict) -> bool:
    def promises(rows: Any) -> list:
        return sorted((str(r.get("product_id")), round(float(r.get("amount", 0)), 2), str(r.get("date"))) for r in rows or [])

    def notes(rows: Any) -> list:
        return sorted((str(r.get("product_id")), str(r.get("reason"))) for r in rows or [])

    def cbs(rows: Any) -> list:
        return sorted((str(r.get("date")), str(r.get("time"))) for r in rows or [])

    def queues(rows: Any) -> list:
        return sorted(str(r.get("queue")) for r in rows or [])

    try:
        return (promises(led.get("promises")) == promises(exp.get("promises")) and notes(led.get("notes")) == notes(exp.get("notes"))
                and cbs(led.get("callbacks")) == cbs(exp.get("callbacks")) and queues(led.get("transfers")) == queues(exp.get("transfers")))
    except (TypeError, ValueError, AttributeError):
        return False


def classify(case: GenCase, result: CaseResult, spec: dict) -> Classification:
    kind = case.kind
    if result.infra:
        return Classification(INFRA, result.infra_error or "infrastructure error", kind)
    led = result.ledger
    calls = [c for c in led.get("calls") or [] if isinstance(c, dict)]
    bad = call_violations(case, spec, calls)
    rejected = [c for c in calls if _rejected(c) and c.get("tool") != "verify_identity"
                and (c.get("result") or {}).get("error") != "identity not verified"]
    if rejected and not bad and not result.ledger_pass:
        return Classification(EXPECTATION, "the bank rejected a call the engine accepts under the spec: " + "; ".join(
            f"{c.get('tool')} -> {(c.get('result') or {}).get('error', 'not ok')}" for c in rejected)[:300], kind)
    if bad:
        return Classification(HOOK, "; ".join(bad)[:400], kind)
    if not result.ledger_pass:
        if _same_rows(led, case.expected) or not calls or calls[-1].get("tool") != "close_call":
            return Classification(CLOSING, "; ".join(result.failed)[:400] or "the call was not closed as expected", kind)
        return Classification(BEHAVIOR, "; ".join(result.failed)[:400], kind)
    if not result.conversation_pass and result.judge_verdict:
        return Classification(CONVERSATION, result.judge_reason, kind)
    return Classification(NOISE, "the judge gave no verdict", kind)


# ---------------------------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------------------------


def _tag_for(case: GenCase, klass: str) -> str:
    if klass == CONVERSATION:
        return "conversation"
    return {"identity_lockout": "lockout", "date_over_limit": "date_over_limit", "amount_floor": "amount",
            "multi_one_date": "multi"}.get(case.kind, "conversation")


def reminder_text(tag: str, spec: dict) -> str:
    """The reminder paragraph for one tag, assembled from spec fields only ('' when the spec has none)."""
    ident, pr, am, mp = (spec.get(k) or {} for k in ("identity", "promise_rules", "amounts", "multi_product"))
    if tag == "lockout":
        of = ident.get("on_failure") or {}
        out = of.get("outcome") or {}
        n = ident.get("max_wrong_total")
        if not n or not out.get("outcome"):
            return ""
        end = f"outcome {out['outcome']}" + (f", reason {out['reason']}" if out.get("reason") else "")
        say = f' Say: "{of["say"]}".' if of.get("say") else ""
        debt = " Never mention a debt." if of.get("disclose_debt") is False else ""
        return f"After {n} wrong security answers in total, stop asking questions and do not read the account.{say}{debt} The call ends with {end}."
    if tag == "date_over_limit":
        if pr.get("on_after_deadline") != "offer_deadline":
            return ""
        return "A date after the deadline is never recorded. Offer the deadline date from the facts instead and record only a date on or before it."
    if tag == "amount":
        parts = ["Only record an amount from the minimum up to the full amount, both taken from the facts."]
        below, much = am.get("below_floor") or {}, am.get("too_much") or {}
        if below.get("say"):
            parts.append(f'If the customer offers less than the minimum, say "{below["say"]}".')
        for src in (below, much):
            if src.get("offer_floor_times"):
                parts.append(f"Offer the minimum {src['offer_floor_times']} time(s) when the amount is not acceptable.")
                break
        return " ".join(parts)
    if tag == "multi":
        mode = mp.get("one_date_for_all")
        if mode == "cap_each_to_own_deadline":
            return "When the customer gives one date for all products, apply it to each product separately: a date after a product's own deadline becomes that product's deadline."
        if mode == "reject_if_any_after_deadline":
            return "When the customer gives one date for all products and any product would be after its deadline, record no promise for the products and ask for another date."
        return ""
    if tag == "conversation":
        said = [f'{m["when"]}: "{m["text"]}"' for m in spec.get("mandated_sentences") or [] if isinstance(m, dict) and m.get("text")]
        rules = [str(r["text"]) for r in spec.get("verbatim_rules") or []
                 if isinstance(r, dict) and r.get("text") and r.get("stage") in ("negotiate", "global")]
        return " ".join(["Say the required sentences exactly: " + "; ".join(said) + "."] * bool(said) + rules)
    return ""


def apply_reminder(workflow: dict, tag: str, spec: dict) -> tuple[dict, str] | None:
    """A copy of the workflow with the reminder appended to the stage's node, or None (idempotent, capped)."""
    text = reminder_text(tag, spec)
    node_name = NODE_IDENTITY if tag == "lockout" else NODE_NEGOTIATE
    nodes = workflow.get("nodes")
    if not text or not isinstance(nodes, dict) or not isinstance(nodes.get(node_name), dict):
        return None
    mark = f"{MARKER} [{tag}]"
    total = sum(str(n.get("task_message", "")).count(MARKER) for n in nodes.values() if isinstance(n, dict))
    if mark in str(nodes[node_name].get("task_message", "")) or total >= MAX_REMINDERS:
        return None
    new = copy.deepcopy(workflow)
    node = new["nodes"][node_name]
    node["task_message"] = f"{node.get('task_message', '')}\n\n{mark}: {text}"
    return new, f"reminder:{tag}"


@dataclass
class Proposal:
    workflow: dict
    names: list[str]


def propose(spec: dict, workflow: dict, failures: list[tuple[GenCase, CaseResult, Classification]],
            streak: dict[str, int], recompile: Callable[[dict], dict | None] | None = None) -> Proposal | None:
    """All applicable repairs together as one new workflow, or None."""
    wf, names = workflow, []
    for case, _res, cls in failures:
        if cls.klass in (BEHAVIOR, CONVERSATION):
            done = apply_reminder(wf, _tag_for(case, cls.klass), spec)
            if done is not None:
                wf, name = done
                names.append(name)
    features = set()
    for case, _res, cls in failures:
        if cls.klass in (HOOK, CLOSING) and streak.get(case.id, 0) >= 2:
            if case.kind == "third_party_callback":
                features.add(selfcheck.FEATURE_CALLBACK)
            elif case.expected.get("notes"):
                features.add(selfcheck.FEATURE_NOTES)
    if features and recompile is not None:
        synthetic = selfcheck.SelfCheckResult(checks=[selfcheck.Check("online test", False, feature=f) for f in sorted(features)])
        fixed = selfcheck.repair_spec(spec, synthetic)
        if fixed is not None:
            rebuilt = recompile(fixed[0])
            if rebuilt is not None:
                wf, names = rebuilt, [f"drop_optional_feature:{n}" for n in fixed[1]]
    return Proposal(wf, names) if names else None


# ---------------------------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------------------------


@dataclass
class LoopResult:
    rounds: list[dict] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)
    live_workflow: dict = field(default_factory=dict)
    results: dict[str, CaseResult] = field(default_factory=dict)
    classes: dict[str, Classification] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)


def _scored(r: CaseResult | None, cls: Classification | None) -> bool:
    return r is not None and not r.infra and not (cls is not None and cls.klass == EXPECTATION)


def _key(ids: list[str], res: dict[str, CaseResult], classes: dict[str, Classification]) -> tuple[int, int]:
    ledger = conv = 0
    for i in ids:
        r = res.get(i)
        if _scored(r, classes.get(i)):
            ledger += r.ledger_pass  # type: ignore[union-attr]
            conv += r.passed or r.judge_noise  # type: ignore[union-attr]
    return ledger, conv


def run_loop(*, cases: list[GenCase], baseline: dict, spec: dict, budget: Budget,
             run_batch: Callable[[list[GenCase], float], list[CaseResult]], save: Callable[[dict], Any],
             recompile: Callable[[dict], dict | None] | None = None,
             log: Callable[[str], None] = lambda m: None) -> LoopResult:
    """Test the baseline, try spec-derived repairs, keep the best workflow. Strictly sequential."""
    out = LoopResult(live_workflow=baseline)
    by_id = {c.id: c for c in cases}
    state = {"est": None}
    best_wf, live_wf = baseline, baseline
    streak: dict[str, int] = {}

    def degrade(msg: str) -> None:
        out.degraded.append(msg)
        log(msg)

    def timed(batch: list[GenCase], repaired_live: bool) -> list[CaseResult] | None:
        reserve = Budget.reserve(repaired_live)
        if not budget.can_start(next_estimate(state["est"]), reserve):
            degrade(f"time budget does not allow another batch ({max(budget.remaining(), 0):.0f} s left)")
            return None
        t0 = budget.clock()
        res = run_batch(batch, reserve)
        if res and not any(r.infra for r in res):
            state["est"] = budget.clock() - t0
        return res

    def classify_all(res: dict[str, CaseResult]) -> None:
        for i, r in res.items():
            if not r.passed:
                out.classes[i] = classify(by_id[i], r, spec)
            else:
                out.classes.pop(i, None)

    # round 0: the baseline, five cases per simulation
    for i in range(0, len(cases), BATCH_SIZE):
        res = timed(cases[i:i + BATCH_SIZE], False)
        if res is None:
            break
        out.results.update({r.id: r for r in res})
        if res and all(r.infra for r in res):
            degrade("simulations are not working (" + str(res[0].infra_error)[:160] + "); testing stopped, workflow unchanged")
            break
    classify_all(out.results)
    noisy = [by_id[i] for i, r in out.results.items() if r.judge_noise]
    if noisy:
        again = timed(noisy, False)
        for r in again or []:
            if r.judge_verdict and not r.infra:
                out.results[r.id] = r
        classify_all(out.results)
    out.rounds.append({"round": 0, "results": [r.to_dict() for r in out.results.values()]})
    tested = [i for i in out.results if _scored(out.results[i], out.classes.get(i))]
    out.decision = {"kept": "baseline", "restore_failed": False, "accepted": [], "rolled_back": []}

    def rollback(names: list[str]) -> None:
        nonlocal live_wf
        out.decision["rolled_back"] += names
        if live_wf is best_wf:
            return
        if not budget.can_mutate():
            out.decision["restore_failed"] = True
            degrade("no time left to restore the best workflow")
            return
        try:
            save(best_wf)
            live_wf = best_wf
        except Exception as e:  # noqa: BLE001
            out.decision["restore_failed"] = True
            degrade(f"RESTORE FAILED, the repaired workflow is still live: {type(e).__name__}: {e}")

    for rnd in range(1, MAX_REPAIR_ROUNDS + 1):
        failures = []
        for c in cases:
            r, cls = out.results.get(c.id), out.classes.get(c.id)
            if r is not None and r.passed:
                streak.pop(c.id, None)
            if r is None or r.passed or cls is None or cls.klass in (INFRA, EXPECTATION, NOISE):
                continue
            streak[c.id] = streak.get(c.id, 0) + 1 if cls.klass in (HOOK, CLOSING) else 0
            failures.append((c, r, cls))
        if not failures:
            break
        prop = propose(spec, best_wf, failures, streak, recompile)
        if prop is None:
            log("no deterministic repair applies to the failures")
            break
        ids = [c.id for c, _r, _c in failures]
        ids += [i for i in tested if i not in ids and out.results[i].passed][:SENTINELS]
        if not budget.can_mutate() or not budget.can_start(next_estimate(state["est"]), Budget.reserve(True)):
            degrade("time budget does not allow a repair round; workflow unchanged")
            break
        try:
            save(prop.workflow)
        except Exception as e:  # noqa: BLE001
            degrade(f"repair save failed ({type(e).__name__}: {e}); the previous workflow stays live")
            break
        live_wf = prop.workflow
        out.repairs += prop.names
        res = timed([by_id[i] for i in ids], True)
        new = {r.id: r for r in res or []}
        old = dict(out.results)
        cls_new = {i: classify(by_id[i], r, spec) for i, r in new.items() if not r.passed}
        usable = [i for i in ids if i in new and _scored(new[i], cls_new.get(i)) and _scored(old.get(i), out.classes.get(i))]
        before = _key(usable, old, out.classes)
        after = _key(usable, new, cls_new)
        accepted = bool(usable) and after > before
        out.rounds.append({"round": rnd, "repairs": prop.names, "cases": ids, "before": list(before), "after": list(after),
                           "accepted": accepted, "results": [r.to_dict() for r in new.values()]})
        if not accepted:
            rollback(prop.names)
            break
        best_wf = live_wf
        out.decision["accepted"] += prop.names
        out.decision["kept"] = "repaired"
        out.results.update(new)
        classify_all(out.results)
    if live_wf is not best_wf and not out.decision["restore_failed"]:
        rollback([])
    out.live_workflow = live_wf
    return out
