# Spec-driven online test stage in builder/pipeline.py

`stage_online_test` is now real. It generates up to 9 cases from the spec dict (`online_cases.py`), runs them one simulation at a time against the saved agent (`online_runner.py`), classifies failures, tries spec-derived reminder repairs with keep-best and rollback (`online_repair.py`), and writes `online_test.json`. The stage keeps its name and slot in `STAGE_ORDER` and stays skipped under `--dry`. Scoring goes through `persona_for`, `score_ledger` and `http` imported from `tools/`. The coder's recorded evidence is 679 tests passing (63 new, all on fakes), contamination script exit 0 with 0 strict hits, and `tools/` untouched. I did not re-run the suites.

Watch for: ledger argument key names are guessed (possible, only a live run confirms); the outcome-table fallback matches the English word "promise" (likely, minor); an abandoned simulation thread can keep the workspace busy (confirmed, logged by design).

**Verdict**: APPROVED

## High-level view

Cases come from the spec alone. The bank mock is closed-world, so each case borrows a carrier account from the cases file. Only the customer script and expected result are generated. Expectations are computed through `builder.engine` (`check_promise_date`, `full_for`, `floor_for`, `attempt_offer`, `one_date_for_all`, `check_callback`, `aggregate_outcome`) and the outcome table, not hardcoded.

Scoring is delegated to the organisers' kit and the ledger is the ground truth. The judge only adds a conversation verdict. Judge-noise cases (ledger fine, no verdict) get one single-case rerun and are not counted as failures.

Time is guarded by a `Budget` with a soft deadline (stage budget minus 45 s, capped by the total deadline). Reserves of 30 s, or 150 s while a repaired workflow is live, are held back for export or rollback. Saves are allowed until the hard deadline. A repair is kept only when strictly better on the re-tested subset. Otherwise the best workflow is re-saved. Failed rollbacks, exhausted budget and dead backends are logged as DEGRADED and leave the last saved workflow in place.

<details>
<summary>Issues (4)</summary>

1. **Guessed ledger arg keys** (possible) — `args/request/input/body/params` are tried tolerantly. Confirm on the first live run; a mismatch only weakens hook/closing classification, not scoring.
2. **English keyword in outcome fallback** (likely, non-blocking) — `_agg` matches "promise" in the outcome-table `when` text. Prefer a structural link if the spec offers one.
3. **Lexicographic keep-best key** (likely, non-blocking) — `after > before` compares (ledger, conversation) tuples, so a ledger gain can be accepted despite a conversation loss. Acceptable because the ledger is ground truth; note it.
4. **Abandoned simulation thread** (confirmed, logged) — a timed-out simulation may keep the workspace busy for the next run. Mitigated by the budget reserve and an infra classification.

</details>

<details><summary>Details</summary>

### Spec-only generation and engine-computed expectations
No brief is read. `generate_cases(spec, carriers)` selects kinds from spec fields (identity limit, promise rules, multi-product, tools, callbacks, intents). A kind is skipped with a reason string when the spec lacks the feature. Carrier selection picks the smallest account that satisfies a predicate evaluated with the engine. The cross-check against the public cases shows promise_full and identity_lockout coincide with PUB-01 and PUB-03, which supports correctness. The other kinds intentionally differ.

### Scoring reuse
`load_scoring` imports `run_cases.persona_for`, `score.score_ledger` and `score.http`. No scoring logic is duplicated. `CaseResult.passed` requires both ledger and conversation. Ledger failures are never masked by the judge. Missing key or tools degrades to a skip rather than a crash.

### One simulation at a time and the republish reserve
`run_batch` runs one simulation per batch (5 cases) in a daemon thread joined for `remaining - reserve`. Nothing is retried. Batch starts need `remaining - reserve >= estimate`, with the estimate learned from the previous batch. Repairs also require `can_mutate`. Because the soft deadline sits 45 s inside the hard one, a final republish fits.

### Rollback behavior
After a failed or tied repair, `rollback` re-saves `best_wf` if the hard deadline allows. Otherwise it records `restore_failed` and logs DEGRADED. The pipeline updates `ctx.results["saved"]` from the last save. `workflow.json` and the export are rewritten only when the kept workflow differs from the baseline, which stays correct because a rolled-back run returns the baseline.

### Wiring and tests
`stage_smoke_test` sets `backend_unhealthy` so the stage skips on a dead backend. A fake client and fake bank in `tests/online_fakes.py` cover each behavior (improve, regress, tie, save-fail, rollback-fail, budget exhaustion, crash, slow stage, dry, no carriers, missing key). I did not run these tests myself. Not tested: a real bank ledger shape and real simulation timings, which only a live run can show.

### Contamination
Reminder texts and skip reasons are assembled from spec fields or generic sentences. The coder's recorded contamination run shows 0 strict hits. I did not re-run it.

</details>

<details><summary>File map</summary>

- builder/online_cases.py: new, spec-driven case generation
- builder/online_runner.py: new, budget, bank adapter, batch runner
- builder/online_repair.py: new, classification, reminders, keep-best loop
- builder/pipeline.py: real stage, `bank` context field, backend_unhealthy flag
- tests/online_fakes.py, test_online_cases.py, test_online_runner.py, test_online_repair.py, test_pipeline_online.py: new

Full diff: `git diff builder/pipeline.py` plus the untracked files above.

</details>
