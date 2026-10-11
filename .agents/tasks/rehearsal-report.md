# Rehearsal report (hands-off builder run)

Sources: rehearsal/run-record.json, rehearsal/evaluation.json, rehearsal/NOTES.md. Run dir: runs/rehearsal-20261011-132458. Extraction model: anthropic/claude-sonnet-5.5.

## Timing (run-record.json)

| Stage | Seconds | Budget (s) |
|---|---|---|
| load_inputs | 0.00 | 10 |
| extract_spec | 104.44 | 540 |
| validate_spec | 0.00 | 10 |
| compile_workflow | 0.08 | 60 |
| lint | 0.00 | 30 |
| offline_checks | 0.08 | 120 |
| dry_run | 1.01 | 120 |
| save | 1.98 | 120 |
| read_back | 0.72 | 60 |
| export | 0.38 | 60 |
| smoke_test | 5.91 | 180 |
| online_test | 174.65 | 600 |

Total wall: 289.5 s vs 1800 s (30 min): within. Internal TOTAL_BUDGET_S 1500 s: within. No stage over budget. Exit code 0. 12 of 12 stages ok.

Note: the stage times for online_test (174.65 s) cover a batch that ended in 504s, so the run is flagged degraded.

## ZERO-MANUAL-STEPS: PASS (for the build)

run-record.json: manual_action_needed=false, retried=false, exit 0. Caveat: the build itself needed no human action. The post-build evaluation did need workarounds (my own tooling, not the builder): waiting out a stuck simulation (~2 h) on the platform that held the single simulation slot, and a 5xx GET-retry wrapper in rehearsal/probe_runner.py. Details in evaluation.json incidents_and_manual_steps.

## Build quality

Real workflow (no fallback). Lint 0 errors / 0 warnings, 0 compile gaps, selfcheck 111 checks, 0 failed. Extraction: 0 repairs, 16 spec warnings, 26 verbatim rules, 6 mandated sentences (6 of 6 present in workflow.json), 8 coverage-missing tokens (all example-call identifiers, harmless). One compile warning: closing.aggregation.default missing.

## Spec vs brief mismatches (evaluation.json, 10 total: 8 mismatch, 1 behaviour, 1 interpretation)

| # | Topic | Brief | Spec / agent | Severity |
|---|---|---|---|---|
| 1 | Deadline, recent band (2-23 days late) | (now - days_late) + 24 days | Spec uses today + 24 and ignores days_late; facts feed wrong date (p1_deadline=2026-11-07 for 4, 5, 6, 12, 20 days late) | high |
| 2 | Floors card 12%, loan 45% | percent of past due | Only car_loan structured; facts say "unknown"; pre-hook overwrites amount with full amount (PROBE-03: agreed 243.00, recorded 540.0) | high |
| 3 | Persuasion attempt 3 date | 3rd business day (2026-10-20) | placeholder next_business_day; model improvised 2026-10-21 (PROBE-08) | medium |
| 4 | Callback window Thursday | 10-17 Sun-Wed, 10-14 Thu | single window to 17:00; Thursday limit only in prose; node contradicts itself (at most once vs twice). Behaviour right in 3/3 probes | low |
| 5 | First sentence | Nora, digital assistant, recorded | identity node asks "Is this ...?" first; intro second. 30/30 sim transcripts | medium |
| 6 | Thanks and goodbye before close_call | rule 46 | end_call messages lack thanks; PROBE-05/06 no goodbye; PROBE-07 "Goodbye." only; run1 PROBE-01 nothing after create_promise | medium |
| 7 | Recorded vs read back | record only what was confirmed | pre-hook clamps date/amount to its own facts silently (PROBE-04 read 11-07 recorded 11-05; PROBE-03 243 -> 540; PROBE-09 loan 10-18 expected, 10-20 recorded) | high |
| 8 | Per-product deadline cap | own deadline recorded, read-back says so | rule correct but deadlines wrong (consequence of 1); PROBE-09 3/3 both recorded 10-20, no capping mentioned | high |
| 9 | Business-day calendar in model | Sun-Thu business days | Spec right; model said Sunday is not a business day in 6/6 relevant calls | medium (behaviour) |
| 10 | Hardship | note and carry on; support transfer only if customer struggling and agrees | Spec adds an active support offer on every hardship mention and keeps "death in the family" trigger from brief-1 | low (interpretation) |

Matches (not mismatches): bands and attempt counts, aged deadline = 4th business day (correct 3/3), calendar and holidays, identity order and 2-wrong limit, outcome table and reasons, 6 of 6 quoted sentences, product names in debt statement.

## Planted ambiguity (Karim vs Priya)

Resolved in favour of Priya's closing table: precedence transfer > any_promise > all_notes_equal(paid) > first_note_not_equal. Surfaced in spec warnings and extraction_notes.md. Verdict: the expected sensible default and flagged at build time. Not flagged as open inside the agent, and not probed with a refuse-big / promise-small call. Other conflicts (callback window, hardship, add_note, wrong-answer count, duplicated "already paid") resolved with brief-2 winning; see mismatches 4 and 10.

## online_test

Generated 5 cases (identity_lockout, promise_full, amount_floor, third_party_callback, intent_no_talk_after_verification). Passed 0, scored 0, excluded 5, all infra: GET /api/v2/simulations/<id> returned 504 for the whole batch. No repairs tried or kept; baseline kept. Skipped kinds: date clamping and multi-product. Result: no signal, degraded=true.

