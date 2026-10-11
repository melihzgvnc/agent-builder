from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder import online_runner
from builder.online_cases import GenCase, generate_cases, load_carriers
from builder.online_runner import Budget, load_scoring, run_batch
from tests.conftest import REPO_ROOT, load_fixture
from tests.online_fakes import FakeBank, FakeSimFreya, World


@pytest.fixture(scope="module")
def cases() -> list[GenCase]:
    return generate_cases(load_fixture("spec_brief_ab.json"), load_carriers(REPO_ROOT / "public-test-set.json"))[0]


@pytest.fixture(scope="module")
def scoring():
    s = load_scoring(require_key=False)
    assert s is not None
    return s


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _run(cases, scoring, tmp_path, world=None, freya=None, bank=None, budget=None, subset=None):
    world = world or World(cases)
    freya = freya or FakeSimFreya(world)
    bank = bank or FakeBank(world)
    budget = budget or Budget(600, None)
    batch = subset or cases[:3]
    return run_batch(freya, bank, scoring, batch, "ag_1", budget, 30.0, tmp_path / "personas.json"), freya, bank


def test_passing_ledgers_score_through_the_real_scorer(cases, scoring, tmp_path) -> None:
    res, freya, _ = _run(cases, scoring, tmp_path)
    assert [r.passed for r in res] == [True, True, True]
    assert freya.sim_count == 1 and all(r.customer_id for r in res)


def test_ledger_mismatch_is_a_ledger_failure(cases, scoring, tmp_path) -> None:
    world = World(cases, {cases[1].id: "wrong_outcome"})
    res, _, _ = _run(cases, scoring, tmp_path, world=world)
    assert res[1].ledger_pass is False and not res[1].passed
    assert any(f.startswith("outcome") for f in res[1].failed)
    assert res[0].passed


def test_judge_failure_marks_conversation_only(cases, scoring, tmp_path) -> None:
    world = World(cases)
    world.judge[cases[0].id] = "fail"
    res, _, _ = _run(cases, scoring, tmp_path, world=world)
    assert res[0].ledger_pass and not res[0].conversation_pass and res[0].judge_verdict
    assert res[0].failed and res[0].failed[0].startswith("conversation:")


def test_judge_without_verdict_is_recorded_as_noise(cases, scoring, tmp_path) -> None:
    world = World(cases)
    world.judge[cases[0].id] = "none"
    res, _, _ = _run(cases, scoring, tmp_path, world=world)
    assert res[0].judge_noise and not res[0].passed and not res[0].judge_verdict


def test_session_and_ledger_errors_become_infra_without_raising(cases, scoring, tmp_path) -> None:
    res, freya, _ = _run(cases, scoring, tmp_path, bank=FakeBank(World(cases), fail_sessions=True))
    assert all(r.infra for r in res) and freya.sim_count == 0
    res, _, _ = _run(cases, scoring, tmp_path, bank=FakeBank(World(cases), fail_ledger=True))
    assert all(r.infra and r.infra_error.startswith("ledger") for r in res)


def test_a_failing_simulation_is_called_once_and_never_retried(cases, scoring, tmp_path) -> None:
    freya = FakeSimFreya(World(cases), raise_on_sim=RuntimeError("502 bad gateway"))
    res, freya, _ = _run(cases, scoring, tmp_path, freya=freya)
    assert freya.sim_count == 1
    assert all(r.infra and "502" in r.infra_error for r in res)


def test_a_simulation_that_never_returns_is_abandoned(cases, scoring, tmp_path) -> None:
    world = World(cases)
    freya = FakeSimFreya(world, sim_delay=2.0)
    budget = Budget(1.0, None, margin_s=0.0)
    res, _, _ = _run(cases, scoring, tmp_path, world=world, freya=freya, budget=budget)
    assert all(r.infra and "abandoned" in r.infra_error for r in res)


def test_persona_cache_creates_only_new_personas(cases, scoring, tmp_path) -> None:
    world = World(cases)
    freya = FakeSimFreya(world)
    _run(cases, scoring, tmp_path, world=world, freya=freya, subset=cases[:2])
    _run(cases, scoring, tmp_path, world=world, freya=freya, subset=cases[:3])
    assert freya.persona_batches == [2, 1]
    store = json.loads((tmp_path / "personas.json").read_text(encoding="utf-8"))
    assert len(store) == 3 and all(":" in k for k in store)


# ---- budget guard -------------------------------------------------------------------------


def test_stage_deadline_is_the_smaller_of_stage_budget_and_total() -> None:
    clock = Clock()
    assert Budget(600, None, clock).remaining() == pytest.approx(600 - online_runner.STAGE_SOFT_MARGIN_S)
    assert Budget(600, clock.t + 200, clock).remaining() == pytest.approx(200)
    assert Budget(600, clock.t + 9999, clock).remaining() == pytest.approx(555)


def test_no_batch_starts_when_remaining_minus_reserve_is_too_small() -> None:
    clock = Clock()
    b = Budget(600, None, clock)
    assert b.can_start(180, 30)
    clock.t += 400
    assert b.remaining() == pytest.approx(155)
    assert not b.can_start(180, 30) and b.can_start(100, 30)
    assert not b.can_start(100, online_runner.REPUBLISH_RESERVE_S)


def test_reserve_switches_to_the_republish_value_when_a_repair_is_live() -> None:
    assert Budget.reserve(False) == online_runner.EXPORT_RESERVE_S
    assert Budget.reserve(True) == online_runner.REPUBLISH_RESERVE_S > online_runner.EXPORT_RESERVE_S


def test_exhausted_total_budget_cannot_start_and_cannot_mutate() -> None:
    clock = Clock()
    b = Budget(600, clock.t - 1, clock)
    assert not b.can_start(1, 0) and not b.can_mutate()


def test_no_total_deadline_means_only_the_stage_limit() -> None:
    clock = Clock()
    b = Budget(600, None, clock)
    clock.t += 500
    assert b.can_mutate()
    clock.t += 200
    assert not b.can_mutate()


def test_batch_estimate_uses_the_measurement_with_a_floor() -> None:
    assert online_runner.next_estimate(None) == online_runner.DEFAULT_BATCH_ESTIMATE_S
    assert online_runner.next_estimate(100) == 125
    assert online_runner.next_estimate(10) == online_runner.MIN_BATCH_ESTIMATE_S


def test_scoring_is_unavailable_without_a_bank_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREYA_BANK_TEAM_KEY", raising=False)
    assert load_scoring(require_key=True) is None
    assert load_scoring(require_key=False) is not None
    assert load_scoring(require_key=False, kit_dir=Path("/nonexistent")) is None
