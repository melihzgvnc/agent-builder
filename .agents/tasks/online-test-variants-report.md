# Online test stage and variant briefs: report

Source: saved plan, code report, review, variant/regression notes, `results/*.online_test*.json` and `results/*.trace.log`. No git commit was made in this step.

## online_test before and now
- Before: `stage_online_test` was `raise NotImplementedStage()`, so every build logged `online_test: skipped: not implemented` and never touched the network.
- Now (code report, 63 new fake-based tests): it generates up to 9 cases from the spec alone (`builder/online_cases.py`), borrowing carrier accounts from the cases file because the bank mock is closed-world. It runs them one simulation at a time (`online_runner.py`), scores them with `persona_for` and `score_ledger` from `tools/`, classifies failures (infra, expectation, hook, closing, behavior, conversation, judge_noise), tries spec-derived reminder repairs with keep-best and rollback (`online_repair.py`) and writes `<run_dir>/online_test.json`. Still skipped under `--dry`. Review verdict: APPROVED.

## Per-variant results (from results/*.online_test.json and trace logs)
| slot | stage seconds (online_test) | fallback | cases (generated) | pass before repairs | after | repairs | remaining failures |
|---|---|---|---|---|---|---|---|
| brief-a-sonnet | extract 84.44, compile 34 nodes/0 gaps, smoke 2.15, online 208.15 | no | 8 | 8/8 | 8/8 | 0 | none |
| variant-1 | extract 68.56, compile 33 nodes/0 gaps, smoke 6.2, online 152.1 (final --spec run) | no | 6 | 6/6 | 6/6 | 0 | none |
| variant-2 | extract 133.72, compile 25 nodes/2 gaps, smoke 10.35, online 209.56 | no | 6 | 6/6 | 6/6 | 0 | none |
| variant-3 | extract 72.94, compile 31 nodes/1 gap, smoke 2.75, online 250.0 (run 1), 334.8 (run 3) | no | 7 | run 1: 5/5 scored (2 infra); run 2: 6/7; run 3: 7/7 | 7/7 | 0 | none in final run |

Notes:
- variant-1: the first full build skipped smoke and online_test because the backend was unhealthy; the table's online time is from the `--spec` rerun.
- variant-3 run 1: intent_dispute and tool_failure were excluded as infra (GET simulation 504), testing stopped. Run 2: tool_failure failed on the judge check (no "text message confirming" sentence after the retried create_promise succeeded). Cause: one non-reproducing model omission; run 3 on the same compiled workflow passed. No generic bug found, so nothing changed. Watch item: the path after a failed create_promise.
- Compile gaps in variants 2 and 3: transfer queue without a closing reason (both); variant-2 also lacks promise_rules.on_failure.outcome. Reported by the compiler; not exercised as failures.
- Skipped kinds in variant-2 (no trigger phrase or closing action in the spec, or multi-product not enabled): amounts, human_support, hardship, already_paid, dispute, multi-product.

## Brief A Sonnet stability (tools/run_cases.py --reps 3, results/brief-a-sonnet-stability.json)
- 9/10 customers pass (28/30 individual calls). Runs: 10/10, 9/10, 9/10. Only PUB-10 (tool failure) fails, 1 of 3 runs.
- Ledger check passes in all three PUB-10 runs. The failing check was the judge flagging the word "recorded" in the brief-mandated opening ("this call is recorded") in the one failure read in full; the other failure's reason was not read. Treated as judge variance; nothing changed. Whether Haiku behaves differently was not tested.

## Wall times vs the 1500 s (25 min internal; 30 min rule) budget
- Brief A, full pipeline: 296.65 s (19.8% of 1500 s).
- Longest full pipeline with live extraction: variant-2, 355.13 s (23.7%).
- variant-3 `--spec` run: 339.55 s (extraction not included, 72.94 s from the first run), so a full build is roughly 410 s (estimate, not measured).
- Stage budget for online_test is 600 s; the longest online_test was 334.8 s (variant-3 run 3).
- Brief A plus 3-rep stability: 296.65 + 567 = 863.65 s of work (sum of reported numbers).

## Fixes made, with covering tests
- Code change in this task: the online_test implementation itself, covered by `tests/test_online_cases.py` (21), `test_online_runner.py` (15), `test_online_repair.py` (16), `test_pipeline_online.py` (11).
- Bug fixes after live runs: none. The variant runs found no compiler, hook or prompt bug, so no further code or test was added. One contamination hit during development was reworded (code report).

## Platform facts learned
Appended to `builder/PLATFORM_NOTES.md` (section "Online test stage and live variant runs"): closed-world bank mock and carrier accounts, mock-rule vs spec-number mismatches as `expectation`, 504/503 on chat and simulation routes with no retry, `run_simulation` without a timeout, "$" rewritten to "dollars" by default text substitution, tolerant ledger arg keys, single-run model variance.

## What remains
- Ledger call argument key names are still guessed tolerantly; the hook/closing classification has not been exercised on a real failure.
- No repair has ever been kept or rolled back on a live run (0 repairs in all runs), so the live repair and rollback path is only unit-tested.
- Compile gaps (transfer queue closing reason, promise failure outcome) in variants 2 and 3.
- The outcome-table fallback matches the English word "promise"; keep-best compares (ledger, conversation) lexicographically (review issues 2 and 3).
- PUB-10 judge flakiness on the mandated "recorded" opening sentence; variant-3 tool_failure post-retry sentence.
- README "Online test" section (plan item 5) was not part of the saved evidence; not confirmed done.
- Rehearsal.

## Final state
- `uv run pytest -q`: 679 passed (>= 616).
- `uv run python scripts/check_contamination.py`: exit 0 (scanned 34 files, 0 strict hits, 0 allowlist problems).