## Probes (10 probes, hand-computed expected values, 30 transcripts in results/rehearsal-transcripts/)

Strict (ledger + platform judge): 0/10 in run1, 0/10 and 0/10 in two stability reps. The judge's first check (Nora + recording in first sentence) fails on all 30 calls, so strict is dominated by mismatch 5.
Ledger-only: run1 6/10 (PROBE-01, 02, 05, 06, 07, 08); rep1 7/10 (adds PROBE-10, keeps 01, 02, 05-08); rep2 6/10 (01, 05, 06, 07, 08, 10).
Stable pass both reps: 01, 05, 06, 07, 08, 10. Flaky: 02. Failed in all runs: 03, 04, 09.

Transcript findings:
- Opening sentence: wrong order in 30/30 (question first, Nora/recording second).
- Forbidden words before verification: clean in 30/30.
- Read-back: present in every promise call; ISO dates; in PROBE-03/04/09 the read-back is not what gets recorded.
- Confirmation: exact singular/plural sentences used when a promise was recorded and the agent spoke; missing in run1 PROBE-01. Multi-product asked once ("Shall I record this?").
- Closing: thanks/goodbye missing on identity-failure and callback exits; goodbye loop without close_call in PROBE-10 reps 1 and 2 (60+ turns) and PROBE-08 rep 2. PROBE-10 run1 wrongly closed at once as no_promise/refused.
- Identity limit: exactly 2 wrong answers in 6/6.
- Platform slowness: median slowest agent turn 19 s (rep1), 14 s (rep2).

## Ranked defects and generic fixes

1. [compiler] Pre-hook on create_promise silently overwrites amount/date with its own (possibly unknown or wrong) facts (mismatches 2, 7, 8). Fix: hook must never replace a value with "unknown"; only reject (and make the agent re-read and re-confirm) when the value breaks a known rule, and never write a value different from the confirmed read-back.
2. [extraction] Date rules that depend on per-account variables (days_late offsets) and percent-of-balance floors are not representable, so they fall to prose (mismatches 1, 2, 3). Fix: extend the spec schema generically with offset-from-variable date rules and percent-of-field floors, and a prompt instruction to map each brief formula to a structured rule.
3. [pipeline] Extractor flagged the schema gap in extraction_notes but the compile neither failed nor fell back, so a known-wrong date/floor shipped. Fix: treat "rule exists in verbatim prose but has no structural form" as a compile gap that blocks or at least marks the build degraded.
4. [prompt] Agent ignores its own facts on business days (Sunday, 6/6) and improvises dates. Fix: have the compiler compute and inject explicit date lists (next business days, deadline) instead of relying on the model's calendar reasoning.
5. [prompt] Opening order: the identity node asks the confirmation question before the mandated introduction (30/30). Fix: generic compile rule that opening.first_sentence.must_include is emitted before any other utterance, whatever entry path.
6. [hook] No guaranteed termination: goodbye loops without close_call (PROBE-10 x2, PROBE-08 rep 2), and a call that ended with no confirmation or goodbye (PROBE-01 run1). Fix: a post-hook/turn counter that forces close_call after the final goodbye and requires the confirmation sentence after a successful create_promise.
7. [compiler] Compiled end_call messages omit required thanks (goodbye_before_close.required). Fix: compile the thanks and goodbye from the spec into every exit message.
8. [prompt] PROBE-10 run1 closed immediately as no_promise/refused with notes and promise lost: unstable multi-product handling. Fix: compile an explicit per-product loop with required notes before close.
9. [compiler] Single structured callback window cannot express a per-weekday window (mismatch 4); node contradicts itself (once vs twice). Fix: allow per-weekday windows in the schema and lint for contradictory repeat counts.
10. [pipeline] Compile warning closing.aggregation.default is missing was not acted on. Fix: provide a default in the compiler or make it an error.
11. [pipeline] online_test gives zero signal on a backend 504 batch (5 of 5 excluded) and the generator skipped date-clamping and multi-product cases. Fix: retry/resume polling on 5xx, and require generated coverage of those case kinds.
12. [budget] No budget breach. Only note: online_test spent 174.65 s on a batch that produced no result; consider bounding it by a failure count rather than waiting through the batch.
13. [prompt] Hardship adds a support offer and keeps a brief-1 trigger (mismatch 10). Fix: extraction instruction to prefer the latest brief and not add offers not stated.

## Workspace check

- uv run pytest -q: PASS, 679 passed in 24.46 s.
- git status --porcelain: only untracked files under rehearsal/ and .agents/tasks/ (plus earlier .agents/tasks files); NO changes under builder/, build.py, tools/, scripts/ or tests/. Clean under frozen paths. (results/ is not listed in git status output, so it is ignored or unchanged. Not verified further.)
- No git commit made.

## Verdict: NO-GO for freezing the builder

The build mechanics are good: hands-off, 289.5 s of 1800 s, 12 of 12 stages ok, 0 repairs, ambiguity resolved sensibly. But the compiled agent records wrong values in three reproducible cases (PROBE-03, 04, 09: wrong amount, wrong deadline, no cap), caused by defects 1-3 which are generic, and the online test gave no signal. Fix defects 1-3 (and ideally 5-6), then re-run the rehearsal on a fresh brief before freezing.
