from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from builder import online_repair as R
from builder.compiler import NODE_IDENTITY, NODE_NEGOTIATE, compile_workflow
from builder.online_cases import GenCase, generate_cases, load_carriers
from builder.online_runner import Budget, CaseResult, _score_case, load_scoring, run_batch
from builder.spec import load_spec
from tests.conftest import REPO_ROOT, load_fixture
from tests.online_fakes import FakeBank, FakeSimFreya, World, ledger_for

SPEC = load_fixture("spec_brief_ab.json")
OPENAPI = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cases() -> list[GenCase]:
    return generate_cases(SPEC, load_carriers(REPO_ROOT / "public-test-set.json"))[0]


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    return compile_workflow(load_spec(SPEC), OPENAPI)[0]


@pytest.fixture(scope="module")
def scoring() -> Any:
    return load_scoring(require_key=False)


def kind(cases: list[GenCase], k: str) -> GenCase:
    return next(c for c in cases if c.kind == k)


class _LedgerBank:
    def __init__(self, led: dict) -> None:
        self.led = led

    def ledger(self, cid: str) -> dict:
        return self.led


def result_for(case: GenCase, scoring: Any, mode: str = "ok", judge: str = "pass") -> CaseResult:
    j = {"outcome": judge, "reason": "r", "callId": "c"} if judge != "none" else {"reason": "r"}
    return _score_case(_LedgerBank(ledger_for(case, mode)), scoring, case, {"customer_id": "cust"}, j)


# ---- classification ------------------------------------------------------------------------


def test_classification_table(cases, scoring) -> None:
    c = kind(cases, "date_over_limit")
    table = {
        "ok": ("pass", None),
        "rejected": ("pass", R.EXPECTATION),
        "bad_date": ("pass", R.HOOK),
        "wrong_outcome": ("pass", R.CLOSING),
        "behavior": ("pass", R.BEHAVIOR),
    }
    for mode, (judge, want) in table.items():
        res = result_for(c, scoring, mode, judge)
        if want is None:
            assert res.passed
        else:
            assert R.classify(c, res, SPEC).klass == want, mode
    assert R.classify(c, result_for(c, scoring, "ok", "fail"), SPEC).klass == R.CONVERSATION
    assert R.classify(c, result_for(c, scoring, "ok", "none"), SPEC).klass == R.NOISE
    infra = CaseResult(c.id, c.kind, infra_error="boom")
    assert R.classify(c, infra, SPEC).klass == R.INFRA


def test_hook_evidence_names_the_broken_rule(cases, scoring) -> None:
    c = kind(cases, "promise_full")
    cls = R.classify(c, result_for(c, scoring, "bad_date"), SPEC)
    assert "is after_deadline" in cls.evidence


# ---- reminders -----------------------------------------------------------------------------


def test_reminder_texts_come_from_the_spec(cases) -> None:
    other = copy.deepcopy(SPEC)
    other["identity"]["max_wrong_total"] = 5
    other["identity"]["on_failure"]["say"] = "Visit us somewhere else."
    a, b = R.reminder_text("lockout", SPEC), R.reminder_text("lockout", other)
    assert "3 wrong" in a and SPEC["identity"]["on_failure"]["say"] in a
    assert "5 wrong" in b and "Visit us somewhere else." in b
    assert SPEC["multi_product"]["one_date_for_all"].split("_")[0] in R.reminder_text("multi", SPEC) or R.reminder_text("multi", SPEC)
    assert SPEC["promise_rules"]["success_sentence"] not in R.reminder_text("date_over_limit", SPEC)
    assert SPEC["amounts"]["below_floor"]["say"] in R.reminder_text("amount", SPEC)
    assert SPEC["mandated_sentences"][0]["text"] in R.reminder_text("conversation", SPEC)
    no_rule = copy.deepcopy(SPEC)
    no_rule["promise_rules"]["on_after_deadline"] = "reject"
    assert R.reminder_text("date_over_limit", no_rule) == ""


def test_reminders_go_to_the_stage_node_idempotently_and_capped(baseline) -> None:
    before = json.dumps(baseline, sort_keys=True)
    new, name = R.apply_reminder(baseline, "lockout", SPEC)  # type: ignore[misc]
    assert name == "reminder:lockout" and json.dumps(baseline, sort_keys=True) == before
    assert R.MARKER in new["nodes"][NODE_IDENTITY]["task_message"]
    assert R.MARKER not in new["nodes"][NODE_NEGOTIATE]["task_message"]
    assert R.apply_reminder(new, "lockout", SPEC) is None
    wf = new
    for tag in ("date_over_limit", "amount"):
        wf = R.apply_reminder(wf, tag, SPEC)[0]  # type: ignore[index]
    assert R.apply_reminder(wf, "multi", SPEC) is None  # the fourth is refused
    assert R.apply_reminder({"nodes": {}}, "amount", SPEC) is None


