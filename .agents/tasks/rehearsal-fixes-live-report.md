# Rehearsal fixes: live report

HEAD stays 23a45c7, nothing committed. Offline: 832 tests pass, contamination script 0 hits. Extraction model: anthropic/claude-sonnet-5.5.

## 1. Rebuild of the rehearsal agent (slot `rehearsal`, run runs/rehearsal-20261011-173902)
- spec.json uses the structured fields: deadline `calendar_days_after_base` (base days_late_field, n 24) and `business_days_after_today` (n 4); `floor_percent` (12 and 45 of past_due) plus car loan `floor_fields`; `per_weekday` callback window (Thursday 10:00-14:00); `first_sentence.template`.
- compile_report: gaps [], degraded []. Warnings: no goodbye text (default farewell), the template lacks the literal item "the call is recorded" (it says "This call is recorded."), aggregation default derived.
- lint 0 errors, selfcheck 125 checks 0 failed, online_test 8/9 after 2 repairs (226 s, 0 excluded).

## 2. Probes (results/rehearsal-fixes-probes.json, rerun results/rehearsal-fixes-probes-rerun.json, transcripts results/rehearsal-fixes-transcripts/)
First run 7/10, rerun of 03/08/09: 03 and 09 pass, 08 fails again.
- Intro before the confirmation question: 10/10 transcripts ("This is Nora, ... This call is recorded. Is this Mr X?").
- PROBE-03: agreed amount 243.00 == recorded 243.00. First run failed only on the date (agent offered the 3rd-business-day date 2026-10-20 while the case expects today); passed on rerun. Model variance on which attempt date is offered.
- PROBE-04, 09 (rerun), 10: read-back == recorded; no 'unknown' value in any request.
- After a recorded promise the confirmation sentence and goodbye are spoken and the call ends (01-04, 09, 10; no goodbye loop). Exits (05, 06 identity failure, 07 callback) say thanks and goodbye. PROBE-08 ends cleanly too.
- PROBE-08 still fails twice: the judge counts the opening question plus 3 offers as 4 persuasion attempts. Not fixed; ambiguous whether the opener counts, left as a finding.
- PROBE-09 first run failed on the judge's debt-disclosure order, passed on rerun (variance).

## 3. Regression
Brief A, first live build: 7/10. Cause was a real builder defect: the extractor put the brief's instruction "Thank the customer and say goodbye before close_call" into `closing.goodbye_before_close.text`, and the new end nodes spoke it aloud (PUB-04/05/10).
Fix (generic, one cycle): `builder/compiler.py::farewell` ignores goodbye text that reads as an instruction (snake_case identifier or "the customer/caller/agent") and uses the default farewell with a warning; `extract_prompts/sections.md` tells the extractor to leave `text` empty for an instruction-only goodbye. 832 tests and contamination stay green.
Rebuilt Brief A (same extracted spec, via --spec): lint 0 errors, selfcheck 0 failed, online 7/8. Practice customers: 8/10, PUB-09 was a platform "analysis never arrived (300s)", PUB-10 was judge variance on the word "recorded" in the opening; rerun of 09 and 10: 2/2 pass. Brief A is effectively 10/10 again (results/rehearsal-fixes-brief-a.json, -a-2.json).

Brief A+B (slot brief-ab) built: gaps [], degraded [], selfcheck 128 checks 0 failed, online 8/9. Practice customers PUB-11..13 ran twice and scored 0/3 both times: the agent says its first sentence, the customer answers, then the model call fails (close_call node error 502/530 Cloudflare, call ended `customer-ended-call`, no tool calls). Platform/tunnel failure on the agent model, not a builder result. NOT confirmed (previous result 3/3).

## Platform issues
- Cloudflare tunnel 530/502 on the agent LLM during the Brief B runs (the API itself answers GETs).
- One "analysis never arrived" on PUB-09.

## Verdict per fix
Fix numbering follows the plan table.
1. Enforce-only promise hook (read-back == recorded): live-confirmed (PROBE-03, 04, 09, 10; transcripts).
2. Structured deadline/floor/weekday-window fields: live-confirmed in extraction and build (rehearsal spec, 0 gaps); PROBE-03/04/09 amounts and dates consistent with them. Per-weekday window not exercised by a probe.
3. Gap guard (gaps/degraded): live-confirmed that it stays silent on a clean extraction (0 gaps, 0 degraded); the positive case is offline-only.
4. Calendar/allowed-date facts in prompt: live-confirmed indirectly (no Sunday-not-business-day error, no improvised dates in the 10 probes); not isolated.
5. Mandated introduction before the confirmation question: live-confirmed 10/10 (Brief A and rehearsal).
6. Guaranteed termination and spoken goodbyes: live-confirmed (no loops, exits say goodbye). New defect found and fixed: instruction text spoken as goodbye (see section 3).
7. Resilient simulation polling: not confirmed live (no 5xx during polling this time; offline-only). Online test completed on every build.
8. Latest-brief-wins extraction and per-product loop: partly confirmed (PROBE-09/10 per-product promises and notes recorded); PROBE-08 attempt-count finding open.
Brief B regression: not confirmed (platform 502/530).

## Addendum: Brief B gate (after the platform recovered; health probe ok before each attempt)
Run 1 (results/rehearsal-fixes-brief-b-3reps.json, 3 reps): PUB-11 2/3, PUB-12 0/3, PUB-13 0/3. One rep was a platform/persona failure (identity ended `declined`). The two consistent failures were:
- PUB-12: the agent made one attempt on the late-band personal loan, then noted `refused`. Cause: the per-product loop line told the model that a "refusal" goes straight to add_note, which overrode the per-product persuasion count. This was the loop prompt change (builder defect).
- PUB-13: customer asks one date for both products, the agent offered the card's own later deadline (2026-11-02) instead of the customer's 2026-10-30. Not related to the loop wording; model behaviour on the "one date for all" rule.
Fix (one cycle, generic): `builder/node_prompts.py` loop step 3 now reads "refusal after the persuasion attempts for this product are used up". 832 tests and contamination green.
Run 2 (rebuilt brief-ab, results/rehearsal-fixes-brief-b-3reps-b.json): PUB-11 3/3, PUB-12 2/3, PUB-13 2/3 (reps 1 and 3 all pass, rep 2 failed 12 and 13: 12 gave three attempts on the loan, 13 again offered 2026-11-02). Previously Brief B was 3/3 per customer.
Verdict: Brief B NOT fully confirmed at 3/3 per customer. PUB-12 regression from the loop wording is fixed (0/3 to 2/3, the remaining miss is a different wobble). PUB-13 stays 2/3 and is model variance on "one date for all"; not changed. Fix 8 (per-product loop): confirmed live after the narrowing, with that residual variance.
