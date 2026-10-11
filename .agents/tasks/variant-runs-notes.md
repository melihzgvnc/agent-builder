# Variant runs (Part 2): online_test on briefs variant 1-3

Extraction model for every live extraction: anthropic/claude-sonnet-5.5 (no --spec on first run). Slots: variant-1, variant-2, variant-3 (new). No git commit. No compiler/hook/prompt bug was found, so no code change and no new unit test.
Results JSONs: results/variant-N.online_test*.json, results/variant-N.spec.json, results/variant-N.trace.log (trace of the final run).

## Backend incidents (logged loudly)
- Chat route 504 / 503 on several occasions (smoke probe timeouts at 30 s on variant-1 four times in a row, a 503 "API key validation temporarily unavailable" and a PUT 504 on a --spec rebuild, and 504s on GET /simulations for 2 variant-3 cases). Chat route was never retried inside a stage; I waited ~4 min and re-ran from the saved spec (--spec) so extraction variance did not enter. Probe with variant-3 workflow also 504'd at that moment, so it was the backend, not variant-1.
- Saved workflow per slot is the compiled one from the saved spec; no repair was ever kept (no repairs were needed).

## Per variant

### variant-1 (Summit Bank, 1 product, DOB then phone)
- Extraction: no fallback (extract_spec ok, 68.56 s; coverage call 10.78 s), 17 warnings, verbatim_rules=15, 4 brief tokens not found in spec after coverage pass.
- Stage times (first full build, 99.9 s total): extract 68.56, validate 0, compile 0.06 (33 nodes, 0 gaps), lint 0, offline checks 0.05 (88 checks, 0 failed), dry_run 0.23, save 0.51, read_back 0.26, export 0.15. Smoke + online_test were skipped (backend unhealthy) in that run.
- Final run (--spec results/variant-1.spec.json): save 1.08, smoke 6.2 s, online_test 152.1 s (160.2 s total).
- Generated cases: 6 (identity_lockout, promise_full, third_party_callback, intent_hardship, intent_already_paid, tool_failure).
- Pass before repairs: 6/6. After: 6/6 (0 repairs tried, kept baseline). Remaining failures: none.
- Transcripts read: promise_full, intent_already_paid (call ids in results/variant-1.online_test.json). Opening, DOB-then-phone verification, amount, ISO date read-back, text-message sentence and goodbye all follow the brief. already_paid said "Payments can take one business day to appear", took no promise, closed.

### variant-2 (Apex Trust, up to four products, callback window, restructuring transfer)
- Extraction: no fallback, extract_spec 133.72 s, 16 warnings, verbatim_rules=18, 6 brief tokens not found.
- Stage times (total 355.1 s): compile 0.07 (25 nodes, 2 gaps: transfer queue 'support' has no closing reason; promise_rules.on_failure.outcome missing), offline checks 0.09 (108 checks, 0 failed), dry_run 0.27, save 0.52, read_back 0.37, export 0.17, smoke 10.35, online_test 209.56.
- Generated cases: 6 (identity_lockout, promise_full, date_over_limit, third_party_callback, intent_restructuring_request, tool_failure). Skipped kinds with reason: amounts, human_support, hardship, already_paid, dispute (no trigger phrase or closing action in the spec), multi-product (not enabled).
- Pass before: 6/6. After: 6/6 (0 repairs). Remaining failures: none.
- Transcripts read: promise_full, date_over_limit (clamped to 2026-10-28, offered the deadline), third_party_callback (asked for callback time, scheduled 2026-10-15 09:00, closed not_reached/callback). Sanity notes, not failures:
  - Amounts are spoken as "dollars420.00": the brief writes "$" and the platform default textSubstitutions rewrites "$" to "dollars" with no space. Platform behaviour on chat transcripts, not something the compiler emits; left alone.
  - "Can you promise on all four products today?" is asked even with one late product. The brief mandates this sentence verbatim (rule 25), the extractor kept it as written.
  - Callback weekday hours (Thu 09:00-16:00, Fri unavailable) were stored as the widest range 08:00-18:00 by the schema; the extractor warned about it. Not exercised by a generated case.

### variant-3 (River Bank, 1 late product in test data, dispute / already paid intents)
- Extraction: no fallback, extract_spec 72.94 s, 18 warnings, verbatim_rules=18, 4 brief tokens not found.
- Stage times (first build, 327.5 s total): compile 0.07 (31 nodes, 1 gap, same 'support' queue gap), offline 0.06 (94 checks, 0 failed, 1 warning, 1 skipped), dry_run 0.28, save 0.74, read_back 0.36, export 0.24, smoke 2.75, online_test 250.0.
- Run 1: 5/5 scored pass, 2 cases (intent_dispute, tool_failure) excluded as infra (GET simulation 504), testing stopped. Run 2 (--spec): 6/7, tool_failure failed (judge: after the retried create_promise succeeded the agent went to close_call without saying "You will receive a text message confirming these promises"; the stage budget did not allow a repair round). Run 3 (--spec, same compiled workflow): 7/7 in 334.8 s, tool_failure passes with create_promise called twice.
- Pass before: 7/7 (run 3; run 2 was 6/7). After: 7/7 (0 repairs tried, kept baseline).
- Classification of the run-2 failure: single non-reproducing model omission (same workflow, 1 fail in 2 samples; variant-1/-2 pass the same case), so no generic bug identified and nothing changed. Watch item: the post-retry path after a failed create_promise is the weakest spot; if it reproduces, the reminder repair in online_repair would be the first thing to try.
- Transcripts read: date_over_limit (clamped to 2026-11-13, accepted, confirmed), intent_dispute (agent says only "Goodbye." after "This debt is not mine"; the judge passed it; the brief's wording for dispute was not re-checked line by line), tool_failure (run 2, above).

## Summary table
| slot | cases | pass before repairs | pass after | repairs |
|---|---|---|---|---|
| variant-1 | 6 | 6/6 | 6/6 | 0 |
| variant-2 | 6 | 6/6 | 6/6 | 0 |
| variant-3 | 7 | 7/7 (run 3; 6/7 in run 2, 5/5 + 2 infra in run 1) | 7/7 | 0 |

## Checks
- `uv run pytest -q`: 679 passed. `uv run python scripts/check_contamination.py`: 0 strict hits. runs/ folders deleted after copying the final traces to results/.