def test_drop_optional_feature_is_used_only_after_two_hook_rounds(cases, scoring, baseline) -> None:
    c = kind(cases, "third_party_callback")
    res = result_for(c, scoring, "bad_date")
    cls = R.Classification(R.HOOK, "x", c.kind)
    rebuilt = {"nodes": {"rebuilt": {}}}
    seen: list[dict] = []

    def recompile(fixed: dict) -> dict | None:
        seen.append(fixed)
        return rebuilt

    assert R.propose(SPEC, baseline, [(c, res, cls)], {c.id: 1}, recompile) is None
    prop = R.propose(SPEC, baseline, [(c, res, cls)], {c.id: 2}, recompile)
    assert prop is not None and prop.workflow is rebuilt and prop.names == ["drop_optional_feature:drop_callback_window"]
    assert "callback_window" not in seen[0]["third_party"] and "callback_window" in SPEC["third_party"]
    assert R.propose(SPEC, baseline, [(c, res, cls)], {c.id: 2}, lambda f: None) is None  # recompile gate said no


# ---- controller ----------------------------------------------------------------------------


def run(cases, baseline, world, *, budget=None, save_fail_after=None, bank=None, freya=None):
    scoring = load_scoring(require_key=False)
    freya = freya or FakeSimFreya(world, fail_save_after=save_fail_after)
    bank = bank or FakeBank(world)
    budget = budget or Budget(600, None)

    def batch(cs: list[GenCase], reserve: float) -> list[CaseResult]:
        return run_batch(freya, bank, scoring, cs, "ag_1", budget, reserve, world.tmp / "p.json")

    world.live = baseline
    out = R.run_loop(cases=cases, baseline=baseline, spec=SPEC, budget=budget, run_batch=batch, save=lambda wf: freya.save_workflow("s", wf),
                     recompile=None)
    return out, freya


@pytest.fixture
def world(cases, tmp_path):
    w = World(cases)
    w.tmp = tmp_path  # type: ignore[attr-defined]
    return w


def test_all_green_changes_nothing(cases, baseline, world) -> None:
    out, freya = run(cases, baseline, world)
    assert out.live_workflow is baseline and freya.saved == [] and out.repairs == []
    assert out.decision["kept"] == "baseline" and freya.sim_count == 2  # nine cases = two batches of at most five


def test_improving_repair_is_kept_without_rollback(cases, baseline, world) -> None:
    c = kind(cases, "date_over_limit")
    world.modes[c.id] = "behavior"
    world.fixed_by_marker.add(c.id)
    out, freya = run(cases, baseline, world)
    assert out.decision["kept"] == "repaired" and out.decision["accepted"] == ["reminder:date_over_limit"]
    assert len(freya.saved) == 1 and out.live_workflow is freya.saved[0] and R.MARKER in json.dumps(out.live_workflow)
    assert out.results[c.id].passed and out.rounds[1]["accepted"] and out.rounds[1]["after"] > out.rounds[1]["before"]
    assert c.id in out.rounds[1]["cases"] and len(out.rounds[1]["cases"]) <= 1 + R.SENTINELS


def test_regressing_repair_is_rolled_back_to_the_best_workflow(cases, baseline, world) -> None:
    bad, victim = kind(cases, "date_over_limit"), kind(cases, "promise_full")
    world.modes[bad.id] = "behavior"
    world.fixed_by_marker.add(bad.id)
    world.broken_by_marker.add(victim.id)
    out, freya = run(cases, baseline, world)
    assert len(freya.saved) == 2 and freya.saved[1] is baseline and world.live is baseline
    assert out.live_workflow is baseline and out.decision["kept"] == "baseline" and out.decision["rolled_back"]
    assert not out.decision["restore_failed"]


def test_a_tie_rolls_back(cases, baseline, world) -> None:
    c = kind(cases, "date_over_limit")
    world.modes[c.id] = "behavior"  # the reminder does not help
    out, freya = run(cases, baseline, world)
    assert len(freya.saved) == 2 and out.live_workflow is baseline and out.rounds[1]["accepted"] is False


