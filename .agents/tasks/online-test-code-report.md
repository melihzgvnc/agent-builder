# Online test stage: code report (part 1, iteration 1)

No git commit made. No live/network run in this step; everything is unit-tested with fakes.

## What changed
- `builder/online_cases.py` (new, pure): `Carrier`, `GenCase`, `load_carriers` (tolerant), `generate_cases(spec, carriers, limit=9)`.
  Kinds in priority order: identity_lockout, promise_full, date_over_limit (clamping), amount_floor (below floor, else too-much),
  third_party_callback, intent_* (up to 2, by precedence, after-verification only), multi_one_date, tool_failure.
  Expectations come from `builder.engine` (`check_promise_date`, `full_for`, `floor_for`, `attempt_offer`, `one_date_for_all`,
  `check_callback`, `aggregate_outcome`) and `node_prompts.note_reason_for_close`. Skipped kinds return a reason string.
  Bank limit: the mock is closed-world, so each case borrows a "carrier" account from the cases file (`--cases` or `<root>/public-test-set.json`).
- `builder/online_runner.py` (new): `Budget` (injectable clock; soft `deadline` = min(stage budget - 45 s, total deadline), `hard_deadline` for saves),
  `load_scoring` (imports `persona_for` from `tools/run_cases.py`, `score_ledger`/`http` from `tools/score.py`; None without key/tools),
  `HttpBank`, persona cache (`.personas.json`, same `id:hash` format), `run_batch` (one simulation at a time, daemon thread with timeout,
  no retries of any error; errors become `infra`), `CaseResult`.
- `builder/online_repair.py` (new): `classify` (infra, expectation, hook, closing, behavior, conversation, judge_noise), spec-derived
  `reminder:*` repairs (idempotent marker, max 3, input not mutated), `drop_optional_feature` through `selfcheck.repair_spec` (only after a hook/closing
  failure in two consecutive rounds, gated by an injected recompile), and `run_loop` (keep-best: a repair is kept only if strictly better on the re-tested
  subset, else the best workflow is re-saved; max 2 repair rounds; judge-noise gets one single-case rerun; budget guard before every batch/save).
- `builder/pipeline.py`: real `stage_online_test` (skips with DEGRADED when no carriers / no scoring tools or bank key; skips on fallback workflow,
  no spec, `backend_unhealthy`); `BuildContext.bank` and `run_build(bank=...)`; `stage_smoke_test` sets `ctx.results["backend_unhealthy"]`;
  `describe_input` covers online_test; `_online_recompile` gate (no lint error, selfcheck failures not worse). Writes `<run_dir>/online_test.json`,
  `trace.log_test("online", ...)`, and on a kept repair rewrites `workflow.json` and re-exports. `STAGE_ORDER`, `NETWORK_STAGES`, `STAGE_BUDGETS`,
  `ESSENTIAL_STAGES` unchanged (stays skipped under `--dry`).
- Tests (new): `tests/online_fakes.py` (fake Freya + fake bank + ledger builder), `tests/test_online_cases.py` (21), `tests/test_online_runner.py` (15),
  `tests/test_online_repair.py` (16), `tests/test_pipeline_online.py` (11). 63 new tests, all without network.

## Commands run and results
- `uv run pytest -q` -> **679 passed** (baseline 616; +63).
- `uv run python scripts/check_contamination.py` -> exit 0, "scanned 34 files, 267 tokens (205 strict): 0 strict hit(s), 0 allowlist problem(s)".
  (One strict hit appeared mid-way on a generated sentence copied from a brief fixture; reworded, then clean.)
- `uv run pytest -q tests/test_contamination.py tests/test_check_contamination.py` -> 13 passed.
- `tests/test_pipeline_compile.py::test_network_stage_order` unchanged and passing (no carriers file in tmp root, so no client call from online_test).
- `tools/` and `public-test-set.json` untouched; no extracted spec edited.

## Coverage map (requirement -> test)
- case generation / skip reasons / determinism / limit / cross-check with public cases: `test_online_cases.py`
- expectation computation (outcome pair in outcome table, business-day dates <= deadline, amounts = full/floor, callback passes engine, wrong answers differ and equal the limit): `test_expectations_follow_the_spec_and_engine`, `test_wrong_identity_answers_...` (5 fixture specs)
- scoring reuse, judge fail, no verdict, infra, no 502 retry, abandoned sim, persona cache: `test_online_runner.py`
- budget guard: `test_stage_deadline_...`, `test_no_batch_starts_...`, `test_reserve_switches_...`, `test_exhausted_total_budget_...`, `test_no_total_deadline_...`, `test_no_batch_runs_when_the_budget_is_already_gone`
- classification table, reminders (spec-derived, idempotent, capped), drop-feature gate, improve/regress/tie/save-fail/rollback-fail/budget-while-live/expectation-infra-only/noise/2-round cap: `test_online_repair.py`
- wiring: full run, dry, no carriers, unhealthy, missing key, improve/regress sync of `workflow.json` and export, crash containment, slow-stage abandon, exhausted budget, spec-repair gate: `test_pipeline_online.py`

## Known limits to review
- Cross-check finding: generated `promise_full` and `identity_lockout` expectations equal the public cases PUB-01 / PUB-03; clamping, callback, floor, intents and the multi-product date use the customer's own choices, so they intentionally differ from the public expectations (documented in the test).
- Clamping case is skipped when the spec's deadline is not a business day (a promise could not be recorded there).
- Ledger call argument key names (`args`) are guessed tolerantly (`args/request/input/body/params`); only a live run can confirm the bank's real shape. A mismatch only weakens hook/expectation classification (falls to closing/behavior), never scoring.
- The bank's own deadline rules may differ from a spec's numbers; such rejections are class `expectation` and excluded from the score.
- README / PLATFORM_NOTES docs (plan item 5) and the live scratch run (item 6) are not part of this step.