def test_failed_repair_save_keeps_the_baseline_and_notes_it(cases, baseline, world) -> None:
    world.modes[kind(cases, "date_over_limit").id] = "behavior"
    out, freya = run(cases, baseline, world, save_fail_after=0)
    assert out.live_workflow is baseline and freya.saved == []
    assert any("repair save failed" in d for d in out.degraded) and out.decision["kept"] == "baseline"


def test_failed_rollback_is_reported_and_does_not_crash(cases, baseline, world) -> None:
    world.modes[kind(cases, "date_over_limit").id] = "behavior"
    out, freya = run(cases, baseline, world, save_fail_after=1)
    assert out.decision["restore_failed"] is True and any("RESTORE FAILED" in d for d in out.degraded)
    assert out.live_workflow is freya.saved[0]  # what is really live is reported


def test_budget_gone_while_a_repair_is_live_and_unscored_rolls_back(cases, baseline, world) -> None:
    class Clock:
        t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    budget = Budget(600, None, clock)
    freya = FakeSimFreya(world)
    real_save = freya.save_workflow

    def save(slot: str, wf: dict, state_file: str = "") -> dict:
        info = real_save(slot, wf)
        if len(freya.saved) == 1:
            clock.t += 420  # the republish ate the time that was left for the re-test
        return info

    freya.save_workflow = save  # type: ignore[method-assign]
    world.modes[kind(cases, "date_over_limit").id] = "behavior"
    out, _ = run(cases, baseline, world, budget=budget, freya=freya)
    assert len(freya.saved) == 2 and freya.saved[1] is baseline and out.live_workflow is baseline
    assert any("time budget" in d for d in out.degraded)


def test_expectation_and_infra_failures_trigger_no_repair(cases, baseline, world) -> None:
    world.modes[kind(cases, "promise_full").id] = "rejected"
    out, freya = run(cases, baseline, world)
    assert out.classes[kind(cases, "promise_full").id].klass == R.EXPECTATION
    assert freya.saved == [] and out.repairs == []
    world2 = World(cases)
    world2.tmp = world.tmp  # type: ignore[attr-defined]
    out2, freya2 = run(cases, baseline, world2, bank=FakeBank(world2, fail_sessions=True))
    assert freya2.saved == [] and any("not working" in d for d in out2.degraded) and out2.live_workflow is baseline


def test_judge_noise_gets_one_single_case_rerun(cases, baseline, world) -> None:
    scoring = load_scoring(require_key=False)
    noisy = kind(cases, "promise_full")
    calls: list[list[str]] = []

    def batch(cs: list[GenCase], reserve: float) -> list[CaseResult]:
        calls.append([c.id for c in cs])
        again = len(calls) > 2
        return [result_for(c, scoring, "ok", "none" if (c.id == noisy.id and not again) else "pass") for c in cs]

    out = R.run_loop(cases=cases, baseline=baseline, spec=SPEC, budget=Budget(600, None), run_batch=batch, save=lambda wf: None)
    assert calls[2] == [noisy.id] and out.results[noisy.id].passed and noisy.id not in out.classes


def test_at_most_two_repair_rounds_and_each_must_improve(cases, baseline) -> None:
    scoring = load_scoring(require_key=False)
    a, b = kind(cases, "date_over_limit"), kind(cases, "amount_floor")
    live = {"wf": baseline}

    def batch(cs: list[GenCase], reserve: float) -> list[CaseResult]:
        text = json.dumps(live["wf"])
        out = []
        for c in cs:
            if c.id == a.id:
                out.append(result_for(c, scoring, "ok" if "[date_over_limit]" in text else "behavior"))
            elif c.id == b.id:
                if "[conversation]" in text:
                    out.append(result_for(c, scoring))
                elif "[amount]" in text:
                    out.append(result_for(c, scoring, "ok", "fail"))
                else:
                    out.append(result_for(c, scoring, "behavior"))
            else:
                out.append(result_for(c, scoring))
        return out

    def save(wf: dict) -> None:
        live["wf"] = wf

    out = R.run_loop(cases=cases, baseline=baseline, spec=SPEC, budget=Budget(600, None), run_batch=batch, save=save)
    assert [r["round"] for r in out.rounds] == [0, 1, 2] and out.decision["accepted"] == [
        "reminder:date_over_limit", "reminder:amount", "reminder:conversation"]
    assert out.results[a.id].passed and out.results[b.id].passed


def test_no_batch_runs_when_the_budget_is_already_gone(cases, baseline, world) -> None:
    out, freya = run(cases, baseline, world, budget=Budget(600, 0.0))
    assert freya.sim_count == 0 and out.results == {} and out.degraded and out.live_workflow is baseline
